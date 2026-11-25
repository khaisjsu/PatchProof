"""The shared tracer is built once, and a failed build is retried later."""

from __future__ import annotations

import tracing


def test_tracer_built_once(monkeypatch):
    monkeypatch.setattr(tracing, "_cached", None)
    built = {"n": 0}

    def fake_make():
        built["n"] += 1
        return tracing.Tracer(object())          # "enabled"

    monkeypatch.setattr(tracing, "make_tracer", fake_make)
    first = tracing.get_tracer()
    assert all(tracing.get_tracer() is first for _ in range(10))
    assert built["n"] == 1
    assert tracing.cached_tracer_enabled() is True


def test_disabled_tracer_is_retried_after_ttl(monkeypatch):
    monkeypatch.setattr(tracing, "_cached", None)
    monkeypatch.setattr(tracing, "make_tracer", lambda: tracing.Tracer(None))
    now = {"t": 1000.0}
    monkeypatch.setattr(tracing.time, "monotonic", lambda: now["t"])

    first = tracing.get_tracer()
    assert tracing.get_tracer() is first          # within the TTL: cached
    now["t"] += tracing._RETRY_DISABLED_S + 1
    assert tracing.get_tracer() is not first      # Langfuse may be up now


def test_env_change_rebuilds(monkeypatch):
    monkeypatch.setattr(tracing, "_cached", None)
    monkeypatch.setattr(tracing, "make_tracer", lambda: tracing.Tracer(None))
    monkeypatch.setenv("LANGFUSE_ENABLED", "1")
    a = tracing.get_tracer()
    monkeypatch.setenv("LANGFUSE_ENABLED", "0")
    assert tracing.get_tracer() is not a


def test_cached_state_unknown_before_first_build(monkeypatch):
    monkeypatch.setattr(tracing, "_cached", None)
    assert tracing.cached_tracer_enabled() is None
