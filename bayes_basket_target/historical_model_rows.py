"""Normalize official historical model rebuild packets once.

Historical model packets can contain both raw decoded points and a materialized
daily-curve view.  For retrospective ET joins, only complete daily curves are
allowed when the packet exposes that view; raw partial points are evidence of a
gap, not a forecast for the whole day.  This module is shared by ET history
and calibration so they cannot silently consume different historical rows.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .contracts import ContractError
from .et_history import event_day_window
from .model_sources import MODEL_ROLES, provider_role, provider_source_class


HISTORICAL_REBUILD_SCHEMA = "bayes_basket_target_historical_native_rebuild_evidence_v1"
ICON_RETAINED_RAW_REBUILD_SCHEMA = "bayes_basket_target_icon_retained_raw_rebuild_evidence_v1"
AGGREGATOR_REBUILD_SCHEMA = "bayes_basket_target_historical_aggregator_rebuild_evidence_v1"


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("historical_rebuild_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("historical_rebuild_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ContractError("historical_model_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("historical_model_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _historical_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    schema = str(payload.get("schema") or "")
    if schema in {HISTORICAL_REBUILD_SCHEMA, AGGREGATOR_REBUILD_SCHEMA}:
        rows = payload.get("model_rows")
        if not isinstance(rows, list):
            raise ContractError("historical_rebuild_model_rows_missing")
        return [dict(item) for item in rows if isinstance(item, Mapping)]

    if schema != ICON_RETAINED_RAW_REBUILD_SCHEMA:
        raise ContractError("historical_rebuild_evidence_schema_invalid")

    curves = payload.get("curves")
    if isinstance(curves, Mapping):
        eligible = curves.get("eligible_model_rows")
        complete_count = int(curves.get("complete_curve_count") or 0)
        if not isinstance(eligible, list):
            raise ContractError("icon_complete_curve_rows_missing")
        if complete_count != len(eligible):
            raise ContractError("icon_complete_curve_count_mismatch")

        # Flatten only complete curves.  The point-level hashes are retained
        # so the downstream exact join still has message lineage for every
        # temperature used in the daily maximum.
        flattened: list[dict[str, Any]] = []
        for curve in eligible:
            if not isinstance(curve, Mapping):
                raise ContractError("icon_complete_curve_invalid")
            points = curve.get("temperature_points")
            if not isinstance(points, list) or not points:
                raise ContractError("icon_complete_curve_points_missing")
            for point in points:
                if not isinstance(point, Mapping):
                    raise ContractError("icon_complete_curve_point_invalid")
                row = dict(point)
                row.setdefault("station_id", curve.get("station_id"))
                row.setdefault("event_date", curve.get("event_date"))
                row.setdefault("provider_id", curve.get("provider_id") or "icon_global_official")
                row.setdefault("model_run_initialization_utc", curve.get("model_run_initialization_utc"))
                flattened.append(row)
        return flattened

    # Older test/research packets did not expose curve completeness.  Preserve
    # their read compatibility; current retained-raw packets always expose
    # ``curves`` and therefore cannot use this partial-point fallback.
    rows = payload.get("model_points")
    if not isinstance(rows, list):
        raise ContractError("historical_rebuild_model_rows_missing")
    return [dict(item) for item in rows if isinstance(item, Mapping)]


def load_historical_model_rows(
    payload: Mapping[str, Any],
    *,
    as_of_utc: str,
    station_timezone_map: Mapping[str, str],
    require_reconstructed_at: bool = True,
    repair_registry_timezone: bool = False,
) -> list[dict[str, Any]]:
    """Return closed settlement-day rows with one canonical capture contract.

    ``repair_registry_timezone`` is an explicit historical-only migration
    switch.  A few retained native packets were materialized with the old
    operational timezone.  When enabled, their local event date is recomputed
    from the immutable valid-time and the station registry timezone; the old
    date/timezone is retained as audit metadata.  Missing valid time or a
    non-reconstructable row still fails closed.  Prospective/native arrival
    callers leave this switch off.
    """

    if not isinstance(payload, Mapping):
        raise ContractError("historical_rebuild_evidence_mapping_required")
    reconstructed_at = str(payload.get("reconstructed_at_utc") or "").strip()
    rows = _historical_rows(payload)
    if not reconstructed_at:
        reconstructed_at = str(
            next(
                (
                    item.get("reconstructed_at_utc")
                    for item in rows
                    if isinstance(item, Mapping) and item.get("reconstructed_at_utc")
                ),
                "",
            )
        ).strip()
    if not reconstructed_at and require_reconstructed_at:
        raise ContractError("historical_rebuild_reconstructed_at_missing")
    if reconstructed_at:
        _utc(reconstructed_at)

    normalized: list[dict[str, Any]] = []
    for item in rows:
        station_id = str(item.get("station_id") or "").strip().upper()
        target_timezone = str(
            station_timezone_map.get(station_id) or ""
        ).strip()
        if not target_timezone:
            raise ContractError("historical_rebuild_settlement_timezone_missing")
        explicit_timezone = str(item.get("target_timezone") or "").strip()
        event_date = str(item.get("event_date") or item.get("target_local_date") or "").strip()
        timezone_repaired = False
        legacy_event_date = event_date
        legacy_timezone = explicit_timezone
        if explicit_timezone != target_timezone:
            if not repair_registry_timezone:
                raise ContractError(
                    "historical_rebuild_settlement_timezone_mismatch"
                )
            valid_raw = str(item.get("valid_time_utc") or "").strip()
            if not valid_raw:
                raise ContractError(
                    "historical_rebuild_settlement_timezone_mismatch"
                )
            valid_at = _utc(valid_raw)
            event_date = valid_at.astimezone(ZoneInfo(target_timezone)).date().isoformat()
            timezone_repaired = True
        if not event_date:
            raise ContractError("historical_rebuild_event_date_missing")
        window = event_day_window(
            event_date,
            as_of_utc=as_of_utc,
            target_timezone=target_timezone,
        )
        if window["state"] != "closed":
            continue
        row = dict(item)
        row["event_date"] = event_date
        row["target_timezone"] = target_timezone
        if timezone_repaired:
            row["legacy_event_date"] = legacy_event_date
            row["legacy_target_timezone"] = legacy_timezone
            row["settlement_timezone_repaired_from_valid_time"] = True
        row["capture_mode"] = "historical_rebuild"
        row["model_run_initialization_utc"] = _timestamp_text(
            row.get("model_run_initialization_utc")
            or row.get("run_initialization_utc")
        )
        row["valid_time_utc"] = _timestamp_text(row.get("valid_time_utc"))
        provider_id = str(row.get("provider_id") or "").strip().lower()
        if provider_id:
            row["provider_id"] = provider_id
            row["provider_role"] = provider_role(provider_id)
            row.setdefault("source_class", provider_source_class(provider_id))
            row.setdefault("aggregator_or_republisher", provider_id == "icon_global_open_meteo")
            row.setdefault("open_meteo", provider_id == "icon_global_open_meteo")
        if str(payload.get("schema") or "") == AGGREGATOR_REBUILD_SCHEMA:
            if provider_id != "icon_global_open_meteo":
                raise ContractError("aggregator_rebuild_provider_invalid")
            if row.get("source_class") != "aggregator_republisher":
                raise ContractError("aggregator_rebuild_source_class_invalid")
            row["aggregator_or_republisher"] = True
            row["open_meteo"] = True
        if row.get("reconstructed_at_utc") or reconstructed_at:
            row["reconstructed_at_utc"] = str(row.get("reconstructed_at_utc") or reconstructed_at)
        for field in ("published_at_utc", "received_at_utc"):
            if row.get(field):
                row[field] = _timestamp_text(row[field])
        # A reconstructed official curve is eligible for historical replay
        # after ET closure, but is never a prospective strict-as-of arrival.
        row["historical_replay_eligible"] = True
        row["prospective_strict_asof_eligible"] = False
        normalized.append(row)
    return normalized


def _valid_time(value: Any) -> datetime:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("historical_model_valid_time_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("historical_model_valid_time_timezone_required")
    return parsed.astimezone(timezone.utc)


def _complete_curve(rows: list[Mapping[str, Any]], *, target_timezone: str) -> bool:
    if len(rows) == 1 and bool(rows[0].get("daily_curve_complete")):
        try:
            return int(rows[0].get("sample_count") or 0) >= 4
        except (TypeError, ValueError):
            return False
    if len(rows) < 4:
        return False
    day = date.fromisoformat(str(rows[0].get("event_date") or ""))
    zone = ZoneInfo(target_timezone)
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
    end = (datetime.combine(day, datetime.min.time(), tzinfo=zone) + timedelta(days=1)).astimezone(timezone.utc)
    times = sorted({_valid_time(row.get("valid_time_utc")) for row in rows})
    if len(times) != len(rows) or not times:
        return False
    if times[0] > start + timedelta(hours=3) or times[-1] < end - timedelta(hours=3):
        return False
    return all(later - earlier <= timedelta(hours=6) for earlier, later in zip(times, times[1:]))


def select_preferred_model_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Select one run generation per station/day, preferring broad model runs.

    Native prospective decode and retrospective rebuilds can overlap the same
    ET day with different run generations.  Mixing those generations creates a
    false daily curve and can make ET history and calibration disagree.  A
    A generation covering more canonical roles wins, with native evidence and
    complete curves as deterministic tie-breakers.
    """

    identity_rows: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        key = (
            str(row.get("station_id") or "").strip().upper(),
            str(row.get("event_date") or "").strip(),
            str(row.get("provider_id") or "").strip().lower(),
            str(row.get("model_run_initialization_utc") or row.get("run_initialization_utc") or "").strip(),
            str(row.get("valid_time_utc") or "").strip(),
        )
        if not all(key):
            raise ContractError("historical_model_identity_incomplete")
        previous = identity_rows.get(key)
        if previous is not None:
            previous_mode = str(previous.get("capture_mode") or "").strip().lower()
            current_mode = str(row.get("capture_mode") or "").strip().lower()
            if previous_mode != current_mode and {previous_mode, current_mode} <= {"", "historical_rebuild", "prospective"}:
                # A prospective native arrival is the stronger same-run
                # source when it overlaps a retrospective raw reconstruction;
                # keep one row and never mix their timestamps or hashes.
                if current_mode == "prospective":
                    identity_rows[key] = row
                continue
            for field in ("temperature_2m_c", "raw_message_sha256", "receipt_sha256"):
                if str(previous.get(field) or "") != str(row.get(field) or ""):
                    raise ContractError("historical_model_exact_key_conflict")
            continue
        row["station_id"] = key[0]
        row["event_date"] = key[1]
        row["provider_id"] = key[2]
        row["model_run_initialization_utc"] = key[3]
        identity_rows[key] = row

    # Collapse provider variants at the exact point/run grain before model
    # generation selection.  The native ICON provider is preferred whenever
    # it overlaps the Open-Meteo replay provider; no aggregator row can win a
    # role merely by arriving later in a packet.
    role_rows: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in identity_rows.values():
        role_key = (
            row["station_id"],
            row["event_date"],
            provider_role(row["provider_id"]),
            row["model_run_initialization_utc"],
            row["valid_time_utc"],
        )
        previous = role_rows.get(role_key)
        if previous is None or row["provider_id"] == "icon_global_official":
            role_rows[role_key] = row
    identity_rows = {
        (
            row["station_id"], row["event_date"], row["provider_id"],
            row["model_run_initialization_utc"], row["valid_time_utc"],
        ): row
        for row in role_rows.values()
    }

    # Small direct calibration fixtures and legacy prospective rows may only
    # contain one point per day and intentionally exercise the probability
    # contract rather than the native daily-curve transport contract.  Apply
    # historical generation selection only when capture provenance is present;
    # real native/historical packets are tagged by their adapters.
    if not any(str(row.get("capture_mode") or "").strip() for row in identity_rows.values()):
        return sorted(
            identity_rows.values(),
            key=lambda row: (
                row["station_id"], row["event_date"], row["provider_id"],
                row["model_run_initialization_utc"], row["valid_time_utc"],
            ),
        )

    by_station_day_run: dict[tuple[str, str, str], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in identity_rows.values():
        key = (row["station_id"], row["event_date"], row["model_run_initialization_utc"])
        by_station_day_run[key][row["provider_id"]].append(row)

    by_station_day: dict[tuple[str, str], list[tuple[str, dict[str, list[dict[str, Any]]]]]] = defaultdict(list)
    for (station, event_date, run_init), provider_rows in by_station_day_run.items():
        complete = {
            provider: provider_rows_for_run
            for provider, provider_rows_for_run in provider_rows.items()
            if _complete_curve(
                provider_rows_for_run,
                target_timezone=str(
                    provider_rows_for_run[0].get("target_timezone") or ""
                ),
            )
        }
        if complete:
            by_station_day[(station, event_date)].append((run_init, complete))

    selected: list[dict[str, Any]] = []
    for key, candidates in sorted(by_station_day.items()):
        def score(item: tuple[str, dict[str, list[dict[str, Any]]]]) -> tuple[int, int, int, int, int, str]:
            run_init, provider_rows = item
            providers = set(provider_rows)
            roles = {provider_role(provider) for provider in providers}
            native_count = sum(
                1
                for provider in providers
                if provider_source_class(provider) != "aggregator_republisher"
            )
            prospective = sum(
                1
                for rows_for_provider in provider_rows.values()
                if any(str(row.get("capture_mode") or "prospective").lower() != "historical_rebuild" for row in rows_for_provider)
            )
            points = sum(len(rows_for_provider) for rows_for_provider in provider_rows.values())
            return (int(roles == MODEL_ROLES), len(roles), native_count, prospective, points, run_init)

        _run_init, provider_rows = max(candidates, key=score)
        for rows_for_provider in provider_rows.values():
            selected.extend(rows_for_provider)
    return sorted(
        selected,
        key=lambda row: (
            row["station_id"], row["event_date"], row["provider_id"],
            row["model_run_initialization_utc"], row["valid_time_utc"],
        ),
    )


__all__ = [
    "AGGREGATOR_REBUILD_SCHEMA",
    "load_historical_model_rows",
    "select_preferred_model_rows",
]
