"""Tiny client for the GreenChecker API, used by the dashboard.

Standard library only, so the dashboard image needs nothing extra.

    TRIAGE_API_URL   where the API listens (compose sets http://api:8080;
                     default http://localhost:8000 for a host-side dashboard)
    TRIAGE_API_KEY   the same shared secret the API checks
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "http://localhost:8000"


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ApiClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 timeout: float = 15.0) -> None:
        self.base_url = (base_url or os.environ.get("TRIAGE_API_URL") or DEFAULT_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("TRIAGE_API_KEY", "")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            raise ApiError(f"{exc.code}: {detail}", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ApiError(f"cannot reach the API at {self.base_url} ({reason})") from exc

    def health(self) -> dict[str, Any]:
        return self._call("GET", "/health")

    def submit(self, issue_url: str, *, base_ref: str | None = None,
               max_tool_calls: int | None = None, timeout_s: int | None = None,
               use_triage: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {"issue_url": issue_url, "use_triage": use_triage}
        if base_ref:
            body["base_ref"] = base_ref
        if max_tool_calls is not None:
            body["max_tool_calls"] = max_tool_calls
        if timeout_s is not None:
            body["timeout_s"] = timeout_s
        return self._call("POST", "/jobs", body)

    def job(self, job_id: str) -> dict[str, Any]:
        return self._call("GET", f"/jobs/{job_id}")

    def jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._call("GET", f"/jobs?limit={int(limit)}")

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self._call("DELETE", f"/jobs/{job_id}")
