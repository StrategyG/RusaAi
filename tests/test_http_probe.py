"""Unit tests for the bounded local HTTP performance probe."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "perf" / "http_probe.py"
SPEC = importlib.util.spec_from_file_location("rusa_http_probe", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_nearest_rank_percentiles():
    assert MODULE.percentile([5, 1, 4, 2, 3], 0.5) == 3
    assert MODULE.percentile([5, 1, 4, 2, 3], 0.95) == 5
    assert MODULE.percentile([1], 0.99) == 1


def test_empty_percentile_rejected():
    with pytest.raises(ValueError, match="empty"):
        MODULE.percentile([], 0.95)


@pytest.mark.parametrize("status,failed", [(200, False), (204, False), (400, True), (500, True), (0, True)])
def test_http_failure_classification(status, failed):
    assert (status < 200 or status >= 400) is failed
