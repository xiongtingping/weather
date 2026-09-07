from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.atomic_ledger import DECISION_SCHEMA
from bayes_basket_target.contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from bayes_basket_target.delta_v2_training import FACT_COLUMNS, FACT_SCHEMA
from bayes_basket_target.target_ssot_replay import replay_durable_target_chain
from bayes_basket_target.market_registry import (
    apply_target_market_registry_schema,
)
from bayes_basket_target.ssot import apply_production_ssot_schema


STATION = "RKPK"
DAY = "2026-07-29"
TZ = "Asia/Seoul"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _seed(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    path = tmp_path / "target.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(conn)
    apply_target_market_registry_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            SHA_A, "reported_trajectory_market_registry_v1",
            "shared_live_registry_export_only", "2026-07-28T00:00:00Z",
            "/source.db", 1, "2026-07-28T00:00:00Z", SHA_B,
        ),
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry("
        "snapshot_sha256,slug,event_date,outcome,city_slug,station_id,"
        "settlement_timezone,settlement_unit,source_lane,source_eligible,"
        "source_block_reason,resolution_url,rules_sha256,yes_token,no_token,"
        "market_active,market_accepting_orders,registry_json,refreshed_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            SHA_A, "rkpk-high", DAY, "39°C", "busan", STATION, TZ, "C",
            "awc", 1, "", "", SHA_C, "yes-39", "no-39", 1, 1, "{}",
            "2026-07-28T00:00:00Z",
        ),
    )
    return conn, path


def _append_hold(
    conn: sqlite3.Connection,
    *,
    ordinal: int,
    stage: str,
    decision_at: str,
    price: float = 0.2,
    core_buckets: list[str] | None = None,
    current_core_buckets: list[str] | None = None,
    direction: str | None = None,
    current_direction: str | None = None,
    plan_state: str = "HOLD",
) -> None:
    suffix = str(ordinal)
    checkpoint = f"checkpoint-{suffix}"
    posterior_sha = content_sha256({"posterior": ordinal})
    envelope = ProbabilityEnvelope.from_mapping({
        "stage": stage,
        "checkpoint_id": checkpoint,
        "station_id": STATION,
        "event_date": DAY,
        "settlement_timezone": TZ,
        "decision_at_utc": decision_at,
        "fresh_until_utc": decision_at.replace(":00Z", ":59Z"),
        "bucket_ordinals": {"39°C": 0, "40°C": 1},
        "probabilities": {"39°C": 0.6, "40°C": 0.4},
        "posterior_version": f"posterior-{suffix}",
        "posterior_sha256": posterior_sha,
        "bridge_sha256": SHA_B,
        "lineage": {
            "settlement_timezone": TZ,
            "settlement_definition": "official_daily_tmax_c",
            "lead_bucket": "d0",
            "hard_excluded_buckets": "NONE",
        },
    })
    encoded = envelope.to_dict()
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES("
        + ",".join("?" for _ in range(17)) + ")",
        (
            checkpoint, stage, checkpoint, STATION, DAY, TZ, decision_at,
            encoded["fresh_until_utc"],
            json.dumps(encoded["bucket_ordinals"]),
            json.dumps(encoded["probabilities"]),
            encoded["posterior_version"], encoded["posterior_sha256"],
            encoded["bridge_sha256"], json.dumps(encoded["lineage"]),
            envelope.content_sha256, "COMMITTED", decision_at,
        ),
    )

    ws_id = f"ws-{suffix}"
    quote_version = f"quote-{suffix}"
    inventory_id = f"inventory-{suffix}"
    inventory_version = f"inv-{suffix}"
    order_id = f"order-{suffix}"
    order_version = f"ord-{suffix}"
    quote_sha = content_sha256({"quote": ordinal})
    inventory_sha = content_sha256({"inventory": ordinal})
    order_sha = content_sha256({"order": ordinal})
    conn.execute(
        "INSERT INTO basket_official_ws_snapshot VALUES("
        + ",".join("?" for _ in range(20)) + ")",
        (
            ws_id, "market-1", STATION, DAY, TZ, ordinal, decision_at,
            quote_version, decision_at,
            decision_at.replace(":00Z", ":45Z"), SHA_D,
            "OFFICIAL_CLOB_WS", inventory_version, order_version, 20.0,
            json.dumps([{
                "bucket": "39°C",
                "token_id": "yes-39",
                "ask_levels": [{"price": price, "shares": 100.0}],
                "bid_levels": [],
            }]),
            "{}", quote_sha, "COMMITTED", decision_at,
        ),
    )
    conn.execute(
        "INSERT INTO basket_confirmed_inventory_snapshot "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            inventory_id, STATION, DAY, inventory_version, decision_at, 1,
            "{}", inventory_sha, "COMMITTED", decision_at,
        ),
    )
    conn.execute(
        "INSERT INTO basket_order_state_snapshot "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            order_id, STATION, DAY, order_version, decision_at, 1, "{}",
            order_sha, "COMMITTED", decision_at,
        ),
    )

    plan_id = f"plan-{suffix}"
    plan_sha = content_sha256({"plan": ordinal})
    cycle_id = f"cycle-{suffix}"
    decision_id = f"decision-{suffix}"
    reasons = ["no_execution_candidate"]
    details = {
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    decision_identity = {
        "cycle_id": cycle_id,
        "plan_id": plan_id,
        "station_id": STATION,
        "event_date": DAY,
        "settlement_timezone": TZ,
        "strategy_lane": "basket",
        "decided_state": "blocked",
        "will_order": False,
        "reason_codes": reasons,
        "details": details,
        "target_execution_hard_deadline_at": None,
    }
    decision_sha = content_sha256({
        "schema": DECISION_SCHEMA,
        **decision_identity,
    })
    conn.execute(
        "INSERT INTO basket_target_execution_cycles VALUES("
        + ",".join("?" for _ in range(11)) + ")",
        (
            cycle_id, plan_id, STATION, DAY, TZ, "basket", plan_sha, 0.0,
            "prepared", decision_at, decision_at,
        ),
    )
    # Columns named, not counted.  A positional insert breaks the day the
    # table grows -- decision_scope_key was added between decision_sha256
    # and created_at and this read "15 columns but 14 values were
    # supplied", which says nothing about what the test is for.
    conn.execute(
        "INSERT INTO auto_trade_decisions("
        "decision_id,cycle_id,plan_id,station_id,event_date,"
        "settlement_timezone,strategy_lane,decided_state,will_order,"
        "target_execution_hard_deadline_at,reason_codes_json,details_json,"
        "decision_sha256,created_at) VALUES("
        + ",".join("?" for _ in range(14)) + ")",
        (
            decision_id, cycle_id, plan_id, STATION, DAY, TZ, "basket",
            "blocked", 0, None, json.dumps(reasons), json.dumps(details),
            decision_sha, decision_at,
        ),
    )
    conn.execute(
        "INSERT INTO basket_target_capital_reservations VALUES("
        + ",".join("?" for _ in range(10)) + ")",
        (
            f"reserve-{suffix}", cycle_id, "basket", 0.0, "released",
            f"reserve-key-{suffix}", SHA_A, "no_execution", decision_at,
            decision_at,
        ),
    )
    no_execution_sha = content_sha256({"no_execution": ordinal})
    conn.execute(
        "INSERT INTO basket_target_execution_version_vectors("
        "order_intent_id,cycle_id,decision_id,training_fact_id,vector_policy,"
        "vector_json,vector_sha256,created_at"
        ") VALUES(NULL,?,?,NULL,'NO_EXECUTION',?,?,?)",
        (
            cycle_id, decision_id, json.dumps({"policy": "NO_EXECUTION"}),
            no_execution_sha, decision_at,
        ),
    )

    feature = {
        "schema": "delta_basket_score_forward_feature_vector_v2",
        "execution_version_vectors": [],
        "no_execution_policy": {
            "schema": "bayes_basket_target_no_execution_vector_policy_v1",
            "policy": "NO_EXECUTION",
            "policy_sha256": no_execution_sha,
            "reason_codes": reasons,
        },
        "no_live_boundary": {
            "execution_mode": "production_no_live",
            "live_submit_authorized": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    if core_buckets is not None or current_core_buckets is not None:
        feature["plan"] = {
            "state": plan_state,
            "core_buckets": core_buckets,
            "current_core_buckets": current_core_buckets,
            "direction": direction,
            "current_direction": current_direction,
            "final_support": list(core_buckets or []),
        }
    identity = {
        "station_id": STATION,
        "event_date": DAY,
        "settlement_timezone": TZ,
        "decision_at_utc": decision_at,
        "decision_scope_key": content_sha256({"scope": ordinal}),
        "captured_at_utc": decision_at,
        "source_sequence": ordinal,
        "quote_observed_at": decision_at,
        "quote_expires_at": decision_at.replace(":00Z", ":45Z"),
        "quote_freshness_policy_sha256": SHA_D,
        "stage": stage,
        "probability_envelope_id": checkpoint,
        "posterior_version": envelope.posterior_version,
        "posterior_sha256": envelope.posterior_sha256,
        "official_exclusion_mask_sha256": SHA_C,
        "ws_snapshot_id": ws_id,
        "quote_version": quote_version,
        "quote_sha256": quote_sha,
        "inventory_snapshot_id": inventory_id,
        "inventory_version": inventory_version,
        "inventory_sha256": inventory_sha,
        "order_state_snapshot_id": order_id,
        "order_state_version": order_version,
        "order_state_sha256": order_sha,
        "cycle_id": cycle_id,
        "decision_id": decision_id,
        "decision_sha256": decision_sha,
        "plan_sha256": plan_sha,
        "intent_count": 0,
        "intent_set_sha256": content_sha256([]),
        "policy_version_vector_json": json.dumps({"policy": "v1"}),
        "feature_json": json.dumps(feature),
        "partition_key": f"{STATION}|summer|{stage}|d0|normal",
        "authority_scope": "PRODUCTION_NO_LIVE_FORWARD",
        "created_at_utc": decision_at,
    }
    fact_sha = content_sha256({"schema": FACT_SCHEMA, **identity})
    fact = {
        "fact_id": f"d2fact_{fact_sha[:40]}",
        **identity,
        "content_sha256": fact_sha,
    }
    conn.execute(
        "INSERT INTO basket_policy_training_fact("
        + ",".join(FACT_COLUMNS) + ") VALUES("
        + ",".join("?" for _ in FACT_COLUMNS) + ")",
        tuple(fact[name] for name in FACT_COLUMNS),
    )


def test_real_schema_replay_is_read_only_prefix_suffix_and_oos_nonblocking(
    tmp_path: Path,
) -> None:
    conn, path = _seed(tmp_path)
    _append_hold(
        conn, ordinal=1, stage="P0", decision_at="2026-07-28T15:01:00Z"
    )
    conn.commit()
    conn.close()
    prefix_conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    prefix_conn.row_factory = sqlite3.Row
    prefix = replay_durable_target_chain(
        prefix_conn, station_id=STATION, event_date=DAY
    )
    prefix_conn.close()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _append_hold(
        conn, ordinal=2, stage="P1", decision_at="2026-07-28T15:02:00Z"
    )
    conn.commit()
    conn.close()
    replay_conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    replay_conn.row_factory = sqlite3.Row
    suffix = replay_durable_target_chain(
        replay_conn,
        station_id=STATION,
        event_date=DAY,
        initial_state=prefix["continuation"],
    )
    full = replay_durable_target_chain(
        replay_conn, station_id=STATION, event_date=DAY
    )
    assert full["rows"] == prefix["rows"] + suffix["rows"]
    assert full["continuation"] == suffix["continuation"]
    assert (
        full["ws_economic_transitions"]
        == prefix["ws_economic_transitions"]
        + suffix["ws_economic_transitions"]
    )
    assert full["label_status"] == {
        "materialized_label_count": 0,
        "required_for_production_no_live_replay": False,
    }
    assert full["forward_oos"] == {
        "consulted": False,
        "blocks_production_no_live": False,
        "blocks_formal_parameter_promotion_or_live": True,
    }
    assert all(row["execution_policy"] == "NO_EXECUTION" for row in full["rows"])
    before = path.read_bytes()
    replay_durable_target_chain(
        replay_conn, station_id=STATION, event_date=DAY
    )
    replay_conn.close()
    assert path.read_bytes() == before


def test_replay_rejects_stage_downgrade_and_registry_timezone_drift(
    tmp_path: Path,
) -> None:
    conn, _path = _seed(tmp_path)
    _append_hold(
        conn, ordinal=1, stage="P1", decision_at="2026-07-28T15:01:00Z"
    )
    _append_hold(
        conn, ordinal=2, stage="P0", decision_at="2026-07-28T15:02:00Z"
    )
    conn.commit()
    with pytest.raises(ContractError, match="probability_stage_downgrade"):
        replay_durable_target_chain(conn, station_id=STATION, event_date=DAY)
    conn.close()

    drift_dir = tmp_path / "drift"
    drift_dir.mkdir()
    drift_conn, _drift_path = _seed(drift_dir)
    _append_hold(
        drift_conn,
        ordinal=1,
        stage="P0",
        decision_at="2026-07-28T15:01:00Z",
    )
    drift_conn.execute(
        "INSERT INTO basket_target_market_registry("
        "snapshot_sha256,slug,event_date,outcome,city_slug,station_id,"
        "settlement_timezone,settlement_unit,source_lane,source_eligible,"
        "source_block_reason,resolution_url,rules_sha256,yes_token,no_token,"
        "market_active,market_accepting_orders,registry_json,refreshed_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            SHA_A, "rkpk-high-drift", DAY, "40°C", "busan", STATION,
            "America/New_York", "C", "awc", 1, "", "", SHA_C,
            "yes-40", "no-40", 1, 1, "{}", "2026-07-28T00:00:00Z",
        ),
    )
    drift_conn.commit()
    with pytest.raises(ContractError, match="registry_timezone_not_unique"):
        replay_durable_target_chain(
            drift_conn, station_id=STATION, event_date=DAY
        )


def test_replay_keeps_low_high_low_and_deduplicates_keepalive_semantics(
    tmp_path: Path,
) -> None:
    conn, _path = _seed(tmp_path)
    for ordinal, price in enumerate((0.10, 0.40, 0.10, 0.10), 1):
        _append_hold(
            conn,
            ordinal=ordinal,
            stage="P0",
            decision_at=f"2026-07-28T15:0{ordinal}:00Z",
            price=price,
        )
    conn.commit()
    result = replay_durable_target_chain(
        conn, station_id=STATION, event_date=DAY
    )
    assert result["ws_economic_summary"]["transition_count"] == 3
    assert (
        result["ws_economic_summary"]["adjacent_keepalive_duplicate_count"]
        == 1
    )
    assert result["ws_economic_summary"]["low_high_low_return_count"] == 1
    assert [
        row["official_ws_economic_changed"] for row in result["rows"]
    ] == [True, True, True, False]
    assert result["ws_economic_transitions"][2][
        "is_low_high_low_return"
    ] is True


def test_explicit_hold_inherits_core_and_direction_and_rejects_mutation(
    tmp_path: Path,
) -> None:
    conn, _path = _seed(tmp_path)
    _append_hold(
        conn,
        ordinal=1,
        stage="P0",
        decision_at="2026-07-28T15:01:00Z",
        core_buckets=["39°C"],
        current_core_buckets=[],
        direction="UP",
        current_direction=None,
    )
    _append_hold(
        conn,
        ordinal=2,
        stage="P0",
        decision_at="2026-07-28T15:02:00Z",
        core_buckets=["39°C"],
        current_core_buckets=["39°C"],
        direction="UP",
        current_direction="UP",
    )
    conn.commit()
    result = replay_durable_target_chain(
        conn, station_id=STATION, event_date=DAY
    )
    assert result["continuation"]["last_core_buckets"] == ["39°C"]
    assert result["continuation"]["last_direction"] == "UP"

    mutate_dir = tmp_path / "mutate"
    mutate_dir.mkdir()
    bad, _path = _seed(mutate_dir)
    _append_hold(
        bad,
        ordinal=1,
        stage="P0",
        decision_at="2026-07-28T15:01:00Z",
        core_buckets=["39°C"],
        current_core_buckets=[],
        direction="UP",
        current_direction=None,
    )
    _append_hold(
        bad,
        ordinal=2,
        stage="P0",
        decision_at="2026-07-28T15:02:00Z",
        core_buckets=["40°C"],
        current_core_buckets=["39°C"],
        direction="UP",
        current_direction="UP",
    )
    bad.commit()
    with pytest.raises(ContractError, match="hold_mutated"):
        replay_durable_target_chain(
            bad, station_id=STATION, event_date=DAY
        )


def test_as_of_excludes_future_facts_without_waiting_for_labels(
    tmp_path: Path,
) -> None:
    conn, _path = _seed(tmp_path)
    _append_hold(
        conn,
        ordinal=1,
        stage="P0",
        decision_at="2026-07-28T15:01:00Z",
    )
    _append_hold(
        conn,
        ordinal=2,
        stage="P1",
        decision_at="2026-07-28T15:02:00Z",
    )
    conn.commit()
    result = replay_durable_target_chain(
        conn,
        station_id=STATION,
        event_date=DAY,
        as_of_utc="2026-07-28T15:01:30Z",
    )
    assert result["replayed_fact_count"] == 1
    assert result["rows"][0]["stage"] == "P0"
    assert result["label_status"][
        "required_for_production_no_live_replay"
    ] is False
