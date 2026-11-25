"""Run log: schema-once, migration of old databases, diff storage."""

from __future__ import annotations

import sqlite3

import pytest

import store

OLD_SCHEMA = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_utc TEXT NOT NULL,
    source TEXT NOT NULL, tag TEXT, issue_id TEXT NOT NULL, status TEXT NOT NULL,
    resolved INTEGER, stop_reason TEXT, model TEXT, tool_call_count INTEGER,
    duration_seconds REAL, tokens_used INTEGER, cost_usd REAL, judge_score INTEGER,
    files_changed TEXT, trace_url TEXT
);
"""

PAYLOAD = {
    "issue_id": "acme/widget#7", "status": "fixed", "stop_reason": "model_finished",
    "tool_call_count": 9, "duration_seconds": 61.2, "tokens_used": 40000,
    "cost_usd": 0.11, "files_changed": ["pkg/mod.py"], "tests_passed": 1096,
    "tests_failed": 0, "diff": "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n",
}


@pytest.fixture(autouse=True)
def sqlite_backend(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_roundtrip_with_diff(tmp_path):
    db = tmp_path / "runs.sqlite"
    run_id = store.log_run(PAYLOAD, source="api", model="m", path=db)
    row = store.get_run(run_id, path=db)
    assert row["diff"] == PAYLOAD["diff"]
    assert row["tests_passed"] == 1096 and row["tests_failed"] == 0


def test_recent_leaves_out_the_diff(tmp_path):
    db = tmp_path / "runs.sqlite"
    store.log_run(PAYLOAD, source="api", path=db)
    [row] = store.recent(path=db)
    assert "diff" not in row
    assert row["tests_passed"] == 1096
    [full] = store.recent(path=db, with_diff=True)
    assert full["diff"] == PAYLOAD["diff"]


def test_old_database_is_migrated_in_place(tmp_path):
    db = tmp_path / "old.sqlite"
    raw = sqlite3.connect(db)
    raw.executescript(OLD_SCHEMA)
    raw.execute("INSERT INTO runs (created_utc, source, issue_id, status) "
                "VALUES ('2026-08-28T00:00:00+00:00', 'eval', 'acme/widget#1', 'failed')")
    raw.commit()
    raw.close()

    old = store.get_run(1, path=db)                     # triggers the migration
    assert old["issue_id"] == "acme/widget#1" and old["diff"] is None
    new_id = store.log_run(PAYLOAD, source="api", path=db)
    assert store.get_run(new_id, path=db)["diff"] == PAYLOAD["diff"]


def test_fill_details_backfills_but_never_overwrites(tmp_path):
    db = tmp_path / "runs.sqlite"
    created = "2026-08-28T20:31:07+00:00"
    bare = PAYLOAD | {"diff": None, "tests_passed": None, "tests_failed": None}
    store.log_run(bare, source="eval", created_utc=created, path=db)

    assert store.fill_details(PAYLOAD["issue_id"], created, PAYLOAD, path=db) == 1
    [row] = store.recent(path=db, with_diff=True)
    assert row["diff"] == PAYLOAD["diff"]

    other = PAYLOAD | {"diff": "something else", "tests_passed": 1}
    assert store.fill_details(PAYLOAD["issue_id"], created, other, path=db) == 0
    [row] = store.recent(path=db, with_diff=True)
    assert row["diff"] == PAYLOAD["diff"] and row["tests_passed"] == 1096


def test_huge_diff_is_clipped(tmp_path):
    db = tmp_path / "runs.sqlite"
    big = "+" + "x" * (store.MAX_STORED_DIFF + 50)
    run_id = store.log_run(PAYLOAD | {"diff": big}, source="api", path=db)
    stored = store.get_run(run_id, path=db)["diff"]
    assert len(stored) < len(big)
    assert "truncated" in stored


def test_schema_ddl_runs_once_per_database(tmp_path, monkeypatch):
    db = tmp_path / "runs.sqlite"
    calls = {"n": 0}
    real = store._existing_columns

    def counting(conn):
        calls["n"] += 1
        return real(conn)

    monkeypatch.setattr(store, "_existing_columns", counting)
    for _ in range(5):
        store.log_run(PAYLOAD, source="api", path=db)
    store.recent(path=db)
    assert calls["n"] == 1
