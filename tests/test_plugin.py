"""Tests for pytest-fixed-by — the standalone portal package.

Test strategy:
  - Unit tests for the decorator, portal functions, and helpers
    use temporary git repos created fresh in each test.
  - Integration tests exercise the full --verify-historical flow
    via pytest's pytester fixture.
  - No dependency on the Ruach-Tov repo or any external state.

All git operations use the same narrow portal the plugin ships:
  _rev_parse, _worktree_add, _worktree_remove
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from pytest_fixed_by import fixed_by
from pytest_fixed_by.plugin import (
    _FixInfo,
    _find_repo_root,
    _inject_tests,
    _rev_parse,
    _verify_against_history,
    _worktree_add,
    _worktree_remove,
)


# ═══════════════════════════════════════════════════════════════
# Helpers — create disposable git repos for testing
# ═══════════════════════════════════════════════════════════════


def _git(repo: Path, *args: str, check: bool = True) -> str:
    """Run a git command in a repo.  Returns stdout."""
    r = subprocess.run(
        ["git"] + list(args),
        cwd=repo, capture_output=True, text=True, timeout=10,
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")

    # Initial commit with a buggy source file
    src = repo / "src"
    src.mkdir()
    (src / "calculator.py").write_text("def add(a, b):\n    return a - b  # BUG!\n")
    (src / "__init__.py").write_text("")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial: buggy add()")

    return repo


def _fix_bug(repo: Path) -> str:
    """Fix the bug and return the fix commit hash."""
    (repo / "src" / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fix: correct add() to use +")
    return _git(repo, "rev-parse", "HEAD")


def _write_test_file(repo: Path, name: str = "test_add.py",
                     commit: str = "PLACEHOLDER") -> Path:
    """Write a test file that catches the bug into the repo.

    The test file needs two forms:
      - At HEAD (outer pytest): needs @fixed_by decorator
      - In worktree (inner pytest): needs to be importable WITHOUT
        pytest_fixed_by installed

    We handle this by making the import conditional — if pytest_fixed_by
    is available, use it; otherwise define a no-op stub.  This mirrors
    what a real project would do if they want the test to be runnable
    both with and without the plugin.
    """
    tests = repo / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "__init__.py").write_text("")
    test_file = tests / name
    test_file.write_text(textwrap.dedent(f"""\
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

        try:
            from pytest_fixed_by import fixed_by
        except ImportError:
            def fixed_by(*a, **kw):
                return lambda fn: fn

        @fixed_by("{commit}")
        def test_add_works():
            from calculator import add
            assert add(2, 3) == 5
    """))
    return test_file


# ═══════════════════════════════════════════════════════════════
# Decorator tests
# ═══════════════════════════════════════════════════════════════


class TestDecorator:
    """The decorator attaches metadata without wrapping."""

    def test_attaches_fix_info(self):
        @fixed_by("abc1234")
        def test_example():
            pass

        assert hasattr(test_example, "_fixed_by")
        assert isinstance(test_example._fixed_by, _FixInfo)
        assert test_example._fixed_by.commit == "abc1234"

    def test_preserves_function_identity(self):
        @fixed_by("abc1234")
        def test_example():
            pass

        assert test_example.__name__ == "test_example"
        assert type(test_example).__name__ == "function"

    def test_preserves_async_nature(self):
        import inspect

        @fixed_by("abc1234")
        async def test_async():
            pass

        assert inspect.iscoroutinefunction(test_async)

    def test_files_stored(self):
        @fixed_by("abc1234", files=["src/foo.py", "src/bar.py"])
        def test_example():
            pass

        assert test_example._fixed_by.files == ["src/foo.py", "src/bar.py"]

    def test_test_deps_stored(self):
        @fixed_by("abc1234", test_deps=["tests/helpers.py"])
        def test_example():
            pass

        assert test_example._fixed_by.test_deps == ["tests/helpers.py"]

    def test_defaults_to_empty_lists(self):
        @fixed_by("abc1234")
        def test_example():
            pass

        assert test_example._fixed_by.files == []
        assert test_example._fixed_by.test_deps == []

    def test_no_wrapping_callable_unchanged(self):
        """Decorated function IS the original function, not a wrapper."""
        sentinel = object()

        @fixed_by("abc1234")
        def test_example():
            return sentinel

        assert test_example() is sentinel


# ═══════════════════════════════════════════════════════════════
# Git portal tests
# ═══════════════════════════════════════════════════════════════


class TestGitPortal:
    """Tests for the three narrow git portal functions."""

    def test_rev_parse_resolves_head(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = _rev_parse("HEAD", repo)
        assert len(result) == 40  # full SHA
        assert all(c in "0123456789abcdef" for c in result)

    def test_rev_parse_resolves_short_hash(self, tmp_path):
        repo = _make_repo(tmp_path)
        full = _rev_parse("HEAD", repo)
        short = full[:7]
        assert _rev_parse(short, repo) == full

    def test_rev_parse_invalid_ref_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        with pytest.raises(ValueError, match="Cannot resolve"):
            _rev_parse("nonexistent-ref-xyz", repo)

    def test_worktree_add_creates_directory(self, tmp_path):
        repo = _make_repo(tmp_path)
        commit = _rev_parse("HEAD", repo)
        wt = tmp_path / "worktree"

        _worktree_add(repo, wt, commit)
        assert wt.exists()
        assert (wt / "src" / "calculator.py").exists()

        # Cleanup
        _worktree_remove(repo, wt)

    def test_worktree_add_detached(self, tmp_path):
        """Worktree is always created in detached HEAD mode."""
        repo = _make_repo(tmp_path)
        commit = _rev_parse("HEAD", repo)
        wt = tmp_path / "worktree"

        _worktree_add(repo, wt, commit)

        # Verify detached HEAD
        r = subprocess.run(
            ["git", "symbolic-ref", "HEAD"],
            cwd=wt, capture_output=True, text=True,
        )
        assert r.returncode != 0  # detached HEAD → symbolic-ref fails

        _worktree_remove(repo, wt)

    def test_worktree_remove_cleans_up(self, tmp_path):
        repo = _make_repo(tmp_path)
        commit = _rev_parse("HEAD", repo)
        wt = tmp_path / "worktree"

        _worktree_add(repo, wt, commit)
        assert wt.exists()

        _worktree_remove(repo, wt)
        assert not wt.exists()

    def test_worktree_remove_nonexistent_is_silent(self, tmp_path):
        """Removing a nonexistent worktree doesn't raise."""
        repo = _make_repo(tmp_path)
        _worktree_remove(repo, tmp_path / "no-such-worktree")


# ═══════════════════════════════════════════════════════════════
# Helper function tests
# ═══════════════════════════════════════════════════════════════


class TestHelpers:

    def test_find_repo_root(self, tmp_path):
        repo = _make_repo(tmp_path)
        deep = repo / "a" / "b" / "c"
        deep.mkdir(parents=True)

        assert _find_repo_root(deep) == repo

    def test_find_repo_root_at_root(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert _find_repo_root(repo) == repo

    def test_find_repo_root_no_repo_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="No git repo"):
            _find_repo_root(tmp_path)

    def test_inject_tests_copies_test_file(self, tmp_path):
        repo = _make_repo(tmp_path)
        test_file = _write_test_file(repo)
        commit = _rev_parse("HEAD", repo)

        wt = tmp_path / "wt"
        _worktree_add(repo, wt, commit)

        _inject_tests(test_file, wt, repo, [])
        assert (wt / "tests" / "test_add.py").exists()

        _worktree_remove(repo, wt)

    def test_inject_tests_copies_init(self, tmp_path):
        repo = _make_repo(tmp_path)
        test_file = _write_test_file(repo)
        commit = _rev_parse("HEAD", repo)

        wt = tmp_path / "wt"
        _worktree_add(repo, wt, commit)

        _inject_tests(test_file, wt, repo, [])
        assert (wt / "tests" / "__init__.py").exists()

        _worktree_remove(repo, wt)

    def test_inject_tests_copies_conftest(self, tmp_path):
        repo = _make_repo(tmp_path)
        test_file = _write_test_file(repo)
        # Write a conftest alongside the test
        (repo / "tests" / "conftest.py").write_text("# conftest\n")
        commit = _rev_parse("HEAD", repo)

        wt = tmp_path / "wt"
        _worktree_add(repo, wt, commit)

        _inject_tests(test_file, wt, repo, [])
        assert (wt / "tests" / "conftest.py").exists()

        _worktree_remove(repo, wt)

    def test_inject_tests_copies_test_deps(self, tmp_path):
        repo = _make_repo(tmp_path)
        test_file = _write_test_file(repo)
        # Write a helper
        helpers = repo / "tests" / "helpers.py"
        helpers.write_text("HELPER = True\n")
        commit = _rev_parse("HEAD", repo)

        wt = tmp_path / "wt"
        _worktree_add(repo, wt, commit)

        _inject_tests(test_file, wt, repo, ["tests/helpers.py"])
        assert (wt / "tests" / "helpers.py").exists()

        _worktree_remove(repo, wt)


# ═══════════════════════════════════════════════════════════════
# Verification engine tests
# ═══════════════════════════════════════════════════════════════


class TestVerification:
    """End-to-end verification against synthetic git history."""

    def test_verified_when_test_catches_bug(self, tmp_path):
        """A test that catches the bug: FAIL at parent, PASS at fix."""
        repo = _make_repo(tmp_path)
        fix_hash = _fix_bug(repo)
        test_file = _write_test_file(repo, commit=fix_hash)

        result = _verify_against_history(
            test_file=test_file,
            test_name="test_add_works",
            fix_commit=fix_hash,
            test_deps=[],
            repo_root=repo,
        )

        assert result["verified"] is True
        assert result["pre_fix"] == "FAIL"
        assert result["post_fix"] == "PASS"

    def test_unverified_when_test_passes_on_both(self, tmp_path):
        """A test that doesn't catch the bug: passes on both commits."""
        repo = _make_repo(tmp_path)
        fix_hash = _fix_bug(repo)

        # Write a test that always passes (doesn't test the fix)
        tests = repo / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "__init__.py").write_text("")
        test_file = tests / "test_trivial.py"
        test_file.write_text(textwrap.dedent(f"""\
            try:
                from pytest_fixed_by import fixed_by
            except ImportError:
                def fixed_by(*a, **kw):
                    return lambda fn: fn

            @fixed_by("{fix_hash}")
            def test_always_passes():
                assert 1 + 1 == 2
        """))

        result = _verify_against_history(
            test_file=test_file,
            test_name="test_always_passes",
            fix_commit=fix_hash,
            test_deps=[],
            repo_root=repo,
        )

        assert result["verified"] is False
        assert result["pre_fix"] == "PASS"  # should have been FAIL

    def test_result_contains_commit_hashes(self, tmp_path):
        repo = _make_repo(tmp_path)
        fix_hash = _fix_bug(repo)
        test_file = _write_test_file(repo, commit=fix_hash)

        result = _verify_against_history(
            test_file=test_file,
            test_name="test_add_works",
            fix_commit=fix_hash,
            test_deps=[],
            repo_root=repo,
        )

        assert result["fix_commit"] == fix_hash[:12]
        assert len(result["parent_commit"]) == 12

    def test_result_contains_output(self, tmp_path):
        repo = _make_repo(tmp_path)
        fix_hash = _fix_bug(repo)
        test_file = _write_test_file(repo, commit=fix_hash)

        result = _verify_against_history(
            test_file=test_file,
            test_name="test_add_works",
            fix_commit=fix_hash,
            test_deps=[],
            repo_root=repo,
        )

        assert len(result["pre_fix_output"]) > 0
        assert len(result["post_fix_output"]) > 0

    def test_invalid_commit_returns_error(self, tmp_path):
        repo = _make_repo(tmp_path)
        test_file = _write_test_file(repo, commit="nonexistent123")

        with pytest.raises(ValueError, match="Cannot resolve"):
            _verify_against_history(
                test_file=test_file,
                test_name="test_add_works",
                fix_commit="nonexistent123",
                test_deps=[],
                repo_root=repo,
            )


# ═══════════════════════════════════════════════════════════════
# Pytest plugin integration tests (via pytester)
# ═══════════════════════════════════════════════════════════════


class TestPluginIntegration:
    """Tests that exercise the plugin through pytest's own test harness.

    Uses pytester (formerly testdir) to run pytest in a subprocess
    and verify the plugin hooks work correctly.
    """

    @pytest.fixture
    def project(self, tmp_path):
        """Create a git repo with a bug, a fix, and a test."""
        repo = _make_repo(tmp_path)
        fix_hash = _fix_bug(repo)
        test_file = _write_test_file(repo, commit=fix_hash)
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "add test")
        return repo, fix_hash, test_file

    def test_normal_run_executes_test(self, project):
        """Without --verify-historical, @fixed_by tests run normally."""
        repo, fix_hash, test_file = project
        r = subprocess.run(
            ["python", "-m", "pytest", str(test_file), "-v", "--no-header",
             "-p", "no:cacheprovider"] + _PLUGIN_ARGS,
            cwd=str(repo), capture_output=True, text=True, timeout=30,
            env=_test_env(repo),
        )
        assert r.returncode == 0, f"stdout: {r.stdout}\nstderr: {r.stderr}"
        assert "PASSED" in r.stdout

    def test_verify_historical_shows_verified(self, project):
        """With --verify-historical, a good test shows VERIFIED."""
        repo, fix_hash, test_file = project
        r = subprocess.run(
            ["python", "-m", "pytest", str(test_file),
             "--verify-historical", "-v", "--no-header",
             "-p", "no:cacheprovider"] + _PLUGIN_ARGS,
            cwd=str(repo), capture_output=True, text=True, timeout=60,
            env=_test_env(repo),
        )
        assert r.returncode == 0, f"stdout: {r.stdout}\nstderr: {r.stderr}"
        assert "VERIFIED" in r.stdout

    def test_verify_historical_shows_unverified(self, tmp_path):
        """A test that doesn't catch the bug shows UNVERIFIED / FAILED."""
        repo = _make_repo(tmp_path)
        fix_hash = _fix_bug(repo)

        tests = repo / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "__init__.py").write_text("")
        (tests / "test_bad.py").write_text(textwrap.dedent(f"""\
            import sys, os
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

            try:
                from pytest_fixed_by import fixed_by
            except ImportError:
                def fixed_by(*a, **kw):
                    return lambda fn: fn

            @fixed_by("{fix_hash}")
            def test_always_passes():
                assert True
        """))
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "add bad test")

        r = subprocess.run(
            ["python", "-m", "pytest", str(tests / "test_bad.py"),
             "--verify-historical", "-v", "--no-header",
             "-p", "no:cacheprovider"] + _PLUGIN_ARGS,
            cwd=str(repo), capture_output=True, text=True, timeout=60,
            env=_test_env(repo),
        )
        assert r.returncode != 0, f"Should fail but passed.\nstdout: {r.stdout}"
        assert "FAILED" in r.stdout or "UNVERIFIED" in r.stdout, \
            f"stdout: {r.stdout}\nstderr: {r.stderr}"

    def test_no_fixed_by_tests_collects_nothing(self, tmp_path):
        """--verify-historical with no @fixed_by tests collects nothing."""
        repo = _make_repo(tmp_path)
        tests = repo / "tests"
        tests.mkdir()
        (tests / "test_plain.py").write_text("def test_plain():\n    pass\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "add plain test")

        r = subprocess.run(
            ["python", "-m", "pytest", str(tests / "test_plain.py"),
             "--verify-historical", "-v", "--no-header",
             "-p", "no:cacheprovider"] + _PLUGIN_ARGS,
            cwd=str(repo), capture_output=True, text=True, timeout=30,
            env=_test_env(repo),
        )
        assert "no tests ran" in r.stdout.lower() or r.returncode == 5, \
            f"stdout: {r.stdout}\nstderr: {r.stderr}"


_PKG_SRC = str(Path(__file__).resolve().parent.parent / "src")


def _test_env(repo: Path) -> dict[str, str]:
    """Build env dict with PYTHONPATH pointing at the package src."""
    env = os.environ.copy()
    # Point at our package source so `from pytest_fixed_by import fixed_by` works
    repo_src = str(repo / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_PKG_SRC}:{repo_src}" + (f":{existing}" if existing else "")
    return env


# When pip-installed, the pytest11 entry point auto-loads the plugin.
# When running from source (PYTHONPATH=src), we need -p to load it explicitly.
# We detect installation by trying to import and checking for the entry point marker.
def _plugin_args() -> list[str]:
    """Return [] if plugin auto-loads via entry point, else explicit -p flag."""
    import subprocess, sys
    r = subprocess.run(
        [sys.executable, "-c",
         "import pytest_fixed_by; "
         "from importlib.metadata import entry_points; "
         "eps = entry_points(); "
         "pts = eps.select(group='pytest11') if hasattr(eps, 'select') else eps.get('pytest11', []); "
         "print('yes' if any(e.name == 'fixed_by' for e in pts) else 'no')"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode == 0 and "yes" in r.stdout:
        return []
    return ["-p", "pytest_fixed_by.plugin"]

_PLUGIN_ARGS = _plugin_args()
