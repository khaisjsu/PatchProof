"""GreenChecker dashboard: run issues, watch them live, and inspect the run log.

    streamlit run scripts/dashboard.py

Three tabs:
  * Overview     -- resolve rate, cost and latency over the run log
  * Run an issue -- queue a run through the API and watch it live
  * Run details  -- any logged run's metrics and the patch it produced

The run log is read directly (MySQL via DATABASE_URL, else data/runs.sqlite).
Running issues goes through the API (TRIAGE_API_URL, TRIAGE_API_KEY), so the
dashboard never starts containers or holds an Anthropic key itself.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

import pandas as pd
import streamlit as st

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from api_client import ApiClient, ApiError  # noqa: E402
from envfile import load_env  # noqa: E402
from store import describe, get_run, recent  # noqa: E402

load_env()
st.set_page_config(page_title="GreenChecker", layout="wide")

ACTIVE = ("queued", "running")
STATUS_ICON = {
    "queued": "⏳", "running": "🔄", "done": "✅", "error": "⚠️", "cancelled": "⏹️",
    "fixed": "✅", "failed": "❌", "skipped": "⏭️",
}


# -- data ---------------------------------------------------------------------------


@st.cache_data(ttl=10)
def load() -> pd.DataFrame:
    # Summary columns only: diffs are fetched one run at a time in Run details.
    df = pd.DataFrame(recent(limit=None))
    if df.empty:
        return df
    df["created_utc"] = pd.to_datetime(df["created_utc"], format="mixed", utc=True)
    df["date"] = df["created_utc"].dt.date
    df["succeeded"] = df["resolved"].fillna(df["status"].eq("fixed").astype(int)).astype(int)
    df["tag"] = df["tag"].fillna("")
    return df


@st.cache_data(ttl=600, max_entries=64)
def load_run(run_id: int) -> dict[str, Any] | None:
    # Logged runs never change, so a long cache is safe.
    return get_run(run_id)


def files_of(raw: Any) -> list[str]:
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
        return list(val or [])
    except (TypeError, ValueError):
        return []


@st.cache_resource
def api() -> ApiClient:
    return ApiClient()


# -- header and filters -----------------------------------------------------------------

df = load()

st.title("GreenChecker")
st.caption(
    "Agent reads a GitHub issue, patches the repo in a container, and is scored "
    "against the maintainer's own test suite."
)

view = df
with st.sidebar:
    st.header("Filters")
    if df.empty:
        st.caption("No runs logged yet.")
    else:
        sources = st.multiselect("Source", sorted(df["source"].unique()),
                                 default=sorted(df["source"].unique()))
        tags = st.multiselect("Tag", sorted(t for t in df["tag"].unique() if t))
        include_cancelled = st.checkbox(
            "Include cancelled runs", value=False,
            help="Cancelled runs were stopped on purpose; they are left out of "
                 "the resolve rate by default.",
        )
        view = df[df["source"].isin(sources)]
        if tags:
            view = view[view["tag"].isin(tags)]
        if not include_cancelled:
            view = view[view["status"] != "cancelled"]
    st.divider()
    st.caption(f"Run log: `{describe()}`")
    st.caption(f"API: `{api().base_url}`")

tab_overview, tab_run, tab_details = st.tabs(["Overview", "Run an issue", "Run details"])


# -- overview ----------------------------------------------------------------------------


def render_overview(view: pd.DataFrame) -> None:
    if df.empty:
        st.info(f"No runs logged yet in {describe()}. Queue one from **Run an issue**, "
                "or import past eval results with `python scripts/backfill_runs.py`.")
        return
    if view.empty:
        st.warning("No runs match the current filters.")
        return

    resolved = int(view["succeeded"].sum())
    total = len(view)
    spend = float(view["cost_usd"].fillna(0).sum())

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Runs", total)
    c2.metric("Resolved", f"{resolved}/{total}", f"{resolved / total:.0%}")
    c3.metric("Cost / resolved fix", f"${spend / resolved:.3f}" if resolved else "n/a")
    c4.metric("p50 latency", f"{view['duration_seconds'].quantile(0.5):.0f}s")
    c5.metric("p95 latency", f"{view['duration_seconds'].quantile(0.95):.0f}s")

    st.divider()
    left, right = st.columns(2)
    with left:
        st.subheader("Resolve rate over time")
        by_day = view.groupby("date").agg(runs=("id", "count"), resolved=("succeeded", "sum"))
        by_day["rate"] = by_day["resolved"] / by_day["runs"]
        st.line_chart(by_day[["rate"]], height=260)
        st.caption("Fraction of runs whose patch satisfied the upstream test suite.")
    with right:
        st.subheader("Cost per run")
        st.bar_chart(
            view.sort_values("created_utc").set_index("created_utc")[["cost_usd"]],
            height=260,
        )
        st.caption(f"Total spend across the runs shown: ${spend:.2f}")

    st.divider()
    left2, right2 = st.columns(2)
    with left2:
        st.subheader("Outcome by stop reason")
        st.dataframe(
            view.groupby("stop_reason")
            .agg(runs=("id", "count"), resolved=("succeeded", "sum"),
                 mean_cost=("cost_usd", "mean"), mean_calls=("tool_call_count", "mean"))
            .sort_values("runs", ascending=False)
            .round(3),
            width="stretch",
        )
        st.caption("Runs ending in tool_budget_exhausted are the ones to attack: they "
                   "cost full price whether or not they produce anything.")
    with right2:
        st.subheader("Spend by outcome")
        st.dataframe(
            view.groupby("status")
            .agg(runs=("id", "count"), total_cost=("cost_usd", "sum"),
                 mean_duration=("duration_seconds", "mean"))
            .round(3),
            width="stretch",
        )
        wasted = float(view.loc[view["succeeded"] == 0, "cost_usd"].fillna(0).sum())
        if spend:
            st.caption(f"Spent on runs that resolved nothing: ${wasted:.2f} "
                       f"({wasted / spend:.0%} of total)")

    st.divider()
    st.subheader("Recent runs")
    table = view.head(50).copy()
    table["files"] = table["files_changed"].apply(lambda s: ", ".join(files_of(s)))
    st.dataframe(
        table[[
            "id", "created_utc", "source", "tag", "issue_id", "status", "succeeded",
            "judge_score", "tool_call_count", "duration_seconds", "cost_usd",
            "stop_reason", "files", "trace_url",
        ]],
        width="stretch",
        hide_index=True,
        column_config={
            "id": st.column_config.NumberColumn("run", format="%d"),
            "trace_url": st.column_config.LinkColumn("trace", display_text="open"),
            "cost_usd": st.column_config.NumberColumn("cost", format="$%.4f"),
            "duration_seconds": st.column_config.NumberColumn("sec", format="%.0f"),
            "succeeded": st.column_config.CheckboxColumn("resolved"),
        },
    )
    st.caption("Open **Run details** and pick a run number to see the patch it produced.")


with tab_overview:
    render_overview(view)


# -- run an issue ------------------------------------------------------------------------


def render_result(result: dict[str, Any]) -> None:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Outcome", f"{STATUS_ICON.get(result['status'], '')} {result['status']}")
    c2.metric("Tests", f"{result['tests_passed']} passed",
              f"{result['tests_failed']} failed" if result["tests_failed"] else None,
              delta_color="inverse")
    c3.metric("Cost", f"${result['cost_usd']:.4f}")
    c4.metric("Time", f"{result['duration_seconds']:.0f}s")
    c5.metric("Tool calls", result["tool_call_count"])
    bits = [f"stop reason `{result['stop_reason']}`"]
    if result.get("model_verdict"):
        bits.append(f"model verdict `{result['model_verdict']}`")
    if result.get("verification"):
        bits.append(f"suite check `{result['verification']}`")
    if result.get("run_id"):
        bits.append(f"logged as run **{result['run_id']}**")
    st.caption(" · ".join(bits))
    if result.get("trace_url"):
        st.link_button("Open trace in Langfuse", result["trace_url"])
    if result.get("files_changed"):
        st.write("**Files changed:** " + ", ".join(f"`{f}`" for f in result["files_changed"]))
    if result.get("diff"):
        with st.expander("Patch", expanded=True):
            st.code(result["diff"], language="diff")


def render_job(job: dict[str, Any]) -> None:
    status = job["status"]
    progress = job.get("progress") or {}
    title = job.get("title") or job["issue_id"]
    st.markdown(f"#### {STATUS_ICON.get(status, '')} {job['issue_id']}: {title}")

    if status == "queued":
        st.info(f"Waiting in the queue (position {job.get('queue_position') or '?'}). "
                "One run executes at a time.")
    elif status == "running":
        calls = int(progress.get("tool_calls") or 0)
        cap = int(job["max_tool_calls"])
        phase = progress.get("phase") or "starting"
        label = {
            "sandbox": "starting the sandbox container",
            "triage": "running the triage pre-check",
            "llm": f"model turn {progress.get('turn', '')}",
            "tool": f"tool {calls}/{cap}: {progress.get('last_tool', '')}",
            "verify": "verifying against the full test suite",
            "done": "finishing up",
        }.get(phase, phase)
        if job.get("cancel_requested"):
            label += " (cancel requested; stops before the next model call)"
        st.progress(min(calls / cap, 1.0), text=label)
        c1, c2 = st.columns(2)
        c1.metric("Elapsed", f"{job.get('elapsed_s') or 0:.0f}s")
        c2.metric("Spent so far", f"${progress.get('cost_usd') or 0:.4f}")
    elif status == "error":
        st.error(job.get("error") or "the run crashed")
    elif status == "cancelled" and not job.get("result"):
        st.warning("Cancelled before it started. Nothing was spent.")

    if status in ACTIVE and not job.get("cancel_requested"):
        if st.button("Cancel run", key=f"cancel-{job['id']}"):
            try:
                api().cancel(job["id"])
                st.toast("Cancel requested.")
            except ApiError as exc:
                st.error(str(exc))

    if job.get("result"):
        render_result(job["result"])

    events = job.get("events") or []
    if events:
        with st.expander(f"Event log ({len(events)})", expanded=status == "running"):
            rows = [
                {
                    "t (s)": e.get("t"),
                    "phase": e.get("phase"),
                    "detail": e.get("tool") or (f"turn {e['turn']}" if "turn" in e else "")
                    or e.get("status") or e.get("note") or "",
                    "ok": e.get("ok"),
                    "spent": e.get("cost_usd"),
                }
                for e in events[-40:][::-1]
            ]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                         column_config={"spent": st.column_config.NumberColumn(format="$%.4f")})


def job_panel() -> None:
    job_id = st.session_state.get("job_id")
    if not job_id:
        return
    try:
        job = api().job(job_id)
    except ApiError as exc:
        st.error(f"Could not load job {job_id}: {exc}")
        return
    render_job(job)
    finished = job["status"] not in ACTIVE
    if finished and st.session_state.get("job_live"):
        # Stop polling: a full rerun redefines the fragment without run_every,
        # and refreshes the run log so the new row appears in the other tabs.
        st.session_state["job_live"] = False
        load.clear()
        st.rerun()


def render_run_tab() -> None:
    client = api()
    if not client.configured:
        st.info(
            "Set **TRIAGE_API_KEY** (and **TRIAGE_API_URL** if the API is not at "
            f"`{client.base_url}`) in `.env` to run issues from here. "
            "`docker compose up` wires both for you."
        )
        return
    try:
        health = client.health()
    except ApiError as exc:
        st.warning(f"The API is not reachable: {exc}. Start it with `docker compose up -d`.")
        return

    st.caption(
        f"API ok · rate limit {health.get('rate_limit')} · "
        f"{health.get('queued_jobs', 0)} queued · "
        + (f"running job `{health['running_job']}`" if health.get("running_job") else "idle")
    )

    with st.form("submit", clear_on_submit=False):
        url = st.text_input("GitHub issue URL",
                            placeholder="https://github.com/asottile/pyupgrade/issues/804")
        with st.expander("Options"):
            base_ref = st.text_input(
                "Base commit (optional)",
                help="For a closed issue, the commit before the fix landed. "
                     "Leave empty for an open issue.",
            )
            c1, c2 = st.columns(2)
            max_calls = c1.number_input("Max tool calls", 1, 40, 15)
            timeout_s = c2.number_input("Timeout (seconds)", 30, 1800, 300, step=30)
            use_triage = st.checkbox(
                "Triage first",
                help="A ~$0.002 Haiku check that skips issues it confidently judges "
                     "unfixable, before the expensive loop starts.",
            )
        submitted = st.form_submit_button("Queue run", type="primary")
        st.caption("Each run starts a container and typically costs $0.03–$0.36 in API credit.")

    if submitted:
        if not url.strip():
            st.error("Enter an issue URL.")
        else:
            try:
                job = client.submit(url.strip(), base_ref=base_ref.strip() or None,
                                    max_tool_calls=int(max_calls), timeout_s=int(timeout_s),
                                    use_triage=use_triage)
                st.session_state["job_id"] = job["id"]
                st.session_state["job_live"] = job["status"] in ACTIVE
                if job.get("deduplicated"):
                    st.info("That exact run is already queued or running, so you are "
                            "watching it instead of paying for it twice.")
            except ApiError as exc:
                st.error(str(exc))

    st.divider()
    live = bool(st.session_state.get("job_live"))
    st.fragment(job_panel, run_every=2 if live else None)()

    try:
        recent_jobs = client.jobs(limit=20)
    except ApiError:
        recent_jobs = []
    if recent_jobs:
        st.divider()
        st.subheader("Recent jobs")
        table = pd.DataFrame([
            {
                "job": j["id"],
                "status": f"{STATUS_ICON.get(j['status'], '')} {j['status']}",
                "issue": j["issue_id"],
                "outcome": (j.get("result") or {}).get("status", ""),
                "cost": (j.get("result") or {}).get("cost_usd"),
                "created": j["created_at"],
            }
            for j in recent_jobs
        ])
        st.dataframe(table, hide_index=True, width="stretch",
                     column_config={"cost": st.column_config.NumberColumn(format="$%.4f")})
        ids = [j["id"] for j in recent_jobs]
        current = st.session_state.get("job_id")
        pick = st.selectbox("Watch a job", ids,
                            index=ids.index(current) if current in ids else 0,
                            format_func=lambda i: next(
                                f"{j['id']} · {j['issue_id']} · {j['status']}"
                                for j in recent_jobs if j["id"] == i))
        if pick != current and st.button("Watch"):
            st.session_state["job_id"] = pick
            st.session_state["job_live"] = True
            st.rerun()


with tab_run:
    render_run_tab()


# -- run details ---------------------------------------------------------------------------


def render_details(view: pd.DataFrame) -> None:
    if view.empty:
        st.info("No logged runs to show.")
        return
    options = view["id"].astype(int).tolist()
    labels = {
        int(r.id): f"run {int(r.id)} · {r.issue_id} · {r.status} · {r.created_utc:%Y-%m-%d %H:%M}"
        for r in view.itertuples()
    }
    run_id = st.selectbox("Run", options, format_func=lambda i: labels.get(i, str(i)))
    row = load_run(int(run_id))
    if row is None:
        st.warning("That run is no longer in the log.")
        return

    resolved = row.get("resolved")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Status", f"{STATUS_ICON.get(row['status'], '')} {row['status']}")
    c2.metric("Resolved", "n/a" if resolved is None else ("yes" if resolved else "no"))
    c3.metric("Judge (0-3)", "n/a" if row.get("judge_score") is None else row["judge_score"])
    passed, failed = row.get("tests_passed"), row.get("tests_failed")
    c4.metric("Tests", "n/a" if passed is None else f"{passed} passed",
              f"{failed} failed" if failed else None, delta_color="inverse")
    c5.metric("Cost", f"${(row.get('cost_usd') or 0):.4f}")
    c6.metric("Time", f"{(row.get('duration_seconds') or 0):.0f}s")
    st.caption(
        f"source `{row['source']}`"
        + (f" · tag `{row['tag']}`" if row.get("tag") else "")
        + f" · stop reason `{row.get('stop_reason')}` · {row.get('tool_call_count')} tool calls"
        + f" · model `{row.get('model') or '?'}` · logged {row['created_utc']}"
    )
    if row.get("trace_url"):
        st.link_button("Open trace in Langfuse", row["trace_url"])

    files = files_of(row.get("files_changed"))
    if files:
        st.write("**Files changed:** " + ", ".join(f"`{f}`" for f in files))
    if row.get("diff"):
        st.code(row["diff"], language="diff")
    elif files:
        st.info("This run was logged before diffs were stored. "
                "`python scripts/backfill_runs.py --refresh` fills in diffs for "
                "runs that came from results/eval.")
    else:
        st.caption("This run changed no files.")


with tab_details:
    render_details(view)
