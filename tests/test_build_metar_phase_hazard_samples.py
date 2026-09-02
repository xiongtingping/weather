"""The sample set the four-layer temperature path is fitted on.

What these pin is the part a fit cannot check for itself: that every row was
built from what was knowable at its own instant.  A leak here does not raise,
it produces a model that scores beautifully out of sample and loses money in
production -- the 2026-08-12 timezone bug is the precedent, where a "noon"
anchor read off the wrong column had already seen the day's peak and the
first results looked better than the human forecasters.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_metar_phase_hazard_samples import (  # noqa: E402
    MAX_REPORT_TRANSMISSION_LEAD_SECONDS,
    build_station_day,
    load_day_observations,
    sample_instant,
)

STATION = "OPKC"
EVENT_DATE = "2026-08-29"
TIMEZONE = "Asia/Karachi"
# Karachi is UTC+5, so the local day runs 19:00Z the previous day to 19:00Z.
DAY_START = datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc)


def _report(index: int, temperature: float, *, receipt_delay_s: float = 120.0):
    valid = DAY_START + timedelta(minutes=30 * index)
    committed = valid + timedelta(seconds=receipt_delay_s)
    return {
        "observation_id": f"rtobs-{index:03d}",
        "valid_at": valid.isoformat().replace("+00:00", "Z"),
        "canonical_committed_at": committed.isoformat().replace("+00:00", "Z"),
        "reported_temperature_c": temperature,
        "raw_text": (
            f"METAR {STATION} {valid.strftime('%d%H%M')}Z 24010KT 9999 "
            f"FEW020 {int(round(temperature)):02d}/18 Q1005 NOSIG"
        ),
        "report_type": "METAR",
    }


def _archive(rows) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reported_trajectory_observations("
        "observation_id TEXT, source_lane TEXT, station_id TEXT,"
        "report_type TEXT, valid_at TEXT, reported_temperature_c REAL,"
        "raw_text TEXT, canonical_committed_at TEXT,"
        "correction_suspected INTEGER)"
    )
    conn.executemany(
        "INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?,?,0)",
        [
            (
                row["observation_id"], "awc", STATION, row["report_type"],
                row["valid_at"], row["reported_temperature_c"],
                row["raw_text"], row["canonical_committed_at"],
            )
            for row in rows
        ],
    )
    return conn


def _target(root_decided_at: str = "2026-08-28T18:00:00.000Z"):
    """A target database holding one P0 root with real bucket geometry."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "envelope_id TEXT, stage TEXT, checkpoint_id TEXT, station_id TEXT,"
        "event_date TEXT, settlement_timezone TEXT, decision_at_utc TEXT,"
        "fresh_until_utc TEXT, bucket_ordinals_json TEXT,"
        "probabilities_json TEXT, posterior_version TEXT,"
        "posterior_sha256 TEXT, bridge_sha256 TEXT, lineage_json TEXT)"
    )
    buckets = ["28°C", "29°C", "30°C", "31°C", "32°C"]
    intervals = {
        "28°C": [27.5, 28.5], "29°C": [28.5, 29.5], "30°C": [29.5, 30.5],
        "31°C": [30.5, 31.5], "32°C": [31.5, 32.5],
    }
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "env-root", "P0", "p0-root-fixture", STATION, EVENT_DATE, TIMEZONE,
            root_decided_at, "2026-08-30T00:00:00.000Z",
            json.dumps({name: index for index, name in enumerate(buckets)}),
            json.dumps({
                "28°C": 0.1, "29°C": 0.2, "30°C": 0.4, "31°C": 0.2,
                "32°C": 0.1,
            }),
            "p0_fixture_v1", "a" * 64, "b" * 64,
            json.dumps({"bucket_intervals_c_json": json.dumps(intervals)}),
        ),
    )
    return conn


def _build(rows, *, truth: float, target=None):
    return build_station_day(
        target=target if target is not None else _target(),
        archive=_archive(rows),
        station_id=STATION,
        event_date=EVENT_DATE,
        settlement_timezone=TIMEZONE,
        truth_tmax_c=truth,
        source_lane="awc",
    )


def test_every_sample_sees_only_reports_already_received() -> None:
    """The prefix is what was knowable, not what exists now.

    One report is given a receipt an hour after its valid time, so between
    those two instants it exists but has not arrived.  Any sample taken in
    that window must not carry it -- and, because the running high is taken
    off the prefix, must not carry its temperature either.
    """

    rows = [_report(index, 27.0 + index) for index in range(6)]
    rows[2]["canonical_committed_at"] = (
        DAY_START + timedelta(hours=2)
    ).isoformat().replace("+00:00", "Z")
    rows[2]["reported_temperature_c"] = 40.0
    samples, _ = _build(rows, truth=32.0)

    late_instant = sample_instant(rows[2])
    for sample in samples:
        instant = datetime.fromisoformat(
            sample["as_of_utc"].replace("Z", "+00:00")
        )
        if instant < late_instant:
            assert sample["running_high_c"] < 40.0
            assert sample["prefix_report_count"] <= 2


def test_sample_instant_is_bounded_by_the_transmission_lead() -> None:
    """A backfilled receipt does not move the instant days into the future.

    Receipts are written when the row was stored, which on a backfilled day
    is long after the report existed.  WMKK 2026-08-04 anchored six days
    late that way, identically for all three of its buckets.
    """

    row = _report(4, 30.0, receipt_delay_s=6 * 24 * 3600.0)
    instant = sample_instant(row)
    valid = datetime.fromisoformat(row["valid_at"].replace("Z", "+00:00"))
    assert instant - valid <= timedelta(
        seconds=MAX_REPORT_TRANSMISSION_LEAD_SECONDS
    )


def test_target_is_remaining_warming_and_zero_means_the_day_is_done() -> None:
    rows = [_report(index, temperature) for index, temperature in enumerate(
        [27.0, 28.0, 29.0, 31.0, 31.0, 30.0]
    )]
    samples, _ = _build(rows, truth=31.0)
    by_prefix = {sample["prefix_report_count"]: sample for sample in samples}
    assert by_prefix[3]["delta_c"] == 2
    assert by_prefix[4]["delta_c"] == 0
    assert by_prefix[6]["delta_c"] == 0
    assert all(sample["truth_tmax_c"] == 31.0 for sample in samples)


def test_hours_since_high_refresh_stays_zero_while_the_high_is_held() -> None:
    """A flat afternoon at the day's high has not turned over.

    Ties go to the latest report: a counter that ran while the temperature
    still sat on its own ceiling would teach the phase layer that a steady
    afternoon is a cooling one.
    """

    rows = [_report(index, temperature) for index, temperature in enumerate(
        [27.0, 30.0, 30.0, 30.0, 28.0]
    )]
    samples, _ = _build(rows, truth=30.0)
    by_prefix = {sample["prefix_report_count"]: sample for sample in samples}
    assert by_prefix[4]["hours_since_high_refresh"] == pytest.approx(0.0)
    assert by_prefix[5]["hours_since_high_refresh"] == pytest.approx(0.5, abs=0.02)


def test_a_running_high_above_the_settled_truth_is_excluded_not_clipped() -> None:
    """That disagreement is the correction lane's, not a cool day.

    Clipping it to zero would teach the model that the day was over when the
    settlement ledger says it was not.
    """

    rows = [_report(index, temperature) for index, temperature in enumerate(
        [27.0, 28.0, 33.0, 33.0, 33.0]
    )]
    samples, exclusions = _build(rows, truth=30.0)
    reasons = {reason for reason, _ in exclusions}
    assert "running_high_above_truth" in reasons
    assert all(sample["running_high_c"] <= 30.0 for sample in samples)


def test_station_day_weights_sum_to_one() -> None:
    """Stations file at different rates; a day is a day either way.

    A station reporting twice an hour would otherwise carry twice the vote on
    what a fleet-wide hour means.
    """

    rows = [_report(index, 27.0 + index * 0.5) for index in range(10)]
    samples, _ = _build(rows, truth=32.0)
    assert samples
    assert sum(sample["station_day_weight"] for sample in samples) == (
        pytest.approx(1.0)
    )


def test_a_root_committed_after_the_instant_is_not_used() -> None:
    """The sample sees the root production was serving then.

    Roots are re-decided through the day; taking the day's final root would
    let a 09:00 sample read a rebase that happened at 15:00.
    """

    rows = [_report(index, 27.0 + index) for index in range(6)]
    late_root = _target(root_decided_at="2026-08-29T12:00:00.000Z")
    samples, exclusions = _build(rows, truth=32.0, target=late_root)
    assert not samples
    assert {reason for reason, _ in exclusions} == {"no_root_as_of"}
