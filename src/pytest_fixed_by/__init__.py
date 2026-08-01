"""pytest-fixed-by — Prove your regression test catches the regression.

A pytest decorator and verification protocol that mechanically proves a test
catches the specific bug it claims to cover.  Uses git worktrees to run
today's test against yesterday's code.

Usage::

    from pytest_fixed_by import fixed_by

    @fixed_by("abc1234", files=["src/foo.py"])
    def test_regression():
        ...

    # Normal run: test executes as usual
    pytest

    # Historical verification: proves test catches the bug
    pytest --verify-historical
"""

from pytest_fixed_by.plugin import fixed_by, xfailed_by

__all__ = ["fixed_by", "xfailed_by"]
