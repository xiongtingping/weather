"""TAF capture reacts in one round (user, 2026-09-19: 「要提高轮询抓取，快速反应」).

* one request asks AWC which stations hold a newer bulletin than ours;
* a bulletin filed minutes before its nominal time is captured, not refused.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import capture_bayes_basket_target_awc_taf_calibration as cal  # noqa: E402
import capture_bayes_basket_target_awc_taf_revision as rev  # noqa: E402


def _rows():
    return [
        {"icaoId": "LTAC", "issueTime": "2026-09-18T22:40:00.000Z"},
        {"icaoId": "ZUUU", "issueTime": "2026-09-19T00:12:00.000Z"},
        {"icaoId": "EGLC", "issueTime": "2026-09-18T16:59:00.000Z"},
    ]


def test_the_probe_names_only_stations_with_something_newer() -> None:
    newer, receipt = rev.awc_newer_taf_stations(
        ["LTAC", "ZUUU", "EGLC"],
        held_issued_at={"LTAC": "2026-09-18T16:40:00Z",
                        "ZUUU": "2026-09-19T00:12:00Z",
                        "EGLC": "2026-09-18T10:57:00Z"},
        already_forced={"EGLC": "2026-09-18T16:59:00Z"},   # forced once already
        fetch=lambda url, timeout_sec: (_rows(), "sha", 123),
    )
    assert newer == {"LTAC": "2026-09-18T22:40:00Z"}
    assert receipt["state"] == "ok" and receipt["newer"] == ["LTAC"]


def test_a_failed_probe_changes_nothing() -> None:
    def boom(url, timeout_sec):
        raise RuntimeError("down")
    newer, receipt = rev.awc_newer_taf_stations(
        ["LTAC"], held_issued_at={}, fetch=boom)
    assert newer == {} and str(receipt["state"]).startswith("probe_failed")


def _kbkf(available: str):
    return [{
        "icaoId": "KBKF", "issueTime": "2026-09-18T17:30:00.000Z",
        "dbPopTime": available,
        "validTimeFrom": 1789750800, "validTimeTo": 1789858800,
        "rawTAF": "TAF KBKF 181730Z 1817/1923 VRB06KT 9999 FEW150 SCT220",
        "fcsts": [],
    }]


def test_a_bulletin_filed_minutes_early_is_read_at_its_issue_time() -> None:
    start = datetime(2026, 9, 19, 6, tzinfo=timezone.utc)
    end = datetime(2026, 9, 20, 6, tzinfo=timezone.utc)
    kwargs = dict(station_id="KBKF", start=start, end=end, event_date="2026-09-19",
                  timezone_name="America/Denver", require_tx=False,
                  require_features=False)
    # Strict order (the historical parser): refused.
    try:
        cal._taf_row(_kbkf("2026-09-18T17:28:13.743Z"), **kwargs)
        raise AssertionError("strict order should refuse")
    except cal.ContractError as exc:
        assert str(exc) == "awc_taf_historical_available_before_issue"
    # The forward capture's tolerance: accepted, available at the issue time.
    taf, _ = cal._taf_row(_kbkf("2026-09-18T17:28:13.743Z"),
                          pre_issue_tolerance_sec=rev.PRE_ISSUE_TOLERANCE_SEC, **kwargs)
    assert taf["available_time_utc"] == "2026-09-18T17:30:00Z"
    # Hours early is the stale-dbPopTime case, and stays refused.
    try:
        cal._taf_row(_kbkf("2026-09-18T15:00:00Z"),
                     pre_issue_tolerance_sec=rev.PRE_ISSUE_TOLERANCE_SEC, **kwargs)
        raise AssertionError("hours early should refuse")
    except cal.ContractError:
        pass
