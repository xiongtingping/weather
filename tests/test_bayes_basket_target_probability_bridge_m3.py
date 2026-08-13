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
