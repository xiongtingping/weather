"""Assemble immutable weak-likelihood training events from closed evidence.

This is the missing bridge between P1 Tier-A forecast observations and the
offline-only joint weak trainer.  It intentionally accepts an *explicit*
forecast-context capture for every cutoff: season is calendar context, while
weather regime / lead bucket must be fixed using forecast-only evidence before
the cutoff.  Neither this assembler nor its callers may derive a context from
D0 observations, a final label, prices, orders, or positions.

Final physical-X labels arrive after the decision cutoff and are supervised
targets only.  Their existence must never affect which forecast revisions were
visible at that cutoff.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from weak_likelihood_source_set_runtime import SOURCE_UNIVERSE
from forecast_weak_context_capture_runtime import IDENTITY_KIND_MARKET_TARGET, IDENTITY_KIND_STATION_DAY


SCHEMA_VERSION = "joint_forecast_weak_training_event_assembly_v1"
CONTEXT_SCHEMA_VERSION = "forecast_weak_context_capture_v1"
_CONTEXT_FIELDS = ("season", "weather_regime", "lead_bucket")
_FORBIDDEN_TOKENS = ("market", "price", "order", "position", "pnl", "edge", "lcb", "metar", "observation", "current_")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _sha256_text(value: Any) -> bool:
    text = _text(value)
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text.lower())


def _forbidden(value: Any, *, path: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = _text(key).lower()
            child_path = f"{path}.{name}" if path else name
            if any(token in name for token in _FORBIDDEN_TOKENS):
                findings.append(child_path)
            findings.extend(_forbidden(child, path=child_path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            findings.extend(_forbidden(child, path=f"{path}[{index}]"))
    return findings


def _context_row(value: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    row = _mapping(value)
    context = _mapping(row.get("forecast_context"))
    cutoff = _time(row.get("decision_as_of"))
    available = _time(row.get("context_available_at"))
    issues: list[str] = []
    if _text(row.get("schema")) != CONTEXT_SCHEMA_VERSION:
        issues.append("forecast_context_capture_schema_invalid")
    for field in ("context_capture_id", "context_raw_sha256", "event_date"):
        if not _text(row.get(field)):
            issues.append(f"forecast_context_capture_{field}_missing")
    if _text(row.get("context_raw_sha256")) and not _sha256_text(row.get("context_raw_sha256")):
        issues.append("forecast_context_capture_raw_sha256_invalid")
    if _text(row.get("context_source_class")) != "forecast_weak_context":
        issues.append("forecast_context_capture_source_class_invalid")
    if cutoff is None or available is None:
        issues.append("forecast_context_capture_time_missing_or_invalid")
    elif available > cutoff:
        issues.append("forecast_context_capture_after_decision_asof")
    if any(not _text(context.get(field)) for field in _CONTEXT_FIELDS):
        issues.append("forecast_context_capture_required_fields_missing")
    if set(context) != set(_CONTEXT_FIELDS):
        issues.append("forecast_context_capture_fields_drift")
    allowed_top_level = {
        "schema", "context_capture_id", "context_raw_sha256", "context_source_class",
        "target_identity", "identity_kind", "training_identity", "station_id", "event_date", "decision_as_of", "context_available_at",
        "forecast_context", "forecast_raw_lineage", "target_timezone", "ok", "state",
        "read_only", "boundary",
    }
    if set(row) - allowed_top_level:
        issues.append("forecast_context_capture_unexpected_field")
    # Capture boundary/provenance metadata deliberately use words such as
    # ``observation`` to state what was *not* consumed.  They are never model
    # inputs and must not cause a valid generated capture to self-quarantine.
    context_surface = {
        "context_source_class": row.get("context_source_class"),
        "identity_kind": row.get("identity_kind"),
        "training_identity": row.get("training_identity"),
        "station_id": row.get("station_id"),
        "target_identity": row.get("target_identity"),
        "event_date": row.get("event_date"),
        "decision_as_of": row.get("decision_as_of"),
        "context_available_at": row.get("context_available_at"),
        "forecast_context": context,
    }
    if _forbidden(context_surface):
        issues.append("forecast_context_capture_contains_forbidden_observation_or_market_input")
    if issues:
        return None, sorted(set(issues))
    target = _text(row.get("target_identity"))
    kind = _text(row.get("identity_kind")) or (IDENTITY_KIND_MARKET_TARGET if target else IDENTITY_KIND_STATION_DAY)
    station = _text(row.get("station_id")).upper() or "ZUUU"
    training_identity = _text(row.get("training_identity")) or (target if target else f"station_day:{station}:{_text(row.get('event_date'))[:10]}")
    if kind not in {IDENTITY_KIND_MARKET_TARGET, IDENTITY_KIND_STATION_DAY}:
        return None, ["forecast_context_capture_identity_kind_invalid"]
    if kind == IDENTITY_KIND_MARKET_TARGET and (not target or training_identity != target):
        return None, ["forecast_context_capture_market_target_identity_invalid"]
    if kind == IDENTITY_KIND_STATION_DAY and training_identity != f"station_day:{station}:{_text(row.get('event_date'))[:10]}":
        return None, ["forecast_context_capture_station_day_identity_invalid"]
    return {
        "context_capture_id": _text(row.get("context_capture_id")),
        "context_raw_sha256": _text(row.get("context_raw_sha256")),
        "identity_kind": kind,
        "training_identity": training_identity,
        "station_id": station,
        "target_identity": target,
        "event_date": _text(row.get("event_date"))[:10],
        "decision_as_of": cutoff.isoformat().replace("+00:00", "Z"),
        "forecast_context": {field: _text(context.get(field)) for field in _CONTEXT_FIELDS},
    }, []


def _revision_row(value: Mapping[str, Any], *, event_date: str, cutoff: datetime) -> tuple[dict[str, Any] | None, list[str]]:
    row = _mapping(value)
    payload = _mapping(row.get("payload"))
    source_id = _text(row.get("source_id"))
    available = _time(row.get("fetched_at"))
    issues: list[str] = []
    if source_id not in SOURCE_UNIVERSE:
        issues.append("forecast_revision_source_not_in_unique_weak_universe")
    if _text(row.get("source_class")) != "forecast_weak_source" or row.get("hard_eligible") not in (False, 0, "0"):
        issues.append("forecast_revision_not_weak_only")
    if _text(row.get("event_date"))[:10] != event_date:
        issues.append("forecast_revision_event_date_mismatch")
    if not _text(row.get("source_event_id")) or not _text(row.get("source_observation_id")):
        issues.append("forecast_revision_identity_missing")
    if not _text(row.get("raw_sha256")) or not _text(row.get("raw_ref")):
        issues.append("forecast_revision_raw_lineage_missing")
    elif not _sha256_text(row.get("raw_sha256")):
        issues.append("forecast_revision_raw_sha256_invalid")
    if available is None:
        issues.append("forecast_revision_first_seen_missing_or_invalid")
    elif available > cutoff:
        issues.append("forecast_revision_after_decision_asof")
    if payload.get("evidence_tier") != "A_live_observed_transport" or payload.get("response_headers_observed") is not True:
        issues.append("forecast_revision_not_tier_a_observed_transport")
    if payload.get("future_training_proposal_only") is not True or payload.get("capture_time_provenance") != "observed_transport":
        issues.append("forecast_revision_training_provenance_invalid")
    if _text(payload.get("first_system_seen_at")) != _text(row.get("fetched_at")) or _text(payload.get("available_at")) != _text(row.get("fetched_at")):
        issues.append("forecast_revision_arrival_binding_invalid")
    # Identity/lineage field names contain words such as ``observation`` but
    # are not model inputs.  Inspect only the forecast feature surface and
    # its capture payload; never mistake an auditable ID for leaked weather.
    forecast_surface = {
        "source_id": source_id,
        "source_class": _text(row.get("source_class")),
        "event_date": _text(row.get("event_date")),
        "valid_time": _text(row.get("valid_time")),
        "forecast_peak_c": row.get("forecast_peak_c"),
        "forecast_sigma_c": row.get("forecast_sigma_c"),
        "payload": payload,
    }
    if _forbidden(forecast_surface):
        issues.append("forecast_revision_contains_forbidden_observation_or_market_input")
    try:
        peak = float(row.get("forecast_peak_c"))
    except (TypeError, ValueError):
        peak = None
    if peak is None:
        issues.append("forecast_revision_peak_missing_or_invalid")
    if issues:
        return None, sorted(set(issues))
    return {
        "source_id": source_id,
        "source_revision_id": _text(row.get("source_event_id")),
        "source_observation_id": _text(row.get("source_observation_id")),
        "source_class": "forecast_weak_source",
        "strict_asof_tier_a": True,
        "response_headers_observed": True,
        "available_at": available.isoformat().replace("+00:00", "Z"),
        "forecast_peak_c": peak,
        "forecast_sigma_c": row.get("forecast_sigma_c"),
        "raw_sha256": _text(row.get("raw_sha256")),
        "raw_ref": _text(row.get("raw_ref")),
    }, []


def _physical_label(value: Mapping[str, Any], *, event_date: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Turn the closed AMSC daily-X source into a weak supervised label.

    Weak likelihood predicts physical maximum X.  It must not wait for the
    later X→S kernel calibration or market bucket B: requiring that bridge
    would make the data needed to learn the bridge unavailable in the first
    place.  S/B remain mandatory for strong-model settlement evaluation.
    """

    observation = _mapping(value)
    payload = _mapping(observation.get("payload"))
    issues: list[str] = []
    if _text(observation.get("source_id")) != "amsc_zuuu_scheduled_metar_daily_max_v2":
        issues.append("final_physical_x_source_id_invalid")
    if _text(observation.get("source_class")) != "settlement_source" or _text(observation.get("role")) != "physical_daily_high_final_truth":
        issues.append("final_physical_x_source_role_invalid")
    if _text(observation.get("station_id")).upper() != "ZUUU" or _text(observation.get("event_date"))[:10] != event_date:
        issues.append("final_physical_x_identity_mismatch")
    if _text(payload.get("label_finality")) != "final":
        issues.append("final_physical_x_not_final")
    if payload.get("strict_asof_training_eligible") is not True:
        issues.append("final_physical_x_not_strict_asof_training_eligible")
    raw_sha = _text(observation.get("raw_sha256"))
    if not _sha256_text(raw_sha) or not _text(observation.get("raw_ref")) or not _text(observation.get("source_observation_id")):
        issues.append("final_physical_x_raw_lineage_missing_or_invalid")
    try:
        temperature = float(payload.get("final_physical_high_c"))
    except (TypeError, ValueError):
        temperature = None
    if not raw_sha or temperature is None:
        issues.append("final_physical_x_scalar_missing_or_invalid")
    if issues:
        return None, sorted(set(issues))
    return {
        "label_kind": "physical_x_final",
        "final": True,
        "temperature_c": temperature,
        "raw_sha256": raw_sha,
        "physical_source_observation_id": _text(observation.get("source_observation_id")),
        "label_available_at": _text(observation.get("fetched_at")),
    }, []


def build_joint_weak_training_events(
    *,
    context_captures: Sequence[Mapping[str, Any]],
    strict_forecast_observations: Sequence[Mapping[str, Any]],
    final_physical_source_observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Assemble only replayable events; invalid evidence is quarantined.

    The result intentionally stays offline/read-only.  It is valid for the
    existing ``build_joint_weak_training_dataset`` input and does not read or
    mutate posterior, market, decision, or order state.
    """

    physical_by_day: dict[str, list[Mapping[str, Any]]] = {}
    for observation in final_physical_source_observations:
        row = _mapping(observation)
        physical_by_day.setdefault(_text(row.get("event_date"))[:10], []).append(row)
    events: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    lineage: set[str] = set()
    for raw_context in context_captures:
        context, issues = _context_row(_mapping(raw_context))
        capture_id = _text(_mapping(raw_context).get("context_capture_id"))
        if context is None:
            quarantined.append({"context_capture_id": capture_id, "reasons": issues})
            continue
        training_identity, day = context["training_identity"], context["event_date"]
        matching_physical = physical_by_day.get(day, [])
        if len(matching_physical) != 1:
            quarantined.append({"context_capture_id": capture_id, "reasons": ["final_physical_x_source_missing" if not matching_physical else "final_physical_x_source_ambiguous"]})
            continue
        physical_x, label_issues = _physical_label(matching_physical[0], event_date=day)
        if physical_x is None:
            quarantined.append({"context_capture_id": capture_id, "reasons": label_issues})
            continue
        cutoff = _time(context["decision_as_of"])
        assert cutoff is not None
        revisions: list[dict[str, Any]] = []
        revision_issues: list[str] = []
        for observation in strict_forecast_observations:
            row = _mapping(observation)
            if _text(row.get("event_date"))[:10] != day:
                continue
            revision, row_issues = _revision_row(row, event_date=day, cutoff=cutoff)
            if revision is not None:
                revisions.append(revision)
            elif "forecast_revision_after_decision_asof" not in row_issues:
                revision_issues.extend(row_issues)
        if not revisions:
            quarantined.append({"context_capture_id": capture_id, "reasons": sorted(set(revision_issues or ["strict_asof_forecast_revision_missing_at_cutoff"]))})
            continue
        # Preserve every visible revision; the trainer deterministically picks
        # the last visible revision per source and records its source-set mask.
        revisions.sort(key=lambda item: (item["source_id"], item["available_at"], item["source_revision_id"]))
        event_core = {
            "identity_kind": context["identity_kind"],
            "training_identity": training_identity,
            "station_id": context["station_id"],
            "event_date": day,
            "decision_as_of": context["decision_as_of"],
            "context_capture_id": context["context_capture_id"],
            "forecast_context": context["forecast_context"],
            "physical_x_final": physical_x,
            "source_revisions": revisions,
        }
        if context["target_identity"]:
            event_core["target_identity"] = context["target_identity"]
        event_id = f"jwev_{_canonical_sha256(event_core)[:24]}"
        events.append({"event_id": event_id, **event_core})
        lineage.add(context["context_raw_sha256"])
        lineage.add(physical_x["raw_sha256"])
        lineage.update(revision["raw_sha256"] for revision in revisions)
    events.sort(key=lambda row: (row["event_date"], row["decision_as_of"], row["event_id"]))
    manifest = {
        "schema": SCHEMA_VERSION,
        "event_count": len(events),
        "quarantined_context_count": len(quarantined),
        "lineage_count": len(lineage),
        "lineage_sha256": _canonical_sha256(sorted(lineage)),
        "event_sha256": _canonical_sha256(events),
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(events),
        "state": "available" if events else "blocked",
        "events": events,
        "quarantined_contexts": quarantined,
        "manifest": manifest,
        "boundary": {
            "strict_tier_a_forecast_only": True,
            "closed_physical_x_label_only": True,
            "final_label_is_supervised_target_only": True,
            "x_to_s_to_b_bridge_not_required_for_weak_x_training": True,
            "context_is_explicit_forecast_only_capture": True,
            "station_day_training_identity_without_market_target_supported": True,
            "writes_posterior": False,
            "writes_archive_db": False,
            "places_orders": False,
            "live_amsc_ingest_blocked": False,
        },
    }


__all__ = ["CONTEXT_SCHEMA_VERSION", "SCHEMA_VERSION", "build_joint_weak_training_events"]
