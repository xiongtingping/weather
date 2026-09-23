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
