"""Content-addressed AWC authority for retrospective P1 replay.

Historical replay must not infer its truth source from an active market or
calibration snapshot.  In particular, an old AMSC snapshot cannot be relabelled
in memory as AWC.  This module therefore requires a separately signed-like
(content-addressed) authority object, binds every consumed formal observation
to it, and rejects absent, mixed, fallback, late, or unreviewed-correction
evidence before P1 compilation starts.

The resolver is deliberately pure: it opens no database, makes no network
request, and performs no mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256


SCHEMA = "bayes_basket_target_p1_replay_awc_source_authority_v1"
AUTHORITY_STATE = "FORMAL_AWC_HISTORICAL_REPLAY"
SOURCE_LANE = "AWC"
SOURCE_PUBLISHER = "AVIATION_WEATHER_CENTER"
AUTHORITY_KEY = "p1_replay_source_authority"
CORRECTION_STATES = frozenset({"NONE", "COR", "AMD"})
MANUAL_REVIEW_NOT_REQUIRED = "NOT_REQUIRED"
MANUAL_REVIEW_ACCEPTED = "ACCEPTED"


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"p1_replay_authority_{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"p1_replay_authority_{name}_invalid")
    return result


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(name, value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"p1_replay_authority_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"p1_replay_authority_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_evidence_sha256(canonical: Mapping[str, Any]) -> str:
    # Older retrospective receipts named this field ``evidence_sha256`` while
    # current canonical exports name their immutable receipt ``receipt_sha256``.
    # Either is an explicit producer-side identity; an arbitrary payload digest
    # computed by this replay is intentionally not accepted as a replacement.
    return _sha(
        "canonical_evidence_sha256",
        canonical.get("evidence_sha256") or canonical.get("receipt_sha256"),
    )


def _authority_identity(authority: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in authority.items()
        if str(key) != "content_sha256"
    }


def build_awc_official_p1_replay_authority(
    *,
    authority_id: str,
    station_id: str,
    event_date: str,
    canonical_evidence_sha256: str,
    source_snapshot_sha256: str,
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build an explicit AWC authority object from already retained evidence.

    This helper never derives source lane, timing, correction state, or raw
    hashes from a legacy snapshot.  Callers must provide each binding
    explicitly; ``resolve_awc_official_p1_replay_authority`` later proves that
    the supplied canonical observations are the exact same bindings.
    """

    normalized_observations = [dict(item) for item in observations]
    if not normalized_observations:
        raise ContractError("p1_replay_authority_observation_bindings_missing")
    identity = {
        "schema": SCHEMA,
        "authority_id": _text("id", authority_id),
        "authority_state": AUTHORITY_STATE,
        "source_lane": SOURCE_LANE,
        "source_publisher": SOURCE_PUBLISHER,
        "station_id": _text("station_id", station_id).upper(),
        "event_date_local": _text("event_date", event_date),
        "canonical_evidence_sha256": _sha(
            "canonical_evidence_sha256", canonical_evidence_sha256
        ),
        "source_snapshot_sha256": _sha(
            "source_snapshot_sha256", source_snapshot_sha256
        ),
        "fallback_used": False,
        "fallback_source_lane": None,
        "observation_bindings": normalized_observations,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _binding_identity(
    binding: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
    decision_at: datetime,
) -> dict[str, Any]:
    station = _text("binding_station_id", binding.get("station_id")).upper()
    if station != station_id:
        raise ContractError("p1_replay_authority_binding_station_mismatch")
    if _text("binding_event_date", binding.get("event_date_local")) != event_date:
        raise ContractError("p1_replay_authority_binding_event_date_mismatch")
    if _text("binding_source_lane", binding.get("source_lane")).upper() != SOURCE_LANE:
        raise ContractError("p1_replay_authority_binding_not_awc")
    report_type = _text("binding_report_type", binding.get("report_type")).upper()
    if report_type not in {"METAR", "SPECI"}:
        raise ContractError("p1_replay_authority_binding_report_type_invalid")
    published_at = _utc("published_at_utc", binding.get("published_at_utc"))
    received_at = _utc("received_at_utc", binding.get("received_at_utc"))
    if published_at > received_at:
        raise ContractError("p1_replay_authority_publication_after_receipt")
    if received_at > decision_at:
        raise ContractError("p1_replay_authority_received_after_decision")
    correction_state = _text(
        "correction_status", binding.get("correction_status")
    ).upper()
    if correction_state not in CORRECTION_STATES:
        raise ContractError("p1_replay_authority_correction_status_invalid")
    manual_review_status = _text(
        "manual_review_status", binding.get("manual_review_status")
    ).upper()
    review_receipt = binding.get("manual_review_receipt_sha256")
    if correction_state == "NONE":
        if manual_review_status != MANUAL_REVIEW_NOT_REQUIRED or review_receipt not in (None, ""):
            raise ContractError("p1_replay_authority_uncorrected_review_state_invalid")
    else:
        # COR/AMD are quarantined by default.  A retained manual acceptance
        # receipt is the only way a reviewed station-day can be replayed.
        if manual_review_status != MANUAL_REVIEW_ACCEPTED:
            raise ContractError("p1_replay_authority_correction_quarantined")
        _sha("manual_review_receipt_sha256", review_receipt)
    return {
        "observation_id": _text("binding_observation_id", binding.get("observation_id")),
        "station_id": station,
        "event_date_local": event_date,
        "source_lane": SOURCE_LANE,
        "report_type": report_type,
        "raw_sha256": _sha("binding_raw_sha256", binding.get("raw_sha256")),
        "published_at_utc": _utc_text(published_at),
        "received_at_utc": _utc_text(received_at),
        "correction_status": correction_state,
        "manual_review_status": manual_review_status,
        "manual_review_receipt_sha256": (
            _sha("manual_review_receipt_sha256", review_receipt)
            if correction_state != "NONE"
            else None
        ),
    }


def resolve_awc_official_p1_replay_authority(
    canonical: Mapping[str, Any],
    *,
    observations: Sequence[Mapping[str, Any]],
    station_id: str,
    event_date: str,
    decision_at_utc: str,
) -> dict[str, Any]:
    """Validate and bind one formal AWC authority to a P1 replay scope.

    Returned data is a normalized authority receipt whose SHA can be retained
    by the replay output.  Missing authority is intentionally an error rather
    than a compatibility path: legacy AMSC-era replay snapshots must be
    re-enveloped from official AWC source evidence first.
    """

    raw_authority = canonical.get(AUTHORITY_KEY)
    if not isinstance(raw_authority, Mapping):
        raise ContractError("p1_replay_authority_missing")
    authority = dict(raw_authority)
    supplied_sha = _sha("content_sha256", authority.get("content_sha256"))
    if content_sha256(_authority_identity(authority)) != supplied_sha:
        raise ContractError("p1_replay_authority_content_hash_invalid")

    station = _text("station_id", station_id).upper()
    event_day = _text("event_date", event_date)
    decision_at = _utc("decision_at_utc", decision_at_utc)
    if authority.get("schema") != SCHEMA:
        raise ContractError("p1_replay_authority_schema_invalid")
    if _text("authority_id", authority.get("authority_id")) == "":  # defensive for clarity
        raise ContractError("p1_replay_authority_id_missing")
    if authority.get("authority_state") != AUTHORITY_STATE:
        raise ContractError("p1_replay_authority_state_invalid")
    if _text("source_lane", authority.get("source_lane")).upper() != SOURCE_LANE:
        raise ContractError("p1_replay_authority_not_awc")
    if _text("source_publisher", authority.get("source_publisher")).upper() != SOURCE_PUBLISHER:
        raise ContractError("p1_replay_authority_not_official_awc")
    if _text("station_id", authority.get("station_id")).upper() != station:
        raise ContractError("p1_replay_authority_station_mismatch")
    if _text("event_date", authority.get("event_date_local")) != event_day:
        raise ContractError("p1_replay_authority_event_date_mismatch")
    if authority.get("fallback_used") is not False or authority.get("fallback_source_lane") not in (None, ""):
        raise ContractError("p1_replay_authority_fallback_forbidden")
    if _sha("canonical_evidence_sha256", authority.get("canonical_evidence_sha256")) != _canonical_evidence_sha256(canonical):
        raise ContractError("p1_replay_authority_canonical_evidence_mismatch")
    _sha("source_snapshot_sha256", authority.get("source_snapshot_sha256"))

    raw_bindings = authority.get("observation_bindings")
    if not isinstance(raw_bindings, list) or not raw_bindings:
        raise ContractError("p1_replay_authority_observation_bindings_missing")
    bindings = [
        _binding_identity(
            item,
            station_id=station,
            event_date=event_day,
            decision_at=decision_at,
        )
        for item in raw_bindings
        if isinstance(item, Mapping)
    ]
    if len(bindings) != len(raw_bindings):
        raise ContractError("p1_replay_authority_binding_mapping_required")
    binding_by_id = {item["observation_id"]: item for item in bindings}
    if len(binding_by_id) != len(bindings):
        raise ContractError("p1_replay_authority_binding_duplicate")

    formal_by_id = {
        _text("observation_id", item.get("observation_id")): item
        for item in observations
    }
    if not formal_by_id or set(formal_by_id) != set(binding_by_id):
        raise ContractError("p1_replay_authority_binding_scope_mismatch")
    for observation_id, observation in formal_by_id.items():
        binding = binding_by_id[observation_id]
        if (
            _text("observation_station_id", observation.get("station_id")).upper() != station
            or _text("observation_event_date", observation.get("event_date_local")) != event_day
            or _text("observation_source_lane", observation.get("source_lane")).upper() != SOURCE_LANE
            or _text("observation_report_type", observation.get("report_type")).upper() != binding["report_type"]
            or _sha("observation_raw_sha256", observation.get("raw_sha256")) != binding["raw_sha256"]
        ):
            raise ContractError("p1_replay_authority_observation_binding_mismatch")
        if bool(observation.get("correction_suspected")) and binding["correction_status"] == "NONE":
            raise ContractError("p1_replay_authority_correction_state_mismatch")

    # Preserve the exact, already self-verified authority object in the replay
    # receipt.  Returning a normalized reconstruction here would make the
    # retained body differ from the body covered by ``content_sha256``.
    return authority


__all__ = [
    "AUTHORITY_KEY",
    "AUTHORITY_STATE",
    "CORRECTION_STATES",
    "MANUAL_REVIEW_ACCEPTED",
    "MANUAL_REVIEW_NOT_REQUIRED",
    "SCHEMA",
    "SOURCE_LANE",
    "SOURCE_PUBLISHER",
    "build_awc_official_p1_replay_authority",
    "resolve_awc_official_p1_replay_authority",
]
