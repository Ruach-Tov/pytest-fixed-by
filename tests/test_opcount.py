"""Tests for @opcounted: determinism, complexity-law recovery, JSON records, nesting."""
import glob
import json
import os
import sys

import pytest

from pytest_fixed_by import opcounted

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="sys.monitoring requires Python 3.12+"
)


def bubble(n):
    a = list(range(n, 0, -1))
    m = len(a)
    for i in range(m):
        for j in range(m - 1 - i):
            if a[j] > a[j + 1]:
                a[j], a[j + 1] = a[j + 1], a[j]
    return a


def _records(tmpdir, name):
    return sorted(
        (json.load(open(p)) for p in glob.glob(os.path.join(tmpdir, f"opcount.*.{name}.*.json"))),
        key=lambda r: r["timestamp_utc"],
    )


def test_writes_json_record(tmp_path):
    work = opcounted(outdir=str(tmp_path))(bubble)
    result = work(10)
    assert result == sorted(result)
    recs = _records(str(tmp_path), "bubble")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["method"] == "bubble"
    assert rec["instructions"] > 0
    assert rec["pid"] == os.getpid()
    assert rec["nested_within_another_opcounted"] is False


def test_deterministic_counts(tmp_path):
    work = opcounted(outdir=str(tmp_path), warm=True)(bubble)
    work(15)
    work(15)
    recs = _records(str(tmp_path), "bubble")
    assert len(recs) == 2
    assert recs[0]["instructions"] == recs[1]["instructions"]


def test_quadratic_law_recovery(tmp_path):
    work = opcounted(outdir=str(tmp_path), warm=True)(bubble)
    work(20)
    work(40)
    recs = _records(str(tmp_path), "bubble")
    ratio = recs[1]["instructions"] / recs[0]["instructions"]
    assert 3.9 < ratio < 4.1, f"O(n^2) law: expected ~4, got {ratio}"


def test_nested_decoration(tmp_path):
    @opcounted(outdir=str(tmp_path))
    def helper(n):
        return sum(range(n))

    @opcounted(outdir=str(tmp_path))
    def outer(n):
        return helper(n) + helper(n)

    outer(50)
    helper_recs = _records(str(tmp_path), "helper")
    outer_recs = _records(str(tmp_path), "outer")
    assert len(helper_recs) == 2 and len(outer_recs) == 1
    assert all(r["nested_within_another_opcounted"] for r in helper_recs)
    assert not outer_recs[0]["nested_within_another_opcounted"]
    # outer's count includes both helper calls:
    assert outer_recs[0]["instructions"] > sum(r["instructions"] for r in helper_recs)


def test_exceptions_still_disarm(tmp_path):
    @opcounted(outdir=str(tmp_path))
    def boom():
        raise ValueError("measured failure")

    with pytest.raises(ValueError):
        boom()
    # a subsequent measurement still works (the hook was disarmed cleanly):
    work = opcounted(outdir=str(tmp_path))(bubble)
    work(5)
    assert len(_records(str(tmp_path), "bubble")) == 1


def test_no_json_mode(tmp_path):
    work = opcounted(outdir=str(tmp_path), write_json=False)(bubble)
    work(10)
    assert _records(str(tmp_path), "bubble") == []
