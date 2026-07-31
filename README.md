# pytest-fixed-by

**Prove your regression test catches the regression.**

A pytest plugin that links tests to the git commits that fixed the bugs they cover, then mechanically verifies the link using git worktrees.

## Install

```bash
pip install pytest-fixed-by
```

## Usage

Decorate regression tests with `@fixed_by`:

```python
from pytest_fixed_by import fixed_by

@fixed_by("4146f11f1", files=["src/persistence.py"])
def test_migration_is_nilpotent():
    """No ALTER TABLE when all columns already exist."""
    ...
```

Run tests normally:

```bash
pytest                      # tests execute as usual, must pass
```

Verify historically:

```bash
pytest --verify-historical  # proves each test catches its bug
```

## What `--verify-historical` Does

For each `@fixed_by`-annotated test:

1. Creates a git worktree at `commit~1` (before the fix)
2. Copies **today's test** into the worktree
3. Runs the test against pre-fix code — expects **FAIL**
4. Creates a worktree at `commit` (the fix)
5. Runs the test against post-fix code — expects **PASS**
6. Reports: **VERIFIED** or **UNVERIFIED**

```
tests/test_migration.py::test_no_alter_when_all_exist V  VERIFIED
tests/test_migration.py::test_timeout_before_alter    V  VERIFIED
tests/test_mcp_cleanup.py::test_force_cancel          F  UNVERIFIED
```

## What It Proves

Three things simultaneously:

1. **The test detects the specific bug** — it fails against pre-fix code
2. **The fix actually fixes it** — the test passes at the fix commit
3. **The test is still valid today** — we run *today's test* against historical code

## Parameters

```python
@fixed_by(
    "abc1234",              # commit hash that fixed the bug (required)
    files=["src/foo.py"],   # source files changed in fix (documentation)
    test_deps=["tests/helpers.py"],  # extra files to copy into worktree
)
```

- `commit`: Short or full git hash. The verifier resolves it via `git rev-parse`.
- `files`: Not used by the verifier — purely for humans reading the annotation.
- `test_deps`: Extra test files needed beyond the test file itself. `conftest*.py` and `__init__.py` from the test directory are always included automatically.

## Requirements

- Python ≥ 3.10
- pytest ≥ 7.0
- git (CLI, available on PATH)

## How It Works

The plugin uses **git worktrees** for isolation — it never checks out different commits in your working tree. Worktrees are created in a temporary directory and cleaned up after verification. Tests sharing a fix commit could share worktree pairs (the batch optimization), but the standalone plugin creates fresh pairs per test for simplicity.

The key insight: **today's test against yesterday's code**. The test file comes from HEAD (current), but the source code under test comes from the historical commit. This answers: "would the test I have *right now* have caught the bug that existed *back then*?"

## Prior Art

| Tool | What it proves |
|------|---------------|
| git bisect | "Which commit broke this?" (opposite direction) |
| Mutation testing | "Would tests catch *some* synthetic change?" |
| @fixed_by | "This test catches this specific historical bug" |

## License

MIT
