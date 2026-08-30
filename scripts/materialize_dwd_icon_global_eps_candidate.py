#!/usr/bin/env python3
"""Materialize a traceable, non-promotable DWD ICON Global/EPS candidate.

The input is the locally retained Open-Meteo archive and forward member bucket
receipts already in the SSOT.  This is deliberately a read-only materializer:
it does not refetch any provider (especially not ECMWF/GEFS), write SQLite, or
construct a native-arrival claim.  The result is a content-addressed candidate
manifest for calibration planning only.
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
from bayes_basket_target.fleet import expected_station_count


ROOT = Path(__file__).resolve().parents[1]
SYSTEM_IDS = ("DWD_ICON_EPS_SEAMLESS", "DWD_ICON_GLOBAL_EPS")
MAX_COHORTS_PER_LANE = 2_048
MAX_REPORT_BYTES = 4 * 1024 * 1024
SCHEMA = "dwd_icon_global_eps_candidate_materialization_v1"
OFFICIAL_READINESS_SCHEMA = "dwd_icon_global_eps_official_readiness_v1"
OFFICIAL_GLOBAL_EPS = "DWD_ICON_GLOBAL_EPS"
OFFICIAL_EPS_MEMBER_COUNT = 40
OFFICIAL_EPS_STATION_COUNT = expected_station_count()
OFFICIAL_EPS_STATION_MEMBER_RECORD_COUNT = (
    OFFICIAL_EPS_MEMBER_COUNT * OFFICIAL_EPS_STATION_COUNT
)
DEFAULT_OFFICIAL_READINESS = (
    ROOT / "output/audits/dwd_icon_global_eps_official_readiness_20260801.json"
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _load_json(path: Path, maximum: int = 4 * 1024 * 1024) -> tuple[dict[str, Any], str]:
    content = path.read_bytes()
    if len(content) > maximum:
        raise RuntimeError("candidate_config_input_exceeds_bound")
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise RuntimeError("candidate_config_json_object_required")
    return parsed, hashlib.sha256(content).hexdigest()


def _catalog(path: Path) -> tuple[dict[str, Any], str]:
    source = path.read_bytes()
    if len(source) > 4 * 1024 * 1024:
        raise RuntimeError("candidate_catalog_input_exceeds_bound")
    module = ast.parse(source.decode("utf-8"), filename=str(path))
    for statement in module.body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "ENSEMBLE_SYSTEMS"
            and statement.value is not None
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, dict):
                return value, hashlib.sha256(source).hexdigest()
    raise RuntimeError("candidate_ensemble_catalog_missing")


def _readonly(db: Path) -> sqlite3.Connection:
    if not db.is_file() or db.is_symlink():
        raise RuntimeError("candidate_ssot_db_invalid")
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _candidate_table(
    conn: sqlite3.Connection,
    *,
    republisher_table: str,
    legacy_table: str,
) -> str:
    """Use the isolated lane; read immutable legacy candidates only as fallback."""

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (republisher_table,),
    ).fetchone()
    return republisher_table if exists is not None else legacy_table


def _archive_cohorts(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    table = _candidate_table(
        conn,
        republisher_table=(
            "calibration_republisher_ensemble_historical_member_bucket"
        ),
        legacy_table="calibration_native_ensemble_historical_member_bucket",
    )
    rows = conn.execute(
        "SELECT ensemble_system_id, station_id, target_local_date, archive_lead_bucket, "
        "archive_query_at_utc, member_set_sha256, member_count, bucket_contract_sha256, "
        "historical_member_bucket_id, content_sha256 "
        f"FROM {table} "
        "WHERE ensemble_system_id IN (?, ?) "
        "AND source_role='aggregator_republisher' "
        "AND authority_scope='CALIBRATION_TRAIN_ONLY' AND holdout_eligible=0 "
        "ORDER BY ensemble_system_id, target_local_date, station_id, archive_query_at_utc",
        SYSTEM_IDS,
    ).fetchall()
    if len(rows) > MAX_COHORTS_PER_LANE * len(SYSTEM_IDS):
        raise RuntimeError("candidate_archive_cohort_bound_exceeded")
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        output[str(row[0])].append({
            "station_id": str(row[1]),
            "target_local_date": str(row[2]),
            "archive_lead_bucket": str(row[3]),
            "archive_query_at_utc": str(row[4]),
            "member_set_sha256": str(row[5]),
            "member_count": int(row[6]),
            "bucket_contract_sha256": str(row[7]),
            "materialized_bucket_id": str(row[8]),
            "content_sha256": str(row[9]),
        })
    return output


def _forward_cohorts(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    # A member-set hash, rather than receipt time, is the semantic identity.
    # Retain each materialized set; duplicate member runs from a later polling
    # receipt do not get promoted into a pseudo-native update.
    table = _candidate_table(
        conn,
        republisher_table="calibration_republisher_ensemble_member_bucket",
        legacy_table="calibration_native_ensemble_member_bucket",
    )
    bucket_id_column = (
        "republisher_member_bucket_id"
        if table == "calibration_republisher_ensemble_member_bucket"
        else "member_bucket_id"
    )
    rows = conn.execute(
        "SELECT ensemble_system_id, station_id, target_local_date, aggregator_snapshot_at_utc, "
        "member_set_sha256, member_count, bucket_contract_sha256, "
        + bucket_id_column
        + ", content_sha256 "
        f"FROM {table} "
        "WHERE ensemble_system_id IN (?, ?) "
        "AND source_role='aggregator_republisher' "
        "AND authority_scope='PRODUCTION_NO_LIVE_CANDIDATE' "
        "ORDER BY ensemble_system_id, target_local_date, station_id, aggregator_snapshot_at_utc",
        SYSTEM_IDS,
    ).fetchall()
    if len(rows) > MAX_COHORTS_PER_LANE * len(SYSTEM_IDS):
        raise RuntimeError("candidate_forward_cohort_bound_exceeded")
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        output[str(row[0])].append({
            "station_id": str(row[1]),
            "target_local_date": str(row[2]),
            "aggregator_snapshot_at_utc": str(row[3]),
            "member_set_sha256": str(row[4]),
            "member_count": int(row[5]),
            "bucket_contract_sha256": str(row[6]),
            "materialized_bucket_id": str(row[7]),
            "content_sha256": str(row[8]),
        })
    return output


def _native_and_oos_status(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        "WITH native_arrival AS ("
        " SELECT ensemble_system_id, COUNT(*) n FROM calibration_native_ensemble_member_run "
        " WHERE ensemble_system_id IN (?, ?) AND native_timestamps_complete=1 "
        " AND run_init_utc IS NOT NULL AND published_at_utc IS NOT NULL "
        " AND transport_provider_id<>'OPEN_METEO' GROUP BY ensemble_system_id"
        "), strict_oos AS ("
        " SELECT je.value ensemble_system_id, "
        " SUM(CASE WHEN e.oos_state='PRODUCTION_NO_LIVE_OOS_PASS' THEN 1 ELSE 0 END) pass_n, "
        " SUM(CASE WHEN e.oos_state='OOS_REJECTED' THEN 1 ELSE 0 END) rejected_n "
        " FROM calibration_cross_native_ensemble_oos_evaluation e "
        " JOIN json_each(e.ensemble_system_ids_json) je "
        " WHERE je.value IN (?, ?) GROUP BY je.value"
        "), activation AS ("
        " SELECT je.value ensemble_system_id, COUNT(*) n FROM calibration_native_ensemble_activation a "
        " JOIN calibration_cross_native_ensemble_artifact x ON x.cross_ensemble_artifact_id=a.artifact_id "
        " JOIN json_each(x.ensemble_system_ids_json) je "
        " WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' AND je.value IN (?, ?) "
        " GROUP BY je.value"
        "), system_artifact AS ("
        " SELECT ensemble_system_id, COUNT(*) n FROM calibration_native_ensemble_system_artifact "
        " WHERE ensemble_system_id IN (?, ?) GROUP BY ensemble_system_id"
        ") SELECT ids.system_id, COALESCE(n.n,0), COALESCE(o.pass_n,0), "
        " COALESCE(o.rejected_n,0), COALESCE(a.n,0), COALESCE(s.n,0) FROM ("
        " SELECT ? system_id UNION SELECT ?"
        ") ids LEFT JOIN native_arrival n ON n.ensemble_system_id=ids.system_id "
        " LEFT JOIN strict_oos o ON o.ensemble_system_id=ids.system_id "
        " LEFT JOIN activation a ON a.ensemble_system_id=ids.system_id "
        " LEFT JOIN system_artifact s ON s.ensemble_system_id=ids.system_id",
        SYSTEM_IDS + SYSTEM_IDS + SYSTEM_IDS + SYSTEM_IDS + SYSTEM_IDS,
    ).fetchall()
    return {
        str(row[0]): {
            "official_native_arrival_member_runs": int(row[1]),
            "strict_oos_pass": int(row[2]),
            "strict_oos_rejected": int(row[3]),
            "activation_reference_count": int(row[4]),
            "system_calibration_artifact_count": int(row[5]),
        }
        for row in rows
    }


def _official_native_readiness(path: Path | None) -> dict[str, dict[str, Any]]:
    """Read a verified native-EPS observation without reclassifying any input.

    The candidate manifest is exclusively an Open-Meteo republisher artifact.
    A DWD sidecar may therefore only correct its *status wording*: it proves
    that an independently retained official 40-member cohort exists.  It
    cannot make the republisher native, create an arrival-index row, or change
    formal P0 eligibility.
    """

    absent = {
        system_id: {
            "state": "OFFICIAL_NATIVE_EVIDENCE_UNVERIFIED",
            "official_native_cohort_observed": False,
            "native_arrival_index_eligible": False,
            "formal_p0_eligible": False,
            "audit_content_sha256": None,
            "ready_cohort_count": 0,
        }
        for system_id in SYSTEM_IDS
    }
    if path is None:
        return absent
    supplied = path.expanduser()
    if supplied.is_symlink() or not supplied.is_file():
        raise RuntimeError("candidate_official_readiness_path_invalid")
    document, _ = _load_json(supplied, maximum=MAX_REPORT_BYTES)
    identity = {key: value for key, value in document.items() if key != "content_sha256"}
    if (
        document.get("schema") != OFFICIAL_READINESS_SCHEMA
        or document.get("content_sha256") != _sha(identity)
        or document.get("state")
        not in {
            "READY_FOR_NATIVE_ADAPTER_EXECUTION",
            "STATION_MEMBER_COHORT_READY_STRICT_OOS_PENDING",
        }
    ):
        raise RuntimeError("candidate_official_readiness_invalid")
    products = document.get("official_eps_products")
    if not isinstance(products, dict):
        raise RuntimeError("candidate_official_readiness_products_invalid")
    global_eps = products.get(OFFICIAL_GLOBAL_EPS)
    if not isinstance(global_eps, dict):
        raise RuntimeError("candidate_official_readiness_global_eps_missing")
    cohorts = global_eps.get("cohorts")
    if (
        global_eps.get("source_role") != "official_native"
        or global_eps.get("native_adapter_execution_ready") is not True
        or not isinstance(cohorts, list)
    ):
        raise RuntimeError("candidate_official_readiness_global_eps_invalid")
    ready = [row for row in cohorts if isinstance(row, dict) and row.get("ready") is True]
    cohort = next(
        (
            row
            for row in ready
            if row.get("station_member_cohort_ready") is True
            and isinstance(row.get("station_member_cohort"), dict)
        ),
        None,
    )
    if cohort is None:
        raise RuntimeError("candidate_official_readiness_station_cohort_missing")
    station_members = cohort["station_member_cohort"]
    assert isinstance(station_members, dict)
    if (
        int(cohort.get("official_raw_object_count") or 0) != 1
        or int(cohort.get("unique_member_count") or 0) != OFFICIAL_EPS_MEMBER_COUNT
        or station_members.get("content_sha256_valid") is not True
        or station_members.get("record_shape_valid") is not True
        or station_members.get("station_member_pairs_unique") is not True
        or station_members.get("per_station_member_set_complete") is not True
        or int(station_members.get("actual_station_count") or 0)
        != OFFICIAL_EPS_STATION_COUNT
        or int(station_members.get("actual_member_record_count") or 0)
        != OFFICIAL_EPS_STATION_MEMBER_RECORD_COUNT
        or station_members.get("native_arrival_index_eligible") is not False
        or station_members.get("formal_p0_eligible") is not False
    ):
        raise RuntimeError("candidate_official_readiness_station_cohort_invalid")
    absent[OFFICIAL_GLOBAL_EPS] = {
        "state": "OFFICIAL_NATIVE_40_MEMBER_45_STATION_COHORT_OBSERVED_STRICT_OOS_PENDING",
        "official_native_cohort_observed": True,
        "native_arrival_index_eligible": False,
        "formal_p0_eligible": False,
        "audit_content_sha256": document["content_sha256"],
        "ready_cohort_count": len(ready),
        "run_initialization_utc": cohort.get("run_initialization_utc"),
        "valid_time_utc": cohort.get("valid_time_utc"),
        "official_raw_object_count": 1,
        "member_count": OFFICIAL_EPS_MEMBER_COUNT,
        "station_count": OFFICIAL_EPS_STATION_COUNT,
        "station_member_record_count": OFFICIAL_EPS_STATION_MEMBER_RECORD_COUNT,
    }
    return absent


def _summarize(cohorts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cohort_count": len(cohorts),
        "station_count": len({item["station_id"] for item in cohorts}),
        "target_dates": sorted({item["target_local_date"] for item in cohorts}),
        "member_count_values": sorted({item["member_count"] for item in cohorts}),
        "content_set_sha256": _sha([
            item["content_sha256"] for item in cohorts
        ]),
    }


def build_manifest(
    config_path: Path,
    catalog_path: Path,
    db_path: Path,
    *,
    official_readiness_path: Path | None = None,
) -> dict[str, Any]:
    config, config_sha = _load_json(config_path)
    catalog, catalog_sha = _catalog(catalog_path)
    global_icon = (config.get("global_aggregator_capture_models") or {}).get("ICON")
    configured_eps = config.get("global_ensemble_aggregator_capture_models")
    station_pool = config.get("station_regional_pool")
    if (
        not isinstance(global_icon, dict)
        or not isinstance(configured_eps, dict)
        or set(configured_eps) != set(SYSTEM_IDS)
        or not isinstance(station_pool, dict)
    ):
        raise RuntimeError("candidate_global_icon_inventory_missing")
    for system_id in SYSTEM_IDS:
        system = catalog.get(system_id)
        if not isinstance(system, dict) or system.get("model_scope") != "global":
            raise RuntimeError(f"candidate_global_icon_catalog_scope_invalid:{system_id}")
        configured = configured_eps.get(system_id)
        if (
            not isinstance(configured, dict)
            or configured.get("system_id") != system_id
            or configured.get("transport_provider_id") != "OPEN_METEO"
            or configured.get("source_class") != "aggregator_republisher"
            or configured.get("geographic_scope") != "global"
            or configured.get("independence_family_id") != "DWD_ICON_EPS"
            or int(configured.get("expected_member_count") or 0) != 40
            or configured.get("formal_p0_eligible") is not False
            or configured.get("native_arrival_index_eligible") is not False
        ):
            raise RuntimeError(
                f"candidate_global_icon_explicit_config_invalid:{system_id}"
            )
    with _readonly(db_path) as conn:
        archive = _archive_cohorts(conn)
        forward = _forward_cohorts(conn)
        status = _native_and_oos_status(conn)
    official_native = _official_native_readiness(official_readiness_path)

    models = []
    for system_id in SYSTEM_IDS:
        item = catalog[system_id]
        archive_items = archive.get(system_id, [])
        forward_items = forward.get(system_id, [])
        if any(row["member_count"] != int(item["expected_member_count"]) for row in archive_items + forward_items):
            raise RuntimeError(f"candidate_member_completeness_failed:{system_id}")
        native = status.get(system_id, {})
        native_sidecar = official_native[system_id]
        fail_closed_reasons = [
            "own_strict_oos_missing",
            "aggregator_republisher_cannot_enter_native_arrival_or_hard_no",
        ]
        if native_sidecar["official_native_cohort_observed"]:
            fail_closed_reasons.insert(
                0,
                "official_native_cohort_is_separate_from_republisher_and_not_arrival_indexed",
            )
        else:
            fail_closed_reasons[:0] = [
                "official_native_arrival_missing",
                "native_publication_and_run_init_missing",
            ]
        models.append({
            "ensemble_system_id": system_id,
            "canonical_name": item["canonical_name"],
            "geographic_scope": "global",
            "upstream_provider_id": "DWD",
            "transport_provider_id": "OPEN_METEO",
            "source_class": "aggregator_republisher",
            "formal_p0_eligible": False,
            "native_arrival_index_eligible": False,
            "hard_no_eligible": False,
            "candidate_state": "AGGREGATOR_REPUBLISHER_MATERIALIZED",
            "formal_p0_state": "FAIL_CLOSED",
            "fail_closed_reasons": fail_closed_reasons,
            "archive_training_candidate": {
                "source_role": "aggregator_republisher",
                "authority_scope": "CALIBRATION_TRAIN_ONLY",
                "holdout_eligible": False,
                "summary": _summarize(archive_items),
                "cohorts": archive_items,
            },
            "forward_candidate": {
                "source_role": "aggregator_republisher",
                "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
                "summary": _summarize(forward_items),
                "cohorts": forward_items,
            },
            "native_oos_activation_status": native,
            "official_native_evidence": native_sidecar,
            "activation_interpretation": (
                "activation references are non-qualifying until this exact system "
                "has official native arrivals and its own strict OOS pass"
            ),
        })
    # Keep the content address independent of wall-clock time.  A repeated
    # read-only materialization over the same local archive must produce the
    # same candidate identity, otherwise downstream OOS joins would see a
    # false new artifact on every audit run.
    identity = {
        "schema": SCHEMA,
        "readiness": "candidate_only",
        "fail_closed": True,
        "model_inventory": {
            "configured_global_model_key": "ICON",
            "configured_model_parameter": global_icon.get("model_parameter"),
            "configured_source_class": global_icon.get("source_class"),
            "configured_station_count": len(station_pool),
            "dwd_icon_global_eps_scope": "global",
            "dwd_icon_eps_seamless_scope": "global",
            "icon_eu_and_icon_d2_scope": "regional_and_excluded_from_this_global_candidate",
        },
        "entrypoint_trace": {
            "open_meteo_forward_capture": "bayes_basket_target.open_meteo_ensemble.capture_scope",
            "candidate_ingress": "bayes_basket_target.native_ensemble_store.ingest_forward_packet",
            "official_native_decoder_separate_lane": "bayes_basket_target.native_model_decode",
            "network_requests": 0,
            "sqlite_writes": 0,
        },
        "input_evidence": {
            "config_sha256": config_sha,
            "ensemble_catalog_sha256": catalog_sha,
            "sqlite_path": str(db_path),
            "sqlite_stat": {"size_bytes": db_path.stat().st_size, "mtime_ns": db_path.stat().st_mtime_ns},
            "official_native_readiness": {
                "path": str(official_readiness_path.expanduser().resolve())
                if official_readiness_path is not None
                else None,
                "DWD_ICON_GLOBAL_EPS": official_native[OFFICIAL_GLOBAL_EPS],
                "DWD_ICON_EPS_SEAMLESS": official_native["DWD_ICON_EPS_SEAMLESS"],
            },
        },
        "models": models,
    }
    return {
        **identity,
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "content_sha256": _sha(identity),
    }


def _write(path: Path, manifest: dict[str, Any]) -> None:
    content = _canonical(manifest) + b"\n"
    if len(content) > MAX_REPORT_BYTES:
        raise RuntimeError("candidate_manifest_output_exceeds_bound")
    # Preserve the prior timestamp when the semantic content address is
    # unchanged.  This makes the materializer genuinely idempotent while
    # retaining a useful generation time for changed inputs.
    if path.is_file() and not path.is_symlink():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and previous.get("content_sha256") == manifest.get("content_sha256"):
                return
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/bayes_basket_target/station_regional_model_pool_v1.json")
    parser.add_argument("--catalog", type=Path, default=ROOT / "bayes_basket_target/open_meteo_ensemble.py")
    parser.add_argument("--db", type=Path, default=ROOT / "data/bayes_basket_target_ssot.db")
    parser.add_argument(
        "--official-readiness",
        type=Path,
        default=DEFAULT_OFFICIAL_READINESS,
        help="verified DWD native readiness sidecar; never an Open-Meteo lineage input",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "output/audits/dwd_icon_global_eps_candidate_closure_20260801.json")
    args = parser.parse_args()
    manifest = build_manifest(
        args.config.resolve(),
        args.catalog.resolve(),
        args.db.resolve(),
        official_readiness_path=args.official_readiness,
    )
    _write(args.output.resolve(), manifest)
    print(json.dumps({
        "output": str(args.output.resolve()),
        "readiness": manifest["readiness"],
        "content_sha256": manifest["content_sha256"],
        "models": {
            model["ensemble_system_id"]: {
                "archive_cohorts": model["archive_training_candidate"]["summary"]["cohort_count"],
                "forward_cohorts": model["forward_candidate"]["summary"]["cohort_count"],
            }
            for model in manifest["models"]
        },
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
