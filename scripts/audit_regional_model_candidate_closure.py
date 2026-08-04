#!/usr/bin/env python3
"""Read-only, fail-closed audit for regional/global model candidates.

This report intentionally treats Open-Meteo as an ``aggregator_republisher``.
It never infers an official native arrival from an Open-Meteo member run, even
where the model is registered in the native-ensemble schema.  The script does
not open a write transaction, mutate an activation, or contact a network.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_REPORT_BYTES = 4 * 1024 * 1024
SCHEMA = "regional_model_candidate_closure_audit_v1"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _read_json_bounded(path: Path) -> tuple[dict[str, Any], str]:
    data = path.read_bytes()
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError(f"input_exceeds_bound:{path}")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value, hashlib.sha256(data).hexdigest()


def _file_sha256_bounded(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise ValueError(f"input_exceeds_bound:{path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    payload = _canonical_json(value) + b"\n"
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError("report_exceeds_bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _connection(db_path: Path) -> sqlite3.Connection:
    # mode=ro prevents journal creation and makes this audit safe beside
    # production.  No immutable=1: a live writer remains authoritative.
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _model_definitions(config: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    station_pool = config.get("station_regional_pool")
    if not isinstance(station_pool, dict):
        raise ValueError("station_regional_pool_missing")
    all_stations = sorted(str(item) for item in station_pool)
    systems = catalog.get("ENSEMBLE_SYSTEMS")
    if not isinstance(systems, dict):
        raise ValueError("ensemble_systems_missing")

    definitions: list[dict[str, Any]] = []
    global_capture = config.get("global_aggregator_capture_models", {})
    if not isinstance(global_capture, dict):
        raise ValueError("global_aggregator_capture_models_missing")
    for key, item in sorted(global_capture.items()):
        if not isinstance(item, dict):
            raise ValueError(f"invalid_global_capture:{key}")
        system_id = item.get("system_id")
        # The deterministic ICON entry is a global candidate.  Its available
        # ensemble records are reported separately without pretending they are
        # the deterministic model or native DWD arrivals.
        related = (
            ["DWD_ICON_EPS_SEAMLESS", "DWD_ICON_GLOBAL_EPS"]
            if key == "ICON"
            else ([str(system_id)] if system_id else [])
        )
        definitions.append(
            {
                "model_key": str(key),
                "geographic_scope": "global",
                "source_class": str(item.get("source_class") or "unknown"),
                "configured_model_parameter": str(item.get("model_parameter") or ""),
                "ensemble_system_ids": related,
                "station_ids": all_stations,
                "configured": True,
            }
        )

    regional_capture = config.get("regional_ensemble_aggregator_capture_models", {})
    if not isinstance(regional_capture, dict):
        raise ValueError("regional_ensemble_aggregator_capture_models_missing")
    for key, item in sorted(regional_capture.items()):
        if not isinstance(item, dict):
            raise ValueError(f"invalid_regional_capture:{key}")
        definitions.append(
            {
                "model_key": str(key),
                "geographic_scope": "regional",
                "source_class": str(item.get("source_class") or "unknown"),
                "configured_model_parameter": str(item.get("model_parameter") or ""),
                "ensemble_system_ids": [str(item["system_id"])] if item.get("system_id") else [],
                "station_ids": all_stations,
                "configured": True,
            }
        )

    regional_models = config.get("regional_models", {})
    if not isinstance(regional_models, dict):
        raise ValueError("regional_models_missing")
    system_by_key = {
        "ICON-EU": ["DWD_ICON_EU_EPS"],
        "ICON-D2": ["DWD_ICON_D2_EPS"],
    }
    for key, item in sorted(regional_models.items()):
        if not isinstance(item, dict):
            raise ValueError(f"invalid_regional_model:{key}")
        definitions.append(
            {
                "model_key": str(key),
                "geographic_scope": "regional",
                "source_class": str(item.get("source_class") or "unknown"),
                "configured_model_parameter": str(item.get("model_parameter") or ""),
                "ensemble_system_ids": system_by_key.get(str(key), []),
                "station_ids": sorted(
                    station for station, values in station_pool.items()
                    if isinstance(values, list) and key in values
                ),
                "configured": True,
            }
        )

    # Keep classifications explicit even if a config entry is later removed.
    # This is especially important because all current DWD rows are transported
    # by Open-Meteo and must not be labelled as native DWD arrivals.
    present_systems = {sid for item in definitions for sid in item["ensemble_system_ids"]}
    for system_id, scope in (
        ("DWD_ICON_EPS_SEAMLESS", "global"),
        ("DWD_ICON_GLOBAL_EPS", "global"),
        ("DWD_ICON_EU_EPS", "regional"),
        ("DWD_ICON_D2_EPS", "regional"),
    ):
        if system_id not in systems:
            raise ValueError(f"icon_system_missing_from_catalog:{system_id}")
        if system_id not in present_systems:
            definitions.append(
                {
                    "model_key": system_id,
                    "geographic_scope": scope,
                    "source_class": "aggregator_republisher",
                    "configured_model_parameter": str(systems[system_id].get("api_model") or ""),
                    "ensemble_system_ids": [system_id],
                    "station_ids": all_stations if scope == "global" else [],
                    "configured": False,
                }
            )
    return definitions


def _load_ensemble_catalog(path: Path) -> dict[str, Any]:
    """Extract the literal catalog without importing executable capture code."""
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(path))
    for node in module.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id != "ENSEMBLE_SYSTEMS" or node.value is None:
            continue
        value = ast.literal_eval(node.value)
        if not isinstance(value, dict):
            break
        return {"ENSEMBLE_SYSTEMS": value}
    raise ValueError("ensemble_systems_literal_missing")


def _grouped_counts(conn: sqlite3.Connection, sql: str) -> dict[tuple[str, str], dict[str, int]]:
    result: dict[tuple[str, str], dict[str, int]] = {}
    for row in conn.execute(sql):
        system_id, station_id, *counts = row
        result[(str(system_id), str(station_id))] = {
            "official_native_arrival_member_runs": int(counts[0]),
            "open_meteo_republisher_member_runs": int(counts[1]),
            "open_meteo_republisher_member_buckets": int(counts[2]),
            "strict_oos_pass": int(counts[3]),
            "strict_oos_rejected": int(counts[4]),
            "cross_ensemble_activations": int(counts[5]),
        }
    return result


def _load_counts(conn: sqlite3.Connection) -> tuple[dict[tuple[str, str], dict[str, int]], str]:
    # Each CTE is grouped in SQLite; no raw member payload is copied into the
    # report.  Native means complete native time evidence and non-Open-Meteo
    # transport, never merely a row in a native-named registry table.
    sql = """
    WITH native_member_counts AS (
      SELECT ensemble_system_id, station_id,
        SUM(CASE WHEN native_timestamps_complete=1
                  AND run_init_utc IS NOT NULL AND published_at_utc IS NOT NULL
                  AND transport_provider_id <> 'OPEN_METEO' THEN 1 ELSE 0 END) AS native_runs
      FROM calibration_native_ensemble_member_run
      GROUP BY ensemble_system_id, station_id
    ), republisher_member_counts AS (
      SELECT ensemble_system_id, station_id, COUNT(*) AS om_runs
      FROM calibration_republisher_ensemble_member_run
      WHERE transport_provider_id='OPEN_METEO'
      GROUP BY ensemble_system_id, station_id
    ), bucket_counts AS (
      SELECT ensemble_system_id, station_id, COUNT(*) AS om_buckets
      FROM calibration_republisher_ensemble_member_bucket
      WHERE source_role='aggregator_republisher'
      GROUP BY ensemble_system_id, station_id
    ), oos_counts AS (
      SELECT je.value AS ensemble_system_id, e.station_id,
        SUM(CASE WHEN e.oos_state='PRODUCTION_NO_LIVE_OOS_PASS' THEN 1 ELSE 0 END) AS oos_pass,
        SUM(CASE WHEN e.oos_state='OOS_REJECTED' THEN 1 ELSE 0 END) AS oos_rejected
      FROM calibration_cross_native_ensemble_oos_evaluation e
      JOIN json_each(e.ensemble_system_ids_json) je
      GROUP BY je.value, e.station_id
    ), activation_counts AS (
      SELECT je.value AS ensemble_system_id, a.station_id, COUNT(*) AS activations
      FROM calibration_native_ensemble_activation a
      JOIN calibration_cross_native_ensemble_artifact x
        ON x.cross_ensemble_artifact_id=a.artifact_id
      JOIN json_each(x.ensemble_system_ids_json) je
      WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE'
      GROUP BY je.value, a.station_id
    ), keys AS (
      SELECT ensemble_system_id, station_id FROM native_member_counts
      UNION SELECT ensemble_system_id, station_id FROM republisher_member_counts
      UNION SELECT ensemble_system_id, station_id FROM bucket_counts
      UNION SELECT ensemble_system_id, station_id FROM oos_counts
      UNION SELECT ensemble_system_id, station_id FROM activation_counts
    )
    SELECT k.ensemble_system_id, k.station_id,
      COALESCE(n.native_runs,0), COALESCE(m.om_runs,0), COALESCE(b.om_buckets,0),
      COALESCE(o.oos_pass,0), COALESCE(o.oos_rejected,0), COALESCE(a.activations,0)
    FROM keys k
    LEFT JOIN native_member_counts n USING(ensemble_system_id, station_id)
    LEFT JOIN republisher_member_counts m USING(ensemble_system_id, station_id)
    LEFT JOIN bucket_counts b USING(ensemble_system_id, station_id)
    LEFT JOIN oos_counts o USING(ensemble_system_id, station_id)
    LEFT JOIN activation_counts a USING(ensemble_system_id, station_id)
    """
    counts = _grouped_counts(conn, sql)
    fingerprint = _sha256_json(
        [
            {"system_id": system, "station_id": station, **values}
            for (system, station), values in sorted(counts.items())
        ]
    )
    return counts, fingerprint


def _empty_republisher_temporal_evidence() -> dict[str, Any]:
    """Return the explicit zero state for one republisher system/station.

    A row in the forward Open-Meteo inbox can target a calendar day that has
    since closed.  That does *not* transform it into historical OOS evidence:
    only the append-only historical contract can do that.  Keep the two
    inventories distinct in the audit so callers cannot infer an OOS sample
    from an old-looking forward packet.
    """

    return {
        "historical_republisher_contract_count": 0,
        "historical_strict_oos_contract_count": 0,
        "historical_republisher_member_run_count": 0,
        "historical_strict_oos_member_run_count": 0,
        "historical_station_day_count": 0,
        "historical_strict_oos_station_day_count": 0,
        "historical_event_date_start": None,
        "historical_event_date_end": None,
        "forward_observational_member_run_count": 0,
        "forward_observational_station_day_count": 0,
        "forward_event_date_start": None,
        "forward_event_date_end": None,
        "historical_oos_temporal_scope": "CLOSED_HISTORICAL_AWC_TRUTH",
        "forward_evidence_role": "observational_candidate_not_oos",
    }


def _load_republisher_temporal_evidence(
    conn: sqlite3.Connection,
) -> tuple[dict[tuple[str, str], dict[str, Any]], str]:
    """Read historical-contract and forward-packet evidence separately.

    This audit does not create a historical OOS contract.  It only verifies
    whether one was already retained and exactly bound to an AWC-closed day.
    The forward packet table is deliberately reported under the observational
    role even when its target date is now in the past: its arrival snapshot is
    not an immutable historical strict-as-of contract.
    """

    sql = """
    WITH historical_contracts AS (
      SELECT ensemble_system_id, station_id,
        COUNT(*) AS historical_contract_count,
        SUM(CASE WHEN historical_oos_eligible=1
                  AND station_day_closed=1
                  AND awc_truth_source_lane='AWC'
                  AND receipt_class='HISTORICAL_ASOF_REPUBLISHER_RECEIPT'
                  AND temporal_scope='CLOSED_HISTORICAL_AWC_TRUTH'
                  AND future_event_count=0
                 THEN 1 ELSE 0 END) AS historical_strict_contract_count,
        MIN(target_local_date) AS historical_event_date_start,
        MAX(target_local_date) AS historical_event_date_end
      FROM calibration_republisher_historical_oos_contract
      GROUP BY ensemble_system_id, station_id
    ), historical_members AS (
      SELECT h.ensemble_system_id, h.station_id,
        COUNT(*) AS historical_member_run_count,
        COUNT(DISTINCT h.target_local_date) AS historical_station_day_count,
        SUM(CASE WHEN h.historical_oos_eligible=1
                  AND c.historical_oos_eligible=1
                  AND c.station_day_closed=1
                  AND c.awc_truth_source_lane='AWC'
                  AND c.receipt_class='HISTORICAL_ASOF_REPUBLISHER_RECEIPT'
                  AND c.temporal_scope='CLOSED_HISTORICAL_AWC_TRUTH'
                  AND c.future_event_count=0
                 THEN 1 ELSE 0 END) AS historical_strict_member_run_count,
        COUNT(DISTINCT CASE WHEN h.historical_oos_eligible=1
                              AND c.historical_oos_eligible=1
                              AND c.station_day_closed=1
                              AND c.awc_truth_source_lane='AWC'
                              AND c.receipt_class='HISTORICAL_ASOF_REPUBLISHER_RECEIPT'
                              AND c.temporal_scope='CLOSED_HISTORICAL_AWC_TRUTH'
                              AND c.future_event_count=0
                            THEN h.target_local_date END)
          AS historical_strict_station_day_count,
        MIN(h.target_local_date) AS historical_member_event_date_start,
        MAX(h.target_local_date) AS historical_member_event_date_end
      FROM calibration_republisher_ensemble_historical_member_run AS h
      LEFT JOIN calibration_republisher_historical_oos_contract AS c
        ON c.historical_oos_contract_id=h.historical_oos_contract_id
       AND c.ensemble_system_id=h.ensemble_system_id
       AND c.station_id=h.station_id
       AND c.target_local_date=h.target_local_date
       AND c.archive_lead_bucket=h.archive_lead_bucket
       AND c.run_cycle=h.run_cycle
      WHERE h.transport_provider_id='OPEN_METEO'
      GROUP BY h.ensemble_system_id, h.station_id
    ), forward_packets AS (
      SELECT ensemble_system_id, station_id,
        COUNT(*) AS forward_member_run_count,
        COUNT(DISTINCT target_local_date) AS forward_station_day_count,
        MIN(target_local_date) AS forward_event_date_start,
        MAX(target_local_date) AS forward_event_date_end
      FROM calibration_republisher_ensemble_member_run
      WHERE transport_provider_id='OPEN_METEO'
      GROUP BY ensemble_system_id, station_id
    ), keys AS (
      SELECT ensemble_system_id, station_id FROM historical_contracts
      UNION SELECT ensemble_system_id, station_id FROM historical_members
      UNION SELECT ensemble_system_id, station_id FROM forward_packets
    )
    SELECT k.ensemble_system_id, k.station_id,
      COALESCE(c.historical_contract_count, 0),
      COALESCE(c.historical_strict_contract_count, 0),
      COALESCE(h.historical_member_run_count, 0),
      COALESCE(h.historical_strict_member_run_count, 0),
      COALESCE(h.historical_station_day_count, 0),
      COALESCE(h.historical_strict_station_day_count, 0),
      COALESCE(c.historical_event_date_start, h.historical_member_event_date_start),
      COALESCE(c.historical_event_date_end, h.historical_member_event_date_end),
      COALESCE(f.forward_member_run_count, 0),
      COALESCE(f.forward_station_day_count, 0),
      f.forward_event_date_start,
      f.forward_event_date_end
    FROM keys AS k
    LEFT JOIN historical_contracts AS c
      USING(ensemble_system_id, station_id)
    LEFT JOIN historical_members AS h
      USING(ensemble_system_id, station_id)
    LEFT JOIN forward_packets AS f
      USING(ensemble_system_id, station_id)
    """
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute(sql):
        (
            system_id,
            station_id,
            contract_count,
            strict_contract_count,
            historical_member_count,
            strict_historical_member_count,
            historical_station_day_count,
            strict_historical_station_day_count,
            historical_start,
            historical_end,
            forward_member_count,
            forward_station_day_count,
            forward_start,
            forward_end,
        ) = row
        result[(str(system_id), str(station_id))] = {
            "historical_republisher_contract_count": int(contract_count),
            "historical_strict_oos_contract_count": int(strict_contract_count),
            "historical_republisher_member_run_count": int(historical_member_count),
            "historical_strict_oos_member_run_count": int(strict_historical_member_count),
            "historical_station_day_count": int(historical_station_day_count),
            "historical_strict_oos_station_day_count": int(
                strict_historical_station_day_count
            ),
            "historical_event_date_start": (
                str(historical_start) if historical_start is not None else None
            ),
            "historical_event_date_end": (
                str(historical_end) if historical_end is not None else None
            ),
            "forward_observational_member_run_count": int(forward_member_count),
            "forward_observational_station_day_count": int(
                forward_station_day_count
            ),
            "forward_event_date_start": (
                str(forward_start) if forward_start is not None else None
            ),
            "forward_event_date_end": (
                str(forward_end) if forward_end is not None else None
            ),
            "historical_oos_temporal_scope": "CLOSED_HISTORICAL_AWC_TRUTH",
            "forward_evidence_role": "observational_candidate_not_oos",
        }
    fingerprint = _sha256_json([
        {"system_id": system_id, "station_id": station_id, **value}
        for (system_id, station_id), value in sorted(result.items())
    ])
    return result, fingerprint


def _merge_republisher_temporal_evidence(
    values: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Merge one or more system-level temporal inventories for a station."""

    merged = _empty_republisher_temporal_evidence()
    if not values:
        return merged
    count_fields = (
        "historical_republisher_contract_count",
        "historical_strict_oos_contract_count",
        "historical_republisher_member_run_count",
        "historical_strict_oos_member_run_count",
        "historical_station_day_count",
        "historical_strict_oos_station_day_count",
        "forward_observational_member_run_count",
        "forward_observational_station_day_count",
    )
    for field in count_fields:
        merged[field] = sum(int(value.get(field) or 0) for value in values)
    for prefix in ("historical", "forward"):
        starts = sorted(
            str(value.get(f"{prefix}_event_date_start"))
            for value in values
            if value.get(f"{prefix}_event_date_start")
        )
        ends = sorted(
            str(value.get(f"{prefix}_event_date_end"))
            for value in values
            if value.get(f"{prefix}_event_date_end")
        )
        merged[f"{prefix}_event_date_start"] = starts[0] if starts else None
        merged[f"{prefix}_event_date_end"] = ends[-1] if ends else None
    return merged


def _system_registry(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    native_rows = conn.execute(
        "SELECT ensemble_system_id, canonical_name, provider_id, expected_member_count "
        "FROM calibration_native_ensemble_system"
    )
    registry = {
        str(row[0]): {
            "canonical_name": str(row[1]),
            "provider_id": str(row[2]),
            "expected_member_count": int(row[3]),
        }
        for row in native_rows
    }
    for row in conn.execute(
        "SELECT ensemble_system_id,canonical_name,upstream_provider_id,"
        "expected_member_count FROM calibration_republisher_ensemble_product"
    ):
        registry.setdefault(str(row[0]), {
            "canonical_name": str(row[1]),
            "provider_id": str(row[2]),
            "expected_member_count": int(row[3]),
        })
    return registry


def build_report(config_path: Path, catalog_path: Path, db_path: Path) -> dict[str, Any]:
    config, config_sha = _read_json_bounded(config_path)
    catalog_sha = _file_sha256_bounded(catalog_path)
    definitions = _model_definitions(config, _load_ensemble_catalog(catalog_path))
    with _connection(db_path) as conn:
        counts, db_evidence_sha = _load_counts(conn)
        temporal_evidence, temporal_evidence_sha = (
            _load_republisher_temporal_evidence(conn)
        )
        registry = _system_registry(conn)

    models: list[dict[str, Any]] = []
    for definition in definitions:
        system_ids = definition["ensemble_system_ids"]
        configured_stations = set(definition["station_ids"])
        observed_stations = {
            station_id
            for (system_id, station_id) in counts
            if system_id in system_ids
        }
        stations: list[dict[str, Any]] = []
        for station_id in sorted(configured_stations | observed_stations):
            aggregate = {
                "official_native_arrival_member_runs": 0,
                "open_meteo_republisher_member_runs": 0,
                "open_meteo_republisher_member_buckets": 0,
                "strict_oos_pass": 0,
                "strict_oos_rejected": 0,
                "cross_ensemble_activations": 0,
            }
            for system_id in system_ids:
                for key, value in counts.get((system_id, station_id), {}).items():
                    aggregate[key] += value
            station_temporal = _merge_republisher_temporal_evidence([
                temporal_evidence.get(
                    (system_id, station_id),
                    _empty_republisher_temporal_evidence(),
                )
                for system_id in system_ids
            ])
            stations.append(
                {
                    "station_id": station_id,
                    "configured_station": station_id in configured_stations,
                    **aggregate,
                    "republisher_temporal_evidence": station_temporal,
                }
            )
        totals = {
            key: sum(item[key] for item in stations)
            for key in (
                "official_native_arrival_member_runs", "open_meteo_republisher_member_runs",
                "open_meteo_republisher_member_buckets", "strict_oos_pass",
                "strict_oos_rejected", "cross_ensemble_activations",
            )
        }
        temporal_totals = _merge_republisher_temporal_evidence([
            item["republisher_temporal_evidence"]
            for item in stations
        ])
        readiness_reasons = [
            "official_native_arrival_absent" if totals["official_native_arrival_member_runs"] == 0 else "",
            "strict_oos_absent" if totals["strict_oos_pass"] == 0 else "",
            "cross_ensemble_activation_absent" if totals["cross_ensemble_activations"] == 0 else "",
            "aggregator_republisher_lineage_only" if totals["open_meteo_republisher_member_runs"] else "",
        ]
        models.append(
            {
                **definition,
                "configured_station_count": len(configured_stations),
                "observed_station_count": len(observed_stations),
                "ensemble_system_registry": {
                    system_id: registry.get(system_id, {"registry_state": "not_registered"})
                    for system_id in system_ids
                },
                "totals": totals,
                "republisher_temporal_evidence": temporal_totals,
                "readiness": "candidate_only",
                "fail_closed_reasons": [item for item in readiness_reasons if item],
                "stations": stations,
            }
        )

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "readiness": "candidate_only",
        "fail_closed": True,
        "lineage_policy": {
            "official_native_arrival": "requires complete native timestamps and non-OPEN_METEO transport",
            "open_meteo": "aggregator_republisher; may seed candidate calibration only; never native arrival, P1, or formal/live authority",
            "open_meteo_historical_oos": "requires retained CLOSED_HISTORICAL_AWC_TRUTH contract; unbound historical rows are not OOS",
            "open_meteo_forward": "observational_candidate_not_oos even after its target date passes; it never contributes OOS samples",
            "promotion": "requires own strict OOS and activation; OOS of another system never transfers",
        },
        "icon_scope_classification": {
            "DWD_ICON_EPS_SEAMLESS": "global",
            "DWD_ICON_GLOBAL_EPS": "global",
            "DWD_ICON_EU_EPS": "regional",
            "DWD_ICON_D2_EPS": "regional",
        },
        "input_evidence": {
            "regional_pool_config_sha256": config_sha,
            "open_meteo_ensemble_catalog_sha256": catalog_sha,
            "sqlite_grouped_evidence_sha256": db_evidence_sha,
            "republisher_temporal_evidence_sha256": temporal_evidence_sha,
            "sqlite_path": str(db_path),
            "sqlite_stat": {"size_bytes": db_path.stat().st_size, "mtime_ns": db_path.stat().st_mtime_ns},
            "bounded_read_limit_bytes": MAX_INPUT_BYTES,
        },
        "models": models,
    }
    report["content_sha256"] = _sha256_json(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/bayes_basket_target/station_regional_model_pool_v1.json")
    parser.add_argument("--catalog", type=Path, default=ROOT / "bayes_basket_target/open_meteo_ensemble.py")
    parser.add_argument("--db", type=Path, default=ROOT / "data/bayes_basket_target_ssot.db")
    parser.add_argument("--output", type=Path, default=ROOT / "output/audits/regional_model_candidate_closure_20260801.json")
    args = parser.parse_args()
    report = build_report(args.config.resolve(), args.catalog.resolve(), args.db.resolve())
    _write_json_atomic(args.output.resolve(), report)
    print(json.dumps({"readiness": report["readiness"], "output": str(args.output.resolve()), "content_sha256": report["content_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
