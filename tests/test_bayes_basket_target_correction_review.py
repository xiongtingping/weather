from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target.correction_review import (
    CLEAR_REFERENCE_ONLY,
    CLEAR_WITH_REPLACEMENT,
    QUARANTINE_STATION_DAY,
    append_correction_review,
    assert_station_day_corrections_resolved,
    auto_clear_station_day_corrections,
)
from weather_basket_yes_schema import (
    apply_reported_trajectory_correction_review_schema,
    apply_weather_basket_yes_schema,
)


START = "2026-07-27T15:00:00Z"
END = "2026-07-28T15:00:00Z"
AS_OF = "2026-07-28T05:00:00Z"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(conn)
    conn.execute(
        """
        INSERT INTO reported_trajectory_market_registry(
          slug,event_date,outcome,city_slug,station_id,
          settlement_timezone,settlement_unit,source_lane,
          source_eligible,source_block_reason,resolution_url,rules_sha256,
          yes_token,no_token,market_active,market_accepting_orders,
          registry_json,refreshed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "rkpk-high-2026-07-28", "2026-07-28", "35°C",
            "busan", "RKPK", "Asia/Seoul", "celsius", "awc", 1,
            "", "https://example.invalid", "a" * 64,
            "yes", "no", 1, 1, "{}", "2026-07-27T14:00:00Z",
        ),
    )
    return conn


def _observation(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    valid_at: str,
    correction: int,
) -> None:
    conn.execute(
        """
        INSERT INTO reported_trajectory_observations(
          observation_id,source_lane,station_id,report_type,valid_at,
          reported_temperature_c,raw_sha256,raw_text,first_seen_at,
          parsed_at,canonical_committed_at,fetch_started_at,
          fetch_completed_at,source_poll_id,correction_suspected
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            observation_id, "awc", "RKPK",
            "SPECI" if correction else "METAR",
            valid_at, 34 if correction else 35,
            ("b" if correction else "a") * 64,
            "SPECI RKPK COR" if correction else "METAR RKPK",
            "2026-07-28T03:25:00Z",
            "2026-07-28T03:25:01Z",
            "2026-07-28T03:25:02Z",
            "2026-07-28T03:24:59Z",
            "2026-07-28T03:25:00Z",
            "poll-1",
            correction,
        ),
    )


def _state(conn: sqlite3.Connection) -> dict:
    return assert_station_day_corrections_resolved(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        window_start_utc=START,
        window_end_utc=END,
        as_of_utc=AS_OF,
    )


def _hourly_observation(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    temperature_c: float,
    correction: int,
) -> None:
    """One on-hour report, which is eligible truth input either way."""

    conn.execute(
        """
        INSERT INTO reported_trajectory_observations(
          observation_id,source_lane,station_id,report_type,valid_at,
          reported_temperature_c,raw_sha256,raw_text,first_seen_at,
          parsed_at,canonical_committed_at,fetch_started_at,
          fetch_completed_at,source_poll_id,correction_suspected
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            observation_id, "awc", "RKPK", "METAR",
            "2026-07-28T03:00:00Z", temperature_c,
            hashlib.sha256(observation_id.encode("utf-8")).hexdigest(),
            "METAR RKPK COR" if correction else "METAR RKPK",
            "2026-07-28T03:05:00Z", "2026-07-28T03:05:01Z",
            "2026-07-28T03:05:02Z", "2026-07-28T03:04:59Z",
            "2026-07-28T03:05:00Z", "poll-1", correction,
        ),
    )


def test_a_fully_reviewed_day_settles_at_the_cadence_a_station_actually_has() -> None:
    """The two cap checks count different sets, so sixteen stranded a day the
    standing policy had already cleared in full.

    ``auto_clear_station_day_corrections`` counts corrections still lacking a
    receipt, so it cleared all of KMIA's without ever reaching the cap; then
    ``assert_station_day_corrections_resolved`` counted every correction in the
    window, reviewed or not, and refused the day anyway.  Measured 2026-09-02,
    KMIA had issued a COR for nearly every hourly report since 08-29 -- 23 on
    08-30, 31 on 08-31, 17 on 09-01, none of them touching the temperature --
    and 08-30 and 08-31 were unsettleable no matter what anyone reviewed.
    """

    conn = _db()
    try:
        # Twenty-four half-hourly corrections inside the settlement window and
        # ahead of the as-of instant: past the old cap of sixteen, and an
        # ordinary day at a station that corrects what it publishes.
        start = datetime(2026, 7, 27, 15, 20, tzinfo=timezone.utc)
        for index in range(24):
            _observation(
                conn,
                observation_id=f"obs-cor-{index:02d}",
                valid_at=(
                    (start + timedelta(minutes=30 * index))
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
                correction=1,
            )

        cleared = auto_clear_station_day_corrections(
            conn,
            source_lane="awc",
            station_id="RKPK",
            event_date="2026-07-28",
            window_start_utc=START,
            window_end_utc=END,
            as_of_utc=AS_OF,
        )

        # Every off-hour correction is reference-only, so none of them can move
        # the day's maximum -- the cap was never what kept them honest.
        assert cleared["cleared_count"] == 24
        state = _state(conn)
        assert state["correction_count"] == 24
        assert len(state["review_ids"]) == 24
    finally:
        conn.close()


def test_on_hour_correction_clears_only_under_the_replacement_contract() -> None:
    """An on-hour report is eligible truth input, so it cannot be demoted.

    Clearing it as reference-only would silently drop a reading the day's
    maximum is entitled to, which is why that combination stays refused.  The
    replacement contract says the opposite -- the correction stands in for what
    it corrected and is counted normally -- and that is what lets the halt
    clear without discarding evidence.
    """

    conn = _db()
    _hourly_observation(
        conn, observation_id="cor-0300", temperature_c=35.0, correction=1
    )

    with pytest.raises(
        ContractError,
        match="correction_review_hourly_clear_requires_replacement_contract",
    ):
        append_correction_review(
            conn,
            source_lane="awc",
            station_id="RKPK",
            event_date="2026-07-28",
            triggering_observation_id="cor-0300",
            decision=CLEAR_REFERENCE_ONLY,
            reviewer="reviewer.test",
            rationale="reference only",
            reviewed_at=AS_OF,
        )

    append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0300",
        decision=CLEAR_WITH_REPLACEMENT,
        reviewer="reviewer.test",
        rationale="stands in for the report it corrected",
        reviewed_at=AS_OF,
    )
    assert _state(conn)["correction_count"] == 1


def test_replacement_is_writable_over_a_disagreeing_report() -> None:
    """The receipt says the correction stands in the slot, not that peers agree.

    It used to be refused whenever another report for the same instant carried
    a different temperature.  Two later rulings took that question off the
    reviewer -- a same-slot disagreement settles by publication order
    (2026-09-02) and a correction supersedes what it corrects (2026-09-03) --
    so the refusal left the one case with a rule downstream no way to reach
    it: VILK 2026-08-28 was quarantined by hand and sat six days without a
    truth row.  A person can still stop a station-day with QUARANTINE.
    """

    conn = _db()
    _hourly_observation(
        conn, observation_id="cor-0300", temperature_c=35.0, correction=1
    )
    _hourly_observation(
        conn, observation_id="peer-0300", temperature_c=31.0, correction=0
    )

    receipt = append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0300",
        decision=CLEAR_WITH_REPLACEMENT,
        reviewer="reviewer.test",
        rationale="stands in for the report it corrected",
        reviewed_at=AS_OF,
    )

    assert receipt["review_id"]
    assert _state(conn)["correction_count"] == 1


def test_replacement_contract_does_not_apply_to_off_hour_reports() -> None:
    # An off-hour report is not truth input to begin with, so there is nothing
    # for a replacement to stand in for; reference-only is the honest record.
    conn = _db()
    _observation(
        conn,
        observation_id="cor-0315",
        valid_at="2026-07-28T03:15:00Z",
        correction=1,
    )

    with pytest.raises(
        ContractError,
        match="correction_review_replacement_requires_hourly_observation",
    ):
        append_correction_review(
            conn,
            source_lane="awc",
            station_id="RKPK",
            event_date="2026-07-28",
            triggering_observation_id="cor-0315",
            decision=CLEAR_WITH_REPLACEMENT,
            reviewer="reviewer.test",
            rationale="replacement",
            reviewed_at=AS_OF,
        )


def test_auto_clear_picks_the_contract_each_report_is_entitled_to() -> None:
    conn = _db()
    _observation(
        conn,
        observation_id="cor-0315",
        valid_at="2026-07-28T03:15:00Z",
        correction=1,
    )
    _hourly_observation(
        conn, observation_id="cor-0300", temperature_c=35.0, correction=1
    )

    result = auto_clear_station_day_corrections(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        window_start_utc=START,
        window_end_utc=END,
        as_of_utc=AS_OF,
    )

    assert result["cleared_count"] == 2
    decisions = {
        str(row[0]): str(row[1])
        for row in conn.execute(
            "SELECT triggering_observation_id,decision "
            "FROM reported_trajectory_station_halt_reviews"
        )
    }
    assert decisions == {
        "cor-0315": CLEAR_REFERENCE_ONLY,
        "cor-0300": CLEAR_WITH_REPLACEMENT,
    }
    assert _state(conn)["correction_count"] == 2


def test_auto_clear_settles_a_disagreeing_slot_instead_of_stranding_it() -> None:
    """A same-slot temperature disagreement no longer stays pending.

    It used to be left contested for a person, and nobody came: VILK
    2026-08-28 was quarantined by hand and had no truth row six days later,
    while the settled lane had had a rule for it since 2026-09-02 -- the
    later report supersedes the earlier.  The receipt says the correction
    stands in the slot; which temperature settles is the lane's own call.
    """

    conn = _db()
    _observation(
        conn,
        observation_id="cor-0315",
        valid_at="2026-07-28T03:15:00Z",
        correction=1,
    )
    _hourly_observation(
        conn, observation_id="cor-0300", temperature_c=35.0, correction=1
    )
    _hourly_observation(
        conn, observation_id="peer-0300", temperature_c=31.0, correction=0
    )

    result = auto_clear_station_day_corrections(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        window_start_utc=START,
        window_end_utc=END,
        as_of_utc=AS_OF,
    )

    assert sorted(result["cleared_observation_ids"]) == ["cor-0300", "cor-0315"]
    assert _state(conn)["correction_count"] == 2


def test_unreviewed_correction_blocks_and_clear_receipt_is_idempotent() -> None:
    conn = _db()
    _observation(
        conn,
        observation_id="cor-0315",
        valid_at="2026-07-28T03:15:00Z",
        correction=1,
    )
    with pytest.raises(
        ContractError,
        match="correction_review_required:cor-0315",
    ):
        _state(conn)

    first = append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0315",
        decision=CLEAR_REFERENCE_ONLY,
        reviewer="operator-xiong",
        rationale="COR applies only to the non-hourly SPECI; hourly METAR remains formal.",
        reviewed_at="2026-07-28T04:30:00Z",
    )
    repeated = append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0315",
        decision=CLEAR_REFERENCE_ONLY,
        reviewer="operator-xiong",
        rationale="COR applies only to the non-hourly SPECI; hourly METAR remains formal.",
        reviewed_at="2026-07-28T04:30:00Z",
    )
    resolved = _state(conn)

    assert first["state"] == "committed"
    assert repeated["state"] == "idempotent"
    assert resolved["state"] == "CLEARED_REFERENCE_ONLY"
    assert resolved["review_ids"] == [first["review_id"]]


def test_latest_quarantine_supersedes_clear_and_blocks_station_day() -> None:
    conn = _db()
    _observation(
        conn,
        observation_id="cor-0315",
        valid_at="2026-07-28T03:15:00Z",
        correction=1,
    )
    append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0315",
        decision=CLEAR_REFERENCE_ONLY,
        reviewer="operator-xiong",
        rationale="Initial review.",
        reviewed_at="2026-07-28T04:20:00Z",
    )
    append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0315",
        decision=QUARANTINE_STATION_DAY,
        reviewer="operator-xiong",
        rationale="Subsequent evidence makes the station day ambiguous.",
        reviewed_at="2026-07-28T04:30:00Z",
    )

    with pytest.raises(
        ContractError,
        match="correction_review_quarantines_station_day",
    ):
        _state(conn)


def test_hourly_correction_cannot_be_cleared_without_replacement_contract() -> None:
    conn = _db()
    _observation(
        conn,
        observation_id="cor-0300",
        valid_at="2026-07-28T03:00:00Z",
        correction=1,
    )

    with pytest.raises(
        ContractError,
        match="hourly_clear_requires_replacement_contract",
    ):
        append_correction_review(
            conn,
            source_lane="awc",
            station_id="RKPK",
            event_date="2026-07-28",
            triggering_observation_id="cor-0300",
            decision=CLEAR_REFERENCE_ONLY,
            reviewer="operator-xiong",
            rationale="Invalid attempt to clear an hourly correction.",
            reviewed_at="2026-07-28T04:30:00Z",
        )


def test_review_rows_are_append_only() -> None:
    conn = _db()
    _observation(
        conn,
        observation_id="cor-0315",
        valid_at="2026-07-28T03:15:00Z",
        correction=1,
    )
    receipt = append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0315",
        decision=CLEAR_REFERENCE_ONLY,
        reviewer="operator-xiong",
        rationale="Reviewed.",
        reviewed_at="2026-07-28T04:30:00Z",
    )

    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(
            "UPDATE reported_trajectory_station_halt_reviews "
            "SET rationale='changed' WHERE review_id=?",
            (receipt["review_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(
            "DELETE FROM reported_trajectory_station_halt_reviews "
            "WHERE review_id=?",
            (receipt["review_id"],),
        )


def test_additive_schema_migration_preserves_existing_observations() -> None:
    conn = _db()
    conn.execute("DROP TRIGGER reported_trajectory_halt_reviews_no_update")
    conn.execute("DROP TRIGGER reported_trajectory_halt_reviews_no_delete")
    conn.execute("DROP TABLE reported_trajectory_station_halt_reviews")
    _observation(
        conn,
        observation_id="existing-cor",
        valid_at="2026-07-28T03:15:00Z",
        correction=1,
    )

    status = apply_reported_trajectory_correction_review_schema(conn)

    assert status["ok"] is True
    assert status["performs_legacy_backfill"] is False
    assert conn.execute(
        "SELECT observation_id FROM reported_trajectory_observations"
    ).fetchone()[0] == "existing-cor"
    assert conn.execute(
        "SELECT COUNT(*) FROM reported_trajectory_station_halt_reviews"
    ).fetchone()[0] == 0


def test_unflagged_same_slot_disagreement_can_reach_the_review_gate() -> None:
    """A measured same-slot disagreement is correction evidence by itself.

    Pinned by VILK 2026-08-28 01:00Z: a complete re-issue with a different
    temperature and no COR marker, so the upstream flag stayed 0 on both
    rows.  The trajectory reader fail-closed on the conflict every cycle,
    while this gate refused the only tool that could resolve it -- the
    review had to be written after a hand UPDATE of the flag, which must
    never be the process.  The gate's intent is "a genuine correction
    suspicion must exist", and two temperatures in one slot is that
    suspicion in its strongest form.
    """

    conn = _db()
    _hourly_observation(
        conn, observation_id="obs-first", temperature_c=26, correction=0
    )
    _hourly_observation(
        conn, observation_id="obs-reissue", temperature_c=27, correction=0
    )
    review = append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="obs-reissue",
        decision=QUARANTINE_STATION_DAY,
        reviewer="test.reviewer",
        rationale="same-slot disagreement, quarantined",
        reviewed_at="2026-07-28T04:00:00Z",
    )
    assert review["state"] == "committed"


def test_unflagged_observation_without_conflict_still_refused() -> None:
    """The widened gate admits measured disagreement, not everything."""

    conn = _db()
    _hourly_observation(
        conn, observation_id="obs-lone", temperature_c=26, correction=0
    )
    with pytest.raises(ContractError, match="scope_invalid"):
        append_correction_review(
            conn,
            source_lane="awc",
            station_id="RKPK",
            event_date="2026-07-28",
            triggering_observation_id="obs-lone",
            decision=QUARANTINE_STATION_DAY,
            reviewer="test.reviewer",
            rationale="no conflict, no flag -- must be refused",
            reviewed_at="2026-07-28T04:00:00Z",
        )


# --- Charter 100 (2026-09-06): the halt row follows the review -----------------

def test_the_halt_write_back_is_in_the_appender() -> None:
    """Pinned on the source: the update lives beside the review insert, so a
    receipt and a halt can never disagree again."""

    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target" / "correction_review.py"
    ).read_text(encoding="utf-8")
    assert "UPDATE reported_trajectory_station_halts" in source
    assert 'halt_state = (\n        "cleared" if resolved_decision in CLEARING_DECISIONS else "active"' in source
    assert "CLEARING_DECISIONS = frozenset({CLEAR_REFERENCE_ONLY, CLEAR_WITH_REPLACEMENT})" in source
    worker = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target" / "production_worker.py"
    ).read_text(encoding="utf-8")
    assert "CORRECTION_AUTO_CLEAR_WINDOW_DAYS = 120" in worker, (
        "a halt that ages out of the pass is a station-day frozen for good"
    )
    recycler = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target" / "certain_win_recycle.py"
    ).read_text(encoding="utf-8")
    # Since 2026-09-18 (「一直挂」 / 「改为0.99」) the recycler judges no
    # winner at all -- every held leg is offered at 0.99 -- so it reads no
    # truth; what stays pinned is that it never reads the running bound for
    # anything but the timezone.
    assert "reported_trajectory_canonical_station_day_bounds" in recycler, (
        "the bound is still read, but only for the timezone fallback"
    )
    assert "SELECT observed_max_c" not in recycler, (
        "the money path reads the settled truth, not the running bound"
    )


def test_a_prelisting_day_is_reviewed_in_its_station_binding_window(tmp_path, monkeypatch) -> None:
    # KDEN, ruling 2026-10-21: a day before the station's first market has no
    # registry row; inside the prelisting allowance its timezone comes from
    # the station's market timezone binding in the SSOT.
    import json

    from bayes_basket_target import correction_review, settlement_basis

    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({
        "prelisting_truth_backfill": {"stations": [
            {"station_id": "RKPK", "from": "2026-07-20", "through": "2026-07-28"}]},
        "settlement_source_contract": {"cities": {"busan": {"station_id": "RKPK"}}},
    }))
    monkeypatch.setattr(settlement_basis, "CONFIG_PATH", baseline)
    ssot = tmp_path / "ssot.db"
    with sqlite3.connect(ssot) as target:
        target.execute(
            "CREATE TABLE calibration_station_market_timezone_binding(station_id TEXT, settlement_timezone TEXT)"
        )
        target.execute("INSERT INTO calibration_station_market_timezone_binding VALUES('RKPK','Asia/Seoul')")
    monkeypatch.setattr(correction_review, "PRELISTING_BINDING_DB", ssot)
    conn = _db()
    conn.execute("DELETE FROM reported_trajectory_market_registry")
    _observation(conn, observation_id="cor-0315", valid_at="2026-07-28T03:15:00Z", correction=1)
    result = auto_clear_station_day_corrections(
        conn, source_lane="awc", station_id="RKPK", event_date="2026-07-28",
        window_start_utc=START, window_end_utc=END, as_of_utc=AS_OF,
    )
    assert result["cleared_count"] == 1
    # outside the allowance the registry is still required
    baseline.write_text(json.dumps({
        "prelisting_truth_backfill": {"stations": [
            {"station_id": "RKPK", "from": "2026-07-20", "through": "2026-07-27"}]},
        "settlement_source_contract": {"cities": {"busan": {"station_id": "RKPK"}}},
    }))
    _observation(conn, observation_id="cor-0415", valid_at="2026-07-28T04:15:00Z", correction=1)
    with pytest.raises(ContractError, match="registry_scope_not_unique"):
        auto_clear_station_day_corrections(
            conn, source_lane="awc", station_id="RKPK", event_date="2026-07-28",
            window_start_utc=START, window_end_utc=END, as_of_utc=AS_OF,
        )
