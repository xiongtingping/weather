from __future__ import annotations

import json
import sqlite3

from bayes_basket_target.probability_evidence_store import (
    apply_probability_evidence_schema,
)
from scripts.bayes_basket_target_production_readiness import (
    _probability_evidence_runtime_status,
)


SHA = "a" * 64


def test_probability_evidence_readiness_distinguishes_missing_and_empty_schema(
) -> None:
    with sqlite3.connect(":memory:") as conn:
        missing = _probability_evidence_runtime_status(conn)
        assert missing["state"] == "SCHEMA_MISSING"
        assert len(missing["missing_tables"]) == 4

        apply_probability_evidence_schema(conn)
        empty = _probability_evidence_runtime_status(conn)
        assert empty["state"] == "SCHEMA_READY_AWAITING_RESIDENT_PROOF"
        assert empty["p0_historical_state"] == "NO_FORMAL_ARTIFACT"
        assert empty["p1_trajectory_state"] == "NO_FORMAL_ARTIFACT"


def test_probability_evidence_readiness_counts_candidate_activation_globally() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_probability_evidence_schema(conn)
        conn.execute(
            "INSERT INTO basket_probability_evidence_artifact VALUES("
            "?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "candidate-zuuu",
                "METAR_TRAJECTORY_LIKELIHOOD",
                "ZUUU",
                "Asia/Shanghai",
                "awc|JJA|MORNING|D0|official_daily_tmax_c",
                "2026-07-29T00:00:00Z",
                "2026-07-30T00:00:00Z",
                "2026-08-30T00:00:00Z",
                0,
                "PRODUCTION_NO_LIVE_CANDIDATE",
                "{}",
                "b" * 64,
            ),
        )
        conn.execute(
            "INSERT INTO basket_probability_evidence_activation VALUES("
            "?,?,?,?,?,?,?,?,?,?,?)",
            (
                "candidate-zuuu-activation",
                "METAR_TRAJECTORY_LIKELIHOOD",
                "candidate-zuuu",
                "ZUUU",
                "awc|JJA|MORNING|D0|official_daily_tmax_c",
                1,
                "2026-07-29T00:00:00Z",
                "2026-07-30T00:00:00Z",
                "2026-07-29T00:00:00Z",
                "PRODUCTION_NO_LIVE_CANDIDATE",
                "c" * 64,
            ),
        )

        status = _probability_evidence_runtime_status(
            conn,
            station_ids=("RKPK",),
        )

        assert status["candidate_activation_count"] == 0
        assert status["candidate_activation_count_all"] == 1


def test_probability_evidence_readiness_reports_governed_fallback_proof() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_probability_evidence_schema(conn)
        proof = {
            "positive_core_buckets": ["38°C", "39°C"],
            "applied_strength": 0.0,
            "likelihood_ratio_by_bucket": {
                "38°C": 1.0,
                "39°C": 1.0,
            },
        }
        conn.execute(
            "INSERT INTO basket_probability_update_proof("
            "proof_id,proof_kind,station_id,event_date,"
            "settlement_timezone,prediction_root_checkpoint_id,"
            "artifact_id_or_none,activation_id_or_none,"
            "canonical_sequence_sha256_or_none,decision_at_utc,"
            "proof_json,content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "proof-fallback",
                "METAR_TRAJECTORY_UPDATE",
                "RKPK",
                "2026-07-30",
                "Asia/Seoul",
                "p0-root",
                "NONE",
                "NONE",
                SHA,
                "2026-07-30T01:00:00Z",
                json.dumps(proof, sort_keys=True),
                SHA,
            ),
        )

        status = _probability_evidence_runtime_status(conn)

        assert status["state"] == "RUNNING_WITH_GOVERNED_FALLBACKS"
        assert status["p1_trajectory_state"] == (
            "IDENTITY_LIKELIHOOD_FALLBACK_OBSERVED"
        )
        assert status["formal_consumption_counts"] == {}
        assert status["fallback_proof_counts"] == {
            "METAR_TRAJECTORY_UPDATE": 1
        }
        assert status["continuous_core_proof_count"] == 1
        assert status["fallback_contract_failures"] == []


def test_probability_evidence_readiness_requires_activation_and_consumption(
) -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_probability_evidence_schema(conn)
        conn.execute(
            "INSERT INTO basket_probability_evidence_artifact VALUES("
            "?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "artifact-p1",
                "METAR_TRAJECTORY_LIKELIHOOD",
                "RKPK",
                "Asia/Seoul",
                "awc|summer|MORNING|D0|daily_high_c",
                "2026-07-29T00:00:00Z",
                "2026-07-30T00:00:00Z",
                "2026-08-30T00:00:00Z",
                1,
                "FORMAL_PROMOTED",
                "{}",
                "b" * 64,
            ),
        )
        conn.execute(
            "INSERT INTO basket_probability_evidence_promotion VALUES("
            "?,?,?,?,?,?,?,?,?,?)",
            (
                "promotion-p1",
                "METAR_TRAJECTORY_LIKELIHOOD",
                "artifact-p1",
                1,
                "PROMOTED",
                "gate-v1",
                SHA,
                "2026-07-30T00:00:00Z",
                SHA,
                "c" * 64,
            ),
        )
        conn.execute(
            "INSERT INTO basket_probability_evidence_activation VALUES("
            "?,?,?,?,?,?,?,?,?,?,?)",
            (
                "activation-p1",
                "METAR_TRAJECTORY_LIKELIHOOD",
                "artifact-p1",
                "RKPK",
                "awc|summer|MORNING|D0|daily_high_c",
                1,
                "2026-07-30T00:00:00Z",
                "2026-08-30T00:00:00Z",
                "2026-07-30T00:00:00Z",
                "FORMAL_PROMOTED",
                "d" * 64,
            ),
        )
        awaiting = _probability_evidence_runtime_status(conn)
        assert awaiting["p1_trajectory_state"] == (
            "FORMAL_ACTIVE_AWAITING_RESIDENT_CONSUMPTION"
        )

        conn.execute(
            "INSERT INTO basket_probability_update_proof VALUES("
            "?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "proof-formal",
                "METAR_TRAJECTORY_UPDATE",
                "RKPK",
                "2026-07-30",
                "Asia/Seoul",
                "p0-root",
                "artifact-p1",
                "activation-p1",
                SHA,
                "2026-07-30T01:00:00Z",
                json.dumps(
                    {"positive_core_buckets": ["39°C"]},
                    sort_keys=True,
                ),
                "e" * 64,
            ),
        )
        consumed = _probability_evidence_runtime_status(conn)
        assert consumed["p1_trajectory_state"] == (
            "FORMAL_ACTIVE_AND_CONSUMED"
        )
        assert consumed["formal_consumption_counts"] == {
            "METAR_TRAJECTORY_UPDATE": 1
        }


def test_readiness_scopes_candidate_and_direction_consumption_to_station(
) -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_probability_evidence_schema(conn)
        conn.execute(
            "CREATE TABLE auto_trade_decisions("
            "station_id TEXT NOT NULL,strategy_lane TEXT NOT NULL,"
            "details_json TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO basket_probability_evidence_artifact VALUES("
            "?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "candidate-rkpk",
                "METAR_TRAJECTORY_LIKELIHOOD",
                "RKPK",
                "Asia/Seoul",
                "awc|JJA|MORNING|D0|official_daily_tmax_c",
                "2026-07-29T00:00:00Z",
                "2026-07-30T00:00:00Z",
                "2026-08-30T00:00:00Z",
                0,
                "PRODUCTION_NO_LIVE_CANDIDATE",
                "{}",
                "b" * 64,
            ),
        )
        conn.execute(
            "INSERT INTO basket_probability_evidence_promotion VALUES("
            "?,?,?,?,?,?,?,?,?,?)",
            (
                "candidate-rkpk-rejected",
                "METAR_TRAJECTORY_LIKELIHOOD",
                "candidate-rkpk",
                1,
                "REJECTED",
                "gate-v1",
                SHA,
                "2026-07-30T00:00:00Z",
                SHA,
                "c" * 64,
            ),
        )
        conn.execute(
            "INSERT INTO basket_probability_update_proof VALUES("
            "?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "cross-station-proof",
                "METAR_TRAJECTORY_UPDATE",
                "ZUUU",
                "2026-07-30",
                "Asia/Shanghai",
                "p0-root",
                "NONE",
                "NONE",
                SHA,
                "2026-07-30T01:00:00Z",
                json.dumps({"positive_core_buckets": ["38°C"]}),
                "d" * 64,
            ),
        )
        conn.execute(
            "INSERT INTO auto_trade_decisions VALUES(?,?,?)",
            (
                "RKPK",
                "basket",
                json.dumps({
                    "core_buckets": ["38°C", "39°C"],
                    "direction": "UP",
                    "current_direction": "NEUTRAL",
                    "core_transition": {
                        "transition": "FILL_GAP",
                        "direction_transition": "MIGRATE",
                    },
                }),
            ),
        )

        status = _probability_evidence_runtime_status(
            conn,
            station_ids=("RKPK",),
        )

        assert status["candidate_artifact_counts"] == {
            "METAR_TRAJECTORY_LIKELIHOOD": 1
        }
        assert status["candidate_rejected_count"] == 1
        assert status["candidate_activation_count"] == 0
        assert status["proof_counts"] == {}
        assert status["continuous_core_decision_count"] == 1
        assert status["direction_transition_decision_count"] == 1
        assert status["legacy_core_decision_count"] == 0

        conn.execute(
            "INSERT INTO basket_probability_evidence_activation VALUES("
            "?,?,?,?,?,?,?,?,?,?,?)",
            (
                "candidate-activation-forbidden",
                "METAR_TRAJECTORY_LIKELIHOOD",
                "candidate-rkpk",
                "RKPK",
                "awc|JJA|MORNING|D0|official_daily_tmax_c",
                1,
                "2026-07-30T00:00:00Z",
                "2026-08-30T00:00:00Z",
                "2026-07-30T00:00:00Z",
                "PRODUCTION_NO_LIVE_CANDIDATE",
                "e" * 64,
            ),
        )
        blocked = _probability_evidence_runtime_status(
            conn,
            station_ids=("RKPK",),
        )
        assert blocked["state"] == "BLOCKED_CANDIDATE_ACTIVATION"
        assert blocked["candidate_activation_count"] == 1
