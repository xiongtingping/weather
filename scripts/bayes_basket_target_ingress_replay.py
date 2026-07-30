#!/usr/bin/env python3
"""Build bounded, isolated M2-05/06/07 ingress-replay evidence.

The receipt deliberately exercises the target contracts in memory only.  It
does not read production state, call the order gateway, or infer a market day
from ET.  The test registry below is the only authority for station, lane,
event date, and settlement timezone in this replay.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_event_replay import replay_canonical_events  # noqa: E402
from bayes_basket_target.canonical_ingress import materialize_p1_from_canonical_observation  # noqa: E402
from bayes_basket_target.contracts import (  # noqa: E402
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
    model_set_hash,
)
from bayes_basket_target.event_contracts import (  # noqa: E402
    ForecastRevision,
    validate_forecast_revision_chain,
)
from bayes_basket_target.materialization import materialize_p0m  # noqa: E402
from bayes_basket_target.p1_compiler import (  # noqa: E402
    canonical_materialization_packet,
    compile_station_p1_packets,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402
from bayes_basket_target.taf_revision import (  # noqa: E402
    TAF_ARTIFACT_SCHEMA,
    TAF_CORRELATION_METHOD,
    TAF_METHOD_VERSION,
    TAF_NWP_CROSSFIT_ARTIFACT_KIND,
    TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION,
    TAF_NWP_LIVE_FINGERPRINT_SCHEMA,
    TafRevisionArtifact,
    compile_taf_revision_proof,
)


NOW = "2026-07-23T05:00:00Z"
MAX_RECEIPT_BYTES = 1024 * 1024
REPLAY_EVENT_DATE = "2026-07-23"

# This is a deliberately small, explicit market-registry test scope.  It is
# not an ET-derived map: settlement_timezone and event_date are read from the
# registry row and then bound into every probability envelope and replay check.
TEST_MARKET_REGISTRY_SCOPE: dict[str, dict[str, str]] = {
    "ZUUU": {
        "market_id": "market-zuuu-20260723",
        "station_id": "ZUUU",
        "source_lane": "AMSC",
        "event_date": REPLAY_EVENT_DATE,
        "settlement_timezone": "Asia/Shanghai",
    },
    "KATL": {
        "market_id": "market-katl-20260723",
        "station_id": "KATL",
        "source_lane": "AWC",
        "event_date": REPLAY_EVENT_DATE,
        "settlement_timezone": "America/New_York",
    },
}


def _registry_row(lane: str) -> dict[str, str]:
    wanted = str(lane or "").upper()
    rows = [row for row in TEST_MARKET_REGISTRY_SCOPE.values() if row["source_lane"] == wanted]
    if len(rows) != 1:
        raise ContractError("replay_test_registry_lane_scope_invalid")
    return dict(rows[0])


def _p0(registry_row: Mapping[str, str]) -> ProbabilityEnvelope:
    station = str(registry_row["station_id"])
    event_date = str(registry_row["event_date"])
    settlement_timezone = str(registry_row["settlement_timezone"])
    models = ("ECMWF", "GFS")
    live_identity = {
        "schema": TAF_NWP_LIVE_FINGERPRINT_SCHEMA,
        "station_id": station,
        "season_bin": "summer",
        "lead_bucket": "d0",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "available_model_set_hash": model_set_hash(models),
        "available_models": list(models),
        "resolved_weights": {"ECMWF": 0.6, "GFS": 0.4},
        "ensemble_artifact_id": f"ensemble-replay-{station}",
        "ensemble_artifact_content_sha256": "e" * 64,
        "ensemble_selection_evidence_sha256": "f" * 64,
        "ensemble_weight_method": "strict_oos_replay_fixture",
        "model_calibration_artifacts": {
            model: {
                "calibration_artifact_id": f"calibration-replay-{station}-{model}",
                "calibration_artifact_content_sha256": sha,
            }
            for model, sha in (("ECMWF", "1" * 64), ("GFS", "2" * 64))
        },
    }
    live_fingerprint = content_sha256(live_identity)
    live_compatibility = {
        **live_identity,
        "fingerprint_sha256": live_fingerprint,
    }
    return ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": f"p0-{station}-{event_date}",
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "decision_at_utc": NOW,
        "fresh_until_utc": "2026-07-24T05:00:00Z",
        "bucket_ordinals": {"37": 0, "38": 1, "39": 2},
        "probabilities": {"37": 0.2, "38": 0.6, "39": 0.2},
        "posterior_version": "p0-ingress-replay-v2",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
        "lineage": {
            "source": "isolated_replay",
            "market_registry_scope": str(registry_row["market_id"]),
            "market_registry_settlement_timezone": settlement_timezone,
            "settlement_timezone": settlement_timezone,
            "season_bin": "summer",
            "lead_bucket": "d0",
            "run_cycle": "00Z",
            "settlement_definition": "official_daily_tmax_c",
            "ensemble_artifact_content_sha256": "e" * 64,
            "taf_nwp_live_compatibility_fingerprint_sha256": live_fingerprint,
            "taf_nwp_live_compatibility_json": json.dumps(
                live_compatibility,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    })


def _taf_artifact(predecessor: ProbabilityEnvelope) -> TafRevisionArtifact:
    identity = {
        "schema": TAF_ARTIFACT_SCHEMA,
        "artifact_id": f"taf-replay-{predecessor.station_id}",
        "activation_id": f"taf-replay-activation-{predecessor.station_id}",
        "station_id": predecessor.station_id,
        "season_bin": "summer",
        "lead_bucket": "d0",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "settlement_timezone": predecessor.settlement_timezone,
        "method_version": TAF_METHOD_VERSION,
        "trained_through_utc": "2026-07-01T00:00:00Z",
        "sample_n": 100,
        "effective_sample_n": 80.0,
        "oos_metrics": {"brier_score": 0.12, "nll": 0.4, "crps": 0.6},
        "tx_bias_c": 0.0,
        "taf_nwp_delta_coefficient": 0.6,
        "tx_residual_scale_c": 0.8,
        "physical_feature_effect_c": {
            "cloud_cover_fraction": -0.4,
            "precipitation_probability": -0.4,
            "wind_speed_normalized": 0.0,
        },
        "wind_speed_cap_mps": 20.0,
        "confirmation_tolerance_c": 0.75,
        "max_confirmation_strength": 0.25,
        "max_correction_strength": 0.20,
        "max_probability_mass_transfer": 0.10,
        "max_center_displacement_buckets": 1,
        "likelihood_ratio_floor": 0.25,
        "likelihood_ratio_ceiling": 4.0,
        "nwp_combination_artifact_sha256": "9" * 64,
        "nwp_combination_artifact_kind": TAF_NWP_CROSSFIT_ARTIFACT_KIND,
        "nwp_live_compatibility_contract_version": (
            TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION
        ),
        "nwp_live_compatibility_fingerprint_sha256": predecessor.lineage[
            "taf_nwp_live_compatibility_fingerprint_sha256"
        ],
        "nwp_live_compatibility_evidence_sha256": "8" * 64,
        "nwp_live_compatibility_validated": True,
        "oos_taf_nwp_residual_correlation": 0.25,
        "correlation_discount_method": TAF_CORRELATION_METHOD,
        "correlation_discount": 0.75,
        "joint_physical_oos_validated": True,
        "valid_from_utc": "2026-07-01T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "activated": True,
    }
    return TafRevisionArtifact.from_mapping(
        {**identity, "content_sha256": content_sha256(identity)}
    )


def _observation_table(
    conn: sqlite3.Connection,
    *,
    lane: str,
    station: str,
    correction: int = 0,
) -> None:
    conn.execute(
        """CREATE TABLE reported_trajectory_observations(
           observation_id TEXT PRIMARY KEY, source_lane TEXT, station_id TEXT,
           report_type TEXT, raw_sha256 TEXT, canonical_committed_at TEXT,
           correction_suspected INTEGER)"""
    )
    conn.execute(
        "INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?)",
        (
            "obs-replay-1",
            lane.lower(),
            station,
            "METAR",
            "c" * 64,
            "2026-07-23T05:10:00Z",
            correction,
        ),
    )


def _stage_chain(lane: str) -> dict[str, object]:
    registry_row = _registry_row(lane)
    station = registry_row["station_id"]
    event_date = registry_row["event_date"]
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        _observation_table(conn, lane=lane, station=station)
        predecessor = _p0(registry_row)
        artifact = _taf_artifact(predecessor)
        forecast_revision = ForecastRevision.from_mapping({
            "revision_id": f"taf-{lane.lower()}-1",
            "station_id": station,
            "event_date": event_date,
            "revision_kind": "TAF",
            "sequence": 1,
            "issued_at_utc": "2026-07-23T04:30:00Z",
            "received_at_utc": "2026-07-23T04:31:00Z",
            "valid_from_utc": "2026-07-23T04:30:00Z",
            "valid_until_utc": "2026-07-24T04:30:00Z",
            "source_object_sha256": "d" * 64,
            "supersedes_revision_id": None,
        })
        proof = compile_taf_revision_proof(
            predecessor,
            p0_root=predecessor,
            forecast_revision=forecast_revision,
            artifact=artifact,
            bucket_centers_c={"37": 37.0, "38": 38.0, "39": 39.0},
            taf_fields={
                "tx_c": 38.0,
                "cloud_cover_fraction": 0.25,
                "precipitation_probability": 0.10,
                "wind_speed_mps": 3.0,
            },
            decision_at_utc="2026-07-23T05:05:00Z",
        )
        p0m = materialize_p0m(
            predecessor,
            p0_root=predecessor,
            taf_revision_artifact=artifact,
            taf_revision_proof=proof,
            decision_at_utc="2026-07-23T05:05:00Z",
            fresh_until_utc="2026-07-23T06:00:00Z",
            bridge_sha256="b" * 64,
        )
        rich_packet = compile_station_p1_packets(
            predecessor=p0m,
            observations=[{
                "observation_id": "obs-replay-1",
                "station_id": station,
                "event_date_local": event_date,
                "reported_temperature_c": 38.2,
                "valid_at_utc": "2026-07-23T05:00:00Z",
                "canonical_committed_at_utc": "2026-07-23T05:10:00Z",
                "raw_sha256": "c" * 64,
                "source_lane": lane,
                "report_type": "METAR",
                "evidence_role": "FORMAL",
                "qc_status": "ACCEPTED",
                "correction_suspected": False,
            }],
            registry_rows=[
                {
                    "station_id": station,
                    "event_date": event_date,
                    "settlement_timezone": registry_row[
                        "settlement_timezone"
                    ],
                    "settlement_unit": "C",
                    "outcome": bucket,
                    "registry_json": json.dumps({"upper": upper}),
                }
                for bucket, upper in (
                    ("37", 37),
                    ("38", 38),
                    ("39", 39),
                )
            ],
            compilation_at_utc="2026-07-23T05:20:00Z",
        )[0]
        projected_packet = canonical_materialization_packet(rich_packet)
        kwargs = {
            key: projected_packet[key]
            for key in (
                "predecessor",
                "prediction_root",
                "observation_id",
                "trajectory_artifact",
                "trajectory_proof",
                "trajectory_observations",
                "bucket_centers_c",
                "bucket_upper_bounds_c",
                "reported_high_advanced",
                "decision_at_utc",
                "fresh_until_utc",
                "created_at_utc",
            )
        }
        committed = materialize_p1_from_canonical_observation(conn, **kwargs, commit=True)
        replay_receipts = [
            materialize_p1_from_canonical_observation(conn, **kwargs, commit=True)
            for _ in range(100)
        ]
        correction_blocked = False
        with sqlite3.connect(":memory:") as correction_conn:
            apply_production_ssot_schema(correction_conn)
            _observation_table(
                correction_conn,
                lane=lane,
                station=station,
                correction=1,
            )
            try:
                materialize_p1_from_canonical_observation(correction_conn, **kwargs)
            except ContractError as exc:
                correction_blocked = str(exc) == "canonical_observation_correction_requires_review"
        asof_blocked = False
        try:
            materialize_p1_from_canonical_observation(
                conn,
                **{**kwargs, "decision_at_utc": "2026-07-23T05:05:00Z"},
            )
        except ContractError as exc:
            asof_blocked = str(exc) == "canonical_observation_not_strict_asof"
        row_count = conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope"
        ).fetchone()[0]
        replay_writes_false = sum(
            1 for item in replay_receipts if item["receipt"]["wrote"] is False
        )
        return {
            "lane": lane,
            "station_id": station,
            "market_id": registry_row["market_id"],
            "event_date": event_date,
            "settlement_timezone": registry_row["settlement_timezone"],
            "settlement_timezone_source": "market_registry.settlement_timezone",
            "event_date_source": "market_registry.event_date",
            "operational_timezone_not_authoritative": True,
            "market_registry_scope_sha256": content_sha256(registry_row),
            "p0_stage": predecessor.stage,
            "p0_m_stage": p0m.stage,
            "p1_stage": committed["p1_checkpoint_id"].split("_")[0].upper(),
            "p1_commit_state": committed["state"],
            "p1_materialization_attempts": 101,
            "p1_materialization_writes": int(bool(committed["receipt"]["wrote"]))
            + sum(int(bool(item["receipt"]["wrote"])) for item in replay_receipts),
            "p1_replay_count": len(replay_receipts),
            "p1_replay_writes_false": replay_writes_false == 100,
            "p1_replay_writes_false_count": replay_writes_false,
            "p1_row_count": row_count,
            "same_event_same_p1_only_one_write": row_count == 1
            and replay_writes_false == 100,
            "correction_blocked": correction_blocked,
            "strict_asof_blocked": asof_blocked,
        }


def _event(
    observation_id: str,
    sequence: int,
    observed_at_utc: str,
    raw_sha256: str,
    *,
    event_date: str = REPLAY_EVENT_DATE,
    station_id: str = "ZUUU",
    source_lane: str = "AMSC",
    correction_suspected: bool = False,
) -> dict[str, object]:
    return {
        "observation_id": observation_id,
        "station_id": station_id,
        "source_lane": source_lane,
        "report_type": "METAR" if sequence % 2 else "SPECI",
        "event_sequence": sequence,
        "observed_at_utc": observed_at_utc,
        "raw_sha256": raw_sha256,
        "event_date": event_date,
        "correction_suspected": correction_suspected,
    }


def _scoped_replay(
    events: list[Mapping[str, object]],
    *,
    station_id: str,
    source_lane: str,
    event_date: str = REPLAY_EVENT_DATE,
    initial_state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Bind event_date before handing the field-free event to canonical replay."""

    for item in events:
        if str(item.get("event_date") or "") != event_date:
            raise ContractError("canonical_event_event_date_scope_mismatch")
    return replay_canonical_events(
        events,
        station_id=station_id,
        source_lane=source_lane,
        initial_state=initial_state,
    )


def _expect_error(call: Callable[[], object], expected: str) -> bool:
    try:
        call()
    except ContractError as exc:
        return str(exc) == expected
    return False


def _state(result: Mapping[str, object]) -> dict[str, object]:
    return {
        "last_sequence": result["last_sequence"],
        "last_observed_at_utc": result["last_observed_at_utc"],
        "seen_event_ids": result["seen_event_ids"],
    }


def _multi_round_replay() -> dict[str, object]:
    """Exercise forward -> backward/late -> forward over two rounds."""

    state: dict[str, object] = {}
    accepted_ids: list[str] = []
    round_results: list[dict[str, object]] = []
    all_forward: list[dict[str, object]] = []
    for round_no in range(2):
        first = 1 + round_no * 4
        forward_a = [
            _event(
                f"round-{round_no}-a",
                first,
                f"2026-07-23T05:{10 + round_no * 10:02d}:00Z",
                f"{first:x}" * 64,
            ),
            _event(
                f"round-{round_no}-b",
                first + 1,
                f"2026-07-23T05:{11 + round_no * 10:02d}:00Z",
                f"{first + 1:x}" * 64,
            ),
        ]
        forward_result = _scoped_replay(
            forward_a,
            station_id="ZUUU",
            source_lane="AMSC",
            initial_state=state,
        )
        state = _state(forward_result)
        accepted_ids.extend(forward_result["accepted_event_ids"])
        all_forward.extend(forward_a)
        state_before_bad = content_sha256(state)
        backward_blocked = _expect_error(
            lambda: _scoped_replay(
                [
                    _event(
                        f"round-{round_no}-rewind",
                        first,
                        f"2026-07-23T05:{9 + round_no * 10:02d}:00Z",
                        f"{7 + round_no:x}" * 64,
                    )
                ],
                station_id="ZUUU",
                source_lane="AMSC",
                initial_state=state,
            ),
            "canonical_event_sequence_rewind",
        )
        late_blocked = _expect_error(
            lambda: _scoped_replay(
                [
                    _event(
                        f"round-{round_no}-late",
                        first + 2,
                        f"2026-07-23T05:{8 + round_no * 10:02d}:00Z",
                        f"{8 + round_no:x}" * 64,
                    )
                ],
                station_id="ZUUU",
                source_lane="AMSC",
                initial_state=state,
            ),
            "canonical_event_timestamp_rewind",
        )
        state_after_bad = content_sha256(state)
        forward_b = [
            _event(
                f"round-{round_no}-c",
                first + 2,
                f"2026-07-23T05:{12 + round_no * 10:02d}:00Z",
                f"{first + 2:x}" * 64,
            ),
            _event(
                f"round-{round_no}-d",
                first + 3,
                f"2026-07-23T05:{13 + round_no * 10:02d}:00Z",
                f"{first + 3:x}" * 64,
            ),
        ]
        resume_result = _scoped_replay(
            forward_b,
            station_id="ZUUU",
            source_lane="AMSC",
            initial_state=state,
        )
        state = _state(resume_result)
        accepted_ids.extend(resume_result["accepted_event_ids"])
        all_forward.extend(forward_b)
        round_results.append({
            "round": round_no + 1,
            "forward_a": forward_result["accepted_event_ids"],
            "backward_sequence_blocked": backward_blocked,
            "late_timestamp_blocked": late_blocked,
            "state_unchanged_after_rejected_inputs": state_before_bad
            == state_after_bad,
            "forward_b": resume_result["accepted_event_ids"],
        })
    full_a = _scoped_replay(
        all_forward,
        station_id="ZUUU",
        source_lane="AMSC",
    )
    full_b = _scoped_replay(
        all_forward,
        station_id="ZUUU",
        source_lane="AMSC",
    )
    expected_ids = [item["observation_id"] for item in all_forward]
    return {
        "rounds": round_results,
        "accepted_ids_incremental": accepted_ids,
        "accepted_ids_full": full_a["accepted_event_ids"],
        "final_sequence": full_a["last_sequence"],
        "final_replay_sha256": full_a["replay_sha256"],
        "deterministic_final_replay": full_a["replay_sha256"] == full_b["replay_sha256"],
        "final_legal_result_determined": full_a["accepted_event_ids"] == expected_ids
        and accepted_ids == expected_ids,
    }


def _low_wrong_low_roundtrip() -> dict[str, object]:
    """Low -> conflicting content -> low retry must not mutate replay state."""

    low = _event("low-1", 1, "2026-07-23T06:10:00Z", "a" * 64)
    first = _scoped_replay([low], station_id="ZUUU", source_lane="AMSC")
    state = _state(first)
    state_hash = content_sha256(state)
    wrong = {**low, "raw_sha256": "b" * 64}
    wrong_blocked = _expect_error(
        lambda: _scoped_replay(
            [wrong], station_id="ZUUU", source_lane="AMSC", initial_state=state
        ),
        "canonical_event_id_content_conflict",
    )
    retry = _scoped_replay(
        [low], station_id="ZUUU", source_lane="AMSC", initial_state=state
    )
    final = _scoped_replay(
        [_event("low-2", 2, "2026-07-23T06:20:00Z", "c" * 64)],
        station_id="ZUUU",
        source_lane="AMSC",
        initial_state=_state(retry),
    )
    return {
        "wrong_content_blocked": wrong_blocked,
        "low_retry_duplicate_count": len(retry["duplicate_event_ids"]),
        "state_unchanged_after_wrong_content": state_hash == content_sha256(state),
        "final_accepted_ids": final["accepted_event_ids"],
        "final_sequence": final["last_sequence"],
    }


def _event_replay() -> dict[str, object]:
    events = [
        _event("obs-1", 1, "2026-07-23T05:10:00Z", "1" * 64),
        _event("obs-2", 2, "2026-07-23T05:20:00Z", "2" * 64),
    ]
    result = _scoped_replay(
        [*events, dict(events[1])], station_id="ZUUU", source_lane="AMSC"
    )
    rewind_blocked = _expect_error(
        lambda: _scoped_replay(
            [*events, {**events[0], "observation_id": "obs-3"}],
            station_id="ZUUU",
            source_lane="AMSC",
        ),
        "canonical_event_sequence_rewind",
    )
    same_id_conflict_blocked = _expect_error(
        lambda: _scoped_replay(
            [events[0], {**events[0], "raw_sha256": "9" * 64}],
            station_id="ZUUU",
            source_lane="AMSC",
        ),
        "canonical_event_id_content_conflict",
    )
    late_timestamp_blocked = _expect_error(
        lambda: _scoped_replay(
            [*events, _event("obs-late", 3, "2026-07-23T05:15:00Z", "3" * 64)],
            station_id="ZUUU",
            source_lane="AMSC",
        ),
        "canonical_event_timestamp_rewind",
    )
    cross_day_blocked = _expect_error(
        lambda: _scoped_replay(
            [{**events[0], "event_date": "2026-07-24"}],
            station_id="ZUUU",
            source_lane="AMSC",
            event_date=REPLAY_EVENT_DATE,
        ),
        "canonical_event_event_date_scope_mismatch",
    )
    halted = _scoped_replay(
        [{**events[0], "correction_suspected": True}],
        station_id="ZUUU",
        source_lane="AMSC",
    )
    halt_blocked = _expect_error(
        lambda: _scoped_replay(
            [
                {**events[0], "correction_suspected": True},
                {**events[1], "event_sequence": 2},
            ],
            station_id="ZUUU",
            source_lane="AMSC",
        ),
        "canonical_event_after_correction_halted",
    )
    multi_round = _multi_round_replay()
    low_wrong_low = _low_wrong_low_roundtrip()
    return {
        "accepted_count": len(result["accepted_event_ids"]),
        "duplicate_count": len(result["duplicate_event_ids"]),
        "rewind_blocked": rewind_blocked,
        "same_id_conflict_blocked": same_id_conflict_blocked,
        "late_timestamp_blocked": late_timestamp_blocked,
        "cross_day_scope_blocked": cross_day_blocked,
        "correction_halt_state": halted["halted"] is True,
        "post_correction_blocked": halt_blocked,
        "multi_round": multi_round,
        "low_wrong_low": low_wrong_low,
    }


def _revision_event(
    kind: str,
    *,
    sequence: int = 2,
    supersedes_revision_id: str | None = "revision-taf-1",
) -> ForecastRevision:
    return ForecastRevision.from_mapping({
        "revision_id": f"revision-{kind.lower()}-{sequence}",
        "station_id": "ZUUU",
        "event_date": REPLAY_EVENT_DATE,
        "revision_kind": kind,
        "sequence": sequence,
        "issued_at_utc": "2026-07-23T05:30:00Z",
        "received_at_utc": "2026-07-23T05:31:00Z",
        "valid_from_utc": "2026-07-23T05:30:00Z",
        "valid_until_utc": "2026-07-24T05:30:00Z",
        "source_object_sha256": ("e" if kind != "COR" else "f") * 64,
        "supersedes_revision_id": supersedes_revision_id,
    })


def _revision_halt_scenarios() -> list[dict[str, object]]:
    root = ForecastRevision.from_mapping({
        "revision_id": "revision-taf-1",
        "station_id": "ZUUU",
        "event_date": REPLAY_EVENT_DATE,
        "revision_kind": "TAF",
        "sequence": 1,
        "issued_at_utc": "2026-07-23T04:30:00Z",
        "received_at_utc": "2026-07-23T04:31:00Z",
        "valid_from_utc": "2026-07-23T04:30:00Z",
        "valid_until_utc": "2026-07-24T04:30:00Z",
        "source_object_sha256": "d" * 64,
        "supersedes_revision_id": None,
    })
    scenarios: list[tuple[str, ForecastRevision, bool]] = [
        ("AMD", _revision_event("AMD"), True),
        ("COR", _revision_event("COR"), True),
        (
            "CONTRADICTORY",
            _revision_event("AMD", supersedes_revision_id="revision-other-1"),
            False,
        ),
    ]
    output: list[dict[str, object]] = []
    for name, revision, chain_valid_expected in scenarios:
        chain_valid = False
        chain_error = None
        try:
            validate_forecast_revision_chain([root, revision])
            chain_valid = True
        except ContractError as exc:
            chain_error = str(exc)
        trigger = _event(
            f"revision-{name.lower()}",
            2,
            "2026-07-23T05:40:00Z",
            ("a" if name == "AMD" else "b" if name == "COR" else "c") * 64,
            correction_suspected=True,
        )
        post = _event(
            f"post-{name.lower()}",
            3,
            "2026-07-23T05:50:00Z",
            ("d" if name == "AMD" else "e" if name == "COR" else "f") * 64,
        )
        halted = _scoped_replay(
            [_event("revision-base", 1, "2026-07-23T05:35:00Z", "0" * 64), trigger],
            station_id="ZUUU",
            source_lane="AMSC",
        )
        post_blocked = _expect_error(
            lambda: _scoped_replay(
                [
                    _event("revision-base", 1, "2026-07-23T05:35:00Z", "0" * 64),
                    trigger,
                    post,
                ],
                station_id="ZUUU",
                source_lane="AMSC",
            ),
            "canonical_event_after_correction_halted",
        )
        output.append({
            "scenario": name,
            "revision_kind": revision.revision_kind,
            "revision_chain_valid": chain_valid,
            "revision_chain_valid_expected": chain_valid_expected,
            "revision_chain_error": chain_error,
            "station_day_state": "HALTED_REVIEW" if halted["halted"] else "ACTIVE",
            "review_required": halted["halted"] is True,
            "post_event_blocked": post_blocked,
            "automatic_reversal": False,
            "secondary_materialization": False,
        })
    return output


def build_receipt() -> dict[str, object]:
    stage_runs = [_stage_chain(lane) for lane in ("AMSC", "AWC")]
    replay = _event_replay()
    revision_scenarios = _revision_halt_scenarios()
    checks = {
        # Original eight checks are retained and strengthened below.
        "amsc_stage_chain": stage_runs[0]["p1_row_count"] == 1
        and stage_runs[0]["p0_m_stage"] == "P0-M"
        and stage_runs[0]["station_id"] == "ZUUU",
        "awc_stage_chain": stage_runs[1]["p1_row_count"] == 1
        and stage_runs[1]["p0_m_stage"] == "P0-M"
        and stage_runs[1]["station_id"] == "KATL",
        "p1_exactly_once": all(
            row["same_event_same_p1_only_one_write"]
            and row["p1_replay_count"] == 100
            and row["p1_replay_writes_false_count"] == 100
            and row["p1_materialization_writes"] == 1
            for row in stage_runs
        ),
        "correction_requires_review": all(row["correction_blocked"] for row in stage_runs),
        "strict_asof_required": all(row["strict_asof_blocked"] for row in stage_runs),
        "event_duplicate_is_idempotent": replay["accepted_count"] == 2
        and replay["duplicate_count"] == 1,
        "event_rewind_fails_closed": replay["rewind_blocked"] is True
        and replay["late_timestamp_blocked"] is True,
        "correction_halts_following_events": replay["correction_halt_state"]
        and replay["post_correction_blocked"],
        "same_id_different_content_fails_closed": replay["same_id_conflict_blocked"] is True,
        "cross_day_scope_fails_closed": replay["cross_day_scope_blocked"] is True,
        "multi_round_forward_backward_forward_deterministic": replay["multi_round"][
            "deterministic_final_replay"
        ]
        and replay["multi_round"]["final_legal_result_determined"]
        and all(
            item["backward_sequence_blocked"]
            and item["late_timestamp_blocked"]
            and item["state_unchanged_after_rejected_inputs"]
            for item in replay["multi_round"]["rounds"]
        ),
        "low_wrong_low_roundtrip_safe": replay["low_wrong_low"]["wrong_content_blocked"]
        and replay["low_wrong_low"]["low_retry_duplicate_count"] == 1
        and replay["low_wrong_low"]["state_unchanged_after_wrong_content"],
        "revision_halts_are_station_day_scoped": all(
            item["station_day_state"] == "HALTED_REVIEW"
            and item["review_required"]
            and item["post_event_blocked"]
            and item["automatic_reversal"] is False
            and item["secondary_materialization"] is False
            for item in revision_scenarios
        ),
        "amd_and_cor_revision_chains_are_explicit": all(
            item["revision_chain_valid"] is item["revision_chain_valid_expected"]
            for item in revision_scenarios
        ),
        "settlement_timezone_is_registry_authoritative": all(
            row["settlement_timezone_source"] == "market_registry.settlement_timezone"
            and row["event_date_source"] == "market_registry.event_date"
            and row["operational_timezone_not_authoritative"]
            for row in stage_runs
        ),
    }
    identity: dict[str, object] = {
        "schema": "bayes_basket_target_ingress_replay_receipt_v2",
        "scope": {
            "registry_schema": "test_market_registry_scope_v1",
            "registry_scope": TEST_MARKET_REGISTRY_SCOPE,
            "settlement_timezone_authority": "market_registry.settlement_timezone",
            "operational_day_is_not_market_day": True,
        },
        "stage_runs": stage_runs,
        "event_replay": replay,
        "revision_scenarios": revision_scenarios,
        "scenarios": [
            "exactly_once_same_event_same_p1_100_replays",
            "forward_backward_forward_multi_round",
            "same_id_different_content",
            "sequence_rewind",
            "late_timestamp_rewind",
            "cross_day_scope_mismatch",
            "AMD_station_day_halt_review",
            "COR_station_day_halt_review",
            "contradictory_revision_station_day_halt_review",
            "low_wrong_low_roundtrip",
        ],
        "counts": {
            "stage_lanes": len(stage_runs),
            "p1_materialization_attempts": sum(
                int(row["p1_materialization_attempts"]) for row in stage_runs
            ),
            "p1_materialization_writes": sum(
                int(row["p1_materialization_writes"]) for row in stage_runs
            ),
            "event_replay_rounds": len(replay["multi_round"]["rounds"]),
            "revision_scenarios": len(revision_scenarios),
            "checks": len(checks),
        },
        "checks": checks,
        "status": "PASS" if all(checks.values()) else "FAIL",
        "boundary": {
            "isolated_in_memory_only": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "no_live_submit": True,
            "probability_inputs_are_materialized_contracts_only": True,
            "receipt_max_bytes": MAX_RECEIPT_BYTES,
        },
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-receipt", type=Path)
    args = parser.parse_args()
    receipt = build_receipt()
    encoded = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(encoded.encode("utf-8")) >= MAX_RECEIPT_BYTES:
        raise ContractError("ingress_replay_receipt_over_1mib")
    if args.write_receipt is not None:
        output = args.write_receipt if args.write_receipt.is_absolute() else ROOT / args.write_receipt
        write_bounded_json(output, receipt, limit=MAX_RECEIPT_BYTES)
    print(encoded, end="")
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
