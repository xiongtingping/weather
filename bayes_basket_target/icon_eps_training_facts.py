"""Closed-day DWD ICON Global/EPS training facts.

This is deliberately a terminal, offline join: it consumes the verified
45-station daily-member archive and closed official AWC truth only.  It never
downloads a DWD object, writes SSOT tables, promotes a provider, or creates a
P0 packet.  The daily archive already proved the member curves; this module
only makes that proof usable once a station's market-local day is closed and
has one formal AWC label.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import math
from typing import Any

from .contracts import ContractError, content_sha256
from .icon_eps_daily_curve import (
    ENSEMBLE_SYSTEM_ID,
    EXPECTED_MEMBERS,
    FAMILY_ID,
    PROVIDER_ID,
    SCHEMA as DAILY_ARCHIVE_SCHEMA,
)
from .icon_eps_native_arrival_index import INDEX_SCHEMA as ARRIVAL_INDEX_SCHEMA
from .settlement_time import settlement_day_state
from .station_registry import coordinate_registry_manifest


TRAINING_FACT_SCHEMA = "dwd_icon_global_eps_official_training_fact_v1"
TRAINING_BATCH_SCHEMA = "dwd_icon_global_eps_official_training_fact_batch_v1"
MODEL_ID = "ICON"
GRID_UUID = "ae487d14fe2e11e4af85e50a2a56a360"
GRID_CELL_COUNT = 737_280
MAX_FACTS = 45
MAX_LEADS = 64
_ARCHIVE_STATE = "OFFICIAL_MEMBER_DAILY_CURVES_READY_STRICT_OOS_PENDING"
_CLOSED_TRUTH_AUTHORITY_SCOPE = "FORMAL_NATIVE_ELIGIBLE"


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"icon_eps_training_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"icon_eps_training_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"icon_eps_training_{name}_invalid_sha256")
    return result


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"icon_eps_training_{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"icon_eps_training_{name}_invalid") from exc
    if not math.isfinite(result) or not -100.0 <= result <= 100.0:
        raise ContractError(f"icon_eps_training_{name}_invalid")
    return result


def _identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in raw.items() if key != "content_sha256"}


def _verified_archive(archive: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Validate the archive and expose a bounded raw-lead lineage index."""

    if not isinstance(archive, Mapping):
        raise ContractError("icon_eps_training_archive_mapping_required")
    value = dict(archive)
    if value.get("content_sha256") != content_sha256(_identity(value)):
        raise ContractError("icon_eps_training_archive_sha_mismatch")
    authority = value.get("source_authority")
    captures = value.get("captures")
    expected_stations = {
        str(station) for station in coordinate_registry_manifest()["station_ids"]
    }
    source_arrival_eligible = (
        isinstance(authority, Mapping)
        and authority.get("native_arrival_index_eligible") is True
    )
    source_arrival_status = (
        "IMMUTABLE_OFFICIAL_NATIVE_ARRIVAL_INDEX_VERIFIED"
        if source_arrival_eligible
        else "PROVIDER_PUBLICATION_AND_LOCAL_RECEIPT_OBSERVED_INDEX_PENDING"
    )
    source_arrival_evidence = (
        _sha(
            "native_arrival_index_evidence",
            authority.get("native_arrival_index_evidence_sha256"),
        )
        if source_arrival_eligible
        else None
    )
    index_pointer = value.get("native_arrival_index")
    if source_arrival_eligible:
        if (
            not isinstance(index_pointer, Mapping)
            or index_pointer.get("schema") != ARRIVAL_INDEX_SCHEMA
            or _sha("native_arrival_index_pointer", index_pointer.get("content_sha256"))
            != source_arrival_evidence
            or int(index_pointer.get("entry_count") or 0) <= 0
            or _stamp(_utc("native_arrival_index_run", index_pointer.get("run_initialization_utc")))
            != _stamp(_utc("archive_run_initialization", value.get("run_init_utc")))
        ):
            raise ContractError("icon_eps_training_archive_arrival_index_invalid")
    if (
        value.get("schema") != DAILY_ARCHIVE_SCHEMA
        or value.get("state") != _ARCHIVE_STATE
        or value.get("ensemble_system_id") != ENSEMBLE_SYSTEM_ID
        or value.get("independence_family_id") != FAMILY_ID
        or value.get("station_count") != len(expected_stations)
        or value.get("member_count") != len(EXPECTED_MEMBERS)
        or value.get("capture_count") != len(expected_stations)
        or value.get("training_fact_eligible_after_closed_awc_truth") is not True
        or value.get("formal_p0_eligible") is not False
        or value.get("native_arrival_index_eligible") is not source_arrival_eligible
        or value.get("hard_no_eligible") is not False
        or not isinstance(authority, Mapping)
        or authority.get("source_role") != "official_native"
        or authority.get("transport_provider_id") != "DWD"
        or authority.get("native_publication_and_arrival_observed") is not True
        # Older retained archives predate the explicit arrival-index fields.
        # They remain index-pending.  A derived archive may switch to true
        # only when it points at one immutable, verified DWD object index.
        or authority.get("native_arrival_index_eligible", False)
        is not source_arrival_eligible
        or authority.get(
            "arrival_lineage_status",
            "PROVIDER_PUBLICATION_AND_LOCAL_RECEIPT_OBSERVED_INDEX_PENDING",
        ) != source_arrival_status
        or authority.get("raw_member_fanout") is not False
        or authority.get("official_grid_uuid") != GRID_UUID
        or authority.get("official_grid_cell_count") != GRID_CELL_COUNT
        or not isinstance(captures, list)
        or len(captures) != len(expected_stations)
    ):
        raise ContractError("icon_eps_training_archive_authority_invalid")

    run_init = _stamp(_utc("archive_run_initialization", value.get("run_init_utc")))
    event_date = str(value.get("event_date") or "").strip()
    if not event_date or str(value.get("run_cycle") or "") != run_init[11:13] + "Z":
        raise ContractError("icon_eps_training_archive_scope_invalid")

    normalized_captures: dict[str, dict[str, Any]] = {}
    for raw in captures:
        if not isinstance(raw, Mapping):
            raise ContractError("icon_eps_training_capture_mapping_required")
        capture = dict(raw)
        if capture.get("content_sha256") != content_sha256(_identity(capture)):
            raise ContractError("icon_eps_training_capture_sha_mismatch")
        station = str(capture.get("station_id") or "").strip().upper()
        timezone_name = str(capture.get("settlement_timezone") or "").strip()
        members = capture.get("members")
        raw_valid_times = capture.get("valid_times_utc")
        if (
            station not in expected_stations
            or station in normalized_captures
            or capture.get("event_date") != event_date
            or capture.get("run_init_utc") != run_init
            or capture.get("run_cycle") != run_init[11:13] + "Z"
            or capture.get("ensemble_system_id") != ENSEMBLE_SYSTEM_ID
            or capture.get("independence_family_id") != FAMILY_ID
            or capture.get("daily_curve_complete") is not True
            or capture.get("member_count") != len(EXPECTED_MEMBERS)
            or not timezone_name
            or not isinstance(members, list)
            or len(members) != len(EXPECTED_MEMBERS)
            or not isinstance(raw_valid_times, list)
            or not raw_valid_times
        ):
            raise ContractError("icon_eps_training_capture_scope_invalid")
        # This validates the IANA timezone and event-date shape without
        # introducing a global/ET clock into the station-local join.
        settlement_day_state(
            event_date,
            as_of_utc=run_init,
            settlement_timezone=timezone_name,
        )
        normalized_members: list[dict[str, Any]] = []
        seen_members: set[str] = set()
        for item in members:
            if not isinstance(item, Mapping):
                raise ContractError("icon_eps_training_member_mapping_required")
            member_id = str(item.get("member_id") or "").strip()
            component_shas = item.get("component_cohort_sha256")
            if (
                member_id not in {f"member_{member:02d}" for member in EXPECTED_MEMBERS}
                or member_id in seen_members
                or item.get("member_role") != "PERTURBED"
                or not isinstance(component_shas, list)
                or not component_shas
            ):
                raise ContractError("icon_eps_training_member_scope_invalid")
            seen_members.add(member_id)
            normalized_members.append({
                "member_id": member_id,
                "member_role": "PERTURBED",
                "daily_max_c": _finite("member_daily_max_c", item.get("raw_tmax_c")),
                "peak_valid_time_utc": _stamp(
                    _utc("member_peak_valid_time", item.get("peak_valid_time_utc"))
                ),
                "curve_sha256": _sha("member_curve", item.get("curve_sha256")),
                "component_cohort_sha256": sorted({
                    _sha("member_component_cohort", component)
                    for component in component_shas
                }),
            })
        expected_member_ids = {f"member_{member:02d}" for member in EXPECTED_MEMBERS}
        if seen_members != expected_member_ids:
            raise ContractError("icon_eps_training_member_scope_invalid")
        member_set_identity = [
            {
                "member_id": item["member_id"],
                "member_role": item["member_role"],
                "raw_tmax_c": item["daily_max_c"],
            }
            for item in sorted(normalized_members, key=lambda item: item["member_id"])
        ]
        if capture.get("member_set_sha256") != content_sha256(member_set_identity):
            raise ContractError("icon_eps_training_member_set_sha_mismatch")
        valid_times = tuple(
            _stamp(_utc("capture_valid_time", item)) for item in raw_valid_times
        )
        if len(valid_times) != len(set(valid_times)):
            raise ContractError("icon_eps_training_capture_valid_time_duplicate")
        normalized_captures[station] = {
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
            "content_sha256": _sha("daily_capture", capture.get("content_sha256")),
            "member_set_sha256": _sha("member_set", capture.get("member_set_sha256")),
            "members": sorted(normalized_members, key=lambda item: item["member_id"]),
            "valid_times_utc": valid_times,
        }
    if set(normalized_captures) != expected_stations:
        raise ContractError("icon_eps_training_capture_station_scope_invalid")

    # ``lead_results`` is intentionally embedded in the verified output
    # artifact.  It is the only accepted bridge from daily-member values to
    # DWD raw hashes; paths are operational scratch data and are excluded.
    leads = value.get("lead_results")
    component_by_valid = value.get("component_cohort_sha256_by_valid_time")
    if (
        not isinstance(leads, list)
        or not 1 <= len(leads) <= MAX_LEADS
        or not isinstance(component_by_valid, Mapping)
        or not component_by_valid
        or len(component_by_valid) != len(leads)
    ):
        raise ContractError("icon_eps_training_lead_lineage_missing")
    lead_by_cohort: dict[str, dict[str, Any]] = {}
    for raw in leads:
        if not isinstance(raw, Mapping):
            raise ContractError("icon_eps_training_lead_mapping_required")
        valid_time = _stamp(_utc("lead_valid_time", raw.get("valid_time_utc")))
        cohort_sha = _sha("lead_cohort", raw.get("cohort_content_sha256"))
        raw_sha = _sha("lead_raw", raw.get("raw_sha256"))
        try:
            forecast_hour = int(raw.get("forecast_hour"))
        except (TypeError, ValueError) as exc:
            raise ContractError("icon_eps_training_lead_forecast_hour_invalid") from exc
        if (
            not 0 <= forecast_hour <= 180
            or cohort_sha in lead_by_cohort
            or component_by_valid.get(valid_time) != cohort_sha
        ):
            raise ContractError("icon_eps_training_lead_lineage_invalid")
        lead_by_cohort[cohort_sha] = {
            "valid_time_utc": valid_time,
            "forecast_hour": forecast_hour,
            "raw_sha256": raw_sha,
            "cohort_content_sha256": cohort_sha,
        }
    canonical_component_by_valid = {
        _stamp(_utc("component_valid_time", valid_time)): _sha(
            "component_cohort", cohort_sha
        )
        for valid_time, cohort_sha in component_by_valid.items()
    }
    if (
        len(canonical_component_by_valid) != len(component_by_valid)
        or canonical_component_by_valid != {
            lead["valid_time_utc"]: cohort_sha
            for cohort_sha, lead in lead_by_cohort.items()
        }
    ):
        raise ContractError("icon_eps_training_lead_lineage_invalid")
    return {
        **value,
        "arrival_index_lineage": {
            "native_arrival_index_eligible": source_arrival_eligible,
            "arrival_lineage_status": source_arrival_status,
            "native_arrival_index_evidence_sha256": source_arrival_evidence,
        },
    }, {station: {**capture, "lead_by_cohort": lead_by_cohort} for station, capture in normalized_captures.items()}


def _truth_index(
    truths: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime,
) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in truths:
        if not isinstance(raw, Mapping):
            raise ContractError("icon_eps_training_truth_mapping_required")
        station = str(raw.get("station_id") or "").strip().upper()
        event_date = str(raw.get("local_date") or raw.get("event_date") or "").strip()
        timezone_name = str(raw.get("settlement_timezone") or "").strip()
        if (
            not station
            or not event_date
            or not timezone_name
            or str(raw.get("source_lane") or "").strip().upper() != "AWC"
            or str(raw.get("settlement_definition") or "") != "official_daily_tmax_c"
            or str(raw.get("qc_status") or "").strip().upper() != "ACCEPTED"
            or str(raw.get("authority_scope") or "")
            != _CLOSED_TRUTH_AUTHORITY_SCOPE
        ):
            raise ContractError("icon_eps_training_truth_authority_invalid")
        committed = _utc(
            "truth_canonical_committed",
            raw.get("canonical_committed_at_utc") or raw.get("canonical_committed_at"),
        )
        # A truth revision learned after the caller's replay point is not an
        # error; it is absent as-of that point and must not leak backward.
        if committed > as_of:
            continue
        closed = settlement_day_state(
            event_date,
            as_of_utc=_stamp(as_of),
            settlement_timezone=timezone_name,
        )
        if closed["state"] != "closed":
            continue
        item = {
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
            "truth_id": str(raw.get("truth_id") or "").strip(),
            "official_tmax_c": _finite("official_tmax_c", raw.get("official_tmax_c")),
            "truth_content_sha256": _sha("truth_content", raw.get("content_sha256")),
            "canonical_committed_at_utc": _stamp(committed),
            "settlement_closed_at_utc": str(closed["target_day_end_utc"]),
        }
        if not item["truth_id"]:
            raise ContractError("icon_eps_training_truth_id_missing")
        key = (station, event_date)
        previous = result.get(key)
        if previous is not None and previous != item:
            raise ContractError("icon_eps_training_truth_duplicate_conflict")
        result[key] = item
    return result


def build_icon_eps_training_facts(
    archive: Mapping[str, Any],
    awc_truths: Iterable[Mapping[str, Any]],
    *,
    as_of_utc: str,
) -> dict[str, Any]:
    """Build content-addressed training facts from one official daily archive.

    Missing and not-yet-closed truth are normal operational states: the
    method returns a valid zero-fact batch, never an inferred label.  Malformed
    archive or truth evidence raises ``ContractError`` instead.
    """

    as_of = _utc("as_of", as_of_utc)
    verified_archive, captures = _verified_archive(archive)
    unresolved: list[dict[str, str]] = []
    closed_stations: dict[str, dict[str, Any]] = {}
    for station, capture in sorted(captures.items()):
        state = settlement_day_state(
            capture["event_date"],
            as_of_utc=_stamp(as_of),
            settlement_timezone=capture["settlement_timezone"],
        )
        if state["state"] != "closed":
            unresolved.append({
                "station_id": station,
                "event_date": capture["event_date"],
                "settlement_timezone": capture["settlement_timezone"],
                "reason": "settlement_day_open_as_of",
            })
        else:
            closed_stations[station] = capture
    # Do not even inspect a future truth source before its forecast day is
    # closed.  This makes the early-as-of path safe for prospective runs.
    truth_by_scope = (
        _truth_index(awc_truths, as_of=as_of) if closed_stations else {}
    )
    facts: list[dict[str, Any]] = []
    for station, capture in sorted(closed_stations.items()):
        truth = truth_by_scope.get((station, capture["event_date"]))
        if truth is None:
            unresolved.append({
                "station_id": station,
                "event_date": capture["event_date"],
                "settlement_timezone": capture["settlement_timezone"],
                "reason": "official_awc_closed_truth_missing_as_of",
            })
            continue
        if truth["settlement_timezone"] != capture["settlement_timezone"]:
            raise ContractError("icon_eps_training_truth_timezone_mismatch")
        component_shas = sorted({
            component
            for member in capture["members"]
            for component in member["component_cohort_sha256"]
        })
        try:
            raw_leads = [capture["lead_by_cohort"][component] for component in component_shas]
        except KeyError as exc:
            raise ContractError("icon_eps_training_member_raw_lead_missing") from exc
        if {
            item["valid_time_utc"] for item in raw_leads
        } != set(capture["valid_times_utc"]):
            raise ContractError("icon_eps_training_member_raw_lead_time_mismatch")
        identity = {
            "schema": TRAINING_FACT_SCHEMA,
            "provider_id": PROVIDER_ID,
            "model_id": MODEL_ID,
            "ensemble_system_id": ENSEMBLE_SYSTEM_ID,
            "independence_family_id": FAMILY_ID,
            "station_id": station,
            "event_date": capture["event_date"],
            "settlement_timezone": capture["settlement_timezone"],
            "run_initialization_utc": verified_archive["run_init_utc"],
            "run_cycle": verified_archive["run_cycle"],
            "member_count": len(EXPECTED_MEMBERS),
            "member_daily_max_c": capture["members"],
            "member_set_sha256": capture["member_set_sha256"],
            "daily_curve_capture_sha256": capture["content_sha256"],
            "daily_archive_content_sha256": verified_archive["content_sha256"],
            # The archive query timestamp bounds the latest local receipt for
            # the full daily curve.  Strict OOS may use it only when the
            # immutable per-object DWD arrival index is present below.
            "forecast_available_at_utc": verified_archive["archive_query_at_utc"],
            "raw_leads": sorted(raw_leads, key=lambda item: item["valid_time_utc"]),
            "official_tmax_c": truth["official_tmax_c"],
            "truth_id": truth["truth_id"],
            "truth_content_sha256": truth["truth_content_sha256"],
            "truth_canonical_committed_at_utc": truth["canonical_committed_at_utc"],
            "settlement_closed_at_utc": truth["settlement_closed_at_utc"],
            "source_lineage": {
                "forecast_source_role": "official_native",
                "forecast_transport_provider_id": "DWD",
                "forecast_raw_member_fanout": False,
                "official_grid_uuid": GRID_UUID,
                "official_grid_cell_count": GRID_CELL_COUNT,
                "native_arrival_index_eligible": verified_archive[
                    "arrival_index_lineage"
                ]["native_arrival_index_eligible"],
                "arrival_lineage_status": verified_archive[
                    "arrival_index_lineage"
                ]["arrival_lineage_status"],
                "native_arrival_index_evidence_sha256": verified_archive[
                    "arrival_index_lineage"
                ]["native_arrival_index_evidence_sha256"],
                "truth_source_lane": "AWC",
                "truth_authority_scope": _CLOSED_TRUTH_AUTHORITY_SCOPE,
                "truth_closed_day_required": True,
            },
            "boundary": {
                "writes_target_database": False,
                "writes_production_database": False,
                "downloads_raw": False,
                "creates_formal_provider_binding": False,
                "activates_p0": False,
                "formal_p0_eligible": False,
                "calls_order_gateway": False,
                "orders_submitted": 0,
            },
        }
        digest = content_sha256(identity)
        facts.append({
            **identity,
            "training_fact_id": "icon-eps-" + digest[:32],
            "content_sha256": digest,
        })
    facts.sort(key=lambda item: item["station_id"])
    if not facts:
        state = "PENDING_CLOSED_AWC_TRUTH"
    elif unresolved:
        state = "PARTIAL_CLOSED_AWC_TRUTH"
    else:
        state = "ACCUMULATING_STRICT_OOS"
    identity = {
        "schema": TRAINING_BATCH_SCHEMA,
        "state": state,
        "as_of_utc": _stamp(as_of),
        "provider_id": PROVIDER_ID,
        "model_id": MODEL_ID,
        "ensemble_system_id": ENSEMBLE_SYSTEM_ID,
        "independence_family_id": FAMILY_ID,
        "daily_archive_content_sha256": verified_archive["content_sha256"],
        "run_initialization_utc": verified_archive["run_init_utc"],
        "event_date": verified_archive["event_date"],
        "station_count": len(captures),
        "fact_count": len(facts),
        "facts": facts,
        "unresolved_station_days": sorted(
            unresolved, key=lambda item: (item["station_id"], item["reason"])
        ),
        "strict_oos_state": "PENDING_ROLLING_STRICT_OOS",
        "formal_p0_eligible": False,
        "boundary": {
            "writes_target_database": False,
            "writes_production_database": False,
            "downloads_raw": False,
            "creates_formal_provider_binding": False,
            "activates_p0": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "TRAINING_BATCH_SCHEMA",
    "TRAINING_FACT_SCHEMA",
    "build_icon_eps_training_facts",
]
