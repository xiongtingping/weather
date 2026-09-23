"""Charter 205: the banner row for the settlement-source check.

The registry refresh reads every market's rules against the verified
per-city baseline and leaves its reading in ``status.json``.  The banner is
where a changed source has to be seen; the 08-23 NOAA rewording starved the
fleet for a day with nothing on the page.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "calibration_dashboard_for_settlement_source_test",
    ROOT / "scripts" / "calibration_dashboard.py",
)
dashboard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dashboard)


def status(**check) -> dict:
    return {
        "registry_refresh": {
            "ok": True,
            "settlement_source": {
                "families": {"NOAA_WRH_TIMESERIES": 44, "WU_DAILY_OBSERVATIONS": 1},
                "deviations": [],
                "blocked_market_count": 0,
                "baseline_cities_without_market": [],
                **check,
            },
        }
    }


def test_matching_baseline_is_green_and_stays_off_the_banner() -> None:
    health = dashboard._settlement_source_health(status())
    assert health["settlement_source_health"] == "green"
    assert "NOAA 44" in health["settlement_source_note"]


def test_a_blocked_market_is_red_and_names_the_clause_and_date() -> None:
    health = dashboard._settlement_source_health(status(
        blocked_market_count=1,
        deviations=[{
            "city_slug": "nyc", "station_id": "KLGA", "field": "hourly_data_only",
            "expected": True, "observed": False, "blocking": True,
            "event_dates": ["2026-09-25"],
        }],
    ))
    assert health["settlement_source_health"] == "red"
    assert "nyc/KLGA 只看整点" in health["settlement_source_note"]
    assert "09-25，已拦" in health["settlement_source_note"]


def test_a_move_we_can_still_settle_is_amber() -> None:
    health = dashboard._settlement_source_health(status(
        deviations=[{
            "city_slug": "taipei", "station_id": "RCSS", "field": "family",
            "expected": "WU_DAILY_OBSERVATIONS", "observed": "NOAA_WRH_TIMESERIES",
            "blocking": False, "event_dates": ["2026-09-25", "2026-09-26"],
        }],
    ))
    assert health["settlement_source_health"] == "amber"
    assert "基线 WU → 页面 NOAA" in health["settlement_source_note"]


def test_a_baseline_city_without_a_market_is_amber() -> None:
    health = dashboard._settlement_source_health(
        status(baseline_cities_without_market=["london"])
    )
    assert health["settlement_source_health"] == "amber"
    assert "london" in health["settlement_source_note"]


def test_one_failed_refresh_is_not_an_unchecked_source_but_half_an_hour_is() -> None:
    failed = {"ok": False, "error": "RuntimeError:gamma"}
    assert dashboard._settlement_source_health(
        {"registry_refresh": failed, "registry": {"registry_age_sec": 310.0}}
    ) == {}
    stale = dashboard._settlement_source_health(
        {"registry_refresh": failed, "registry": {"registry_age_sec": 2400.0}}
    )
    assert stale["settlement_source_health"] == "amber"
    assert "40 分钟" in stale["settlement_source_note"]


# Charter 207: the banner row for the truth reconciliation.

import json as _json
from datetime import datetime as _datetime, timezone as _timezone


def _reconciliation(tmp_path, *, checked_at=None, disagreements=(), window_end="2026-09-22"):
    path = tmp_path / "settlement_truth_reconciliation_status.json"
    path.write_text(_json.dumps({
        "checked_at": checked_at or _datetime.now(_timezone.utc).isoformat(),
        "window": {"start": "2026-09-13", "end": window_end},
        "counts": {"AGREE": 440, "OUR_EVIDENCE_GAP": 0},
        "decided": 440 + len(disagreements),
        "disagreements": list(disagreements),
    }), encoding="utf-8")
    return path


def test_reconciliation_that_agrees_is_green(tmp_path) -> None:
    health = dashboard._settlement_reconciliation_health(_reconciliation(tmp_path))
    assert health["settlement_reconciliation_health"] == "green"
    assert "440/440 一致" in health["settlement_reconciliation_note"]


def test_our_afternoon_hole_is_red_and_names_it(tmp_path) -> None:
    health = dashboard._settlement_reconciliation_health(_reconciliation(tmp_path, disagreements=[{
        "verdict": "OUR_EVIDENCE_GAP", "station_id": "KORD", "event_date": "2026-09-14",
        "our_bucket": "62-63°F", "market_bucket": "66-67°F",
        "warm_window_gap": {"gap_local": "10:51-15:51"},
    }]))
    assert health["settlement_reconciliation_health"] == "red"
    assert "KORD 09-14 我们 62-63°F / 市场 66-67°F（午后缺 10:51-15:51）" in health[
        "settlement_reconciliation_note"
    ]


def test_market_side_disagreement_is_amber_only_while_recent(tmp_path) -> None:
    row = {"verdict": "MARKET_BELOW", "station_id": "ZBAA", "our_bucket": "30°C",
           "market_bucket": "28°C"}
    recent = dashboard._settlement_reconciliation_health(
        _reconciliation(tmp_path, disagreements=[{**row, "event_date": "2026-09-20"}])
    )
    assert recent["settlement_reconciliation_health"] == "amber"
    assert "市场偏低 1" in recent["settlement_reconciliation_note"]
    old = dashboard._settlement_reconciliation_health(
        _reconciliation(tmp_path, disagreements=[{**row, "event_date": "2026-09-14"}])
    )
    assert old["settlement_reconciliation_health"] == "green"


def test_a_reconciliation_that_stopped_running_is_amber(tmp_path) -> None:
    stale = dashboard._settlement_reconciliation_health(
        _reconciliation(tmp_path, checked_at="2026-09-01T00:00:00+00:00")
    )
    assert stale["settlement_reconciliation_health"] == "amber"
    missing = dashboard._settlement_reconciliation_health(tmp_path / "absent.json")
    assert missing["settlement_reconciliation_health"] == "amber"
