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
    _record_station_day_failure,
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
        # The reader selects it, so the fixture has to carry it; a column the
        # production table has and this one did not turned every test in this
        # file into an OperationalError rather than a finding.
        "raw_sha256": f"{index:064d}",
    }


def _archive(rows) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reported_trajectory_observations("
        "observation_id TEXT, source_lane TEXT, station_id TEXT,"
        "report_type TEXT, valid_at TEXT, reported_temperature_c REAL,"
        "raw_text TEXT, canonical_committed_at TEXT,"
        "correction_suspected INTEGER, raw_sha256 TEXT)"
    )
    conn.executemany(
        "INSERT INTO reported_trajectory_observations "
        "VALUES(?,?,?,?,?,?,?,?,0,?)",
        [
            (
                row["observation_id"], "awc", STATION, row["report_type"],
                row["valid_at"], row["reported_temperature_c"],
                row["raw_text"], row["canonical_committed_at"],
                row.get("raw_sha256") or "0" * 64,
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
    assert late_instant == DAY_START + timedelta(hours=2)
    for sample in samples:
        instant = datetime.fromisoformat(
            sample["as_of_utc"].replace("Z", "+00:00")
        )
        arrived = [
            row for row in rows if sample_instant(row) <= instant
        ]
        assert sample["prefix_report_count"] == len(arrived)
        if instant < late_instant:
            assert sample["running_high_c"] < 40.0
            assert rows[2] not in arrived


def test_a_backfilled_receipt_is_not_a_decision_instant_of_that_day() -> None:
    """A report stored days later did not become knowable ninety seconds in.

    The instant is the canonical receipt, which is what ``p1_compiler`` clocks
    a step at.  This used to be bounded by the transmission lead -- an
    allowance for a report transmitted a few seconds *before* its nominal
    minute -- and using it as a ceiling on receipt wrote hours of real arrival
    delay out of the training set.  A receipt outside the settlement day is
    excluded as a sample instant rather than moved back inside it; the row
    still rides every later prefix on the day it actually arrived.
    """

    row = _report(4, 30.0, receipt_delay_s=6 * 24 * 3600.0)
    valid = datetime.fromisoformat(row["valid_at"].replace("Z", "+00:00"))
    assert sample_instant(row) - valid == timedelta(days=6)

    rows = [_report(index, 27.0 + index) for index in range(4)]
    rows.append(row)
    samples, exclusions = _build(rows, truth=32.0)
    # The path state needs three reports before it may speak, so the day's
    # first two instants produce nothing; what matters here is that the
    # backfilled fifth row produces no sample of its own.
    assert [sample["prefix_report_count"] for sample in samples] == [3, 4]
    assert ("receipt_outside_event_day", 1) in [
        (reason, count) for reason, count in exclusions
    ] or any(reason == "receipt_outside_event_day" for reason, _ in exclusions)


def test_a_report_whose_minute_leads_its_receipt_too_far_is_refused() -> None:
    """The reader's own time-order gate, asked of a historical row."""

    row = _report(2, 29.0)
    row["canonical_committed_at"] = (
        datetime.fromisoformat(row["valid_at"].replace("Z", "+00:00"))
        - timedelta(seconds=MAX_REPORT_TRANSMISSION_LEAD_SECONDS + 1)
    ).isoformat().replace("+00:00", "Z")
    rows = [_report(0, 27.0), _report(1, 28.0), row, _report(3, 30.0)]
    samples, exclusions = _build(rows, truth=32.0)
    assert any(reason == "report_time_reversed" for reason, _ in exclusions)
    assert max(
        sample["prefix_report_count"] for sample in samples
    ) == 3


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


def test_the_phase_label_is_a_bucket_crossing_not_a_rounded_degree() -> None:
    """``delta_c >= 1`` and "settled a rung higher" are different questions.

    The served rate is assigned to the mass above the bucket the running high
    sits in, so the label has to be that crossing.  Here the day rises 0.8C
    without leaving its rung: the rounded target says "it rose", the ladder
    says it did not, and the ladder is what the split is rescaled on.
    """

    rows = [_report(index, temperature) for index, temperature in enumerate(
        [27.0, 28.0, 29.0, 29.6, 29.6, 29.6]
    )]
    samples, _ = _build(rows, truth=30.4)
    assert samples
    by_prefix = {sample["prefix_report_count"]: sample for sample in samples}
    assert by_prefix[6]["running_high_c"] == 29.6
    assert by_prefix[6]["delta_c"] == 1
    assert by_prefix[6]["crossed_bucket"] == 0
    # And where it does cross, both agree.
    crossing, _ = _build(
        [_report(index, temperature) for index, temperature in enumerate(
            [27.0, 28.0, 29.0, 29.6, 29.6, 29.6]
        )],
        truth=31.0,
    )
    assert crossing[-1]["delta_c"] == 1
    assert crossing[-1]["crossed_bucket"] == 1


# -- The local day is an instant window, not a string range (2026-09-24) ----
#
# The archive stores ``valid_at`` as ``...T19:00:00.000Z``; the settlement
# window is spelled ``...T19:00:00Z``.  Compared as text, ``'.' < 'Z'`` put the
# local-midnight report before the day's own start (dropped) and the next
# local midnight before the day's end (kept).  The fixtures above spell both
# sides the same way, which is why none of them could see it; these spell the
# archive the way the archive is spelled.


def _archive_report(index: int, temperature: float, *, receipt_delay_s=120.0):
    row = _report(index, temperature, receipt_delay_s=receipt_delay_s)
    for key in ("valid_at", "canonical_committed_at"):
        instant = datetime.fromisoformat(row[key].replace("Z", "+00:00"))
        row[key] = instant.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
    return row


def test_the_local_midnight_report_opens_the_day_and_the_next_one_does_not() -> None:
    from bayes_basket_target.settlement_time import settlement_day_window

    window = settlement_day_window(EVENT_DATE, TIMEZONE)
    assert window["target_day_start_utc"] == "2026-08-28T19:00:00Z"
    # 00:00 local, 00:30, 01:00 ... and 00:00 of the next local day.
    rows = [_archive_report(index, 20.0 + index) for index in range(3)]
    rows.append(_archive_report(48, 45.0))
    assert rows[0]["valid_at"] == "2026-08-28T19:00:00.000Z"
    assert rows[-1]["valid_at"] == "2026-08-29T19:00:00.000Z"

    loaded = load_day_observations(
        _archive(rows),
        station_id=STATION,
        window_start_utc=window["target_day_start_utc"],
        window_end_utc=window["target_day_end_utc"],
        source_lane="awc",
    )
    assert [row["valid_at"] for row in loaded] == [
        "2026-08-28T19:00:00.000Z",
        "2026-08-28T19:30:00.000Z",
        "2026-08-28T20:00:00.000Z",
    ]


def test_warming_so_far_is_measured_from_the_local_midnight_report() -> None:
    """The first report of the day is the one the serving reader starts from.

    ``resident_canonical`` admits ``[start, end)`` on ``julianday``, so the
    serving ``warming_so_far_c`` is taken off the 00:00 local report; a
    training row that started from 00:30 learned a different feature.  The
    next local midnight -- here transmitted 60 s early, inside the reader's
    lead allowance, so its receipt falls inside the day -- must not ride the
    day's last prefix either.
    """

    rows = [
        _archive_report(index, temperature)
        for index, temperature in enumerate(
            [20.0, 26.0, 27.0, 28.0, 29.0, 30.0]
        )
    ]
    rows.append(_archive_report(48, 45.0, receipt_delay_s=-60.0))
    samples, exclusions = _build(rows, truth=30.0)
    assert samples
    by_prefix = {sample["prefix_report_count"]: sample for sample in samples}
    assert min(by_prefix) == 3
    assert by_prefix[3]["as_of_utc"] == "2026-08-28T20:02:00.000Z"
    assert by_prefix[3]["warming_so_far_c"] == pytest.approx(27.0 - 20.0)
    assert by_prefix[6]["warming_so_far_c"] == pytest.approx(30.0 - 20.0)
    assert max(sample["prefix_report_count"] for sample in samples) == 6
    assert all(sample["running_high_c"] <= 30.0 for sample in samples)
    assert not any(
        reason == "running_high_above_truth" for reason, _ in exclusions
    )


def test_roots_are_ordered_by_instant_not_by_spelling() -> None:
    """``decision_at_utc`` carries two spellings; ``root_at`` needs time order.

    ``...:00.500Z`` sorts before ``...:00Z`` as text and after it as an
    instant.  In text order the later root came first, ``root_at`` stopped on
    it, and a sample between the two found no root at all.
    """

    from scripts.build_metar_phase_hazard_samples import (  # noqa: PLC0415
        load_root_index,
        root_at,
    )

    target = _target(root_decided_at="2026-08-28T18:00:00Z")
    row = target.execute(
        "SELECT * FROM basket_probability_envelope"
    ).fetchone()
    later = list(row)
    later[0] = "env-root-later"
    later[2] = "p0-root-later"
    later[6] = "2026-08-28T18:00:00.500Z"
    target.execute(
        "INSERT INTO basket_probability_envelope"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        later,
    )
    roots = load_root_index(target, station_id=STATION, event_date=EVENT_DATE)
    assert [item[1]["checkpoint_id"] for item in roots] == [
        "p0-root-fixture", "p0-root-later",
    ]
    chosen = root_at(roots, datetime(2026, 8, 28, 18, 0, 0, 200000, tzinfo=timezone.utc))
    assert chosen is not None
    assert chosen[1]["checkpoint_id"] == "p0-root-fixture"


@pytest.mark.parametrize("missing_parent", [False, True])
def test_failed_station_day_write_preserves_both_errors_in_status_tail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    missing_parent: bool,
) -> None:
    """A failed exclusion write must preserve the earlier station-day error.

    The hosted sweep stores only a short output tail.  Keep the diagnostic
    useful inside that limit while reporting the SQLite codes and the
    contemporaneous output-directory state.
    """

    with pytest.raises(sqlite3.OperationalError) as write_error:
        sqlite3.connect(tmp_path / "missing-output" / "samples.db")
    out_path = tmp_path / "p1_observation_samples.db.new"
    if missing_parent:
        out_path = tmp_path / "unavailable-parent" / out_path.name

    class FailedOutput:
        def execute(self, *_args, **_kwargs):
            raise write_error.value

    try:
        sqlite3.connect(tmp_path / "missing-source" / "source.db")
    except sqlite3.Error as original_error:
        recorded = _record_station_day_failure(
            out=FailedOutput(),
            station_id="KATL",
            event_date="2026-10-22",
            original_error=original_error,
            out_path=out_path,
        )
    else:
        pytest.fail("the fixture must produce the original SQLite error")
    output = capsys.readouterr().err
    diagnostic = output.strip().splitlines()[-1]
    failure_tail = (
        output
        + "sample build failed; the refit below reads yesterday's samples"
    )[-300:]

    assert not recorded
    assert "unable to open database file" in output
    assert "SQLITE_CANTOPEN" in output
    assert "During handling of the above exception" in output
    assert "P1DBFAIL st=KATL d=2026-10-22" in diagnostic
    assert diagnostic.count("SQLITE_CANTOPEN") == 2
    assert "p1_observation_samples.db.new" in diagnostic
    assert "journal=missing" in diagnostic
    assert len(diagnostic) <= 220
    assert diagnostic in failure_tail
    if missing_parent:
        assert "dir=0/0" in diagnostic
        assert "freeMiB=?" in diagnostic
    else:
        assert "dir=1/1" in diagnostic
        assert "freeMiB=" in diagnostic


def test_station_day_failure_record_is_still_written() -> None:
    out = sqlite3.connect(":memory:")
    out.execute(
        "CREATE TABLE metar_phase_hazard_exclusion("
        "station_id TEXT,event_date TEXT,reason TEXT,detail TEXT,count INTEGER)"
    )

    try:
        raise RuntimeError("station-day fixture failure")
    except RuntimeError as original_error:
        recorded = _record_station_day_failure(
            out=out,
            station_id="KATL",
            event_date="2026-10-22",
            original_error=original_error,
            out_path=Path("samples.db.new"),
        )

    assert recorded
    assert out.execute(
        "SELECT station_id,event_date,reason,detail,count "
        "FROM metar_phase_hazard_exclusion"
    ).fetchone() == (
        "KATL", "2026-10-22", "station_day_failed",
        "RuntimeError: station-day fixture failure", 1,
    )
    out.close()
