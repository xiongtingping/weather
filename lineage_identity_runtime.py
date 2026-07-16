from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "lineage_identity_v1"


LINEAGE_ID_PREFIXES: dict[str, str] = {
    "correlation_id": "corr",
    "source_event_id": "srcevt",
    "source_observation_id": "srcobs",
    "fact_snapshot_id": "fact",
    "posterior_run_id": "post",
    "decision_id": "dec",
    "signal_id": "sig",
    "order_intent_id": "oint",
    "review_id": "rev",
    "canonical_observation_event_id": "canobs",
    "strong_observation_update_id": "stupd",
    "posterior_checkpoint_id": "pchk",
    "realization_state_id": "rstate",
    "training_sample_id": "trsample",
    "calibration_candidate_id": "cal",
    "daily_calibration_record_id": "dcal",
    "parameter_change_request_id": "pchg",
}

LINEAGE_FIELD_ORDER: tuple[str, ...] = tuple(LINEAGE_ID_PREFIXES.keys())
_LINEAGE_RE = re.compile(r"^[a-z][a-z0-9]*_[0-9a-f]{16,64}$")


def canonical_json(value: Any) -> str:
    """Return a stable JSON representation for lineage hashing."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def lineage_id_from_parts(kind: str, *parts: Any, namespace: str = "weather", length: int = 24) -> str:
    """Build a deterministic lineage id from natural keys.

    The function is intentionally deterministic so Phase 0 probes, replay, and
    future forward-writes can agree on IDs without a database write.
    """

    normalized_kind = str(kind or "").strip()
    prefix = LINEAGE_ID_PREFIXES.get(normalized_kind)
    if not prefix:
        raise ValueError(f"unknown_lineage_kind={normalized_kind}")
    digest_len = max(16, min(64, int(length)))
    payload = {
        "schema": SCHEMA_VERSION,
        "namespace": str(namespace or "weather"),
        "kind": normalized_kind,
        "parts": list(parts),
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:digest_len]
    return f"{prefix}_{digest}"


def validate_lineage_id(kind: str, value: Any) -> bool:
    expected_prefix = LINEAGE_ID_PREFIXES.get(str(kind or "").strip())
    text = str(value or "").strip()
    if not expected_prefix or not text.startswith(f"{expected_prefix}_"):
        return False
    return _LINEAGE_RE.match(text) is not None


@dataclass(frozen=True)
class LineageIds:
    correlation_id: str = ""
    source_event_id: str = ""
    source_observation_id: str = ""
    fact_snapshot_id: str = ""
    posterior_run_id: str = ""
    decision_id: str = ""
    signal_id: str = ""
    order_intent_id: str = ""
    review_id: str = ""
    canonical_observation_event_id: str = ""
    strong_observation_update_id: str = ""
    posterior_checkpoint_id: str = ""
    realization_state_id: str = ""
    training_sample_id: str = ""
    calibration_candidate_id: str = ""
    daily_calibration_record_id: str = ""
    parameter_change_request_id: str = ""

    def as_dict(self, *, include_empty: bool = False) -> dict[str, str]:
        values = {
            field: str(getattr(self, field) or "")
            for field in LINEAGE_FIELD_ORDER
        }
        if include_empty:
            return values
        return {key: value for key, value in values.items() if value}

    def missing(self, required: Iterable[str]) -> list[str]:
        required_set = [str(item or "").strip() for item in required if str(item or "").strip()]
        return [field for field in required_set if not str(getattr(self, field, "") or "").strip()]

    def invalid(self) -> list[str]:
        bad: list[str] = []
        for field, value in self.as_dict().items():
            if not validate_lineage_id(field, value):
                bad.append(field)
        return bad


def lineage_ids_from_mapping(payload: Mapping[str, Any] | None) -> LineageIds:
    source = payload or {}
    values = {field: str(source.get(field) or "") for field in LINEAGE_FIELD_ORDER}
    return LineageIds(**values)


def build_correlation_id(*parts: Any, namespace: str = "weather") -> str:
    return lineage_id_from_parts("correlation_id", *parts, namespace=namespace)


def build_source_event_id(
    *,
    source_id: str,
    station_id: str = "",
    event_date: str = "",
    valid_time: str = "",
    fetched_at: str = "",
    raw_sha256: str = "",
    namespace: str = "weather",
) -> str:
    return lineage_id_from_parts(
        "source_event_id",
        {
            "source_id": source_id,
            "station_id": station_id,
            "event_date": event_date,
            "valid_time": valid_time,
            "fetched_at": fetched_at,
            "raw_sha256": raw_sha256,
        },
        namespace=namespace,
    )


def build_source_observation_id(
    *,
    source_id: str,
    station_id: str = "",
    event_date: str = "",
    valid_time: str = "",
    valid_time_local: str = "",
    fetched_at: str = "",
    temperature_c: Any = None,
    dewpoint_c: Any = None,
    observed_today_max_c: Any = None,
    forecast_peak_c: Any = None,
    raw_ref: str = "",
    raw_sha256: str = "",
    namespace: str = "weather",
) -> str:
    time_key = str(valid_time or valid_time_local or fetched_at or "").strip()
    fetched_fallback = "" if str(valid_time or valid_time_local or "").strip() else str(fetched_at or "").strip()
    return lineage_id_from_parts(
        "source_observation_id",
        {
            "source_id": source_id,
            "station_id": station_id,
            "event_date": event_date,
            "observation_time_key": time_key,
            "fetched_at_if_no_observation_time": fetched_fallback,
            "temperature_c": temperature_c,
            "dewpoint_c": dewpoint_c,
            "observed_today_max_c": observed_today_max_c,
            "forecast_peak_c": forecast_peak_c,
            "raw_ref": raw_ref,
            "raw_sha256": raw_sha256,
        },
        namespace=namespace,
    )


def build_fact_snapshot_id(
    *,
    slug: str,
    event_date: str,
    fact_as_of: str,
    source_event_ids: Sequence[str] = (),
    identity_salt: Any = "",
    namespace: str = "weather",
) -> str:
    parts = {
        "slug": slug,
        "event_date": event_date,
        "fact_as_of": fact_as_of,
        "source_event_ids": list(source_event_ids),
    }
    if identity_salt not in (None, "", [], {}):
        parts["identity_salt"] = identity_salt
    return lineage_id_from_parts(
        "fact_snapshot_id",
        parts,
        namespace=namespace,
    )


def build_posterior_run_id(
    *,
    slug: str,
    event_date: str,
    fact_snapshot_id: str,
    source_event_ids: Sequence[str] = (),
    posterior_hash: str = "",
    phase: str = "",
    namespace: str = "weather",
) -> str:
    return lineage_id_from_parts(
        "posterior_run_id",
        {
            "slug": slug,
            "event_date": event_date,
            "fact_snapshot_id": fact_snapshot_id,
            "source_event_ids": list(source_event_ids),
            "posterior_hash": posterior_hash,
            "phase": phase,
        },
        namespace=namespace,
    )


def canonical_observation_key_from_parts(
    *,
    target_identity: str,
    station_id: str,
    report_type: str,
    valid_time: str,
    namespace: str = "weather",
) -> str:
    """Build the stable physical-event natural key, not a transport identity."""

    payload = {
        "schema": SCHEMA_VERSION,
        "namespace": str(namespace or "weather"),
        "target_identity": str(target_identity or "").strip(),
        "station_id": str(station_id or "").strip().upper(),
        "report_type": str(report_type or "").strip().upper(),
        "valid_time": str(valid_time or "").strip(),
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:24]
    return f"canokey_{digest}"


def build_canonical_observation_event_id(
    *,
    canonical_observation_key: str,
    revision_no: int,
    normalized_physical_sha256: str,
    namespace: str = "weather",
) -> str:
    return lineage_id_from_parts(
        "canonical_observation_event_id",
        {
            "canonical_observation_key": str(canonical_observation_key or "").strip(),
            "revision_no": int(revision_no),
            "normalized_physical_sha256": str(normalized_physical_sha256 or "").strip(),
        },
        namespace=namespace,
    )


def build_strong_observation_update_id(
    *,
    previous_posterior_run_id: str,
    previous_posterior_sha256: str,
    canonical_observation_event_id: str,
    forecast_evidence_snapshot_id: str,
    model_artifact_sha256: str,
    namespace: str = "weather",
) -> str:
    return lineage_id_from_parts(
        "strong_observation_update_id",
        {
            "previous_posterior_run_id": str(previous_posterior_run_id or "").strip(),
            "previous_posterior_sha256": str(previous_posterior_sha256 or "").strip(),
            "canonical_observation_event_id": str(canonical_observation_event_id or "").strip(),
            "forecast_evidence_snapshot_id": str(forecast_evidence_snapshot_id or "").strip(),
            "model_artifact_sha256": str(model_artifact_sha256 or "").strip(),
        },
        namespace=namespace,
    )


def build_posterior_checkpoint_id(
    *,
    target_identity: str,
    posterior_run_id: str,
    replay_generation: int,
    joint_posterior_sha256: str,
    namespace: str = "weather",
) -> str:
    return lineage_id_from_parts(
        "posterior_checkpoint_id",
        {
            "target_identity": str(target_identity or "").strip(),
            "posterior_run_id": str(posterior_run_id or "").strip(),
            "replay_generation": int(replay_generation),
            "joint_posterior_sha256": str(joint_posterior_sha256 or "").strip(),
        },
        namespace=namespace,
    )


def build_realization_state_id(
    *,
    posterior_run_id: str,
    fact_snapshot_id: str,
    state_sha256: str,
    namespace: str = "weather",
) -> str:
    return lineage_id_from_parts(
        "realization_state_id",
        {
            "posterior_run_id": str(posterior_run_id or "").strip(),
            "fact_snapshot_id": str(fact_snapshot_id or "").strip(),
            "state_sha256": str(state_sha256 or "").strip(),
        },
        namespace=namespace,
    )


def build_training_sample_id(
    *,
    target_identity: str,
    canonical_observation_event_id: str,
    cutoff_as_of: str,
    artifact_sha256: str,
    namespace: str = "weather",
) -> str:
    return lineage_id_from_parts(
        "training_sample_id",
        {
            "target_identity": str(target_identity or "").strip(),
            "canonical_observation_event_id": str(canonical_observation_event_id or "").strip(),
            "cutoff_as_of": str(cutoff_as_of or "").strip(),
            "artifact_sha256": str(artifact_sha256 or "").strip(),
        },
        namespace=namespace,
    )
