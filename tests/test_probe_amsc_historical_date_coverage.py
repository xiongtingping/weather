from __future__ import annotations

import pytest

from scripts.probe_amsc_historical_date_coverage import _dates


def test_probe_expands_small_inclusive_physical_date_range():
    assert _dates(["2026-07-09:2026-07-11", "2026-07-11"]) == [
        "2026-07-09",
        "2026-07-10",
        "2026-07-11",
    ]


def test_probe_refuses_unbounded_retention_scan():
    with pytest.raises(ValueError, match="exceeds_31_days"):
        _dates(["2025-01-01:2025-02-15"])
