"""Read-only, content-addressed audit of the 45-station TAF ↔ P0-M join.

The exact join is a calibration-only bridge.  This module therefore accepts
only already persisted pointers (TAF capture, forecast revision and AWC truth)
and never replays, fetches or synthesizes any of them.  In particular an
``AMD`` or ``COR`` is an exclusion for the whole station day, not a revision
that can be selected as a replacement feature.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .settlement_time import settlement_day_window
from .taf_oos_contract import HISTORICAL_OOS_TEMPORAL_SCOPE


SCHEMA = "bayes_basket_target_taf_p0m_exact_join_45station_audit_v1"
EXACT_JOIN_SCHEMA = "taf_native_ensemble_strict_oos_exact_join_v1"
P0_INBOX_SCHEMA = "bayes_basket_target_p0_inbox_v1"
MIN_TAF_NATIVE_OOS_SAMPLE_N = 24
MAX_STATIONS = 45
MAX_EXACT_JOINS = 45
MAX_SAMPLES_PER_JOIN = 366
_HEX = frozenset("0123456789abcdef")
_ALLOWED_TAF_SOURCE_CLASSES = frozenset({
    "official_forward_native_receipt",
    "official_date_addressed_historical_replay",
})


def _text(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"taf_exact_join_audit_{name}_required")
    return text


def _sha(name: str, value: Any) -> str:
    text = _text(name, value).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise ContractError(f"taf_exact_join_audit_{name}_invalid")
    return text


def _verify_hash(value: Mapping[str, Any], *, name: str) -> None:
    supplied = _sha(f"{name}_content_sha256", value.get("content_sha256"))
    identity = {key: child for key, child in value.items() if key != "content_sha256"}
    if content_sha256(identity) != supplied:
        raise ContractError(f"taf_exact_join_audit_{name}_content_sha_mismatch")


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"taf_exact_join_audit_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"taf_exact_join_audit_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _event_is_historical_closed(
    *, event_date: str, settlement_timezone: str, as_of: datetime
) -> bool:
    """Return whether a station event is eligible to be an OOS sample."""

    closed_at = _utc(
        "sample_settlement_closed",
        settlement_day_window(event_date, settlement_timezone)[
            "target_day_end_utc"
        ],
    )
    return closed_at <= as_of


def _scope_from_join(value: Mapping[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        _text("join_station_id", value.get("station_id")).upper(),
        _text("join_season_bin", value.get("season_bin")),
        _text("join_lead_bucket", value.get("lead_bucket")),
        _text("join_run_cycle", value.get("run_cycle")),
        _text("join_settlement_definition", value.get("settlement_definition")),
        _text("join_settlement_timezone", value.get("settlement_timezone")),
    )


def _registry_index(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    if len(rows) != MAX_STATIONS:
        raise ContractError("taf_exact_join_audit_station_count_not_45")
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ContractError("taf_exact_join_audit_station_registry_row_invalid")
        station = _text("station_id", row.get("station_id")).upper()
        if station in indexed:
            raise ContractError("taf_exact_join_audit_station_registry_duplicate")
        if _text("station_source_lane", row.get("source_lane")).upper() != "AWC":
            raise ContractError("taf_exact_join_audit_station_lane_not_awc")
        _text("station_timezone", row.get("timezone_name"))
        _text("station_settlement_definition", row.get("settlement_definition"))
        indexed[station] = row
    return indexed


def _p0_index(inbox: Mapping[str, Any]) -> dict[str, tuple[str, str, str, str, str]]:
    if inbox.get("schema") != P0_INBOX_SCHEMA:
        raise ContractError("taf_exact_join_audit_p0_inbox_schema_invalid")
    _verify_hash(inbox, name="p0_inbox")
    packets = inbox.get("packets")
    if not isinstance(packets, list) or len(packets) > MAX_STATIONS:
        raise ContractError("taf_exact_join_audit_p0_inbox_rows_invalid")
    result: dict[str, tuple[str, str, str, str, str]] = {}
    for wrapper in packets:
        if not isinstance(wrapper, Mapping) or not isinstance(wrapper.get("packet"), Mapping):
            raise ContractError("taf_exact_join_audit_p0_packet_invalid")
        packet = wrapper["packet"]
        if _text("p0_stage", packet.get("stage")).upper() != "P0":
            raise ContractError("taf_exact_join_audit_p0_stage_invalid")
        station = _text("p0_station_id", packet.get("station_id")).upper()
        envelope = packet.get("native_probability_envelope")
        if not isinstance(envelope, Mapping):
            raise ContractError("taf_exact_join_audit_p0_envelope_missing")
        if _text("p0_envelope_station_id", envelope.get("station_id")).upper() != station:
            raise ContractError("taf_exact_join_audit_p0_station_mismatch")
        lineage = envelope.get("lineage")
        if not isinstance(lineage, Mapping):
            raise ContractError("taf_exact_join_audit_p0_lineage_missing")
        scope = (
            _text("p0_season_bin", lineage.get("season_bin")),
            _text("p0_lead_bucket", lineage.get("lead_bucket")),
            _text("p0_run_cycle", lineage.get("cross_calibration_scope_run_cycle") or lineage.get("run_cycle")),
            _text("p0_settlement_definition", lineage.get("settlement_definition")),
            _text("p0_settlement_timezone", packet.get("settlement_timezone")),
        )
        if station in result:
            raise ContractError("taf_exact_join_audit_p0_station_duplicate")
        result[station] = scope
    return result


def _pointer_indexes(
    pointers: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[tuple[str, str], list[Mapping[str, Any]]]]:
    by_content: dict[str, Mapping[str, Any]] = {}
    corrections: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for pointer in pointers:
        if not isinstance(pointer, Mapping):
            raise ContractError("taf_exact_join_audit_pointer_invalid")
        content = _sha("taf_capture_content", pointer.get("content_sha256"))
        if content in by_content:
            raise ContractError("taf_exact_join_audit_taf_capture_duplicate")
        kind = _text("taf_capture_kind", pointer.get("kind")).upper()
        if kind not in {"FORWARD", "HISTORICAL", "HISTORICAL_QUARANTINE"}:
            raise ContractError("taf_exact_join_audit_taf_capture_kind_invalid")
        station = _text("taf_capture_station_id", pointer.get("station_id")).upper()
        event_date = _text("taf_capture_event_date", pointer.get("event_date"))
        _text("taf_capture_timezone", pointer.get("settlement_timezone"))
        source_class = _text("taf_capture_source_class", pointer.get("source_class"))
        if source_class not in _ALLOWED_TAF_SOURCE_CLASSES:
            raise ContractError("taf_exact_join_audit_taf_capture_source_class_invalid")
        _sha("taf_capture_source_object", pointer.get("source_object_sha256"))
        revision_kind = str(pointer.get("revision_kind") or "").upper()
        if kind in {"FORWARD", "HISTORICAL_QUARANTINE"}:
            _text("taf_capture_revision_id", pointer.get("revision_id"))
            if revision_kind not in {"TAF", "AMD", "COR"}:
                raise ContractError("taf_exact_join_audit_revision_kind_invalid")
            if revision_kind in {"AMD", "COR"}:
                corrections[(station, event_date)].append(pointer)
        elif revision_kind:
            raise ContractError("taf_exact_join_audit_historical_revision_forged")
        by_content[content] = pointer
    for rows in corrections.values():
        rows.sort(key=lambda item: (
            str(item.get("provider_available_at_utc") or ""),
            str(item.get("revision_id") or ""),
        ))
    return by_content, corrections


def _truth_index(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ContractError("taf_exact_join_audit_truth_pointer_invalid")
        truth_id = _text("truth_id", row.get("truth_id"))
        if truth_id in indexed:
            raise ContractError("taf_exact_join_audit_truth_pointer_duplicate")
        if _text("truth_source_lane", row.get("source_lane")).upper() != "AWC":
            raise ContractError("taf_exact_join_audit_truth_lane_not_awc")
        if _text("truth_qc_status", row.get("qc_status")).upper() != "ACCEPTED":
            raise ContractError("taf_exact_join_audit_truth_not_accepted")
        if _text("truth_authority_scope", row.get("authority_scope")) not in {
            "FORMAL_NATIVE_ELIGIBLE", "NO_LIVE_REPLAY_CANDIDATE",
        }:
            raise ContractError("taf_exact_join_audit_truth_authority_invalid")
        if row.get("latest_accepted") != True:  # SQLite returns integer 1.
            raise ContractError("taf_exact_join_audit_truth_not_current_revision")
        _text("truth_station_id", row.get("station_id"))
        _text("truth_event_date", row.get("event_date"))
        _text("truth_settlement_definition", row.get("settlement_definition"))
        _sha("truth_source_payload", row.get("source_payload_sha256"))
        indexed[truth_id] = row
    return indexed


def _validate_join_document(
    document: Mapping[str, Any],
    *,
    path: str,
    registry: Mapping[str, Mapping[str, Any]],
    captures: Mapping[str, Mapping[str, Any]],
    truths: Mapping[str, Mapping[str, Any]],
    corrections: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
    as_of: datetime,
) -> tuple[str, dict[str, Any]]:
    if document.get("schema") != EXACT_JOIN_SCHEMA:
        raise ContractError("taf_exact_join_audit_schema_invalid")
    if document.get("source_class") != "strict_oos_native_p0_taf_truth_exact_join":
        raise ContractError("taf_exact_join_audit_source_class_invalid")
    _verify_hash(document, name="exact_join")
    station, season, lead, run, settlement_definition, timezone_name = _scope_from_join(document)
    registry_row = registry.get(station)
    if registry_row is None:
        raise ContractError("taf_exact_join_audit_station_not_in_registry")
    # ``calibration_station_registry`` is an immutable legacy identity
    # snapshot.  Its timezone is intentionally not an authority for this
    # exact join (the join's P0/TAF/truth scope already carries the bound
    # settlement timezone); only its day-definition is a stable key here.
    if _text(
        "registry_settlement_definition", registry_row.get("settlement_definition")
    ) != settlement_definition:
        raise ContractError("taf_exact_join_audit_station_scope_mismatch")
    samples = document.get("samples")
    has_quarantine_evidence = (
        "excluded_correction_samples" in document
        and "excluded_correction_sample_count" in document
    )
    excluded = document.get("excluded_correction_samples", [])
    if (
        not isinstance(samples, list)
        or len(samples) > MAX_SAMPLES_PER_JOIN
        or int(document.get("sample_count", -1)) != len(samples)
        or not isinstance(excluded, list)
        or (
            has_quarantine_evidence
            and int(document.get("excluded_correction_sample_count", -1))
            != len(excluded)
        )
    ):
        raise ContractError("taf_exact_join_audit_sample_count_invalid")
    seen_days: set[str] = set()
    sample_errors: list[str] = []
    if not has_quarantine_evidence:
        # Older artifacts cannot establish that an AMD/COR exclusion scan ran.
        # They remain auditable but are never treated as a promotable exact
        # scope until rebuilt from the persisted facts by the current builder.
        sample_errors.append("amd_cor_quarantine_evidence_missing")
    lineage_pointers: list[dict[str, str]] = []
    unquarantined_correction_pointers: list[dict[str, Any]] = []
    non_oos_observational_event_dates: list[str] = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise ContractError("taf_exact_join_audit_sample_invalid")
        event_date = _text("sample_event_date", sample.get("event_date"))
        if event_date in seen_days:
            raise ContractError("taf_exact_join_audit_sample_event_date_duplicate")
        seen_days.add(event_date)
        if (
            _text("sample_station_id", sample.get("station_id")).upper() != station
            or _text("sample_timezone", sample.get("settlement_timezone")) != timezone_name
        ):
            raise ContractError("taf_exact_join_audit_sample_scope_mismatch")
        if not _event_is_historical_closed(
            event_date=event_date,
            settlement_timezone=timezone_name,
            as_of=as_of,
        ):
            # Preserve the event as an arrival/revision observation, but it
            # cannot inflate the exact-join OOS count or TAF-24 gate.
            non_oos_observational_event_dates.append(event_date)
            continue
        scope = sample.get("calibration_scope")
        native_p0 = sample.get("native_p0")
        taf = sample.get("taf")
        truth = sample.get("truth")
        if not all(isinstance(value, Mapping) for value in (scope, native_p0, taf, truth)):
            raise ContractError("taf_exact_join_audit_sample_components_missing")
        if (
            _text("sample_season_bin", scope.get("season_bin")) != season
            or _text("sample_lead_bucket", scope.get("lead_bucket")) != lead
            or _text("sample_run_cycle", scope.get("run_cycle")) != run
            or _text("sample_settlement_definition", scope.get("settlement_definition"))
            != settlement_definition
        ):
            raise ContractError("taf_exact_join_audit_sample_calibration_scope_mismatch")
        if native_p0.get("prediction_oos") is not True:
            sample_errors.append("prediction_oos_required")
        systems = native_p0.get("ensemble_system_ids")
        families = native_p0.get("independence_family_ids")
        if (
            not isinstance(systems, list)
            or len(set(map(str, systems))) < 2
            or not isinstance(families, list)
            or len(set(map(str, families))) < 2
        ):
            sample_errors.append("minimum_two_independent_native_models_required")
        capture_sha = _sha("sample_taf_capture", taf.get("capture_content_sha256"))
        capture = captures.get(capture_sha)
        if capture is None:
            sample_errors.append("taf_capture_pointer_missing")
        else:
            capture_scope = (
                _text("capture_station", capture.get("station_id")).upper(),
                _text("capture_date", capture.get("event_date")),
                _text("capture_timezone", capture.get("settlement_timezone")),
            )
            if capture_scope != (station, event_date, timezone_name):
                sample_errors.append("taf_capture_pointer_scope_mismatch")
            if str(capture.get("source_class")) != str(taf.get("source_class")):
                sample_errors.append("taf_capture_source_class_mismatch")
            # A forward/local receipt is an arrival observation for the
            # resident worker, never a historical OOS label.  Keep its date
            # visible in the audit but remove it from the 24-sample gate.
            if (
                str(capture.get("kind") or "").upper() == "FORWARD"
                or str(capture.get("source_class") or "")
                == "official_forward_native_receipt"
                or str(sample.get("oos_event_temporal_scope") or "")
                not in {"", HISTORICAL_OOS_TEMPORAL_SCOPE}
            ):
                if str(capture.get("revision_kind") or "").upper() in {"AMD", "COR"}:
                    sample_errors.append("amd_cor_used_as_taf_feature")
                if corrections.get((station, event_date)):
                    sample_errors.append("amd_cor_station_day_not_quarantined")
                    unquarantined_correction_pointers.append({
                        "event_date": event_date,
                        "correction_content_sha256": sorted(
                            _sha(
                                "unquarantined_correction_content",
                                item.get("content_sha256"),
                            )
                            for item in corrections[(station, event_date)]
                        ),
                    })
                non_oos_observational_event_dates.append(event_date)
                sample_errors.append("forward_taf_observational_not_oos")
                continue
            if "historical_oos_eligible" in sample and sample.get(
                "historical_oos_eligible"
            ) is not True:
                non_oos_observational_event_dates.append(event_date)
                sample_errors.append("historical_oos_eligibility_required")
                continue
            if str(capture.get("revision_kind") or "").upper() in {"AMD", "COR"}:
                sample_errors.append("amd_cor_used_as_taf_feature")
            lineage_pointers.append({
                "event_date": event_date,
                "taf_capture_content_sha256": capture_sha,
                "taf_source_object_sha256": str(capture["source_object_sha256"]),
                "taf_revision_id": str(capture.get("revision_id") or ""),
                "taf_revision_kind": str(capture.get("revision_kind") or "HISTORICAL"),
                "truth_id": _text("sample_truth_id", truth.get("canonical_truth_id")),
            })
        truth_id = _text("sample_truth_id", truth.get("canonical_truth_id"))
        truth_pointer = truths.get(truth_id)
        if truth_pointer is None:
            sample_errors.append("awc_truth_pointer_missing")
        else:
            truth_scope = (
                _text("truth_station", truth_pointer.get("station_id")).upper(),
                _text("truth_date", truth_pointer.get("event_date")),
                _text("truth_settlement_definition", truth_pointer.get("settlement_definition")),
            )
            if truth_scope != (station, event_date, settlement_definition):
                sample_errors.append("awc_truth_pointer_scope_mismatch")
            if _text("sample_truth_lane", truth.get("source_lane")).upper() != "AWC":
                sample_errors.append("sample_truth_lane_not_awc")
        station_day_corrections = corrections.get((station, event_date), [])
        if station_day_corrections:
            sample_errors.append("amd_cor_station_day_not_quarantined")
            unquarantined_correction_pointers.append({
                "event_date": event_date,
                "correction_content_sha256": sorted(
                    _sha("unquarantined_correction_content", item.get("content_sha256"))
                    for item in station_day_corrections
                ),
            })

    excluded_pointers: list[dict[str, Any]] = []
    excluded_days: set[str] = set()
    for exclusion in excluded:
        if not isinstance(exclusion, Mapping):
            raise ContractError("taf_exact_join_audit_exclusion_invalid")
        if _text("exclusion_station", exclusion.get("station_id")).upper() != station:
            raise ContractError("taf_exact_join_audit_exclusion_station_mismatch")
        event_date = _text("exclusion_event_date", exclusion.get("event_date"))
        if event_date in excluded_days or event_date in seen_days:
            raise ContractError("taf_exact_join_audit_exclusion_day_ambiguous")
        excluded_days.add(event_date)
        if exclusion.get("reason") not in {
            "forward_taf_correction_requires_manual_review",
            "taf_correction_requires_manual_review",
        }:
            raise ContractError("taf_exact_join_audit_exclusion_reason_invalid")
        rows = exclusion.get("corrections")
        if not isinstance(rows, list) or not rows:
            raise ContractError("taf_exact_join_audit_exclusion_corrections_missing")
        expected = {
            _sha("exclusion_correction_content", row.get("content_sha256"))
            for row in rows
            if isinstance(row, Mapping)
        }
        if len(expected) != len(rows):
            raise ContractError("taf_exact_join_audit_exclusion_corrections_invalid")
        actual = {
            _sha("correction_content", row.get("content_sha256"))
            for row in corrections.get((station, event_date), [])
        }
        if expected != actual:
            sample_errors.append("amd_cor_quarantine_pointer_mismatch")
        excluded_pointers.append({
            "event_date": event_date,
            "correction_content_sha256": sorted(expected),
        })

    return station, {
        "path": path,
        "content_sha256": _sha("exact_join_content", document.get("content_sha256")),
        "scope": {
            "season_bin": season,
            "lead_bucket": lead,
            "run_cycle": run,
            "settlement_definition": settlement_definition,
            "settlement_timezone": timezone_name,
        },
        "sample_count": len(seen_days) - len(non_oos_observational_event_dates),
        "non_oos_observational_event_count": len(
            non_oos_observational_event_dates
        ),
        "non_oos_observational_event_dates": sorted(
            non_oos_observational_event_dates
        ),
        "lineage_pointer_count": len(lineage_pointers),
        "lineage_pointers": lineage_pointers,
        "unquarantined_correction_pointers": unquarantined_correction_pointers,
        "excluded_correction_sample_count": len(excluded_pointers),
        "excluded_correction_pointers": excluded_pointers,
        "errors": sorted(set(sample_errors)),
    }


def audit_taf_p0m_exact_join_45station(
    *,
    station_registry: Sequence[Mapping[str, Any]],
    p0_inbox: Mapping[str, Any],
    p0_inbox_path: Path | str | None = None,
    exact_joins: Sequence[tuple[Path | str, Mapping[str, Any]]],
    taf_capture_pointers: Sequence[Mapping[str, Any]],
    truth_pointers: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Audit every canonical station with only persisted local evidence."""

    registry = _registry_index(station_registry)
    as_of = _utc("p0_inbox_generated", p0_inbox.get("generated_at_utc"))
    current_p0 = _p0_index(p0_inbox)
    captures, corrections = _pointer_indexes(taf_capture_pointers)
    truths = _truth_index(truth_pointers)
    if len(exact_joins) > MAX_EXACT_JOINS:
        raise ContractError("taf_exact_join_audit_exact_join_count_exceeded")
    joins_by_station: dict[str, dict[str, Any]] = {}
    for path, document in exact_joins:
        if not isinstance(document, Mapping):
            raise ContractError("taf_exact_join_audit_document_mapping_required")
        station, parsed = _validate_join_document(
            document,
            path=str(path),
            registry=registry,
            captures=captures,
            truths=truths,
            corrections=corrections,
            as_of=as_of,
        )
        if station in joins_by_station:
            raise ContractError("taf_exact_join_audit_exact_join_station_duplicate")
        joins_by_station[station] = parsed

    entries: list[dict[str, Any]] = []
    blockers = Counter()
    for station in sorted(registry):
        join = joins_by_station.get(station)
        current_scope = current_p0.get(station)
        reasons: list[str] = []
        if current_scope is None:
            reasons.append("current_p0_scope_missing")
        if join is None:
            reasons.append("exact_join_artifact_missing")
            sample_count = 0
            correction_count = 0
            scope: dict[str, Any] | None = None
        else:
            sample_count = int(join["sample_count"])
            correction_count = int(join["excluded_correction_sample_count"])
            scope = dict(join["scope"])
            reasons.extend(str(value) for value in join["errors"])
            if sample_count < MIN_TAF_NATIVE_OOS_SAMPLE_N:
                reasons.append("taf_native_oos_sample_n_insufficient")
            if current_scope is not None and current_scope != (
                scope["season_bin"],
                scope["lead_bucket"],
                scope["run_cycle"],
                scope["settlement_definition"],
                scope["settlement_timezone"],
            ):
                reasons.append("current_p0_scope_not_exact_join_scope")
        for reason in set(reasons):
            blockers[reason] += 1
        entries.append({
            "station_id": station,
            "state": "BLOCKED" if reasons else "EXACT_OOS_GATE_MET_UNPROMOTED",
            "sample_count": sample_count,
            "minimum_sample_n": MIN_TAF_NATIVE_OOS_SAMPLE_N,
            "missing_to_24": max(0, MIN_TAF_NATIVE_OOS_SAMPLE_N - sample_count),
            "current_p0_scope": (
                None if current_scope is None else {
                    "season_bin": current_scope[0],
                    "lead_bucket": current_scope[1],
                    "run_cycle": current_scope[2],
                    "settlement_definition": current_scope[3],
                    "settlement_timezone": current_scope[4],
                }
            ),
            "exact_join": join,
            "excluded_amd_cor_station_day_count": correction_count,
            "blockers": sorted(set(reasons)),
        })
    if set(joins_by_station) - set(registry):
        raise ContractError("taf_exact_join_audit_unregistered_join_station")
    qualified_scope_count = sum(
        int(
            entry["state"] == "EXACT_OOS_GATE_MET_UNPROMOTED"
            and entry["sample_count"] >= MIN_TAF_NATIVE_OOS_SAMPLE_N
        )
        for entry in entries
    )
    sample_count_total = sum(int(entry["sample_count"]) for entry in entries)
    quarantined_station_day_count = sum(
        int(entry["excluded_amd_cor_station_day_count"])
        for entry in entries
    )
    # The P0 inbox is a mutable handoff file that is periodically rewritten
    # as packets expire.  The scope rows above are not sufficient to prove
    # which inbox snapshot drove this receipt, so retain the verified input
    # content address (and its generation instant/path when available) in the
    # receipt identity.  ``_p0_index`` has already validated this hash against
    # the canonical inbox payload before we reach this point.
    p0_inbox_artifact: dict[str, Any] = {
        "content_sha256": str(p0_inbox["content_sha256"]),
    }
    generated_at_utc = p0_inbox.get("generated_at_utc")
    if generated_at_utc is not None and str(generated_at_utc).strip():
        p0_inbox_artifact["generated_at_utc"] = str(generated_at_utc)
    if p0_inbox_path is not None:
        p0_inbox_artifact["path"] = str(
            Path(p0_inbox_path).expanduser().resolve()
        )

    identity = {
        "schema": SCHEMA,
        # The audit's evidence clock is the already-validated generation
        # instant of the exact P0 inbox consumed above.  Persist it explicitly
        # so downstream atomic snapshots never infer time from a filename or
        # filesystem mtime.
        "as_of_utc": as_of.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "state": "BLOCKED" if blockers else "EXACT_OOS_GATE_MET_UNPROMOTED",
        "canonical_station_count": len(registry),
        "exact_join_artifact_count": len(joins_by_station),
        "current_p0_station_count": len(current_p0),
        "minimum_taf_native_oos_sample_n": MIN_TAF_NATIVE_OOS_SAMPLE_N,
        "input_artifacts": {"p0_inbox": p0_inbox_artifact},
        "totals": {
            "qualified_scope_count_at_24": qualified_scope_count,
            "sample_count_total": sample_count_total,
            "max_sample_count": max(
                (int(entry["sample_count"]) for entry in entries),
                default=0,
            ),
            "quarantined_station_day_count": quarantined_station_day_count,
        },
        "blocker_counts": dict(sorted(blockers.items())),
        "entries": entries,
        "boundary": {
            "read_only": True,
            "network_fetches": 0,
            "writes_database": False,
            "writes_production_p0m": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "forbids_forged_revisions": True,
            "amd_cor_station_day_quarantine_required": True,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "EXACT_JOIN_SCHEMA",
    "MAX_STATIONS",
    "MIN_TAF_NATIVE_OOS_SAMPLE_N",
    "P0_INBOX_SCHEMA",
    "SCHEMA",
    "audit_taf_p0m_exact_join_45station",
]
