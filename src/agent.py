"""The agent loop: one GitHub issue in, one structured result out.

The control flow is deliberately explicit -- no orchestration framework -- so
that every budget check, every stopping condition, and every place the model
could mislead us is visible in this file.

Three limits are enforced here, not suggested to the model:
  * MAX_TOOL_CALLS tool calls per issue
  * a wall-clock timeout for the whole run
  * MAX_PATCH_ATTEMPTS failed test runs before we stop

And one trust rule: the model's `VERDICT: FIXED` is a claim, not a result. The
harness decides the status from a suite run it executed itself. When the model's
own last full-suite run (also executed by the harness, through run_tests) was
green and the working tree is byte-identical since, that observation is reused
instead of paying for an identical second run; any change at all forces a fresh
run. See Toolbox.reusable_suite_result.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import pathlib
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from github import fetch_issue_by_url  # noqa: E402
from llm import MODEL_FIX, Usage, make_client  # noqa: E402
from prompts import (  # noqa: E402
    MAX_PATCH_ATTEMPTS,
    MAX_TOOL_CALLS,
    SYSTEM_PROMPT,
    build_issue_prompt,
)
from sandbox import Sandbox  # noqa: E402
from tools import TOOL_SCHEMAS, Toolbox  # noqa: E402
from tracing import Tracer, get_tracer, summarize_messages  # noqa: E402

DEFAULT_TIMEOUT_S = 300
MAX_TOKENS = 16000

_VERDICT_RE = re.compile(r"VERDICT:\s*(FIXED|SKIP|GIVE_UP)\s*(?:--|—|-)?\s*(.*)", re.I)
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)


# Progress callback: receives small dicts such as {"phase": "tool", "n": 3, ...}.
EventFn = Callable[[dict[str, Any]], None]


@dataclass
class AgentResult:
    issue_id: str
    status: str                      # fixed | failed | skipped | cancelled
    stop_reason: str                 # why the loop ended
    diff: str
    test_output: str
    tool_call_count: int
    duration_seconds: float
    tokens_used: int
    cost_usd: float
    base_ref: str | None = None
    trace_id: str | None = None
    trace_url: str | None = None
    model_verdict: str | None = None
    files_changed: list[str] = field(default_factory=list)
    tests_passed: int = 0
    tests_failed: int = 0
    touched_tests_only: bool = False
    # How the final status was verified: "rerun" (harness ran the suite at the
    # end), "reused" (tree unchanged since a green harness-executed full run),
    # or "none" (no changes to verify, triage decline, or cancelled).
    verification: str = "rerun"
    transcript: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.issue_id}  {self.status.upper():8} "
            f"({self.stop_reason})  {self.tool_call_count} calls  "
            f"{self.duration_seconds:.0f}s  ${self.cost_usd:.4f}"
        )


def _clear_cache_marks(messages: list[dict[str, Any]]) -> None:
    """Only the newest turn should carry a cache breakpoint (max 4 per request)."""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)


def _mark_cache_breakpoint(messages: list[dict[str, Any]]) -> None:
    """Cache the conversation prefix so each turn re-reads rather than re-pays."""
    if not messages:
        return
    content = messages[-1].get("content")
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = {"type": "ephemeral"}


def _text_of(blocks: Any) -> str:
    out = []
    for b in blocks or []:
        if getattr(b, "type", None) == "text":
            out.append(b.text)
    return "\n".join(out).strip()


def run_issue(
    issue: dict[str, Any],
    *,
    client: Any = None,
    model: str = MODEL_FIX,
    base_ref: str | None = None,
    max_tool_calls: int = MAX_TOOL_CALLS,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    verbose: bool = False,
    tracer: Tracer | None = None,
    use_triage: bool = False,
    on_event: EventFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
    reuse_green_suite: bool = True,
) -> AgentResult:
    """Run one issue to completion.

    `base_ref` matters for closed issues: the sandbox must start at the commit
    *before* the real fix landed, otherwise the bug is already fixed on main and
    the run measures nothing. It defaults to the image's pristine HEAD, which is
    only correct for issues that are still open.

    `on_event` receives progress events (used by the API's job view);
    `should_cancel` is polled before every model turn, and a True stops the run
    with status "cancelled" before any more money is spent. Pass
    `reuse_green_suite=False` to force the final suite run unconditionally.
    """
    client = client or make_client()
    tracer = tracer if tracer is not None else get_tracer()
    usage = Usage()
    started = time.perf_counter()

    def elapsed() -> float:
        return time.perf_counter() - started

    def log(msg: str) -> None:
        if verbose:
            print(f"  [{elapsed():5.1f}s] {msg}", flush=True)

    def emit(phase: str, **data: Any) -> None:
        # Progress reporting must never be able to break a run.
        if on_event is None:
            return
        try:
            on_event(
                {"phase": phase, "t": round(elapsed(), 1),
                 "cost_usd": round(usage.cost_usd, 6)} | data
            )
        except Exception:  # noqa: BLE001
            pass

    def cancelled() -> bool:
        if should_cancel is None:
            return False
        try:
            return bool(should_cancel())
        except Exception:  # noqa: BLE001
            return False

    # Pre-flight: a ~$0.002 Haiku call that can decline before the expensive
    # loop starts. Biased toward attempting -- see triage.py.
    if use_triage:
        from triage import triage_issue

        emit("triage")
        with tracer.observation(
            name="triage", as_type="span", input={"issue_id": issue["id"]}
        ) as tspan:
            verdict = triage_issue(
                client,
                title=issue.get("title", ""),
                body=issue.get("body", ""),
                comments=issue.get("comments"),
                usage=usage,
            )
            tspan.update(output=verdict)
        log(f"triage: {verdict['decision']} "
            f"(conf {verdict['confidence']}) ${verdict['cost_usd']:.5f}")

        if not verdict["attempt"]:
            tracer.flush()
            emit("done", status="skipped", stop_reason="declined_by_triage")
            return AgentResult(
                issue_id=issue["id"],
                status="skipped",
                stop_reason="declined_by_triage",
                diff="",
                test_output="",
                tool_call_count=0,
                duration_seconds=round(elapsed(), 1),
                tokens_used=usage.input_tokens + usage.output_tokens,
                cost_usd=round(usage.cost_usd, 6),
                base_ref=base_ref,
                model_verdict="SKIP",
                verification="none",
                transcript=[{"triage": verdict}],
            )

    transcript: list[dict[str, Any]] = []
    tool_calls = 0
    turns = 0
    failed_test_runs = 0
    stop_reason = "model_finished"
    final_text = ""

    trace_id: str | None = None
    trace_url: str | None = None

    # ExitStack rather than nested `with`s: the tracing contexts wrap the whole
    # run, but nesting them would re-indent the entire loop for no benefit.
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            tracer.attributes(
                session_id=issue["id"],
                tags=["agent-run", f"model:{model}"],
                trace_name=f"triage {issue['id']}",
            )
        )
        root = stack.enter_context(
            tracer.observation(
                name=f"agent-run {issue['id']}",
                as_type="agent",
                input={"issue_id": issue["id"], "title": issue.get("title", "")},
                metadata={
                    "base_ref": base_ref,
                    "max_tool_calls": max_tool_calls,
                    "timeout_s": timeout_s,
                    "model": model,
                },
            )
        )
        trace_id = tracer.trace_id()
        trace_url = tracer.trace_url()

        emit("sandbox", note="starting container")
        sb = stack.enter_context(Sandbox(ref=base_ref))
        tb = Toolbox(sb)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": build_issue_prompt(
                            number=issue["number"],
                            title=issue["title"],
                            body=issue["body"],
                            comments=issue.get("comments"),
                        ),
                    }
                ],
            }
        ]

        while True:
            if cancelled():
                stop_reason = "cancelled"
                log("STOP: cancelled by caller")
                break
            if elapsed() > timeout_s:
                stop_reason = "timeout"
                log(f"STOP: wall-clock timeout after {timeout_s}s")
                break
            if tool_calls >= max_tool_calls:
                stop_reason = "tool_budget_exhausted"
                log(f"STOP: hit the {max_tool_calls} tool call budget")
                break
            if failed_test_runs >= MAX_PATCH_ATTEMPTS:
                stop_reason = "patch_attempts_exhausted"
                log(f"STOP: {MAX_PATCH_ATTEMPTS} failed test runs")
                break

            _clear_cache_marks(messages)
            _mark_cache_breakpoint(messages)

            turns += 1
            turn = turns
            emit("llm", turn=turn, tool_calls=tool_calls)
            with tracer.observation(
                name=f"llm-turn-{turn}",
                as_type="generation",
                model=model,
                input=summarize_messages(messages),
                metadata={"tool_calls_so_far": tool_calls},
            ) as gen:
                resp = client.messages.create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=[
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    thinking={"type": "adaptive", "display": "summarized"},
                    tools=TOOL_SCHEMAS,
                    messages=messages,
                )
                call_cost = usage.add(model, resp.usage)
                gen.update(
                    output=_text_of(resp.content)
                    or f"(tool_use: {[getattr(b, 'name', '') for b in resp.content if getattr(b, 'type', None) == 'tool_use']})",
                    usage_details={
                        "input": resp.usage.input_tokens,
                        "output": resp.usage.output_tokens,
                        "cache_read_input_tokens": getattr(
                            resp.usage, "cache_read_input_tokens", 0
                        ) or 0,
                    },
                    cost_details={"total": call_cost},
                    metadata={"stop_reason": resp.stop_reason},
                )
            log(f"llm turn: stop={resp.stop_reason} ${call_cost:.4f}")

            if resp.stop_reason == "refusal":
                stop_reason = "model_refusal"
                final_text = _text_of(resp.content)
                break

            messages.append({"role": "assistant", "content": resp.content})

            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                final_text = _text_of(resp.content)
                stop_reason = (
                    "max_tokens" if resp.stop_reason == "max_tokens" else "model_finished"
                )
                log(f"model finished: {final_text[:120]!r}")
                break

            results = []
            for block in tool_uses:
                if tool_calls >= max_tool_calls:
                    # Budget ran out mid-turn; answer the remaining calls
                    # honestly rather than silently dropping them.
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "is_error": True,
                            "content": json.dumps(
                                {"ok": False, "error": "tool call budget exhausted"}
                            ),
                        }
                    )
                    continue

                tool_calls += 1
                args = block.input if isinstance(block.input, dict) else {}
                log(f"tool {tool_calls}/{max_tool_calls}: {block.name}({list(args)})")

                with tracer.observation(
                    name=f"{tool_calls:02d}-{block.name}",
                    as_type="tool",
                    input=args,
                ) as tool_span:
                    out = tb.dispatch(block.name, args)
                    tool_span.update(
                        output=out,
                        level="DEFAULT" if out.get("ok") else "WARNING",
                        status_message=None if out.get("ok") else str(out.get("error", ""))[:200],
                    )

                if block.name == "run_tests" and not out.get("ok"):
                    failed_test_runs += 1

                emit("tool", n=tool_calls, tool=block.name, ok=bool(out.get("ok")),
                     max_tool_calls=max_tool_calls)

                transcript.append(
                    {"n": tool_calls, "tool": block.name, "args": args, "ok": out.get("ok")}
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(out)[:20000],
                    }
                )

            messages.append({"role": "user", "content": results})

        # -- verification: never take the model's word for it ---------------
        verification = "rerun"
        if stop_reason == "cancelled":
            # The caller asked us to stop spending. Record what was changed,
            # skip the suite, and let the stack tear the container down.
            final_diff = tb.full_diff()
            final_tests: dict[str, Any] = {}
            verification = "none"
        else:
            log("verifying: harness checks the suite itself")
            emit("verify")
            with tracer.observation(
                name="verify-harness-suite",
                as_type="span",
                input={"note": "harness-executed suite; the model's VERDICT is only a claim"},
            ) as verify_span:
                final_diff = tb.full_diff()
                reused = None
                if reuse_green_suite and final_diff.get("ok") and not final_diff.get("empty"):
                    reused = tb.reusable_suite_result(tb.tree_fingerprint())
                if reused is not None:
                    final_tests = reused
                    verification = "reused"
                    log("tree unchanged since the last green full-suite run: reusing it")
                elif final_diff.get("ok") and final_diff.get("empty"):
                    # Nothing changed, so there is no fix to verify; the status
                    # below is decided by has_changes alone. Skip the suite.
                    final_tests = {}
                    verification = "none"
                else:
                    final_tests = tb.run_tests()
                verify_span.update(
                    output={
                        "verification": verification,
                        "suite_ok": final_tests.get("ok"),
                        "passed": final_tests.get("passed"),
                        "failed": final_tests.get("failed"),
                        "diff_bytes": final_diff.get("diff_bytes"),
                    }
                )

        diff_text = final_diff.get("diff", "") if final_diff.get("ok") else ""
        has_changes = bool(diff_text.strip())
        files = sorted(
            f
            for f in set(_DIFF_FILE_RE.findall(diff_text))
            # Patch-tool leftovers are not changes the agent intended.
            if not f.endswith((".rej", ".orig"))
        )

        m = _VERDICT_RE.search(final_text or "")
        verdict = m.group(1).upper() if m else None

        if stop_reason == "cancelled":
            status = "cancelled"
        elif not has_changes:
            status = "skipped" if verdict == "SKIP" else "failed"
        elif final_tests.get("ok"):
            status = "fixed"
        else:
            status = "failed"

        # Update the root span before the stack closes it.
        root.update(
            output={
                "status": status,
                "stop_reason": stop_reason,
                "model_verdict": verdict,
                "files_changed": files,
                "tool_calls": tool_calls,
                "verification": verification,
            },
            metadata={
                "cost_usd": round(usage.cost_usd, 6),
                "tokens": usage.input_tokens + usage.output_tokens,
                "duration_s": round(elapsed(), 1),
            },
            level="DEFAULT" if status == "fixed" else "WARNING",
            status_message=None if status == "fixed" else stop_reason,
        )

    tracer.flush()
    emit("done", status=status, stop_reason=stop_reason, verification=verification)

    return AgentResult(
        issue_id=issue["id"],
        status=status,
        stop_reason=stop_reason,
        diff=diff_text,
        test_output=final_tests.get("output", ""),
        tool_call_count=tool_calls,
        duration_seconds=round(elapsed(), 1),
        tokens_used=usage.input_tokens + usage.output_tokens,
        cost_usd=round(usage.cost_usd, 6),
        base_ref=base_ref,
        trace_id=trace_id,
        trace_url=trace_url,
        model_verdict=verdict,
        files_changed=files,
        tests_passed=final_tests.get("passed", 0),
        tests_failed=final_tests.get("failed", 0) + final_tests.get("errors", 0),
        touched_tests_only=bool(files) and all(f.startswith("tests/") for f in files),
        verification=verification,
        transcript=transcript,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the triage agent against one issue.")
    ap.add_argument("issue_url", help="https://github.com/owner/repo/issues/N")
    ap.add_argument(
        "--base-ref",
        help="commit to start from; for a closed issue use the fix commit's parent",
    )
    ap.add_argument("--max-tool-calls", type=int, default=MAX_TOOL_CALLS)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--json", help="write the full result to this path")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument(
        "--always-rerun-suite",
        action="store_true",
        help="always run the full suite at the end, even if the tree is "
             "unchanged since the model's last green full run",
    )
    args = ap.parse_args()

    issue = fetch_issue_by_url(args.issue_url)
    print(f"{issue['id']}: {issue['title']}")

    result = run_issue(
        issue,
        base_ref=args.base_ref,
        max_tool_calls=args.max_tool_calls,
        timeout_s=args.timeout,
        verbose=not args.quiet,
        reuse_green_suite=not args.always_rerun_suite,
    )
    print("\n" + result.summary())
    if result.files_changed:
        print("files changed:", ", ".join(result.files_changed))

    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(asdict(result), indent=2), encoding="utf-8"
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
