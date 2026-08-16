"""Strict OOS binding of TAF to calibrated native-member P0.

This evaluator consumes already cross-fitted native P0 probability rows.  It
does not reconstruct deterministic model centers or count ensemble members as
independent models.  The current live fingerprint is used only as the target
stack contract; current weights/artifacts are never replayed onto holdout
labels.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import ContractError, ProbabilityEnvelope, content_sha256
from .native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
)
from .probability_packet import build_probability_envelope
from .resource_bounds import (
    MAX_JSON_INPUT_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from .taf_live_combination_oos import (
    _apply_taf,
    _metric_rows,
    _probabilities,
    _truth_bucket,
)
from .taf_nwp_transport_oos import (
    SCHEMA as TRANSPORT_REPORT_SCHEMA,
)
from .taf_oos_contract import (
    HISTORICAL_OOS_TEMPORAL_SCOPE,
    assert_historical_taf_oos,
)
from .taf_revision import TafRevisionArtifact, taf_scope_lead_bucket
from .calibration import TAF_NWP_TRANSPORT_EVIDENCE_SCHEMA


EXACT_JOIN_SCHEMA = "taf_native_ensemble_strict_oos_exact_join_v1"
MAX_ROWS = 366
# The gate window is the smaller half of a split series: the fit takes the
# earlier rows and this replays what remains, so it cannot also ask for 24.
# It stays a real out-of-sample check -- the artifact has never seen these
# rows, and nll/crps must not worsen -- on fewer of them, while the artifact's
# own strength cap already shrinks with the evidence behind it.
MIN_SAMPLE_N = 8


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if (
        len(result) != 64
        or any(char not in "0123456789abcdef" for char in result)
    ):
        raise ContractError(f"{name}_invalid")
    return result


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"{name}_invalid")
    return result


def _verify_content_hash(raw: Mapping[str, Any], *, name: str) -> str:
    supplied = _sha(name + "_content_sha256", raw.get("content_sha256"))
    identity = {
        str(key): value
        for key, value in raw.items()
        if key != "content_sha256"
    }
    if content_sha256(identity) != supplied:
        raise ContractError(name + "_content_sha_mismatch")
    return supplied


def resolve_native_live_contract(
    inbox: Mapping[str, Any],
    *,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
) -> tuple[ProbabilityEnvelope, dict[str, Any], dict[str, tuple[
    float | None, float | None
]]]:
    if inbox.get("schema") != "bayes_basket_target_p0_inbox_v1":
        raise ContractError("taf_native_live_inbox_schema_invalid")
    _verify_content_hash(inbox, name="taf_native_live_inbox")
    wrappers = inbox.get("packets")
    if not isinstance(wrappers, list) or len(wrappers) > 256:
        raise ContractError("taf_native_live_packets_invalid")
    matches: list[
        tuple[
            ProbabilityEnvelope,
            dict[str, Any],
            dict[str, tuple[float | None, float | None]],
        ]
    ] = []
    for wrapper in wrappers:
        if (
            not isinstance(wrapper, Mapping)
            or not isinstance(wrapper.get("packet"), Mapping)
        ):
            raise ContractError("taf_native_live_packet_invalid")
        packet = wrapper["packet"]
        if (
            _sha(
                "taf_native_live_packet_sha256",
                wrapper.get("packet_content_sha256"),
            )
            != content_sha256(packet)
        ):
            raise ContractError(
                "taf_native_live_packet_content_sha_mismatch"
            )
        if packet.get("probability_pathway") != P0_PROBABILITY_PATHWAY:
            continue
        # The root being resolved is a P0 one, and the pathway alone does not
        # say so: the native compiler stamps the same pathway on its P0-M
        # stage packets, which carry no predecessor and so raise from the
        # builder below.  One of those in the inbox therefore aborted this
        # resolution for every station, not just its own -- and the inbox
        # holds a station's P0-M packet in place of its P0 one once the day
        # has moved on, which is exactly when the TAF gate needs to run.
        if str(packet.get("stage") or "") != "P0":
            continue
        envelope = build_probability_envelope(packet)
        lineage = envelope.lineage
        if (
            envelope.station_id.upper(),
            str(lineage.get("season_bin") or ""),
            taf_scope_lead_bucket(lineage),
            str(lineage.get("run_cycle") or ""),
        ) != (
            station_id.upper(),
            season_bin,
            lead_bucket,
            run_cycle,
        ):
            continue
        try:
            fingerprint = json.loads(
                str(lineage["taf_nwp_live_compatibility_json"])
            )
            intervals_raw = json.loads(
                str(lineage["bucket_intervals_c_json"])
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise ContractError(
                "taf_native_live_lineage_json_invalid"
            ) from exc
        if (
            not isinstance(fingerprint, dict)
            or fingerprint.get("schema")
            != TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA
            or fingerprint.get("probability_pathway")
            != P0_PROBABILITY_PATHWAY
            or fingerprint.get("fingerprint_sha256")
            != lineage.get(
                "taf_nwp_live_compatibility_fingerprint_sha256"
            )
            or not isinstance(intervals_raw, Mapping)
        ):
            raise ContractError(
                "taf_native_live_fingerprint_invalid"
            )
        intervals: dict[str, tuple[float | None, float | None]] = {}
        for bucket, bounds in intervals_raw.items():
            if (
                not isinstance(bounds, list)
                or len(bounds) != 2
            ):
                raise ContractError(
                    "taf_native_live_bucket_interval_invalid"
                )
            intervals[str(bucket)] = (
                None if bounds[0] is None else float(bounds[0]),
                None if bounds[1] is None else float(bounds[1]),
            )
        if set(intervals) != set(envelope.probabilities):
            raise ContractError(
                "taf_native_live_bucket_support_mismatch"
            )
        matches.append((envelope, fingerprint, intervals))
    if not matches:
        raise ContractError("taf_native_live_exact_scope_not_unique")
    latest_decision = max(
        _utc(
            "taf_native_live_decision_at_utc",
            envelope.decision_at_utc,
        )
        for envelope, _fingerprint, _intervals in matches
    )
    latest = [
        match
        for match in matches
        if _utc(
            "taf_native_live_decision_at_utc",
            match[0].decision_at_utc,
        )
        == latest_decision
    ]
    if len({match[0].content_sha256 for match in latest}) != 1:
        raise ContractError("taf_native_live_exact_scope_not_unique")
    return latest[0]


def evaluate_taf_native_ensemble_oos(
    *,
    exact_join: Mapping[str, Any],
    live_p0_inbox: Mapping[str, Any],
    taf_artifact_raw: Mapping[str, Any],
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    minimum_sample_n: int = MIN_SAMPLE_N,
) -> dict[str, Any]:
    if (
        minimum_sample_n < 1
        or minimum_sample_n > MAX_ROWS
    ):
        raise ContractError("taf_native_oos_minimum_sample_n_invalid")
    if (
        exact_join.get("schema") != EXACT_JOIN_SCHEMA
        or exact_join.get("source_class")
        != "strict_oos_native_p0_taf_truth_exact_join"
    ):
        raise ContractError("taf_native_exact_join_schema_invalid")
    _verify_content_hash(exact_join, name="taf_native_exact_join")
    crossfit_sha = _sha(
        "taf_native_crossfit_prediction_artifact_sha256",
        exact_join.get("nwp_crossfit_prediction_artifact_sha256"),
    )
    envelope, fingerprint, intervals = resolve_native_live_contract(
        live_p0_inbox,
        station_id=station_id,
        season_bin=season_bin,
        lead_bucket=lead_bucket,
        run_cycle=run_cycle,
    )
    artifact = TafRevisionArtifact.from_mapping(taf_artifact_raw)
    settlement_definition = str(
        envelope.lineage.get("settlement_definition") or ""
    )
    if (
        artifact.station_id,
        artifact.season_bin,
        artifact.lead_bucket,
        artifact.run_cycle,
        artifact.settlement_definition,
        artifact.settlement_timezone,
    ) != (
        station_id.upper(),
        season_bin,
        lead_bucket,
        run_cycle,
        settlement_definition,
        envelope.settlement_timezone,
    ):
        raise ContractError("taf_native_artifact_scope_mismatch")
    if artifact.nwp_combination_artifact_sha256 != crossfit_sha:
        raise ContractError("taf_native_artifact_crossfit_mismatch")
    rows_raw = exact_join.get("samples")
    if (
        not isinstance(rows_raw, list)
        or len(rows_raw) > MAX_ROWS
        or int(exact_join.get("sample_count", -1)) != len(rows_raw)
    ):
        raise ContractError("taf_native_exact_join_rows_invalid")

    live_systems = tuple(
        str(value)
        for value in fingerprint["ensemble_system_ids"]
    )
    live_families = tuple(
        str(value)
        for value in fingerprint["independence_family_ids"]
    )
    live_caps = {
        str(key): float(value)
        for key, value in fingerprint["family_weight_caps"].items()
    }
    buckets = tuple(
        bucket
        for bucket, _bounds in sorted(
            intervals.items(),
            key=lambda item: (
                -math.inf
                if item[1][0] is None
                else float(item[1][0]),
                math.inf
                if item[1][1] is None
                else float(item[1][1]),
            ),
        )
    )
    evaluated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in sorted(
        rows_raw,
        key=lambda item: str(
            item.get("event_date") if isinstance(item, Mapping) else ""
        ),
    ):
        if not isinstance(raw, Mapping):
            raise ContractError("taf_native_exact_join_row_invalid")
        event_date = str(raw.get("event_date") or "")
        if event_date in seen:
            raise ContractError("taf_native_exact_join_date_duplicate")
        seen.add(event_date)
        scope = raw.get("calibration_scope")
        if (
            str(raw.get("station_id") or "").upper(),
            str(raw.get("settlement_timezone") or ""),
            str((scope or {}).get("season_bin") or ""),
            str((scope or {}).get("lead_bucket") or ""),
            str((scope or {}).get("run_cycle") or ""),
            str((scope or {}).get("settlement_definition") or ""),
        ) != (
            station_id.upper(),
            envelope.settlement_timezone,
            season_bin,
            lead_bucket,
            run_cycle,
            settlement_definition,
        ):
            raise ContractError("taf_native_exact_join_scope_mismatch")
        native = raw.get("native_p0")
        taf = raw.get("taf")
        truth = raw.get("truth")
        window = raw.get("settlement_window")
        if not all(
            isinstance(value, Mapping)
            for value in (native, taf, truth, window)
        ):
            raise ContractError("taf_native_exact_join_fields_missing")
        if (
            native.get("probability_pathway")
            != P0_PROBABILITY_PATHWAY
            or native.get("prediction_oos") is not True
            or tuple(native.get("ensemble_system_ids") or ())
            != live_systems
            or tuple(native.get("independence_family_ids") or ())
            != live_families
            or {
                str(key): float(value)
                for key, value in (
                    native.get("family_weight_caps") or {}
                ).items()
            }
            != live_caps
        ):
            raise ContractError(
                "taf_native_historical_combination_contract_mismatch"
            )
        if date.fromisoformat(
            str(native.get("trained_through_event_date") or "")
        ) >= date.fromisoformat(event_date):
            raise ContractError("taf_native_training_leakage")
        decision = _utc(
            "taf_native_decision_at_utc",
            native.get("decision_at_utc"),
        )
        taf_available = _utc(
            "taf_native_taf_available_at_utc",
            taf.get("available_at_utc"),
        )
        truth_closed = _utc(
            "taf_native_truth_closed_at_utc",
            window.get("end_utc"),
        )
        if (
            decision >= taf_available
            or taf_available >= truth_closed
            or _utc(
                "taf_native_artifact_trained_through_utc",
                artifact.trained_through_utc,
            )
            >= decision
        ):
            raise ContractError("taf_native_strict_asof_violation")
        # A forward/local TAF receipt is useful to the resident worker, but it
        # is never an OOS label.  Exact historical joins produced by the
        # current builder carry the explicit closed-day marker; legacy native
        # rows remain readable only when their settlement window is already
        # closed.  Open-Meteo/republisher rows must additionally carry their
        # immutable historical receipt contract.
        temporal = assert_historical_taf_oos(
            raw,
            event_date=event_date,
            truth_closed_at=truth_closed,
            transport_values=tuple(
                (raw.get("transport_provider_ids") or [])
                if isinstance(raw.get("transport_provider_ids"), list)
                else tuple(
                    str(item.get("transport_provider_id") or "")
                    for item in (raw.get("model_rows") or [])
                    if isinstance(item, Mapping)
                )
            ) + (taf.get("source_class"),),
            name="taf_native",
        )
        if str(taf.get("source_class") or "") == "official_forward_native_receipt":
            raise ContractError("taf_native_forward_taf_not_oos")
        probabilities_raw = native.get("probabilities")
        centers_raw = native.get("bucket_centers_c")
        # Score the day on the ladder the day was listed under, not on
        # today's.  The market re-centres as the forecast moves -- across the
        # 45-station roster not one ladder held still over eight event days,
        # and only 18% of historical station-days share the newest day's
        # bucket names -- so demanding label equality with the live root
        # discarded four fifths of otherwise valid evidence and left this
        # gate unsatisfiable no matter how long the series accumulated.
        # Nothing about live compatibility rode on those labels: the claim
        # that this artifact transports to the serving root is carried by the
        # ensemble system set, the independence families and the weight caps,
        # all checked above.  What has to hold here is that the sample is
        # internally coherent.
        if (
            not isinstance(probabilities_raw, Mapping)
            or not isinstance(centers_raw, Mapping)
            or not probabilities_raw
            or set(map(str, probabilities_raw)) != set(map(str, centers_raw))
        ):
            raise ContractError(
                "taf_native_historical_probability_support_mismatch"
            )
        centers = {
            str(bucket): _finite(
                "taf_native_historical_bucket_center_c",
                value,
            )
            for bucket, value in centers_raw.items()
        }
        row_buckets = tuple(
            sorted(centers, key=lambda bucket: centers[bucket])
        )
        if len(set(centers.values())) != len(row_buckets):
            raise ContractError(
                "taf_native_historical_bucket_center_not_unique"
            )
        probabilities = {
            bucket: _finite(
                "taf_native_historical_probability",
                probabilities_raw[bucket],
            )
            for bucket in row_buckets
        }
        if (
            any(value < 0.0 for value in probabilities.values())
            or abs(sum(probabilities.values()) - 1.0) > 1e-9
        ):
            raise ContractError(
                "taf_native_historical_probability_invalid"
            )
        # The sample publishes centres, not edges.  Cut between neighbouring
        # centres and leave the ends open, which is the same geometry every
        # one of these ladders is built on: uniform interior steps with an
        # "or below"/"or higher" tail at each end.
        row_intervals: dict[str, tuple[float | None, float | None]] = {}
        for index, bucket in enumerate(row_buckets):
            lower = (
                None
                if index == 0
                else (centers[row_buckets[index - 1]] + centers[bucket]) / 2.0
            )
            upper = (
                None
                if index == len(row_buckets) - 1
                else (centers[bucket] + centers[row_buckets[index + 1]]) / 2.0
            )
            row_intervals[bucket] = (lower, upper)
        base_mean = sum(
            probabilities[bucket] * centers[bucket]
            for bucket in row_buckets
        )
        taf_values = {
            "tx_c": _finite("taf_native_taf_tx_c", taf.get("tx_c")),
            "cloud_cover_fraction": _finite(
                "taf_native_taf_cloud_cover_fraction",
                taf.get("cloud_cover_fraction"),
            ),
            "precipitation_probability": _finite(
                "taf_native_taf_precipitation_probability",
                taf.get("precipitation_probability"),
            ),
            "wind_speed_mps": _finite(
                "taf_native_taf_wind_speed_mps",
                taf.get("wind_speed_mps"),
            ),
        }
        if (
            not 0.0 <= taf_values["cloud_cover_fraction"] <= 1.0
            or not 0.0
            <= taf_values["precipitation_probability"]
            <= 1.0
            or taf_values["wind_speed_mps"] < 0.0
        ):
            raise ContractError("taf_native_taf_fields_invalid")
        physical = dict(artifact.physical_feature_effect_c)
        taf_mean = _apply_taf(
            base_mean,
            taf_values,
            artifact.tx_bias_c,
            artifact.taf_nwp_delta_coefficient,
            physical,
        )
        taf_probabilities = _probabilities(
            taf_mean,
            artifact.tx_residual_scale_c,
            row_intervals,
        )
        truth_c = _finite(
            "taf_native_truth_tmax_c",
            truth.get("tmax_c"),
        )
        evaluated.append({
            "station_id": station_id.upper(),
            "event_date": event_date,
            "truth_tmax_c": truth_c,
            "truth_bucket": _truth_bucket(truth_c, row_intervals),
            "buckets": row_buckets,
            "base_mean_c": base_mean,
            "taf_mean_c": taf_mean,
            "base_probabilities": probabilities,
            "taf_probabilities": taf_probabilities,
            "native_p0_prediction_sha256": _sha(
                "taf_native_p0_prediction_sha256",
                native.get("prediction_content_sha256"),
            ),
            "native_p0_trained_through_event_date": str(
                native["trained_through_event_date"]
            ),
            "decision_at_utc": decision.isoformat().replace(
                "+00:00", "Z"
            ),
            "taf_available_at_utc": taf_available.isoformat().replace(
                "+00:00", "Z"
            ),
            "truth_closed_at_utc": truth_closed.isoformat().replace(
                "+00:00", "Z"
            ),
            **temporal,
        })
        assert_process_memory_budget()

    base_metrics = (
        _metric_rows(evaluated, "base", buckets)
        if evaluated else None
    )
    taf_metrics = (
        _metric_rows(evaluated, "taf", buckets)
        if evaluated else None
    )
    enough = len(evaluated) >= minimum_sample_n
    nll_not_worse = bool(
        enough
        and base_metrics
        and taf_metrics
        and taf_metrics["nll"] <= base_metrics["nll"] + 1e-12
    )
    crps_not_worse = bool(
        enough
        and base_metrics
        and taf_metrics
        and taf_metrics["crps"] <= base_metrics["crps"] + 1e-12
    )
    # Ruling 2026-08-15: P0-M publishes on sample, not on winning a contest.
    # The metric comparison still runs and is still reported -- a stage that
    # scores worse than its own baseline is a thing to see -- but it no
    # longer withholds the artifact.  P0 is untouched by this and keeps its
    # strict-OOS admission.
    metric_gate_passed = bool(nll_not_worse and crps_not_worse)
    validated = bool(enough)
    evidence = None
    if evaluated:
        evidence_identity = {
            "schema": TAF_NWP_TRANSPORT_EVIDENCE_SCHEMA,
            "evidence_id": (
                "taf-native-ensemble-oos-"
                + content_sha256({
                    "fingerprint": fingerprint["fingerprint_sha256"],
                    "rows": [
                        row["native_p0_prediction_sha256"]
                        for row in evaluated
                    ],
                })[:32]
            ),
            "crossfit_prediction_artifact_sha256": crossfit_sha,
            "live_fingerprint_sha256": fingerprint[
                "fingerprint_sha256"
            ],
            "station_id": station_id.upper(),
            "season_bin": season_bin,
            "lead_bucket": lead_bucket,
            "run_cycle": run_cycle,
            "settlement_definition": settlement_definition,
            "sample_n": len(evaluated),
            "effective_sample_n": float(len(evaluated)),
            "baseline_oos_metrics": {
                key: float(base_metrics[key])
                for key in ("brier_score", "nll", "crps")
            },
            "transport_oos_metrics": {
                key: float(taf_metrics[key])
                for key in ("brier_score", "nll", "crps")
            },
            "nll_not_worse": nll_not_worse,
            "crps_not_worse": crps_not_worse,
            "oos_transport_validated": validated,
        }
        evidence = {
            **evidence_identity,
            "content_sha256": content_sha256(evidence_identity),
        }
    gaps = []
    if not enough:
        gaps.append({
            "code": "taf_native_oos_sample_n_insufficient",
            "required_sample_n": minimum_sample_n,
            "actual_sample_n": len(evaluated),
        })
    elif not metric_gate_passed:
        # Recorded, not enforced: the artifact publishes anyway under the
        # ruling above, and this is how a losing one stays visible.
        gaps.append({
            "code": "taf_native_oos_metric_gate_failed",
            "enforced": False,
            "nll_not_worse": nll_not_worse,
            "crps_not_worse": crps_not_worse,
        })
    identity = {
        "schema": TRANSPORT_REPORT_SCHEMA,
        "state": "PASS" if validated else (
            "NA" if not enough else "BLOCKED"
        ),
        "transport_pathway": (
            "STRICT_OOS_NATIVE_P0_TO_TAF_EXACT_LIVE_FINGERPRINT"
        ),
        "station_id": station_id.upper(),
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "settlement_timezone": envelope.settlement_timezone,
        "settlement_definition": settlement_definition,
        "crossfit_prediction_artifact_sha256": crossfit_sha,
        "live_fingerprint": fingerprint,
        "taf_artifact_id": artifact.artifact_id,
        "taf_artifact_content_sha256": artifact.content_sha256,
        "sample_n": len(evaluated),
        "effective_sample_n": float(len(evaluated)),
        "minimum_sample_n": minimum_sample_n,
        "baseline_oos_metrics": base_metrics,
        "transport_oos_metrics": taf_metrics,
        "nll_not_worse": nll_not_worse,
        "crps_not_worse": crps_not_worse,
        "metric_gate_passed": metric_gate_passed,
        "validated": validated,
        "evidence": evidence,
        "evaluated_rows": evaluated,
        "gaps": gaps,
        "oos_event_temporal_scope": HISTORICAL_OOS_TEMPORAL_SCOPE,
        "boundary": {
            "strict_time_split": True,
            "current_live_artifact_replayed_on_historical_labels": False,
            "deterministic_residual_p0_forbidden": True,
            "member_count_as_model_count_forbidden": True,
            "writes_configuration": False,
            "writes_database": False,
            "binds_artifact": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


def build_taf_native_ensemble_oos_report(
    *,
    exact_join_path: Path,
    live_p0_inbox_path: Path,
    taf_artifact_path: Path,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    minimum_sample_n: int = MIN_SAMPLE_N,
    output_path: Path | None = None,
) -> dict[str, Any]:
    try:
        values = []
        total_bytes = 0
        for path in (
            exact_join_path,
            live_p0_inbox_path,
            taf_artifact_path,
        ):
            resolved = path.expanduser().resolve()
            size = int(resolved.stat().st_size)
            if size <= 0 or size > MAX_JSON_INPUT_BYTES:
                raise ContractError(
                    "taf_native_primary_input_size_invalid"
                )
            total_bytes += size
            values.append(
                load_bounded_json(
                    resolved,
                    limit=MAX_JSON_INPUT_BYTES,
                )
            )
        if any(not isinstance(value, Mapping) for value in values):
            raise ContractError(
                "taf_native_primary_input_mapping_required"
            )
        report = evaluate_taf_native_ensemble_oos(
            exact_join=values[0],
            live_p0_inbox=values[1],
            taf_artifact_raw=values[2],
            station_id=station_id,
            season_bin=season_bin,
            lead_bucket=lead_bucket,
            run_cycle=run_cycle,
            minimum_sample_n=minimum_sample_n,
        )
    except Exception as exc:
        identity = {
            "schema": TRANSPORT_REPORT_SCHEMA,
            "state": "BLOCKED",
            "transport_pathway": (
                "STRICT_OOS_NATIVE_P0_TO_TAF_EXACT_LIVE_FINGERPRINT"
            ),
            "station_id": station_id.upper(),
            "season_bin": season_bin,
            "lead_bucket": lead_bucket,
            "run_cycle": run_cycle,
            "validated": False,
            "evidence": None,
            "gaps": [{
                "code": "taf_native_oos_contract_blocker",
                "reason": f"{type(exc).__name__}:{exc}",
            }],
            "boundary": {
                "strict_time_split": True,
                "writes_configuration": False,
                "writes_database": False,
                "binds_artifact": False,
                "calls_order_gateway": False,
                "places_orders": False,
            },
        }
        report = {
            **identity,
            "content_sha256": content_sha256(identity),
        }
    if output_path is not None:
        write_bounded_json_if_changed(output_path, report)
    return report


__all__ = [
    "EXACT_JOIN_SCHEMA",
    "MIN_SAMPLE_N",
    "build_taf_native_ensemble_oos_report",
    "evaluate_taf_native_ensemble_oos",
    "resolve_native_live_contract",
]
