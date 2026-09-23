"""Charter 207: our settled bucket against the bucket the market paid."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.settlement_reconciliation import (  # noqa: E402
    AGREE,
    MARKET_ABOVE,
    MARKET_BELOW,
    MARKET_LOWEST_BRACKET,
    NO_TRUTH,
    OUR_EVIDENCE_GAP,
    PENDING,
    classify,
    event_date_of,
    market_resolution,
    reconcile,
    summarize,
    warm_window_gap,
)

LADDER = ["18°C or below", "19°C", "20°C", "21°C", "22°C", "23°C or higher"]


def event(city: str = "london", day: str = "september-21-2026", *, winner: str | None = "21°C") -> dict:
    return {
        "slug": f"highest-temperature-in-{city}-on-{day}",
        "closedTime": "2026-09-21T23:51:12Z",
        "markets": [
            {
                "groupItemTitle": label,
                "outcomePrices": json.dumps(["1", "0"] if label == winner else ["0", "1"]),
                "umaResolutionStatus": "resolved" if winner else "",
            }
            for label in LADDER
        ],
    }


def evidence(hours_utc: list[str], *, zone: str = "Europe/London", day: str = "2026-09-21") -> dict:
    return {
        "settlement_timezone": zone,
        "observations": [{"valid_at_utc": f"{day}T{hour}:50:00.000Z"} for hour in hours_utc],
    }


def test_the_paid_bucket_is_read_off_the_prices() -> None:
    paid = market_resolution(event())
    assert paid["state"] == "RESOLVED"
    assert paid["winner"] == "21°C"
    assert paid["lowest_bracket"] == "18°C or below"
    assert market_resolution(event(winner=None))["state"] == "PENDING"


def test_event_date_comes_from_the_slug() -> None:
    assert event_date_of("highest-temperature-in-nyc-on-september-2-2026") == "2026-09-02"


def test_a_complete_hourly_afternoon_has_no_hole() -> None:
    # London is UTC+1 in September: 10:00-19:00 local is 09:00-18:00Z.
    gap = warm_window_gap(evidence([f"{h:02d}" for h in range(8, 20)]), event_date="2026-09-21")
    assert gap["complete"] is True
    assert gap["gap_minutes"] <= 60


def test_a_missing_afternoon_is_a_hole() -> None:
    # KORD 2026-09-21 shape: nothing between 10:51 and 15:51 local.
    hours = ["08", "09", "14", "15", "16", "17"]
    gap = warm_window_gap(evidence(hours), event_date="2026-09-21")
    assert gap["complete"] is False
    assert gap["gap_minutes"] >= 240


def test_an_evidence_format_without_timestamps_is_unknown_not_complete() -> None:
    assert warm_window_gap(["RCSS-2026-06-28-00-5fac"], event_date="2026-06-28") is None


def test_verdicts() -> None:
    paid = market_resolution(event(winner="21°C"))
    whole = {"complete": True}
    assert classify({"bucket": "21°C"}, paid, gap=None) == AGREE
    assert classify(None, paid, gap=None) == NO_TRUTH
    assert classify({"bucket": "21°C"}, market_resolution(event(winner=None)), gap=None) == PENDING
    assert classify({"bucket": "19°C"}, paid, gap={"complete": False}) == OUR_EVIDENCE_GAP
    assert classify({"bucket": "22°C"}, paid, gap=whole) == MARKET_BELOW
    assert classify({"bucket": "19°C"}, paid, gap=whole) == MARKET_ABOVE
    lowest = market_resolution(event(winner="18°C or below"))
    # Panama 2026-08-31: the "no data" bracket, not a temperature.
    assert classify({"bucket": "22°C"}, lowest, gap=whole) == MARKET_LOWEST_BRACKET


def test_evidence_is_read_only_for_rows_that_disagree() -> None:
    asked: list[tuple[str, str]] = []

    def evidence_of(station: str, day: str) -> dict:
        asked.append((station, day))
        return evidence(["08", "09", "14", "15", "16", "17"])

    rows = reconcile(
        events=[event("london", winner="21°C"), event("paris", winner="21°C")],
        ours={
            ("EGLC", "2026-09-21"): {"bucket": "21°C", "tmax_c": 21.0, "revision": 0},
            ("LFPB", "2026-09-21"): {"bucket": "19°C", "tmax_c": 19.0, "revision": 0},
        },
        city_stations={"london": "EGLC", "paris": "LFPB"},
        evidence_of=evidence_of,
    )
    assert asked == [("LFPB", "2026-09-21")]
    verdicts = {row["station_id"]: row["verdict"] for row in rows}
    assert verdicts == {"EGLC": AGREE, "LFPB": OUR_EVIDENCE_GAP}
    summary = summarize(rows)
    assert summary["counts"] == {AGREE: 1, OUR_EVIDENCE_GAP: 1}
    assert summary["agree_share"] == 0.5
    assert [row["station_id"] for row in summary["disagreements"]] == ["LFPB"]


def test_cities_outside_the_fleet_are_not_reconciled() -> None:
    rows = reconcile(
        events=[event("hong-kong")], ours={}, city_stations={"london": "EGLC"},
        evidence_of=lambda *_: None,
    )
    assert rows == []
