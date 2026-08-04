from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.station_policy_exact_stage_scorecard import (
    build_exact_stage_scorecard,
)
from bayes_basket_target.station_policy_forward_outcome_binding import (
    METRIC_CONTRACT_STATE,
    UNAVAILABLE_REASONS,
    assert_station_policy_utility_contract_pointer,
    build_future_station_policy_outcome_materialization,
)
from scripts.build_station_policy_candidate_bundle import build_bundle


def _bundles(tmp_path: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for stage in ("P0", "P0-M", "P1"):
        path = tmp_path / f"{stage}.json"
        path.write_text(json.dumps(build_bundle(stage=stage)), encoding="utf-8")
        paths[stage] = path
    return paths


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE basket_policy_training_fact(
          fact_id TEXT, station_id TEXT, event_date TEXT,
          settlement_timezone TEXT, decision_at_utc TEXT,
          captured_at_utc TEXT, stage TEXT, partition_key TEXT,
          feature_json TEXT, created_at_utc TEXT
        );
        CREATE TABLE basket_policy_training_label(
          label_id TEXT, fact_id TEXT, truth_revision_no INTEGER,
          winning_bucket TEXT, settlement_closed_at_utc TEXT,
          labeled_at_utc TEXT, authority_scope TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "d2fact_" + "a" * 40,
            "RKPK",
            "2026-07-31",
            "Asia/Seoul",
            "2026-07-31T00:00:00Z",
            "2026-07-31T00:00:00Z",
            "P0",
            "RKPK|summer|P0|d1|normal",
            "{}",
            "2026-07-31T00:00:00Z",
        ),
    )
    return conn


def test_exact_stage_scorecard_emits_all_three_stages_and_fails_closed_without_metric_contracts(
    tmp_path: Path,
) -> None:
    conn = _conn()
    stations = [row["station_id"] for row in build_bundle()["activations"]]
    receipt = build_exact_stage_scorecard(
        conn,
        as_of_utc="2026-08-01T00:00:00Z",
        station_ids=stations,
        bundle_paths=_bundles(tmp_path),
    )

    assert set(receipt["stages"]) == {"P0", "P0-M", "P1"}
    assert receipt["checks"]["promotion_permitted"] is False
    assert receipt["checks"]["policy_utility_metric_contracts_defined"] is True
    assert receipt["checks"]["policy_utility_metric_binding_complete"] is False
    assert receipt["checks"]["policy_utility_score_rows_emitted"] is False
    assert receipt["orders_submitted"] == 0
    # The scorecard must expose immutable utility-contract lineage while
    # remaining explicitly unbound and non-promotable.
    pointer = receipt["utility_contract_pointer"]
    assert_station_policy_utility_contract_pointer(pointer)
    assert pointer["state"] == "DEFINED_UNBOUND"
    assert pointer["metric_state"] == METRIC_CONTRACT_STATE
    assert pointer["promotion_permitted"] is False
    p0 = receipt["stages"]["P0"]
    assert p0["candidate_bundle"]["artifact_count"] == 135
    assert "source_formula_not_exact_stage_trained" in p0["families"]["target"]["blockers"]
    assert "station_policy_utility_evidence_binding_unbound" in p0["families"]["target"]["blockers"]
    assert "official_clob_ws_snapshot_reference_column_missing" in p0["families"]["target"]["blockers"]
    assert p0["families"]["target"]["strict_oos"]["complete"] is False
    target_materialization = p0["family_outcome_materialization"]["target"]
    assert target_materialization["frozen_labeled_fact_count"] == 0
    assert target_materialization["unavailable_frozen_outcome_materialization_count"] == 0
    assert target_materialization["weather_or_execution_proxy_scoring_forbidden"] is True
    # All exact-stage family reports carry the same pointer, preventing a
    # downstream 45-station ledger join from silently losing contract
    # lineage.  This is not a utility score or an activation signal.
    for stage in receipt["stages"].values():
        for family in stage["families"].values():
            family_pointer = family["utility_evidence_contract"]["utility_contract_pointer"]
            assert_station_policy_utility_contract_pointer(family_pointer)
            assert family_pointer == pointer


def test_exact_stage_scorecard_excludes_open_future_event_from_coverage(
    tmp_path: Path,
) -> None:
    conn = _conn()
    stations = [row["station_id"] for row in build_bundle()["activations"]]
    receipt = build_exact_stage_scorecard(
        conn,
        # RKPK's 2026-07-31 Asia/Seoul settlement day closes at 2026-07-31
        # 15:00Z, so this fact is observational and must not look frozen.
        as_of_utc="2026-07-31T00:00:00Z",
        station_ids=stations,
        bundle_paths=_bundles(tmp_path),
    )
    assert receipt["input_counts"]["observational_candidate_not_oos_count"] == 1
    assert receipt["input_counts"]["observational_candidate_not_oos_by_stage"] == {
        "P0": 1,
        "P0-M": 0,
        "P1": 0,
    }
    # Open/future rows remain interface observations only.  They cannot
    # inflate strict OOS coverage, but they also must not become a
    # production_no_live blocker merely because the scorecard saw them.
    assert "future_event_not_oos" not in receipt["blockers"]
    assert receipt["checks"]["observational_candidate_not_oos_blocks_production_no_live"] is False
    p0 = receipt["stages"]["P0"]
    assert p0["fact_label_coverage"]["observed_station_count"] == 0
    rkpk = next(row for row in p0["station_reports"] if row["station_id"] == "RKPK")
    assert rkpk["frozen_fact_count"] == 0
    assert "future_event_not_oos" not in rkpk["blockers"]


def _outcome_gap_feature() -> dict[str, object]:
    return {
        "station_policy_outcome_materialization": (
            build_future_station_policy_outcome_materialization(
                station_id="RKPK",
                event_date="2026-07-31",
                settlement_timezone="Asia/Seoul",
                stage="P1",
                decision_at_utc="2026-07-31T00:00:00Z",
                strategy_version="target-v2-shadow",
                season_bin="summer",
                maturity_bin="d1",
                liquidity_bin="liquid",
            )
        )
    }


def test_exact_stage_scorecard_counts_future_outcome_contract_gaps_without_scores(
    tmp_path: Path,
) -> None:
    conn = _conn()
    fact_id = "d2fact_" + "b" * 40
    conn.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            fact_id,
            "RKPK",
            "2026-07-31",
            "Asia/Seoul",
            "2026-07-31T00:00:00Z",
            "2026-07-31T00:00:00Z",
            "P1",
            "RKPK|summer|P1|d1|liquid",
            json.dumps(_outcome_gap_feature(), sort_keys=True),
            "2026-07-31T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO basket_policy_training_label VALUES(?,?,?,?,?,?,?)",
        (
            "label-1",
            fact_id,
            0,
            "38°C",
            "2026-07-31T12:00:00Z",
            "2026-07-31T12:01:00Z",
            "FORMAL_NATIVE_ELIGIBLE",
        ),
    )
    stations = [row["station_id"] for row in build_bundle()["activations"]]
    receipt = build_exact_stage_scorecard(
        conn,
        as_of_utc="2026-08-01T00:00:00Z",
        station_ids=stations,
        bundle_paths=_bundles(tmp_path),
    )

    p1 = receipt["stages"]["P1"]
    assert p1["forward_outcome_materialization_state_counts"] == {"UNAVAILABLE": 1}
    for family, reason in UNAVAILABLE_REASONS.items():
        materialization = p1["family_outcome_materialization"][family]
        assert materialization["frozen_labeled_fact_count"] == 1
        assert materialization["unavailable_frozen_outcome_materialization_count"] == 1
        assert materialization["unavailable_reasons"] == {reason: 1}
        assert p1["families"][family]["metric_contract_status"] == METRIC_CONTRACT_STATE
        assert p1["families"][family]["row_count"] == 0
        assert p1["families"][family]["promotion_permitted"] is False
    rendered = json.dumps(receipt, sort_keys=True)
    forbidden_legacy_fields = (
        f"candidate_{'score'}", f"baseline_{'score'}",
        f"bucket_{'probabilities'}", f"station_policy_family_{'outcomes'}",
    )
    assert all(field not in rendered for field in forbidden_legacy_fields)
    assert receipt["checks"]["promotion_permitted"] is False


def test_exact_stage_scorecard_materializes_45_station_evidence_gap_rows_without_inferring_fills(
    tmp_path: Path,
) -> None:
    conn = _conn()
    for column in (
        "ws_snapshot_id TEXT",
        "quote_sha256 TEXT",
        "inventory_snapshot_id TEXT",
        "order_state_snapshot_id TEXT",
        "decision_id TEXT",
        "plan_sha256 TEXT",
        "intent_set_sha256 TEXT",
    ):
        conn.execute("ALTER TABLE basket_policy_training_fact ADD COLUMN " + column)
    conn.execute("ALTER TABLE basket_policy_training_label ADD COLUMN truth_sha256 TEXT")
    conn.execute("ALTER TABLE basket_policy_training_label ADD COLUMN outcome_json TEXT")
    fact_id = "d2fact_" + "a" * 40
    sha = "a" * 64
    conn.execute(
        "UPDATE basket_policy_training_fact SET "
        "ws_snapshot_id=?,quote_sha256=?,inventory_snapshot_id=?,"
        "order_state_snapshot_id=?,decision_id=?,plan_sha256=?,intent_set_sha256=? "
        "WHERE fact_id=?",
        ("ws-1", sha, "inventory-1", "orders-1", "decision-1", sha, sha, fact_id),
    )
    conn.execute(
        "INSERT INTO basket_policy_training_label "
        "(label_id,fact_id,truth_revision_no,winning_bucket,settlement_closed_at_utc,"
        "labeled_at_utc,authority_scope,truth_sha256,outcome_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "label-utility-1",
            fact_id,
            0,
            "38°C",
            "2026-07-31T12:00:00Z",
            "2026-07-31T12:01:00Z",
            "FORMAL_NATIVE_ELIGIBLE",
            sha,
            "{}",
        ),
    )
    stations = [row["station_id"] for row in build_bundle()["activations"]]
    receipt = build_exact_stage_scorecard(
        conn,
        as_of_utc="2026-08-01T00:00:00Z",
        station_ids=stations,
        bundle_paths=_bundles(tmp_path),
    )

    p0 = receipt["stages"]["P0"]
    assert len(p0["station_reports"]) == 45
    rkpk = next(row for row in p0["station_reports"] if row["station_id"] == "RKPK")
    assert rkpk["frozen_fact_count"] == 1
    assert rkpk["frozen_labeled_fact_count"] == 1
    assert rkpk["decision_evidence_reference_counts"]["official_clob_ws_snapshot"] == 1
    assert rkpk["decision_evidence_reference_state"]["official_clob_ws_quote"] == "REFERENCE_PRESENT"
    assert rkpk["decision_evidence_reference_state"]["confirmed_yes_fill_receipt"] == "BINDING_UNBOUND"
    assert rkpk["families"]["target"]["utility_evidence_contract"]["metric_state"] == METRIC_CONTRACT_STATE
    assert rkpk["families"]["target"]["promotion_permitted"] is False
    assert p0["family_blocker_type_counts"]["target"]["station_policy_utility_evidence_binding_unbound"] == 45
    assert p0["frozen_settlement_reference_counts"] == {
        "official_settlement_outcome": 1,
        "official_settlement_truth": 1,
    }


def test_exact_stage_scorecard_keeps_sparse_evidence_column_names_aligned(
    tmp_path: Path,
) -> None:
    """The sorted SQL projection must use the same positional field order."""

    conn = _conn()
    conn.execute(
        "ALTER TABLE basket_policy_training_fact ADD COLUMN ws_snapshot_id TEXT"
    )
    conn.execute(
        "ALTER TABLE basket_policy_training_fact ADD COLUMN quote_sha256 TEXT"
    )
    conn.execute(
        "ALTER TABLE basket_policy_training_label ADD COLUMN truth_sha256 TEXT"
    )
    conn.execute(
        "ALTER TABLE basket_policy_training_label ADD COLUMN outcome_json TEXT"
    )
    fact_id = "d2fact_" + "a" * 40
    truth_sha = "c" * 64
    conn.execute(
        "UPDATE basket_policy_training_fact SET quote_sha256=? WHERE fact_id=?",
        ("b" * 64, fact_id),
    )
    conn.execute(
        "INSERT INTO basket_policy_training_label "
        "(label_id,fact_id,truth_revision_no,winning_bucket,"
        "settlement_closed_at_utc,labeled_at_utc,authority_scope,truth_sha256) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            "label-sparse-evidence-1",
            fact_id,
            0,
            "38°C",
            "2026-07-31T12:00:00Z",
            "2026-07-31T12:01:00Z",
            "FORMAL_NATIVE_ELIGIBLE",
            truth_sha,
        ),
    )
    stations = [row["station_id"] for row in build_bundle()["activations"]]
    receipt = build_exact_stage_scorecard(
        conn,
        as_of_utc="2026-08-01T00:00:00Z",
        station_ids=stations,
        bundle_paths=_bundles(tmp_path),
    )

    p0 = receipt["stages"]["P0"]
    assert p0["decision_evidence_reference_counts"] == {
        "decision": 0,
        "inventory": 0,
        "official_clob_ws_quote": 1,
        "official_clob_ws_snapshot": 0,
        "order_state": 0,
        "plan": 0,
        "yes_intent_set": 0,
    }
    assert p0["frozen_settlement_reference_counts"] == {
        "official_settlement_outcome": 0,
        "official_settlement_truth": 1,
    }


def test_exact_stage_scorecard_audits_clob_and_empty_order_events_without_calling_them_utility(
    tmp_path: Path,
) -> None:
    conn = _conn()
    conn.executescript(
        """
        CREATE TABLE basket_official_ws_snapshot(
          snapshot_id TEXT, station_id TEXT, event_date TEXT,
          observed_at TEXT, quote_source TEXT
        );
        CREATE TABLE basket_target_order_events(
          event_id TEXT, order_intent_id TEXT, event_type TEXT,
          observed_at TEXT
        );
        INSERT INTO basket_official_ws_snapshot VALUES(
          'ws-1','RKPK','2026-07-31','2026-07-31T00:00:00Z','OFFICIAL_CLOB_WS'
        );
        """
    )
    stations = [row["station_id"] for row in build_bundle()["activations"]]
    receipt = build_exact_stage_scorecard(
        conn,
        as_of_utc="2026-08-01T00:00:00Z",
        station_ids=stations,
        bundle_paths=_bundles(tmp_path),
    )

    inventory = receipt["utility_evidence_inventory"]
    assert inventory["official_clob_ws_snapshots"] == {
        "state": "PRESENT_UNBOUND",
        "record_count": 1,
        "bounded": True,
        "blocker": None,
    }
    assert inventory["order_events"]["state"] == "EMPTY"
    assert inventory["order_events"]["record_count"] == 0
    assert inventory["confirmed_yes_fill_receipt"] == {
        "state": "BINDING_UNBOUND",
        "inference_from_zero_events_forbidden": True,
        "source_execution_receipt_count": 0,
        "non_simulated_receipt_count": 0,
        "authoritative_fill_candidate_count": 0,
        "direct_fact_to_receipt_link": "MISSING",
    }
    assert "confirmed_yes_fill_receipt_records_absent" in receipt["blockers"]
    assert "confirmed_yes_fill_receipt_direct_binding_unbound" in (
        receipt["stages"]["P1"]["families"]["reserve"]["blockers"]
    )


def test_execution_receipts_are_counted_as_simulated_candidates_without_fill_binding(
    tmp_path: Path,
) -> None:
    conn = _conn()
    conn.executescript(
        """
        CREATE TABLE basket_target_execution_receipts(
          receipt_id TEXT, order_intent_id TEXT, cycle_id TEXT,
          receipt_type TEXT, authoritative INTEGER, execution_mode TEXT,
          simulated INTEGER, observed_at_utc TEXT, matched_size REAL,
          matched_notional_usdc REAL
        );
        INSERT INTO basket_target_execution_receipts VALUES(
          'receipt-1','intent-1','cycle-1','FOK_FAILED',0,
          'production_no_live',1,'2026-07-31T00:00:00Z',0.0,0.0
        );
        CREATE TABLE basket_target_order_intents(
          order_intent_id TEXT, decision_id TEXT, cycle_id TEXT,
          bucket TEXT, side TEXT, order_action TEXT, state TEXT,
          live_submit_authorized INTEGER, created_at TEXT
        );
        INSERT INTO basket_target_order_intents VALUES(
          'intent-1','decision-1','cycle-1','38°C','YES','BUY',
          'cancelled',0,'2026-07-31T00:00:00Z'
        );
        CREATE TABLE basket_target_execution_cycles(
          cycle_id TEXT, station_id TEXT, event_date TEXT,
          settlement_timezone TEXT, strategy_lane TEXT, state TEXT,
          created_at TEXT
        );
        INSERT INTO basket_target_execution_cycles VALUES(
          'cycle-1','RKPK','2026-07-31','Asia/Seoul','basket',
          'prepared','2026-07-31T00:00:00Z'
        );
        """
    )
    stations = [row["station_id"] for row in build_bundle()["activations"]]
    receipt = build_exact_stage_scorecard(
        conn,
        as_of_utc="2026-08-01T00:00:00Z",
        station_ids=stations,
        bundle_paths=_bundles(tmp_path),
    )

    inventory = receipt["utility_evidence_inventory"]
    assert inventory["candidate_execution_receipts"] == {
        "state": "PRESENT_UNBOUND_SIMULATED_ONLY",
        "record_count": 1,
        "bounded": True,
        "blocker": None,
        "simulated_receipt_count": 1,
        "non_simulated_receipt_count": 0,
        "authoritative_receipt_count": 0,
        "authoritative_fill_candidate_count": 0,
        "direct_fill_binding": "UNBOUND",
        "receipt_to_policy_fact_binding": "MISSING",
        "candidate_only_not_direct_fill_evidence": True,
    }
    assert inventory["candidate_order_intents"]["record_count"] == 1
    assert inventory["candidate_order_intents"]["candidate_only_not_fill_evidence"] is True
    assert inventory["candidate_execution_cycles"]["record_count"] == 1
    assert inventory["candidate_execution_cycles"]["candidate_only_not_fill_evidence"] is True
    assert "confirmed_yes_fill_receipt_records_absent" not in receipt["blockers"]
    assert "confirmed_yes_fill_real_receipt_records_absent" in receipt["blockers"]
    assert "confirmed_yes_fill_candidate_records_absent" in receipt["blockers"]
    assert "confirmed_yes_fill_receipt_direct_binding_unbound" in receipt["blockers"]


def test_non_simulated_authoritative_fill_candidate_is_still_unbound_without_fact_receipt_join(
    tmp_path: Path,
) -> None:
    conn = _conn()
    conn.executescript(
        """
        CREATE TABLE basket_target_execution_receipts(
          receipt_id TEXT, order_intent_id TEXT, cycle_id TEXT,
          receipt_type TEXT, authoritative INTEGER, execution_mode TEXT,
          simulated INTEGER, observed_at_utc TEXT, matched_size REAL,
          matched_notional_usdc REAL
        );
        INSERT INTO basket_target_execution_receipts VALUES(
          'receipt-real-candidate','intent-1','cycle-1','MAKER_PARTIAL_FILL',1,
          'external_receipt',0,'2026-07-31T00:00:00Z',2.0,0.8
        );
        """
    )
    stations = [row["station_id"] for row in build_bundle()["activations"]]
    receipt = build_exact_stage_scorecard(
        conn,
        as_of_utc="2026-08-01T00:00:00Z",
        station_ids=stations,
        bundle_paths=_bundles(tmp_path),
    )

    inventory = receipt["utility_evidence_inventory"]
    execution = inventory["candidate_execution_receipts"]
    assert execution["state"] == "PRESENT_UNBOUND_REAL_FILL_CANDIDATES"
    assert execution["non_simulated_receipt_count"] == 1
    assert execution["authoritative_fill_candidate_count"] == 1
    assert execution["receipt_to_policy_fact_binding"] == "MISSING"
    assert inventory["confirmed_yes_fill_receipt"] == {
        "state": "BINDING_UNBOUND",
        "inference_from_zero_events_forbidden": True,
        "source_execution_receipt_count": 1,
        "non_simulated_receipt_count": 1,
        "authoritative_fill_candidate_count": 1,
        "direct_fact_to_receipt_link": "MISSING",
    }
    assert "confirmed_yes_fill_candidate_records_absent" not in receipt["blockers"]
    assert "confirmed_yes_fill_receipt_direct_binding_unbound" in receipt["blockers"]
    assert receipt["checks"]["promotion_permitted"] is False


def test_exact_stage_scorecard_validates_closed_awc_receipt_binding_but_never_scores(
    tmp_path: Path,
) -> None:
    """A complete raw binding is auditable, not a proxy utility score."""

    conn = _conn()
    for column in (
        "content_sha256 TEXT",
        "plan_sha256 TEXT",
        "inventory_sha256 TEXT",
        "ws_snapshot_id TEXT",
        "authority_scope TEXT",
    ):
        conn.execute("ALTER TABLE basket_policy_training_fact ADD COLUMN " + column)
    for column in (
        "truth_id TEXT",
        "truth_sha256 TEXT",
        "outcome_json TEXT",
        "content_sha256 TEXT",
    ):
        conn.execute("ALTER TABLE basket_policy_training_label ADD COLUMN " + column)
    conn.executescript(
        """
        CREATE TABLE calibration_station_daily_high_truth(
          truth_id TEXT, station_id TEXT, local_date TEXT, source_lane TEXT,
          qc_status TEXT, authority_scope TEXT, content_sha256 TEXT
        );
        CREATE TABLE basket_official_ws_snapshot(
          snapshot_id TEXT, content_sha256 TEXT, quote_source TEXT, state TEXT,
          quote_observed_at TEXT, quote_expires_at TEXT
        );
        """
    )
    sha = lambda char: char * 64
    outcome = {"winner_bucket": "38°C"}
    outcome_sha = content_sha256(outcome)
    fact_id = "d2fact_" + "a" * 40
    binding = {
        "family": "target",
        "evidence_role": "historical_strict_oos",
        "utility_contract_pointer": build_future_station_policy_outcome_materialization(
            station_id="RKPK",
            event_date="2026-07-31",
            settlement_timezone="Asia/Seoul",
            stage="P0",
            decision_at_utc="2026-07-31T00:00:00Z",
            strategy_version="test",
            season_bin="summer",
            maturity_bin="d1",
            liquidity_bin="liquid",
        )["utility_contract_pointer"],
        "scope": {
            "station_id": "RKPK",
            "event_date": "2026-07-31",
            "settlement_timezone": "Asia/Seoul",
            "stage": "P0",
            "decision_at_utc": "2026-07-31T00:00:00Z",
            "strategy_version": "test",
            "season_bin": "summer",
            "maturity_bin": "d1",
            "liquidity_bin": "liquid",
        },
        "candidate_vs_baseline": {
            "candidate_decision_fact_sha256": sha("1"),
            "baseline_decision_fact_sha256": sha("2"),
            "candidate_plan_sha256": sha("3"),
            "baseline_plan_sha256": sha("4"),
            "candidate_inventory_vector_sha256": sha("5"),
            "baseline_inventory_vector_sha256": sha("6"),
            "candidate_support_bucket_ids": ["38°C"],
            "baseline_support_bucket_ids": ["38°C"],
        },
        "candidate_inventory": {
            "plan_sha256": sha("3"),
            "inventory_vector_sha256": sha("5"),
            "support_bucket_ids": ["38°C"],
            "target_quantity_by_bucket": {"38°C": 1.0},
        },
        "baseline_inventory": {
            "plan_sha256": sha("4"),
            "inventory_vector_sha256": sha("6"),
            "support_bucket_ids": ["38°C"],
            "target_quantity_by_bucket": {"38°C": 1.0},
        },
        "official_clob_ws": {
            "candidate_ws_snapshot_sha256": sha("7"),
            "baseline_ws_snapshot_sha256": sha("8"),
            "candidate_quote_sequence": 1,
            "baseline_quote_sequence": 2,
            "candidate_all_in_yes_ask_by_bucket": {"38°C": 0.42},
            "baseline_all_in_yes_ask_by_bucket": {"38°C": 0.43},
            "quote_source": "OFFICIAL_CLOB_WS",
            "quote_observed_at_utc": "2026-07-30T23:59:00Z",
            "quote_expires_at_utc": "2026-07-31T00:01:00Z",
        },
        "execution": {
            "candidate_fill_receipt_bindings": [{
                "receipt_id": "candidate-zero",
                "order_intent_id": "candidate-intent",
                "bucket": "38°C",
                "receipt_type": "CANCEL_CONFIRMED",
                "authoritative": True,
                "simulated": False,
                "matched_size": 0.0,
                "matched_notional_usdc": 0.0,
                "observed_at_utc": "2026-07-31T00:00:10Z",
                "receipt_content_sha256": sha("a"),
                "official_ws_snapshot_sha256": sha("7"),
            }],
            "baseline_fill_receipt_bindings": [{
                "receipt_id": "baseline-zero",
                "order_intent_id": "baseline-intent",
                "bucket": "38°C",
                "receipt_type": "CANCEL_CONFIRMED",
                "authoritative": True,
                "simulated": False,
                "matched_size": 0.0,
                "matched_notional_usdc": 0.0,
                "observed_at_utc": "2026-07-31T00:00:11Z",
                "receipt_content_sha256": sha("b"),
                "official_ws_snapshot_sha256": sha("8"),
            }],
            "candidate_fill_receipt_set_sha256": content_sha256([sha("a")]),
            "baseline_fill_receipt_set_sha256": content_sha256([sha("b")]),
            "candidate_confirmed_filled_qty_by_bucket": {"38°C": 0.0},
            "baseline_confirmed_filled_qty_by_bucket": {"38°C": 0.0},
            "candidate_confirmed_filled_notional_usdc_by_bucket": {"38°C": 0.0},
            "baseline_confirmed_filled_notional_usdc_by_bucket": {"38°C": 0.0},
            "candidate_fill_outcome_by_bucket": {"38°C": "CONFIRMED_ZERO_FILL"},
            "baseline_fill_outcome_by_bucket": {"38°C": "CONFIRMED_ZERO_FILL"},
        },
        "settlement": {
            "settlement_truth_sha256": sha("9"),
            "settlement_outcome_sha256": outcome_sha,
            "winner_bucket": "38°C",
            "settlement_closed_at_utc": "2026-07-31T15:00:00Z",
            "settlement_authority": "OFFICIAL_CLOSED_SETTLEMENT_TRUTH",
        },
        "timing": {},
    }
    conn.execute(
        "UPDATE basket_policy_training_fact SET content_sha256=?,plan_sha256=?,"
        "inventory_sha256=?,ws_snapshot_id=?,authority_scope=?,feature_json=? WHERE fact_id=?",
        (sha("1"), sha("3"), sha("5"), "ws-candidate", "HISTORICAL_STRICT_OOS", json.dumps({
            "station_policy_historical_utility_bindings_v1": {"target": binding}
        }), fact_id),
    )
    conn.execute(
        "INSERT INTO basket_policy_training_label "
        "(label_id,fact_id,truth_revision_no,winning_bucket,settlement_closed_at_utc,"
        "labeled_at_utc,authority_scope,truth_id,truth_sha256,outcome_json,content_sha256) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "label-1", fact_id, 0, "38°C", "2026-07-31T15:00:00Z",
            "2026-07-31T15:01:00Z", "FORMAL_NATIVE_ELIGIBLE", "truth-1",
            sha("9"), json.dumps(outcome), sha("c"),
        ),
    )
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?)",
        ("truth-1", "RKPK", "2026-07-31", "AWC", "ACCEPTED",
         "FORMAL_NATIVE_ELIGIBLE", sha("d")),
    )
    conn.execute(
        "INSERT INTO basket_official_ws_snapshot VALUES(?,?,?,?,?,?)",
        ("ws-candidate", sha("7"), "OFFICIAL_CLOB_WS", "COMMITTED",
         "2026-07-30T23:59:00Z", "2026-07-31T00:01:00Z"),
    )
    stations = [row["station_id"] for row in build_bundle()["activations"]]
    receipt = build_exact_stage_scorecard(
        conn,
        as_of_utc="2026-08-01T00:00:00Z",
        station_ids=stations,
        bundle_paths=_bundles(tmp_path),
    )
    inventory = receipt["historical_utility_binding_inventory"]
    target = inventory["stages"]["P0"]["families"]["target"]
    assert target["declared_binding_count"] == 1
    assert target["validated_raw_binding_count"] == 1
    assert target["score_row_count"] == 0
    assert inventory["score_rows_emitted"] == 0
    assert receipt["checks"]["policy_utility_score_rows_emitted"] is False
