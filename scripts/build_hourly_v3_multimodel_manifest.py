#!/usr/bin/env python3
"""Join native and republisher hourly-v3 model evidence without family inflation."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)

SCHEMA = "bayes_basket_target_hourly_v3_multimodel_manifest_v1"
NATIVE_SCHEMA = "native_ensemble_forward_inbox_v1"
REPUBLISHER_SCHEMA = "bayes_basket_target_historical_aggregator_rebuild_evidence_v1"
FAMILY_BY_REPUBLISHER_MODEL = {
    "AIFS": "ECMWF",
    "ICON": "DWD_ICON",
    "ICON-EU": "DWD_ICON",
    "ICON-D2": "DWD_ICON",
}
SCOPE_BY_REPUBLISHER_MODEL = {
    "AIFS": "global",
    "ICON": "global",
    "ICON-EU": "regional",
    "ICON-D2": "regional",
}


def _verified_hashed(raw: object, *, schema: str, hash_key: str) -> dict[str, object]:
    if not isinstance(raw, Mapping) or raw.get("schema") != schema:
        raise RuntimeError("hourly_v3_manifest_source_schema_invalid")
    value = dict(raw)
    supplied = str(value.pop(hash_key, ""))
    if len(supplied) != 64 or supplied != content_sha256(value):
        raise RuntimeError("hourly_v3_manifest_source_hash_invalid")
    value[hash_key] = supplied
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-inbox", type=Path, action="append", default=[])
    parser.add_argument("--republisher-evidence", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.native_inbox or not args.republisher_evidence:
        raise RuntimeError("hourly_v3_manifest_sources_missing")

    source_artifacts: list[dict[str, object]] = []
    station_days: dict[tuple[str, str], dict[str, object]] = {}
    for path in args.native_inbox:
        resolved = path.expanduser().resolve(strict=True)
        raw = _verified_hashed(
            load_bounded_json(resolved),
            schema=NATIVE_SCHEMA,
            hash_key="content_sha256",
        )
        source_artifacts.append({
            "path": str(resolved),
            "content_sha256": raw["content_sha256"],
            "source_class": "official_native_archive_query",
        })
        packets = raw.get("packets")
        if not isinstance(packets, list) or len(packets) != 45:
            raise RuntimeError("hourly_v3_manifest_native_scope_invalid")
        for packet in packets:
            if not isinstance(packet, Mapping):
                raise RuntimeError("hourly_v3_manifest_native_packet_invalid")
            station = str(packet.get("station_id") or "").upper()
            event_date = str(packet.get("event_date") or "")
            key = (station, event_date)
            if key in station_days:
                raise RuntimeError("hourly_v3_manifest_native_scope_duplicate")
            systems = packet.get("systems")
            if not isinstance(systems, list) or len(systems) < 2:
                raise RuntimeError("hourly_v3_manifest_native_systems_invalid")
            native_rows = []
            for system in systems:
                curve = system.get("family_temperature_curve") if isinstance(system, Mapping) else None
                if not isinstance(curve, Mapping) or len(curve.get("points") or []) < 4:
                    raise RuntimeError("hourly_v3_manifest_native_curve_invalid")
                native_rows.append({
                    "model_product_id": str(system.get("ensemble_system_id") or ""),
                    "independence_family_id": str(system.get("independence_family_id") or ""),
                    "source_class": "official_native_archive_query",
                    "run_init_utc": str(system.get("run_init_utc") or ""),
                    "curve_content_sha256": str(curve.get("content_sha256") or ""),
                    "temperature_point_count": len(curve["points"]),
                })
            station_days[key] = {
                "station_id": station,
                "event_date": event_date,
                "settlement_timezone": str(packet.get("settlement_timezone") or ""),
                "bucket_contract_authority": packet.get("bucket_contract_authority"),
                "native_models": sorted(native_rows, key=lambda row: row["model_product_id"]),
                "republisher_models": [],
            }

    for path in args.republisher_evidence:
        resolved = path.expanduser().resolve(strict=True)
        raw = _verified_hashed(
            load_bounded_json(resolved),
            schema=REPUBLISHER_SCHEMA,
            hash_key="receipt_sha256",
        )
        checks = raw.get("checks")
        if (
            not isinstance(checks, Mapping)
            or checks.get("all_curves_complete") is not True
            or checks.get("native_arrival_index_eligible") is not False
            or raw.get("blockers") != []
        ):
            raise RuntimeError("hourly_v3_manifest_republisher_incomplete")
        model_id = str(raw.get("model_id") or "").upper()
        family = FAMILY_BY_REPUBLISHER_MODEL.get(model_id)
        scope = SCOPE_BY_REPUBLISHER_MODEL.get(model_id)
        if family is None or scope is None:
            raise RuntimeError("hourly_v3_manifest_republisher_model_unsupported")
        source_artifacts.append({
            "path": str(resolved),
            "content_sha256": raw["receipt_sha256"],
            "source_class": "aggregator_republisher",
            "model_id": model_id,
        })
        curves = raw.get("curves")
        rows = curves.get("eligible_model_rows") if isinstance(curves, Mapping) else None
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("hourly_v3_manifest_republisher_curves_missing")
        for row in rows:
            if not isinstance(row, Mapping) or row.get("complete") is not True:
                raise RuntimeError("hourly_v3_manifest_republisher_curve_invalid")
            key = (
                str(row.get("station_id") or "").upper(),
                str(row.get("event_date") or ""),
            )
            target = station_days.get(key)
            if target is None:
                raise RuntimeError("hourly_v3_manifest_republisher_scope_orphan")
            target["republisher_models"].append({
                "model_product_id": model_id,
                "provider_id": str(row.get("provider_id") or ""),
                "independence_family_id": family,
                "geographic_scope": scope,
                "source_class": "aggregator_republisher",
                "run_init_utc": str(row.get("model_run_initialization_utc") or ""),
                "temperature_point_count": int(row.get("temperature_point_count") or 0),
                "forecast_daily_max_c": float(row.get("forecast_daily_max_c")),
                "raw_message_sha256": str(row.get("raw_message_sha256") or ""),
            })

    output_rows = []
    for key in sorted(station_days):
        row = station_days[key]
        republisher = sorted(
            row["republisher_models"],
            key=lambda item: item["model_product_id"],
        )
        global_models = [item for item in republisher if item["geographic_scope"] == "global"]
        if {item["model_product_id"] for item in global_models} != {"AIFS", "ICON"}:
            raise RuntimeError("hourly_v3_manifest_global_republisher_scope_incomplete")
        families = sorted({
            item["independence_family_id"]
            for item in [*row["native_models"], *republisher]
        })
        products = sorted({
            item["model_product_id"]
            for item in [*row["native_models"], *republisher]
        })
        output_rows.append({
            **{key: value for key, value in row.items() if key != "republisher_models"},
            "republisher_models": republisher,
            "model_product_ids": products,
            "model_product_count": len(products),
            "independence_family_ids": families,
            "independence_family_count": len(families),
            "family_vote_deduplicated": True,
        })

    identity = {
        "schema": SCHEMA,
        "state": "READY_CANDIDATE_ONLY",
        "station_day_count": len(output_rows),
        "station_count": len({row["station_id"] for row in output_rows}),
        "event_dates": sorted({row["event_date"] for row in output_rows}),
        "source_artifacts": sorted(source_artifacts, key=lambda row: str(row["path"])),
        "station_days": output_rows,
        "boundary": {
            "oos_blocks_data_materialization": False,
            "formal_promotion_allowed": False,
            "live_activation_allowed": False,
            "republisher_enters_native_arrival_index": False,
            "historical_arrival_claimed": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    artifact = {**identity, "content_sha256": content_sha256(identity)}
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, artifact)
    print(json.dumps({
        "state": artifact["state"],
        "station_day_count": len(output_rows),
        "station_count": artifact["station_count"],
        "event_dates": artifact["event_dates"],
        "family_count_min": min(row["independence_family_count"] for row in output_rows),
        "family_count_max": max(row["independence_family_count"] for row in output_rows),
        "model_product_count_min": min(row["model_product_count"] for row in output_rows),
        "model_product_count_max": max(row["model_product_count"] for row in output_rows),
        "output": str(output),
        "wrote": wrote,
        "orders_submitted": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
