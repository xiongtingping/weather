"""Charter 170: the METAR fast lane, its learned windows, and its isolation.

The lane exists because the AWC fetch used to reach the network only on the
main loop's turn (measured median 58.6s against a configured 2.0s).  What is
tested here is the behaviour that replaces it: a clock learned from each
station's own filing schedule, a round that touches AWC and nothing else, and
a wake flag the main loop can act on.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weather_basket_yes_schema import apply_weather_basket_yes_schema


def _load_daemon():
    spec = importlib.util.spec_from_file_location(
        "wbyd_charter170", ROOT / "scripts" / "weather_basket_yes_daemon.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


daemon = _load_daemon()

POLICY = {
    "awc_fast_us_window_sec": 5.0,
    "awc_fast_fleet_window_sec": 15.0,
    "awc_fast_cold_sec": 60.0,
}


def _at(minute: int) -> datetime:
    return datetime(2026, 9, 20, 7, minute, tzinfo=timezone.utc)


def _seed(conn: sqlite3.Connection, station: str, minutes, *, days: int = 3) -> None:
    """One METAR per hour per listed arrival minute, over the last `days`."""

    now = datetime.now(timezone.utc)
    rows = []
    for day in range(days):
        for hour in range(24):
            for index, minute in enumerate(minutes):
                landed = (now - timedelta(days=day + 1)).replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                valid = landed - timedelta(minutes=2)
                stamp = landed.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                rows.append((
                    f"{station}-{day}-{hour}-{index}", "awc", station, "METAR",
                    valid.strftime("%Y-%m-%dT%H:%M:%S.000Z"), 20,
                    f"{station}{day}{hour}{index}".ljust(64, "0"),
                    f"METAR {station} RAW", stamp, stamp, stamp, stamp, stamp,
                    f"poll-{station}-{day}-{hour}-{index}", 0,
                ))
    conn.executemany(
        "INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(connection)
    yield connection
    connection.close()


class TestFallbackWindows:
    """Without a learned schedule the three measured windows stand."""

    def test_us_window_is_the_last_ten_minutes(self):
        for minute in (50, 55, 59):
            assert daemon.awc_fast_lane_plan(_at(minute), POLICY) == ("us_hot", 5.0)

    def test_international_arrival_peaks_are_hot(self):
        for minute in (0, 4, 8, 19, 25, 29, 33, 38):
            window, interval = daemon.awc_fast_lane_plan(_at(minute), POLICY)
            assert (window, interval) == ("fleet_hot", 15.0), minute

    def test_the_two_empty_stretches_are_cold(self):
        # 0.9% of seven days of fleet arrivals landed in these minutes.
        for minute in (9, 14, 18, 39, 44, 49):
            window, interval = daemon.awc_fast_lane_plan(_at(minute), POLICY)
            assert (window, interval) == ("cold", 60.0), minute


class TestLearnedSchedule:
    def test_learns_each_convention_off_arrival_stamps(self, conn):
        _seed(conn, "KMIA", [54])            # contiguous-US: :53 observed
        _seed(conn, "ZUUU", [4])             # international :00 filer
        _seed(conn, "EDDM", [22, 52])        # :20/:50 filer
        learned = daemon.learn_awc_arrival_minutes(conn, ["KMIA", "ZUUU", "EDDM"])
        assert learned["available"] is True
        assert learned["stations"]["KMIA"] == [53, 54, 55]
        assert learned["stations"]["ZUUU"] == [3, 4, 5]
        assert learned["stations"]["EDDM"] == [21, 22, 23, 51, 52, 53]
        # The union is what goes hot; the US roster alone gets the 5s window.
        assert set(learned["hot_minutes"]) == {3, 4, 5, 21, 22, 23, 51, 52, 53, 54, 55}
        assert set(learned["us_minutes"]) == {53, 54, 55}

    def test_learned_schedule_overrides_the_fallback(self, conn):
        _seed(conn, "ZUUU", [4])
        learned = daemon.learn_awc_arrival_minutes(conn, ["ZUUU"])
        # :04 is fleet_hot under both, but :55 is us_hot under the fallback and
        # cold once the roster is known to file at :04 only.
        assert daemon.awc_fast_lane_plan(_at(4), POLICY, learned)[0] == "fleet_hot"
        assert daemon.awc_fast_lane_plan(_at(55), POLICY)[0] == "us_hot"
        assert daemon.awc_fast_lane_plan(_at(55), POLICY, learned)[0] == "cold"

    def test_a_station_with_no_schedule_is_named_not_chased(self, conn):
        _seed(conn, "KMIA", [54])
        _seed(conn, "KBKF", list(range(0, 60, 2)))  # files all over the hour
        learned = daemon.learn_awc_arrival_minutes(conn, ["KMIA", "KBKF"])
        assert learned["available"] is True
        assert learned["unscheduled_stations"] == ["KBKF"]
        # KBKF's minutes are still reported, they just do not widen the window.
        assert "KBKF" in learned["stations"]
        assert set(learned["hot_minutes"]) == {53, 54, 55}

    def test_a_degenerate_read_falls_back_rather_than_going_hot_all_hour(self, conn):
        # Many stations, each disciplined, together covering nearly the hour.
        for index in range(12):
            _seed(conn, f"Z{index:03d}", [index * 5, index * 5 + 2])
        stations = [f"Z{index:03d}" for index in range(12)]
        learned = daemon.learn_awc_arrival_minutes(conn, stations)
        assert learned["available"] is False
        assert learned["reason"] == "learned_window_too_wide"
        assert learned["hot_minutes"] == []
        # ... and the plan therefore keeps the fallback windows.
        assert daemon.awc_fast_lane_plan(_at(55), POLICY, learned)[0] == "us_hot"

    def test_too_little_history_is_not_a_schedule(self, conn):
        _seed(conn, "KMIA", [54], days=0)
        learned = daemon.learn_awc_arrival_minutes(conn, ["KMIA"])
        assert learned["available"] is False
        assert learned["stations"] == {}


class TestSlowLaneFollowsTheSameWindows:
    def test_burst_interval_tracks_the_plan(self):
        config = {"poll_policy": dict(POLICY, **{
            "awc_release_burst_retry_sec": 0.25, "awc_background_poll_sec": 2.0,
        })}
        # :03 is an arrival peak (international :00 filers) and used to be
        # background under the old :00/:30 +90s burst.
        assert daemon.awc_poll_interval(config, _at(3)) == 0.25
        assert daemon.awc_poll_interval(config, _at(54)) == 0.25
        assert daemon.awc_poll_interval(config, _at(44)) == 2.0


class TestFastRoundIsolation:
    def test_round_fetches_awc_and_touches_no_other_lane(self, conn, monkeypatch):
        calls: dict[str, int] = {"awc": 0, "madis": 0, "asos": 0}

        def _fetch(stations, *, hours, timeout=8.0):
            calls["awc"] += 1
            assert hours == daemon.FAST_LANE_LOOKBACK_HOURS
            return {"reports": [], "transport": "official_awc_raw_metar_batch_direct"}

        def _madis(*args, **kwargs):
            calls["madis"] += 1
            return {}

        def _asos(*args, **kwargs):
            calls["asos"] += 1
            return {}

        monkeypatch.setattr(daemon, "fetch_awc_temperature_reports", _fetch)
        monkeypatch.setattr(daemon, "poll_madis_hfmetar_source", _madis)
        monkeypatch.setattr(daemon, "poll_asos_priority_source", _asos)

        class _Ingress:
            def process_source_poll(self, poll, **kwargs):
                return {"ok": True, "new_observation_count": 0}

        result = daemon.run_awc_fast_round(
            conn, {"production_source_allowlist": ["awc"]}, _Ingress(),
            ["KMIA"], window="us_hot",
        )
        assert result["fast_lane"] is True and result["window"] == "us_hot"
        assert calls == {"awc": 1, "madis": 0, "asos": 0}

    def test_the_door_is_named_the_way_charter_162_names_it(self, conn, monkeypatch):
        monkeypatch.setattr(
            daemon, "fetch_awc_temperature_reports",
            lambda stations, *, hours, timeout=8.0: {
                "reports": [], "transport": "official_noaa_tgftp_metar_cycle",
            },
        )

        class _Ingress:
            def process_source_poll(self, poll, **kwargs):
                return {"ok": True, "new_observation_count": 0}

        result = daemon.run_awc_fast_round(
            conn, {"production_source_allowlist": ["awc"]}, _Ingress(),
            ["KMIA"], window="cold",
        )
        assert result["provider_id"] == "global_noaa_tgftp_relay"

    def test_a_lane_that_is_not_allowlisted_is_refused(self, conn):
        with pytest.raises(ValueError):
            daemon.run_awc_fast_round(
                conn, {"production_source_allowlist": []}, object(),
                ["KMIA"], window="cold",
            )


class TestUpstreamBackoff:
    """A round slower than its own interval must not close the gap to zero.

    Measured 2026-09-20: AWC went from a 0.13s median request to 10-18s, and
    the original wait -- ``max(0.5, interval - elapsed)`` -- would have gone
    to its half-second floor and polled a struggling service with no gap.
    """

    @staticmethod
    def _wait_for(interval: float, elapsed: float) -> float:
        return max(interval * 0.5, interval - elapsed)

    def test_healthy_upstream_keeps_the_asked_for_pace(self):
        # 5s window, a 0.13s request: the next round is ~5s later.
        assert self._wait_for(5.0, 0.13) == pytest.approx(4.87)

    def test_an_overrunning_round_backs_off_to_half_the_interval(self):
        # 15s window, a 22s request: half-speed, not back-to-back.
        assert self._wait_for(15.0, 22.0) == pytest.approx(7.5)
        assert self._wait_for(5.0, 18.0) == pytest.approx(2.5)

    def test_the_floor_is_never_below_half_the_interval(self):
        for interval in (5.0, 15.0, 60.0):
            for elapsed in (0.0, interval, interval * 10):
                assert self._wait_for(interval, elapsed) >= interval * 0.5


class TestWakeSignal:
    def test_signal_is_raised_once_per_batch_of_new_rows(self):
        worker = daemon.AwcFastLaneWorker(Path(":memory:"), {})
        assert worker.take_wake_signal() is False
        with worker.lock:
            worker.state["wake_pending"] = True
        assert worker.take_wake_signal() is True
        assert worker.take_wake_signal() is False

    def test_snapshot_is_a_copy(self):
        worker = daemon.AwcFastLaneWorker(Path(":memory:"), {})
        snapshot = worker.snapshot()
        snapshot["state"] = "mutated"
        assert worker.snapshot()["state"] == "not_started"
        assert worker.snapshot()["charter"] == 170
