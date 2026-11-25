"""The agent loop with a scripted model and a fake sandbox."""

from __future__ import annotations

import pytest

import agent
from conftest import FakeSandbox, ScriptedClient, text_block, tool_block
from tracing import Tracer

PATCH = "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1,2 @@\n x = 1\n+patched\n"


def fix_script(*, edit_after_tests: bool = False) -> list:
    turns = [
        [tool_block("read_file", {"path": "pkg/mod.py"}, 1)],
        [tool_block("apply_patch", {"diff": PATCH}, 2)],
        [tool_block("run_tests", {}, 3)],
    ]
    if edit_after_tests:
        turns.append([tool_block("write_file", {"path": "pkg/mod.py", "content": "x = 9\n"}, 4)])
    turns.append([text_block("Fixed it.\nVERDICT: FIXED")])
    return turns


@pytest.fixture
def sandbox(monkeypatch) -> FakeSandbox:
    sb = FakeSandbox()
    monkeypatch.setattr(agent, "Sandbox", lambda ref=None: sb)
    return sb


def run(issue, client, **kw):
    return agent.run_issue(issue, client=client, tracer=Tracer(None), **kw)


def test_unchanged_tree_reuses_green_suite(issue, sandbox):
    res = run(issue, ScriptedClient(fix_script()))
    assert res.status == "fixed"
    assert res.verification == "reused"
    assert len(sandbox.pytest_runs) == 1          # only the model's run; no repeat
    assert res.tests_passed == 1096
    assert res.files_changed == ["pkg/mod.py"]


def test_reuse_can_be_disabled(issue, sandbox):
    res = run(issue, ScriptedClient(fix_script()), reuse_green_suite=False)
    assert res.status == "fixed"
    assert res.verification == "rerun"
    assert len(sandbox.pytest_runs) == 2


def test_edit_after_green_run_forces_rerun(issue, sandbox):
    res = run(issue, ScriptedClient(fix_script(edit_after_tests=True)))
    assert res.verification == "rerun"
    assert len(sandbox.pytest_runs) == 2


def test_false_fixed_claim_is_still_caught(issue, sandbox):
    # The model claims FIXED, but the harness-run suite is red.
    sandbox.suite_ok = False
    res = run(issue, ScriptedClient(fix_script()))
    assert res.model_verdict == "FIXED"
    assert res.status == "failed"
    assert res.verification == "rerun"            # red results are never reused


def test_no_changes_skips_the_suite(issue, sandbox):
    client = ScriptedClient([[text_block("Not actionable.\nVERDICT: SKIP -- a question")]])
    res = run(issue, client)
    assert res.status == "skipped"
    assert res.verification == "none"
    assert sandbox.pytest_runs == []


def test_cancel_stops_before_next_model_call(issue, sandbox):
    client = ScriptedClient(fix_script())
    asked = {"n": 0}

    def should_cancel() -> bool:
        asked["n"] += 1
        return asked["n"] > 1                     # let exactly one turn through

    res = run(issue, client, should_cancel=should_cancel)
    assert res.status == "cancelled"
    assert res.stop_reason == "cancelled"
    assert client.calls == 1
    assert sandbox.pytest_runs == []


def test_events_report_progress(issue, sandbox):
    events = []
    run(issue, ScriptedClient(fix_script()), on_event=events.append)
    phases = [e["phase"] for e in events]
    assert phases[0] == "sandbox"
    assert phases.count("tool") == 3
    assert phases[-1] == "done"
    tools = [e for e in events if e["phase"] == "tool"]
    assert [e["tool"] for e in tools] == ["read_file", "apply_patch", "run_tests"]
    assert tools[-1]["n"] == 3
    assert events[-1]["status"] == "fixed"
    assert all(e["cost_usd"] >= 0 for e in events)


def test_a_broken_event_callback_cannot_break_the_run(issue, sandbox):
    def boom(_):
        raise RuntimeError("UI went away")

    res = run(issue, ScriptedClient(fix_script()), on_event=boom)
    assert res.status == "fixed"


def test_tool_budget_still_enforced(issue, sandbox):
    turns = [[tool_block("read_file", {"path": "pkg/mod.py"}, i)] for i in range(5)]
    res = run(issue, ScriptedClient(turns), max_tool_calls=3)
    assert res.stop_reason == "tool_budget_exhausted"
    assert res.tool_call_count == 3
