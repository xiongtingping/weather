"""Charter 207: an AWC outage longer than one capped response is re-asked.

2026-09-21: AWC went quiet ~15:55Z-01:24Z, the first poll back ingested
exactly 401 reports reaching 19:55Z, and KORD's 16:51-19:51Z METARs never
arrived -- the truth lane settled 63F where the market paid 66-67F.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import scripts.weather_basket_yes_daemon as daemon

NOW = datetime(2026, 9, 22, 1, 24, 50, tzinfo=timezone.utc)
STATIONS = [f"K{index:03d}" for index in range(45)]


class Ingress:
    def __init__(self) -> None:
        self.polls: list[dict] = []

    def process_source_poll(self, poll, **_kwargs):
        self.polls.append(poll)
        return {"new_observation_count": len(poll.get("reports") or [])}


def reports(count: int) -> list[dict]:
    return [{"station_id": "X", "raw_text": f"METAR X {index}"} for index in range(count)]


def fetcher(answer_size):
    asked: list[tuple[list[str], float]] = []

    def fetch(group, *, hours, timeout):
        asked.append((list(group), hours))
        return {"reports": reports(answer_size(group)), "transport": "official_awc_raw_metar_batch_direct"}

    return fetch, asked


def main_poll() -> dict:
    return {"reports": reports(400), "station_errors": []}


def test_an_ordinary_gap_is_left_to_the_live_poll() -> None:
    fetch, asked = fetcher(lambda group: 3)
    result = daemon.awc_outage_catchup(
        Ingress(), STATIONS, previous_success_at=NOW - timedelta(minutes=20),
        main_poll=main_poll(), now=NOW, fetch=fetch,
    )
    assert result["state"] == "not_needed"
    assert asked == []


def test_a_long_outage_is_re_asked_in_groups_that_cannot_hit_the_cap() -> None:
    ingress = Ingress()
    fetch, asked = fetcher(lambda group: 30 * len(group))
    result = daemon.awc_outage_catchup(
        ingress, STATIONS, previous_success_at=NOW - timedelta(hours=9, minutes=30),
        main_poll=main_poll(), now=NOW, fetch=fetch,
    )
    assert result["state"] == "caught_up"
    assert result["requested_hours"] == 11
    # 350 reports of headroom over 11 hours at 3.5 a station-hour.
    assert result["stations_per_request"] == 9
    assert sorted(station for group, _ in asked for station in group) == sorted(STATIONS)
    assert all(hours == 11 for _, hours in asked)
    assert len(ingress.polls) == len(asked)
    assert all(poll["source_lane"] == "awc" for poll in ingress.polls)


def test_a_group_that_comes_back_full_is_split_and_asked_again() -> None:
    ingress = Ingress()
    fetch, asked = fetcher(lambda group: 400 if len(group) > 2 else 50)
    result = daemon.awc_outage_catchup(
        ingress, STATIONS[:8], previous_success_at=NOW - timedelta(hours=29),
        main_poll=main_poll(), now=NOW, fetch=fetch,
    )
    assert result["state"] == "caught_up"
    # Every station reached exactly once through an answer under the cap.
    ingested = [group for group, _ in asked if len(group) <= 2]
    assert sorted(station for group in ingested for station in group) == sorted(STATIONS[:8])
    assert len(ingress.polls) == len(ingested)


def test_the_hole_is_capped_at_what_the_lookback_can_serve() -> None:
    fetch, asked = fetcher(lambda group: 1)
    result = daemon.awc_outage_catchup(
        Ingress(), STATIONS[:2], previous_success_at=NOW - timedelta(days=3),
        main_poll=main_poll(), now=NOW, fetch=fetch,
    )
    assert result["requested_hours"] == daemon.AWC_CATCHUP_MAX_HOURS


def test_nothing_is_asked_while_awc_is_still_failing() -> None:
    fetch, asked = fetcher(lambda group: 1)
    result = daemon.awc_outage_catchup(
        Ingress(), STATIONS, previous_success_at=NOW - timedelta(hours=9),
        main_poll={"reports": [], "station_errors": ["K000:URLError"]}, now=NOW, fetch=fetch,
    )
    assert result["state"] == "deferred_source_unhealthy"
    assert asked == []


def test_last_success_is_the_newest_clean_receipt() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reported_trajectory_source_polls("
        "source_lane TEXT, response_completed_at TEXT, report_count INTEGER, error_code TEXT)"
    )
    conn.executemany(
        "INSERT INTO reported_trajectory_source_polls VALUES(?,?,?,?)",
        [
            ("awc", "2026-09-21T15:55:00.000Z", 400, ""),
            ("awc", "2026-09-21T16:10:00.000Z", 0, "K000:URLError"),
            ("awc", "2026-09-21T16:20:00.000Z", 12, "K001:URLError"),
        ],
    )
    assert daemon._last_successful_awc_poll_at(conn) == datetime(
        2026, 9, 21, 15, 55, tzinfo=timezone.utc
    )
