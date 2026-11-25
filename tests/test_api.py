"""The HTTP service: jobs, live progress, cancel, de-duplication, run history."""

from __future__ import annotations

import functools
import threading
import time

import pytest
from fastapi.testclient import TestClient

import api
import store
from agent import AgentResult
from github import GitHubError

KEY = "test-key"
H = {"X-API-Key": KEY}
URL = "https://github.com/acme/widget/issues/7"


class FakeRun:
    """Replaces run_issue. Emits progress, honours cancel, and can be held
    'running' until the test releases it."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = 0
        self.hold = False

    def __call__(self, issue, *, on_event=None, should_cancel=None, **_kw) -> AgentResult:
        self.calls += 1
        self.started.set()
        on_event({"phase": "sandbox", "t": 0.0, "cost_usd": 0.0})
        for n in range(1, 4):
            if should_cancel():
                return self._result(issue, "cancelled", "cancelled")
            on_event({"phase": "llm", "turn": n, "t": n, "cost_usd": 0.01 * n})
            on_event({"phase": "tool", "n": n, "tool": "read_file", "ok": True,
                      "t": n, "cost_usd": 0.01 * n})
            if self.hold:
                deadline = time.time() + 5
                while not self.release.is_set() and not should_cancel() and time.time() < deadline:
                    time.sleep(0.01)
        on_event({"phase": "done", "status": "fixed", "t": 4.0, "cost_usd": 0.03})
        return self._result(issue, "fixed", "model_finished")

    @staticmethod
    def _result(issue, status, stop) -> AgentResult:
        return AgentResult(
            issue_id=issue["id"], status=status, stop_reason=stop, diff="--- a/x\n+++ b/x\n",
            test_output="", tool_call_count=3, duration_seconds=4.0, tokens_used=100,
            cost_usd=0.03, files_changed=["x"], tests_passed=10, tests_failed=0,
            verification="reused" if status == "fixed" else "none",
        )


@pytest.fixture
def fake_run(monkeypatch) -> FakeRun:
    fr = FakeRun()
    monkeypatch.setattr(api, "run_issue", fr)
    return fr


@pytest.fixture
def client(monkeypatch, tmp_path, fake_run):
    monkeypatch.setenv("TRIAGE_API_KEY", KEY)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LANGFUSE_ENABLED", "0")
    db = tmp_path / "runs.sqlite"
    monkeypatch.setattr(api, "log_run", functools.partial(store.log_run, path=db))
    monkeypatch.setattr(api, "recent", functools.partial(store.recent, path=db))
    monkeypatch.setattr(api, "get_run", functools.partial(store.get_run, path=db))

    fetches = {"n": 0}

    def fake_fetch(url):
        fetches["n"] += 1
        if "missing" in url:
            raise GitHubError("GET /repos/acme/widget/issues/999 -> 404")
        owner, repo, number = api.parse_issue_url(url)
        return {"id": f"{owner}/{repo}#{number}", "number": number,
                "title": "Widget breaks", "body": "", "comments": []}

    monkeypatch.setattr(api, "fetch_issue_by_url", fake_fetch)
    monkeypatch.setattr(api, "jobs", api.JobManager())
    api.reset_rate_limit()
    with TestClient(api.app) as c:
        c.fetches = fetches
        yield c
    api.reset_rate_limit()


def wait_for(c, job_id, statuses=("done", "error", "cancelled"), timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = c.get(f"/jobs/{job_id}", headers=H).json()
        if job["status"] in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job stuck in {job['status']}")


def test_auth_closed_without_key(client, monkeypatch):
    monkeypatch.delenv("TRIAGE_API_KEY")
    assert client.post("/jobs", json={"issue_url": URL}).status_code == 503


def test_auth_rejects_bad_key(client):
    r = client.post("/jobs", json={"issue_url": URL}, headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_health_is_cheap_and_reports_queue(client):
    body = client.get("/health").json()
    assert body["worker_alive"] is True
    assert body["queued_jobs"] == 0


def test_job_runs_and_reports_progress(client):
    r = client.post("/jobs", json={"issue_url": URL}, headers=H)
    assert r.status_code == 202
    job = wait_for(client, r.json()["id"])
    assert job["status"] == "done"
    assert job["result"]["status"] == "fixed"
    assert job["result"]["verification"] == "reused"
    assert job["progress"]["tool_calls"] == 3
    assert [e["phase"] for e in job["events"]][-1] == "done"
    # Logged to the run log, diff included.
    run_id = job["result"]["run_id"]
    assert run_id
    detail = client.get(f"/runs/{run_id}", headers=H).json()
    assert detail["diff"].startswith("--- a/x")
    assert detail["files_changed"] == ["x"]
    listed = client.get("/runs", headers=H).json()
    assert listed[0]["id"] == run_id and "diff" not in listed[0]


def test_identical_request_joins_the_running_job(client, fake_run):
    fake_run.hold = True
    first = client.post("/jobs", json={"issue_url": URL}, headers=H).json()
    fake_run.started.wait(2)
    second = client.post("/jobs", json={"issue_url": URL + "#frag"}, headers=H).json()
    assert second["id"] == first["id"]
    assert second["deduplicated"] is True
    fake_run.release.set()
    wait_for(client, first["id"])
    assert fake_run.calls == 1
    assert client.fetches["n"] == 1          # the duplicate did not even re-fetch


def test_different_options_are_not_deduplicated(client, fake_run):
    a = client.post("/jobs", json={"issue_url": URL}, headers=H).json()
    b = client.post("/jobs", json={"issue_url": URL, "max_tool_calls": 10}, headers=H).json()
    assert a["id"] != b["id"]
    wait_for(client, b["id"])


def test_cancel_running_job(client, fake_run):
    fake_run.hold = True
    job = client.post("/jobs", json={"issue_url": URL}, headers=H).json()
    fake_run.started.wait(2)
    r = client.delete(f"/jobs/{job['id']}", headers=H)
    assert r.status_code == 202 and r.json()["cancel_requested"] is True
    done = wait_for(client, job["id"])
    assert done["status"] == "cancelled"
    assert done["result"]["status"] == "cancelled"
    # A finished job cannot be cancelled again.
    assert client.delete(f"/jobs/{job['id']}", headers=H).status_code == 409


def test_cancel_queued_job_never_runs(client, fake_run):
    fake_run.hold = True
    running = client.post("/jobs", json={"issue_url": URL}, headers=H).json()
    fake_run.started.wait(2)
    queued = client.post("/jobs", json={"issue_url": URL.replace("/7", "/8")},
                         headers=H).json()
    assert queued["status"] == "queued" and queued["queue_position"] == 1
    cancelled = client.delete(f"/jobs/{queued['id']}", headers=H).json()
    assert cancelled["status"] == "cancelled"
    fake_run.release.set()
    wait_for(client, running["id"])
    time.sleep(0.1)
    assert fake_run.calls == 1


def test_bad_issue_refunds_the_rate_limit_slot(client, monkeypatch):
    monkeypatch.setattr(api, "RATE_LIMIT_MAX", 1)
    bad = client.post("/jobs", json={"issue_url": URL.replace("/7", "/999") + "missing"},
                      headers=H)
    assert bad.status_code == 400
    ok = client.post("/jobs", json={"issue_url": URL}, headers=H)
    assert ok.status_code == 202                # the failed fetch did not use the slot
    wait_for(client, ok.json()["id"])
    third = client.post("/jobs", json={"issue_url": URL.replace("/7", "/9")}, headers=H)
    assert third.status_code == 429


def test_not_an_issue_url(client):
    r = client.post("/jobs", json={"issue_url": "https://example.com/nope"}, headers=H)
    assert r.status_code == 400


def test_blocking_run_endpoint_still_works(client):
    r = client.post("/run", json={"issue_url": URL}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "fixed" and body["tests_passed"] == 10 and body["job_id"]


def test_worker_survives_a_crashing_run(client, monkeypatch):
    def crash(*_a, **_k):
        raise RuntimeError("docker daemon went away")

    monkeypatch.setattr(api, "run_issue", crash)
    job = client.post("/jobs", json={"issue_url": URL}, headers=H).json()
    failed = wait_for(client, job["id"])
    assert failed["status"] == "error" and "docker daemon" in failed["error"]
    assert client.get("/health").json()["worker_alive"] is True


def test_list_jobs_newest_first(client):
    ids = [client.post("/jobs", json={"issue_url": URL.replace("/7", f"/{n}")},
                       headers=H).json()["id"] for n in (1, 2)]
    for i in ids:
        wait_for(client, i)
    listed = client.get("/jobs", headers=H).json()
    assert [j["id"] for j in listed[:2]] == ids[::-1]
    assert listed[0]["events"] is None          # list view omits event logs
