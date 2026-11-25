"""Shared fakes. Nothing here needs Docker, a network, or an API key."""

from __future__ import annotations

import hashlib
import pathlib
import re
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sandbox import ExecResult  # noqa: E402


@dataclass
class FakeSandbox:
    """Stands in for sandbox.Sandbox: an in-memory "repo" of path -> text.

    Understands exactly the shell commands Toolbox sends.
    """

    files: dict[str, str] = field(default_factory=lambda: {"pkg/mod.py": "x = 1\n"})
    pristine: dict[str, str] = field(default_factory=dict)
    suite_ok: bool = True
    commands: list[str] = field(default_factory=list)
    ref: str | None = None

    def __post_init__(self) -> None:
        self.pristine = dict(self.files)

    # context-manager protocol, like the real one
    def __enter__(self) -> "FakeSandbox":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    @property
    def pytest_runs(self) -> list[str]:
        return [c for c in self.commands if "python -m pytest" in c]

    def _diff(self) -> str:
        out = []
        for path in sorted(set(self.files) | set(self.pristine)):
            if self.files.get(path) != self.pristine.get(path):
                out.append(f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n"
                           f"-{self.pristine.get(path, '')}+{self.files.get(path, '')}")
        return "".join(out)

    def exec(self, command: str, *, timeout: float = 60, stdin: str | None = None,
             workdir: str = "/work/repo") -> ExecResult:
        self.commands.append(command)
        if "sha256sum" in command:
            digest = hashlib.sha256(self._diff().encode()).hexdigest()
            return ExecResult(0, f"{digest}  -\n", "", False)
        if "python -m pytest" in command:
            if self.suite_ok:
                return ExecResult(0, "....\n1096 passed, 1 skipped in 0.9s\n", "", False)
            return ExecResult(1, "FAILED tests/x_test.py::test_y - boom\n"
                                 "1 failed, 1095 passed in 0.9s\n", "", False)
        if command.startswith("git add -A -N . && git diff"):
            return ExecResult(0, self._diff(), "", False)
        m = re.match(r"cat '?([^' ]+)'?( 2>/dev/null)?$", command)
        if m:
            path = m.group(1)
            if path in self.files:
                return ExecResult(0, self.files[path], "", False)
            return ExecResult(1, "", "No such file", False)
        m = re.search(r"&& cat > '?([^' ]+)'?$", command)
        if m:
            self.files[m.group(1)] = stdin or ""
            return ExecResult(0, "", "", False)
        if command.startswith("git apply"):
            # Pretend the patch appends a line to the first file it names.
            target = re.search(r"\+\+\+ b/(\S+)", stdin or "")
            if target:
                path = target.group(1)
                self.files[path] = self.files.get(path, "") + "patched\n"
                return ExecResult(0, "", "", False)
            return ExecResult(1, "", "error: corrupt patch", False)
        if command.startswith("patch -p1"):
            return ExecResult(1, "", "patch: **** Only garbage was found", False)
        return ExecResult(0, "", "", False)


@pytest.fixture
def fake_sandbox() -> FakeSandbox:
    return FakeSandbox()


# -- a scripted Anthropic client ------------------------------------------------


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, args: dict[str, Any], idx: int) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=args, id=f"toolu_{idx}")


class ScriptedClient:
    """client.messages.create() returns the next scripted turn."""

    def __init__(self, turns: list[list[SimpleNamespace]]) -> None:
        self.turns = list(turns)
        self.calls = 0
        self.messages = self

    def create(self, **_: Any) -> SimpleNamespace:
        self.calls += 1
        content = self.turns.pop(0)
        has_tool = any(b.type == "tool_use" for b in content)
        return SimpleNamespace(
            content=content,
            stop_reason="tool_use" if has_tool else "end_turn",
            usage=SimpleNamespace(input_tokens=1000, output_tokens=100,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )


@pytest.fixture
def issue() -> dict[str, Any]:
    return {"id": "acme/widget#7", "number": 7, "title": "Widget breaks",
            "body": "It breaks.", "comments": []}
