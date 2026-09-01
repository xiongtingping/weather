"""M3-08..11 bridge/materialization/replay acceptance tests."""

from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target.contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from bayes_basket_target.probability_bridge import (
    SCHEMA,
    backfill_probability_bridges,
    build_xsb_projection,
    canonical_replay_probability_bridges,
    install_probability_bridge_schema,
    load_current_probability_bridge,
    materialize_probability_bridge,
    verify_probability_bridge_replay_equivalence,
)
from bayes_basket_target.materialization import insert_probability_envelope
from bayes_basket_target.ssot import apply_production_ssot_schema


def _envelope(
    *,
    stage: str = "P0",
    checkpoint_id: str = "p0-1",
    decision_at_utc: str = "2026-07-23T04:00:00Z",
    fresh_until_utc: str = "2026-07-23T12:00:00Z",
    probabilities: dict[str, float] | None = None,
) -> ProbabilityEnvelope:
    values = probabilities or {"35": 0.20, "36": 0.50, "37": 0.30}
    lineage = {
        "settlement_definition": "official_daily_tmax_c",
        "bucket_intervals_c_json": json.dumps(
            {"35": [None, 35.5], "36": [35.5, 36.5], "37": [36.5, None]},
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    if stage == "P1":
        lineage.update(
            {
                "canonical_observation_event_id": checkpoint_id + "-obs",
                "p0_root_checkpoint_id": "p0-1",
            }
        )
    return ProbabilityEnvelope.from_mapping(
        {
            "stage": stage,
            "checkpoint_id": checkpoint_id,
            "station_id": "RKPK",
            "event_date": "2026-07-23",
            "settlement_timezone": "Asia/Karachi",
            "decision_at_utc": decision_at_utc,
            "fresh_until_utc": fresh_until_utc,
            "bucket_ordinals": {"35": 0, "36": 1, "37": 2},
            "probabilities": values,
            "posterior_version": "test-v1",
            "posterior_sha256": "a" * 64,
            "bridge_sha256": "b" * 64,
            "lineage": lineage,
        }
    )


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    install_probability_bridge_schema(conn)
    return conn


def _counts(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
    return tuple(
        int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "basket_probability_envelope",
            "basket_probability_bridge_history",
            "basket_probability_bridge_latest",
            "basket_probability_bridge_bucket_probability",
        )
    )


def _materialize_all(conn: sqlite3.Connection, rows: list[ProbabilityEnvelope]) -> None:
    for row in rows:
        materialize_probability_bridge(conn, row, created_at_utc=row.decision_at_utc)


def _bridge_snapshot(conn: sqlite3.Connection) -> tuple[list[tuple], list[tuple], list[tuple]]:
    history = conn.execute(
        "SELECT history_id,stage,checkpoint_id,decision_at_utc,projection_sha256 "
        "FROM basket_probability_bridge_history ORDER BY decision_at_utc,stage,checkpoint_id"
    ).fetchall()
    latest = conn.execute(
        "SELECT station_id,event_date,stage,checkpoint_id,projection_sha256 "
        "FROM basket_probability_bridge_latest ORDER BY station_id,event_date"
    ).fetchall()
    buckets = conn.execute(
        "SELECT history_id,bucket,ordinal,probability FROM basket_probability_bridge_bucket_probability "
        "ORDER BY history_id,ordinal,bucket"
    ).fetchall()
    return history, latest, buckets


def test_xsb_freezes_bucket_map_binds_hash_and_requires_one_plus_minus_one_e9() -> None:
    envelope = _envelope()
    projection = build_xsb_projection(envelope)
    assert projection["bucket_map_sha256"]
    assert projection["projection_sha256"]
    assert [row["bucket"] for row in projection["probability_by_bucket"]] == ["35", "36", "37"]
    assert abs(sum(row["probability"] for row in projection["probability_by_bucket"]) - 1.0) <= 1e-9

    # Envelope validation permits a small floating-point tolerance.  The B
    # bridge deliberately tightens its externally consumed contract to 1e-9.
    near_miss = _envelope(probabilities={"35": 0.2, "36": 0.5, "37": 0.30000001})
    with pytest.raises(ContractError, match="xsb_probability_not_normalized"):
        build_xsb_projection(near_miss)

    changed_bridge = ProbabilityEnvelope.from_mapping({
        **envelope.to_dict(),
        "bridge_sha256": "c" * 64,
    })
    changed_contract = ProbabilityEnvelope.from_mapping({
        **envelope.to_dict(),
        "lineage": {
            **envelope.lineage,
            "bucket_intervals_c_json": json.dumps(
                {
                    "35": [None, 35.25],
                    "36": [35.25, 36.5],
                    "37": [36.5, None],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    })
    assert (
        build_xsb_projection(changed_bridge)["projection_sha256"]
        != projection["projection_sha256"]
    )
    assert (
        build_xsb_projection(changed_contract)["bucket_map_sha256"]
        != projection["bucket_map_sha256"]
    )

    noncanonical_ordinals = ProbabilityEnvelope.from_mapping({
        **envelope.to_dict(),
        "bucket_ordinals": {"35": 0, "36": 2, "37": 3},
    })
    with pytest.raises(
        ContractError,
        match="xsb_bucket_contract_ordinal_invalid",
    ):
        build_xsb_projection(noncanonical_ordinals)


@pytest.mark.parametrize("point", ["checkpoint", "history", "latest", "p_b:0", "p_b:1", "p_b:2", "p_b"])
def test_materialization_write_point_faults_are_all_or_zero(point: str) -> None:
    conn = _connection()

    def inject(current: str) -> None:
        if current == point:
            raise RuntimeError("injected:" + current)

    with pytest.raises(RuntimeError, match="injected"):
        materialize_probability_bridge(
            conn,
            _envelope(),
            created_at_utc="2026-07-23T04:00:00Z",
            failure_injector=inject,
        )
    assert _counts(conn) == (0, 0, 0, 0)


def test_post_write_resource_failure_is_still_zero_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection()
    calls = 0

    def capacity(_conn: sqlite3.Connection) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("post-write-capacity-failure")

    monkeypatch.setattr(
        "bayes_basket_target.probability_bridge."
        "assert_connection_target_capacity",
        capacity,
    )
    with pytest.raises(RuntimeError, match="post-write-capacity-failure"):
        materialize_probability_bridge(
            conn,
            _envelope(),
            created_at_utc="2026-07-23T04:00:00Z",
        )
    assert _counts(conn) == (0, 0, 0, 0)


def test_nested_transaction_failure_preserves_caller_rows_only() -> None:
    conn = _connection()
    conn.execute("CREATE TABLE caller_owned(value TEXT NOT NULL)")
    conn.execute("INSERT INTO caller_owned VALUES('before-bridge')")

    def inject(point: str) -> None:
        if point == "p_b:1":
            raise RuntimeError("nested-fault")

    with pytest.raises(RuntimeError, match="nested-fault"):
        materialize_probability_bridge(
            conn,
            _envelope(),
            created_at_utc="2026-07-23T04:00:00Z",
            failure_injector=inject,
        )
    assert conn.execute("SELECT value FROM caller_owned").fetchall() == [
        ("before-bridge",)
    ]
    assert _counts(conn) == (0, 0, 0, 0)
    assert conn.in_transaction is True


def test_p1_precedence_and_stale_or_corrupt_p1_never_bare_downgrade() -> None:
    conn = _connection()
    p0 = _envelope()
    p1 = _envelope(
        stage="P1",
        checkpoint_id="p1-stale",
        decision_at_utc="2026-07-23T05:00:00Z",
        fresh_until_utc="2026-07-23T06:00:00Z",
    )
    p0_new = _envelope(
        checkpoint_id="p0-new",
        decision_at_utc="2026-07-23T07:00:00Z",
    )
    _materialize_all(conn, [p0, p1])
    latest_before_lower_stage_replay = conn.execute(
        "SELECT content_sha256,updated_at_utc "
        "FROM basket_probability_bridge_latest "
        "WHERE station_id='RKPK' AND event_date='2026-07-23'"
    ).fetchone()
    lower_stage_result = materialize_probability_bridge(
        conn,
        p0_new,
        created_at_utc=p0_new.decision_at_utc,
    )
    latest_after_lower_stage_replay = conn.execute(
        "SELECT content_sha256,updated_at_utc "
        "FROM basket_probability_bridge_latest "
        "WHERE station_id='RKPK' AND event_date='2026-07-23'"
    ).fetchone()
    assert lower_stage_result["latest_wrote"] is False
    assert (
        latest_after_lower_stage_replay
        == latest_before_lower_stage_replay
    )
    # Past its cadence the P1 is still the newest thing anyone knows, so it
    # serves.  ``p0_new`` is fresh through 12:00 here on purpose: the point is
    # that a fresh lower stage never wins, whether the P1 is servable or not.
    served = load_current_probability_bridge(
        conn,
        station_id="RKPK",
        event_date="2026-07-23",
        as_of_utc="2026-07-23T07:30:00Z",
    )
    assert served["stage"] == "P1"
    assert served["checkpoint_id"] == "p1-stale"

    # Far enough past it and the envelope no longer describes this day.  That
    # blocks; it still does not hand authority back to the fresh P0.
    with pytest.raises(ContractError, match="xsb_latest_stage_stale_fail_closed"):
        load_current_probability_bridge(
            conn,
            station_id="RKPK",
            event_date="2026-07-23",
            as_of_utc="2026-07-23T09:00:01Z",
        )

    # Simulate on-disk corruption outside the append-only writer.  Selecting
    # P1 precedes p(B) verification, so this must fail rather than return P0.
    conn = _connection()
    p1_fresh = _envelope(stage="P1", checkpoint_id="p1-corrupt", decision_at_utc="2026-07-23T05:00:00Z")
    _materialize_all(conn, [p0, p1_fresh])
    conn.execute("DROP TRIGGER basket_probability_bridge_bucket_no_update")
    history_id = conn.execute(
        "SELECT history_id FROM basket_probability_bridge_history WHERE checkpoint_id='p1-corrupt'"
    ).fetchone()[0]
    conn.execute(
        "UPDATE basket_probability_bridge_bucket_probability SET probability=0.99 "
        "WHERE history_id=? AND bucket='35'",
        (history_id,),
    )
    with pytest.raises(ContractError, match="xsb_projection_bucket_row_mismatch"):
        load_current_probability_bridge(
            conn,
            station_id="RKPK",
            event_date="2026-07-23",
            as_of_utc="2026-07-23T05:30:00Z",
        )

    before_replay = _counts(conn)
    lower_after_corruption = _envelope(
        checkpoint_id="lower-after-corrupt-p1",
        decision_at_utc="2026-07-23T05:15:00Z",
    )
    with pytest.raises(
        ContractError,
        match="xsb_projection_bucket_row_mismatch",
    ):
        materialize_probability_bridge(
            conn,
            lower_after_corruption,
            created_at_utc=lower_after_corruption.decision_at_utc,
        )
    assert _counts(conn) == before_replay


def test_replay_is_idempotent_and_latest_pointer_drift_fails_closed() -> None:
    conn = _connection()
    envelope = _envelope()
    first = materialize_probability_bridge(
        conn, envelope, created_at_utc=envelope.decision_at_utc
    )
    before = _counts(conn)
    repeated = materialize_probability_bridge(
        conn, envelope, created_at_utc=envelope.decision_at_utc
    )
    assert first["history_wrote"] is True
    assert repeated["history_wrote"] is False
    assert repeated["latest_wrote"] is False
    assert _counts(conn) == before

    conn.execute(
        "UPDATE basket_probability_bridge_latest SET checkpoint_id='pointer-drift' "
        "WHERE station_id='RKPK' AND event_date='2026-07-23'"
    )
    with pytest.raises(ContractError, match="xsb_latest_pointer_content_sha256_mismatch"):
        load_current_probability_bridge(
            conn,
            station_id="RKPK",
            event_date="2026-07-23",
            as_of_utc="2026-07-23T04:30:00Z",
        )


def test_future_latest_pointer_still_requires_valid_content_hash() -> None:
    conn = _connection()
    _materialize_all(
        conn,
        [
            _envelope(),
            _envelope(
                stage="P1",
                checkpoint_id="future-p1",
                decision_at_utc="2026-07-23T06:00:00Z",
            ),
        ],
    )
    conn.execute(
        "UPDATE basket_probability_bridge_latest "
        "SET content_sha256=? WHERE station_id='RKPK'",
        ("0" * 64,),
    )
    with pytest.raises(
        ContractError,
        match="xsb_latest_pointer_content_sha256_mismatch",
    ):
        load_current_probability_bridge(
            conn,
            station_id="RKPK",
            event_date="2026-07-23",
            as_of_utc="2026-07-23T04:30:00Z",
        )


def test_rebuilt_latest_pointer_hash_binds_authority_creation_time() -> None:
    conn = _connection()
    p1 = _envelope(
        stage="P1",
        checkpoint_id="p1-authority",
        decision_at_utc="2026-07-23T05:00:00Z",
    )
    _materialize_all(conn, [_envelope(), p1])
    conn.execute(
        "DELETE FROM basket_probability_bridge_latest "
        "WHERE station_id='RKPK' AND event_date='2026-07-23'"
    )
    lower = _envelope(
        checkpoint_id="later-p0",
        decision_at_utc="2026-07-23T06:00:00Z",
    )
    materialize_probability_bridge(
        conn,
        lower,
        created_at_utc=lower.decision_at_utc,
    )
    current = load_current_probability_bridge(
        conn,
        station_id="RKPK",
        event_date="2026-07-23",
        as_of_utc="2026-07-23T05:30:00Z",
    )
    assert current["checkpoint_id"] == p1.checkpoint_id


def test_low_high_low_incremental_and_full_canonical_replay_are_deterministic() -> None:
    rows = [
        _envelope(),
        _envelope(stage="P1", checkpoint_id="p1-low", decision_at_utc="2026-07-23T05:00:00Z", probabilities={"35": 0.60, "36": 0.30, "37": 0.10}),
        _envelope(stage="P1", checkpoint_id="p1-high", decision_at_utc="2026-07-23T06:00:00Z", probabilities={"35": 0.10, "36": 0.30, "37": 0.60}),
        _envelope(stage="P1", checkpoint_id="p1-low-again", decision_at_utc="2026-07-23T07:00:00Z", probabilities={"35": 0.55, "36": 0.35, "37": 0.10}),
    ]
    full = _connection()
    incremental = _connection()
    _materialize_all(full, rows)
    _materialize_all(incremental, rows[:2])
    _materialize_all(incremental, rows[2:])
    assert _bridge_snapshot(full) == _bridge_snapshot(incremental)
    current = load_current_probability_bridge(
        incremental,
        station_id="RKPK",
        event_date="2026-07-23",
        as_of_utc="2026-07-23T07:30:00Z",
    )
    assert current["stage"] == "P1"
    assert current["checkpoint_id"] == "p1-low-again"
    assert abs(sum(row["probability"] for row in current["probability_by_bucket"]) - 1.0) <= 1e-9

    full_receipt = canonical_replay_probability_bridges(rows)
    prefix_receipt = canonical_replay_probability_bridges(rows[:2])
    suffix_receipt = canonical_replay_probability_bridges(rows[2:])
    assert full_receipt["authoritative_checkpoint_id"] == current["checkpoint_id"]
    assert prefix_receipt["rows"] + suffix_receipt["rows"] == full_receipt["rows"]
    equivalence = verify_probability_bridge_replay_equivalence(
        incremental,
        rows,
    )
    assert equivalence["state"] == "EQUIVALENT"
    assert equivalence["max_absolute_probability_difference"] <= 1e-9
    assert equivalence["equivalence_sha256"]


def test_out_of_order_incremental_arrival_equals_full_canonical_replay() -> None:
    rows = [
        _envelope(),
        _envelope(
            stage="P1",
            checkpoint_id="p1-first",
            decision_at_utc="2026-07-23T05:00:00Z",
            probabilities={"35": 0.6, "36": 0.3, "37": 0.1},
        ),
        _envelope(
            stage="P1",
            checkpoint_id="p1-latest",
            decision_at_utc="2026-07-23T07:00:00Z",
            probabilities={"35": 0.1, "36": 0.3, "37": 0.6},
        ),
    ]
    chronological = _connection()
    delayed = _connection()
    _materialize_all(chronological, rows)
    _materialize_all(delayed, [rows[2], rows[0], rows[1]])

    assert _bridge_snapshot(delayed) == _bridge_snapshot(chronological)
    receipt = verify_probability_bridge_replay_equivalence(delayed, rows)
    assert receipt["state"] == "EQUIVALENT"
    assert receipt["max_absolute_probability_difference"] <= 1e-9


def test_equivalence_receipt_enforces_bucket_hash_and_probability_tolerance() -> None:
    rows = [_envelope()]
    conn = _connection()
    _materialize_all(conn, rows)
    history_id = conn.execute(
        "SELECT history_id FROM basket_probability_bridge_history"
    ).fetchone()[0]
    conn.execute("DROP TRIGGER basket_probability_bridge_bucket_no_update")

    def rewrite_probability(value: float) -> None:
        identity = {
            "schema": SCHEMA,
            "history_id": history_id,
            "bucket": "35",
            "ordinal": 0,
            "lower_c": None,
            "upper_c": 35.5,
            "probability": value,
        }
        conn.execute(
            "UPDATE basket_probability_bridge_bucket_probability "
            "SET probability=?,content_sha256=? "
            "WHERE history_id=? AND bucket='35'",
            (value, content_sha256(identity), history_id),
        )

    rewrite_probability(0.20 + 0.5e-9)
    assert verify_probability_bridge_replay_equivalence(
        conn,
        rows,
    )["max_absolute_probability_difference"] <= 1e-9

    rewrite_probability(0.20 + 2e-9)
    with pytest.raises(
        ContractError,
        match="xsb_equivalence_probability_mismatch",
    ):
        verify_probability_bridge_replay_equivalence(conn, rows)


def test_legacy_derived_envelope_backfills_from_immutable_p0_root() -> None:
    conn = _connection()
    p0 = _envelope()
    p1 = ProbabilityEnvelope.from_mapping({
        **_envelope(
            stage="P1",
            checkpoint_id="legacy-p1-with-root-contract",
            decision_at_utc="2026-07-23T05:00:00Z",
        ).to_dict(),
        "lineage": {
            "settlement_definition": "official_daily_tmax_c",
            "canonical_observation_event_id": "legacy-observation",
            "p0_root_checkpoint_id": p0.checkpoint_id,
        },
    })
    insert_probability_envelope(
        conn,
        p0,
        created_at_utc=p0.decision_at_utc,
    )
    insert_probability_envelope(
        conn,
        p1,
        created_at_utc=p1.decision_at_utc,
    )

    receipt = backfill_probability_bridges(conn)

    assert receipt["materialized_count"] == 2
    assert receipt["skipped_legacy_non_authoritative_count"] == 0
    current = load_current_probability_bridge(
        conn,
        station_id="RKPK",
        event_date="2026-07-23",
        as_of_utc="2026-07-23T05:30:00Z",
    )
    assert current["stage"] == "P1"
    assert current["checkpoint_id"] == p1.checkpoint_id


def test_unbridgeable_authoritative_legacy_envelope_has_no_latest() -> None:
    conn = _connection()
    p0 = ProbabilityEnvelope.from_mapping({
        **_envelope().to_dict(),
        "lineage": {
            "settlement_definition": "official_daily_tmax_c",
        },
    })
    insert_probability_envelope(
        conn,
        p0,
        created_at_utc=p0.decision_at_utc,
    )

    receipt = backfill_probability_bridges(conn)

    assert receipt["state"] == "BACKFILLED_WITH_LEGACY_GAPS"
    assert receipt["unbridgeable_authoritative_scope_count"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_bridge_history"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_bridge_latest"
    ).fetchone()[0] == 0


def test_a_p0_recompile_that_discovered_nothing_writes_nothing() -> None:
    """P0 appended a row whether or not the probabilities moved.

    Measured 2026-09-01: 578 committed P0 rows over five stations for
    2026-08-31 carrying 58 distinct probability vectors -- ZUUU alone 117 rows
    over 7, 16.7x.  At ~10.6 KB a row, 95% of it lineage_json, that was 188 MB
    a day and 64% of everything the store grew, with a regime artifact, a
    bridge history row and eleven bucket rows following each one.

    Suppressing it is safe because ``fresh_until`` belongs to the evidence,
    not to the write: all 26 rows of ZUUU's largest identical group carry the
    same ``fresh_until_utc`` across four hours of recompiles, so the last one
    is servable until exactly the instant the first one is.
    """

    conn = _connection()
    first = _envelope()
    materialize_probability_bridge(conn, first, created_at_utc=first.decision_at_utc)
    after_first = _counts(conn)

    restatement = _envelope(
        checkpoint_id="p0-restated",
        decision_at_utc="2026-07-23T04:30:00Z",
    )
    receipt = materialize_probability_bridge(
        conn, restatement, created_at_utc=restatement.decision_at_utc
    )
    assert receipt["history_wrote"] is False
    assert receipt["checkpoint"]["wrote"] is False
    assert receipt["checkpoint"]["envelope_id"] == first.checkpoint_id
    # Not one row in any of the four tables.
    assert _counts(conn) == after_first
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_envelope WHERE envelope_id=?",
        (restatement.checkpoint_id,),
    ).fetchone()[0] == 0

    # The newest servable P0 is still the first one, with its window intact --
    # which is the whole reason dropping the restatement costs nothing.
    current = load_current_probability_bridge(
        conn,
        station_id="RKPK",
        event_date="2026-07-23",
        as_of_utc="2026-07-23T05:00:00Z",
    )
    assert current["checkpoint_id"] == first.checkpoint_id
    assert current["envelope_content_sha256"] == first.content_sha256
    # And the window the store still serves is the first one's, unchanged.
    assert conn.execute(
        "SELECT fresh_until_utc FROM basket_probability_bridge_latest"
        " WHERE station_id='RKPK' AND event_date='2026-07-23'"
    ).fetchone()[0] == first.fresh_until_utc


def test_a_p0_whose_probabilities_moved_is_a_new_fact() -> None:
    """Anything that moved is appended.  Only an identical restatement is not."""

    conn = _connection()
    first = _envelope()
    materialize_probability_bridge(conn, first, created_at_utc=first.decision_at_utc)
    before = _counts(conn)

    moved = _envelope(
        checkpoint_id="p0-moved",
        decision_at_utc="2026-07-23T04:30:00Z",
        probabilities={"35": 0.10, "36": 0.60, "37": 0.30},
    )
    receipt = materialize_probability_bridge(
        conn, moved, created_at_utc=moved.decision_at_utc
    )
    assert receipt["history_wrote"] is True
    assert _counts(conn)[0] == before[0] + 1

    # A different evidence window is a different fact too, even at the same
    # probabilities: fresh_until is what makes the row servable.
    rewindowed = _envelope(
        checkpoint_id="p0-rewindowed",
        decision_at_utc="2026-07-23T04:45:00Z",
        fresh_until_utc="2026-07-23T18:00:00Z",
    )
    assert materialize_probability_bridge(
        conn, rewindowed, created_at_utc=rewindowed.decision_at_utc
    )["history_wrote"] is True


def test_only_p0_restatements_are_suppressed() -> None:
    """P1 is seated per observation and reaffirmation is re-clocked on purpose."""

    conn = _connection()
    p0 = _envelope()
    materialize_probability_bridge(conn, p0, created_at_utc=p0.decision_at_utc)
    p1 = _envelope(stage="P1", checkpoint_id="p1-a", decision_at_utc="2026-07-23T05:00:00Z")
    materialize_probability_bridge(conn, p1, created_at_utc=p1.decision_at_utc)
    before = _counts(conn)
    p1_again = _envelope(
        stage="P1", checkpoint_id="p1-b", decision_at_utc="2026-07-23T05:30:00Z"
    )
    assert materialize_probability_bridge(
        conn, p1_again, created_at_utc=p1_again.decision_at_utc
    )["history_wrote"] is True
    assert _counts(conn)[0] == before[0] + 1


def test_a_churning_posterior_digest_alone_is_not_a_discovery() -> None:
    """``posterior_sha256`` records how the answer was computed, not what it is.

    It is the one envelope field deliberately left out of the restatement
    predicate.  Measured 2026-08-31: including it matched 0 of 6,666 P0 rows,
    because it takes a fresh value on every recompile -- 26 distinct values
    across ZUUU's 26-row identical group -- while the probabilities, the
    bucket grid, the evidence window, the posterior version and the bridge
    digest are each constant across the same 26 rows.  Re-running the same
    arithmetic against archive queries stamped a minute apart is not a
    discovery, and treating it as one is what wrote 6,010 rows a day saying
    nothing new.
    """

    conn = _connection()
    first = _envelope()
    materialize_probability_bridge(conn, first, created_at_utc=first.decision_at_utc)
    before = _counts(conn)

    churned = ProbabilityEnvelope.from_mapping({
        **first.to_dict(),
        "checkpoint_id": "p0-churned-posterior",
        "decision_at_utc": "2026-07-23T04:30:00Z",
        # Same served distribution, same window, same bridge digest; only the
        # provenance digest of the computation moved.
        "posterior_sha256": "c" * 64,
    })
    assert churned.bridge_sha256 == first.bridge_sha256
    assert churned.posterior_sha256 != first.posterior_sha256
    receipt = materialize_probability_bridge(
        conn, churned, created_at_utc=churned.decision_at_utc
    )
    assert receipt["history_wrote"] is False
    assert _counts(conn) == before

    # But the served digest moving *is* a discovery.
    served_moved = ProbabilityEnvelope.from_mapping({
        **first.to_dict(),
        "checkpoint_id": "p0-served-moved",
        "decision_at_utc": "2026-07-23T04:45:00Z",
        "bridge_sha256": "d" * 64,
    })
    assert materialize_probability_bridge(
        conn, served_moved, created_at_utc=served_moved.decision_at_utc
    )["history_wrote"] is True


def test_large_lineage_bodies_are_stored_once_and_read_back_whole() -> None:
    """Dehydration is a storage encoding, not a change of identity.

    ``lineage_json`` is 95.5% of an envelope row and three keys are 65% of it,
    while carrying almost nothing per row.  Measured over 4,000 committed
    envelopes from 2026-08-31: the live-compatibility document repeats 49.7x,
    the forward availability evidence 60.5x and the conditional curve 4.5x --
    11.75 KB of duplication per envelope, about 208 MB a day at the fleet's
    17,711.

    What makes it safe is the order: the loader rehydrates *before* it builds
    the envelope and therefore before it verifies ``content_sha256``, so the
    sha is computed over the full lineage exactly as it always was.  Rows
    written before this existed carry no references and pass through
    untouched.
    """

    from bayes_basket_target.materialization import (  # noqa: PLC0415
        DEHYDRATED_LINEAGE_KEYS,
        LINEAGE_DOCUMENT_REF_PREFIX,
    )
    from bayes_basket_target.production_io import (  # noqa: PLC0415
        load_materialized_probability_envelopes,
    )

    conn = _connection()
    base = _envelope()
    lineage = dict(base.lineage)
    lineage["settlement_timezone"] = "Asia/Karachi"
    lineage["taf_nwp_live_compatibility_json"] = json.dumps(
        {"archive_query_at_utc_by_system": {"ECMWF": "2026-07-23T00:00:00Z"}},
        sort_keys=True,
    )
    lineage["native_forward_availability_evidence_json"] = json.dumps(
        {"available": True}, sort_keys=True
    )
    lineage["conditional_future_temperature_curve_json"] = json.dumps(
        {"curve": [1, 2, 3]}, sort_keys=True
    )
    envelope = ProbabilityEnvelope.from_mapping(
        {**base.to_dict(), "lineage": lineage}
    )
    sha_before = envelope.content_sha256
    insert_probability_envelope(
        conn, envelope, created_at_utc=envelope.decision_at_utc
    )

    stored_lineage, stored_sha = conn.execute(
        "SELECT lineage_json,content_sha256 FROM basket_probability_envelope"
        " WHERE envelope_id=?",
        (envelope.checkpoint_id,),
    ).fetchone()
    on_disk = json.loads(stored_lineage)
    for key in DEHYDRATED_LINEAGE_KEYS:
        assert str(on_disk[key]).startswith(LINEAGE_DOCUMENT_REF_PREFIX)
    # The identity is the *rehydrated* one, so nothing that references this
    # envelope has to be rebound.
    assert stored_sha == sha_before

    loaded = load_materialized_probability_envelopes(
        conn,
        station_id="RKPK",
        event_date="2026-07-23",
        evaluation_at_utc="2026-07-23T05:00:00Z",
        required_probability_pathway=None,
    )
    assert len(loaded) == 1
    assert loaded[0].content_sha256 == sha_before
    assert dict(loaded[0].lineage) == dict(lineage)

    # One row per distinct document, however many envelopes carry it.
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_lineage_document"
    ).fetchone()[0] == len(DEHYDRATED_LINEAGE_KEYS)
    twin = ProbabilityEnvelope.from_mapping({
        **envelope.to_dict(),
        "checkpoint_id": "p0-twin",
        "decision_at_utc": "2026-07-23T04:30:00Z",
        "bridge_sha256": "e" * 64,
    })
    insert_probability_envelope(conn, twin, created_at_utc=twin.decision_at_utc)
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_lineage_document"
    ).fetchone()[0] == len(DEHYDRATED_LINEAGE_KEYS)


def test_a_lineage_body_that_cannot_be_found_fails_closed() -> None:
    """The body is the evidence; a reference nobody can resolve is not servable.

    Handing a consumer the string ``@ref/...`` where it expected a document
    would be the silent version of this, and ``production_io`` reads that
    document to validate the model authority.
    """

    from bayes_basket_target.production_io import (  # noqa: PLC0415
        load_materialized_probability_envelopes,
    )

    conn = _connection()
    base = _envelope()
    lineage = dict(base.lineage)
    lineage["settlement_timezone"] = "Asia/Karachi"
    lineage["taf_nwp_live_compatibility_json"] = json.dumps({"a": 1})
    envelope = ProbabilityEnvelope.from_mapping(
        {**base.to_dict(), "lineage": lineage}
    )
    insert_probability_envelope(
        conn, envelope, created_at_utc=envelope.decision_at_utc
    )
    conn.execute("DELETE FROM basket_probability_lineage_document")
    with pytest.raises(
        ContractError, match="probability_lineage_document_missing"
    ):
        load_materialized_probability_envelopes(
            conn,
            station_id="RKPK",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:00Z",
            required_probability_pathway=None,
        )


def test_a_row_written_before_dehydration_reads_back_unchanged() -> None:
    """No migration: an old row carries no references and passes through."""

    from bayes_basket_target.production_io import (  # noqa: PLC0415
        load_materialized_probability_envelopes,
        rehydrate_lineage_documents,
    )

    conn = _connection()
    base = _envelope()
    lineage = dict(base.lineage)
    lineage["settlement_timezone"] = "Asia/Karachi"
    lineage["taf_nwp_live_compatibility_json"] = json.dumps({"a": 1})
    envelope = ProbabilityEnvelope.from_mapping(
        {**base.to_dict(), "lineage": lineage}
    )
    # Write the pre-dehydration shape by hand, exactly as the old code did.
    conn.execute(
        "INSERT INTO basket_probability_envelope(envelope_id,stage,checkpoint_id,"
        "station_id,event_date,settlement_timezone,decision_at_utc,fresh_until_utc,"
        "bucket_ordinals_json,probabilities_json,posterior_version,posterior_sha256,"
        "bridge_sha256,lineage_json,content_sha256,state,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            envelope.checkpoint_id, envelope.stage, envelope.checkpoint_id,
            envelope.station_id, envelope.event_date, envelope.settlement_timezone,
            envelope.decision_at_utc, envelope.fresh_until_utc,
            json.dumps(dict(envelope.bucket_ordinals), ensure_ascii=False, sort_keys=True),
            json.dumps(dict(envelope.probabilities), ensure_ascii=False, sort_keys=True),
            envelope.posterior_version, envelope.posterior_sha256,
            envelope.bridge_sha256,
            json.dumps(dict(envelope.lineage), ensure_ascii=False, sort_keys=True),
            envelope.content_sha256, "COMMITTED", envelope.decision_at_utc,
        ),
    )
    assert rehydrate_lineage_documents(conn, lineage) == lineage
    loaded = load_materialized_probability_envelopes(
        conn,
        station_id="RKPK",
        event_date="2026-07-23",
        evaluation_at_utc="2026-07-23T05:00:00Z",
        required_probability_pathway=None,
    )
    assert dict(loaded[0].lineage) == dict(lineage)


def test_the_history_body_rebuild_rehydrates_before_it_hashes() -> None:
    """The bridge rebuilds envelope bodies without going through the loader.

    ``basket_probability_bridge_history.envelope_json`` has been empty since
    2026-08-26 -- the body lives in ``basket_probability_envelope`` and is
    rebuilt from it, then hashed and checked against the sha the history row
    has always carried.  That reader does not use
    ``load_materialized_probability_envelopes``, so it has to do the same
    rehydration the loader does; without it the rebuilt body hashes to
    something else and every consumer of the bridge fails closed.  Ten
    production_io tests caught exactly that.
    """

    from bayes_basket_target.probability_bridge import (  # noqa: PLC0415
        history_envelope_payload,
    )

    conn = _connection()
    base = _envelope()
    lineage = dict(base.lineage)
    lineage["settlement_timezone"] = "Asia/Karachi"
    lineage["taf_nwp_live_compatibility_json"] = json.dumps(
        {"archive_query_at_utc_by_system": {"ECMWF": "2026-07-23T00:00:00Z"}},
        sort_keys=True,
    )
    envelope = ProbabilityEnvelope.from_mapping(
        {**base.to_dict(), "lineage": lineage}
    )
    materialize_probability_bridge(
        conn, envelope, created_at_utc=envelope.decision_at_utc
    )
    stored_json, stored_sha = conn.execute(
        "SELECT envelope_json,envelope_content_sha256"
        "  FROM basket_probability_bridge_history WHERE checkpoint_id=?",
        (envelope.checkpoint_id,),
    ).fetchone()
    assert not stored_json, "the history row must not carry the body inline"
    payload = history_envelope_payload(
        conn,
        checkpoint_id=envelope.checkpoint_id,
        envelope_content_sha256=stored_sha,
    )
    assert payload["lineage"] == dict(lineage)
    assert stored_sha == envelope.content_sha256
