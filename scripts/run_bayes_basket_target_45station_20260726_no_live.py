#!/usr/bin/env python3
"""Bounded, isolated audit runner for the canonical 45-station 2026-07-26 lane.

This runner deliberately has no network or gateway imports.  It evaluates the
retained 2026-07-25 00Z ECMWF/GFS/ICON evidence strictly as of
2026-07-25T00:00:00Z, reads the target SSOT through SQLite ``mode=ro``, and
writes only an isolated audit SQLite database plus one compact receipt.

An object published or received after the decision cutoff is evidence that the
capture exists, but it is never eligible for the historical decision.  Missing
or mismatched upstream evidence therefore propagates as an explicit
fail-closed status instead of producing a synthetic P0/P1, quote, inventory,
basket, intent, fill, or order.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# Keep direct CLI execution hermetic.  Other repository runners are callable
# both as ``python scripts/...`` and as imported modules; this audit runner
# must not require callers to inject PYTHONPATH merely to perform its
# read-only 45-station verification.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.resource_bounds import MAX_TARGET_PROCESS_RSS_BYTES
from bayes_basket_target.resource_bounds import MIB


OUTPUT_DIR = ROOT / "output/audits/bayes_basket_target_45station_20260726_no_live"
TARGET_DB = ROOT / "data/bayes_basket_target_ssot.db"
EVENT_DATE = "2026-07-26"
DECISION_CUTOFF = "2026-07-25T00:00:00Z"
RUN_INITIALIZATION = "2026-07-25T00:00:00Z"
MODEL_IDS = ("ECMWF", "GFS", "ICON")

CONTRACT_PATH = (
    ROOT / "output/audits/bayes_basket_target_model_run_contract_20260725_00z.json"
)
ARRIVAL_PATHS = {
    "ECMWF": ROOT / "output/audits/bayes_basket_target_model_arrival_evidence_ecmwf_20260725_00z.json",
    "GFS": ROOT / "output/audits/bayes_basket_target_model_arrival_evidence_gfs_20260725_00z.json",
    "ICON": ROOT / "output/audits/bayes_basket_target_model_arrival_evidence_icon_20260725_00z_exact5.json",
}
DECODE_PATHS = {
    "ECMWF": ROOT / "output/audits/bayes_basket_target_native_model_arrival_decode_ecmwf_full_retry_20260725_00z.json",
    "GFS": ROOT / "output/audits/bayes_basket_target_native_model_arrival_decode_gfs_20260725_00z.json",
    "ICON": ROOT / "output/audits/bayes_basket_target_native_model_arrival_decode_icon_20260725_00z_exact5.json",
}

MAX_STATIONS = expected_station_count()
MAX_INPUT_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_INPUT_BYTES = 256 * MIB
MAX_OUTPUT_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_RSS_BYTES = MAX_TARGET_PROCESS_RSS_BYTES
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 400_000

SCHEMA = "bayes_basket_target_45station_20260726_no_live_v1"
STAGES = (
    "contract",
    "model_arrival_decode_exact_join",
    "market_registry",
    "p0_p1",
    "official_ws",
    "q0_order_state",
    "basket",
    "no_live_decision_reconcile",
)


class FailClosed(RuntimeError):
    """A bounded audit precondition failed."""


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FailClosed("timezone_required")
    return parsed.astimezone(timezone.utc)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS reports bytes; Linux reports KiB.
    return value if sys.platform == "darwin" else value * 1024


def _assert_resources() -> dict[str, int]:
    free = int(shutil.disk_usage(OUTPUT_DIR.parent).free)
    rss = _rss_bytes()
    if free < MIN_FREE_BYTES:
        raise FailClosed("minimum_free_disk_not_met")
    if rss > MAX_RSS_BYTES:
        raise FailClosed("rss_limit_exceeded")
    return {"free_bytes": free, "rss_bytes": rss}


def _json_shape(value: Any) -> tuple[int, int]:
    nodes = 0
    max_depth = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        max_depth = max(max_depth, depth)
        if nodes > MAX_JSON_NODES or max_depth > MAX_JSON_DEPTH:
            raise FailClosed("json_shape_limit_exceeded")
        if isinstance(item, Mapping):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return nodes, max_depth


def _load_json(path: Path, budget: dict[str, int]) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FailClosed(f"input_missing:{path.name}")
    size = int(path.stat().st_size)
    if size <= 0 or size > MAX_INPUT_FILE_BYTES:
        raise FailClosed(f"input_file_size_invalid:{path.name}")
    budget["input_bytes"] += size
    if budget["input_bytes"] > MAX_TOTAL_INPUT_BYTES:
        raise FailClosed("total_input_limit_exceeded")
    raw = path.read_bytes()
    if len(raw) != size:
        raise FailClosed(f"input_size_changed_while_reading:{path.name}")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FailClosed(f"input_json_invalid:{path.name}") from exc
    if not isinstance(value, dict):
        raise FailClosed(f"input_mapping_required:{path.name}")
    nodes, depth = _json_shape(value)
    budget["json_nodes"] += nodes
    budget["max_json_depth"] = max(budget["max_json_depth"], depth)
    budget["inputs"].append(
        {
            "path": str(path),
            "bytes": size,
            "sha256": _sha256_bytes(raw),
        }
    )
    _assert_resources()
    return value


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _read_only_connection() -> sqlite3.Connection:
    if TARGET_DB.is_symlink() or not TARGET_DB.is_file():
        raise FailClosed("target_ssot_missing")
    # The resident SSOT is a WAL database.  immutable=1 ignores the -wal
    # sidecar and can make a healthy live database appear malformed.  mode=ro
    # plus query_only preserves the no-write boundary while reading the
    # authoritative WAL view.
    uri = f"file:{TARGET_DB.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _db_fingerprint() -> dict[str, int]:
    stat = TARGET_DB.stat()
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _select_registry(
    conn: sqlite3.Connection, station_ids: Iterable[str]
) -> tuple[str, dict[str, dict[str, Any]]]:
    if not _table_exists(conn, "basket_target_market_registry_snapshot"):
        return "", {}
    snapshot = conn.execute(
        "SELECT snapshot_sha256 FROM basket_target_market_registry_snapshot "
        "WHERE exported_at_utc<=? ORDER BY exported_at_utc DESC, snapshot_sha256 DESC LIMIT 1",
        (DECISION_CUTOFF,),
    ).fetchone()
    if snapshot is None:
        return "", {}
    snapshot_sha = str(snapshot[0])
    stations = tuple(station_ids)
    placeholders = ",".join("?" for _ in stations)
    rows = conn.execute(
        "SELECT station_id,settlement_timezone,settlement_unit,source_lane,"
        "source_eligible,market_active,market_accepting_orders,slug,outcome,"
        "rules_sha256,yes_token,no_token,refreshed_at "
        "FROM basket_target_market_registry WHERE snapshot_sha256=? "
        "AND event_date=? AND station_id IN (" + placeholders + ") "
        "ORDER BY station_id,slug,outcome",
        (snapshot_sha, EVENT_DATE, *stations),
    ).fetchall()
    by_station: dict[str, dict[str, Any]] = {}
    for row in rows:
        station = str(row["station_id"])
        entry = by_station.setdefault(
            station,
            {
                "snapshot_sha256": snapshot_sha,
                "station_id": station,
                "settlement_timezone": str(row["settlement_timezone"] or ""),
                "settlement_unit": str(row["settlement_unit"] or ""),
                "source_lane": str(row["source_lane"] or ""),
                "source_eligible": bool(row["source_eligible"]),
                "market_active": bool(row["market_active"]),
                "market_accepting_orders": bool(row["market_accepting_orders"]),
                "market_count": 0,
                "complete_token_count": 0,
                "rules_sha256s": set(),
                "latest_refreshed_at": "",
            },
        )
        entry["market_count"] += 1
        if str(row["yes_token"] or "") and str(row["no_token"] or ""):
            entry["complete_token_count"] += 1
        if str(row["rules_sha256"] or ""):
            entry["rules_sha256s"].add(str(row["rules_sha256"]))
        entry["latest_refreshed_at"] = max(
            str(entry["latest_refreshed_at"]), str(row["refreshed_at"] or "")
        )
    for entry in by_station.values():
        entry["rules_sha256s"] = sorted(entry["rules_sha256s"])
    return snapshot_sha, by_station


def _asof_count(
    conn: sqlite3.Connection,
    table: str,
    station_id: str,
    *,
    time_columns: tuple[str, ...],
) -> int:
    if not _table_exists(conn, table):
        return 0
    cols = _columns(conn, table)
    if not {"station_id", "event_date"}.issubset(cols):
        return 0
    clock = next((item for item in time_columns if item in cols), "")
    if not clock:
        return 0
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE station_id=? AND event_date=? "
            f"AND {clock}<=?",
            (station_id, EVENT_DATE, DECISION_CUTOFF),
        ).fetchone()[0]
    )


def _probability_counts(
    conn: sqlite3.Connection, station_id: str
) -> dict[str, int]:
    counts = {"P0": 0, "P0-M": 0, "P1": 0}
    if not _table_exists(conn, "basket_probability_envelope"):
        return counts
    for row in conn.execute(
        "SELECT stage,COUNT(*) FROM basket_probability_envelope "
        "WHERE station_id=? AND event_date=? AND decision_at_utc<=? "
        "AND state='COMMITTED' GROUP BY stage",
        (station_id, EVENT_DATE, DECISION_CUTOFF),
    ):
        if str(row[0]) in counts:
            counts[str(row[0])] = int(row[1])
    return counts


def _arrival_index(payloads: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for expected_model, payload in payloads.items():
        rows = payload.get("arrivals")
        if not isinstance(rows, list):
            raise FailClosed(f"{expected_model.lower()}_arrival_rows_missing")
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise FailClosed(f"{expected_model.lower()}_arrival_row_invalid")
            station = str(raw.get("station_id") or "").strip().upper()
            model = str(raw.get("model_id") or "").strip().upper()
            if model != expected_model or not station:
                raise FailClosed(f"{expected_model.lower()}_arrival_identity_invalid")
            result.setdefault(station, {}).setdefault(model, []).append(dict(raw))
    return result


def _decode_index(payloads: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for expected_model, payload in payloads.items():
        container = payload.get("curves")
        curves = container.get("curves") if isinstance(container, Mapping) else None
        if not isinstance(curves, list):
            raise FailClosed(f"{expected_model.lower()}_decode_curves_missing")
        for raw in curves:
            if not isinstance(raw, Mapping):
                raise FailClosed(f"{expected_model.lower()}_decode_curve_invalid")
            station = str(raw.get("station_id") or "").strip().upper()
            model = str(raw.get("model_id") or "").strip().upper()
            if model != expected_model or not station:
                raise FailClosed(f"{expected_model.lower()}_decode_identity_invalid")
            if model in result.setdefault(station, {}):
                raise FailClosed(f"{expected_model.lower()}_decode_curve_ambiguous:{station}")
            result[station][model] = dict(raw)
    return result


def _model_status(
    station: str,
    model: str,
    arrivals: Mapping[str, Mapping[str, list[dict[str, Any]]]],
    decodes: Mapping[str, Mapping[str, dict[str, Any]]],
    settlement_timezone: str,
) -> dict[str, Any]:
    rows = list(arrivals.get(station, {}).get(model, []))
    curve = decodes.get(station, {}).get(model)
    reasons: list[str] = []
    if not rows:
        reasons.append("arrival_missing")
    if curve is None:
        reasons.append("decode_curve_missing")
    run_values = sorted({str(item.get("run_initialization_utc") or "") for item in rows})
    if run_values != [RUN_INITIALIZATION]:
        reasons.append("run_initialization_not_exact")
    after_cutoff = 0
    for item in rows:
        published = str(item.get("published_at_utc") or "")
        received = str(item.get("received_at_utc") or "")
        decision_at = str(item.get("decision_at_utc") or "")
        if (
            not published
            or not received
            or not decision_at
            or _utc(published) > _utc(DECISION_CUTOFF)
            or _utc(received) > _utc(DECISION_CUTOFF)
            or _utc(decision_at) > _utc(DECISION_CUTOFF)
        ):
            after_cutoff += 1
    if rows and after_cutoff:
        reasons.append("arrival_not_visible_as_of_cutoff")
    target_dates = sorted({str(item.get("target_local_date") or "") for item in rows})
    target_timezones = sorted({str(item.get("target_timezone") or "") for item in rows})
    if rows and target_dates != [EVENT_DATE]:
        reasons.append("arrival_event_date_scope_mismatch")
    if settlement_timezone and rows and target_timezones != [settlement_timezone]:
        reasons.append("arrival_settlement_timezone_scope_mismatch")
    if curve is not None:
        if str(curve.get("event_date") or "") != EVENT_DATE:
            reasons.append("decode_event_date_scope_mismatch")
        if settlement_timezone and str(curve.get("target_timezone") or "") != settlement_timezone:
            reasons.append("decode_settlement_timezone_scope_mismatch")
        if str(curve.get("state") or "") != "COMPLETE":
            reasons.append("decode_curve_incomplete")
    return {
        "status": "EXACT" if not reasons else "BLOCKED",
        "reason_codes": sorted(set(reasons)),
        "arrival_count": len(rows),
        "arrival_visible_count": len(rows) - after_cutoff,
        "run_initializations": run_values,
        "target_local_dates": target_dates,
        "target_timezones": target_timezones,
        "decode_present": curve is not None,
        "decode_state": str(curve.get("state") or "") if curve else "",
        "curve_sha256": str(curve.get("curve_sha256") or "") if curve else "",
    }


def _status(state: str, reasons: Iterable[str], **details: Any) -> dict[str, Any]:
    return {
        "state": state,
        "reason_codes": sorted(set(str(item) for item in reasons if item)),
        **details,
    }


def _build_station_rows(
    conn: sqlite3.Connection,
    station_ids: list[str],
    registry: Mapping[str, Mapping[str, Any]],
    arrivals: Mapping[str, Mapping[str, list[dict[str, Any]]]],
    decodes: Mapping[str, Mapping[str, dict[str, Any]]],
    contract_scope_reasons: Iterable[str],
) -> list[dict[str, Any]]:
    contract_reasons = sorted(set(str(item) for item in contract_scope_reasons if item))
    rows: list[dict[str, Any]] = []
    for station in station_ids:
        reg = dict(registry.get(station, {}))
        timezone_name = str(reg.get("settlement_timezone") or "")
        models = {
            model: _model_status(
                station, model, arrivals, decodes, timezone_name
            )
            for model in MODEL_IDS
        }
        exact_models = [model for model, item in models.items() if item["status"] == "EXACT"]
        model_reasons = sorted(
            {
                f"{model.lower()}:{reason}"
                for model, item in models.items()
                for reason in item["reason_codes"]
            }
        )
        if len(exact_models) != len(MODEL_IDS):
            model_reasons.append("three_model_exact_join_incomplete")

        if reg:
            registry_reasons = []
            if not reg["source_eligible"]:
                registry_reasons.append("registry_source_ineligible")
            if not timezone_name:
                registry_reasons.append("registry_settlement_timezone_missing")
            if int(reg["market_count"]) <= 0:
                registry_reasons.append("registry_markets_missing")
            registry_status = _status(
                "PASS" if not registry_reasons else "BLOCKED",
                registry_reasons,
                **reg,
            )
        else:
            registry_status = _status(
                "BLOCKED",
                ["registry_scope_missing_as_of_cutoff"],
                snapshot_sha256="",
                station_id=station,
                settlement_timezone="",
                market_count=0,
                complete_token_count=0,
            )

        probabilities = _probability_counts(conn, station)
        probability_reasons: list[str] = []
        if probabilities["P0"] + probabilities["P0-M"] <= 0:
            probability_reasons.append("p0_or_p0m_missing_as_of_cutoff")
        if probabilities["P1"] <= 0:
            probability_reasons.append("p1_missing_as_of_cutoff")
        if model_reasons:
            probability_reasons.append("upstream_three_model_join_blocked")

        ws_count = _asof_count(
            conn,
            "basket_official_ws_snapshot",
            station,
            time_columns=("quote_observed_at", "observed_at", "created_at"),
        )
        inventory_count = _asof_count(
            conn,
            "basket_confirmed_inventory_snapshot",
            station,
            time_columns=("observed_at", "created_at"),
        )
        order_state_count = _asof_count(
            conn,
            "basket_order_state_snapshot",
            station,
            time_columns=("observed_at", "created_at"),
        )
        basket_reasons = []
        if probability_reasons:
            basket_reasons.append("probability_checkpoint_unavailable")
        if registry_status["state"] != "PASS":
            basket_reasons.append("market_registry_unavailable")
        if ws_count <= 0:
            basket_reasons.append("official_ws_snapshot_missing_as_of_cutoff")
        if inventory_count <= 0:
            basket_reasons.append("confirmed_q0_inventory_missing_as_of_cutoff")
        if order_state_count <= 0:
            basket_reasons.append("order_state_missing_as_of_cutoff")

        stage_status = {
            "contract": _status(
                "PASS" if not contract_reasons else "BLOCKED",
                contract_reasons,
                station_id=station,
                event_date=EVENT_DATE,
                decision_cutoff_utc=DECISION_CUTOFF,
                run_initialization_utc=RUN_INITIALIZATION,
            ),
            "model_arrival_decode_exact_join": _status(
                "PASS" if not model_reasons else "BLOCKED",
                model_reasons,
                exact_model_count=len(exact_models),
                required_model_count=len(MODEL_IDS),
                models=models,
            ),
            "market_registry": registry_status,
            "p0_p1": _status(
                "PASS" if not probability_reasons else "BLOCKED",
                probability_reasons,
                committed_as_of_cutoff=probabilities,
            ),
            "official_ws": _status(
                "PASS" if ws_count > 0 else "BLOCKED",
                [] if ws_count > 0 else ["official_ws_snapshot_missing_as_of_cutoff"],
                snapshot_count=ws_count,
                source="official_clob_websocket_only",
            ),
            "q0_order_state": _status(
                "PASS" if inventory_count > 0 and order_state_count > 0 else "BLOCKED",
                [
                    *(
                        []
                        if inventory_count > 0
                        else ["confirmed_q0_inventory_missing_as_of_cutoff"]
                    ),
                    *(
                        []
                        if order_state_count > 0
                        else ["order_state_missing_as_of_cutoff"]
                    ),
                ],
                inventory_snapshot_count=inventory_count,
                order_state_snapshot_count=order_state_count,
            ),
            "basket": _status(
                "BLOCKED" if basket_reasons else "PASS",
                basket_reasons,
                target_inventory_materialized=False,
                delta_basket_score_evaluated=False,
                intents_generated=0,
            ),
            "no_live_decision_reconcile": _status(
                "FAIL_CLOSED",
                basket_reasons or ["no_executable_positive_delta"],
                execution_mode="production_no_live",
                will_order=False,
                intents_written=0,
                orders_submitted=0,
                calls_order_gateway=False,
                reconciliation_state="NO_INTENTS_TO_RECONCILE",
            ),
        }
        rows.append(
            {
                "station_id": station,
                "event_date": EVENT_DATE,
                "settlement_timezone": timezone_name,
                "final_state": "FAIL_CLOSED",
                "first_blocked_stage": next(
                    (
                        name
                        for name in STAGES
                        if stage_status[name]["state"] not in {"PASS"}
                    ),
                    "",
                ),
                "stages": stage_status,
            }
        )
        _assert_resources()
    return rows


def _write_sqlite(path: Path, rows: list[dict[str, Any]], run_sha256: str) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=".station_chain.", suffix=".sqlite.tmp", dir=path.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        conn = sqlite3.connect(temporary)
        conn.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE run_metadata (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              schema_name TEXT NOT NULL,
              event_date TEXT NOT NULL,
              decision_cutoff_utc TEXT NOT NULL,
              run_initialization_utc TEXT NOT NULL,
              station_count INTEGER NOT NULL CHECK(station_count<="""
            + str(MAX_STATIONS)
            + """),
              execution_mode TEXT NOT NULL CHECK(execution_mode='production_no_live'),
              live_submit_authorized INTEGER NOT NULL CHECK(live_submit_authorized=0),
              calls_order_gateway INTEGER NOT NULL CHECK(calls_order_gateway=0),
              orders_submitted INTEGER NOT NULL CHECK(orders_submitted=0),
              run_sha256 TEXT NOT NULL CHECK(length(run_sha256)=64)
            );
            CREATE TABLE station_chain_status (
              station_id TEXT PRIMARY KEY,
              event_date TEXT NOT NULL,
              settlement_timezone TEXT NOT NULL,
              final_state TEXT NOT NULL CHECK(final_state='FAIL_CLOSED'),
              first_blocked_stage TEXT NOT NULL,
              stage_status_json TEXT NOT NULL,
              station_sha256 TEXT NOT NULL CHECK(length(station_sha256)=64)
            );
            CREATE TABLE no_live_audit_decision (
              station_id TEXT PRIMARY KEY REFERENCES station_chain_status(station_id),
              decided_state TEXT NOT NULL CHECK(decided_state='blocked'),
              will_order INTEGER NOT NULL CHECK(will_order=0),
              intents_written INTEGER NOT NULL CHECK(intents_written=0),
              orders_submitted INTEGER NOT NULL CHECK(orders_submitted=0),
              reconciliation_state TEXT NOT NULL CHECK(reconciliation_state='NO_INTENTS_TO_RECONCILE'),
              reason_codes_json TEXT NOT NULL,
              decision_sha256 TEXT NOT NULL CHECK(length(decision_sha256)=64)
            );
            """
        )
        conn.execute(
            "INSERT INTO run_metadata VALUES(1,?,?,?,?,?,?,?,?,?,?)",
            (
                SCHEMA,
                EVENT_DATE,
                DECISION_CUTOFF,
                RUN_INITIALIZATION,
                len(rows),
                "production_no_live",
                0,
                0,
                0,
                run_sha256,
            ),
        )
        for row in rows:
            station_core = {
                "station_id": row["station_id"],
                "event_date": row["event_date"],
                "settlement_timezone": row["settlement_timezone"],
                "final_state": row["final_state"],
                "first_blocked_stage": row["first_blocked_stage"],
                "stages": row["stages"],
            }
            station_sha = _sha256_bytes(_canonical(station_core))
            stage_json = _canonical(row["stages"]).decode("utf-8")
            conn.execute(
                "INSERT INTO station_chain_status VALUES(?,?,?,?,?,?,?)",
                (
                    row["station_id"],
                    row["event_date"],
                    row["settlement_timezone"],
                    row["final_state"],
                    row["first_blocked_stage"],
                    stage_json,
                    station_sha,
                ),
            )
            decision = row["stages"]["no_live_decision_reconcile"]
            reason_json = _canonical(decision["reason_codes"]).decode("utf-8")
            decision_core = {
                "station_id": row["station_id"],
                "decided_state": "blocked",
                "will_order": 0,
                "intents_written": 0,
                "orders_submitted": 0,
                "reconciliation_state": "NO_INTENTS_TO_RECONCILE",
                "reason_codes": decision["reason_codes"],
            }
            conn.execute(
                "INSERT INTO no_live_audit_decision VALUES(?,?,?,?,?,?,?,?)",
                (
                    row["station_id"],
                    "blocked",
                    0,
                    0,
                    0,
                    "NO_INTENTS_TO_RECONCILE",
                    reason_json,
                    _sha256_bytes(_canonical(decision_core)),
                ),
            )
        conn.commit()
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise FailClosed("isolated_sqlite_integrity_failed")
        conn.close()
        size = temporary.stat().st_size
        if size <= 0 or size > MAX_OUTPUT_FILE_BYTES:
            raise FailClosed("isolated_sqlite_size_limit_exceeded")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    body = json.dumps(
        receipt, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    if len(body) > MAX_OUTPUT_FILE_BYTES:
        raise FailClosed("receipt_size_limit_exceeded")
    fd, temporary_name = tempfile.mkstemp(
        prefix=".receipt.", suffix=".json.tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    if _utc(datetime.now(timezone.utc).isoformat()) < _utc("2026-07-26T04:00:00Z"):
        raise FailClosed("et_day_not_closed")
    resources_before = _assert_resources()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if OUTPUT_DIR.is_symlink():
        raise FailClosed("output_directory_symlink_forbidden")
    budget: dict[str, Any] = {
        "input_bytes": 0,
        "json_nodes": 0,
        "max_json_depth": 0,
        "inputs": [],
    }

    contract = _load_json(CONTRACT_PATH, budget)
    if contract.get("schema") != "bayes_basket_target_model_run_contract_v1":
        raise FailClosed("model_run_contract_schema_invalid")
    if str(contract.get("run_initialization_utc") or "") != RUN_INITIALIZATION:
        raise FailClosed("model_run_contract_run_mismatch")
    station_ids = sorted(
        {str(item).strip().upper() for item in contract.get("station_ids", [])}
    )
    if len(station_ids) != MAX_STATIONS:
        raise FailClosed(f"canonical_station_count_not_{MAX_STATIONS}")
    if str(contract.get("target_local_date") or "") == EVENT_DATE:
        contract_scope_state = "EXACT"
        contract_scope_reasons: list[str] = []
    else:
        contract_scope_state = "RETAINED_SCOPE_MISMATCH"
        contract_scope_reasons = ["retained_contract_target_local_date_is_2026-07-25"]

    arrival_payloads = {
        model: _load_json(path, budget) for model, path in ARRIVAL_PATHS.items()
    }
    decode_payloads = {
        model: _load_json(path, budget) for model, path in DECODE_PATHS.items()
    }
    arrivals = _arrival_index(arrival_payloads)
    decodes = _decode_index(decode_payloads)

    db_before = _db_fingerprint()
    with _read_only_connection() as conn:
        snapshot_sha, registry = _select_registry(conn, station_ids)
        rows = _build_station_rows(
            conn,
            station_ids,
            registry,
            arrivals,
            decodes,
            contract_scope_reasons,
        )
    db_after = _db_fingerprint()
    if db_before != db_after:
        raise FailClosed("target_ssot_metadata_changed_during_read")
    if len(rows) != MAX_STATIONS:
        raise FailClosed("station_result_count_not_45")

    stage_counts: dict[str, dict[str, int]] = {}
    for stage in STAGES:
        counts: dict[str, int] = {}
        for row in rows:
            state = str(row["stages"][stage]["state"])
            counts[state] = counts.get(state, 0) + 1
        stage_counts[stage] = dict(sorted(counts.items()))
    run_core = {
        "schema": SCHEMA,
        "event_date": EVENT_DATE,
        "decision_cutoff_utc": DECISION_CUTOFF,
        "run_initialization_utc": RUN_INITIALIZATION,
        "station_ids": station_ids,
        "rows": rows,
    }
    run_sha = _sha256_bytes(_canonical(run_core))
    sqlite_path = OUTPUT_DIR / "isolated_no_live.sqlite"
    _write_sqlite(sqlite_path, rows, run_sha)

    resources_after = _assert_resources()
    receipt: dict[str, Any] = {
        **run_core,
        "run_sha256": run_sha,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "state": "FAIL_CLOSED",
        "contract_scope": {
            "state": contract_scope_state,
            "reason_codes": contract_scope_reasons,
            "retained_contract_sha256": str(contract.get("contract_sha256") or ""),
        },
        "registry_snapshot_sha256_as_of_cutoff": snapshot_sha,
        "summary": {
            "station_count": len(rows),
            "fully_executable_station_count": 0,
            "fail_closed_station_count": len(rows),
            "orders_submitted": 0,
            "intents_written": 0,
            "calls_order_gateway": False,
            "writes_production_db": False,
            "live_submit_authorized": False,
            "bounded_fetch_attempted": False,
            "bounded_fetch_skip_reason": (
                "historical_cutoff_precedes_retained_publication_and_receive_times;"
                "a current fetch cannot create valid prospective as-of evidence"
            ),
            "stage_counts": stage_counts,
        },
        "boundary": {
            "execution_mode": "production_no_live",
            "isolated_sqlite": str(sqlite_path),
            "target_ssot_open_mode": "read_only_wal_safe_query_only",
            "output_write_root": str(OUTPUT_DIR),
            "network_used": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "writes_production_db": False,
            "writes_real_ssot": False,
            "live_submit_authorized": False,
        },
        "resource_budget": {
            "max_stations": MAX_STATIONS,
            "max_rss_bytes": MAX_RSS_BYTES,
            "max_input_file_bytes": MAX_INPUT_FILE_BYTES,
            "max_total_input_bytes": MAX_TOTAL_INPUT_BYTES,
            "max_output_file_bytes": MAX_OUTPUT_FILE_BYTES,
            "max_total_output_bytes": MAX_TOTAL_OUTPUT_BYTES,
            "minimum_free_bytes": MIN_FREE_BYTES,
            "observed_input_bytes": budget["input_bytes"],
            "observed_json_nodes": budget["json_nodes"],
            "observed_max_json_depth": budget["max_json_depth"],
            "rss_bytes_before": resources_before["rss_bytes"],
            "rss_bytes_after": resources_after["rss_bytes"],
            "free_bytes_before": resources_before["free_bytes"],
            "free_bytes_after": resources_after["free_bytes"],
        },
        "input_artifacts": budget["inputs"],
        "target_ssot_fingerprint_before": db_before,
        "target_ssot_fingerprint_after": db_after,
    }
    receipt_core = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    receipt["receipt_sha256"] = _sha256_bytes(_canonical(receipt_core))
    receipt_path = OUTPUT_DIR / "receipt.json"
    _write_receipt(receipt_path, receipt)

    output_total = sum(
        path.stat().st_size for path in OUTPUT_DIR.iterdir() if path.is_file()
    )
    if output_total > MAX_TOTAL_OUTPUT_BYTES:
        raise FailClosed("total_output_limit_exceeded")
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "state": receipt["state"],
                "station_count": len(rows),
                "fail_closed_station_count": len(rows),
                "orders_submitted": 0,
                "calls_order_gateway": False,
                "receipt": str(receipt_path),
                "isolated_sqlite": str(sqlite_path),
                "receipt_sha256": receipt["receipt_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FailClosed as exc:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "state": "RUNNER_FAIL_CLOSED",
                    "reason": str(exc),
                    "orders_submitted": 0,
                    "calls_order_gateway": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
