"""Settlement-timezone historical evidence boundary for the Bayes basket target.

The market registry fixes each station/event settlement-day timezone.
``America/New_York`` remains only the default operational/reporting boundary.
A model row must carry the registry timezone and its valid times must
independently resolve to that same settlement event day. Truth is
aggregated from canonical report observations by the settlement boundary, with the
AMSC/AWC source lane fixed by the target registry.  A source-lane mismatch,
missing lineage, or legacy timezone is fatal.  Canonical corrections and
same-slot revisions are retained as quarantined evidence and excluded from
truth without invalidating unrelated station-days.

This is a pure evidence builder.  It does not open SQLite, fetch the network,
write a production artifact, or call an order gateway.  Every truth row must
be committed by the single strict-as-of clock.  Historical joins and final
truth use the station/market settlement day supplied by the registry; the
``America/New_York`` default is only an operational/reporting fallback.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import (
    MODEL_IDS,
    MIN_FORMAL_ENSEMBLE_MODELS,
    ContractError,
    content_sha256,
)
from .model_sources import (
    ALLOWED_MODEL_PROVIDERS,
    MODEL_ROLES,
    model_id_for_provider,
    provider_role,
    provider_source_class,
)
from .source_policy import (
    SOURCE_LANE_CROSS_FALLBACK_FORBIDDEN,
    normalize_station_lane_map,
    source_policy_reason,
)


SCHEMA = "bayes_basket_target_et_history_evidence_v1"
TARGET_TIMEZONE = "America/New_York"
MODEL_PROVIDERS = frozenset({
    "ecmwf_ifs025_official",
    "gfs_0p25_official",
    "icon_global_official",
})
LANES = frozenset({"amsc", "awc"})


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _utc(name: str, value: Any) -> datetime:
    raw = _text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _date(name: str, value: Any) -> date:
    raw = _text(name, value)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_date") from exc


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid_sha256")
    return result


def _sha_lineage(name: str, value: Any) -> str:
    """Validate a scalar or a bounded list of hashes and return one hash.

    A daily curve is made from several native messages.  The join still needs
    one deterministic lineage key, but it must not silently discard the
    individual message hashes.  Callers retain the original list alongside
    this content-addressed aggregate.
    """

    if isinstance(value, (list, tuple)):
        hashes = sorted({_sha(f"{name}_item", item) for item in value})
        if not hashes:
            raise ContractError(f"{name}_missing")
        return content_sha256({"sha256": hashes})
    return _sha(name, value)


def _temperature(name: str, value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid_number") from exc
    if result != result or result in {float("inf"), float("-inf")}:
        raise ContractError(f"{name}_invalid_number")
    return result


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ContractError("target_timezone_invalid") from exc


def _event_date_settlement(valid_at: datetime, target_timezone: str) -> str:
    return valid_at.astimezone(_zone(target_timezone)).date().isoformat()


def event_day_window(
    event_date: str,
    *,
    as_of_utc: str | datetime,
    target_timezone: str = TARGET_TIMEZONE,
) -> dict[str, str]:
    """Classify a registry-scoped settlement day as not started/open/closed."""

    day = _date("event_date", event_date)
    decision = (
        _utc("as_of_utc", as_of_utc)
        if not isinstance(as_of_utc, datetime)
        else as_of_utc.astimezone(timezone.utc)
    )
    zone = _zone(target_timezone)
    start_local = datetime(day.year, day.month, day.day, tzinfo=zone)
    next_day = day + timedelta(days=1)
    end_local = datetime(next_day.year, next_day.month, next_day.day, tzinfo=zone)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    if decision < start_utc:
        state = "not_started"
        reason = "target_settlement_day_not_started"
    elif decision < end_utc:
        state = "open"
        reason = "target_settlement_day_not_closed"
    else:
        state = "closed"
        reason = "target_settlement_day_closed"
    return {
        "event_date": day.isoformat(),
        "target_timezone": target_timezone,
        "target_day_start_utc": start_utc.isoformat().replace("+00:00", "Z"),
        "target_day_end_utc": end_utc.isoformat().replace("+00:00", "Z"),
        "as_of_utc": decision.isoformat().replace("+00:00", "Z"),
        "state": state,
        "reason": reason,
    }


def assert_event_day_started(
    event_date: str,
    *,
    as_of_utc: str | datetime,
    target_timezone: str = TARGET_TIMEZONE,
) -> dict[str, str]:
    """Reject a future settlement day at a formal target-SSOT boundary."""

    window = event_day_window(
        event_date,
        as_of_utc=as_of_utc,
        target_timezone=target_timezone,
    )
    if window["state"] == "not_started":
        raise ContractError("future_event_day_not_allowed")
    return window


def assert_event_day_closed(
    event_date: str,
    *,
    as_of_utc: str | datetime,
    target_timezone: str = TARGET_TIMEZONE,
) -> dict[str, str]:
    """Require a final, closed settlement day for historical rebuilds.

    Historical reconstruction must never manufacture a final label or a
    calibration input for the current open day.  The caller may still collect
    prospective evidence for an open day through ``assert_event_day_started``;
    this stricter boundary is only for retrospective rebuilds and final truth.
    """

    window = event_day_window(
        event_date,
        as_of_utc=as_of_utc,
        target_timezone=target_timezone,
    )
    if window["state"] == "not_started":
        raise ContractError("future_event_day_not_allowed")
    if window["state"] != "closed":
        raise ContractError("historical_event_day_not_closed")
    return window


def _provider_model(provider_id: str) -> str:
    try:
        return model_id_for_provider(provider_id)
    except ContractError:
        return ""


def _lane_map(station_lane_map: Mapping[str, Any]) -> dict[str, str]:
    try:
        result = normalize_station_lane_map(station_lane_map)
    except ValueError as exc:
        raise ContractError(str(exc)) from exc
    if not result:
        raise ContractError("station_lane_map_empty")
    return result


def normalize_et_model_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    as_of_utc: str,
    target_timezone: str = TARGET_TIMEZONE,
    station_timezone_map: Mapping[str, Any] | None = None,
    history_only: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Accept only registry-timezone, prospective, lineage-complete model rows.

    The returned rows are a new normalized view.  The input is never mutated;
    in particular, a legacy ``event_date`` is never silently relabeled to the
    registry settlement day.
    """

    timezone_by_station = {
        str(station).upper(): _text("settlement_timezone", timezone_name)
        for station, timezone_name in (station_timezone_map or {}).items()
    }
    for timezone_name in timezone_by_station.values():
        _zone(timezone_name)
    decision = _utc("as_of_utc", as_of_utc)
    accepted: list[dict[str, Any]] = []
    blocked: list[dict[str, str]] = []
    for index, raw in enumerate(rows):
        try:
            if not isinstance(raw, Mapping):
                raise ContractError("model_row_mapping_required")
            provider_id = _text("provider_id", raw.get("provider_id")).lower()
            if provider_id not in ALLOWED_MODEL_PROVIDERS:
                raise ContractError("model_provider_not_allowed")
            station = _text("station_id", raw.get("station_id")).upper()
            expected_timezone = timezone_by_station.get(station, target_timezone)
            _zone(expected_timezone)
            explicit_timezone = _text("target_timezone", raw.get("target_timezone"))
            if explicit_timezone != expected_timezone:
                raise ContractError("model_target_timezone_registry_mismatch")
            event_day = _date("event_date", raw.get("event_date"))
            # A row whose event day has not started at the decision clock is a
            # future-day input.  It must never enter the accepted model set,
            # historical SSOT, truth, or calibration.  Forward planning is
            # not a target-lane input; callers must quarantine it separately.
            event_window = event_day_window(
                event_day.isoformat(),
                as_of_utc=decision,
                target_timezone=expected_timezone,
            )
            if event_window["state"] == "not_started":
                raise ContractError("future_event_day_not_allowed")
            valid_at = _utc("valid_time_utc", raw.get("valid_time_utc"))
            if valid_at.astimezone(_zone(expected_timezone)).date() != event_day:
                raise ContractError("model_valid_time_settlement_day_mismatch")
            run_init = _utc(
                "model_run_initialization_utc",
                raw.get("model_run_initialization_utc", raw.get("run_initialization_utc")),
            )
            published_raw = str(raw.get("published_at_utc") or "").strip()
            published_at = _utc("published_at_utc", published_raw) if published_raw else None
            capture_mode = str(raw.get("capture_mode") or "prospective").strip().lower()
            source_class = str(
                raw.get("source_class") or provider_source_class(provider_id)
            ).strip().lower()
            aggregator = bool(
                raw.get("aggregator_or_republisher", source_class == "aggregator_republisher")
            )
            open_meteo = bool(raw.get("open_meteo", provider_id == "icon_global_open_meteo"))
            if provider_id == "icon_global_open_meteo" and not (aggregator and open_meteo):
                raise ContractError("aggregator_source_flags_invalid")
            if provider_id == "icon_global_open_meteo" and capture_mode != "historical_rebuild":
                raise ContractError("aggregator_prospective_not_allowed")
            reconstructed_at = ""
            historical_replay_eligible = event_window["state"] == "closed"
            prospective_strict_asof_eligible = False
            if capture_mode == "historical_rebuild":
                if not history_only:
                    raise ContractError("retrospective_model_not_allowed_in_live_history_mode")
                # A retrospective rebuild is a historical label/input repair,
                # not a forward forecast cache.  It may only be admitted after
                # the settlement event day has closed.  A prospective row cannot be
                # converted into a historical rebuild by merely changing the
                # capture label.
                if event_day_window(
                    event_day.isoformat(),
                    as_of_utc=decision,
                    target_timezone=expected_timezone,
                )["state"] != "closed":
                    raise ContractError("historical_rebuild_event_day_not_closed")
                reconstructed_at = _utc("reconstructed_at_utc", raw.get("reconstructed_at_utc"))
                if published_at is not None and published_at > reconstructed_at:
                    raise ContractError("historical_rebuild_timestamp_invalid")
                if reconstructed_at > decision:
                    raise ContractError("historical_rebuild_timestamp_invalid")
                published_at_text = published_at.isoformat().replace("+00:00", "Z") if published_at else ""
                received_at_text = ""
                arrival_evidence_type = "RETROSPECTIVE_REBUILD_NOT_ARRIVAL"
            else:
                if published_at is None:
                    raise ContractError("published_at_utc_missing")
                received_at = _utc("received_at_utc", raw.get("received_at_utc"))
                if published_at > received_at:
                    raise ContractError("model_published_after_received")
                if received_at > decision:
                    raise ContractError("model_received_after_asof")
                received_at_text = received_at.isoformat().replace("+00:00", "Z")
                published_at_text = published_at.isoformat().replace("+00:00", "Z")
                arrival_evidence_type = "PROSPECTIVE_NATIVE_ARRIVAL"
                prospective_strict_asof_eligible = True
            if run_init >= valid_at:
                raise ContractError("model_run_not_before_valid_time")
            raw_sha = _sha_lineage("raw_message_sha256", raw.get("raw_message_sha256"))
            receipt_sha = _sha_lineage("receipt_sha256", raw.get("receipt_sha256"))
            temperature = _temperature("temperature_2m_c", raw.get("temperature_2m_c"))
            accepted.append({
                "station_id": station,
                "event_date": event_day.isoformat(),
                "target_timezone": expected_timezone,
                "provider_id": provider_id,
                "provider_role": provider_role(provider_id),
                "model_id": _provider_model(provider_id),
                "source_class": source_class,
                "aggregator_or_republisher": aggregator,
                "open_meteo": open_meteo,
                "model_run_initialization_utc": run_init.isoformat().replace("+00:00", "Z"),
                "valid_time_utc": valid_at.isoformat().replace("+00:00", "Z"),
                "published_at_utc": published_at_text,
                "received_at_utc": received_at_text,
                "temperature_2m_c": temperature,
                "raw_message_sha256": raw_sha,
                "receipt_sha256": receipt_sha,
                "raw_message_sha256s": sorted({
                    str(item).strip().lower()
                    for item in (raw.get("raw_message_sha256s") or ([raw.get("raw_message_sha256")] if not isinstance(raw.get("raw_message_sha256"), (list, tuple)) else raw.get("raw_message_sha256")))
                    if str(item).strip()
                }),
                "receipt_sha256s": sorted({
                    str(item).strip().lower()
                    for item in (raw.get("receipt_sha256s") or ([raw.get("receipt_sha256")] if not isinstance(raw.get("receipt_sha256"), (list, tuple)) else raw.get("receipt_sha256")))
                    if str(item).strip()
                }),
                "capture_mode": capture_mode,
                "reconstructed_at_utc": reconstructed_at,
                "arrival_evidence_type": arrival_evidence_type,
                "historical_replay_eligible": historical_replay_eligible,
                "prospective_strict_asof_eligible": prospective_strict_asof_eligible,
                "source_url": str(raw.get("source_url") or "").strip(),
                "raw_object_ref": str(raw.get("raw_object_ref") or "").strip(),
            })
        except (ContractError, TypeError, ValueError) as exc:
            blocked.append({"row_index": str(index), "reason": str(exc)})
    return accepted, blocked


def build_et_truth_rows(
    observations: Iterable[Mapping[str, Any]],
    *,
    station_lane_map: Mapping[str, Any],
    as_of_utc: str,
    target_timezone: str = TARGET_TIMEZONE,
    station_timezone_map: Mapping[str, Any] | None = None,
    canonical_ingress: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Aggregate canonical observations on registry settlement-day boundaries."""

    decision = _utc("as_of_utc", as_of_utc)
    lanes = _lane_map(station_lane_map)
    timezone_by_station = {
        str(station).upper(): _text("settlement_timezone", timezone_name)
        for station, timezone_name in (station_timezone_map or {}).items()
    }
    for timezone_name in timezone_by_station.values():
        _zone(timezone_name)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    seen_identity: set[tuple[str, str, str, str, str]] = set()
    slot_hashes: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    blocked: list[dict[str, str]] = []
    for index, raw in enumerate(observations):
        try:
            if not isinstance(raw, Mapping):
                raise ContractError("truth_observation_mapping_required")
            station = _text("station_id", raw.get("station_id")).upper()
            expected_timezone = timezone_by_station.get(station, target_timezone)
            _zone(expected_timezone)
            lane = _text("source_lane", raw.get("source_lane")).lower()
            if lane not in LANES:
                raise ContractError("truth_source_lane_invalid")
            expected_lane = lanes.get(station)
            if expected_lane is None:
                raise ContractError("truth_station_not_in_target_registry")
            report_type = _text("report_type", raw.get("report_type")).upper()
            if report_type not in {"METAR", "SPECI"}:
                raise ContractError("truth_report_type_invalid")
            valid_at = _utc("valid_at_utc", raw.get("valid_at_utc", raw.get("valid_at")))
            policy_reason = source_policy_reason(
                source_lane=lane,
                report_type=report_type,
                valid_at=valid_at,
                expected_lane=expected_lane,
            )
            qc_status = str(raw.get("qc_status") or "ACCEPTED").upper()
            if policy_reason and "reference_only" in policy_reason:
                blocked.append({
                    "row_index": str(index),
                    "reason": policy_reason,
                })
                continue
            if policy_reason:
                if policy_reason == SOURCE_LANE_CROSS_FALLBACK_FORBIDDEN:
                    raise ContractError("truth_source_lane_cross_fallback_forbidden")
                raise ContractError(policy_reason)
            if qc_status == "REFERENCE_ONLY":
                if lane != expected_lane:
                    raise ContractError("truth_source_lane_cross_fallback_forbidden")
                blocked.append({
                    "row_index": str(index),
                    "reason": str(raw.get("reference_reason") or "truth_reference_only"),
                })
                continue
            if lane != expected_lane:
                raise ContractError("truth_source_lane_cross_fallback_forbidden")
            if qc_status != "ACCEPTED" or raw.get("correction_suspected") in {1, True, "1"}:
                raise ContractError("truth_revision_or_quarantine")
            if not canonical_ingress:
                raise ContractError("truth_not_canonical_ingress")
            temperature = _temperature(
                "reported_temperature_c",
                raw.get("reported_temperature_c", raw.get("temperature_c")),
            )
            raw_sha = _sha("raw_sha256", raw.get("raw_sha256"))
            identity_key = (station, lane, report_type, valid_at.isoformat(), raw_sha)
            if identity_key in seen_identity:
                raise ContractError("truth_duplicate_observation")
            seen_identity.add(identity_key)
            slot_key = (
                station,
                lane,
                report_type,
                valid_at.isoformat().replace("+00:00", "Z"),
            )
            slot_hashes[slot_key].add(raw_sha)
            source_poll_id = _text("source_poll_id", raw.get("source_poll_id"))
            first_seen = _utc("first_seen_at_utc", raw.get("first_seen_at_utc", raw.get("first_seen_at")))
            committed = _utc(
                "canonical_committed_at_utc",
                raw.get("canonical_committed_at_utc", raw.get("canonical_committed_at")),
            )
            if not (valid_at <= first_seen <= committed):
                raise ContractError("truth_timestamp_order_invalid")
            if committed > decision:
                raise ContractError("truth_committed_after_asof")
            event_date = _event_date_settlement(valid_at, expected_timezone)
            groups[(station, event_date, lane)].append({
                "station_id": station,
                "event_date": event_date,
                "target_timezone": expected_timezone,
                "source_lane": lane,
                "report_type": report_type,
                "valid_at_utc": valid_at.isoformat().replace("+00:00", "Z"),
                "reported_temperature_c": temperature,
                "raw_sha256": raw_sha,
                "source_poll_id": source_poll_id,
                "first_seen_at_utc": first_seen.isoformat().replace("+00:00", "Z"),
                "canonical_committed_at_utc": committed.isoformat().replace("+00:00", "Z"),
            })
        except (ContractError, TypeError, ValueError) as exc:
            blocked.append({"row_index": str(index), "reason": str(exc)})

    revised_slots = {slot for slot, hashes in slot_hashes.items() if len(hashes) > 1}
    if revised_slots:
        for slot in sorted(revised_slots):
            station, lane, report_type, valid_at = slot
            blocked.append({
                "row_index": "-",
                "reason": "truth_same_slot_revision",
                "slot": "|".join(slot),
            })
        # Isolate only the revised report slot.  A revision is not evidence
        # that every other METAR/SPECI in the same station-day is invalid.
        # Keep the report type in the key as well: a revised METAR slot must
        # not quarantine an unrelated SPECI at the same timestamp.
        for key, items in list(groups.items()):
            retained = [
                item
                for item in items
                if (
                    item["station_id"],
                    item["source_lane"],
                    item["report_type"],
                    item["valid_at_utc"],
                ) not in revised_slots
            ]
            if retained:
                groups[key] = retained
            else:
                del groups[key]

    result: list[dict[str, Any]] = []
    for (station, event_date, lane), items in sorted(groups.items()):
        items.sort(key=lambda item: (item["valid_at_utc"], item["report_type"], item["raw_sha256"]))
        identity = {
            "station_id": station,
            "event_date": event_date,
            "source_lane": lane,
            "raw_sha256": [item["raw_sha256"] for item in items],
            "source_poll_ids": sorted({item["source_poll_id"] for item in items}),
        }
        result.append({
            "truth_id": "et-truth-" + content_sha256(identity)[:32],
            "station_id": station,
            "event_date": event_date,
            "target_timezone": items[0]["target_timezone"],
            "source_lane": lane,
            "official_tmax_c": max(item["reported_temperature_c"] for item in items),
            "report_count": len(items),
            "raw_sha256": sorted({item["raw_sha256"] for item in items}),
            "source_poll_ids": sorted({item["source_poll_id"] for item in items}),
            "received_at_utc": max(item["first_seen_at_utc"] for item in items),
            "canonical_committed_at_utc": max(item["canonical_committed_at_utc"] for item in items),
            "canonical_ingress": True,
            "content_sha256": content_sha256(identity),
        })
    return result, blocked


def build_et_history_evidence(
    *,
    model_rows: Iterable[Mapping[str, Any]],
    observations: Iterable[Mapping[str, Any]],
    station_lane_map: Mapping[str, Any],
    as_of_utc: str,
    target_timezone: str = TARGET_TIMEZONE,
    station_timezone_map: Mapping[str, Any] | None = None,
    canonical_ingress: bool = True,
    history_only: bool = False,
) -> dict[str, Any]:
    """Build a bounded settlement-day history receipt and exact model/truth join.

    The public function name is retained for compatibility; ``ET`` in the
    schema/legacy names does not override the registry settlement timezone.
    """

    model_input = list(model_rows)
    observation_input = list(observations)
    accepted_models, blocked_models = normalize_et_model_rows(
        model_input,
        as_of_utc=as_of_utc,
        target_timezone=target_timezone,
        station_timezone_map=station_timezone_map,
        history_only=history_only,
    )
    truth_rows, blocked_truth = build_et_truth_rows(
        observation_input,
        station_lane_map=station_lane_map,
        as_of_utc=as_of_utc,
        target_timezone=target_timezone,
        station_timezone_map=station_timezone_map,
        canonical_ingress=canonical_ingress,
    )
    truth_by_key = {(row["station_id"], row["event_date"]): row for row in truth_rows}
    model_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in accepted_models:
        model_groups[(row["station_id"], row["event_date"])].append(row)
    decision = _utc("as_of_utc", as_of_utc)
    event_day_windows = {
        (station_id, event_date): event_day_window(
            event_date,
            as_of_utc=decision,
            target_timezone=rows[0]["target_timezone"],
        )
        for (station_id, event_date), rows in sorted(model_groups.items())
    }
    joined: list[dict[str, Any]] = []
    join_blockers: list[dict[str, Any]] = []
    coverage_gaps: list[dict[str, Any]] = []
    pending_event_days: list[dict[str, Any]] = []
    for key, rows in sorted(model_groups.items()):
        providers = {row["provider_id"] for row in rows}
        provider_roles_present = {provider_role(provider) for provider in providers}
        truth = truth_by_key.get(key)
        if truth is None:
            window = event_day_windows[key]
            missing_truth = {
                "station_id": key[0],
                "event_date": key[1],
                "reason": "truth_missing",
                "truth_window_state": window["state"],
                "truth_window_reason": window["reason"],
                "target_day_start_utc": window["target_day_start_utc"],
                "target_day_end_utc": window["target_day_end_utc"],
            }
            if history_only and window["state"] != "closed":
                pending_event_days.append(missing_truth)
            elif history_only:
                coverage_gaps.append(missing_truth)
            else:
                join_blockers.append(missing_truth)
            continue
        window = event_day_windows[key]
        if window["state"] != "closed":
            pending = {
                "station_id": key[0],
                "event_date": key[1],
                "reason": "event_day_not_closed_in_settlement_timezone",
                "truth_window_state": window["state"],
                "truth_window_reason": window["reason"],
                "target_day_start_utc": window["target_day_start_utc"],
                "target_day_end_utc": window["target_day_end_utc"],
            }
            if history_only:
                pending_event_days.append(pending)
            else:
                join_blockers.append(pending)
            continue
        if len(provider_roles_present) < MIN_FORMAL_ENSEMBLE_MODELS:
            gap = {
                "station_id": key[0],
                "event_date": key[1],
                "reason": "multi_model_provider_set_incomplete",
                "available_providers": sorted(providers),
                "missing_providers": sorted(
                    role for role in MODEL_ROLES if role not in provider_roles_present
                ),
                "missing_model_roles": sorted(MODEL_ROLES - provider_roles_present),
            }
            if history_only:
                coverage_gaps.append(gap)
            else:
                join_blockers.append(gap)
            continue
        provider_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            provider_rows[row["provider_id"]].append(row)
        # Native ICON wins over the explicit Open-Meteo historical variant
        # whenever both cover the same model role/run.  The selected provider
        # remains in the evidence so the aggregator lineage is never hidden.
        selected_by_role: dict[str, str] = {}
        for provider in sorted(provider_rows):
            role = provider_role(provider)
            if role not in selected_by_role or provider == "icon_global_official":
                selected_by_role[role] = provider
        provider_rows = {
            provider: items
            for provider, items in provider_rows.items()
            if selected_by_role.get(provider_role(provider)) == provider
        }
        provider_daily: dict[str, dict[str, Any]] = {}
        provider_blocked = False
        for provider, provider_items in sorted(provider_rows.items()):
            valid_times = [item["valid_time_utc"] for item in provider_items]
            if len(valid_times) != len(set(valid_times)):
                join_blockers.append({
                    "station_id": key[0],
                    "event_date": key[1],
                    "reason": "model_duplicate_valid_time",
                    "provider_id": provider,
                })
                provider_blocked = True
                continue
            provider_run_ids = {item["model_run_initialization_utc"] for item in provider_items}
            if len(provider_run_ids) != 1:
                join_blockers.append({
                    "station_id": key[0],
                    "event_date": key[1],
                    "reason": "model_run_generation_mismatch",
                    "provider_id": provider,
                })
                provider_blocked = True
                continue
            raw_hashes = sorted({item["raw_message_sha256"] for item in provider_items})
            receipt_hashes = sorted({item["receipt_sha256"] for item in provider_items})
            provider_daily[provider] = {
                "forecast_daily_max_c": max(item["temperature_2m_c"] for item in provider_items),
                "run_initialization_utc": next(iter(provider_run_ids)),
                "raw_message_sha256": raw_hashes,
                "receipt_sha256": receipt_hashes,
                "model_run_lineage_sha256": content_sha256({
                    "station_id": key[0],
                    "event_date": key[1],
                    "provider_id": provider,
                    "run_initialization_utc": next(iter(provider_run_ids)),
                    "raw_message_sha256": raw_hashes,
                    "receipt_sha256": receipt_hashes,
                }),
                "capture_modes": sorted({item["capture_mode"] for item in provider_items}),
                "historical_replay_eligible": all(
                    item["historical_replay_eligible"] for item in provider_items
                ),
                "prospective_strict_asof_eligible": all(
                    item["prospective_strict_asof_eligible"] for item in provider_items
                ),
                "provider_role": provider_role(provider),
                "source_classes": sorted({item["source_class"] for item in provider_items}),
                "aggregator_or_republisher": any(
                    item["aggregator_or_republisher"] for item in provider_items
                ),
            }
        if provider_blocked:
            continue
        run_ids = {row["run_initialization_utc"] for row in provider_daily.values()}
        if len(run_ids) != 1:
            join_blockers.append({"station_id": key[0], "event_date": key[1], "reason": "model_run_generation_mismatch"})
            continue
        joined.append({
            "station_id": key[0],
            "event_date": key[1],
            "target_timezone": truth["target_timezone"],
            "truth_id": truth["truth_id"],
            "official_tmax_c": truth["official_tmax_c"],
            "run_initialization_utc": next(iter(run_ids)),
            "forecasts": {
                provider: item["forecast_daily_max_c"]
                for provider, item in sorted(provider_daily.items())
            },
            "model_lineage_sha256": {
                provider: item["model_run_lineage_sha256"]
                for provider, item in sorted(provider_daily.items())
            },
            "model_raw_message_sha256": {
                provider: item["raw_message_sha256"]
                for provider, item in sorted(provider_daily.items())
            },
            "model_receipt_sha256": {
                provider: item["receipt_sha256"]
                for provider, item in sorted(provider_daily.items())
            },
            "model_capture_modes": {
                provider: item["capture_modes"]
                for provider, item in sorted(provider_daily.items())
            },
            "model_roles": {
                item["provider_role"]: provider
                for provider, item in sorted(provider_daily.items())
            },
            "model_source_classes": {
                provider: item["source_classes"]
                for provider, item in sorted(provider_daily.items())
            },
            "historical_replay_eligible": all(
                item["historical_replay_eligible"] for item in provider_daily.values()
            ),
            "prospective_strict_asof_eligible": all(
                item["prospective_strict_asof_eligible"] for item in provider_daily.values()
            ),
        })

    # Quarantine is an expected canonical-ingress outcome, not a structural
    # failure of the remaining truth rows.  Keep it as bounded diagnostic
    # evidence while allowing unrelated station-days to join.  Unexpected
    # malformed/non-canonical rows remain fatal.
    quarantine_reasons = {
        "truth_revision_or_quarantine",
        "truth_same_slot_revision",
    }
    reference_reasons = {
        "amsc_half_hour_reference_only",
        "awc_hourly_reference_only",
        "awc_hourly_reference_only_for_amsc_station",
        "truth_reference_only",
    }
    quarantined_truth = [
        item for item in blocked_truth if item.get("reason") in quarantine_reasons
    ]
    reference_only_truth = [
        item for item in blocked_truth if item.get("reason") in reference_reasons
    ]
    fatal_truth = [
        item
        for item in blocked_truth
        if item.get("reason") not in quarantine_reasons | reference_reasons
    ]
    quarantine_reason_counts: dict[str, int] = defaultdict(int)
    for item in quarantined_truth:
        reason = str(item.get("reason") or "unknown")
        quarantine_reason_counts[reason] += 1
    reference_reason_counts: dict[str, int] = defaultdict(int)
    for item in reference_only_truth:
        reason = str(item.get("reason") or "unknown")
        reference_reason_counts[reason] += 1

    # Keep exact-join blockers first so a bounded receipt cannot hide the
    # actionable cause behind thousands of malformed rows.  Quarantine is
    # emitted separately below and never authorizes a join by itself.
    blockers = [
        *join_blockers,
        *({"reason": item["reason"], "row_index": item["row_index"]} for item in fatal_truth),
        *({"reason": item["reason"], "row_index": item["row_index"]} for item in blocked_models),
    ]
    checks = {
        "registry_settlement_timezones_bound": bool(station_timezone_map),
        "model_input_nonempty": bool(model_input),
        "truth_input_nonempty": bool(observation_input),
        "model_rows_et_compatible": bool(accepted_models) and not blocked_models,
        "truth_rows_canonical_et_compatible": bool(truth_rows) and not fatal_truth and canonical_ingress,
        "truth_quarantine_isolated": not fatal_truth,
        "multi_model_join_positive": bool(joined),
        "all_joined_model_sets_at_least_two": bool(joined) and all(
            len({provider_role(provider) for provider in row["forecasts"]})
            >= MIN_FORMAL_ENSEMBLE_MODELS
            for row in joined
        ),
        "history_only_mode": history_only,
        "open_event_days_pending_not_blocking_history": history_only,
        "historical_coverage_gaps_nonfatal": history_only,
        "promotion_allowed": False,
        "writes_production_db": False,
        "writes_target_db": False,
        "calls_order_gateway": False,
    }
    core = {
        "schema": SCHEMA,
        "state": "READY_FOR_STRICT_ASOF" if not blockers and joined else "BLOCKED_EVIDENCE",
        "as_of_utc": _utc("as_of_utc", as_of_utc).isoformat().replace("+00:00", "Z"),
        "target_timezone": (
            "MARKET_REGISTRY_SCOPED" if station_timezone_map else target_timezone
        ),
        "event_day_windows": {
            "target_timezone": (
                "MARKET_REGISTRY_SCOPED" if station_timezone_map else target_timezone
            ),
            "by_station_event_date": {
                f"{station_id}|{event_date}": event_day_windows[(station_id, event_date)]
                for station_id, event_date in sorted(event_day_windows)
            },
        },
        "input_counts": {
            "model_rows": len(model_input),
            "accepted_model_rows": len(accepted_models),
            "blocked_model_rows": len(blocked_models),
            "truth_observation_rows": len(observation_input),
            "truth_day_rows": len(truth_rows),
            "quarantined_truth_rows": len(quarantined_truth),
            "fatal_truth_rows": len(fatal_truth),
            "joined_station_days": len(joined),
            "pending_event_days": len(pending_event_days),
            "coverage_gap_count": len(coverage_gaps),
        },
        "truth_rows": truth_rows,
        "joined_station_days": joined,
        "pending_event_days": pending_event_days[:500],
        "coverage_gaps": coverage_gaps[:500],
        "quarantine": {
            "excluded_from_truth": True,
            "row_count": len(quarantined_truth),
            "reason_counts": dict(sorted(quarantine_reason_counts.items())),
        },
        "reference_only": {
            "excluded_from_truth": True,
            "row_count": len(reference_only_truth),
            "reason_counts": dict(sorted(reference_reason_counts.items())),
        },
        "blockers": blockers[:500],
        "checks": checks,
        "boundary": {
            "target_only": True,
            "offline_research_only": True,
            "places_orders": False,
            "orders_submitted": 0,
            "writes_production_db": False,
            "writes_target_db": False,
            "calls_order_gateway": False,
            "history_only": history_only,
        },
    }
    return {**core, "receipt_sha256": content_sha256(core)}


__all__ = [
    "MODEL_PROVIDERS",
    "SCHEMA",
    "TARGET_TIMEZONE",
    "build_et_history_evidence",
    "build_et_truth_rows",
    "assert_event_day_started",
    "event_day_window",
    "normalize_et_model_rows",
]
