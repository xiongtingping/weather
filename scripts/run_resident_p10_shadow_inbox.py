#!/usr/bin/env python3
"""Run the isolated resident P10 shadow host from immutable AMSC inbox files.

This is deliberately an *inbox consumer*, never an AMSC poller.  A future
source sidecar must raw-archive an AMSC bundle first and atomically place its
JSON payload in the inbox.  The resident process then reuses one prepared
host for every received file, with no generic task queue, forecast refresh,
subprocess-per-event, production database write, signer, or CLOB capability.

Default mode is a no-write preflight.  ``--apply`` writes only the explicitly
isolated shadow SQLite database and requires an exact confirmation phrase.
It cannot be pointed at ``data/archive.db``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_p10_shadow_bundle_loader import (  # noqa: E402
    FORWARD_NEW_ONLY,
    REPLAY_ALL,
    SUPPORTED_LOAD_MODES,
    load_amsc_p10_shadow_bundle,
)
from amsc_p10_shadow_source_adapter import build_amsc_p10_shadow_arrival_candidates  # noqa: E402
from resident_p10_shadow_service import ResidentP10ShadowService  # noqa: E402


CONFIRMATION = "RUN_RESIDENT_P10_ISOLATED_SHADOW"
SCHEMA_VERSION = "resident_p10_shadow_inbox_command_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _payload(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("resident_p10_inbox_payload_not_object")
    return dict(value)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_production_archive(path: Path) -> bool:
    return path.resolve() == (ROOT / "data" / "archive.db").resolve() or path.name == "archive.db"


def _open_shadow_db(path: Path, *, writable: bool) -> sqlite3.Connection:
    if _is_production_archive(path):
        raise ValueError("resident_p10_inbox_production_archive_forbidden")
    resolved = path.expanduser().resolve()
    if writable:
        return sqlite3.connect(resolved)
    return sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)


def _preflight(
    *,
    payload: Mapping[str, Any],
    raw_archive_dir: Path,
    mode: str,
    conn: sqlite3.Connection,
    prepared_cycle_id: str,
) -> dict[str, Any]:
    loaded = load_amsc_p10_shadow_bundle(
        payload,
        raw_archive_dir=raw_archive_dir,
        mode=mode,
    )
    selection: dict[str, Any] = {}
    if loaded.get("ok") is True:
        selection = build_amsc_p10_shadow_arrival_candidates(loaded.get("bundle") or {})
    try:
        service = ResidentP10ShadowService(conn, prepared_cycle_id=prepared_cycle_id)
        readiness = service.readiness()
    except Exception as exc:
        readiness = {
            "ok": False,
            "state": "blocked",
            "blockers": [f"resident_p10_inbox_prepared_host_unavailable:{type(exc).__name__}"],
        }
    blockers = list(loaded.get("blockers") or [])
    if selection.get("ok") is not True:
        blockers.extend(str(value) for value in selection.get("blockers") or ["resident_p10_inbox_source_selection_invalid"])
    if readiness.get("ok") is not True:
        blockers.extend(str(value) for value in readiness.get("blockers") or ["resident_p10_inbox_prepared_host_not_ready"])
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "prepared_cycle_id": prepared_cycle_id,
        "bundle_load": loaded,
        "source_selection": selection,
        "host_readiness": readiness,
        "read_only": True,
        "shadow_only": True,
        "places_orders": False,
        "boundary": {
            "network_acquisition": False,
            "generic_task_queue": False,
            "forecast_refresh_on_hot_path": False,
            "production_archive_write": False,
            "clob": False,
            "t1_synthesized_from_inbox_read": False,
        },
    }


def _apply_one(
    *,
    payload: Mapping[str, Any],
    raw_archive_dir: Path,
    mode: str,
    service: ResidentP10ShadowService,
) -> dict[str, Any]:
    loaded = load_amsc_p10_shadow_bundle(
        payload,
        raw_archive_dir=raw_archive_dir,
        mode=mode,
    )
    if loaded.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked_before_dispatch",
            "blockers": list(loaded.get("blockers") or ["resident_p10_inbox_bundle_load_failed"]),
            "bundle_load": loaded,
            "shadow_only": True,
            "places_orders": False,
        }
    dispatch = service.process_amsc_bundle(amsc_observation_bundle=loaded.get("bundle") or {})
    return {
        "schema": SCHEMA_VERSION,
        "ok": dispatch.get("ok") is True,
        "state": _text(dispatch.get("state")),
        "bundle_load": loaded,
        "dispatch": dispatch,
        "shadow_only": True,
        "places_orders": False,
    }


def _inbox_paths(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(
        (candidate for candidate in path.glob("*.json") if candidate.is_file() and not candidate.name.startswith(".")),
        key=lambda candidate: (candidate.stat().st_mtime_ns, candidate.name),
    )


def _emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(dict(value), ensure_ascii=False, sort_keys=True), flush=True)


def _write_runtime_status(path: Path, value: Mapping[str, Any]) -> None:
    payload = {
        "schema": "resident_p10_shadow_runtime_status_v1",
        "resident_pid": os.getpid(),
        "observed_at": _utc_now_iso(),
        "shadow_only": True,
        "places_orders": False,
        **dict(value),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _compact_bundle_load(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep operational logs lineage-rich without echoing raw weather payloads."""

    loaded = dict(value)
    bundle = loaded.get("bundle") if isinstance(loaded.get("bundle"), Mapping) else {}
    return {
        "schema": _text(loaded.get("schema")),
        "ok": loaded.get("ok") is True,
        "state": _text(loaded.get("state")),
        "blockers": list(loaded.get("blockers") or []),
        "input_kind": _text(loaded.get("input_kind")),
        "mode": _text(loaded.get("mode")),
        "manifest": dict(loaded.get("manifest") or {}),
        "selected_primary_count": int(loaded.get("selected_primary_count") or 0),
        "selected_source_observation_ids": list(bundle.get("source_observation_ids") or []),
        "rejected_primary_rows": list(loaded.get("rejected_primary_rows") or []),
        "excluded_preexisting_primary_source_observation_ids": list(
            loaded.get("excluded_preexisting_primary_source_observation_ids") or []
        ),
        "boundary": dict(loaded.get("boundary") or {}),
        "read_only": loaded.get("read_only") is True,
        "shadow_only": loaded.get("shadow_only") is True,
        "places_orders": False,
    }


def _compact_selection(value: Mapping[str, Any]) -> dict[str, Any]:
    selection = dict(value)
    arrivals: list[dict[str, Any]] = []
    for raw in selection.get("arrival_candidates") or []:
        row = dict(raw) if isinstance(raw, Mapping) else {}
        arrivals.append(
            {
                "source_observation_id": _text(row.get("source_observation_id")),
                "source_event_id": _text(row.get("source_event_id")),
                "report_type": _text(row.get("report_type")),
                "first_transport_seen_at": _text(row.get("first_transport_seen_at")),
                "first_actionable_seen_at": _text(row.get("first_actionable_seen_at")),
            }
        )
    return {
        "schema": _text(selection.get("schema")),
        "ok": selection.get("ok") is True,
        "state": _text(selection.get("state")),
        "blockers": list(selection.get("blockers") or []),
        "candidate_count": int(selection.get("candidate_count") or 0),
        "arrival_candidates": arrivals,
        "rejected_primary_rows": list(selection.get("rejected_primary_rows") or []),
        "boundary": dict(selection.get("boundary") or {}),
        "shadow_only": selection.get("shadow_only") is True,
        "places_orders": False,
    }


def _compact_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize every stdout record into an immutable operational summary."""

    result = dict(value)
    if isinstance(result.get("bundle_load"), Mapping):
        result["bundle_load"] = _compact_bundle_load(result["bundle_load"])
    if isinstance(result.get("source_selection"), Mapping):
        result["source_selection"] = _compact_selection(result["source_selection"])
    dispatch = result.get("dispatch")
    if isinstance(dispatch, Mapping):
        dispatched = dict(dispatch)
        bundle_dispatch = dispatched.get("bundle_dispatch")
        compact_dispatches: list[dict[str, Any]] = []
        if isinstance(bundle_dispatch, Mapping):
            for row in bundle_dispatch.get("dispatches") or []:
                item = dict(row) if isinstance(row, Mapping) else {}
                compact_dispatches.append(
                    {
                        "source_observation_id": _text(item.get("source_observation_id")),
                        "ok": item.get("ok") is True,
                        "state": _text(item.get("state")),
                        "terminal_deadline_blocked": item.get("terminal_deadline_blocked") is True,
                    }
                )
            dispatched["bundle_dispatch"] = {
                "schema": _text(bundle_dispatch.get("schema")),
                "ok": bundle_dispatch.get("ok") is True,
                "state": _text(bundle_dispatch.get("state")),
                "dispatch_count": int(bundle_dispatch.get("dispatch_count") or 0),
                "p10_latency_sample_created": bundle_dispatch.get("p10_latency_sample_created") is True,
                "selection": _compact_selection(bundle_dispatch.get("selection") or {}),
                "dispatches": compact_dispatches,
            }
        result["dispatch"] = dispatched
    return result


def _run_follow(args: argparse.Namespace, *, shadow_db: Path, raw_archive_dir: Path) -> int:
    if args.mode != FORWARD_NEW_ONLY:
        _emit({"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["resident_p10_inbox_follow_requires_forward_mode"]})
        return 2
    inbox = Path(args.inbox_dir).expanduser().resolve()
    if not inbox.is_dir():
        _emit({"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["resident_p10_inbox_directory_missing"]})
        return 2
    seen: dict[Path, str] = {}
    if args.from_now:
        for path in _inbox_paths(inbox):
            try:
                seen[path] = _file_digest(path)
            except OSError:
                continue
    processed = 0
    last_processed_at = ""
    last_result_ok: bool | None = None
    last_result_state = ""
    status_path = Path(args.status_path).expanduser().resolve()
    with _open_shadow_db(shadow_db, writable=True) as conn:
        service = ResidentP10ShadowService(conn, prepared_cycle_id=args.prepared_cycle_id)
        cycle_row = conn.execute(
            "SELECT target_identity,event_date FROM resident_p10_prepared_cycles_shadow WHERE prepared_cycle_id=?",
            (args.prepared_cycle_id,),
        ).fetchone()
        target_identity = _text(cycle_row[0]) if cycle_row else ""
        event_date = _text(cycle_row[1]) if cycle_row else ""
        _write_runtime_status(status_path, {
            "state": "running",
            "prepared_cycle_id": args.prepared_cycle_id,
            "target_identity": target_identity,
            "event_date": event_date,
            "processed_file_count": 0,
            "last_processed_at": "",
        })
        _emit({
            "schema": SCHEMA_VERSION,
            "ok": service.readiness().get("ok") is True,
            "state": "resident_inbox_started",
            "started_at": _utc_now_iso(),
            "prepared_cycle_id": args.prepared_cycle_id,
            "from_now": bool(args.from_now),
            "shadow_only": True,
            "places_orders": False,
        })
        last_heartbeat = time.monotonic()
        while True:
            ready: list[tuple[Path, str]] = []
            for path in _inbox_paths(inbox):
                try:
                    digest = _file_digest(path)
                except OSError:
                    continue
                if seen.get(path) != digest:
                    ready.append((path, digest))
            # Filename mtime is not T1.  Each payload's own immutable T1 is
            # revalidated by the loader/adapter/journal before dispatch.
            for path, digest in ready:
                try:
                    result = _apply_one(
                        payload=_payload(path),
                        raw_archive_dir=raw_archive_dir,
                        mode=args.mode,
                        service=service,
                    )
                except Exception as exc:
                    result = {
                        "schema": SCHEMA_VERSION,
                        "ok": False,
                        "state": "blocked",
                        "blockers": [f"resident_p10_inbox_process_failed:{type(exc).__name__}"],
                        "shadow_only": True,
                        "places_orders": False,
                    }
                seen[path] = digest
                processed += 1
                last_processed_at = _utc_now_iso()
                last_result_ok = result.get("ok") is True
                last_result_state = _text(result.get("state"))
                _emit(_compact_result({**result, "inbox_path": str(path), "processed_at": last_processed_at}))
                _write_runtime_status(status_path, {
                    "state": "running",
                    "prepared_cycle_id": args.prepared_cycle_id,
                    "target_identity": target_identity,
                    "event_date": event_date,
                    "processed_file_count": processed,
                    "last_processed_at": last_processed_at,
                    "last_result_ok": last_result_ok,
                    "last_result_state": last_result_state,
                })
                if args.max_files and processed >= args.max_files:
                    return 0
            if time.monotonic() - last_heartbeat >= 5.0:
                _write_runtime_status(status_path, {
                    "state": "running",
                    "prepared_cycle_id": args.prepared_cycle_id,
                    "target_identity": target_identity,
                    "event_date": event_date,
                    "processed_file_count": processed,
                    "last_processed_at": last_processed_at,
                    "last_result_ok": last_result_ok,
                    "last_result_state": last_result_state,
                })
                last_heartbeat = time.monotonic()
            time.sleep(args.poll_interval_ms / 1000.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", help="one immutable AMSC bundle or fact-plan JSON file")
    group.add_argument("--inbox-dir", help="directory of atomically-published immutable AMSC JSON files")
    parser.add_argument("--shadow-db", required=True, help="isolated shadow DB; archive.db is forbidden")
    parser.add_argument("--prepared-cycle-id", required=True)
    parser.add_argument("--raw-archive-dir", default=str(ROOT / "data" / "shadow" / "p10_amsc_raw"))
    parser.add_argument("--status-path", default=str(ROOT / "data" / "shadow" / "resident_p10_shadow_status_latest.json"))
    parser.add_argument("--mode", choices=sorted(SUPPORTED_LOAD_MODES), default=FORWARD_NEW_ONLY)
    parser.add_argument("--apply", action="store_true", help="dispatch only into the isolated shadow DB")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--from-now", action="store_true", help="follow mode ignores inbox files present at startup")
    parser.add_argument("--poll-interval-ms", type=int, default=50)
    parser.add_argument("--max-files", type=int, default=0, help="test-only follow stop after N processed files")
    args = parser.parse_args(argv)
    shadow_db = Path(args.shadow_db).expanduser().resolve()
    raw_archive_dir = Path(args.raw_archive_dir).expanduser().resolve()
    if _is_production_archive(shadow_db):
        _emit({"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["resident_p10_inbox_production_archive_forbidden"]})
        return 2
    if args.poll_interval_ms < 10 or args.poll_interval_ms > 1000:
        _emit({"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["resident_p10_inbox_poll_interval_out_of_range"]})
        return 2
    if args.apply and args.confirm != CONFIRMATION:
        _emit({"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["resident_p10_inbox_apply_confirmation_invalid"]})
        return 2
    if args.inbox_dir:
        if not args.apply:
            _emit({"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["resident_p10_inbox_follow_apply_required"]})
            return 2
        return _run_follow(args, shadow_db=shadow_db, raw_archive_dir=raw_archive_dir)

    input_path = Path(args.input).expanduser().resolve()
    try:
        payload = _payload(input_path)
        if not args.apply:
            with _open_shadow_db(shadow_db, writable=False) as conn:
                result = _preflight(
                    payload=payload,
                    raw_archive_dir=raw_archive_dir,
                    mode=args.mode,
                    conn=conn,
                    prepared_cycle_id=args.prepared_cycle_id,
                )
        else:
            with _open_shadow_db(shadow_db, writable=True) as conn:
                service = ResidentP10ShadowService(conn, prepared_cycle_id=args.prepared_cycle_id)
                result = _apply_one(
                    payload=payload,
                    raw_archive_dir=raw_archive_dir,
                    mode=args.mode,
                    service=service,
                )
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": [f"resident_p10_inbox_command_failed:{type(exc).__name__}"],
            "shadow_only": True,
            "places_orders": False,
        }
    _emit(_compact_result({**result, "input_path": str(input_path), "apply": bool(args.apply)}))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
