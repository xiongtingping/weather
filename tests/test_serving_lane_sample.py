"""What the calibration lanes still read through the retired band store.

Charter 232 (2026-09-26) retired the band layer and its lane; the module kept
the helpers the centre/width and off-centre lanes share.  These tests carry
over the ones that pinned those helpers when they lived beside the band fit.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target import band_reliability_store as store
from bayes_basket_target.band_reliability_store import (
    _begin_immediate,
    _invert_band_receipt,
    serving_lane_by_station_day,
)


def _evidence_lanes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "CREATE TABLE calibration_station_daily_high_truth ("
        " station_id TEXT, local_date TEXT, official_tmax_c REAL,"
        " revision_no INTEGER DEFAULT 0, source_lane TEXT DEFAULT 'AWC',"
        " settlement_definition TEXT DEFAULT 'official_daily_tmax_c');"
        "CREATE TABLE basket_probability_bridge_history ("
        " station_id TEXT, event_date TEXT, history_id TEXT,"
        " envelope_json TEXT, decision_at_utc TEXT, stage TEXT,"
        " checkpoint_id TEXT DEFAULT '',"
        " envelope_content_sha256 TEXT DEFAULT '');"
        "CREATE TABLE basket_probability_bridge_bucket_probability ("
        " history_id TEXT, bucket TEXT, lower_c REAL, upper_c REAL,"
        " probability REAL);"
    )


def _decision(
    conn: sqlite3.Connection,
    *,
    day: str,
    decided: str,
    history_id: str,
    buckets: list[tuple[str, float, float, float]],
    envelope: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO basket_probability_bridge_history"
        " (station_id, event_date, history_id, envelope_json,"
        "  decision_at_utc, stage) VALUES (?,?,?,?,?,?)",
        ("ZBAA", day, history_id, json.dumps(envelope or {}), decided, "P0"),
    )
    for bucket, lower, upper, probability in buckets:
        conn.execute(
            "INSERT INTO basket_probability_bridge_bucket_probability"
            " VALUES (?,?,?,?,?)",
            (history_id, bucket, lower, upper, probability),
        )


def _two(warm: float) -> list[tuple[str, float, float, float]]:
    return [("29°C", 28.5, 29.5, 1.0 - warm), ("30°C", 29.5, 30.5, warm)]


def _claims(lane: dict, day: str) -> list[tuple[float, int]]:
    ((entries,),) = [rows for (_s, event), rows in lane.items() if event == day]
    return sorted((round(claim, 6), hit) for claim, hit, _lower in entries)


def test_receipt_inversion_recovers_the_preband_claim() -> None:
    """Decisions served before the retirement are read as they stood
    before the band table touched them."""

    raw = [(0.06, "34"), (0.15, "35"), (0.79, "36")]
    factors = {"34": 0.86, "35": 1.08}
    served_mass = sum(p * factors.get(k, 1.0) for p, k in raw)
    served = [(p * factors.get(k, 1.0) / served_mass, k) for p, k in raw]
    receipt = {
        "state": "APPLIED",
        "applied_factors": factors,
        "pre_normalisation_mass": served_mass,
    }
    recovered = _invert_band_receipt(served, receipt)
    for (probability, key), (expected, expected_key) in zip(recovered, raw):
        assert key == expected_key
        assert abs(probability - expected) < 1e-12


def test_a_banded_row_is_inverted_and_an_unbanded_one_is_read_as_served() -> None:
    conn = sqlite3.connect(":memory:")
    _evidence_lanes(conn)
    served_mass = 0.3 * 0.8 + 0.7
    receipt = {
        "state": "APPLIED",
        "applied_factors": {"29°C": 0.8},
        "pre_normalisation_mass": served_mass,
    }
    _decision(
        conn, day="2026-09-20", decided="2026-09-19T02:00:00Z",
        history_id="h_banded",
        buckets=[
            ("29°C", 28.5, 29.5, 0.3 * 0.8 / served_mass),
            ("30°C", 29.5, 30.5, 0.7 / served_mass),
        ],
        envelope={"lineage": {"band_reliability_json": json.dumps(receipt)}},
    )
    _decision(
        conn, day="2026-09-28", decided="2026-09-27T02:00:00Z",
        history_id="h_after", buckets=_two(0.6),
    )
    truth = {("ZBAA", "2026-09-20"): 30.0, ("ZBAA", "2026-09-28"): 30.0}

    pre_band = serving_lane_by_station_day(conn, truth, stage="P0")
    assert _claims(pre_band, "2026-09-20") == [(0.3, 0), (0.7, 1)]
    assert _claims(pre_band, "2026-09-28") == [(0.4, 0), (0.6, 1)]

    as_served = serving_lane_by_station_day(
        conn, truth, stage="P0", pre_band=False
    )
    assert _claims(as_served, "2026-09-20") == [
        (round(0.3 * 0.8 / served_mass, 6), 0),
        (round(0.7 / served_mass, 6), 1),
    ]
    assert _claims(as_served, "2026-09-28") == [(0.4, 0), (0.6, 1)]


def test_the_lane_reads_the_first_decision_not_the_last() -> None:
    """Fitted where the seat is priced, not where the day ends.

    Within the first burst two rows share the decision clock -- the P1
    prefix-chain shape -- and only the later row is the complete belief.
    """

    conn = sqlite3.connect(":memory:")
    _evidence_lanes(conn)
    _decision(conn, day="2026-08-20", decided="2026-08-20T02:00:00Z",
              history_id="h_early_partial", buckets=_two(0.1))
    _decision(conn, day="2026-08-20", decided="2026-08-20T02:00:00Z",
              history_id="h_early_full", buckets=_two(0.7))
    _decision(conn, day="2026-08-20", decided="2026-08-20T22:00:00Z",
              history_id="h_late", buckets=_two(0.99))
    lane = serving_lane_by_station_day(
        conn, {("ZBAA", "2026-08-20"): 30.0}, stage="P0"
    )
    assert _claims(lane, "2026-08-20") == [(0.3, 0), (0.7, 1)]


def test_the_bound_reaches_the_inner_first_decision() -> None:
    """Bounded, the lane keeps the first decision THIS arithmetic made
    rather than dropping the station-day."""

    conn = sqlite3.connect(":memory:")
    _evidence_lanes(conn)
    _decision(conn, day="2026-08-20", decided="2026-08-20T02:00:00Z",
              history_id="h_retired", buckets=_two(0.10))
    _decision(conn, day="2026-08-20", decided="2026-08-20T09:00:00Z",
              history_id="h_current", buckets=_two(0.70))
    truth = {("ZBAA", "2026-08-20"): 30.0}
    assert _claims(
        serving_lane_by_station_day(conn, truth, stage="P0"), "2026-08-20"
    ) == [(0.1, 1), (0.9, 0)]
    bounded = serving_lane_by_station_day(
        conn, truth, stage="P0", min_decision_at_utc="2026-08-20T06:00:00Z"
    )
    assert _claims(bounded, "2026-08-20") == [(0.3, 0), (0.7, 1)]


def test_the_bound_excludes_the_days_it_names() -> None:
    conn = sqlite3.connect(":memory:")
    _evidence_lanes(conn)
    days = ("2026-08-10", "2026-08-11", "2026-08-20")
    for day in days:
        _decision(conn, day=day, decided=f"{day}T09:00:00Z",
                  history_id=f"h_{day}", buckets=_two(0.7))
    truth = {("ZBAA", day): 30.0 for day in days}
    assert len(serving_lane_by_station_day(conn, truth, stage="P0")) == 3
    bounded = serving_lane_by_station_day(
        conn, truth, stage="P0", min_decision_at_utc="2026-08-19T00:00:00Z"
    )
    assert [event for _station, event in bounded] == ["2026-08-20"]


def test_write_lock_is_retried_for_contention_only(monkeypatch) -> None:
    """Wait out the resident; surface anything else on the first try."""

    slept: list[float] = []
    monkeypatch.setattr(store.time, "sleep", slept.append)

    class _Writer:
        def __init__(self, failures: int, message: str) -> None:
            self.remaining = failures
            self.message = message
            self.attempts = 0

        def execute(self, _sql: str) -> None:
            self.attempts += 1
            if self.remaining > 0:
                self.remaining -= 1
                raise sqlite3.OperationalError(self.message)

    busy = _Writer(2, "database is locked")
    assert _begin_immediate(busy) == 3
    assert len(slept) == 2

    wedged = _Writer(99, "database is locked")
    with pytest.raises(sqlite3.OperationalError):
        _begin_immediate(wedged)
    assert wedged.attempts == store.WRITE_LOCK_ATTEMPTS

    # A missing table is a fault, not contention.
    broken = _Writer(99, "no such table: calibration_center_width_table")
    with pytest.raises(sqlite3.OperationalError):
        _begin_immediate(broken)
    assert broken.attempts == 1


def test_the_band_fit_and_its_loaders_are_gone() -> None:
    """Charter 232: nothing is left that could fit, activate or serve a band
    table, so no path can bring the layer back by accident."""

    for name in (
        "fit_band_reliability_table", "refit_all_stages", "load_band_table",
        "load_band_table_with_axis", "persist_and_activate_band_table",
        "band_table_axis", "pinned_band_table_sha",
    ):
        assert not hasattr(store, name), name
