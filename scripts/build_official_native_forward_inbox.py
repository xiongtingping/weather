#!/usr/bin/env python3
"""Build bounded official strict-as-of native-member forward packets.

Every native system retains its own cycle and initialization lineage, so one
snapshot may combine (for example) GEFS/ECMWF 18Z with ECCC GEPS 00Z.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.ecmwf_aifs_ens_archive import (  # noqa: E402
    INDEPENDENCE_FAMILY_ID as AIFS_ENS_FAMILY_ID,
    PRE_SPLIT_INDEPENDENCE_FAMILY_ID as AIFS_ENS_PRE_SPLIT_FAMILY_ID,
)
from bayes_basket_target.market_registry import (  # noqa: E402
    load_market_registry_snapshot,
)
from bayes_basket_target.official_native_forward_packet import (  # noqa: E402
    DWD_ICON_EPS_MULTISTATION_SCHEMA,
    ECMWF_AIFS_ENS_MULTISTATION_SCHEMA,
    ECCC_GEPS_MULTISTATION_SCHEMA,
    NOAA_AIGEFS_MULTISTATION_SCHEMA,
    ECMWF_MULTISTATION_SCHEMA,
    NOAA_GEFS_MULTISTATION_SCHEMA,
    build_official_native_forward_packet,
)
from bayes_basket_target.p0_producer import (  # noqa: E402
    bucket_intervals_from_registry_rows,
    market_bucket_bridge_identity,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)


_ARCHIVE_SCHEMA_BY_ROLE = {
    "NOAA_GEFS": NOAA_GEFS_MULTISTATION_SCHEMA,
    "ECMWF": ECMWF_MULTISTATION_SCHEMA,
    "ECCC_GEM": ECCC_GEPS_MULTISTATION_SCHEMA,
    "DWD_ICON_EPS": DWD_ICON_EPS_MULTISTATION_SCHEMA,
}
_ARCHIVE_FAMILY_BY_SCHEMA = {
    schema: family
    for family, schema in _ARCHIVE_SCHEMA_BY_ROLE.items()
}
_SUPPORTED_ARCHIVE_SCHEMAS = frozenset(_ARCHIVE_FAMILY_BY_SCHEMA)
# Charter 204: AIFS ENS is a follow-only family -- it never comes in as a
# base ``--archive`` (the ladder has no AIFS rung), only per station as a
# ``--follow-archive`` that is ADDED to the station's set, since there is no
# AIFS base archive for it to replace.
#
# The families a follow archive may carry, per schema.  Charter 217 moved AIFS
# ENS out of the ECMWF family into its own, and the archive module has written
# that family since (``ecmwf_aifs_ens_archive.INDEPENDENCE_FAMILY_ID``); this
# map still said "ECMWF", so from the first archive captured under the split
# (2026-09-25 06:16Z) every build that took one refused the whole inbox --
# ``official_native_forward_follow_archive_invalid``, capture exit 2 on every
# rung of every date -- and at 11:43Z the last P0 of 2026-09-26 expired with
# forty-five stations listed and none fresh.  The family is read from the
# module that writes it, not restated.  The pre-split label stays admissible
# for the archives captured before the split: they are the same product, the
# registry row itself was relabelled in place (charter 217), and the compile
# weighs, caps and records a system under the registry's family, never the
# label on the archive -- while one refused follow archive costs the build
# every station, not only its own.
_FOLLOW_FAMILIES_BY_SCHEMA = {
    **{
        schema: frozenset({family})
        for schema, family in _ARCHIVE_FAMILY_BY_SCHEMA.items()
    },
    ECMWF_AIFS_ENS_MULTISTATION_SCHEMA: frozenset({
        AIFS_ENS_FAMILY_ID, AIFS_ENS_PRE_SPLIT_FAMILY_ID,
    }),
    # Charter 212: NOAA AIGEFS, its own family, follow-only like AIFS.
    NOAA_AIGEFS_MULTISTATION_SCHEMA: frozenset({"NOAA_AIGEFS"}),
}


def _load_archives(
    archive_paths: list[Path],
    *,
    explicit_eccc_paths: list[Path],
) -> list[Mapping[str, object]]:
    """Load required GEFS/ECMWF and one optional official GEPS archive."""

    if not archive_paths:
        raise RuntimeError(
            "official_native_forward_required_archives_missing"
        )
    explicit_eccc = {
        path.expanduser().resolve(strict=True)
        for path in explicit_eccc_paths
    }
    resolved_paths = [
        path.expanduser().resolve(strict=True)
        for path in archive_paths
    ]
    if len(set(resolved_paths)) != len(resolved_paths):
        raise RuntimeError(
            "official_native_forward_archive_duplicate_path"
        )
    archives: list[Mapping[str, object]] = []
    for path in resolved_paths:
        raw = load_bounded_json(path)
        if not isinstance(raw, Mapping):
            raise RuntimeError("official_native_forward_archive_invalid")
        archive = dict(raw)
        schema = str(archive.get("schema") or "")
        if schema not in _SUPPORTED_ARCHIVE_SCHEMAS:
            raise RuntimeError(
                "official_native_forward_archive_schema_unsupported:"
                + schema
            )
        if path in explicit_eccc and schema != ECCC_GEPS_MULTISTATION_SCHEMA:
            raise RuntimeError(
                "official_native_forward_eccc_archive_schema_invalid"
            )
        expected_family = _ARCHIVE_FAMILY_BY_SCHEMA[schema]
        if str(archive.get("independence_family_id") or "") != expected_family:
            raise RuntimeError(
                "official_native_forward_archive_family_invalid:"
                + schema
            )
        archives.append(archive)

    schemas = [str(archive.get("schema") or "") for archive in archives]
    counts = {
        schema: schemas.count(schema)
        for schema in _SUPPORTED_ARCHIVE_SCHEMAS
    }
    if counts[NOAA_GEFS_MULTISTATION_SCHEMA] != 1:
        raise RuntimeError(
            "official_native_forward_required_gefs_archive_missing_or_duplicate"
        )
    if counts[ECMWF_MULTISTATION_SCHEMA] != 1:
        raise RuntimeError(
            "official_native_forward_required_ecmwf_archive_missing_or_duplicate"
        )
    if counts[ECCC_GEPS_MULTISTATION_SCHEMA] > 1:
        raise RuntimeError(
            "official_native_forward_optional_eccc_archive_duplicate"
        )
    if counts[DWD_ICON_EPS_MULTISTATION_SCHEMA] > 1:
        raise RuntimeError(
            "official_native_forward_optional_icon_archive_duplicate"
        )
    # GEFS and ECMWF required, ECCC and ICON each optional.  The old {2, 3}
    # cap predated ICON's admission to the forward lane and rejected every
    # four-archive capture outright -- which, once the resident worker began
    # passing --icon-output, silently stalled every station's P0 recompile
    # behind an "archive set invalid" that named no family.
    if len(archives) not in {2, 3, 4}:
        raise RuntimeError("official_native_forward_archive_set_invalid")
    return archives


def _common_station_ids(
    archives: list[Mapping[str, object]],
) -> set[str]:
    station_sets: list[set[str]] = []
    for archive in archives:
        captures = archive.get("captures")
        if not isinstance(captures, list):
            raise RuntimeError("official_native_forward_archive_invalid")
        station_sets.append({
            str(capture.get("station_id") or "").strip().upper()
            for capture in captures
            if isinstance(capture, Mapping)
            and str(capture.get("station_id") or "").strip()
        })
    if not station_sets:
        return set()
    return set.intersection(*station_sets)


def _archive_station_ids(archive: Mapping[str, object]) -> set[str]:
    captures = archive.get("captures")
    if not isinstance(captures, list):
        raise RuntimeError("official_native_forward_archive_invalid")
    return {
        str(capture.get("station_id") or "").strip().upper()
        for capture in captures
        if isinstance(capture, Mapping)
        and str(capture.get("station_id") or "").strip()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        type=Path,
        action="append",
        default=[],
        help=(
            "Official native archive; repeat for each required native "
            "system (the optional GEPS archive may also be supplied here)."
        ),
    )
    parser.add_argument(
        "--eccc-geps-archive",
        type=Path,
        action="append",
        default=[],
        help=(
            "Optional official ECCC GEPS archive. Repeatable CLI option; "
            "one ECCC archive is valid per station-day packet."
        ),
    )
    parser.add_argument(
        "--eccc-station-id",
        action="append",
        default=[],
        help=(
            "Restrict the optional ECCC GEPS system to these stations. "
            "Stations outside this set remain the exact ECMWF+GEFS pair."
        ),
    )
    parser.add_argument(
        "--station-id",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--market-registry",
        type=Path,
        help=(
            "Authoritative current station-day market registry. "
            "Required when source archives froze different bucket contracts."
        ),
    )
    parser.add_argument(
        "--target-contract-archive",
        type=Path,
        help=(
            "Offline train-only bridge: project all systems onto the exact "
            "station contract frozen by one supplied source archive. Cannot "
            "be combined with --market-registry."
        ),
    )
    parser.add_argument(
        "--regional-point-archive",
        type=Path,
        help=(
            "Per-station regional deterministic point archives (charter 95); "
            "each station present gets its regional models as further packet "
            "systems, absent stations keep the set they had."
        ),
    )
    parser.add_argument(
        "--follow-archive",
        type=Path,
        action="append",
        default=[],
        help=(
            "Charter 192: a retained ICON or ECCC follow archive (a newer run "
            "than the family's archive above).  Read only for the stations "
            "--follow-assignment names."
        ),
    )
    parser.add_argument(
        "--follow-assignment",
        action="append",
        default=[],
        help=(
            "STATION=PATH: this station takes the family's run from that "
            "follow archive instead of the family's archive above."
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        args.market_registry is not None
        and args.target_contract_archive is not None
    ):
        raise RuntimeError(
            "official_native_forward_target_contract_authority_ambiguous"
        )
    archive_paths = [*args.archive, *args.eccc_geps_archive]
    archives = _load_archives(
        archive_paths,
        explicit_eccc_paths=list(args.eccc_geps_archive),
    )
    base_archives = [
        archive
        for archive in archives
        if str(archive.get("schema") or "")
        in {
            NOAA_GEFS_MULTISTATION_SCHEMA,
            ECMWF_MULTISTATION_SCHEMA,
        }
    ]
    eccc_archives = [
        archive
        for archive in archives
        if str(archive.get("schema") or "")
        == ECCC_GEPS_MULTISTATION_SCHEMA
    ]
    eccc_archive = eccc_archives[0] if eccc_archives else None
    # ICON is optional in exactly the way ECCC is, and needs the same partition
    # to reach a packet.  Without it the archive was accepted by the loader --
    # right schema, right family, inside the four-archive cap -- and then never
    # read, so every ``--archive icon_*`` argument was validated and dropped.
    # The capture had been paying for those objects daily and no packet ever
    # carried the family.
    icon_archives = [
        archive
        for archive in archives
        if str(archive.get("schema") or "")
        == DWD_ICON_EPS_MULTISTATION_SCHEMA
    ]
    icon_archive = icon_archives[0] if icon_archives else None
    requested_eccc_stations = {
        str(value or "").strip().upper()
        for value in args.eccc_station_id
        if str(value or "").strip()
    }
    if requested_eccc_stations and eccc_archive is None:
        raise RuntimeError(
            "official_native_forward_eccc_station_scope_without_archive"
        )
    if requested_eccc_stations and not requested_eccc_stations.issubset(
        _archive_station_ids(eccc_archive)
    ):
        raise RuntimeError(
            "official_native_forward_eccc_station_scope_invalid"
        )
    available = _common_station_ids(base_archives)
    requested = {
        str(value or "").strip().upper()
        for value in args.station_id
        if str(value or "").strip()
    }
    stations = sorted(requested or available)
    if (
        not stations
        or len(stations) > expected_station_count()
        or not set(stations).issubset(available)
    ):
        raise RuntimeError("official_native_forward_station_scope_invalid")
    target_contracts: dict[str, dict[str, object]] = {}
    base_event_dates = {
        str(archive.get("event_date") or "")
        for archive in base_archives
    }
    if len(base_event_dates) != 1 or "" in base_event_dates:
        raise RuntimeError("official_native_forward_archive_date_invalid")
    base_event_date = next(iter(base_event_dates))
    if (
        eccc_archive is not None
        and str(eccc_archive.get("event_date") or "") != base_event_date
    ):
        raise RuntimeError("official_native_forward_archive_date_invalid")
    if (
        icon_archive is not None
        and str(icon_archive.get("event_date") or "") != base_event_date
    ):
        raise RuntimeError("official_native_forward_archive_date_invalid")
    if args.market_registry is not None:
        registry = load_market_registry_snapshot(
            args.market_registry.expanduser().resolve(strict=True)
        )
        event_date = base_event_date
        for station in stations:
            rows = [
                row
                for row in registry["rows"]
                if str(row.get("station_id") or "").strip().upper()
                == station
                and str(row.get("event_date") or "") == event_date
                and int(row.get("market_active") or 0) == 1
                and int(row.get("source_eligible") or 0) == 1
            ]
            if not rows:
                raise RuntimeError(
                    "official_native_forward_target_registry_scope_missing:"
                    f"{station}:{event_date}"
                )
            timezones = {
                str(row.get("settlement_timezone") or "")
                for row in rows
            }
            if len(timezones) != 1 or "" in timezones:
                raise RuntimeError(
                    "official_native_forward_target_registry_timezone_invalid:"
                    f"{station}:{event_date}"
                )
            intervals = bucket_intervals_from_registry_rows(rows)
            frozen = {
                bucket: [
                    None if lower is None else float(lower),
                    None if upper is None else float(upper),
                ]
                for bucket, (lower, upper) in intervals.items()
            }
            bridge = market_bucket_bridge_identity(
                rows,
                bucket_intervals=intervals,
                settlement_timezone=next(iter(timezones)),
            )
            target_contracts[station] = {
                "schema": (
                    "official_native_forward_target_bucket_contract_v1"
                ),
                "station_id": station,
                "event_date": event_date,
                "settlement_timezone": next(iter(timezones)),
                "bucket_intervals": frozen,
                "bucket_contract_sha256": content_sha256(frozen),
                "authority_content_sha256": content_sha256(bridge),
                "authority_source_class": (
                    "AUTHORITATIVE_MARKET_REGISTRY_STATION_DAY"
                ),
            }
    elif args.target_contract_archive is not None:
        target_raw = load_bounded_json(
            args.target_contract_archive.expanduser().resolve(strict=True)
        )
        if not isinstance(target_raw, Mapping):
            raise RuntimeError(
                "official_native_forward_target_archive_invalid"
            )
        target_sha = str(target_raw.get("content_sha256") or "")
        target_archive = next(
            (
                archive
                for archive in archives
                if str(archive.get("content_sha256") or "") == target_sha
            ),
            None,
        )
        if target_archive is None:
            raise RuntimeError(
                "official_native_forward_target_archive_not_supplied"
            )
        captures = target_archive.get("captures")
        if not isinstance(captures, list):
            raise RuntimeError(
                "official_native_forward_target_archive_invalid"
            )
        capture_by_station = {
            str(capture.get("station_id") or "").strip().upper(): capture
            for capture in captures
            if isinstance(capture, Mapping)
        }
        for station in stations:
            capture = capture_by_station.get(station)
            if not isinstance(capture, Mapping):
                raise RuntimeError(
                    "official_native_forward_target_archive_scope_missing:"
                    + station
                )
            intervals = capture.get("bucket_intervals")
            settlement_timezone = str(
                capture.get("settlement_timezone") or ""
            )
            if (
                not isinstance(intervals, Mapping)
                or not intervals
                or not settlement_timezone
            ):
                raise RuntimeError(
                    "official_native_forward_target_archive_contract_invalid:"
                    + station
                )
            frozen = {
                str(bucket): [
                    None if bounds[0] is None else float(bounds[0]),
                    None if bounds[1] is None else float(bounds[1]),
                ]
                for bucket, bounds in intervals.items()
            }
            bridge = {
                "schema": "official_native_forward_offline_contract_bridge_v1",
                "source_archive_content_sha256": target_sha,
                "source_station_capture_content_sha256": str(
                    capture.get("content_sha256") or ""
                ),
                "station_id": station,
                "event_date": base_event_date,
                "settlement_timezone": settlement_timezone,
                "bucket_intervals": frozen,
                "historical_market_arrival_claimed": False,
            }
            target_contracts[station] = {
                "schema": (
                    "official_native_forward_target_bucket_contract_v1"
                ),
                "station_id": station,
                "event_date": base_event_date,
                "settlement_timezone": settlement_timezone,
                "bucket_intervals": frozen,
                "bucket_contract_sha256": content_sha256(frozen),
                "authority_content_sha256": content_sha256(bridge),
                "authority_source_class": (
                    "SOURCE_ARCHIVE_OFFLINE_CONTRACT_BRIDGE"
                ),
            }
    regional_archives_by_station: dict[str, list] = {}
    if args.regional_point_archive is not None:
        regional_payload = load_bounded_json(
            args.regional_point_archive.expanduser().resolve(strict=True)
        )
        if (
            not isinstance(regional_payload, dict)
            or regional_payload.get("schema")
            != "official_regional_point_forward_archives_v1"
            or not isinstance(
                regional_payload.get("archives_by_station"), dict
            )
        ):
            raise RuntimeError("regional_point_archive_payload_invalid")
        regional_archives_by_station = {
            str(station): list(archives)
            for station, archives in regional_payload[
                "archives_by_station"
            ].items()
        }

    # Charter 192: per-station follow runs.  A follow archive replaces its
    # family's archive for the stations assigned to it and nowhere else, so a
    # build given none is exactly the build it was.
    follow_by_path: dict[Path, Mapping[str, object]] = {}
    for path in args.follow_archive:
        resolved = path.expanduser().resolve(strict=True)
        raw = load_bounded_json(resolved)
        if not isinstance(raw, Mapping):
            raise RuntimeError("official_native_forward_follow_archive_invalid")
        schema = str(raw.get("schema") or "")
        if schema not in {
            ECCC_GEPS_MULTISTATION_SCHEMA,
            DWD_ICON_EPS_MULTISTATION_SCHEMA,
            ECMWF_AIFS_ENS_MULTISTATION_SCHEMA,
            NOAA_AIGEFS_MULTISTATION_SCHEMA,
        } or str(raw.get("independence_family_id") or "") not in (
            _FOLLOW_FAMILIES_BY_SCHEMA[schema]
        ) or str(raw.get("event_date") or "") != base_event_date:
            raise RuntimeError("official_native_forward_follow_archive_invalid")
        follow_by_path[resolved] = dict(raw)
    follow_by_station: dict[str, dict[str, Mapping[str, object]]] = {}
    for assignment in args.follow_assignment:
        station_text, _sep, path_text = str(assignment).partition("=")
        station_key = station_text.strip().upper()
        archive = follow_by_path.get(
            Path(path_text).expanduser().resolve()
        )
        if (
            not station_key
            or archive is None
            or station_key not in _archive_station_ids(archive)
        ):
            raise RuntimeError(
                "official_native_forward_follow_assignment_invalid:"
                + str(assignment)
            )
        schema = str(archive.get("schema") or "")
        if schema in follow_by_station.get(station_key, {}):
            raise RuntimeError(
                "official_native_forward_follow_assignment_duplicate:"
                + str(assignment)
            )
        follow_by_station.setdefault(station_key, {})[schema] = archive

    def _station_archives(station: str) -> list:
        archives = list(base_archives)
        follows = follow_by_station.get(station, {})
        if ECCC_GEPS_MULTISTATION_SCHEMA in follows:
            archives.append(follows[ECCC_GEPS_MULTISTATION_SCHEMA])
        elif eccc_archive is not None and station in (
            requested_eccc_stations
            if requested_eccc_stations
            else _archive_station_ids(eccc_archive)
        ):
            archives.append(eccc_archive)
        if DWD_ICON_EPS_MULTISTATION_SCHEMA in follows:
            archives.append(follows[DWD_ICON_EPS_MULTISTATION_SCHEMA])
        elif icon_archive is not None and station in _archive_station_ids(
            icon_archive
        ):
            archives.append(icon_archive)
        # Charter 204: added, never replacing -- there is no AIFS base.
        if ECMWF_AIFS_ENS_MULTISTATION_SCHEMA in follows:
            archives.append(follows[ECMWF_AIFS_ENS_MULTISTATION_SCHEMA])
        if NOAA_AIGEFS_MULTISTATION_SCHEMA in follows:
            archives.append(follows[NOAA_AIGEFS_MULTISTATION_SCHEMA])
        archives.extend(regional_archives_by_station.get(station, []))
        return archives

    packets = [
        build_official_native_forward_packet(
            archives=_station_archives(station),
            station_id=station,
            target_bucket_contract=target_contracts.get(station),
        )
        for station in stations
    ]
    identity = {
        "schema": "native_ensemble_forward_inbox_v1",
        "packets": packets,
    }
    inbox = {
        **identity,
        "content_sha256": content_sha256(identity),
    }
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, inbox)
    print(json.dumps({
        "output": str(output),
        "wrote": wrote,
        "packet_count": len(packets),
        "event_date": packets[0]["event_date"],
        "lead_bucket": packets[0]["lead_bucket"],
        "run_cycle": packets[0]["run_cycle"],
        "ensemble_system_ids": sorted({
            row["ensemble_system_id"]
            for packet in packets
            for row in packet["systems"]
        }),
        "independence_family_ids": sorted({
            row["independence_family_id"]
            for packet in packets
            for row in packet["systems"]
        }),
        "independence_family_count": len({
            row["independence_family_id"]
            for packet in packets
            for row in packet["systems"]
        }),
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
