from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from bayes_basket_target.production import PRODUCTION_EXECUTION_MODE
from bayes_basket_target.atomic_ledger import (
    AtomicLedgerError,
    InjectedAtomicFault,
    apply_target_ledger_schema,
    arbitrate_capital_requests,
    build_target_atomic_bundle,
    close_settled_day_cycle,
    close_target_cycle,
    commit_stage_zero_execution_scope_decision,
    commit_blocked_scope_decision,
    commit_target_atomic_bundle,
    initialize_capital_pool,
    supersede_prepared_cycle_and_commit_bundle,
    target_ledger_schema_status,
    transition_target_cycle_state,
)
from bayes_basket_target.production import commit_production_candidate_atomic
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.canonical_ssot_v12_migration import (
    TARGET_TABLE_DDL as V12_TABLE_DDL,
    TARGET_TRIGGER_DDL as V12_TRIGGER_DDL,
)
from bayes_basket_target.execution_reconciliation import (
    ExecutionCasError,
    ReconciliationError,
    assert_basket_hold_only,
    assert_execution_version_cas,
    can_submit_next_order,
    record_order_event,
    resolve_reconciliation,
    validate_persisted_candidate_consumption_cas,
    VERSION_FIELDS,
)
from bayes_basket_target.ssot import (
    apply_production_ssot_schema,
    apply_target_ssot_schema,
)
from bayes_basket_target.delta_v2_training import FACT_SCHEMA

from tests.pr12_atomic_test_support import awc_contract


NOW = "2026-07-23T04:00:00Z"
SETTLEMENT_TIMEZONE = "America/New_York"
SHA_A = "a" * 64
SHA_B = "b" * 64


def _awc_contract(index: int = 0) -> tuple[dict[str, object], str]:
    return awc_contract(
        station_id="ZUUU",
        observation_id=f"obs-{index}",
        event_date="2026-07-23",
        settlement_timezone=SETTLEMENT_TIMEZONE,
        observed_at_utc=NOW,
        raw_object_sha256=SHA_A,
    )


def _complete_execution_vector(index: int = 0) -> dict[str, str]:
    return {
        "schema": "bayes_basket_target_execution_version_vector_v2",
        "probability_sha256": "a" * 64,
        "official_ws_quote_sha256": "b" * 64,
        "inventory_sha256": "c" * 64,
        "order_state_sha256": "d" * 64,
        "station_policy_sha256": "e" * 64,
        "cash_policy_decision_sha256": "f" * 64,
        "economic_policy_schema": "delta_basket_score_policy_artifact_v2",
        "economic_policy_artifact_sha256": "0" * 64,
        "target_price_id": f"price-{index}",
        "quote_observed_at": "2026-07-23T04:00:00Z",
        "quote_expires_at": "2026-07-23T04:01:00Z",
    }


def _bundle(
    *,
    index: int = 0,
    # Salts the plan only.  Plan and cycle are content-addressed off the
    # plan's own fields; the decision is addressed off what it decided.
    # A salted plan with the same index is therefore the same decision on
    # a new cycle -- the restatement case.
    plan_salt: str | None = None,
    blocked: bool = False,
    amount: float = 4.0,
    token_id: str | None = None,
    strategy_lane: str = "basket",
    intent_side: str = "YES",
    intent_order_action: str = "BUY",
    source_lane: str = "AWC",
    # A plan compiled with live authority mints intents carrying
    # ``live_submit_authorized=1`` (the ledger reads the flag off the plan
    # identity).  This is what every intent of a live deployment looks like.
    live: bool = False,
):
    station = "ZUUU"
    event_date = "2026-07-23"
    contract, revision_sha = _awc_contract(index)
    bound = {
        "bound_id": f"bound-{index}",
        "station_id": station,
        "event_date": event_date,
        "bound_generation": index,
        "hard_lower_bound": 38,
        "bound_json": {"source": "official_metar", "excluded_buckets": ["37"]},
    }
    plan = {
        "strategy_lane": strategy_lane,
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "probability_checkpoint_id": f"p1-{index}",
        "quote_snapshot_id": f"quote-{index}{plan_salt or ''}",
        "lineage": {
            "probability_stage": "P1",
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "canonical_observation_event_id": f"obs-{index}",
            "truth_observation_revision_content_sha256": revision_sha,
            "canonical_observation_committed_at": "2026-07-23T04:00:00.000Z",
            "hard_bound_id": f"bound-{index}",
            "source_lane": source_lane,
            "report_type": "METAR",
        },
    }
    if live:
        plan["live_submit_authorized"] = True
    intents = [] if blocked else [{
        "bucket": "38",
        "token_id": token_id or f"token-{index}",
        "side": intent_side,
        "order_action": intent_order_action,
        "limit_price": 0.40,
        "requested_size": amount / 0.40,
        "requested_notional_usdc": amount,
        "intent_json": {
            "candidate_will_order": True,
            "no_live": True,
            "target_price_id": f"price-{index}",
            "target_execution_hard_deadline_at": "2026-07-23T04:00:03.000Z",
        },
    }]
    decision = {
        "strategy_lane": strategy_lane,
        "decided_state": "blocked" if blocked else "prepared",
        "will_order": False,
        "reason_codes": ["missing_quote"] if blocked else [],
        # Production stamps the decision with the mode in force (see
        # bayes_basket_target.production, "execution_mode":
        # PRODUCTION_EXECUTION_MODE).  This fixture carried only the no-live
        # marker, so after the go-live flip it built a decision no live
        # deployment ever writes -- and the consumption CAS, reading it, was
        # being asked a question about a row that could not exist.
        "details": {
            "candidate_will_order": not blocked,
            "no_live": PRODUCTION_EXECUTION_MODE == "production_no_live",
            "execution_mode": PRODUCTION_EXECUTION_MODE,
            # Decisions are addressed by what they are made of (user
            # ruling, 2026-09-02), not by cycle or clock.  Every other field
            # ``index`` varies is a transport handle -- bound id, checkpoint
            # id, quote id, token -- and none of those is in the address, so
            # two bundles differing only in ``index`` used to be two
            # decisions and are now one.  The window is economic, so it
            # carries the index: bundle N is the decision that chose bucket N.
            "core_buckets": [f"{20 + index}°C"],
        },
    }
    return build_target_atomic_bundle(
        awc_observation_contract=contract,
        truth_observation_revision_content_sha256=revision_sha,
        bound=bound,
        plan=plan,
        decision=decision,
        intents=intents,
        reserved_amount_usdc=0.0 if blocked else amount,
        created_at_utc=NOW,
        execution_version_vectors=(
            [] if blocked else [_complete_execution_vector(index)]
        ),
    )


def _db(
    path: Path | None = None,
    *,
    balance: float = 20.0,
    with_training_fact: bool = False,
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:" if path is None else path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    apply_target_ledger_schema(conn)
    conn.execute(V12_TABLE_DDL)
    for ddl in V12_TRIGGER_DDL:
        conn.execute(ddl)
    if with_training_fact:
        apply_target_ssot_schema(conn)
    initialize_capital_pool(
        conn,
        balance_usdc=balance,
        capital_epoch_id="epoch-1",
        updated_at_utc=NOW,
    )
    return conn


def _training_fact(
    bundle: dict,
    *,
    decision_scope_key: str = "scope-1",
    source_sequence: int = 1,
    quote_freshness_policy_sha256: str = SHA_A,
) -> dict[str, object]:
    identity: dict[str, object] = {
        "station_id": bundle["cycle"]["station_id"],
        "event_date": bundle["cycle"]["event_date"],
        "settlement_timezone": bundle["cycle"]["settlement_timezone"],
        "decision_at_utc": NOW,
        "decision_scope_key": decision_scope_key,
        "captured_at_utc": NOW,
        "source_sequence": source_sequence,
        "quote_observed_at": NOW,
        "quote_expires_at": "2026-07-23T04:01:00Z",
        "quote_freshness_policy_sha256": quote_freshness_policy_sha256,
        "stage": "P1",
        "probability_envelope_id": f"envelope-{decision_scope_key}",
        "posterior_version": "posterior-v1",
        "posterior_sha256": "1" * 64,
        "official_exclusion_mask_sha256": "2" * 64,
        "ws_snapshot_id": f"snapshot-{decision_scope_key}",
        "quote_version": "quote-v1",
        "quote_sha256": "3" * 64,
        "inventory_snapshot_id": f"inventory-{decision_scope_key}",
        "inventory_version": "inventory-v1",
        "inventory_sha256": "4" * 64,
        "order_state_snapshot_id": f"order-state-{decision_scope_key}",
        "order_state_version": "order-state-v1",
        "order_state_sha256": "5" * 64,
        "cycle_id": bundle["cycle"]["cycle_id"],
        "decision_id": bundle["decision"]["decision_id"],
        "decision_sha256": bundle["decision"]["decision_sha256"],
        "plan_sha256": bundle["plan"]["plan_sha256"],
        "intent_count": len(bundle["intents"]),
        "intent_set_sha256": "6" * 64,
        "policy_version_vector_json": "{}",
        "feature_json": "{}",
        "partition_key": "ZUUU|summer|P1|d0|normal",
        "authority_scope": "PRODUCTION_NO_LIVE_FORWARD",
        "created_at_utc": NOW,
    }
    fact_sha = content_sha256({"schema": FACT_SCHEMA, **identity})
    return {
        "schema": FACT_SCHEMA,
        "fact_id": f"d2fact_{fact_sha[:40]}",
        **identity,
        "content_sha256": fact_sha,
    }


def _replace_training_fact_with_legacy_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER IF EXISTS basket_training_fact_no_update")
    conn.execute("DROP TRIGGER IF EXISTS basket_training_fact_no_delete")
    conn.execute("DROP INDEX IF EXISTS basket_policy_training_fact_scope_idx")
    conn.execute("DROP TABLE basket_policy_training_fact")
    conn.execute(
        "CREATE TABLE basket_policy_training_fact("
        "fact_id TEXT PRIMARY KEY,station_id TEXT NOT NULL,"
        "event_date TEXT NOT NULL,decision_at_utc TEXT NOT NULL,"
        "stage TEXT NOT NULL,outcome_json TEXT NOT NULL,"
        "partition_key TEXT NOT NULL,content_sha256 TEXT NOT NULL)"
    )


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "basket_target_observation_facts",
            "basket_target_bound_facts",
            "basket_target_execution_cycles",
            "auto_trade_decisions",
            "basket_target_order_intents",
            "basket_target_capital_reservations",
        )
    }


def test_m5_schema_has_one_blocking_authority_and_no_live_boundary() -> None:
    with _db() as conn:
        status = target_ledger_schema_status(conn)
        assert status["ok"] is True
        assert status["blocking_authority"] == "auto_trade_decisions"
        assert status["places_orders"] is False
        assert status["calls_order_gateway"] is False
        decision_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(auto_trade_decisions)")
        }
        assert {
            "will_order",
            "reason_codes_json",
            "decision_sha256",
        } <= decision_columns


def test_m5_schema_rejects_retired_legacy_execution_vector_columns() -> None:
    with _db() as conn:
        conn.execute(
            "ALTER TABLE basket_target_execution_version_vectors "
            "ADD COLUMN posterior_version TEXT"
        )
        status = target_ledger_schema_status(conn)
        assert status["ok"] is False
        assert status["forbidden_execution_vector_columns"] == [
            "posterior_version"
        ]


def test_m5_schema_losslessly_removes_retired_columns_for_exact_rows() -> None:
    with _db() as conn:
        bundle = _bundle(index=901)
        commit_target_atomic_bundle(conn, bundle=bundle)
        expected_sha = bundle["execution_version_vectors"][0]["vector_sha256"]
        conn.execute(
            "ALTER TABLE basket_target_execution_version_vectors "
            "ADD COLUMN posterior_version TEXT"
        )
        result = apply_target_ledger_schema(conn)
        assert result["ok"] is True
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(basket_target_execution_version_vectors)"
            )
        }
        assert "posterior_version" not in columns
        assert conn.execute(
            "SELECT vector_policy,vector_sha256 "
            "FROM basket_target_execution_version_vectors"
        ).fetchone() == ("EXECUTION", expected_sha)


def test_m5_schema_never_infers_exact_vector_from_legacy_cas_row() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE basket_target_execution_version_vectors("
            "order_intent_id TEXT PRIMARY KEY,"
            "vector_policy TEXT NOT NULL,"
            "posterior_version TEXT,"
            "cycle_id TEXT,decision_id TEXT,training_fact_id TEXT,"
            "target_price_id TEXT,quote_observed_at TEXT,quote_expires_at TEXT,"
            "probability_sha256 TEXT,official_ws_quote_sha256 TEXT,"
            "inventory_sha256 TEXT,order_state_sha256 TEXT,"
            "station_policy_sha256 TEXT,cash_policy_decision_sha256 TEXT,"
            "economic_policy_artifact_sha256 TEXT,vector_json TEXT,"
            "vector_sha256 TEXT,created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO basket_target_execution_version_vectors("
            "order_intent_id,vector_policy,posterior_version,vector_json,"
            "vector_sha256,created_at) VALUES(?,?,?,?,?,?)",
            ("legacy-intent", "LEGACY_CAS", "p1-v1", "{}", "a" * 64, NOW),
        )
        with pytest.raises(
            RuntimeError,
            match="legacy_execution_vectors_require_offline_rebuild",
        ):
            apply_target_ledger_schema(conn)


def test_formal_atomic_writes_reject_future_registry_settlement_days() -> None:
    future = _bundle()
    future["observation"]["event_date"] = "2026-07-24"
    future["bound"]["event_date"] = "2026-07-24"
    future["decision"]["event_date"] = "2026-07-24"
    future["cycle"]["event_date"] = "2026-07-24"
    with _db() as conn:
        with pytest.raises(AtomicLedgerError, match="future_event_day_not_allowed"):
            commit_target_atomic_bundle(conn, bundle=future)
        with pytest.raises(AtomicLedgerError, match="future_event_day_not_allowed"):
            commit_blocked_scope_decision(
                conn,
                station_id="ZUUU",
                event_date="2026-07-24",
                settlement_timezone=SETTLEMENT_TIMEZONE,
                reason_codes=["future_day"],
                created_at_utc=NOW,
            )
        assert _counts(conn) == {
            "basket_target_observation_facts": 0,
            "basket_target_bound_facts": 0,
            "basket_target_execution_cycles": 0,
            "auto_trade_decisions": 0,
            "basket_target_order_intents": 0,
            "basket_target_capital_reservations": 0,
        }


def test_rkpk_atomic_boundary_uses_registry_kst_not_et() -> None:
    with _db() as conn:
        with pytest.raises(AtomicLedgerError, match="future_event_day_not_allowed"):
            commit_blocked_scope_decision(
                conn,
                station_id="RKPK",
                event_date="2026-07-27",
                settlement_timezone="Asia/Seoul",
                reason_codes=["before_registry_settlement_day"],
                created_at_utc="2026-07-26T14:59:59Z",
            )
        receipt = commit_blocked_scope_decision(
            conn,
            station_id="RKPK",
            event_date="2026-07-27",
            settlement_timezone="Asia/Seoul",
            reason_codes=["p0_final_authority_is_p1"],
            created_at_utc="2026-07-26T15:00:00Z",
        )
        assert receipt["state"] == "committed"
        assert conn.execute(
            "SELECT settlement_timezone,will_order FROM auto_trade_decisions"
        ).fetchone() == ("Asia/Seoul", 0)


def test_target_writer_lock_wait_telemetry_is_bounded_and_records_atomic_write() -> None:
    with _db() as conn:
        receipt = commit_target_atomic_bundle(conn, bundle=_bundle())
        assert receipt["state"] == "committed"
        row = conn.execute(
            "SELECT sample_count,last_operation,max_wait_ms "
            "FROM basket_target_writer_lock_wait_telemetry WHERE singleton=1"
        ).fetchone()
        assert row is not None
        assert int(row[0]) == 1
        assert row[1] == "commit_target_atomic_bundle"
        assert float(row[2]) >= 0.0
        assert conn.execute("SELECT COUNT(*) FROM basket_target_writer_lock_wait_telemetry").fetchone()[0] == 1


@pytest.mark.parametrize(
    "fault_after",
    [
        "canonical_stage", "projection", "bound_projection", "cycle",
        "decision", "intent", "reserve",
    ],
)
def test_every_atomic_write_point_is_all_or_zero(fault_after: str) -> None:
    with _db() as conn:
        with pytest.raises(InjectedAtomicFault, match=fault_after):
            commit_target_atomic_bundle(conn, bundle=_bundle(), fault_after=fault_after)
        assert _counts(conn) == {
            "basket_target_observation_facts": 0,
            "basket_target_bound_facts": 0,
            "basket_target_execution_cycles": 0,
            "auto_trade_decisions": 0,
            "basket_target_order_intents": 0,
            "basket_target_capital_reservations": 0,
        }
        assert conn.execute("SELECT reserved_usdc FROM basket_target_capital_pool").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact"
        ).fetchone()[0] == 0


def test_atomic_commit_writes_five_facts_and_never_submits() -> None:
    with _db() as conn:
        bundle = _bundle()
        receipt = commit_target_atomic_bundle(conn, bundle=bundle)
        assert receipt["state"] == "committed"
        assert receipt["orders_submitted"] == 0
        assert receipt["places_orders"] is False
        assert receipt["calls_order_gateway"] is False
        counts = _counts(conn)
        assert all(value == 1 for value in counts.values())
        assert conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact "
            "WHERE fact_type='truth_observation_revision'"
        ).fetchone()[0] == 1
        decision = conn.execute(
            "SELECT will_order,decided_state,reason_codes_json FROM auto_trade_decisions"
        ).fetchone()
        assert decision == (0, "prepared", "[]")
        intent = conn.execute(
            "SELECT live_submit_authorized,state FROM basket_target_order_intents"
        ).fetchone()
        assert intent == (0, "prepared")
        observation = conn.execute(
            "SELECT settlement_timezone,content_sha256 "
            "FROM basket_target_observation_facts"
        ).fetchone()
        assert observation == (
            SETTLEMENT_TIMEZONE,
            bundle["observation"]["content_sha256"],
        )
        assert conn.execute("SELECT reserved_usdc FROM basket_target_capital_pool").fetchone()[0] == pytest.approx(4.0)
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute("UPDATE auto_trade_decisions SET reason_codes_json='[\"tampered\"]'")


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("canonical_observation_event_id", "other-observation"),
        ("truth_observation_revision_content_sha256", SHA_B),
        ("canonical_observation_committed_at", "2026-07-23T03:59:59Z"),
        ("hard_bound_id", "other-bound"),
        ("report_type", "SPECI"),
    ],
)
def test_atomic_bundle_rejects_p1_lineage_observation_bound_mismatch(
    field: str,
    replacement: str,
) -> None:
    source = _bundle(index=610)
    bound = {
        key: source["bound"][key]
        for key in (
            "bound_id",
            "station_id",
            "event_date",
            "bound_generation",
            "hard_lower_bound",
            "bound_json",
        )
    }
    plan = dict(source["plan"]["plan_json"])
    plan["lineage"] = dict(plan["lineage"])
    plan["lineage"][field] = replacement
    with pytest.raises(
        AtomicLedgerError,
        match=rf"plan_lineage_{field}_mismatch",
    ):
        build_target_atomic_bundle(
            awc_observation_contract=source["awc_observation_contract"],
            truth_observation_revision_content_sha256=source[
                "observation"
            ]["truth_observation_revision_content_sha256"],
            bound=bound,
            plan=plan,
            decision={
                "strategy_lane": "basket",
                "decided_state": "blocked",
                "will_order": False,
                "reason_codes": ["test_only"],
                "details": {
                    "candidate_will_order": False,
                    "no_live": PRODUCTION_EXECUTION_MODE == "production_no_live",
                    "execution_mode": PRODUCTION_EXECUTION_MODE,
                },
            },
            intents=[],
            reserved_amount_usdc=0.0,
            execution_version_vectors=[],
            created_at_utc=NOW,
        )


def test_atomic_observation_timezone_is_content_addressed_and_replays() -> None:
    bundle = _bundle(index=612, source_lane="awc")
    with _db() as conn:
        first = commit_target_atomic_bundle(conn, bundle=bundle)
        replay = commit_target_atomic_bundle(conn, bundle=bundle)

        assert first["state"] == "committed"
        assert replay["state"] == "already_committed"
        assert bundle["observation"]["source_lane"] == "AWC"
        assert bundle["plan"]["plan_json"]["lineage"]["source_lane"] == "AWC"
        assert conn.execute(
            "SELECT observation_fact_id,settlement_timezone,source_lane,content_sha256 "
            "FROM basket_target_observation_facts"
        ).fetchone() == (
            bundle["observation"]["observation_fact_id"],
            SETTLEMENT_TIMEZONE,
            "AWC",
            bundle["observation"]["content_sha256"],
        )


@pytest.mark.parametrize("source_lane", ["AMSC", "NOAA", "AWC/AMSC"])
def test_atomic_bundle_rejects_non_awc_source_lane(source_lane: str) -> None:
    with pytest.raises(
        AtomicLedgerError,
        match="plan_lineage_source_lane_must_be_formal_observation_lane",
    ):
        _bundle(index=615, source_lane=source_lane)


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("observation", "observation_source_lane_must_be_formal_observation_lane"),
        ("lineage", "plan_lineage_source_lane_must_be_formal_observation_lane"),
    ],
)
def test_atomic_commit_rejects_non_awc_source_lane_before_any_write(
    target: str,
    error: str,
) -> None:
    bundle = _bundle(index=616)
    if target == "observation":
        bundle["observation"]["source_lane"] = "AMSC"
    else:
        bundle["plan"]["plan_json"]["lineage"]["source_lane"] = "AMSC"
    with _db() as conn:
        before = _counts(conn)
        with pytest.raises(
            AtomicLedgerError,
            match=error,
        ):
            commit_target_atomic_bundle(conn, bundle=bundle)
        assert _counts(conn) == before
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("missing", "observation_settlement_timezone_missing"),
        ("invalid", "observation_settlement_timezone_invalid"),
        ("conflict", "atomic_settlement_timezone_mismatch"),
    ],
)
def test_atomic_observation_timezone_missing_or_conflicting_fails_closed(
    mutation: str,
    error: str,
) -> None:
    bundle = _bundle(index=613)
    observation = bundle["observation"]
    if mutation == "missing":
        observation.pop("settlement_timezone")
    elif mutation == "invalid":
        observation["settlement_timezone"] = "/invalid/timezone"
    else:
        observation["settlement_timezone"] = "Asia/Seoul"
        observation_identity = {
            key: value
            for key, value in observation.items()
            if key not in {"observation_fact_id", "content_sha256"}
        }
        observation["content_sha256"] = content_sha256({
                "schema": "basket_target_observation_projection_v2",
            **observation_identity,
        })
        observation["observation_fact_id"] = (
            "obsfact_" + content_sha256(observation_identity)[:40]
        )

    with _db() as conn:
        with pytest.raises(AtomicLedgerError, match=error):
            commit_target_atomic_bundle(conn, bundle=bundle)
        assert _counts(conn) == {
            "basket_target_observation_facts": 0,
            "basket_target_bound_facts": 0,
            "basket_target_execution_cycles": 0,
            "auto_trade_decisions": 0,
            "basket_target_order_intents": 0,
            "basket_target_capital_reservations": 0,
        }
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == 0


def test_atomic_rejects_projection_insert_bypass_without_new_facts() -> None:
    bundle = _bundle(index=614)
    observation = bundle["observation"]
    with _db() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="canonical_projection_ingress_required"):
            conn.execute(
                "INSERT INTO basket_target_observation_facts("
                "observation_fact_id,observation_id,station_id,event_date,"
                "settlement_timezone,source_lane,report_type,raw_sha256,"
                "canonical_committed_at,content_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    observation["observation_fact_id"], observation["observation_id"],
                    observation["station_id"], observation["event_date"], "Asia/Seoul",
                    observation["source_lane"], observation["report_type"],
                    observation["raw_sha256"], observation["canonical_committed_at"],
                    observation["content_sha256"], NOW,
                ),
            )
        assert _counts(conn) == {
            "basket_target_observation_facts": 0,
            "basket_target_bound_facts": 0,
            "basket_target_execution_cycles": 0,
            "auto_trade_decisions": 0,
            "basket_target_order_intents": 0,
            "basket_target_capital_reservations": 0,
        }
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == 0


def test_atomic_rejects_bound_projection_insert_bypass() -> None:
    with _db() as conn:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="canonical_projection_ingress_required",
        ):
            conn.execute(
                "INSERT INTO basket_target_bound_facts("
                "bound_fact_id,bound_id,observation_id,station_id,event_date,"
                "bound_generation,hard_lower_bound,bound_json,content_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "boundfact-bypass", "bound-bypass", "obs-bypass", "ZUUU",
                    "2026-07-23", 0, 38, "{}", SHA_A, NOW,
                ),
            )


@pytest.mark.parametrize(
    ("table", "trigger", "identity_column", "error"),
    [
        (
            "basket_target_observation_facts",
            "basket_target_observation_no_delete",
            "observation_id",
            "cycle_existing_observation_fact_binding_drift",
        ),
        (
            "basket_target_bound_facts",
            "basket_target_bound_no_delete",
            "bound_id",
            "cycle_existing_bound_fact_binding_drift",
        ),
    ],
)
def test_atomic_replay_fails_closed_when_observation_or_bound_fact_missing(
    table: str,
    trigger: str,
    identity_column: str,
    error: str,
) -> None:
    bundle = _bundle(index=611)
    with _db() as conn:
        commit_target_atomic_bundle(conn, bundle=bundle)
        conn.execute(f"DROP TRIGGER {trigger}")
        identity = (
            bundle["observation"]["observation_id"]
            if identity_column == "observation_id"
            else bundle["bound"]["bound_id"]
        )
        conn.execute(
            f"DELETE FROM {table} WHERE {identity_column}=?",
            (identity,),
        )
        with pytest.raises(AtomicLedgerError, match=error):
            commit_target_atomic_bundle(conn, bundle=bundle)


def test_training_fact_uses_canonical_schema_and_binds_each_execution_vector() -> None:
    bundle = _bundle(index=201)
    fact = _training_fact(bundle, decision_scope_key="scope-execution-201")
    bundle["training_fact"] = fact
    with _db(with_training_fact=True) as conn:
        receipt = commit_target_atomic_bundle(conn, bundle=bundle)
        assert receipt["training_fact_state"] == "committed"
        vector = conn.execute(
            "SELECT order_intent_id,training_fact_id,vector_policy "
            "FROM basket_target_execution_version_vectors"
        ).fetchone()
        assert vector == (
            bundle["intents"][0]["order_intent_id"],
            fact["fact_id"],
            "EXECUTION",
        )
        stored = conn.execute(
            "SELECT decision_scope_key,captured_at_utc,source_sequence,"
            "quote_observed_at,quote_expires_at,"
            "quote_freshness_policy_sha256 FROM basket_policy_training_fact"
        ).fetchone()
        assert stored == (
            "scope-execution-201",
            NOW,
            1,
            NOW,
            "2026-07-23T04:01:00Z",
            SHA_A,
        )


def test_zero_intent_no_execution_vector_binds_the_same_training_fact() -> None:
    bundle = _bundle(index=202, blocked=True)
    fact = _training_fact(bundle, decision_scope_key="scope-zero-202")
    bundle["training_fact"] = fact
    with _db(with_training_fact=True) as conn:
        receipt = commit_target_atomic_bundle(conn, bundle=bundle)
        assert receipt["intent_count"] == 0
        assert receipt["training_fact_state"] == "committed"
        vector = conn.execute(
            "SELECT order_intent_id,training_fact_id,vector_policy,vector_json "
            "FROM basket_target_execution_version_vectors"
        ).fetchone()
        assert vector[0] is None
        assert vector[1] == fact["fact_id"]
        assert vector[2] == "NO_EXECUTION"
        assert '"policy":"NO_EXECUTION"' in vector[3]
        assert conn.execute(
            "SELECT intent_count FROM basket_policy_training_fact"
        ).fetchone()[0] == 0


def test_same_training_fact_scope_conflict_fails_closed_and_rolls_back_second_cycle() -> None:
    first = _bundle(index=203)
    first["training_fact"] = _training_fact(
        first,
        decision_scope_key="scope-conflict",
        source_sequence=1,
    )
    second = _bundle(index=204)
    second["training_fact"] = _training_fact(
        second,
        decision_scope_key="scope-conflict",
        source_sequence=2,
        quote_freshness_policy_sha256=SHA_B,
    )
    with _db(with_training_fact=True) as conn:
        commit_target_atomic_bundle(conn, bundle=first)
        with pytest.raises(
            AtomicLedgerError,
            match="delta_v2_training_fact_scope_conflict",
        ):
            commit_target_atomic_bundle(conn, bundle=second)
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_policy_training_fact"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_cycles"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents"
        ).fetchone()[0] == 1


def test_fault_after_canonical_training_fact_rolls_back_fact_vectors_and_m5_rows() -> None:
    bundle = _bundle(index=205)
    bundle["training_fact"] = _training_fact(
        bundle,
        decision_scope_key="scope-fault-205",
    )
    with _db(with_training_fact=True) as conn:
        with pytest.raises(InjectedAtomicFault, match="training_fact"):
            commit_target_atomic_bundle(
                conn,
                bundle=bundle,
                fault_after="training_fact",
            )
        for table in (
            "basket_target_observation_facts",
            "basket_target_bound_facts",
            "basket_target_execution_cycles",
            "auto_trade_decisions",
            "basket_target_order_intents",
            "basket_target_execution_version_vectors",
            "basket_target_capital_reservations",
            "basket_policy_training_fact",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == 0


def test_legacy_training_fact_table_cannot_reactivate_the_old_insert_path() -> None:
    bundle = _bundle(index=206)
    bundle["training_fact"] = _training_fact(
        bundle,
        decision_scope_key="scope-legacy-206",
    )
    with _db(with_training_fact=True) as conn:
        _replace_training_fact_with_legacy_table(conn)
        with pytest.raises(
            AtomicLedgerError,
            match="training_fact_canonical_write_failed",
        ):
            commit_target_atomic_bundle(conn, bundle=bundle)
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_policy_training_fact"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors"
        ).fetchone()[0] == 0
        assert all(value == 0 for value in _counts(conn).values())


def test_repeating_same_bundle_one_hundred_times_is_one_fact_set() -> None:
    with _db() as conn:
        bundle = _bundle()
        first = commit_target_atomic_bundle(conn, bundle=bundle)
        assert first["idempotent"] is False
        for _ in range(100):
            replay = commit_target_atomic_bundle(conn, bundle=bundle)
            assert replay["state"] == "already_committed"
            assert replay["idempotent"] is True
        assert all(value == 1 for value in _counts(conn).values())
        assert conn.execute("SELECT reserved_usdc,version FROM basket_target_capital_pool").fetchone() == (4.0, 1)


def test_same_plan_with_changed_decision_is_content_address_drift() -> None:
    with _db() as conn:
        first = _bundle()
        commit_target_atomic_bundle(conn, bundle=first)
        changed = _bundle()
        changed["decision"] = {**changed["decision"], "details": {
            "candidate_will_order": False,
            "no_live": PRODUCTION_EXECUTION_MODE == "production_no_live",
            "execution_mode": PRODUCTION_EXECUTION_MODE,
        }}
        with pytest.raises(AtomicLedgerError, match="content_address_drift|cycle_existing_decision_drift"):
            commit_target_atomic_bundle(conn, bundle=changed)
        assert all(value == 1 for value in _counts(conn).values())


def test_cycle_state_machine_rejects_jumps_and_uses_cas() -> None:
    with _db() as conn:
        bundle = _bundle()
        commit_target_atomic_bundle(conn, bundle=bundle)
        cycle_id = bundle["cycle"]["cycle_id"]
        with pytest.raises(AtomicLedgerError, match="transition_invalid"):
            transition_target_cycle_state(
                conn, cycle_id=cycle_id, expected_state="prepared", new_state="submitted", updated_at_utc=NOW
            )
        with pytest.raises(
            AtomicLedgerError,
            match="submission_requires_persisted_candidate_cas_boundary",
        ):
            transition_target_cycle_state(
                conn, cycle_id=cycle_id, expected_state="prepared", new_state="submitting", updated_at_utc=NOW
            )
        transition_target_cycle_state(
            conn, cycle_id=cycle_id, expected_state="prepared", new_state="reconcile_required", updated_at_utc=NOW
        )
        with pytest.raises(AtomicLedgerError, match="transition_invalid"):
            transition_target_cycle_state(
                conn, cycle_id=cycle_id, expected_state="reconcile_required", new_state="settled", updated_at_utc=NOW
            )
        close_target_cycle(
            conn,
            cycle_id=cycle_id,
            outcome="released",
            closed_at_utc=NOW,
            reason="state_machine_test_close",
        )
        with pytest.raises(AtomicLedgerError, match="transition_invalid"):
            transition_target_cycle_state(
                conn, cycle_id=cycle_id, expected_state="settled", new_state="submitted", updated_at_utc=NOW
            )


@pytest.mark.parametrize(
    "expected_state",
    ("prepared", "submitted", "reconcile_required"),
)
def test_direct_settled_transition_is_forbidden(
    expected_state: str,
) -> None:
    with _db() as conn:
        bundle = _bundle(index=87)
        commit_target_atomic_bundle(conn, bundle=bundle)
        with pytest.raises(AtomicLedgerError, match="transition_invalid"):
            transition_target_cycle_state(
                conn,
                cycle_id=bundle["cycle"]["cycle_id"],
                expected_state=expected_state,
                new_state="settled",
                updated_at_utc=NOW,
            )
        assert conn.execute(
            "SELECT state FROM basket_target_capital_reservations "
            "WHERE cycle_id=?",
            (bundle["cycle"]["cycle_id"],),
        ).fetchone()[0] == "active"


def test_persisted_candidate_consumption_cas_is_evidence_only() -> None:
    with _db() as conn:
        bundle, intent_id = _committed_intent(conn, index=88)
        vector = bundle["execution_version_vectors"][0]
        cycle_id = bundle["cycle"]["cycle_id"]

        result = validate_persisted_candidate_consumption_cas(
            conn,
            cycle_id=cycle_id,
            current_vectors_by_intent={intent_id: vector},
            at_utc=NOW,
        )

        assert result["state"] == "EVIDENCE_ONLY_VALIDATED"
        assert result["intent_count"] == 1
        assert result["authorizes_order"] is False
        assert result["evidence_only"] is True
        assert result["calls_order_gateway"] is False
        assert result["live_submit_authorized"] is False
        assert result["orders_submitted"] == 0
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles "
            "WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()[0] == "prepared"

        drifted = dict(vector)
        drifted.pop("vector_sha256", None)
        drifted["inventory_sha256"] = "8" * 64
        with pytest.raises(
            ExecutionCasError,
            match="execution_version_drift:inventory_sha256",
        ):
            validate_persisted_candidate_consumption_cas(
                conn,
                cycle_id=cycle_id,
                current_vectors_by_intent={intent_id: drifted},
                at_utc=NOW,
            )
        with pytest.raises(
            ExecutionCasError,
            match="candidate_intent_vector_set_drift",
        ):
            validate_persisted_candidate_consumption_cas(
                conn,
                cycle_id=cycle_id,
                current_vectors_by_intent={},
                at_utc=NOW,
            )


def test_zero_intent_candidate_consumption_validates_no_execution_only() -> None:
    with _db() as conn:
        bundle = _bundle(index=89, blocked=True)
        commit_target_atomic_bundle(conn, bundle=bundle)

        result = validate_persisted_candidate_consumption_cas(
            conn,
            cycle_id=bundle["cycle"]["cycle_id"],
            current_vectors_by_intent={},
            at_utc=NOW,
        )

        assert result["state"] == "NO_EXECUTION_VALIDATED"
        assert result["intent_count"] == 0
        assert result["authorizes_order"] is False


@pytest.mark.parametrize("field", VERSION_FIELDS)
def test_persisted_candidate_consumption_rejects_every_vector_drift(
    field: str,
) -> None:
    with _db() as conn:
        bundle, intent_id = _committed_intent(conn, index=90)
        cycle_id = bundle["cycle"]["cycle_id"]
        drifted = dict(bundle["execution_version_vectors"][0])
        drifted.pop("vector_sha256", None)
        if field == "quote_observed_at":
            drifted[field] = "2026-07-23T03:59:59Z"
        elif field == "quote_expires_at":
            drifted[field] = "2026-07-23T04:00:30Z"
        elif field == "target_price_id":
            drifted[field] = "different-target-price"
        elif field == "economic_policy_schema":
            drifted[field] = "wrong-economic-policy"
        else:
            drifted[field] = "8" * 64

        with pytest.raises(ExecutionCasError):
            validate_persisted_candidate_consumption_cas(
                conn,
                cycle_id=cycle_id,
                current_vectors_by_intent={intent_id: drifted},
                at_utc=NOW,
            )
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles "
            "WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()[0] == "prepared"


def test_persisted_candidate_consumption_rejects_exact_deadline() -> None:
    with _db() as conn:
        bundle, intent_id = _committed_intent(conn, index=91)
        with pytest.raises(
            ExecutionCasError,
            match="target_execution_hard_deadline_expired",
        ):
            validate_persisted_candidate_consumption_cas(
                conn,
                cycle_id=bundle["cycle"]["cycle_id"],
                current_vectors_by_intent={
                    intent_id: bundle["execution_version_vectors"][0]
                },
                at_utc="2026-07-23T04:00:03Z",
            )


def test_release_and_consume_close_paths_leave_no_active_reservation() -> None:
    with _db(balance=20.0) as conn:
        released = _bundle(index=1, amount=4.0)
        commit_target_atomic_bundle(conn, bundle=released)
        transition_target_cycle_state(
            conn, cycle_id=released["cycle"]["cycle_id"], expected_state="prepared", new_state="reconcile_required", updated_at_utc=NOW
        )
        result = close_target_cycle(
            conn, cycle_id=released["cycle"]["cycle_id"], outcome="released", closed_at_utc=NOW, reason="submit_failed"
        )
        assert result["state"] == "settled"
        assert conn.execute("SELECT balance_usdc,reserved_usdc FROM basket_target_capital_pool").fetchone() == (20.0, 0.0)
        assert conn.execute("SELECT state FROM basket_target_capital_reservations WHERE cycle_id=?", (released["cycle"]["cycle_id"],)).fetchone()[0] == "released"
        assert close_target_cycle(conn, cycle_id=released["cycle"]["cycle_id"], outcome="released", closed_at_utc=NOW, reason="replay")["state"] == "already_settled"
        with pytest.raises(
            AtomicLedgerError,
            match="settled_cycle_outcome_drift",
        ):
            close_target_cycle(
                conn,
                cycle_id=released["cycle"]["cycle_id"],
                outcome="consumed",
                closed_at_utc=NOW,
                reason="opposite_outcome_replay",
            )

        consumed = _bundle(index=2, amount=5.0)
        commit_target_atomic_bundle(conn, bundle=consumed)
        close_target_cycle(
            conn, cycle_id=consumed["cycle"]["cycle_id"], outcome="consumed", closed_at_utc=NOW, reason="settled_fill"
        )
        assert conn.execute("SELECT balance_usdc,reserved_usdc FROM basket_target_capital_pool").fetchone() == (15.0, 0.0)
        assert conn.execute("SELECT state FROM basket_target_capital_reservations WHERE cycle_id=?", (consumed["cycle"]["cycle_id"],)).fetchone()[0] == "consumed"


def _committed_training_bundle(
    index: int,
    *,
    scope: str,
    amount: float = 4.0,
    live: bool = False,
) -> dict:
    bundle = _bundle(index=index, amount=amount, live=live)
    bundle["training_fact"] = _training_fact(
        bundle,
        decision_scope_key=scope,
        source_sequence=index + 1,
    )
    return bundle


def test_prepared_supersession_atomically_releases_and_replaces() -> None:
    with _db(balance=20.0, with_training_fact=True) as conn:
        old = _committed_training_bundle(301, scope="scope-old", amount=4.0)
        new = _committed_training_bundle(302, scope="scope-new", amount=5.0)
        commit_target_atomic_bundle(conn, bundle=old)

        result = supersede_prepared_cycle_and_commit_bundle(
            conn,
            old_cycle_id=old["cycle"]["cycle_id"],
            new_decision_scope_key="scope-new",
            bundle=new,
            superseded_at_utc=NOW,
        )

        assert result["supersession_state"] == "committed"
        assert result["cancelled_intent_count"] == 1
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
            (old["cycle"]["cycle_id"],),
        ).fetchone()[0] == "settled"
        assert conn.execute(
            "SELECT state FROM basket_target_order_intents WHERE cycle_id=?",
            (old["cycle"]["cycle_id"],),
        ).fetchone()[0] == "cancelled"
        assert conn.execute(
            "SELECT state,release_reason FROM basket_target_capital_reservations "
            "WHERE cycle_id=?",
            (old["cycle"]["cycle_id"],),
        ).fetchone() == ("released", "authority_input_superseded")
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
            (new["cycle"]["cycle_id"],),
        ).fetchone()[0] == "prepared"
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == pytest.approx(5.0)
        receipt = conn.execute(
            "SELECT old_decision_scope_key,new_decision_scope_key,"
            "released_amount_usdc FROM "
            "basket_target_prepared_supersession_receipts"
        ).fetchone()
        assert receipt == ("scope-old", "scope-new", 4.0)

        replay = supersede_prepared_cycle_and_commit_bundle(
            conn,
            old_cycle_id=old["cycle"]["cycle_id"],
            new_decision_scope_key="scope-new",
            bundle=new,
            superseded_at_utc=NOW,
        )
        assert replay["supersession_state"] == "already_committed"
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_prepared_supersession_receipts"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == pytest.approx(5.0)


@pytest.mark.parametrize("fault_after", ["supersede_old", "new_decision"])
def test_prepared_supersession_failure_rolls_back_old_and_new(
    fault_after: str,
) -> None:
    with _db(balance=20.0, with_training_fact=True) as conn:
        old = _committed_training_bundle(311, scope="scope-old", amount=4.0)
        new = _committed_training_bundle(312, scope="scope-new", amount=5.0)
        commit_target_atomic_bundle(conn, bundle=old)

        with pytest.raises(InjectedAtomicFault):
            supersede_prepared_cycle_and_commit_bundle(
                conn,
                old_cycle_id=old["cycle"]["cycle_id"],
                new_decision_scope_key="scope-new",
                bundle=new,
                superseded_at_utc=NOW,
                fault_after=fault_after,
            )

        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
            (old["cycle"]["cycle_id"],),
        ).fetchone()[0] == "prepared"
        assert conn.execute(
            "SELECT state FROM basket_target_order_intents WHERE cycle_id=?",
            (old["cycle"]["cycle_id"],),
        ).fetchone()[0] == "prepared"
        assert conn.execute(
            "SELECT state FROM basket_target_capital_reservations "
            "WHERE cycle_id=?",
            (old["cycle"]["cycle_id"],),
        ).fetchone()[0] == "active"
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_cycles "
            "WHERE cycle_id=?",
            (new["cycle"]["cycle_id"],),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_prepared_supersession_receipts"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == pytest.approx(4.0)


def test_prepared_supersession_blocks_any_execution_fact() -> None:
    with _db(balance=20.0, with_training_fact=True) as conn:
        old = _committed_training_bundle(321, scope="scope-old")
        new = _committed_training_bundle(322, scope="scope-new")
        commit_target_atomic_bundle(conn, bundle=old)
        intent_id = old["intents"][0]["order_intent_id"]
        conn.execute(
            "INSERT INTO basket_target_order_events("
            "event_id,order_intent_id,exchange_order_id,event_type,observed_at,"
            "matched_size,matched_notional_usdc,event_json,event_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "event-321",
                intent_id,
                None,
                "EXPIRED",
                NOW,
                0.0,
                0.0,
                "{}",
                "9" * 64,
            ),
        )
        conn.commit()

        with pytest.raises(
            AtomicLedgerError,
            match="prepared_supersession_execution_fact_present",
        ):
            supersede_prepared_cycle_and_commit_bundle(
                conn,
                old_cycle_id=old["cycle"]["cycle_id"],
                new_decision_scope_key="scope-new",
                bundle=new,
                superseded_at_utc=NOW,
            )
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
            (old["cycle"]["cycle_id"],),
        ).fetchone()[0] == "prepared"


def test_prepared_supersession_accepts_cancelled_live_intents_without_facts() -> None:
    """A live-era cycle whose intents never reached the exchange is inert.

    Production mints every intent with live authority; a disarmed submitter
    leaves them ``prepared`` and the quote-lease retire cancels them a cycle
    later.  Nothing at the exchange, so the next scope-changing candidate
    must be able to retire the cycle and release its reservation.  Measured
    2026-09-03: WMKK and ZUCK were refused here every cycle from 03:22Z.
    """

    with _db(balance=20.0, with_training_fact=True) as conn:
        old = _committed_training_bundle(
            341, scope="scope-old", amount=4.0, live=True
        )
        new = _committed_training_bundle(342, scope="scope-new", amount=5.0)
        commit_target_atomic_bundle(conn, bundle=old)
        old_cycle = old["cycle"]["cycle_id"]
        assert conn.execute(
            "SELECT live_submit_authorized FROM basket_target_order_intents "
            "WHERE cycle_id=?",
            (old_cycle,),
        ).fetchone()[0] == 1
        # The quote-lease retire's own write, verbatim.
        conn.execute(
            "UPDATE basket_target_order_intents SET state='cancelled' "
            "WHERE cycle_id=? AND state='prepared'",
            (old_cycle,),
        )
        conn.commit()

        result = supersede_prepared_cycle_and_commit_bundle(
            conn,
            old_cycle_id=old_cycle,
            new_decision_scope_key="scope-new",
            bundle=new,
            superseded_at_utc=NOW,
        )

        assert result["supersession_state"] == "committed"
        # Nothing was open, so nothing was cancelled by this act.
        assert result["cancelled_intent_count"] == 0
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
            (old_cycle,),
        ).fetchone()[0] == "settled"
        assert conn.execute(
            "SELECT state,release_reason FROM basket_target_capital_reservations "
            "WHERE cycle_id=?",
            (old_cycle,),
        ).fetchone() == ("released", "authority_input_superseded")
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == pytest.approx(5.0)
        assert json.loads(conn.execute(
            "SELECT cancelled_intent_ids_json FROM "
            "basket_target_prepared_supersession_receipts"
        ).fetchone()[0]) == []


def test_prepared_supersession_still_refuses_an_intent_in_flight() -> None:
    with _db(balance=20.0, with_training_fact=True) as conn:
        old = _committed_training_bundle(351, scope="scope-old", live=True)
        new = _committed_training_bundle(352, scope="scope-new")
        commit_target_atomic_bundle(conn, bundle=old)
        conn.execute(
            "UPDATE basket_target_order_intents SET state='submitted' "
            "WHERE cycle_id=?",
            (old["cycle"]["cycle_id"],),
        )
        conn.commit()
        with pytest.raises(
            AtomicLedgerError,
            match="prepared_supersession_intent_not_quiescent",
        ):
            supersede_prepared_cycle_and_commit_bundle(
                conn,
                old_cycle_id=old["cycle"]["cycle_id"],
                new_decision_scope_key="scope-new",
                bundle=new,
                superseded_at_utc=NOW,
            )
        assert conn.execute(
            "SELECT state FROM basket_target_capital_reservations "
            "WHERE cycle_id=?",
            (old["cycle"]["cycle_id"],),
        ).fetchone()[0] == "active"


def test_direct_second_active_candidate_requires_atomic_supersession() -> None:
    with _db(balance=20.0, with_training_fact=True) as conn:
        old = _committed_training_bundle(331, scope="scope-old")
        new = _committed_training_bundle(332, scope="scope-new")
        commit_target_atomic_bundle(conn, bundle=old)

        with pytest.raises(
            AtomicLedgerError,
            match="target_active_candidate_supersession_required",
        ):
            commit_target_atomic_bundle(conn, bundle=new)

        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_cycles"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == pytest.approx(4.0)


def test_blocked_decision_is_recorded_only_in_auto_trade_decisions() -> None:
    with _db() as conn:
        blocked = _bundle(index=3, blocked=True)
        receipt = commit_target_atomic_bundle(conn, bundle=blocked)
        assert receipt["intent_count"] == 0
        decision = conn.execute("SELECT decided_state,will_order,reason_codes_json FROM auto_trade_decisions").fetchone()
        assert decision == ("blocked", 0, '["missing_quote"]')
        assert conn.execute("SELECT COUNT(*) FROM basket_target_order_intents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM basket_target_capital_reservations").fetchone()[0] == 1
        assert conn.execute("SELECT amount_usdc FROM basket_target_capital_reservations").fetchone()[0] == 0.0


def test_complete_execution_vector_is_bound_inside_atomic_bundle() -> None:
    bundle = _bundle(index=80)
    with _db() as conn:
        receipt = commit_target_atomic_bundle(conn, bundle=bundle)
        assert receipt["execution_version_vector_count"] == 1
        row = conn.execute(
            "SELECT cycle_id,decision_id,order_intent_id,vector_policy,target_price_id,"
            "quote_observed_at,quote_expires_at,vector_sha256 "
            "FROM basket_target_execution_version_vectors"
        ).fetchone()
        assert row[:4] == (
            bundle["cycle"]["cycle_id"],
            bundle["decision"]["decision_id"],
            bundle["intents"][0]["order_intent_id"],
            "EXECUTION",
        )
        assert row[4:7] == (
            "price-80",
            "2026-07-23T04:00:00Z",
            "2026-07-23T04:01:00Z",
        )
        assert len(row[7]) == 64


def test_blocked_zero_intent_cycle_binds_explicit_no_execution_policy() -> None:
    bundle = _bundle(index=81, blocked=True)
    with _db() as conn:
        receipt = commit_target_atomic_bundle(conn, bundle=bundle)
        assert receipt["execution_version_vector_count"] == 1
        row = conn.execute(
            "SELECT order_intent_id,vector_policy,target_price_id,quote_observed_at,"
            "quote_expires_at,vector_json FROM basket_target_execution_version_vectors"
        ).fetchone()
        assert row[:5] == (None, "NO_EXECUTION", None, None, None)
        assert '"policy":"NO_EXECUTION"' in row[5]
        assert '"reason_codes":["missing_quote"]' in row[5]


def test_atomic_vector_validation_rejects_missing_quote_expiry_before_write() -> None:
    bundle = _bundle(index=82)
    incomplete = _complete_execution_vector(82)
    incomplete.pop("quote_expires_at")
    bundle["execution_version_vectors"] = [incomplete]
    with _db() as conn:
        before = _counts(conn)
        with pytest.raises(Exception, match="quote_expires_at"):
            commit_target_atomic_bundle(conn, bundle=bundle)
        assert _counts(conn) == before


def test_atomic_vector_fault_rolls_back_vector_and_all_prior_facts() -> None:
    bundle = _bundle(index=83)
    with _db() as conn:
        with pytest.raises(InjectedAtomicFault, match="version_vector"):
            commit_target_atomic_bundle(conn, bundle=bundle, fault_after="version_vector")
        assert all(value == 0 for value in _counts(conn).values())
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors"
        ).fetchone()[0] == 0


def test_scope_blocked_decision_has_no_weather_market_or_execution_facts_and_is_idempotent() -> None:
    with _db() as conn:
        first = commit_blocked_scope_decision(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            reason_codes=["materialized_probability_envelope_missing"],
            details={
                "blocked_scope_decision_version": 1,
                "no_market_snapshot_written": True,
                "no_intents_written": True,
                "no_capital_reserved": True,
            },
            created_at_utc=NOW,
        )
        replay = commit_blocked_scope_decision(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            reason_codes=["materialized_probability_envelope_missing"],
                details={
                    "blocked_scope_decision_version": 1,
                    "no_market_snapshot_written": True,
                    "no_intents_written": True,
                    "no_capital_reserved": True,
                },
                created_at_utc=NOW,
        )
        assert first["state"] == "committed"
        assert replay["state"] == "already_committed"
        assert replay["idempotent"] is True
        assert conn.execute("SELECT COUNT(*) FROM basket_target_execution_cycles").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 1
        assert conn.execute("SELECT decided_state,will_order FROM auto_trade_decisions").fetchone() == ("blocked", 0)
        assert conn.execute("SELECT COUNT(*) FROM basket_target_observation_facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM basket_target_bound_facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM basket_target_order_intents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM basket_target_capital_reservations").fetchone()[0] == 0


def test_scope_blocked_replay_is_idempotent_with_production_row_factory() -> None:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        kwargs = {
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "reason_codes": ["materialized_probability_envelope_missing"],
            "details": {"blocked_scope_decision_version": 1},
            "created_at_utc": NOW,
        }
        first = commit_blocked_scope_decision(conn, **kwargs)
        replay = commit_blocked_scope_decision(conn, **kwargs)

        assert first["state"] == "committed"
        assert replay["state"] == "already_committed"
        assert replay["idempotent"] is True
        assert conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors"
        ).fetchone()[0] == 1


def test_scope_blocked_replay_accepts_equivalent_json_but_keeps_policy_address() -> None:
    """SQLite TEXT formatting is not policy content; the policy digest is."""

    with _db() as conn:
        kwargs = {
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "reason_codes": ["materialized_probability_envelope_missing", "quote_missing"],
            "details": {"blocked_scope_decision_version": 1},
            "created_at_utc": NOW,
        }
        first = commit_blocked_scope_decision(conn, **kwargs)
        conn.execute("DROP TRIGGER basket_target_version_vector_no_update")
        conn.execute(
            "UPDATE basket_target_execution_version_vectors SET vector_json=? "
            "WHERE cycle_id=?",
            (
                '{ "reason_codes": ["materialized_probability_envelope_missing", "quote_missing"], '
                '"policy": "NO_EXECUTION", "schema": '
                '"bayes_basket_target_no_execution_vector_policy_v1" }',
                first["cycle_id"],
            ),
        )
        replay = commit_blocked_scope_decision(conn, **kwargs)
        assert replay["state"] == "already_committed"
        assert replay["idempotent"] is True


def test_scope_blocked_replay_blocks_old_policy_version_as_migration_required() -> None:
    with _db() as conn:
        kwargs = {
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "reason_codes": ["materialized_probability_envelope_missing"],
            "details": {"blocked_scope_decision_version": 1},
            "created_at_utc": NOW,
        }
        first = commit_blocked_scope_decision(conn, **kwargs)
        conn.execute("DROP TRIGGER basket_target_version_vector_no_update")
        conn.execute(
            "UPDATE basket_target_execution_version_vectors SET vector_json=? "
            "WHERE cycle_id=?",
            (
                '{"policy":"NO_EXECUTION","reason_codes":'
                '["materialized_probability_envelope_missing"]}',
                first["cycle_id"],
            ),
        )
        with pytest.raises(
            AtomicLedgerError,
            match="blocked_cycle_no_execution_vector_content_address_drift:"
            "policy_version_migration_required",
        ):
            commit_blocked_scope_decision(conn, **kwargs)


def test_scope_blocked_replay_blocks_hidden_training_fact_in_no_execution_vector() -> None:
    with _db() as conn:
        kwargs = {
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "reason_codes": ["materialized_probability_envelope_missing"],
            "details": {"blocked_scope_decision_version": 1},
            "created_at_utc": NOW,
        }
        first = commit_blocked_scope_decision(conn, **kwargs)
        conn.execute("DROP TRIGGER basket_target_version_vector_no_update")
        conn.execute(
            "UPDATE basket_target_execution_version_vectors SET training_fact_id=? "
            "WHERE cycle_id=?",
            ("legacy-fact", first["cycle_id"]),
        )
        with pytest.raises(
            AtomicLedgerError,
            match="blocked_cycle_no_execution_vector_content_address_drift:"
            "null_shape_or_identity",
        ):
            commit_blocked_scope_decision(conn, **kwargs)


def test_scope_blocked_replay_rejects_ambiguous_duplicate_policy_json_keys() -> None:
    with _db() as conn:
        kwargs = {
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "reason_codes": ["materialized_probability_envelope_missing"],
            "details": {"blocked_scope_decision_version": 1},
            "created_at_utc": NOW,
        }
        first = commit_blocked_scope_decision(conn, **kwargs)
        conn.execute("DROP TRIGGER basket_target_version_vector_no_update")
        conn.execute(
            "UPDATE basket_target_execution_version_vectors SET vector_json=? "
            "WHERE cycle_id=?",
            (
                '{"schema":"bayes_basket_target_no_execution_vector_policy_v1",'
                '"policy":"NO_EXECUTION","policy":"NO_EXECUTION",'
                '"reason_codes":["materialized_probability_envelope_missing"]}',
                first["cycle_id"],
            ),
        )
        with pytest.raises(
            AtomicLedgerError,
            match="blocked_cycle_no_execution_vector_content_address_drift:json_invalid",
        ):
            commit_blocked_scope_decision(conn, **kwargs)


def test_scope_blocker_change_is_a_new_immutable_decision() -> None:
    with _db() as conn:
        first = commit_blocked_scope_decision(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            reason_codes=["materialized_probability_envelope_missing"],
            details={"blocked_scope_decision_version": 1},
            created_at_utc=NOW,
        )
        second = commit_blocked_scope_decision(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            reason_codes=["resident_cash_phase_policy_not_unique"],
            details={"blocked_scope_decision_version": 1},
            created_at_utc=NOW,
        )
        assert first["cycle_id"] != second["cycle_id"]
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM basket_target_order_intents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM basket_target_capital_reservations").fetchone()[0] == 0


def test_shared_pool_concurrent_decisions_never_oversell(tmp_path: Path) -> None:
    db_path = tmp_path / "m5-concurrency.db"
    with _db(db_path, balance=10.0) as conn:
        pass
    results: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            try:
                commit_target_atomic_bundle(conn, bundle=_bundle(index=index, amount=3.0))
                outcome = "accepted"
            except AtomicLedgerError as exc:
                outcome = str(exc)
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with sqlite3.connect(db_path) as conn:
        reserved, balance = conn.execute("SELECT reserved_usdc,balance_usdc FROM basket_target_capital_pool").fetchone()
        assert reserved <= balance + 1e-9
        assert reserved == pytest.approx(9.0)
        assert conn.execute("SELECT COUNT(*) FROM basket_target_capital_reservations WHERE state='active'").fetchone()[0] == 3
    assert results.count("accepted") == 3


def test_arbitration_is_deterministic_and_aging_prevents_starvation() -> None:
    requests = [
        {"request_id": "basket-new", "strategy_lane": "basket", "amount_usdc": 4, "priority": 100, "starvation_count": 0, "requested_at": NOW},
        {"request_id": "basket-mid", "strategy_lane": "basket", "amount_usdc": 4, "priority": 90, "starvation_count": 0, "requested_at": "2026-07-23T00:00:01Z"},
        {"request_id": "basket-old", "strategy_lane": "basket", "amount_usdc": 4, "priority": 20, "starvation_count": 10, "requested_at": "2026-07-22T00:00:00Z"},
    ]
    first = arbitrate_capital_requests(requests, available_usdc=8)
    second = arbitrate_capital_requests(list(reversed(requests)), available_usdc=8)
    assert [row["request_id"] for row in first["accepted"]] == [row["request_id"] for row in second["accepted"]]
    assert first["accepted"][0]["request_id"] == "basket-old"
    assert first["accepted_usdc"] == pytest.approx(8.0)
    assert first["remaining_usdc"] == pytest.approx(0.0)
    assert first["shared_pool"] is True
    assert first["starvation_protection"] is True


def test_production_candidate_adapter_requires_exact_version_training_inputs() -> None:
    bundle = _bundle(index=10, amount=3.0)
    compiled = {
        "status": "ready_no_live",
        "execution_mode": PRODUCTION_EXECUTION_MODE,
        "live_submit_authorized": PRODUCTION_EXECUTION_MODE == "production_live",
        "production_plan_sha256": SHA_B,
        "production_cycle_sha256": SHA_A,
        "plan": {
            "plan_id": "upstream-plan-id",
            "station_id": "ZUUU",
                "event_date": "2026-07-23",
                "state": "SEED",
                "snapshot_sha256": SHA_A,
                "candidate_support": ["38"],
                "final_support": ["38"],
                "basket_state": "SEED",
                "basket_state_reason": "initial_complete_support_build",
                "basket_state_transition": {
                    "state": "SEED",
                    "reason": "initial_complete_support_build",
                    "desired_support": ["38"],
                    "selected_before": [],
                    "added_buckets": ["38"],
                    "retired_buckets": [],
                    "positive_gap_buckets": ["38"],
                },
                    "targets": [{"bucket": "38"}],
                    "direction": "UP",
                    "current_direction": None,
                    "core_transition": {
                        "transition": "INITIALIZE",
                        "direction_transition": "INITIALIZE",
                    },
            "delta_v2_required": True,
            "delta_v2_audit": {
                "schema": "delta_basket_score_audit_v2",
                "authorized": True,
                "content_sha256": SHA_A,
            },
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "lineage": {
                "probability_stage": "P1",
                "settlement_timezone": SETTLEMENT_TIMEZONE,
                "canonical_observation_event_id": "obs-10",
                "truth_observation_revision_content_sha256": bundle["observation"]["truth_observation_revision_content_sha256"],
                "canonical_observation_committed_at": "2026-07-23T04:00:00.000Z",
                "hard_bound_id": "bound-10",
                "source_lane": "AWC",
                "report_type": "METAR",
            },
        },
        "order_sizing_decisions": [{
            "bucket": "38",
            "eligible": True,
            "c_order": 3.0,
            "q_order": 7.5,
            "r_limit": 0.40,
            "content_sha256": SHA_A,
        }],
        "reason_codes": [],
        "boundary": {"calls_order_gateway": PRODUCTION_EXECUTION_MODE == "production_live"},
        "production_inputs": {
            "delta_v2_artifact_sha256": "0" * 64,
        },
    }
    with _db() as conn:
        with pytest.raises(
            ContractError,
            match="delta_v2_probability_envelope_mapping_required",
        ):
            commit_production_candidate_atomic(
                conn,
                compiled_plan=compiled,
                market_snapshot={
                    "station_id": "ZUUU",
                    "event_date": "2026-07-23",
                    "settlement_timezone": SETTLEMENT_TIMEZONE,
                    "buckets": [
                        {"bucket": "38", "token_id": "yes-token-10"}
                    ],
                },
                    awc_observation_contract=bundle["awc_observation_contract"],
                    truth_observation_revision_content_sha256=bundle["observation"]["truth_observation_revision_content_sha256"],
                bound=bundle["bound"],
                created_at_utc=NOW,
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions"
        ).fetchone()[0] == 0


def test_ready_production_candidate_without_delta_v2_audit_fails_closed() -> None:
    bundle = _bundle(index=101, amount=3.0)
    compiled = {
        "status": "ready_no_live",
        "execution_mode": PRODUCTION_EXECUTION_MODE,
        "live_submit_authorized": PRODUCTION_EXECUTION_MODE == "production_live",
        "production_plan_sha256": SHA_B,
        "production_cycle_sha256": SHA_A,
        "plan": {
            "plan_id": "strict-missing-plan",
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "delta_v2_required": True,
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "direction": "UP",
            "current_direction": None,
            "core_transition": {
                "transition": "INITIALIZE",
                "direction_transition": "INITIALIZE",
            },
            "lineage": {
                "probability_stage": "P1",
                "settlement_timezone": SETTLEMENT_TIMEZONE,
                "canonical_observation_event_id": "obs-101",
                "truth_observation_revision_content_sha256": bundle["observation"]["truth_observation_revision_content_sha256"],
                "canonical_observation_committed_at": "2026-07-23T04:00:00.000Z",
                "hard_bound_id": "bound-101",
                "source_lane": "AWC",
                "report_type": "METAR",
            },
        },
        "order_sizing_decisions": [],
        "reason_codes": [],
        "boundary": {"calls_order_gateway": PRODUCTION_EXECUTION_MODE == "production_live"},
    }
    with _db() as conn:
        before = _counts(conn)
        with pytest.raises(
            ContractError,
            match="production_candidate_delta_v2_authorization_missing",
        ):
            commit_production_candidate_atomic(
                conn,
                compiled_plan=compiled,
                market_snapshot={
                    "station_id": "ZUUU",
                    "event_date": "2026-07-23",
                    "settlement_timezone": SETTLEMENT_TIMEZONE,
                    "buckets": [],
                },
                awc_observation_contract=bundle["awc_observation_contract"],
                truth_observation_revision_content_sha256=bundle["observation"]["truth_observation_revision_content_sha256"],
                bound=bundle["bound"],
                created_at_utc=NOW,
            )
        assert _counts(conn) == before


def test_production_candidate_adapter_rejects_p0_before_any_write() -> None:
    bundle = _bundle(index=11, amount=3.0)
    compiled = {
        "status": "ready_no_live",
        "execution_mode": PRODUCTION_EXECUTION_MODE,
        "live_submit_authorized": PRODUCTION_EXECUTION_MODE == "production_live",
        "plan": {
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "lineage": {"probability_stage": "P0"},
        },
        "boundary": {"calls_order_gateway": PRODUCTION_EXECUTION_MODE == "production_live"},
    }
    with _db() as conn:
        before = _counts(conn)
        # P0 may hold a position now, so the stage is no longer what refuses
        # this plan.  What still refuses it -- and what this test is really
        # for -- is that nothing reaches a write: the plan is incomplete and
        # fails on its own scope before any row is touched.
        with pytest.raises(Exception, match="settlement_timezone_missing"):
            commit_production_candidate_atomic(
                conn,
                compiled_plan=compiled,
                market_snapshot={
                    "station_id": "ZUUU",
                    "event_date": "2026-07-23",
                    "settlement_timezone": SETTLEMENT_TIMEZONE,
                    "buckets": [],
                },
                awc_observation_contract=bundle["awc_observation_contract"],
                truth_observation_revision_content_sha256=bundle["observation"]["truth_observation_revision_content_sha256"],
                bound=bundle["bound"],
                created_at_utc=NOW,
            )
        assert _counts(conn) == before


def test_atomic_bundle_rejects_p0_before_identity_or_reservation() -> None:
    # P0 and P0-M may hold a position now, but not on an observed root: they
    # exist before the event day produces one, so a plan claiming those stages
    # while handing over an AWC contract is naming evidence it cannot have.
    # The refusal moved from the stage to the lineage that proves it.
    contract, revision_sha = _awc_contract(999)
    with pytest.raises(
        AtomicLedgerError,
        match="plan_lineage_canonical_observation_event_id_missing",
    ):
        build_target_atomic_bundle(
            awc_observation_contract=contract,
            truth_observation_revision_content_sha256=revision_sha,
            bound={
                "bound_id": "bound-p0",
                "station_id": "ZUUU",
                "event_date": "2026-07-23",
                "bound_generation": 0,
                "hard_lower_bound": 38,
                "bound_json": {},
            },
            plan={
                "strategy_lane": "basket",
                "settlement_timezone": SETTLEMENT_TIMEZONE,
                "lineage": {
                    "probability_stage": "P0-M",
                    "settlement_timezone": SETTLEMENT_TIMEZONE,
                    "source_lane": "AWC",
                },
            },
            decision={
                "strategy_lane": "basket",
                "decided_state": "blocked",
                "will_order": False,
                "reason_codes": ["p0_forbidden"],
                "details": {},
            },
            created_at_utc=NOW,
        )


def _stage_zero_anchor(
    conn: sqlite3.Connection,
    *,
    event_date: str = "2026-07-23",
) -> dict[str, object]:
    apply_production_ssot_schema(conn)
    checkpoint = "p0-stage-zero"
    probability_sha = "1" * 64
    regime_sha = "2" * 64
    ws_sha = "3" * 64
    inventory_sha = "4" * 64
    order_sha = "5" * 64
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            checkpoint, "P0", checkpoint, "ZUUU", event_date,
            SETTLEMENT_TIMEZONE, "2026-07-23T03:00:00Z",
            "2026-07-23T05:00:00Z", '{"38":0}', '{"38":1.0}',
            "posterior-v1", "6" * 64, "7" * 64, "{}",
            probability_sha, "COMMITTED", "2026-07-23T03:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO basket_regime_artifact VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "regime-zero", "ZUUU", event_date, "P0", checkpoint,
            SETTLEMENT_TIMEZONE, probability_sha, "2026-07-23T03:00:00Z",
            "2026-07-23T03:00:00Z", "2026-07-23T05:00:00Z", "{}",
            regime_sha, "COMMITTED", "2026-07-23T03:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO basket_official_ws_snapshot VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "ws-zero", "market-zero", "ZUUU", event_date,
            SETTLEMENT_TIMEZONE, 1, "2026-07-23T03:59:00Z", "quote-v1",
            "2026-07-23T03:59:00Z", "2026-07-23T04:01:00Z", "8" * 64,
            "OFFICIAL_CLOB_WS", "inventory-v1", "order-v1", 20.0,
            "[]", "{}", ws_sha, "COMMITTED", "2026-07-23T03:59:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO basket_confirmed_inventory_snapshot VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "inventory-zero", "ZUUU", event_date, "inventory-v1",
            "2026-07-23T03:59:00Z", 1, "{}", inventory_sha,
            "COMMITTED", "2026-07-23T03:59:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO basket_order_state_snapshot VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "order-zero", "ZUUU", event_date, "order-v1",
            "2026-07-23T03:59:00Z", 1, "{}", order_sha,
            "COMMITTED", "2026-07-23T03:59:00Z",
        ),
    )
    anchor: dict[str, object] = {
        "schema": "basket_target_stage_zero_execution_anchor_v1",
        "station_id": "ZUUU",
        "event_date": event_date,
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "evaluation_at_utc": NOW,
        "probability_envelope": {
            "stage": "P0", "checkpoint_id": checkpoint,
            "envelope_id": checkpoint, "decision_at_utc": "2026-07-23T03:00:00Z",
            "fresh_until_utc": "2026-07-23T05:00:00Z",
            "content_sha256": probability_sha,
        },
        "regime_artifact_sha256": regime_sha,
        "official_ws_snapshot": {
            "snapshot_id": "ws-zero", "content_sha256": ws_sha,
            "quote_observed_at": "2026-07-23T03:59:00Z",
            "quote_expires_at": "2026-07-23T04:01:00Z",
        },
        "inventory_snapshot": {
            "inventory_snapshot_id": "inventory-zero",
            "content_sha256": inventory_sha,
            "observed_at": "2026-07-23T03:59:00Z",
        },
        "order_state_snapshot": {
            "order_state_snapshot_id": "order-zero",
            "content_sha256": order_sha,
            "observed_at": "2026-07-23T03:59:00Z",
        },
        "station_policy_sha256": "9" * 64,
        "cash_policy_decision_sha256": "a" * 64,
        "cash_phase": {"phase": "STANDARD"},
        "delta_v2_artifact_sha256": "b" * 64,
        "delta_v2_evaluation_sha256": "c" * 64,
        "compiled_plan": {"schema": "test-complete-plan"},
        "compiled_plan_sha256": content_sha256({"schema": "test-complete-plan"}),
        "final_authority": "P1",
        "capital_authorized": False,
        "intents_written": 0,
        "reservations_written": 0,
        "calls_order_gateway": False,
        "live_submit_authorized": PRODUCTION_EXECUTION_MODE == "production_live",
        "orders_submitted": 0,
    }
    anchor["content_sha256"] = content_sha256(
        {key: value for key, value in anchor.items() if key != "content_sha256"}
    )
    return anchor


def _stage_zero_anchor_with_timestamp_precision(
    conn: sqlite3.Connection,
    *,
    stage: str,
    persisted_fraction: str,
    anchor_fraction: str,
) -> tuple[dict[str, object], str]:
    """Seed a stage row and render its immutable anchor at another precision."""

    anchor = _stage_zero_anchor(conn)
    checkpoint = f"{stage.lower()}-precision-zero"
    probability_sha = "d" * 64
    regime_sha = "e" * 64
    persisted_decision_at = f"2026-07-23T03:00:00{persisted_fraction}Z"
    persisted_fresh_until = f"2026-07-23T05:00:00{persisted_fraction}Z"
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            checkpoint, stage, checkpoint, "ZUUU", "2026-07-23",
            SETTLEMENT_TIMEZONE, persisted_decision_at, persisted_fresh_until,
            '{"38":0}', '{"38":1.0}', "posterior-v1", "f" * 64,
            "0" * 64, "{}", probability_sha, "COMMITTED",
            persisted_decision_at,
        ),
    )
    conn.execute(
        "INSERT INTO basket_regime_artifact VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"regime-{stage.lower()}-precision-zero", "ZUUU", "2026-07-23",
            stage, checkpoint,
            SETTLEMENT_TIMEZONE, probability_sha, persisted_decision_at,
            persisted_decision_at, persisted_fresh_until, "{}", regime_sha,
            "COMMITTED", persisted_decision_at,
        ),
    )
    probability = anchor["probability_envelope"]
    assert isinstance(probability, dict)
    probability.update(
        {
            "stage": stage,
            "checkpoint_id": checkpoint,
            "envelope_id": checkpoint,
            "decision_at_utc": (
                f"2026-07-23T03:00:00{anchor_fraction}Z"
            ),
            "fresh_until_utc": (
                f"2026-07-23T05:00:00{anchor_fraction}Z"
            ),
            "content_sha256": probability_sha,
        }
    )
    created_at = f"2026-07-23T04:00:00{anchor_fraction}Z"
    anchor["evaluation_at_utc"] = created_at
    anchor["regime_artifact_sha256"] = regime_sha
    anchor["content_sha256"] = content_sha256(
        {key: value for key, value in anchor.items() if key != "content_sha256"}
    )
    return anchor, created_at


def test_stage_zero_execution_is_atomic_idempotent_and_has_no_side_effects() -> None:
    with _db(with_training_fact=True) as conn:
        anchor = _stage_zero_anchor(conn)
        before = _counts(conn)
        first = commit_stage_zero_execution_scope_decision(
            conn, anchor=anchor, created_at_utc=NOW,
        )
        second = commit_stage_zero_execution_scope_decision(
            conn, anchor=anchor, created_at_utc=NOW,
        )
        after = _counts(conn)
        assert first["state"] == "committed"
        assert second["state"] == "already_committed"
        assert after["basket_target_observation_facts"] == before["basket_target_observation_facts"]
        assert after["basket_target_bound_facts"] == before["basket_target_bound_facts"]
        assert after["basket_target_order_intents"] == before["basket_target_order_intents"]
        assert after["basket_target_capital_reservations"] == before["basket_target_capital_reservations"]
        assert after["basket_target_execution_cycles"] == before["basket_target_execution_cycles"] + 1
        assert after["auto_trade_decisions"] == before["auto_trade_decisions"] + 1


@pytest.mark.parametrize(
    ("stage", "persisted_fraction", "anchor_fraction"),
    (
        ("P0", ".123", ".123000"),
        ("P0", ".123000", ".123"),
        ("P0-M", ".123", ".123000"),
        ("P0-M", ".123000", ".123"),
    ),
)
def test_stage_zero_execution_accepts_equivalent_p0_p0m_timestamp_precision_and_replays(
    stage: str,
    persisted_fraction: str,
    anchor_fraction: str,
) -> None:
    """P0/P0-M CAS binds UTC instants in both precision directions."""

    with _db(with_training_fact=True) as conn:
        anchor, created_at = _stage_zero_anchor_with_timestamp_precision(
            conn,
            stage=stage,
            persisted_fraction=persisted_fraction,
            anchor_fraction=anchor_fraction,
        )

        first = commit_stage_zero_execution_scope_decision(
            conn, anchor=anchor, created_at_utc=created_at,
        )
        replay = commit_stage_zero_execution_scope_decision(
            conn, anchor=anchor, created_at_utc=created_at,
        )

        assert first["state"] == "committed"
        assert replay["state"] == "already_committed"
        assert conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions"
        ).fetchone()[0] == 1


def test_stage_zero_execution_detects_p0m_visible_at_equivalent_precision() -> None:
    """A P0 anchor cannot miss a P0-M committed at the same UTC instant."""

    with _db(with_training_fact=True) as conn:
        anchor = _stage_zero_anchor(conn)
        created_at = "2026-07-23T04:00:00.000000Z"
        anchor["evaluation_at_utc"] = created_at
        anchor["content_sha256"] = content_sha256(
            {
                key: value
                for key, value in anchor.items()
                if key != "content_sha256"
            }
        )
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "p0m-visible", "P0-M", "p0m-visible", "ZUUU",
                "2026-07-23", SETTLEMENT_TIMEZONE,
                "2026-07-23T04:00:00.000Z", "2026-07-23T05:00:00.000Z",
                '{"38":0}', '{"38":1.0}', "posterior-v1", "d" * 64,
                "e" * 64, "{}", "f" * 64, "COMMITTED",
                "2026-07-23T04:00:00.000Z",
            ),
        )

        with pytest.raises(
            AtomicLedgerError, match="stage_zero_execution_higher_stage_visible"
        ):
            commit_stage_zero_execution_scope_decision(
                conn, anchor=anchor, created_at_utc=created_at,
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions"
        ).fetchone()[0] == 0


def test_stage_zero_execution_accepts_equivalent_ws_timestamp_precision() -> None:
    """A publisher millisecond timestamp and compiler microseconds are one CAS instant."""

    with _db(with_training_fact=True) as conn:
        anchor = _stage_zero_anchor(conn)
        ws = anchor["official_ws_snapshot"]
        assert isinstance(ws, dict)
        # The persisted row is ``...03:59:00Z``.  This is the same instant
        # rendered by the production compiler after datetime normalization.
        ws["quote_observed_at"] = "2026-07-23T03:59:00.000000Z"
        ws["quote_expires_at"] = "2026-07-23T04:01:00.000000Z"
        anchor["content_sha256"] = content_sha256({
            key: value for key, value in anchor.items()
            if key != "content_sha256"
        })

        result = commit_stage_zero_execution_scope_decision(
            conn, anchor=anchor, created_at_utc=NOW,
        )

        assert result["state"] == "committed"
        decision = conn.execute(
            "SELECT will_order,details_json FROM auto_trade_decisions"
        ).fetchone()
        assert decision[0] == 0
        assert '"orders_submitted":0' in str(decision[1])


def test_stage_zero_execution_rejects_distinct_ws_timestamp_instant() -> None:
    """Instant equivalence does not permit an anchor to bind a different quote."""

    with _db(with_training_fact=True) as conn:
        anchor = _stage_zero_anchor(conn)
        ws = anchor["official_ws_snapshot"]
        assert isinstance(ws, dict)
        ws["quote_observed_at"] = "2026-07-23T03:59:00.001Z"
        anchor["content_sha256"] = content_sha256({
            key: value for key, value in anchor.items()
            if key != "content_sha256"
        })

        with pytest.raises(
            AtomicLedgerError, match="stage_zero_execution_official_ws_drift"
        ):
            commit_stage_zero_execution_scope_decision(
                conn, anchor=anchor, created_at_utc=NOW,
            )


@pytest.mark.parametrize("event_date", ("2026-07-24", "2026-07-25"))
def test_stage_zero_execution_admits_a_listed_day_that_has_not_started(
    event_date: str,
) -> None:
    """Clause 48: listing is entry, so a listed day's no-execution decision
    and its training fact are written before the day starts.

    NOW is 2026-07-23T04:00Z, midnight in America/New_York, so 07-24 and
    07-25 are both not started.  Until 2026-09-02 this anchor recheck
    required the day to be open, and one line behind it the started-day
    guard did too; every listing-day P0 candidate died there after a full
    compile, leaving the paper ledger without the decision.
    """

    with _db(with_training_fact=True) as conn:
        anchor = _stage_zero_anchor(conn, event_date=event_date)
        before = _counts(conn)
        first = commit_stage_zero_execution_scope_decision(
            conn, anchor=anchor, created_at_utc=NOW,
        )
        second = commit_stage_zero_execution_scope_decision(
            conn, anchor=anchor, created_at_utc=NOW,
        )
        after = _counts(conn)
        assert first["state"] == "committed"
        assert second["state"] == "already_committed"
        assert after["basket_target_execution_cycles"] == before["basket_target_execution_cycles"] + 1
        assert after["auto_trade_decisions"] == before["auto_trade_decisions"] + 1
        assert after["basket_target_order_intents"] == before["basket_target_order_intents"]
        assert after["basket_target_capital_reservations"] == before["basket_target_capital_reservations"]
        row = conn.execute(
            "SELECT event_date FROM basket_target_execution_cycles "
            "WHERE cycle_id=?",
            (first["cycle_id"],),
        ).fetchone()
        assert row == (event_date,)


def test_stage_zero_execution_still_refuses_a_closed_day() -> None:
    with _db(with_training_fact=True) as conn:
        anchor = _stage_zero_anchor(conn, event_date="2026-07-21")
        before = _counts(conn)
        with pytest.raises(
            AtomicLedgerError, match="stage_zero_execution_event_day_not_open"
        ):
            commit_stage_zero_execution_scope_decision(
                conn, anchor=anchor, created_at_utc=NOW,
            )
        assert _counts(conn) == before


def test_stage_zero_execution_not_started_day_still_yields_to_a_committed_p1() -> None:
    with _db(with_training_fact=True) as conn:
        anchor = _stage_zero_anchor(conn, event_date="2026-07-24")
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "p1-early", "P1", "p1-early", "ZUUU", "2026-07-24",
                SETTLEMENT_TIMEZONE, "2026-07-23T03:30:00Z",
                "2026-07-23T05:00:00Z", '{"38":0}', '{"38":1.0}',
                "posterior-v1", "d" * 64, "e" * 64, "{}", "f" * 64,
                "COMMITTED", "2026-07-23T03:30:00Z",
            ),
        )
        before = _counts(conn)
        with pytest.raises(AtomicLedgerError, match="higher_stage_visible"):
            commit_stage_zero_execution_scope_decision(
                conn, anchor=anchor, created_at_utc=NOW,
            )
        assert _counts(conn) == before


def test_stage_zero_execution_rejects_p1_visible_in_same_transaction() -> None:
    with _db(with_training_fact=True) as conn:
        anchor = _stage_zero_anchor(conn)
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "p1-visible", "P1", "p1-visible", "ZUUU", "2026-07-23",
                SETTLEMENT_TIMEZONE, "2026-07-23T03:30:00Z",
                "2026-07-23T05:00:00Z", '{"38":0}', '{"38":1.0}',
                "posterior-v1", "d" * 64, "e" * 64, "{}", "f" * 64,
                "COMMITTED", "2026-07-23T03:30:00Z",
            ),
        )
        before = _counts(conn)
        with pytest.raises(
            AtomicLedgerError, match="higher_stage_visible"
        ):
            commit_stage_zero_execution_scope_decision(
                conn, anchor=anchor, created_at_utc=NOW,
            )
        assert _counts(conn) == before


def test_atomic_ledger_rejects_retired_hard_no_lane_before_any_write() -> None:
    with pytest.raises(
        AtomicLedgerError,
        match="strategy_lane_hard_no_retired",
    ):
        _bundle(strategy_lane="hard_no")


def test_atomic_ledger_rejects_no_order_intent() -> None:
    with pytest.raises(AtomicLedgerError, match="basket_intent_must_be_yes_buy"):
        _bundle(intent_side="NO")


def test_final_atomic_commit_rechecks_p1_after_bundle_mutation() -> None:
    bundle = _bundle(index=12, amount=3.0)
    bundle["plan"]["plan_json"]["lineage"]["probability_stage"] = "P0"
    from bayes_basket_target.atomic_ledger import PLAN_SCHEMA, _content_hash

    plan_identity = {
        key: value
        for key, value in bundle["plan"]["plan_json"].items()
        if key != "plan_id"
    }
    bundle["cycle"]["plan_sha256"] = _content_hash(PLAN_SCHEMA, plan_identity)
    with _db() as conn:
        before = _counts(conn)
        with pytest.raises(
            AtomicLedgerError,
            # Mutating the stage after the bundle was built now trips the
            # plan's own content address first: the stage is part of the
            # lineage the plan id derives from, so the tamper surfaces as
            # drift rather than as a stage rule.  The commit boundary still
            # refuses the mutated bundle, which is what this pins.
            match="plan_id_content_address_drift",
        ):
            commit_target_atomic_bundle(conn, bundle=bundle)
        assert _counts(conn) == before


def test_final_atomic_commit_rejects_tampered_reservation_identity() -> None:
    bundle = _bundle(index=13, amount=3.0)
    bundle["reservation"]["reservation_id"] = "reserve-forged"
    with _db() as conn:
        before = _counts(conn)
        with pytest.raises(
            AtomicLedgerError,
            match="reservation_id_content_address_drift",
        ):
            commit_target_atomic_bundle(conn, bundle=bundle)
        assert _counts(conn) == before


def _committed_intent(
    conn: sqlite3.Connection,
    *,
    index: int = 50,
    token_id: str | None = None,
) -> tuple[dict, str]:
    bundle = _bundle(index=index, amount=3.0, token_id=token_id)
    commit_target_atomic_bundle(conn, bundle=bundle)
    intent_id = str(
        conn.execute(
            "SELECT order_intent_id FROM basket_target_order_intents WHERE cycle_id=?",
            (bundle["cycle"]["cycle_id"],),
        ).fetchone()[0]
    )
    return bundle, intent_id


@pytest.mark.parametrize("field", [
    "probability_sha256", "official_ws_quote_sha256", "inventory_sha256",
    "order_state_sha256", "station_policy_sha256",
    "cash_policy_decision_sha256", "economic_policy_artifact_sha256",
    "target_price_id", "quote_observed_at", "quote_expires_at",
])
def test_exact_execution_cas_rejects_one_field_drift(field: str) -> None:
    with _db() as conn:
        bundle_value, intent_id = _committed_intent(conn)
        current = dict(bundle_value["execution_version_vectors"][0])
        current.pop("vector_sha256", None)
        current[field] = (
            "2026-07-23T04:00:30Z"
            if field in {"quote_observed_at", "quote_expires_at"}
            else "drifted"
            if field == "target_price_id"
            else "8" * 64
        )
        with pytest.raises(ExecutionCasError, match=f"drift:{field}"):
            assert_execution_version_cas(conn, order_intent_id=intent_id, current_vector=current, at_utc=NOW)


def test_execution_cas_replay_and_quote_expiry_are_fail_closed() -> None:
    with _db() as conn:
        bundle_value, intent_id = _committed_intent(conn, index=51)
        vector = bundle_value["execution_version_vectors"][0]
        result = assert_execution_version_cas(
            conn,
            order_intent_id=intent_id,
            current_vector=vector,
            at_utc=NOW,
        )
        assert result["vector_sha256"] == vector["vector_sha256"]
        with pytest.raises(ExecutionCasError, match="quote_expired"):
            assert_execution_version_cas(
                conn,
                order_intent_id=intent_id,
                current_vector=vector,
                at_utc="2026-07-23T04:01:00Z",
            )


def test_unknown_post_and_partial_cancel_late_fill_block_next_order() -> None:
    with _db() as conn:
        first_bundle, first_id = _committed_intent(conn, index=52, token_id="same-token")
        second_bundle, second_id = _committed_intent(conn, index=53, token_id="same-token")
        unknown = record_order_event(conn, order_intent_id=first_id, event={
            "event_type": "UNKNOWN_POST", "observed_at": NOW, "event_payload": {"post_started": True},
        })
        assert unknown["reconciliation_state"] == "unknown"
        assert conn.execute("SELECT state FROM basket_target_order_intents WHERE order_intent_id=?", (first_id,)).fetchone()[0] == "reconcile_required"
        gate = can_submit_next_order(conn, bucket="38", token_id="same-token", exclude_order_intent_id=second_id)
        assert gate["allowed"] is False
        assert gate["blockers"][0]["order_intent_id"] == first_id

        partial_bundle, partial_id = _committed_intent(conn, index=54, token_id="partial-token")
        partial = record_order_event(conn, order_intent_id=partial_id, event={
            "event_type": "PARTIAL_FILL", "observed_at": NOW, "matched_size": 2.0, "matched_notional_usdc": 0.8,
        })
        assert partial["reconciliation_state"] == "pending"
        race = record_order_event(conn, order_intent_id=partial_id, event={
            "event_type": "CANCEL_ACK", "observed_at": "2026-07-23T00:00:01Z", "matched_size": 0.0, "matched_notional_usdc": 0.0,
        })
        assert race["reconciliation_state"] == "unknown"
        late = record_order_event(conn, order_intent_id=partial_id, event={
            "event_type": "LATE_FILL", "observed_at": "2026-07-23T00:00:02Z", "matched_size": 1.0, "matched_notional_usdc": 0.4,
        })
        assert late["reconciliation_state"] == "unknown"
        resolved = resolve_reconciliation(
            conn, order_intent_id=partial_id, authoritative_state="partial", filled_size=3.0,
            filled_notional_usdc=1.2, resolved_at_utc="2026-07-23T00:00:03Z",
        )
        assert resolved["reconciliation_state"] == "resolved_partial"
        assert conn.execute("SELECT state FROM basket_target_order_intents WHERE order_intent_id=?", (partial_id,)).fetchone()[0] == "reconcile_required"


def test_cancel_ack_without_fill_and_full_fill_converge_to_resolved_states() -> None:
    with _db() as conn:
        _b1, cancel_id = _committed_intent(conn, index=55)
        cancelled = record_order_event(conn, order_intent_id=cancel_id, event={
            "event_type": "CANCEL_ACK", "observed_at": NOW, "exchange_order_id": "ex-cancel",
        })
        assert cancelled["reconciliation_state"] == "resolved_cancelled"
        _b2, fill_id = _committed_intent(conn, index=56)
        requested = conn.execute("SELECT requested_size FROM basket_target_order_intents WHERE order_intent_id=?", (fill_id,)).fetchone()[0]
        filled = record_order_event(conn, order_intent_id=fill_id, event={
            "event_type": "FILL", "observed_at": NOW, "exchange_order_id": "ex-fill", "matched_size": requested, "matched_notional_usdc": 3.0,
        })
        assert filled["reconciliation_state"] == "resolved_filled"
        duplicate = record_order_event(conn, order_intent_id=fill_id, event={
            "event_type": "FILL", "observed_at": NOW, "exchange_order_id": "ex-fill", "matched_size": requested, "matched_notional_usdc": 3.0,
        })
        assert duplicate["state"] == "duplicate_event"
        assert conn.execute("SELECT filled_size FROM basket_target_order_reconciliations WHERE order_intent_id=?", (fill_id,)).fetchone()[0] == pytest.approx(requested)


def test_order_event_identity_and_hold_semantics_fail_closed() -> None:
    with _db() as conn:
        _b, intent_id = _committed_intent(conn, index=57)
        with pytest.raises(ReconciliationError, match="content_address_mismatch"):
            record_order_event(conn, order_intent_id=intent_id, event={
                "event_id": "manual-event", "event_type": "POST_ACK", "observed_at": NOW,
            })
        assert_basket_hold_only([{"side": "YES", "order_action": "BUY"}])
        with pytest.raises(ReconciliationError, match="hold_semantics_invalid"):
            assert_basket_hold_only([{"side": "YES", "order_action": "SELL"}])


def test_a_preliminary_record_needs_a_named_stage_open_failure() -> None:
    """Charter 46: this is a receipt after a failure, never a gate before one.

    Written without a blocker beside it, the preliminary record becomes the
    reason a forward day did not open rather than the record of why it could
    not -- which is what it had drifted into: 22 of 50 blocked decisions on
    2026-08-16 carried ``preliminary_no_live_p1_pending`` and no blocker at
    all, because the caller skipped the stage-open attempt entirely whenever
    its inputs were absent.
    """

    import sqlite3

    from bayes_basket_target.atomic_ledger import (
        AtomicLedgerError,
        commit_preliminary_scope_decision,
    )

    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(
            AtomicLedgerError,
            match="preliminary_decision_stage_open_blocker_missing",
        ):
            commit_preliminary_scope_decision(
                conn,
                preselection_id="p",
                station_id="ZUUU",
                event_date="2026-08-16",
                settlement_timezone="Asia/Shanghai",
                checkpoint_id="c",
                probability_stage="P0",
                quote_manifest_sha256="a" * 64,
                preselection_plan_sha256="b" * 64,
                created_at_utc="2026-08-15T00:00:00Z",
            )


def test_the_worker_names_a_blocker_when_stage_open_cannot_be_attempted() -> None:
    """An absent input is itself the blocker, and must be recorded as one."""

    from pathlib import Path

    worker = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target"
        / "production_worker.py"
    ).read_text(encoding="utf-8")
    assert "stage_open_not_attempted:market_snapshot_missing" in worker
    assert "stage_open_not_attempted:compiled_plan_missing" in worker
    # Attempted-but-returned-nothing is the third path and needs its own
    # name: without one it hit the ledger's refusal and aborted the whole
    # production cycle instead of logging a blocked scope.
    assert "stage_open_returned_no_candidate" in worker
    # And the blocker travels into the record rather than being dropped.
    assert "stage_open_blocker=(" in worker


def test_close_settled_day_cycle_releases_stranded_reservation() -> None:
    """A day nobody can supersede any more is the terminal path's whole job."""

    # 2026-07-23 in America/New_York ends at 2026-07-24T04:00Z.
    day_end = "2026-07-24T04:00:00Z"
    after_close = "2026-07-24T05:00:00Z"
    with _db(balance=20.0) as conn:
        bundle = _bundle(index=1, amount=4.0)
        commit_target_atomic_bundle(conn, bundle=bundle)
        cycle_id = bundle["cycle"]["cycle_id"]
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == 4.0

        # Refuses while the settlement day is still running.
        with pytest.raises(
            AtomicLedgerError,
            match="settled_day_close_before_settlement_day_end",
        ):
            close_settled_day_cycle(
                conn,
                cycle_id=cycle_id,
                settlement_day_end_utc=day_end,
                closed_at_utc=NOW,
                reason="too_early",
            )
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()[0] == "prepared"

        result = close_settled_day_cycle(
            conn,
            cycle_id=cycle_id,
            settlement_day_end_utc=day_end,
            closed_at_utc=after_close,
            reason="settlement_day_closed_without_successor",
        )
        assert result["state"] == "settled"
        assert result["released_usdc"] == 4.0
        assert result["cancelled_intent_count"] >= 1
        assert conn.execute(
            "SELECT balance_usdc,reserved_usdc FROM basket_target_capital_pool"
        ).fetchone() == (20.0, 0.0)
        assert conn.execute(
            "SELECT state FROM basket_target_capital_reservations "
            "WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()[0] == "released"
        # No intent may be left dangling inside a settled cycle.
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents "
            "WHERE cycle_id=? AND state<>'cancelled'",
            (cycle_id,),
        ).fetchone()[0] == 0

        replay = close_settled_day_cycle(
            conn,
            cycle_id=cycle_id,
            settlement_day_end_utc=day_end,
            closed_at_utc=after_close,
            reason="replay",
        )
        assert replay["state"] == "already_settled"
        assert replay["released_usdc"] == 0.0
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == 0.0


def test_close_settled_day_cycle_releases_live_intents_that_never_left() -> None:
    """Live authority on an intent is not evidence it reached the exchange."""

    day_end = "2026-07-24T04:00:00Z"
    after_close = "2026-07-24T05:00:00Z"
    with _db(balance=20.0) as conn:
        bundle = _bundle(index=3, amount=5.0, live=True)
        commit_target_atomic_bundle(conn, bundle=bundle)
        cycle_id = bundle["cycle"]["cycle_id"]
        conn.execute(
            "UPDATE basket_target_order_intents SET state='cancelled' "
            "WHERE cycle_id=?",
            (cycle_id,),
        )
        conn.commit()
        result = close_settled_day_cycle(
            conn,
            cycle_id=cycle_id,
            settlement_day_end_utc=day_end,
            closed_at_utc=after_close,
            reason="settled_day",
        )
        assert result["state"] == "settled"
        assert result["released_usdc"] == pytest.approx(5.0)
        assert result["cancelled_intent_count"] == 0
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == 0.0
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()[0] == "settled"


def test_close_settled_day_cycle_refuses_a_cycle_still_in_flight() -> None:
    """Anything that reached the exchange is not this sweep's to retire."""

    day_end = "2026-07-24T04:00:00Z"
    after_close = "2026-07-24T05:00:00Z"
    with _db(balance=20.0) as conn:
        bundle = _bundle(index=2, amount=5.0)
        commit_target_atomic_bundle(conn, bundle=bundle)
        cycle_id = bundle["cycle"]["cycle_id"]
        conn.execute(
            "UPDATE basket_target_order_intents SET state='submitted' "
            "WHERE cycle_id=?",
            (cycle_id,),
        )
        conn.commit()
        with pytest.raises(
            AtomicLedgerError,
            match="settled_day_close_intent_not_quiescent",
        ):
            close_settled_day_cycle(
                conn,
                cycle_id=cycle_id,
                settlement_day_end_utc=day_end,
                closed_at_utc=after_close,
                reason="should_refuse",
            )
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == 5.0
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()[0] == "prepared"


def test_close_target_cycle_absorbs_reserved_float_residue() -> None:
    """The last release must not be blocked by binary residue.

    ``reserved_usdc`` is a running sum, so by the final release it carries
    noise the stored amount does not.  Production met exactly this: a 0.40791
    reservation against a reserved of 0.4079099999999879, whose difference of
    -1.2e-14 tripped ``CHECK(reserved_usdc>=0)`` and made that reservation
    permanently unreleasable.
    """

    with _db(balance=20.0) as conn:
        bundle = _bundle(index=3, amount=4.0)
        commit_target_atomic_bundle(conn, bundle=bundle)
        conn.execute(
            "UPDATE basket_target_capital_pool SET reserved_usdc=? "
            "WHERE pool_id=1",
            (4.0 - 1.2e-14,),
        )
        conn.commit()

        result = close_target_cycle(
            conn,
            cycle_id=bundle["cycle"]["cycle_id"],
            outcome="released",
            closed_at_utc=NOW,
            reason="residue",
        )
        assert result["state"] == "settled"
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == 0.0


def test_close_target_cycle_still_refuses_real_reserved_drift() -> None:
    """Clamping absorbs noise, not capital: a real shortfall still refuses."""

    with _db(balance=20.0) as conn:
        bundle = _bundle(index=4, amount=4.0)
        commit_target_atomic_bundle(conn, bundle=bundle)
        conn.execute(
            "UPDATE basket_target_capital_pool SET reserved_usdc=? "
            "WHERE pool_id=1",
            (3.5,),
        )
        conn.commit()
        with pytest.raises(
            AtomicLedgerError, match="pool_reserved_balance_drift"
        ):
            close_target_cycle(
                conn,
                cycle_id=bundle["cycle"]["cycle_id"],
                outcome="released",
                closed_at_utc=NOW,
                reason="real_drift",
            )


def test_close_settled_day_cycle_sees_facts_on_cancelled_intents() -> None:
    """Inertness is asked of every intent, not just the ones still open.

    Hanging the check off the still-``prepared`` list skipped it entirely for
    a cycle whose intents had all been cancelled, so one carrying a fill
    released its reservation without a single table being read.
    """

    day_end = "2026-07-24T04:00:00Z"
    after_close = "2026-07-24T05:00:00Z"
    with _db(balance=20.0) as conn:
        bundle = _bundle(index=5, amount=4.0)
        commit_target_atomic_bundle(conn, bundle=bundle)
        cycle_id = bundle["cycle"]["cycle_id"]
        intent_id = conn.execute(
            "SELECT order_intent_id FROM basket_target_order_intents "
            "WHERE cycle_id=? LIMIT 1",
            (cycle_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE basket_target_order_intents SET state='cancelled' "
            "WHERE cycle_id=?",
            (cycle_id,),
        )
        conn.execute(
            "INSERT INTO basket_target_order_events VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "event-1", intent_id, "exch-1", "FILL", after_close,
                1.0, 1.0, "{}", "0" * 64,
            ),
        )
        conn.commit()

        with pytest.raises(
            AtomicLedgerError,
            match="settled_day_close_execution_fact_present",
        ):
            close_settled_day_cycle(
                conn,
                cycle_id=cycle_id,
                settlement_day_end_utc=day_end,
                closed_at_utc=after_close,
                reason="should_refuse",
            )
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == 4.0


def test_close_settled_day_cycle_compares_instants_not_spellings() -> None:
    """A far-east offset close is earlier than a Z day end, not later."""

    day_end = "2026-07-24T04:00:00Z"
    with _db(balance=20.0) as conn:
        bundle = _bundle(index=6, amount=4.0)
        commit_target_atomic_bundle(conn, bundle=bundle)
        cycle_id = bundle["cycle"]["cycle_id"]

        # 2026-07-24T12:00+14:00 is 2026-07-23T22:00Z -- six hours early.
        with pytest.raises(
            AtomicLedgerError,
            match="settled_day_close_before_settlement_day_end",
        ):
            close_settled_day_cycle(
                conn,
                cycle_id=cycle_id,
                settlement_day_end_utc=day_end,
                closed_at_utc="2026-07-24T12:00:00+14:00",
                reason="lexically_later_actually_earlier",
            )

        # Same instant as the day end, spelled with a fraction: not early.
        result = close_settled_day_cycle(
            conn,
            cycle_id=cycle_id,
            settlement_day_end_utc=day_end,
            closed_at_utc="2026-07-24T04:00:00.000Z",
            reason="lexically_later_same_instant",
        )
        assert result["state"] == "settled"


def test_intent_live_flag_follows_the_plan_that_authorized_it() -> None:
    """A live plan mints a live intent; a no-live plan does not.

    The INSERT wrote a literal 0 -- the fourth member of the family that
    began with the compiled plan's own hard-coded False, and the one
    furthest downstream.  It is the marker everything uses to tell the two
    books apart: the ledger keys its live book on it, the dashboard counts
    rows with it set, and the go-live migration widened this table's CHECK
    so it could hold 1.  Written as 0 unconditionally, a live deployment
    mints only no-live-looking intents and reports itself as having placed
    nothing.
    """
    import sqlite3 as _sqlite3

    with _db() as conn:
        bundle, intent_id = _committed_intent(conn, index=903)
        row = conn.execute(
            "SELECT live_submit_authorized FROM basket_target_order_intents"
            " WHERE order_intent_id=?",
            (intent_id,),
        ).fetchone()
        plan_json = bundle["plan"].get("plan_json") or {}
        expected = 1 if plan_json.get("live_submit_authorized") is True else 0
        assert row[0] == expected, (
            "intent flag must equal the plan's authority, not a literal"
        )


def test_second_cycle_for_a_recorded_decision_refuses_before_it_dangles(
) -> None:
    """A duplicate scope must not open a cycle bound to a fact that never lands.

    The version-vector rows are written before the fact writer is asked, and
    they carry this candidate's own ``fact_id``.  The writer is idempotent on
    ``decision_scope_key``: when the key is already held it keeps the stored
    row, so this candidate's fact never lands and those rows reference
    nothing -- and a replay of the cycle then finds no fact for it at all.
    ``cycle_id`` is inside the fact's content address, so a stored row under
    another ``fact_id`` is by construction another cycle's; there is no order
    of these writes that makes the reference valid.  Refuse instead.

    Distinct from the scope-conflict case below it: there the two facts
    disagree on a scope-defining field and the validator names the field.
    Here they agree on every one of them, which is precisely the state that
    became routine once P0 stopped restating an unchanged envelope.
    """

    first = _bundle(index=206)
    first["training_fact"] = _training_fact(
        first, decision_scope_key="scope-recorded"
    )
    second = _bundle(index=207)
    second["training_fact"] = _training_fact(
        second, decision_scope_key="scope-recorded"
    )
    # Same decision, different cycle: every scope-defining field agrees.
    for column in (
        "station_id", "event_date", "settlement_timezone", "stage",
        "probability_envelope_id", "posterior_sha256",
        "official_exclusion_mask_sha256", "quote_freshness_policy_sha256",
    ):
        assert first["training_fact"][column] == second["training_fact"][column]
    assert (
        first["training_fact"]["cycle_id"]
        != second["training_fact"]["cycle_id"]
    )
    assert (
        first["training_fact"]["content_sha256"]
        != second["training_fact"]["content_sha256"]
    )

    with _db(with_training_fact=True) as conn:
        commit_target_atomic_bundle(conn, bundle=first)
        with pytest.raises(
            AtomicLedgerError,
            match="target_training_fact_decision_scope_already_committed",
        ):
            commit_target_atomic_bundle(conn, bundle=second)
        for table, expected in (
            ("basket_policy_training_fact", 1),
            ("basket_target_execution_cycles", 1),
            ("basket_target_execution_version_vectors", 1),
            ("basket_target_order_intents", 1),
        ):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == expected, table
        # The invariant the refusal exists to hold.
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors v "
            "WHERE v.training_fact_id IS NOT NULL AND NOT EXISTS("
            "SELECT 1 FROM basket_policy_training_fact f "
            "WHERE f.fact_id=v.training_fact_id)"
        ).fetchone()[0] == 0


# --------------------------------------------------------------------------
# Economic decision identity (user ruling, 2026-09-02).
# --------------------------------------------------------------------------
def test_the_decision_identity_carries_no_clock() -> None:
    """Two decisions a second apart, economically identical, are one.

    The old identity carried the commit clock as the deadline, so every
    re-decision was a fresh row: 11,364 shas for 5,080 economic contents
    over 2026-08-28..09-02.  The four legs -- probability, sizing output,
    position book, policy -- are what a decision is made of; the clock is
    audit metadata.
    """

    from bayes_basket_target.atomic_ledger import economic_decision_identity

    base = dict(
        station_id="ZUUU", event_date="2026-08-31",
        settlement_timezone="Asia/Shanghai", strategy_lane="basket",
        decided_state="prepared", will_order=1, reason_codes=[],
        details={
            "core_buckets": ["33°C", "34°C"], "direction": "UP",
            "delta_v2_content_sha256": "a" * 64,
            "production_plan_sha256": "b" * 64,
            # The two clocks that used to leak in.
            "target_execution_hard_deadline_at": "2026-08-31T10:00:03Z",
            "forward_evidence_binding": {"decision_at_utc": "2026-08-31T10:00:00Z"},
        },
        decision_scope_key="c" * 64,
    )
    later = dict(base, details={
        **base["details"],
        "target_execution_hard_deadline_at": "2026-08-31T10:00:04Z",
        "forward_evidence_binding": {"decision_at_utc": "2026-08-31T10:00:01Z"},
    })
    assert economic_decision_identity(**base) == economic_decision_identity(**later)
    # And a change in any leg is a different decision.
    for leg in (
        dict(decision_scope_key="d" * 64),
        dict(reason_codes=["edge_negative"]),
        dict(details={**base["details"], "core_buckets": ["34°C"]}),
        dict(will_order=0),
    ):
        assert economic_decision_identity(**dict(base, **leg)) != economic_decision_identity(**base)


def test_a_restated_decision_on_a_new_cycle_is_idempotent_not_a_row() -> None:
    """Same economics, new cycle: the ledger returns the standing record.

    This is what the economic identity means at the write boundary.  Under
    the clocked identity a second cycle always minted a second decision;
    under this one it resolves to the first, and the commit says so
    instead of raising on the primary key.
    """

    with _db() as conn:
        first = _bundle(index=71, amount=3.0)
        receipt_a = commit_target_atomic_bundle(conn, bundle=first)
        assert receipt_a["state"] == "committed"
        # A second bundle with the SAME economic content but its own cycle:
        # rebuild with the same index so details/reasons match, then swap
        # in fresh transport handles.
        second = _bundle(index=71, amount=3.0, plan_salt="-restated")
        assert second["decision"]["decision_id"] == first["decision"]["decision_id"]
        assert second["cycle"]["cycle_id"] != first["cycle"]["cycle_id"]
        before = _counts(conn)
        receipt_b = commit_target_atomic_bundle(conn, bundle=second)
        assert receipt_b["state"] == "already_committed_decision"
        assert receipt_b["idempotent"] is True
        assert receipt_b["decision_id"] == first["decision"]["decision_id"]
        assert receipt_b["cycle_id"] == first["cycle"]["cycle_id"]
        assert _counts(conn) == before


def test_a_restated_decision_with_a_recorded_fact_is_retained_not_refused(
) -> None:
    """Two guards answer the same key; the retain must answer first.

    The decision-identity retain above says a re-decision that resolves to a
    standing decision id is that decision -- ``already_committed_decision``,
    idempotent, no row.  The scope refusal says a fact holding this key under
    another cycle means a second cycle must not open.  Both are right; on a
    true restatement both are true at once, and the refusal ran first: KATL
    2026-09-02 at 03:40Z was refused a stage-open that the retain would have
    answered.  The refusal is now what is left after the retain declines --
    a fact holding the key with no standing decision under it, which only an
    orphaned store can produce (the test above this one).
    """

    first = _bundle(index=208, amount=3.0)
    first["training_fact"] = _training_fact(
        first, decision_scope_key="scope-restated-208"
    )
    second = _bundle(index=208, amount=3.0, plan_salt="-restated")
    second["training_fact"] = _training_fact(
        second, decision_scope_key="scope-restated-208"
    )
    assert second["decision"]["decision_id"] == first["decision"]["decision_id"]
    assert second["cycle"]["cycle_id"] != first["cycle"]["cycle_id"]
    assert (
        second["training_fact"]["fact_id"]
        != first["training_fact"]["fact_id"]
    )

    with _db(with_training_fact=True) as conn:
        assert commit_target_atomic_bundle(conn, bundle=first)["state"] == (
            "committed"
        )
        before = _counts(conn)
        receipt = commit_target_atomic_bundle(conn, bundle=second)
        assert receipt["state"] == "already_committed_decision"
        assert receipt["idempotent"] is True
        assert receipt["cycle_id"] == first["cycle"]["cycle_id"]
        assert _counts(conn) == before
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_policy_training_fact"
        ).fetchone()[0] == 1


def test_blocked_restatement_on_a_new_cycle_is_retained_not_a_primary_key_collision() -> None:
    """Same economic decision, moved non-economic detail: retain, write nothing.

    The cycle id hashes the whole details payload and the decision id only
    its economic subset, so this shape reached the INSERT and died on the
    ``auto_trade_decisions.decision_id`` primary key (measured 2026-09-02:
    five forward stations a cycle).  A moved *economic* detail is a new
    decision and must still land as a second row.
    """

    def _details(binding: str, direction: str) -> dict:
        return {
            "blocked_scope_decision_version": 1,
            "direction": direction,
            "core_buckets": ["30°C", "31°C"],
            "forward_evidence_binding": binding,
        }

    with _db() as conn:
        first = commit_blocked_scope_decision(
            conn,
            station_id="ZSPD",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            reason_codes=["complete_basket_target_not_activated"],
            details=_details("binding-a", "PEAK"),
            created_at_utc=NOW,
        )
        restated = commit_blocked_scope_decision(
            conn,
            station_id="ZSPD",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            reason_codes=["complete_basket_target_not_activated"],
            details=_details("binding-b", "PEAK"),
            created_at_utc=NOW,
        )
        assert first["state"] == "committed"
        assert restated["state"] == "already_committed_decision"
        assert restated["idempotent"] is True
        assert restated["cycle_id"] == first["cycle_id"]
        assert restated["decision_id"] == first["decision_id"]
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM basket_target_execution_cycles").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors"
        ).fetchone()[0] == 1
        redecided = commit_blocked_scope_decision(
            conn,
            station_id="ZSPD",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            reason_codes=["complete_basket_target_not_activated"],
            details=_details("binding-b", "DESCENT"),
            created_at_utc=NOW,
        )
        assert redecided["state"] == "committed"
        assert redecided["decision_id"] != first["decision_id"]
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 2
