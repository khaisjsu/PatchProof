"""GitHub client: the gh CLI token is looked up once, not per request."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import github


def test_gh_cli_token_is_cached(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    github._gh_cli_token.cache_clear()
    calls = {"n": 0}

    def fake_run(*_a, **_k):
        calls["n"] += 1
        return SimpleNamespace(returncode=0, stdout=b"gho_abc\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    try:
        assert [github._token() for _ in range(4)] == ["gho_abc"] * 4
        assert calls["n"] == 1
    finally:
        github._gh_cli_token.cache_clear()


def test_env_token_wins_without_spawning(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", " ghp_env ")
    github._gh_cli_token.cache_clear()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert github._token() == "ghp_env"


def test_comments_request_full_pages(monkeypatch):
    seen = []

    def fake_get(path):
        seen.append(path)
        if path.endswith("/comments?per_page=100"):
            return [{"user": {"login": "a"}, "body": "hi"}]
        return {"title": "t", "body": "b", "comments": 1, "labels": []}

    monkeypatch.setattr(github, "_get", fake_get)
    issue = github.fetch_issue("o", "r", 1)
    assert issue["comments"] == [{"author": "a", "body": "hi"}]
    assert seen[1] == "/repos/o/r/issues/1/comments?per_page=100"
