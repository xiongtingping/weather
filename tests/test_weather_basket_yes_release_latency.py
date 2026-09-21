from __future__ import annotations

import unittest
import sqlite3
import inspect
from datetime import datetime, timezone

from reported_trajectory_market_registry import load_config
from scripts import weather_basket_yes_daemon as daemon
from scripts.weather_basket_yes_daemon import (
    RegistryRefreshWorker,
    awc_settlement_lookback_hours,
    awc_poll_interval,
    refresh_registry,
)


def _lookback_registry() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE reported_trajectory_market_registry(
             station_id TEXT,event_date TEXT,settlement_timezone TEXT,
             source_lane TEXT,source_eligible INTEGER,market_active INTEGER,
             market_accepting_orders INTEGER)"""
    )
    return conn


def test_awc_lookback_tracks_registry_settlement_day_and_ignores_future_day() -> None:
    conn = _lookback_registry()
    try:
        conn.executemany(
            "INSERT INTO reported_trajectory_market_registry VALUES(?,?,?,?,?,?,?)",
            [
                ("RKPK", "2026-07-27", "Asia/Seoul", "awc", 1, 1, 1),
                ("RKPK", "2026-07-28", "Asia/Seoul", "awc", 1, 1, 1),
            ],
        )
        assert awc_settlement_lookback_hours(
            conn,
            ["RKPK"],
            now_utc=datetime(2026, 7, 26, 19, 5, tzinfo=timezone.utc),
        ) == 6.0
    finally:
        conn.close()


def test_awc_lookback_is_bounded_and_future_only_scope_stays_minimal() -> None:
    conn = _lookback_registry()
    try:
        conn.execute(
            "INSERT INTO reported_trajectory_market_registry VALUES(?,?,?,?,?,?,?)",
            ("RKPK", "2026-07-28", "Asia/Seoul", "awc", 1, 1, 1),
        )
        assert awc_settlement_lookback_hours(
            conn,
            ["RKPK"],
            now_utc=datetime(2026, 7, 26, 19, 5, tzinfo=timezone.utc),
        ) == 2.0
        assert awc_settlement_lookback_hours(
            conn,
            ["RKPK"],
            now_utc=datetime(2026, 7, 28, 12, 5, tzinfo=timezone.utc),
            maximum_hours=8.0,
        ) == 8.0
    finally:
        conn.close()


def test_resident_registry_uses_bounded_automatic_official_discovery() -> None:
    source = inspect.getsource(daemon.main)
    policy = load_config()["poll_policy"]
    assert "automatic_official_gamma_registry" in source
    assert "registry_executor.submit" in source
    assert "RegistryRefreshWorker" in source
    assert policy["market_registry_refresh_sec"] >= 60
    assert policy["market_registry_retry_sec"] >= 10
    assert policy["market_registry_gamma_max_pages"] <= 4


def test_registry_refresh_independently_exports_future_target_catalog(
    tmp_path,
    monkeypatch,
) -> None:
    worker = object.__new__(RegistryRefreshWorker)
    worker.db_path = tmp_path / "archive.db"
    worker.config = {"poll_policy": {}}
    worker.conn = sqlite3.connect(":memory:")
    worker.target_registry_path = tmp_path / "market_registry_forward.json"
    calls = []

    monkeypatch.setattr(
        daemon,
        "refresh_registry",
        lambda conn, config: {
            "state": "materialized",
            "official_market_count": 45,
        },
    )

    def export(**kwargs):
        calls.append(kwargs)
        return {
            "state": "EXPORTED",
            "row_count": 495,
            "snapshot_sha256": "a" * 64,
        }

    monkeypatch.setattr(daemon, "export_shared_live_registry", export)
    try:
        result = worker.run()
    finally:
        worker.conn.close()

    assert calls == [{
        "source_db": worker.db_path,
        "output_path": worker.target_registry_path,
        "as_of_utc": calls[0]["as_of_utc"],
        "include_not_started_for_p0": True,
        "current_catalog_only": True,
    }]
    assert result["target_registry_export"]["row_count"] == 495
    assert result["target_registry_export_decoupled_from_nwp"] is True
    assert result["writes_target_database"] is False
    assert result["calls_order_gateway"] is False


def test_empty_official_registry_fetch_is_a_failure_and_never_a_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.weather_basket_yes_daemon.discovered_slugs",
        lambda *_args, **_kwargs: [("one-market", "2026-07-18")],
    )
    monkeypatch.setattr(
        "scripts.weather_basket_yes_daemon.fetch_gamma_events_window",
        lambda **_kwargs: [],
    )
    with sqlite3.connect(":memory:") as conn:
        try:
            refresh_registry(conn, load_config())
        except RuntimeError as exc:
            assert str(exc).startswith("market_registry_official_fetch_empty")
        else:
            raise AssertionError("empty official registry was accepted")


def test_registry_poll_policy_rejects_resource_unbounded_config(tmp_path) -> None:
    config = load_config()
    config["poll_policy"]["market_registry_gamma_max_pages"] = 5
    path = tmp_path / "config.json"
    path.write_text(__import__("json").dumps(config), encoding="utf-8")
    with unittest.TestCase().assertRaisesRegex(
        ValueError,
        "reported_trajectory_awc_ingress_registry_poll_policy_invalid",
    ):
        load_config(path)


def test_registry_config_rejects_cross_lane_fallback_or_mixing_guards(tmp_path) -> None:
    for guard in (
        "source_lanes_disjoint",
        "awc_fallback_for_amsc_forbidden",
        "amsc_fallback_for_awc_forbidden",
    ):
        config = load_config()
        config["guards"][guard] = False
        path = tmp_path / f"{guard}.json"
        path.write_text(__import__("json").dumps(config), encoding="utf-8")
        with unittest.TestCase().assertRaisesRegex(
            ValueError,
            "reported_trajectory_awc_ingress_global_contract_invalid",
        ):
            load_config(path)


class ReportedTrajectoryReleaseLatencyTests(unittest.TestCase):
    """Charter 170 moved these windows from a release clock to arrival windows.

    The burst used to be the 90 seconds after :00 and after :30, on the
    premise that reports are released there.  Measured over seven days of
    fleet arrivals, that pair covers 11% of what lands: the international
    roster observes at :00/:30 and arrives :03-:07 and :33-:37, and the
    contiguous-US roster observes :51-:58 and arrives :52-:59.  So :05 --
    which this class used to assert was background polling -- is one of the
    two busiest minutes of the hour (923 of 12,807 arrivals).
    """

    def test_awc_bursts_across_the_whole_international_arrival_peak(self) -> None:
        config = load_config()
        for minute in (0, 3, 5, 7, 30, 33, 37):
            self.assertEqual(
                awc_poll_interval(
                    config, datetime(2026, 7, 17, 14, minute, tzinfo=timezone.utc)
                ),
                0.25,
                minute,
            )

    def test_awc_bursts_over_the_contiguous_us_arrival_window(self) -> None:
        config = load_config()
        for minute in (50, 54, 59):
            self.assertEqual(
                awc_poll_interval(
                    config, datetime(2026, 7, 17, 14, minute, tzinfo=timezone.utc)
                ),
                0.25,
                minute,
            )

    def test_awc_returns_to_bounded_background_polling_between_the_peaks(self) -> None:
        config = load_config()
        # 0.9% of seven days of arrivals landed in :09-:18 and :39-:48.
        for minute in (12, 16, 40, 44, 48):
            self.assertEqual(
                awc_poll_interval(
                    config, datetime(2026, 7, 17, 14, minute, tzinfo=timezone.utc)
                ),
                2.0,
                minute,
            )

if __name__ == "__main__":
    unittest.main()
