#!/usr/bin/env python3
"""Archive cold, unreferenced preselection plans off the writer, then retire one batch.

``archive`` is read-only against the SSOT and may run beside production.
``apply`` is the only SSOT writer and must be hosted in the resident's writer
rotation.  It rechecks the external archive before deleting; an unavailable
disk postpones retention without affecting production.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import retire_ssot_class_a_pre_20260905 as legacy  # noqa: E402
from bayes_basket_target.resource_bounds import assert_process_memory_budget  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STATE = ROOT / "data" / "runtime" / "preselection_retention_cursor.json"
PENDING = ROOT / "data" / "runtime" / "preselection_retention_pending.json"
EXTERNAL = Path("/Volumes/weather_data/weather_cold/ssot_preselection_rolling")
TABLE = "basket_p0_preselection_plan"
MAX_BATCH_BYTES = 64 * 1024 * 1024
MAX_BATCH_ROWS = 1500
MAX_SCANNED_ROWS = 5000
MAX_PENDING_BATCHES = 128
MAX_APPLY_BATCHES_PER_TURN = 4
MAX_APPLY_TURN_SECONDS = 60.0
MAX_PROCESS_RSS_BYTES = 512 * 1024 * 1024
CONTINUE_MARKER = "hosted_sweep:continue"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("preselection_retention_state_invalid")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".preselection_retention_", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _cutoff(now: datetime) -> str:
    return (now.astimezone(timezone.utc).date() - timedelta(days=7)).isoformat()


def _external_available(db: Path, external: Path) -> bool:
    try:
        return (external.parent.is_dir()
                and os.stat(external.parent).st_dev != os.stat(db).st_dev)
    except OSError:
        return False


def _row_payload(columns: list[str], row: tuple) -> bytes:
    return json.dumps({
        "table": TABLE,
        "rowid": int(row[0]),
        "row": {name: legacy._encode(value) for name, value in zip(columns, row[1:])},
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _pending_paths(base: Path) -> list[Path]:
    paths = [base] if base.exists() else []
    paths.extend(base.parent.glob(base.stem + "_*.json"))
    return sorted(paths, key=lambda path: int(_read_json(path)["rowids"][0]))


def archive_once(
    db: Path = DB, state_path: Path = STATE, pending_path: Path = PENDING,
    external: Path = EXTERNAL, *, now: datetime | None = None,
) -> dict:
    assert_process_memory_budget(limit=MAX_PROCESS_RSS_BYTES)
    if len(_pending_paths(pending_path)) >= MAX_PENDING_BATCHES:
        return {"state": "pending_queue_full"}
    if not _external_available(db, external):
        return {"state": "external_unavailable"}
    try:
        external.mkdir(exist_ok=True)
        legacy._require_external_export(db, external)
    except (OSError, SystemExit):
        return {"state": "external_unavailable"}
    cutoff = _cutoff(now or datetime.now(timezone.utc))
    state = _read_json(state_path)
    previous_cutoff = str(state.get("cutoff") or "")
    if previous_cutoff and cutoff < previous_cutoff:
        raise RuntimeError("preselection_retention_clock_reversed")
    cursor = int(state.get("cursor") or 0)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    conn.execute("PRAGMA query_only=ON")
    try:
        columns = legacy._columns(conn, TABLE)
        required = {"preselection_id", "station_id", "event_date", "created_at", "plan_json"}
        if not required.issubset(columns):
            raise RuntimeError("preselection_retention_schema_invalid")
        named_ids, named_plans, highwater = legacy._preselection_decision_references(conn)
        latest = {int(rid) for _station, rid in conn.execute(
            f"SELECT station_id,MAX(rowid) FROM {TABLE} GROUP BY station_id"
        )}
        candidate: list[tuple] = []
        candidate_meta: list[tuple[str, str, str, str]] = []
        candidate_bytes = 0
        scanned = 0
        end_cursor = cursor
        first_ineligible = False
        rows = conn.execute(
            f"SELECT rowid,preselection_id,station_id,event_date,created_at,"
            " CASE WHEN json_valid(plan_json) THEN"
            " json_extract(plan_json,'$.compiled_plan.production_plan_sha256')"
            f" ELSE NULL END FROM {TABLE} WHERE rowid>? ORDER BY rowid LIMIT ?",
            (cursor, MAX_SCANNED_ROWS),
        )
        for rid, raw_id, raw_station, raw_day, raw_created, raw_sha in rows:
            created = str(raw_created or "")
            if not created:
                raise RuntimeError("preselection_retention_created_at_missing")
            if created >= cutoff:
                first_ineligible = True
                break
            scanned += 1
            end_cursor = int(rid)
            plan_id = str(raw_id)
            station = str(raw_station)
            day = str(raw_day)
            if end_cursor in latest or plan_id in named_ids:
                continue
            plan_sha = str(raw_sha or "")
            if not plan_sha or (station, day, plan_sha) in named_plans:
                continue
            row = conn.execute(f"SELECT rowid,* FROM {TABLE} WHERE rowid=?", (rid,)).fetchone()
            if row is None:
                raise RuntimeError("preselection_retention_candidate_disappeared")
            payload = _row_payload(columns, row)
            if len(payload) > MAX_BATCH_BYTES:
                raise RuntimeError("preselection_retention_row_too_large")
            if candidate and (candidate_bytes + len(payload) > MAX_BATCH_BYTES
                              or len(candidate) >= MAX_BATCH_ROWS):
                # This row has not been archived, so resume at its predecessor.
                end_cursor = int(candidate[-1][0])
                break
            candidate.append(row)
            candidate_meta.append((plan_id, station, day, plan_sha))
            candidate_bytes += len(payload)
            assert_process_memory_budget(
                projected_bytes=2 * candidate_bytes, limit=MAX_PROCESS_RSS_BYTES,
            )
        # The scan never writes SSOT.  It may leave a short read snapshot only.
    finally:
        conn.close()
    if not candidate:
        _atomic_json(state_path, {"schema": "preselection_retention_cursor_v1",
                                  "cutoff": cutoff, "cursor": end_cursor})
        return {"state": "no_candidates", "scanned": scanned,
                "cursor": end_cursor, "continue": not first_ineligible and scanned == MAX_SCANNED_ROWS}
    payload = b"".join(_row_payload(columns, row) for row in candidate)
    digest = hashlib.sha256(payload).hexdigest()
    archive_file = external / f"{TABLE}_{candidate[0][0]}_{candidate[-1][0]}.jsonl.gz"
    temporary = archive_file.with_suffix(".partial")
    try:
        if not archive_file.exists():
            with open(temporary, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as stream:
                    stream.write(payload)
                raw.flush()
                os.fsync(raw.fileno())
            legacy._verify_export_member(temporary, 0, digest, len(payload))
            os.replace(temporary, archive_file)
            directory_fd = os.open(external, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        legacy._verify_export_member(archive_file, 0, digest, len(payload))
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return {"state": "external_unavailable", "cursor": cursor}
    receipt = {
        "schema": "preselection_retention_pending_v1",
        "database": str(db.resolve()), "cutoff": cutoff,
        "decision_highwater": highwater,
        "archive": str(archive_file.resolve()), "archive_offset": 0,
        "payload_sha256": digest, "payload_bytes": len(payload),
        "rowids": [int(row[0]) for row in candidate],
        "row_sha256": [hashlib.sha256(_row_payload(columns, row)).hexdigest() for row in candidate],
        "plan_ids": [item[0] for item in candidate_meta],
        "plan_scopes": [[item[1], item[2], item[3]] for item in candidate_meta],
    }
    receipt_path = (pending_path if not pending_path.exists() else pending_path.with_name(
        f"{pending_path.stem}_{candidate[0][0]}_{candidate[-1][0]}.json"
    ))
    if receipt_path.exists():
        prior = _read_json(receipt_path)
        if (prior.get("payload_sha256") != digest
                or prior.get("rowids") != receipt["rowids"]):
            raise RuntimeError("preselection_retention_pending_conflict")
    else:
        _atomic_json(receipt_path, receipt)
    _atomic_json(state_path, {"schema": "preselection_retention_cursor_v1",
                              "cutoff": cutoff, "cursor": end_cursor})
    return {"state": "archived", "rows": len(candidate),
            "payload_bytes": len(payload), "cursor": end_cursor, "continue": True}


def apply_once(db: Path = DB, pending_path: Path = PENDING) -> dict:
    assert_process_memory_budget(limit=MAX_PROCESS_RSS_BYTES)
    queued = _pending_paths(pending_path)
    if not queued:
        return {"state": "nothing_pending"}
    pending_path = queued[0]
    receipt = _read_json(pending_path)
    if receipt.get("schema") != "preselection_retention_pending_v1" or str(db.resolve()) != receipt.get("database"):
        raise RuntimeError("preselection_retention_receipt_invalid")
    if not 0 < int(receipt["payload_bytes"]) <= MAX_BATCH_BYTES:
        raise RuntimeError("preselection_retention_payload_out_of_bounds")
    archive = Path(receipt["archive"])
    if not _external_available(db, archive.parent) or not archive.is_file():
        return {"state": "external_unavailable"}
    try:
        legacy._verify_export_member(
            archive, 0, str(receipt["payload_sha256"]), int(receipt["payload_bytes"])
        )
    except OSError:
        return {"state": "external_unavailable"}
    rowids = [int(value) for value in receipt["rowids"]]
    hashes = list(receipt["row_sha256"])
    plan_ids = list(receipt["plan_ids"])
    plan_scopes = [tuple(scope) for scope in receipt["plan_scopes"]]
    ids = set(plan_ids)
    scopes = set(plan_scopes)
    if (not rowids or len(rowids) > MAX_BATCH_ROWS
            or len(set(rowids)) != len(rowids) or rowids != sorted(rowids)
            or any(len(scope) != 3 for scope in plan_scopes)
            or not len(rowids) == len(hashes) == len(plan_ids) == len(plan_scopes)):
        raise RuntimeError("preselection_retention_batch_invalid")
    conn = sqlite3.connect(db, timeout=30, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        if int(conn.execute("SELECT COALESCE(MAX(rowid),0) FROM auto_trade_decisions").fetchone()[0]) < int(receipt["decision_highwater"]):
            raise RuntimeError("preselection_retention_decision_history_reversed")
        for station, day, raw in conn.execute(
            "SELECT station_id,event_date,details_json FROM auto_trade_decisions WHERE rowid>?",
            (int(receipt["decision_highwater"]),),
        ):
            try:
                details = json.loads(raw or "{}")
            except (TypeError, ValueError) as exc:
                raise RuntimeError("preselection_retention_new_decision_unreadable") from exc
            if not isinstance(details, dict):
                raise RuntimeError("preselection_retention_new_decision_invalid")
            if str(details.get("preselection_id") or "") in ids or (
                str(station), str(day), str(details.get("production_plan_sha256") or "")
            ) in scopes:
                raise RuntimeError("preselection_retention_new_reference")
        columns = legacy._columns(conn, TABLE)
        id_idx = columns.index("preselection_id") + 1
        station_idx = columns.index("station_id") + 1
        day_idx = columns.index("event_date") + 1
        plan_idx = columns.index("plan_json") + 1
        triggers = legacy._delete_triggers(conn, TABLE)
        present: list[int] = []
        for rid, digest, plan_id, scope in zip(rowids, hashes, plan_ids, plan_scopes):
            row = conn.execute(f"SELECT rowid,* FROM {TABLE} WHERE rowid=?", (rid,)).fetchone()
            if row is None:
                continue  # A previous attempt committed, then died before its receipt was retired.
            if hashlib.sha256(_row_payload(columns, row)).hexdigest() != digest:
                raise RuntimeError("preselection_retention_row_changed")
            try:
                actual_scope = (
                    str(row[station_idx]), str(row[day_idx]),
                    str(json.loads(row[plan_idx])["compiled_plan"]["production_plan_sha256"]),
                )
            except (TypeError, ValueError, KeyError) as exc:
                raise RuntimeError("preselection_retention_plan_unreadable") from exc
            if str(row[id_idx]) != str(plan_id) or actual_scope != scope:
                raise RuntimeError("preselection_retention_receipt_identity_changed")
            present.append(rid)
        stations = {scope[0] for scope in scopes}
        for station in stations:
            latest = conn.execute(
                f"SELECT MAX(rowid) FROM {TABLE} WHERE station_id=?", (station,)
            ).fetchone()[0]
            if latest is not None and int(latest) in present:
                raise RuntimeError("preselection_retention_latest_plan_protected")
        for name, _sql in triggers:
            conn.execute(f'DROP TRIGGER "{name}"')
        for start in range(0, len(present), 200):
            part = present[start:start + 200]
            conn.execute(f"DELETE FROM {TABLE} WHERE rowid IN ({','.join('?' * len(part))})", part)
        for _name, sql in triggers:
            conn.execute(sql)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    applied = pending_path.with_name(f"preselection_retention_applied_{rowids[0]}_{rowids[-1]}.json")
    _atomic_json(applied, {**receipt, "state": "applied", "deleted_rows": len(present)})
    pending_path.unlink()
    return {"state": "applied", "deleted_rows": len(present), "continue": True}


def apply_turn(db: Path = DB, pending_path: Path = PENDING) -> dict:
    """Bound one hosted writer turn; each batch commits and receipts separately."""
    started = time.monotonic()
    batches = 0
    rows = 0
    while batches < MAX_APPLY_BATCHES_PER_TURN:
        if batches and time.monotonic() - started >= MAX_APPLY_TURN_SECONDS:
            break
        result = apply_once(db, pending_path)
        if result["state"] != "applied":
            if batches:
                return {"state": "applied", "batches": batches, "deleted_rows": rows,
                        "continue": False}
            return result
        batches += 1
        rows += int(result["deleted_rows"])
    return {"state": "applied", "batches": batches, "deleted_rows": rows,
            "continue": bool(_pending_paths(pending_path))}


def restore_archived_batch(db: Path, applied_receipt: Path) -> dict:
    """Manual recovery from a verified archive; never called by a resident."""
    receipt = _read_json(applied_receipt)
    if receipt.get("schema") != "preselection_retention_pending_v1" or receipt.get("state") != "applied":
        raise RuntimeError("preselection_retention_restore_receipt_invalid")
    archive = Path(receipt["archive"])
    legacy._verify_export_member(
        archive, 0, str(receipt["payload_sha256"]), int(receipt["payload_bytes"])
    )
    if int(receipt["payload_bytes"]) > MAX_BATCH_BYTES + 1024 * 1024:
        raise RuntimeError("preselection_retention_restore_payload_too_large")
    with gzip.open(archive, "rt", encoding="utf-8") as stream:
        documents = [json.loads(line) for line in stream]
    if [int(doc["rowid"]) for doc in documents] != [int(rid) for rid in receipt["rowids"]]:
        raise RuntimeError("preselection_retention_restore_rowids_changed")
    batch = [
        (int(doc["rowid"]), {key: legacy._decode(value) for key, value in doc["row"].items()})
        for doc in documents
    ]
    conn = sqlite3.connect(db, timeout=30, isolation_level=None)
    try:
        for start in range(0, len(batch), 200):
            legacy._insert(conn, TABLE, batch[start:start + 200])
    finally:
        conn.close()
    return {"state": "restored", "rows": len(batch)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("archive", "apply"))
    args = parser.parse_args(argv)
    result = archive_once() if args.mode == "archive" else apply_turn()
    print(json.dumps(result, sort_keys=True))
    if result.get("continue"):
        print(CONTINUE_MARKER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
