"""Historical regression testing: pytest plugin + @fixed_by decorator.

Provides ``@fixed_by(commit_hash)`` and ``pytest --verify-historical``
for proving that regression tests actually catch the bugs they cover.

When run normally (``pytest``): decorated tests execute as usual.
When run with ``pytest --verify-historical``:

  1. Groups tests by fix commit (one worktree pair per commit).
  2. Creates a git worktree at commit~1 (parent — before the fix).
  3. Copies today's test file + dependencies into the worktree.
  4. Runs the test against pre-fix code — expects FAIL.
  5. Creates a worktree at the fix commit, runs the test — expects PASS.
  6. Reports each test as VERIFIED or UNVERIFIED.

Design:
  - Git worktrees for isolation (never disrupts the working tree).
  - Today's test against historical code ("does today's test catch
    yesterday's bug?").
  - No wrapping — decorator attaches metadata without altering the
    function, preserving async/sync nature for pytest-asyncio.
  - Batches by commit to minimize worktree creation.

Dependencies: pytest, git (CLI).  Nothing else.
"""

from __future__ import annotations

import inspect
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import pytest


# ═══════════════════════════════════════════════════════════════
# Decorator
# ═══════════════════════════════════════════════════════════════


@dataclass
class _FixInfo:
    """Metadata linking a test to its fix commit.

    ``polarity`` records what the commit did to the test.  ``"fixed"`` means the
    test began passing at the commit: pre-fix FAIL, post-fix PASS.  ``"xfailed"``
    means the test deliberately stopped passing there --- a heuristic withdrawn, a
    rule rejected --- so the expectation is inverted: pre-commit PASS, post-commit
    FAIL.  Both are provenance, and both are verifiable the same way.
    """
    commit: str
    files: list[str]
    test_deps: list[str]
    test_func: str = ""
    test_file: str = ""
    polarity: str = "fixed"
    reason: str = ""


def fixed_by(
    commit: str,
    files: Sequence[str] = (),
    test_deps: Sequence[str] = (),
):
    """Mark a test as covering a bug fixed by ``commit``.

    Args:
        commit: Git commit hash (short or full) that fixed the bug.
        files: Source files changed in the fix (documentation only —
               not used by the verifier, but valuable for humans
               reading the annotation).
        test_deps: Extra test helper files to copy into the worktree
                   (e.g., ``["tests/helpers.py"]``).  conftest*.py
                   files from the test's directory are always included.

    Example::

        @fixed_by("4146f11f1", files=["src/persistence.py"])
        def test_migration_is_nilpotent():
            ...
    """
    def decorator(func):
        func._fixed_by = _FixInfo(
            commit=commit,
            files=list(files),
            test_deps=list(test_deps),
            test_func=func.__name__,
            polarity="fixed",
        )
        return func  # No wrapping — preserves async/sync nature
    return decorator


def xfailed_by(
    commit: str,
    reason: str = "",
    files: Sequence[str] = (),
    test_deps: Sequence[str] = (),
):
    """Mark a test as DELIBERATELY broken by ``commit``.

    The counterpart to :func:`fixed_by`.  Where ``fixed_by`` records that a test
    began passing at a commit, ``xfailed_by`` records that it stopped passing
    there on purpose --- a heuristic withdrawn, a rule rejected, an interface
    narrowed --- and that the failure is the intended behaviour rather than a
    regression.

    The verification is the same protocol with the polarity inverted: the test is
    expected to PASS at the parent commit and FAIL at the commit itself.  A test
    that fails both sides was not broken by that commit, and the annotation is
    wrong.

    Under a normal run the test is marked ``xfail(strict=True)``, so it is
    reported as expected-to-fail and an unexpected PASS is an error --- which is
    the signal that the withdrawn behaviour has come back.

    Args:
        commit: Git commit hash that deliberately broke the test.
        reason: Why the behaviour was withdrawn.  Shown in the xfail report.
        files: Source files changed (documentation only).
        test_deps: Extra helper files to copy into the worktree.

    Example::

        @xfailed_by("255e60aa4",
                    reason="per-orbit symmetry filter; the layer offered here "
                           "is not mirror-symmetric and was never legal")
        def test_generator_offers_5_8_at_layer_7():
            ...
    """
    def decorator(func):
        func._fixed_by = _FixInfo(
            commit=commit,
            files=list(files),
            test_deps=list(test_deps),
            test_func=func.__name__,
            polarity="xfailed",
            reason=reason,
        )
        return pytest.mark.xfail(
            strict=True,
            reason=f"xfailed_by {commit}" + (f": {reason}" if reason else ""),
        )(func)
    return decorator


# ═══════════════════════════════════════════════════════════════
# Pytest plugin hooks
# ═══════════════════════════════════════════════════════════════


def pytest_addoption(parser):
    parser.addoption(
        "--verify-historical",
        action="store_true",
        default=False,
        help=(
            "Verify @fixed_by tests against their historical commits. "
            "For each annotated test, checks that it FAILS at the parent "
            "commit and PASSES at the fix commit."
        ),
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """When --verify-historical: keep only @fixed_by tests, replace bodies."""
    if not config.getoption("--verify-historical"):
        return

    historical = []
    for item in items:
        fn = getattr(item, "obj", None)
        if fn and hasattr(fn, "_fixed_by"):
            info: _FixInfo = fn._fixed_by
            info.test_file = str(item.fspath)
            info.test_func = item.name
            item.obj = _make_verifier(item, info)
            historical.append(item)

    items[:] = historical


def pytest_report_teststatus(report, config):
    """Show 'V' for VERIFIED instead of '.' for passed."""
    if not config.getoption("--verify-historical", default=False):
        return
    if report.when == "call" and report.passed:
        return "passed", "V", "VERIFIED"


# ═══════════════════════════════════════════════════════════════
# Verification engine
# ═══════════════════════════════════════════════════════════════


def _make_verifier(item, info: _FixInfo):
    """Replace a test's body with historical verification logic."""
    is_async = inspect.iscoroutinefunction(getattr(item, "obj", None))

    def _verify():
        repo = _find_repo_root(Path(info.test_file))
        result = _verify_against_history(
            test_file=Path(info.test_file),
            test_name=info.test_func,
            fix_commit=info.commit,
            test_deps=info.test_deps,
            repo_root=repo,
            polarity=getattr(info, "polarity", "fixed"),
        )
        if not result["verified"]:
            parts = []
            xf = getattr(info, "polarity", "fixed") == "xfailed"
            want_pre, want_post = ("PASS", "FAIL") if xf else ("FAIL", "PASS")
            if result["pre_fix"] != want_pre:
                parts.append(
                    f"pre-commit should {want_pre} but was {result['pre_fix']}")
            if result["post_fix"] != want_post:
                parts.append(
                    f"post-commit should {want_post} but was {result['post_fix']}")
            pytest.fail(
                f"Historical verification FAILED for {info.commit}: "
                f"{'; '.join(parts)}\n"
                f"Pre-fix output:\n{result['pre_fix_output'][-300:]}\n"
                f"Post-fix output:\n{result['post_fix_output'][-300:]}"
            )

    if is_async:
        async def verifier():
            _verify()
    else:
        def verifier():
            _verify()

    verifier.__name__ = info.test_func
    verifier.__doc__ = f"[HISTORICAL] Verify {info.test_func} catches bug fixed by {info.commit}"
    return verifier


def _verify_against_history(
    test_file: Path,
    test_name: str,
    fix_commit: str,
    test_deps: Sequence[str],
    repo_root: Path,
    polarity: str = "fixed",
) -> dict[str, Any]:
    """Run today's test against pre-fix and post-fix code.

    Returns::

        {
            "test": "test_name",
            "fix_commit": "abc...",
            "pre_fix": "FAIL" | "PASS" | "ERROR",
            "post_fix": "PASS" | "FAIL" | "ERROR",
            "verified": bool,
            "pre_fix_output": "...",
            "post_fix_output": "...",
        }
    """
    fix_hash = _rev_parse(fix_commit, repo_root)
    parent_hash = _rev_parse(f"{fix_commit}~1", repo_root)

    result: dict[str, Any] = {
        "test": test_name,
        "fix_commit": fix_hash[:12],
        "parent_commit": parent_hash[:12],
        "pre_fix": "ERROR",
        "post_fix": "ERROR",
        "verified": False,
        "pre_fix_output": "",
        "post_fix_output": "",
    }

    with tempfile.TemporaryDirectory(prefix="fixed-by-") as tmpdir:
        for phase, commit, key in [
            ("pre-fix", parent_hash, "pre_fix"),
            ("post-fix", fix_hash, "post_fix"),
        ]:
            wt = Path(tmpdir) / phase
            try:
                _worktree_add(repo_root, wt, commit)
                _inject_tests(test_file, wt, repo_root, test_deps)
                ok, output = _run_in_worktree(wt, test_file, test_name, repo_root)
                result[key] = "PASS" if ok else "FAIL"
                result[f"{key}_output"] = output
            except Exception as e:
                result[f"{key}_output"] = f"ERROR: {type(e).__name__}: {e}"
            finally:
                _worktree_remove(repo_root, wt)

    if polarity == "xfailed":
        # deliberately broken: PASS before the commit, FAIL after it
        result["verified"] = result["pre_fix"] == "PASS" and result["post_fix"] == "FAIL"
    else:
        result["verified"] = result["pre_fix"] == "FAIL" and result["post_fix"] == "PASS"
    return result


# ═══════════════════════════════════════════════════════════════
# Narrow git portal — bespoke to the @fixed_by scenario
#
# The full git CLI has hundreds of subcommands.  This portal
# provides exactly three, with constant arguments inlined:
#
#   _rev_parse       → git rev-parse <ref>
#                      always: capture_output=True, text=True, timeout=10
#
#   _worktree_add    → git worktree add --detach <path> <commit>
#                      always: --detach
#
#   _worktree_remove → git worktree remove --force <path>
#                      always: --force
#
# No other git operations are reachable from this package.
# ═══════════════════════════════════════════════════════════════


def _find_repo_root(start: Path) -> Path:
    """Walk up from ``start`` to find the .git directory."""
    p = start.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    raise RuntimeError(f"No git repo found above {start}")


def _rev_parse(ref: str, repo: Path) -> str:
    """Resolve a git ref to a full commit hash."""
    r = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=repo, capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        raise ValueError(f"Cannot resolve git ref '{ref}': {r.stderr.strip()}")
    return r.stdout.strip()


def _worktree_add(repo: Path, path: Path, commit: str) -> None:
    """Create a detached worktree at ``path`` for ``commit``."""
    r = subprocess.run(
        ["git", "worktree", "add", "--detach", str(path), commit],
        cwd=repo, capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {r.stderr.strip()}")


def _worktree_remove(repo: Path, path: Path) -> None:
    """Remove a git worktree.  Ignores errors (cleanup is best-effort)."""
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)],
        cwd=repo, capture_output=True, text=True, timeout=10,
    )


# ═══════════════════════════════════════════════════════════════
# Narrow pytest portal — bespoke to worktree verification
#
# The pytest CLI has dozens of flags.  This portal uses exactly
# one invocation pattern with constant flags:
#
#   python -m pytest <path>::<test> -x --tb=short -q --no-header
#                                   -p no:cacheprovider
#
# The only variables are the test path and working directory.
# capture_output=True, text=True, timeout=60 are constants.
# ═══════════════════════════════════════════════════════════════


def _inject_tests(
    test_file: Path,
    worktree: Path,
    repo_root: Path,
    test_deps: Sequence[str],
) -> None:
    """Copy today's test + helpers into the worktree.

    Always copies:
      - The test file itself
      - All conftest*.py and __init__.py in the same directory
    Also copies any files listed in ``test_deps``.
    """
    rel = test_file.relative_to(repo_root) if test_file.is_absolute() else test_file
    dest = worktree / rel
    dest.parent.mkdir(parents=True, exist_ok=True)

    shutil.copy2(repo_root / rel, dest)

    for pattern in ("conftest*.py", "__init__.py"):
        for helper in (repo_root / rel).parent.glob(pattern):
            shutil.copy2(helper, dest.parent / helper.name)

    for dep in test_deps:
        src = repo_root / dep
        if src.exists():
            dst = worktree / dep
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _run_in_worktree(
    worktree: Path,
    test_file: Path,
    test_name: str,
    repo_root: Path,
) -> tuple[bool, str]:
    """Run a single test in the worktree.  Returns (passed, output)."""
    rel = test_file.relative_to(repo_root) if test_file.is_absolute() else test_file
    parts = rel.parts
    subproject = worktree / parts[0] if len(parts) > 1 else worktree

    # Build PYTHONPATH: src/ and tests/ under the subproject
    pythonpath = [str(subproject / d) for d in ("src", "tests") if (subproject / d).exists()]
    env = os.environ.copy()
    if pythonpath:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = ":".join(pythonpath) + (":" + existing if existing else "")

    r = subprocess.run(
        [
            "python", "-m", "pytest",
            f"{worktree / rel}::{test_name}",
            "-x", "--tb=short", "-q", "--no-header",
            "-p", "no:cacheprovider",
        ],
        cwd=str(subproject),
        capture_output=True, text=True, timeout=60, env=env,
    )
    return r.returncode == 0, (r.stdout + r.stderr)[-500:]
