"""Toolbox: test targets, the green-suite cache, and the untruncated diff."""

from __future__ import annotations

import shlex

from tools import Toolbox


def test_single_target_is_one_argument(fake_sandbox):
    # Node ids contain spaces; a string target must stay one argument.
    Toolbox(fake_sandbox).run_tests(target="tests/a_test.py::test_fix[simple case]")
    assert fake_sandbox.pytest_runs[-1].endswith("'tests/a_test.py::test_fix[simple case]'")


def test_list_target_is_several_arguments(fake_sandbox):
    # The #976 bug: two files joined into one string became one bogus path.
    Toolbox(fake_sandbox).run_tests(
        target=["tests/features/dict_literals_test.py", "tests/features/set_literals_test.py"]
    )
    argv = shlex.split(fake_sandbox.pytest_runs[-1])
    assert argv[-2:] == [
        "tests/features/dict_literals_test.py",
        "tests/features/set_literals_test.py",
    ]


def test_spaced_node_id_survives_shell_split(fake_sandbox):
    Toolbox(fake_sandbox).run_tests(target=["tests/a_test.py::test_fix[simple case]"])
    assert shlex.split(fake_sandbox.pytest_runs[-1])[-1] == "tests/a_test.py::test_fix[simple case]"


def test_green_full_run_is_reusable_while_tree_unchanged(fake_sandbox):
    tb = Toolbox(fake_sandbox)
    tb.write_file("pkg/mod.py", "x = 2\n")
    first = tb.run_tests()
    assert first["ok"]
    reused = tb.reusable_suite_result(tb.tree_fingerprint())
    assert reused is not None and reused["passed"] == first["passed"]


def test_edit_after_green_run_invalidates(fake_sandbox):
    tb = Toolbox(fake_sandbox)
    tb.run_tests()
    tb.write_file("pkg/mod.py", "x = 3\n")
    assert tb.reusable_suite_result(tb.tree_fingerprint()) is None


def test_failed_patch_still_invalidates(fake_sandbox):
    # GNU patch can half-apply; any apply_patch call must count as an edit.
    tb = Toolbox(fake_sandbox)
    tb.run_tests()
    out = tb.apply_patch("not a diff at all")
    assert not out["ok"]
    assert tb.reusable_suite_result(tb.tree_fingerprint()) is None


def test_tree_change_without_tool_edit_invalidates(fake_sandbox):
    # e.g. a test run that rewrote a tracked file: the fingerprint catches it.
    tb = Toolbox(fake_sandbox)
    tb.run_tests()
    fake_sandbox.files["pkg/mod.py"] = "changed behind our back\n"
    assert tb.reusable_suite_result(tb.tree_fingerprint()) is None


def test_red_run_is_never_reused(fake_sandbox):
    fake_sandbox.suite_ok = False
    tb = Toolbox(fake_sandbox)
    assert not tb.run_tests()["ok"]
    assert tb.reusable_suite_result(tb.tree_fingerprint()) is None


def test_targeted_run_does_not_populate_cache(fake_sandbox):
    tb = Toolbox(fake_sandbox)
    tb.run_tests(target="tests/a_test.py")
    assert tb.reusable_suite_result(tb.tree_fingerprint()) is None


def test_full_diff_is_not_truncated(fake_sandbox):
    tb = Toolbox(fake_sandbox)
    tb.write_file("pkg/big.py", "y = 1\n" * 5000)
    assert "chars omitted" in tb.git_diff()["diff"]
    full = tb.full_diff()
    assert "chars omitted" not in full["diff"]
    assert len(full["diff"]) == full["diff_bytes"]
