"""Build a decision snapshot from official native ensemble archives.

The archives in one snapshot must agree on station-day, lead and the target
bucket contract, but they do not have to share a model run cycle.  A model
family's cycle is part of its own lineage and is never collapsed into a
synthetic common cycle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .contracts import ContractError, content_sha256
from .metar_physical_tmax import (
    EXPECTED_MEMBER_COUNT as METAR_PHYSICAL_EXPECTED_MEMBER_COUNT,
    INDEPENDENCE_FAMILY_ID as METAR_PHYSICAL_FAMILY_ID,
    PROVIDER_ID as METAR_PHYSICAL_PROVIDER_ID,
    SYSTEM_ID as METAR_PHYSICAL_SYSTEM_ID,
)


SCHEMA = "official_native_ensemble_forward_packet_v1"
NOAA_GEFS_MULTISTATION_SCHEMA = (
    "noaa_gefs_official_historical_multistation_members_v2"
)
ECMWF_MULTISTATION_SCHEMA = (
    "ecmwf_ifs_open_pf50_historical_multistation_members_v1"
)
ECCC_GEPS_MULTISTATION_SCHEMA = (
    "eccc_geps_official_historical_multistation_members_v1"
)
DWD_ICON_EPS_MULTISTATION_SCHEMA = (
    "dwd_icon_eps_official_historical_multistation_members_v1"
)
OFFICIAL_TAF_TX_MULTISTATION_SCHEMA = (
    "official_airport_taf_tx_point_members_v1"
)
SUPPORTED_ARCHIVE_SCHEMAS = {
    NOAA_GEFS_MULTISTATION_SCHEMA: "NOAA_GEFS_PDS_S3",
    ECMWF_MULTISTATION_SCHEMA: "ECMWF_OPEN_DATA_S3",
    ECCC_GEPS_MULTISTATION_SCHEMA: "ECCC_DD_WEATHER",
    DWD_ICON_EPS_MULTISTATION_SCHEMA: "DWD_OPENDATA_ICON_EPS",
    OFFICIAL_TAF_TX_MULTISTATION_SCHEMA: "AWC_TAF_DATA_API",
}
# The forecaster's TAF TX as a single-member point system.  Registered here --
# the shared upstream of both the OOS admission and the live compile -- so one
# registration serves both, and neither table CHECK (plural members) nor the
# NWP registry rows are stretched to cover a different kind of candidate.
# TAF issue instants jitter across the 6h UTC grid, so a point archive's
# ``run_cycle`` is its training-scope label (the sticky station-lead label the
# archive carries) while ``run_init_utc`` keeps the true issuance instant; the
# hour-equality check between the two is therefore waived for these systems
# and replaced by plain ordering against the query time.
POINT_FORECAST_SYSTEM_IDS = frozenset({
    "OFFICIAL_AIRPORT_TAF_TX",
    METAR_PHYSICAL_SYSTEM_ID,
})
POINT_FORECAST_SYSTEMS = {
    "OFFICIAL_AIRPORT_TAF_TX": {
        "ensemble_system_id": "OFFICIAL_AIRPORT_TAF_TX",
        "canonical_name": "Official Airport TAF TX Point",
        "independence_family_id": "HUMAN_FORECASTER",
        "provider_id": "ICAO_MET_OFFICE",
        "expected_member_count": 1,
        "content_sha256": content_sha256({
            "ensemble_system_id": "OFFICIAL_AIRPORT_TAF_TX",
            "member_identity_contract_version": "taf_tx_single_point_v1",
            "publication_contract_version": (
                "issue_time_known_amendments_supersede_v1"
            ),
        }),
    },
    # The day's own observations, forecasting the day's maximum without
    # consulting a model.  It sits in its own independence family for the
    # reason the family exists: the weight machinery aggregates within a
    # family before capping across families, so filing this under any NWP
    # family would let a source that shares none of their physics dilute the
    # cap that keeps them honest.  It is not the human forecaster either --
    # the TAF and this system disagree in useful ways, and collapsing them
    # would hide exactly the disagreement worth seeing.
    METAR_PHYSICAL_SYSTEM_ID: {
        "ensemble_system_id": METAR_PHYSICAL_SYSTEM_ID,
        "canonical_name": "METAR Physical Path Daily Maximum",
        "independence_family_id": METAR_PHYSICAL_FAMILY_ID,
        "provider_id": METAR_PHYSICAL_PROVIDER_ID,
        # One, like the TAF.  METAR is a single observation stream producing a
        # single forecast; it has no exchangeable draws to contribute, and its
        # uncertainty is measured from its record rather than declared here.
        "expected_member_count": METAR_PHYSICAL_EXPECTED_MEMBER_COUNT,
        "content_sha256": content_sha256({
            "ensemble_system_id": METAR_PHYSICAL_SYSTEM_ID,
            "member_identity_contract_version": (
                "metar_physical_tmax_single_point_v1"
            ),
            "publication_contract_version": (
                "recomputed_on_each_canonical_observation_v1"
            ),
        }),
    },
}
# Only a fallback, for archives written before collectors stated their lead.
# Every current archive carries ``archive_lead_bucket`` derived from the run it
# was actually drawn from, and that always wins.
ARCHIVE_LEAD_BY_SCHEMA = {
    schema: "d2" for schema in SUPPORTED_ARCHIVE_SCHEMAS
}
# P0 is recomputed as the models update, so the same station-day is captured at
# d2, again at d1, and again at d0.  This builder used to accept d2 alone: the
# collectors were parameterised, the scheduler emitted all three leads, the
# worker passed the lead through and the archives came back correctly labelled
# -- and then every shorter-lead capture died here with
# ``official_native_forward_scope_mismatch`` after doing all the downloading.
# Nothing but d2 had ever reached a packet.
SUPPORTED_ARCHIVE_LEAD_BUCKETS = frozenset({"d0", "d1", "d2"})
VALID_RUN_CYCLES = frozenset({"00Z", "06Z", "12Z", "18Z"})
MIXED_NATIVE_CYCLES = "MIXED_NATIVE_CYCLES"


def _verified_utc_timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ContractError(
            "official_native_forward_lineage_timestamp_missing"
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "official_native_forward_lineage_timestamp_invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise ContractError(
            "official_native_forward_lineage_timestamp_invalid"
        )
    return parsed.astimezone(timezone.utc)


def _verified_archive(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ContractError("official_native_forward_archive_invalid")
    archive = dict(raw)
    identity = {
        key: value
        for key, value in archive.items()
        if key != "content_sha256"
    }
    if (
        archive.get("schema") not in SUPPORTED_ARCHIVE_SCHEMAS
        or archive.get("content_sha256") != content_sha256(identity)
        or archive.get("training_only") is not True
        or archive.get("holdout_eligible") is not False
        or not isinstance(archive.get("captures"), list)
    ):
        raise ContractError(
            "official_native_forward_archive_invalid"
        )
    return archive


def _verified_target_bucket_contract(
    raw: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
) -> dict[str, Any]:
    contract = dict(raw)
    intervals = contract.get("bucket_intervals")
    authority_source_class = str(
        contract.get("authority_source_class")
        or "AUTHORITATIVE_MARKET_REGISTRY_STATION_DAY"
    )
    if (
        contract.get("schema")
        != "official_native_forward_target_bucket_contract_v1"
        or str(contract.get("station_id") or "").strip().upper()
        != station_id
        or str(contract.get("event_date") or "") != event_date
        or not str(contract.get("settlement_timezone") or "")
        or not isinstance(intervals, Mapping)
        or not intervals
        or len(str(contract.get("authority_content_sha256") or ""))
        != 64
        or authority_source_class not in {
            "AUTHORITATIVE_MARKET_REGISTRY_STATION_DAY",
            "SOURCE_ARCHIVE_OFFLINE_CONTRACT_BRIDGE",
        }
    ):
        raise ContractError(
            "official_native_forward_target_contract_invalid"
        )
    frozen: dict[str, list[float | None]] = {}
    ordered: list[tuple[str, float | None, float | None]] = []
    for bucket, raw_bounds in intervals.items():
        if (
            not str(bucket)
            or not isinstance(raw_bounds, Sequence)
            or isinstance(raw_bounds, (str, bytes, bytearray))
            or len(raw_bounds) != 2
        ):
            raise ContractError(
                "official_native_forward_target_intervals_invalid"
            )
        lower = (
            None
            if raw_bounds[0] is None
            else float(raw_bounds[0])
        )
        upper = (
            None
            if raw_bounds[1] is None
            else float(raw_bounds[1])
        )
        if lower is not None and upper is not None and lower >= upper:
            raise ContractError(
                "official_native_forward_target_intervals_invalid"
            )
        frozen[str(bucket)] = [lower, upper]
        ordered.append((str(bucket), lower, upper))
    ordered.sort(key=lambda row: (
        float("-inf") if row[1] is None else row[1],
        float("inf") if row[2] is None else row[2],
    ))
    if (
        ordered[0][1] is not None
        or ordered[-1][2] is not None
        or any(
            left[2] is None
            or right[1] is None
            or left[2] != right[1]
            for left, right in zip(ordered, ordered[1:])
        )
        or content_sha256(frozen)
        != str(contract.get("bucket_contract_sha256") or "")
    ):
        raise ContractError(
            "official_native_forward_target_intervals_invalid"
        )
    return {
        **contract,
        "station_id": station_id,
        "bucket_intervals": frozen,
        "authority_source_class": authority_source_class,
    }


def build_official_native_forward_packet(
    *,
    archives: Sequence[Mapping[str, Any]],
    station_id: str,
    target_bucket_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge official member captures for one station-day."""

    if (
        not isinstance(archives, Sequence)
        or isinstance(archives, (str, bytes, bytearray))
        or len(archives) < 2
        or len(archives) > 8
    ):
        raise ContractError(
            "official_native_forward_archive_set_invalid"
        )
    station = str(station_id or "").strip().upper()
    if not station:
        raise ContractError(
            "official_native_forward_station_invalid"
        )
    verified = [_verified_archive(row) for row in archives]
    event_dates = {str(row.get("event_date") or "") for row in verified}
    lead_buckets = {
        str(
            row.get("archive_lead_bucket")
            or (
                row["captures"][0].get("archive_lead_bucket")
                if row["captures"]
                and isinstance(row["captures"][0], Mapping)
                else ""
            )
            or ARCHIVE_LEAD_BY_SCHEMA.get(str(row.get("schema")), "")
            or ""
        )
        for row in verified
    }
    system_ids = {
        str(row.get("ensemble_system_id") or "") for row in verified
    }
    family_ids = {
        str(row.get("independence_family_id") or "")
        for row in verified
    }
    archive_metadata_by_system: dict[str, dict[str, Any]] = {}
    for archive in verified:
        system_id = str(archive.get("ensemble_system_id") or "")
        run_cycle = str(archive.get("run_cycle") or "")
        archive_lead = str(
            archive.get("archive_lead_bucket")
            or (
                archive["captures"][0].get("archive_lead_bucket")
                if archive["captures"]
                and isinstance(archive["captures"][0], Mapping)
                else ""
            )
            or ARCHIVE_LEAD_BY_SCHEMA.get(str(archive.get("schema")), "")
            or ""
        )
        run_init_utc = str(archive.get("run_init_utc") or "").strip()
        archive_query_at_utc = str(
            archive.get("archive_query_at_utc") or ""
        ).strip()
        if (
            not system_id
            or system_id in archive_metadata_by_system
            or run_cycle not in VALID_RUN_CYCLES
            or archive_lead not in SUPPORTED_ARCHIVE_LEAD_BUCKETS
            or not run_init_utc
            or not archive_query_at_utc
        ):
            raise ContractError(
                "official_native_forward_scope_mismatch"
            )
        run_init = _verified_utc_timestamp(run_init_utc)
        archive_query = _verified_utc_timestamp(archive_query_at_utc)
        if (
            (
                system_id not in POINT_FORECAST_SYSTEM_IDS
                and run_init.hour != int(run_cycle[:2])
            )
            or archive_query < run_init
        ):
            # A model run's init hour is its cycle by definition.  A point
            # forecast's cycle is a scope label over a jittering issuance
            # instant, so only the temporal ordering is checked for those.
            raise ContractError(
                "official_native_forward_run_cycle_lineage_mismatch"
            )
        archive_metadata_by_system[system_id] = {
            "run_cycle": run_cycle,
            "archive_lead_bucket": archive_lead,
            "run_init_utc": run_init_utc,
            "archive_query_at_utc": archive_query_at_utc,
            "run_init": run_init,
            "archive_query": archive_query,
        }
    run_cycles = {
        str(metadata["run_cycle"])
        for metadata in archive_metadata_by_system.values()
    }
    if (
        len(event_dates) != 1
        or "" in event_dates
        # One lead per packet, not one particular lead.  Mixing a d1 archive
        # with a d2 one would blend two different forecasts of the same day
        # into a single cross-family ensemble and label the result with
        # whichever lead won the set.
        or len(lead_buckets) != 1
        or not lead_buckets.issubset(SUPPORTED_ARCHIVE_LEAD_BUCKETS)
        or not run_cycles.issubset(VALID_RUN_CYCLES)
        or len(system_ids) != len(verified)
        or "" in system_ids
        or len(family_ids) < 2
        or "" in family_ids
        or set(archive_metadata_by_system) != system_ids
    ):
        raise ContractError(
            "official_native_forward_scope_mismatch"
        )
    event_date = next(iter(event_dates))
    target_contract = (
        None
        if target_bucket_contract is None
        else _verified_target_bucket_contract(
            target_bucket_contract,
            station_id=station,
            event_date=event_date,
        )
    )
    captures: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    for archive in verified:
        matches = [
            dict(row)
            for row in archive["captures"]
            if isinstance(row, Mapping)
            and str(row.get("station_id") or "").strip().upper()
            == station
        ]
        if len(matches) != 1:
            raise ContractError(
                "official_native_forward_station_capture_missing"
            )
        capture = matches[0]
        system_id = str(archive["ensemble_system_id"])
        archive_metadata = archive_metadata_by_system[system_id]
        archive_cycle = str(archive_metadata["run_cycle"])
        capture_lead = str(capture.get("archive_lead_bucket") or "")
        capture_identity = {
            key: value
            for key, value in capture.items()
            if key != "content_sha256"
        }
        if (
            capture.get("content_sha256")
            != content_sha256(capture_identity)
            or str(capture.get("ensemble_system_id") or "")
            != str(archive["ensemble_system_id"])
            or str(capture.get("independence_family_id") or "")
            != str(archive["independence_family_id"])
            or str(capture.get("event_date") or "")
            != event_date
            or str(capture.get("run_cycle") or "")
            != archive_cycle
            or (
                capture_lead
                and capture_lead
                != str(archive_metadata["archive_lead_bucket"])
            )
            or not isinstance(capture.get("members"), list)
            or len(capture["members"])
            != int(
                archive.get("member_count")
                or capture.get("member_count")
                or 0
            )
        ):
            raise ContractError(
                "official_native_forward_station_capture_invalid"
            )
        captures.append(capture)
        source_rows.append({
            "ensemble_system_id": str(
                archive["ensemble_system_id"]
            ),
            "independence_family_id": str(
                archive["independence_family_id"]
            ),
            "transport_provider_id": SUPPORTED_ARCHIVE_SCHEMAS[
                str(archive["schema"])
            ],
            "run_cycle": archive_cycle,
            "run_init_utc": str(archive_metadata["run_init_utc"]),
            "archive_query_at_utc": str(
                archive_metadata["archive_query_at_utc"]
            ),
            "archive_content_sha256": str(
                archive["content_sha256"]
            ),
            "station_capture_content_sha256": str(
                capture["content_sha256"]
            ),
            "source_bucket_contract_sha256": str(
                capture["bucket_contract_sha256"]
            ),
            **(
                {
                    "family_temperature_curve_sha256": str(
                        capture["family_temperature_curve"][
                            "content_sha256"
                        ]
                    )
                }
                if isinstance(
                    capture.get("family_temperature_curve"), Mapping
                )
                else {}
            ),
        })
    timezones = {
        str(row.get("settlement_timezone") or "") for row in captures
    }
    if len(timezones) != 1 or "" in timezones:
        raise ContractError(
            "official_native_forward_station_contract_mismatch"
        )
    if target_contract is None:
        interval_hashes = {
            str(row.get("bucket_contract_sha256") or "")
            for row in captures
        }
        if (
            len(interval_hashes) != 1
            or "" in interval_hashes
            or any(
                row.get("bucket_intervals")
                != captures[0].get("bucket_intervals")
                for row in captures[1:]
            )
        ):
            raise ContractError(
                "official_native_forward_station_contract_mismatch"
            )
        bucket_intervals = dict(captures[0]["bucket_intervals"])
        bucket_contract_sha = next(iter(interval_hashes))
        bucket_contract_authority = {
            "source_class": "SOURCE_ARCHIVES_EXACT_CONTRACT",
            "authority_content_sha256": bucket_contract_sha,
        }
    else:
        if (
            str(target_contract["settlement_timezone"])
            != next(iter(timezones))
        ):
            raise ContractError(
                "official_native_forward_station_contract_mismatch"
            )
        bucket_intervals = dict(target_contract["bucket_intervals"])
        bucket_contract_sha = str(
            target_contract["bucket_contract_sha256"]
        )
        bucket_contract_authority = {
            "source_class": str(
                target_contract["authority_source_class"]
            ),
            "authority_content_sha256": str(
                target_contract["authority_content_sha256"]
            ),
        }
    received_at = max(
        _verified_utc_timestamp(row["archive_query_at_utc"])
        for row in source_rows
    )
    source_rows.sort(key=lambda row: row["ensemble_system_id"])
    source_by_system = {
        str(row["ensemble_system_id"]): row for row in source_rows
    }
    run_cycle_by_system = {
        system_id: str(row["run_cycle"])
        for system_id, row in source_by_system.items()
    }
    run_init_utc_by_system = {
        system_id: str(row["run_init_utc"])
        for system_id, row in source_by_system.items()
    }
    archive_query_at_utc_by_system = {
        system_id: str(row["archive_query_at_utc"])
        for system_id, row in source_by_system.items()
    }
    availability_identity = {
        "source_class": "OFFICIAL_NATIVE_ARCHIVE_QUERY_RECEIPT",
        "available_at_definition": "MAX_ARCHIVE_QUERY_AT_UTC",
        "available_at_utc": received_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "by_system": {
            system_id: {
                "run_cycle": str(row["run_cycle"]),
                "run_init_utc": str(row["run_init_utc"]),
                "archive_query_at_utc": str(
                    row["archive_query_at_utc"]
                ),
                "archive_content_sha256": str(
                    row["archive_content_sha256"]
                ),
                "station_capture_content_sha256": str(
                    row["station_capture_content_sha256"]
                ),
            }
            for system_id, row in source_by_system.items()
        },
    }
    availability_evidence = {
        **availability_identity,
        "content_sha256": content_sha256(availability_identity),
    }
    run_inits = {
        str(row["run_init_utc"]) for row in source_rows
    }
    same_native_cycle = len(run_cycles) == 1 and len(run_inits) == 1
    packet_run_cycle = (
        next(iter(run_cycles)) if same_native_cycle else MIXED_NATIVE_CYCLES
    )
    run_cycle_scope = (
        "SAME_NATIVE_CYCLE"
        if same_native_cycle
        else "PER_SYSTEM_NATIVE_CYCLE"
    )
    source_class = (
        "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT"
        if same_native_cycle
        else "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT"
    )
    systems = [
        {
            "ensemble_system_id": str(
                capture["ensemble_system_id"]
            ),
            "independence_family_id": str(
                capture["independence_family_id"]
            ),
            "run_cycle": str(
                source_by_system[str(capture["ensemble_system_id"])]
                ["run_cycle"]
            ),
            "run_init_utc": str(
                source_by_system[str(capture["ensemble_system_id"])]
                ["run_init_utc"]
            ),
            "archive_query_at_utc": str(
                source_by_system[str(capture["ensemble_system_id"])]
                ["archive_query_at_utc"]
            ),
            "member_count": int(capture["member_count"]),
            "member_set_sha256": str(
                capture["member_set_sha256"]
            ),
            "members": list(capture["members"]),
            **(
                {
                    "family_temperature_curve": dict(
                        capture["family_temperature_curve"]
                    )
                }
                if isinstance(
                    capture.get("family_temperature_curve"), Mapping
                )
                else {}
            ),
        }
        for capture in sorted(
            captures,
            key=lambda row: str(row["ensemble_system_id"]),
        )
    ]
    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": next(iter(timezones)),
        "lead_bucket": next(iter(lead_buckets)),
        "run_cycle": packet_run_cycle,
        "run_cycle_scope": run_cycle_scope,
        "run_cycle_by_system": run_cycle_by_system,
        "run_init_utc_by_system": run_init_utc_by_system,
        "archive_query_at_utc_by_system": (
            archive_query_at_utc_by_system
        ),
        "received_at_utc": received_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "availability_evidence": availability_evidence,
        "bucket_intervals": bucket_intervals,
        "bucket_contract_sha256": bucket_contract_sha,
        "bucket_contract_authority": bucket_contract_authority,
        "systems": systems,
        "source_archives": source_rows,
        "source_archive_set_sha256": content_sha256(source_rows),
        "source_class": source_class,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {
        **identity,
        "snapshot_content_sha256": content_sha256(identity),
    }


__all__ = [
    "SCHEMA",
    "ARCHIVE_LEAD_BY_SCHEMA",
    "SUPPORTED_ARCHIVE_LEAD_BUCKETS",
    "VALID_RUN_CYCLES",
    "MIXED_NATIVE_CYCLES",
    "SUPPORTED_ARCHIVE_SCHEMAS",
    "build_official_native_forward_packet",
]
