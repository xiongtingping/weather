#!/usr/bin/env python3
"""Durable, observation-keyed queue for deferred AMSC probability updates.

The raw archive remains authoritative.  This queue only retains the earliest
frozen causal plan for one primary AMSC observation and audits duplicate plans
without touching archive.db or any order path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_successor_activation_contract import activation_sha256  # noqa: E402


COMPACT_CONFIRM = "COMPACT_AMSC_PROBABILITY_PENDING_QUEUE"
EXPIRE_CONFIRM = "EXPIRE_NONCURRENT_AMSC_PROBABILITY_PLAN"
ARCHIVE_CONSUMED_CONFIRM = "ARCHIVE_CONSUMED_AMSC_PROBABILITY_PLANS"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def observation_identity(plan: dict[str, Any]) -> dict[str, Any]:
    effective = plan.get("effective_payload") or {}
    features = effective.get("weather_likelihood_features") or {}
    event_date = str(plan.get("event_date") or "unknown-date")[:10]
    metar_at = str(features.get("zuuu_metar_raw_amsc_latest_at") or plan.get("fact_as_of") or "")
    source_observations = effective.get("source_observations") or plan.get("source_observations") or []
    primary = sorted(
        {
            (
                str(row.get("source_observation_id") or ""),
                str(row.get("raw_sha256") or (row.get("payload") or {}).get("raw_sha256") or ""),
                str(row.get("valid_time") or ""),
            )
            for row in source_observations
            if isinstance(row, dict)
            and str(row.get("source_id") or "") == "zuuu_metar_raw_amsc"
            and str(row.get("event_date") or "")[:10] == event_date
            and str(row.get("valid_time") or "") == metar_at
        }
    )
    payload: dict[str, Any] = {
        "schema": "amsc_probability_observation_identity_v1",
        "event_date": event_date,
        "metar_at": metar_at,
    }
    if primary:
        payload["primary_observations"] = primary
        source = "primary_source_observation"
    else:
        latest_raw = str(features.get("zuuu_metar_raw_amsc_latest_raw") or "")
        payload["latest_raw_sha256"] = hashlib.sha256(latest_raw.encode("utf-8")).hexdigest()
        source = "latest_raw_compatibility_fallback"
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return {
        "payload": payload,
        "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20],
        "source": source,
        "event_date": event_date,
        "metar_at": metar_at,
    }


def enqueue(plan_path: Path, result_path: Path, pending_dir: Path) -> Path:
    plan_bytes = plan_path.read_bytes()
    plan = json.loads(plan_bytes.decode("utf-8"))
    identity = observation_identity(plan)
    safe_metar = re.sub(r"[^0-9A-Za-z]+", "", identity["metar_at"]) or "unknown"
    pending_dir.mkdir(parents=True, exist_ok=True)
    pending_path = pending_dir / (
        f"{identity['event_date']}_{safe_metar}_{identity['digest']}.plan.json"
    )
    try:
        descriptor = os.open(pending_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        created = False
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(plan_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        created = True

    result = _read_json(result_path) if result_path.exists() else {}
    receipt_path = pending_path.with_suffix(".deferred.json")
    receipt = _read_json(receipt_path) if receipt_path.exists() else {}
    now = _utc_now()
    if receipt:
        duplicate_count = int(receipt.get("duplicate_deferral_count") or 0) + 1
        queued_at = str(receipt.get("queued_at") or now)
    else:
        duplicate_count = 0
        queued_at = now
    receipt.update(
        {
            "schema": "amsc_probability_fast_lane_deferral_v2",
            "state": "pending_replay",
            "event_date": identity["event_date"],
            "metar_at": identity["metar_at"],
            "queued_at": queued_at,
            "last_deferred_at": now,
            "duplicate_deferral_count": duplicate_count,
            "reason": "canonical_or_probability_fast_lane_not_committed",
            "observation_identity_digest": identity["digest"],
            "observation_identity_source": identity["source"],
            "pending_plan_path": str(pending_path),
            "raw_archive_remains_authoritative": True,
            "earliest_frozen_plan_preserved": True,
            "recovery_policy": "canonical_then_sequential_posterior_no_direct_order",
            "last_result": result,
            "plan_created_this_attempt": created,
        }
    )
    _atomic_json(receipt_path, receipt)
    return pending_path


def _causal_key(path: Path, plan: dict[str, Any]) -> tuple[str, str, int, str]:
    receipt = _read_json(path.with_suffix(".deferred.json"))
    return (
        str(receipt.get("queued_at") or "9999"),
        str(plan.get("fact_as_of") or "9999"),
        path.stat().st_mtime_ns,
        path.name,
    )


def compact(pending_dir: Path, *, apply: bool, confirm: str) -> dict[str, Any]:
    if apply and confirm != COMPACT_CONFIRM:
        raise ValueError(f"apply_requires_confirm:{COMPACT_CONFIRM}")
    groups: dict[str, list[tuple[Path, dict[str, Any], dict[str, Any]]]] = {}
    invalid: list[str] = []
    for path in sorted(pending_dir.glob("*.plan.json")) if pending_dir.exists() else []:
        plan = _read_json(path)
        if not plan:
            invalid.append(str(path))
            continue
        identity = observation_identity(plan)
        groups.setdefault(identity["digest"], []).append((path, plan, identity))

    actions: list[dict[str, Any]] = []
    for digest, rows in sorted(groups.items()):
        ordered = sorted(rows, key=lambda row: _causal_key(row[0], row[1]))
        keeper, _, identity = ordered[0]
        duplicates = ordered[1:]
        if not duplicates:
            continue
        action: dict[str, Any] = {
            "observation_identity_digest": digest,
            "event_date": identity["event_date"],
            "metar_at": identity["metar_at"],
            "keeper": str(keeper),
            "duplicate_plan_count": len(duplicates),
            "duplicates": [str(row[0]) for row in duplicates],
            "applied": apply,
        }
        if apply:
            destination_dir = pending_dir / "superseded" / digest
            destination_dir.mkdir(parents=True, exist_ok=True)
            moved: list[dict[str, str]] = []
            for duplicate, _, _ in duplicates:
                base = duplicate.name[: -len(".plan.json")]
                for related in sorted(pending_dir.glob(f"{base}*")):
                    if not related.is_file():
                        continue
                    destination = destination_dir / related.name
                    if destination.exists():
                        if destination.read_bytes() != related.read_bytes():
                            destination = destination_dir / f"{related.name}.conflict.{os.getpid()}"
                        else:
                            related.unlink()
                            moved.append({"from": str(related), "to": str(destination)})
                            continue
                    shutil.move(str(related), str(destination))
                    moved.append({"from": str(related), "to": str(destination)})
            keeper_receipt_path = keeper.with_suffix(".deferred.json")
            keeper_receipt = _read_json(keeper_receipt_path)
            keeper_receipt.update(
                {
                    "schema": "amsc_probability_fast_lane_deferral_v2",
                    "state": "pending_replay",
                    "observation_identity_digest": digest,
                    "pending_plan_path": str(keeper),
                    "earliest_frozen_plan_preserved": True,
                    "compacted_duplicate_plan_count": int(
                        keeper_receipt.get("compacted_duplicate_plan_count") or 0
                    )
                    + len(duplicates),
                    "last_compacted_at": _utc_now(),
                }
            )
            _atomic_json(keeper_receipt_path, keeper_receipt)
            compaction_receipt = {
                "schema": "amsc_probability_pending_compaction_receipt_v1",
                "compacted_at": _utc_now(),
                "observation_identity": identity["payload"],
                "observation_identity_digest": digest,
                "keeper": str(keeper),
                "moved": moved,
                "raw_archive_remains_authoritative": True,
                "writes_archive_db": False,
                "places_orders": False,
            }
            _atomic_json(destination_dir / "compaction_receipt.json", compaction_receipt)
            action["moved_file_count"] = len(moved)
        actions.append(action)

    return {
        "schema": "amsc_probability_pending_queue_compaction_v1",
        "ok": not invalid,
        "state": "compacted" if apply else "read_only_plan",
        "pending_dir": str(pending_dir),
        "plan_count": sum(len(rows) for rows in groups.values()),
        "observation_identity_count": len(groups),
        "duplicate_plan_count": sum(max(0, len(rows) - 1) for rows in groups.values()),
        "invalid_plans": invalid,
        "actions": actions,
        "writes_archive_db": False,
        "places_orders": False,
    }


def expire_noncurrent(
    plan_path: Path,
    *,
    current_event_date: str,
    apply: bool,
    confirm: str,
) -> dict[str, Any]:
    """Archive a prior station-day plan without replaying it as live evidence."""

    if apply and confirm != EXPIRE_CONFIRM:
        raise ValueError(f"apply_requires_confirm:{EXPIRE_CONFIRM}")
    plan = _read_json(plan_path)
    if not plan:
        return {
            "schema": "amsc_probability_pending_expiry_v1",
            "ok": False,
            "state": "invalid_plan",
            "plan_path": str(plan_path),
            "writes_archive_db": False,
            "places_orders": False,
        }
    identity = observation_identity(plan)
    event_date = str(identity["event_date"] or "")[:10]
    current = str(current_event_date or "")[:10]
    if not current or event_date >= current:
        return {
            "schema": "amsc_probability_pending_expiry_v1",
            "ok": True,
            "state": "current_or_future_plan_retained",
            "event_date": event_date,
            "current_event_date": current,
            "plan_path": str(plan_path),
            "writes_archive_db": False,
            "places_orders": False,
        }

    destination_dir = plan_path.parent / "expired" / event_date / identity["digest"]
    moved: list[dict[str, str]] = []
    if apply:
        destination_dir.mkdir(parents=True, exist_ok=True)
        base = plan_path.name[: -len(".plan.json")]
        for related in sorted(plan_path.parent.glob(f"{base}*")):
            if not related.is_file():
                continue
            destination = destination_dir / related.name
            if destination.exists():
                if destination.read_bytes() == related.read_bytes():
                    related.unlink()
                else:
                    destination = destination_dir / f"{related.name}.conflict.{os.getpid()}"
                    shutil.move(str(related), str(destination))
            else:
                shutil.move(str(related), str(destination))
            moved.append({"from": str(related), "to": str(destination)})
        _atomic_json(
            destination_dir / "expiry_receipt.json",
            {
                "schema": "amsc_probability_pending_expiry_receipt_v1",
                "state": "expired_noncurrent_physical_station_day",
                "expired_at": _utc_now(),
                "event_date": event_date,
                "current_event_date": current,
                "observation_identity": identity["payload"],
                "observation_identity_digest": identity["digest"],
                "reason": "noncurrent_physical_station_day_never_replayed_as_live",
                "raw_archive_remains_authoritative": True,
                "writes_archive_db": False,
                "writes_posterior": False,
                "places_orders": False,
                "moved": moved,
            },
        )
    return {
        "schema": "amsc_probability_pending_expiry_v1",
        "ok": True,
        "state": "expired_noncurrent_physical_station_day",
        "applied": apply,
        "event_date": event_date,
        "current_event_date": current,
        "plan_path": str(plan_path),
        "destination_dir": str(destination_dir),
        "moved_file_count": len(moved),
        "raw_archive_remains_authoritative": True,
        "writes_archive_db": False,
        "writes_posterior": False,
        "places_orders": False,
    }


def archive_consumed(
    pending_dir: Path,
    *,
    db: Path,
    activation_path: Path,
    event_date: str,
    apply: bool,
    confirm: str,
) -> dict[str, Any]:
    """Archive plans already represented by the active checkpoint sequence.

    The database is opened read-only.  Raw archives and canonical facts remain
    authoritative; this only removes redundant filesystem retries after a new
    activation generation has replayed the same observation.
    """

    if apply and confirm != ARCHIVE_CONSUMED_CONFIRM:
        raise ValueError(f"apply_requires_confirm:{ARCHIVE_CONSUMED_CONFIRM}")
    day = str(event_date or "")[:10]
    try:
        activation = _read_json(activation_path)
        activation_sha = activation_sha256(activation)
        target = f"physical_weather_day_v1:ZUUU:{day}"
        with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as conn:
            lineage_rows = conn.execute(
                """SELECT p.replay_generation
                   FROM production_initial_posterior_lineage l
                   JOIN posterior_checkpoints p
                     ON p.posterior_checkpoint_id=l.posterior_checkpoint_id
                   WHERE l.target_identity=? AND l.activation_contract_sha256=?""",
                (target, activation_sha),
            ).fetchall()
            if len(lineage_rows) != 1:
                raise ValueError("active_successor_p0_lineage_not_unique")
            generation = int(lineage_rows[0][0] or 0)
            checkpoint = conn.execute(
                """SELECT posterior_checkpoint_id,consumed_event_ids_json
                   FROM posterior_checkpoints
                   WHERE target_identity=? AND replay_generation=? AND state='committed'
                   ORDER BY committed_at DESC,rowid DESC LIMIT 1""",
                (target, generation),
            ).fetchone()
            if checkpoint is None:
                raise ValueError("active_reported_x_checkpoint_missing")
            consumed_raw = json.loads(str(checkpoint[1] or "[]"))
            consumed_ids = {
                str(value) for value in consumed_raw if str(value or "").strip()
            }
            actions: list[dict[str, Any]] = []
            retained: list[dict[str, Any]] = []
            for plan_path in sorted(pending_dir.glob("*.plan.json")) if pending_dir.exists() else []:
                plan = _read_json(plan_path)
                identity = observation_identity(plan) if plan else {}
                if not plan or str(identity.get("event_date") or "")[:10] != day:
                    retained.append(
                        {
                            "plan_path": str(plan_path),
                            "reason": "invalid_or_other_event_date",
                        }
                    )
                    continue
                primary_rows = (identity.get("payload") or {}).get("primary_observations") or []
                primary_ids = [
                    str(row[0])
                    for row in primary_rows
                    if isinstance(row, (list, tuple)) and row and str(row[0] or "")
                ]
                if not primary_ids:
                    retained.append(
                        {
                            "plan_path": str(plan_path),
                            "reason": "primary_source_observation_identity_missing",
                        }
                    )
                    continue
                params: list[Any] = [target, str(identity.get("metar_at") or "")]
                source_filter = (
                    " AND selected_source_observation_id IN ("
                    + ",".join("?" for _ in primary_ids)
                    + ")"
                )
                params.extend(primary_ids)
                canonical_rows = conn.execute(
                    """SELECT canonical_observation_event_id
                       FROM canonical_observation_events
                       WHERE target_identity=? AND valid_time=?
                         AND station_id='ZUUU'
                         AND selected_source_id='zuuu_metar_raw_amsc'
                         AND state IN ('accepted_primary','accepted_substitute','confirmed')"""
                    + source_filter,
                    params,
                ).fetchall()
                canonical_ids = [str(row[0]) for row in canonical_rows]
                matched_consumed = [value for value in canonical_ids if value in consumed_ids]
                if not matched_consumed:
                    retained.append(
                        {
                            "plan_path": str(plan_path),
                            "reason": "canonical_event_not_consumed_by_active_checkpoint",
                            "canonical_event_ids": canonical_ids,
                        }
                    )
                    continue
                destination_dir = pending_dir / "consumed" / day / str(identity["digest"])
                moved: list[dict[str, str]] = []
                if apply:
                    destination_dir.mkdir(parents=True, exist_ok=True)
                    base = plan_path.name[: -len(".plan.json")]
                    for related in sorted(pending_dir.glob(f"{base}*")):
                        if not related.is_file():
                            continue
                        destination = destination_dir / related.name
                        if destination.exists() and destination.read_bytes() == related.read_bytes():
                            related.unlink()
                        elif destination.exists():
                            destination = destination_dir / f"{related.name}.conflict.{os.getpid()}"
                            shutil.move(str(related), str(destination))
                        else:
                            shutil.move(str(related), str(destination))
                        moved.append({"from": str(related), "to": str(destination)})
                    _atomic_json(
                        destination_dir / "consumed_checkpoint_receipt.json",
                        {
                            "schema": "amsc_probability_pending_consumed_receipt_v1",
                            "state": "archived_consumed_by_active_checkpoint",
                            "archived_at": _utc_now(),
                            "activation_contract_sha256": activation_sha,
                            "posterior_checkpoint_id": str(checkpoint[0]),
                            "canonical_observation_event_ids": matched_consumed,
                            "observation_identity": identity["payload"],
                            "observation_identity_digest": identity["digest"],
                            "raw_archive_remains_authoritative": True,
                            "writes_archive_db": False,
                            "writes_posterior": False,
                            "places_orders": False,
                            "moved": moved,
                        },
                    )
                actions.append(
                    {
                        "plan_path": str(plan_path),
                        "canonical_observation_event_ids": matched_consumed,
                        "destination_dir": str(destination_dir),
                        "moved_file_count": len(moved),
                        "applied": apply,
                    }
                )
    except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "schema": "amsc_probability_pending_consumed_archive_v1",
            "ok": False,
            "state": "blocked",
            "block_reason": str(exc),
            "pending_dir": str(pending_dir),
            "read_only": not apply,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        }
    return {
        "schema": "amsc_probability_pending_consumed_archive_v1",
        "ok": True,
        "state": "archived" if apply else "read_only_plan",
        "event_date": day,
        "activation_contract_sha256": activation_sha,
        "posterior_checkpoint_id": str(checkpoint[0]),
        "consumed_event_count": len(consumed_ids),
        "archived_plan_count": len(actions),
        "retained_plan_count": len(retained),
        "actions": actions,
        "retained": retained,
        "read_only": not apply,
        "writes_archive_db": False,
        "writes_posterior": False,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    enqueue_parser = subparsers.add_parser("enqueue")
    enqueue_parser.add_argument("--plan", required=True)
    enqueue_parser.add_argument("--result", required=True)
    enqueue_parser.add_argument("--pending-dir", required=True)
    compact_parser = subparsers.add_parser("compact")
    compact_parser.add_argument("--pending-dir", required=True)
    compact_parser.add_argument("--apply", action="store_true")
    compact_parser.add_argument("--confirm", default="")
    expire_parser = subparsers.add_parser("expire-noncurrent")
    expire_parser.add_argument("--plan", required=True)
    expire_parser.add_argument("--current-event-date", required=True)
    expire_parser.add_argument("--apply", action="store_true")
    expire_parser.add_argument("--confirm", default="")
    consumed_parser = subparsers.add_parser("archive-consumed")
    consumed_parser.add_argument("--pending-dir", required=True)
    consumed_parser.add_argument("--db", required=True)
    consumed_parser.add_argument("--activation", required=True)
    consumed_parser.add_argument("--event-date", required=True)
    consumed_parser.add_argument("--apply", action="store_true")
    consumed_parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.command == "enqueue":
        print(enqueue(Path(args.plan), Path(args.result), Path(args.pending_dir)))
        return 0
    try:
        if args.command == "compact":
            result = compact(Path(args.pending_dir), apply=args.apply, confirm=args.confirm)
        elif args.command == "archive-consumed":
            result = archive_consumed(
                Path(args.pending_dir),
                db=Path(args.db),
                activation_path=Path(args.activation),
                event_date=args.event_date,
                apply=args.apply,
                confirm=args.confirm,
            )
        else:
            result = expire_noncurrent(
                Path(args.plan),
                current_event_date=args.current_event_date,
                apply=args.apply,
                confirm=args.confirm,
            )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
