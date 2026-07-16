"""Build forecast-only live evidence for the one joint weak-likelihood model.

This adapter is intentionally narrow.  It reads immutable official daily-peak
revisions and independent forecast observations already available before the
decision cutoff, strips them to the weak-model forecast contract, and creates
one immutable evidence snapshot.  It neither inspects METAR/SPECI/current
temperature nor market/trading state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from amsc_taf_revision_runtime import taf_revision_order_key
from joint_forecast_weak_likelihood_runtime import build_joint_forecast_weak_likelihood
from joint_weak_baseline_artifact_runtime import load_approved_joint_weak_artifact
from independent_weak_live_source_runtime import load_live_independent_weak_source_observations
from weak_likelihood_source_set_runtime import SOURCE_UNIVERSE
from forecast_background_context_runtime import build_forecast_background_context


SCHEMA_VERSION = "joint_weak_live_forecast_evidence_v1"
DEFAULT_CURVE_ROOT = Path(__file__).resolve().parent / "data" / "runtime" / "official_nwp_curve_sources_v2"
_OFFICIAL_SOURCE_IDS = ("ecmwf", "icon", "gfs", "jma")
_INDEPENDENT_SOURCE_IDS = ("taf", "wu", "twc")
_FORECAST_REGIMES = frozenset(
    {
        "clear_dry",
        "cloudy_stable",
        "wet_precipitation",
        "convection_or_showers",
        "fog_low_cloud",
        "unknown_forecast_regime",
        "mixed_forecast_regime",
    }
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if output == output and output not in (float("inf"), float("-inf")) else None


def _time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _season(event_date: str) -> str:
    try:
        month = datetime.fromisoformat(event_date[:10]).month
    except ValueError:
        return ""
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "autumn"
    return "winter"


def _lead_bucket(event_date: str, decision: datetime) -> str:
    try:
        target_start = datetime.fromisoformat(event_date[:10]).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return ""
    hours = (decision.astimezone(ZoneInfo("Asia/Shanghai")) - target_start).total_seconds() / 3600.0
    if hours < -24.0:
        return "d_minus_2_or_earlier"
    if hours < 0.0:
        return "d_minus_1"
    if hours < 6.0:
        return "d0_0000_0559"
    if hours < 9.0:
        return "d0_0600_0859"
    if hours < 12.0:
        return "d0_0900_1159"
    if hours < 15.0:
        return "d0_1200_1459"
    return "d0_1500_or_later"


def _clean_revision(
    *, source_id: str, revision_id: str, event_date: str, available_at: datetime,
    forecast_peak_c: float, raw_sha256: str, forecast_sigma_c: float | None,
    provenance: Mapping[str, Any], forecast_weather_regime: str = "",
    forecast_peak_valid_time: str = "",
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "source_revision_id": revision_id,
        "source_class": "forecast_weak_source",
        "event_date": event_date,
        "available_at": _iso(available_at),
        "forecast_peak_c": forecast_peak_c,
        "forecast_peak_valid_time": _text(forecast_peak_valid_time),
        "forecast_sigma_c": forecast_sigma_c,
        "raw_sha256": raw_sha256,
        "forecast_weather_regime": forecast_weather_regime,
        "provenance": dict(provenance),
    }


def _official_revisions(*, curve_root: Path, event_date: str, decision: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected: dict[str, tuple[datetime, str, dict[str, Any]]] = {}
    trace: list[dict[str, Any]] = []
    for provider in _OFFICIAL_SOURCE_IDS:
        directory = curve_root / provider / "daily_peak_revisions"
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                trace.append({"source_id": provider, "path": str(path), "included": False, "exclusion_reason": "official_peak_revision_unreadable"})
                continue
            available = _time(row.get("available_at"))
            peak = _finite(row.get("forecast_peak_c"))
            revision_id = _text(row.get("source_revision_id"))
            raw_sha = _text(row.get("raw_sha256"))
            trace_row = {"source_id": provider, "path": str(path), "source_revision_id": revision_id, "included": False, "exclusion_reason": ""}
            if _text(row.get("source_id")) != provider or _text(row.get("event_date"))[:10] != event_date:
                trace_row["exclusion_reason"] = "official_peak_revision_identity_mismatch"
            elif available is None or available > decision:
                trace_row["exclusion_reason"] = "official_peak_revision_after_asof_or_time_invalid"
            elif peak is None or not raw_sha or not revision_id:
                trace_row["exclusion_reason"] = "official_peak_revision_forecast_or_lineage_missing"
            else:
                candidate = _clean_revision(
                    source_id=provider,
                    revision_id=revision_id,
                    event_date=event_date,
                    available_at=available,
                    forecast_peak_c=peak,
                    raw_sha256=raw_sha,
                    forecast_sigma_c=_finite(row.get("forecast_sigma_c")),
                    forecast_weather_regime=_text(row.get("forecast_weather_regime")).lower(),
                    forecast_peak_valid_time=_text(row.get("forecast_peak_valid_time")),
                    provenance={
                        "kind": "official_complete_daily_curve_revision",
                        "revision_path": str(path),
                        "strict_asof_tier_a": row.get("strict_asof_tier_a") is True,
                    },
                )
                previous = selected.get(provider)
                if previous is None or (available, revision_id) > (previous[0], previous[1]):
                    if previous is not None:
                        previous[2]["_trace"]["included"] = False
                        previous[2]["_trace"]["exclusion_reason"] = "superseded_by_latest_visible_official_revision"
                    candidate["_trace"] = trace_row
                    selected[provider] = (available, revision_id, candidate)
                else:
                    trace_row["exclusion_reason"] = "superseded_by_latest_visible_official_revision"
            trace.append(trace_row)
    revisions: list[dict[str, Any]] = []
    for provider in _OFFICIAL_SOURCE_IDS:
        item = selected.get(provider)
        if item is None:
            continue
        row = item[2]
        row["_trace"]["included"] = True
        row.pop("_trace", None)
        revisions.append(row)
    return revisions, trace


def _canonical_independent_source_id(row: Mapping[str, Any]) -> str:
    payload = _mapping(row.get("payload"))
    candidate = _text(payload.get("weak_source_id") or row.get("weak_source_id") or row.get("source_id"))
    if candidate in _INDEPENDENT_SOURCE_IDS:
        return candidate
    source = _text(row.get("source_id")).lower()
    if "taf" in source:
        return "taf"
    if source in {"wu", "wunderground", "wunderground_forecast"}:
        return "wu"
    if source in {"twc", "weather_com", "weather_channel"}:
        return "twc"
    return ""


def _independent_revisions(*, source_observations: Iterable[Mapping[str, Any]], event_date: str, decision: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
    trace: list[dict[str, Any]] = []
    for raw in source_observations:
        row = _mapping(raw)
        payload = _mapping(row.get("payload"))
        source_id = _canonical_independent_source_id(row)
        if not source_id:
            continue
        available = _time(row.get("response_completed_at") or row.get("fetched_at") or payload.get("available_at"))
        issued = _time(row.get("issue_time_utc") or payload.get("issue_time_utc"))
        peak = _finite(row.get("forecast_peak_c") or payload.get("forecast_peak_c"))
        revision_id = _text(row.get("source_event_id") or row.get("source_revision_id"))
        revision_kind = _text(row.get("forecast_revision_kind") or payload.get("forecast_revision_kind")).lower()
        raw_sha = _text(row.get("raw_sha256") or payload.get("raw_sha256"))
        revision_event_date = _text(row.get("event_date") or payload.get("event_date"))[:10]
        trace_row = {
            "source_id": source_id,
            "source_revision_id": revision_id,
            "forecast_revision_kind": revision_kind,
            "forecast_issue_time_utc": _iso(issued) if issued else "",
            "included": False,
            "exclusion_reason": "",
        }
        if _text(row.get("source_class") or payload.get("source_class")) != "forecast_weak_source":
            trace_row["exclusion_reason"] = "independent_source_class_not_forecast_weak"
        elif revision_event_date != event_date:
            trace_row["exclusion_reason"] = "independent_forecast_event_date_mismatch"
        elif available is None or available > decision:
            trace_row["exclusion_reason"] = "independent_forecast_after_asof_or_time_invalid"
        elif peak is None or not revision_id or not raw_sha:
            trace_row["exclusion_reason"] = "independent_forecast_peak_or_lineage_missing"
        else:
            candidate = _clean_revision(
                source_id=source_id,
                revision_id=revision_id,
                event_date=event_date,
                available_at=available,
                forecast_peak_c=peak,
                raw_sha256=raw_sha,
                forecast_sigma_c=_finite(row.get("forecast_sigma_c") or payload.get("forecast_sigma_c")),
                forecast_weather_regime=_text(payload.get("forecast_weather_regime")).lower(),
                forecast_peak_valid_time=_text(
                    row.get("forecast_peak_valid_time")
                    or payload.get("forecast_peak_valid_time")
                ),
                provenance={
                    "kind": "independent_forecast_source_observation",
                    "source_observation_id": _text(row.get("source_observation_id")),
                    "strict_asof_tier_a": payload.get("response_headers_observed") is True,
                    "forecast_revision_kind": revision_kind,
                    "forecast_issue_time_utc": _iso(issued) if issued else "",
                },
            )
            # TAF meteorological issue time defines revision order. A report
            # first discovered later is still archived, but an older issue
            # must not overwrite a newer TAF already visible to the system.
            # At an identical issue time, COR > AMD > original.
            if source_id == "taf" and issued is not None:
                selection_key: tuple[Any, ...] = taf_revision_order_key(
                    issue_time_utc=_iso(issued),
                    revision_kind=revision_kind,
                    first_seen_at=_iso(available),
                    revision_id=revision_id,
                )
            else:
                selection_key = (available.timestamp(), 0, available.timestamp(), revision_id)
            previous = selected.get(source_id)
            if previous is None or selection_key > previous[0]:
                if previous is not None:
                    previous[1]["_trace"]["included"] = False
                    previous[1]["_trace"]["exclusion_reason"] = "superseded_by_latest_visible_independent_revision"
                candidate["_trace"] = trace_row
                selected[source_id] = (selection_key, candidate)
            else:
                trace_row["exclusion_reason"] = "superseded_by_latest_visible_independent_revision"
        trace.append(trace_row)
    revisions: list[dict[str, Any]] = []
    for source_id in _INDEPENDENT_SOURCE_IDS:
        item = selected.get(source_id)
        if item is None:
            continue
        row = item[1]
        row["_trace"]["included"] = True
        row.pop("_trace", None)
        revisions.append(row)
    return revisions, trace


def build_joint_weak_live_forecast_evidence(
    *, event_date: str, decision_as_of: str, source_observations: Iterable[Mapping[str, Any]] | None = None,
    curve_root: Path | str = DEFAULT_CURVE_ROOT,
) -> dict[str, Any]:
    """Build one immutable forecast-only source revision snapshot for live use."""

    day = _text(event_date)[:10]
    decision = _time(decision_as_of)
    if not day or decision is None or not _season(day):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["joint_weak_live_event_date_or_asof_invalid"], "read_only": True}
    root = Path(curve_root).expanduser().resolve()
    official, official_trace = _official_revisions(curve_root=root, event_date=day, decision=decision)
    independent_load = None
    if source_observations is None:
        independent_load = load_live_independent_weak_source_observations(event_date=day)
        source_observations = independent_load.get("source_observations") or []
    independent, independent_trace = _independent_revisions(source_observations=source_observations, event_date=day, decision=decision)
    revisions = [*official, *independent]
    revisions.sort(key=lambda row: (SOURCE_UNIVERSE.index(str(row["source_id"])), str(row["available_at"]), str(row["source_revision_id"])))
    regimes = {
        _text(row.get("forecast_weather_regime")).lower()
        for row in revisions
        if _text(row.get("forecast_weather_regime")).lower() in _FORECAST_REGIMES - {"unknown_forecast_regime", "mixed_forecast_regime"}
    }
    regime = "unknown_forecast_regime" if not regimes else next(iter(regimes)) if len(regimes) == 1 else "mixed_forecast_regime"
    context = {
        "season": _season(day),
        "weather_regime": regime,
        "lead_bucket": _lead_bucket(day, decision),
    }
    identity = {
        "schema": SCHEMA_VERSION,
        "event_date": day,
        "decision_as_of": _iso(decision),
        "forecast_context": context,
        "source_revisions": revisions,
    }
    snapshot_sha = _sha(identity)
    available = [source for source in SOURCE_UNIVERSE if source in {row["source_id"] for row in revisions}]
    return {
        **identity,
        "ok": True,
        "state": "available" if revisions else "neutral_empty_source_set",
        "forecast_evidence_snapshot_id": f"forev_{snapshot_sha[:24]}",
        "forecast_evidence_snapshot_sha256": snapshot_sha,
        "available_source_ids": available,
        "missing_source_ids": [source for source in SOURCE_UNIVERSE if source not in available],
        "source_trace": [*official_trace, *independent_trace],
        "independent_source_load": independent_load,
        "read_only": True,
        "boundary": {
            "forecast_only": True,
            "d0_observation_or_trajectory_not_consumed": True,
            "market_or_trading_not_consumed": True,
            "strict_asof_training_tier_is_not_live_ingest_gate": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def build_forecast_peak_timing_prior(
    *, forecast_evidence: Mapping[str, Any], joint_weak_likelihood: Mapping[str, Any],
) -> dict[str, Any]:
    """Project forecast-only peak timestamps using the weak model's own weights.

    This is timing context, not another temperature likelihood. Missing timing
    is explicit and never blocks the physical-X probability chain.
    """

    evidence = _mapping(forecast_evidence)
    weak = _mapping(joint_weak_likelihood)
    weights = {
        (_text(row.get("source_id")), _text(row.get("source_revision_id"))):
        float(_finite(row.get("information_weight")) or 0.0)
        for row in weak.get("source_expert_trace") or []
        if isinstance(row, Mapping)
    }
    slot_weights: dict[str, float] = {}
    source_rows: list[dict[str, Any]] = []
    for raw in evidence.get("source_revisions") or []:
        row = _mapping(raw)
        peak_at = _time(row.get("forecast_peak_valid_time"))
        weight = weights.get((_text(row.get("source_id")), _text(row.get("source_revision_id"))), 0.0)
        if peak_at is None or weight <= 0.0:
            continue
        local = peak_at.astimezone(ZoneInfo("Asia/Shanghai"))
        if local.date().isoformat() != _text(evidence.get("event_date"))[:10]:
            continue
        minute = 30 if local.minute >= 15 and local.minute < 45 else 0
        if local.minute >= 45:
            local = local.replace(minute=0, second=0, microsecond=0)
            local += timedelta(hours=1)
        else:
            local = local.replace(minute=minute, second=0, microsecond=0)
        slot = _iso(local.astimezone(timezone.utc))
        slot_weights[slot] = slot_weights.get(slot, 0.0) + weight
        source_rows.append({
            "source_id": _text(row.get("source_id")),
            "source_revision_id": _text(row.get("source_revision_id")),
            "forecast_peak_at": _iso(peak_at),
            "timing_weight": weight,
        })
    total = sum(slot_weights.values())
    if total <= 0.0:
        return {
            "schema": "forecast_peak_timing_prior_v1",
            "state": "na",
            "na_reason": "forecast_sources_with_peak_timestamp_missing",
            "forecast_evidence_snapshot_id": _text(evidence.get("forecast_evidence_snapshot_id")),
            "distribution": [],
            "forecast_only": True,
            "does_not_update_physical_x": True,
        }
    distribution = [
        {"peak_at": slot, "probability": weight / total}
        for slot, weight in sorted(slot_weights.items())
    ]
    mode = max(distribution, key=lambda row: (float(row["probability"]), row["peak_at"]))
    core = {
        "schema": "forecast_peak_timing_prior_v1",
        "state": "available",
        "forecast_evidence_snapshot_id": _text(evidence.get("forecast_evidence_snapshot_id")),
        "distribution": distribution,
        "mode_peak_at": mode["peak_at"],
        "source_timing_trace": source_rows,
        "forecast_only": True,
        "does_not_update_physical_x": True,
    }
    return {**core, "timing_prior_sha256": _sha(core)}


def build_live_joint_weak_likelihood(
    *, temperature_grid_c: Sequence[Any], event_date: str, decision_as_of: str,
    source_observations: Iterable[Mapping[str, Any]] | None = None, curve_root: Path | str = DEFAULT_CURVE_ROOT,
    artifact_path: Path | str | None = None,
) -> dict[str, Any]:
    """Evaluate the approved joint weak model from a live forecast-only snapshot."""

    evidence = build_joint_weak_live_forecast_evidence(
        event_date=event_date,
        decision_as_of=decision_as_of,
        source_observations=source_observations,
        curve_root=curve_root,
    )
    artifact_load = load_approved_joint_weak_artifact(artifact_path)
    if evidence.get("ok") is not True or artifact_load.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": [
                *list(evidence.get("block_reasons") or []),
                *list(artifact_load.get("block_reasons") or []),
            ],
            "forecast_evidence": evidence,
            "artifact_load": artifact_load,
            "read_only": True,
        }
    runtime = build_joint_forecast_weak_likelihood(
        temperature_grid_c=temperature_grid_c,
        source_revisions=evidence["source_revisions"],
        artifact=artifact_load["artifact"],
        forecast_context={
            **dict(evidence["forecast_context"]),
            "forecast_evidence_snapshot_id": evidence["forecast_evidence_snapshot_id"],
        },
        event_date=evidence["event_date"],
        decision_as_of=evidence["decision_as_of"],
    )
    timing_prior = build_forecast_peak_timing_prior(
        forecast_evidence=evidence,
        joint_weak_likelihood=runtime,
    ) if runtime.get("ok") is True else {
        "schema": "forecast_peak_timing_prior_v1",
        "state": "na",
        "na_reason": "joint_weak_likelihood_unavailable",
        "distribution": [],
        "forecast_only": True,
        "does_not_update_physical_x": True,
    }
    background_context = (
        build_forecast_background_context(
            forecast_evidence_snapshot_id=_text(evidence.get("forecast_evidence_snapshot_id")),
            decision_as_of=_text(evidence.get("decision_as_of")),
            source_revisions=evidence.get("source_revisions") or [],
            source_expert_trace=runtime.get("source_expert_trace") or [],
            missing_source_ids=evidence.get("missing_source_ids") or [],
        )
        if runtime.get("ok") is True
        else {}
    )
    runtime = {
        **runtime,
        "forecast_peak_timing_prior": timing_prior,
        "forecast_background_context": background_context,
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": runtime.get("ok") is True,
        "state": runtime.get("state"),
        "block_reasons": list(runtime.get("block_reasons") or []),
        "forecast_evidence": evidence,
        "artifact_load": artifact_load,
        "joint_weak_likelihood": runtime,
        "forecast_peak_timing_prior": timing_prior,
        "forecast_background_context": background_context,
        "read_only": True,
        "boundary": {
            "forecast_only": True,
            "uses_approved_joint_weak_artifact": True,
            "legacy_v31_or_open_meteo_used": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def bind_live_joint_weak_likelihood_for_production(
    weak_envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a freshly evaluated forecast-only likelihood to production.

    Artifact and evidence hashes are retained and no source weight or
    probability is changed by this execution-boundary operation.
    """

    envelope = dict(weak_envelope) if isinstance(weak_envelope, Mapping) else {}
    runtime = dict(_mapping(envelope.get("joint_weak_likelihood")))
    boundary = dict(_mapping(envelope.get("boundary")))
    runtime_boundary = dict(_mapping(runtime.get("boundary")))
    if (
        envelope.get("ok") is not True
        or runtime.get("ok") is not True
        or _text(runtime.get("state")) != "available"
        or runtime.get("shadow_only") is not True
        or not _text(runtime.get("artifact_sha256"))
        or not _text(runtime.get("forecast_evidence_snapshot_id"))
        or runtime.get("market_data_consumed") is not False
        or runtime.get("observation_data_consumed") is not False
        or boundary.get("forecast_only") is not True
    ):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["joint_weak_live_result_not_bindable_for_production"],
            "read_only": True,
        }
    production_runtime = {
        **runtime,
        "production_runtime_eligible": True,
        "shadow_only": False,
        "boundary": {
            **runtime_boundary,
            "production_probability_input": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }
    return {
        **envelope,
        "state": "available",
        "joint_weak_likelihood": production_runtime,
        "production_runtime_eligible": True,
        "shadow_only": False,
        "boundary": {
            **boundary,
            "production_probability_input": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = [
    "DEFAULT_CURVE_ROOT",
    "SCHEMA_VERSION",
    "bind_live_joint_weak_likelihood_for_production",
    "build_joint_weak_live_forecast_evidence",
    "build_live_joint_weak_likelihood",
    "build_forecast_peak_timing_prior",
]
