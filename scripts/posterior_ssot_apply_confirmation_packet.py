#!/usr/bin/env python3
"""Build a read-only confirmation packet for posterior SSOT fact repair.

The packet is intentionally no-apply. It identifies persisted fact rows whose
current payload is out of contract, shows how the canonical fact writer would
repair the payload, and lists the operator preflight needed before any future
controlled apply.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
for import_path in (ROOT, SCRIPT_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

import posterior_ssot_fact_coverage_audit as coverage_audit
import posterior_ssot_rematerialization_plan as remat_plan
import weather_fact_snapshot_writer as fact_writer


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "posterior_ssot_apply_confirmation_packet_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"
CONFIRMATION_PHRASE = "APPLY_POSTERIOR_SSOT_FACT_REPAIR"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_stamp(value: str) -> str:
    return str(value or "").replace("-", "").replace(":", "").replace(".", "").replace("+00:00", "Z")


def _q(value: Path | str) -> str:
    return shlex.quote(str(value))


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_fact_ids(value: str | Sequence[Any] | None = None) -> list[int]:
    values: Sequence[Any]
    if value is None:
        values = []
    elif isinstance(value, str):
        values = value.replace(";", ",").split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        values = [value]
    out: list[int] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            fact_id = int(text)
        except Exception:
            continue
        if fact_id > 0 and fact_id not in out:
            out.append(fact_id)
    return out


def _prob(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if out == out else None


def _bucket_label(row: Mapping[str, Any]) -> str:
    return str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()


def _surface_probability_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def append(path: str, bucket: str, probability: Any, field: str) -> None:
        label = str(bucket or "").strip()
        prob = _prob(probability)
        if not label or prob is None:
            return
        rows.append(
            {
                "path": path,
                "bucket": label,
                "probability": float(prob),
                "posterior_field": field,
            }
        )

    posterior_distribution = payload.get("posterior_temperature_distribution")
    if isinstance(posterior_distribution, list):
        for index, row in enumerate(posterior_distribution):
            row_map = _as_mapping(row)
            append(
                f"posterior_temperature_distribution[{index}]",
                _bucket_label(row_map),
                row_map.get("probability")
                if row_map.get("probability") is not None
                else row_map.get("posterior")
                if row_map.get("posterior") is not None
                else row_map.get("q_temperature_bucket_posterior"),
                str(row_map.get("posterior_field") or ""),
            )
    elif isinstance(posterior_distribution, Mapping):
        bucket_rows = posterior_distribution.get("bucket_probabilities")
        if isinstance(bucket_rows, Sequence) and not isinstance(bucket_rows, (str, bytes, bytearray)):
            for index, row in enumerate(bucket_rows):
                row_map = _as_mapping(row)
                append(
                    f"posterior_temperature_distribution.bucket_probabilities[{index}]",
                    _bucket_label(row_map),
                    row_map.get("probability")
                    if row_map.get("probability") is not None
                    else row_map.get("posterior")
                    if row_map.get("posterior") is not None
                    else row_map.get("q_temperature_bucket_posterior"),
                    str(row_map.get("posterior_field") or ""),
                )
    distribution_map = payload.get("posterior_temperature_distribution_map")
    if isinstance(distribution_map, Mapping):
        for bucket, probability in distribution_map.items():
            if str(bucket).endswith("__pre_posterior_ssot_fact_alignment"):
                continue
            append("posterior_temperature_distribution_map", str(bucket), probability, "posterior_temperature_distribution_map")
    distribution = payload.get("distribution")
    if isinstance(distribution, Sequence) and not isinstance(distribution, (str, bytes, bytearray)):
        for index, row in enumerate(distribution):
            row_map = _as_mapping(row)
            append(
                f"distribution[{index}]",
                _bucket_label(row_map),
                row_map.get("q_temperature_bucket_posterior")
                if row_map.get("q_temperature_bucket_posterior") is not None
                else row_map.get("probability"),
                str(row_map.get("posterior_field") or ""),
            )
    return rows


def _surface_diff(before: Mapping[str, Any], after: Mapping[str, Any], *, max_rows: int = 30) -> list[dict[str, Any]]:
    after_by_key = {
        (str(row.get("path") or ""), str(row.get("bucket") or "")): row
        for row in _surface_probability_rows(after)
    }
    diffs: list[dict[str, Any]] = []
    for before_row in _surface_probability_rows(before):
        key = (str(before_row.get("path") or ""), str(before_row.get("bucket") or ""))
        after_row = after_by_key.get(key)
        if not after_row:
            continue
        before_prob = _prob(before_row.get("probability"))
        after_prob = _prob(after_row.get("probability"))
        if before_prob is None or after_prob is None:
            continue
        if abs(float(before_prob) - float(after_prob)) <= 1e-12:
            continue
        diffs.append(
            {
                "path": key[0],
                "bucket": key[1],
                "before_probability": float(before_prob),
                "after_probability": float(after_prob),
                "delta": float(after_prob) - float(before_prob),
                "before_posterior_field": str(before_row.get("posterior_field") or ""),
                "after_posterior_field": str(after_row.get("posterior_field") or ""),
            }
        )
    return diffs[: max(0, int(max_rows))]


def _load_fact_row(conn: sqlite3.Connection, fact_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        FROM event_fact_history
        WHERE id = ?
        """,
        (int(fact_id),),
    ).fetchone()


def _writer_apply_projection(row: sqlite3.Row, payload: Mapping[str, Any], *, db_path: Path) -> dict[str, Any]:
    from posterior_ssot_projection_runtime import build_fact_projection_no_apply

    slug = str(payload.get("slug") or row["slug"] or "")
    event_date = str(payload.get("event_date") or row["event_date"] or "")
    run_date = str(payload.get("run_date") or row["run_date"] or event_date)
    fact_as_of = str(payload.get("fact_as_of") or row["fact_as_of"] or "")
    projection = build_fact_projection_no_apply(
        slug=slug,
        event_date=event_date,
        run_date=run_date,
        fact_as_of=fact_as_of,
        payload=payload,
        current_fact_id=int(row["id"] or 0),
        current_fact_as_of=str(row["fact_as_of"] or ""),
        composer_version=str(row["composer_version"] or fact_writer.PARSER_VERSION),
        write_reason="posterior_ssot_apply_confirmation_packet",
        db_path=db_path,
        enable_apply_contract=True,
        write_latest=True,
    )
    plan = _as_mapping(projection.get("plan"))
    projected_payload = plan.get("effective_payload") if isinstance(plan.get("effective_payload"), Mapping) else {}
    projected_audit = (
        coverage_audit.audit_fact_payload(
            projected_payload,
            meta={
                "id": row["id"],
                "slug": row["slug"],
                "event_date": row["event_date"],
                "fact_as_of": row["fact_as_of"],
                "composer_version": row["composer_version"],
            },
        )
        if projected_payload
        else {"ok": False, "issues": ["writer_projection_payload_missing"]}
    )
    return {
        "projection": projection,
        "plan": plan,
        "projected_payload": projected_payload,
        "projected_audit": projected_audit,
    }


def _plan_actions(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for action in plan.get("plan_actions") or []:
        item = _as_mapping(action)
        if not item:
            continue
        actions.append(
            {
                "action": str(item.get("action") or ""),
                "target_table": str(item.get("target_table") or ""),
                "would_write": bool(item.get("would_write")),
                "apply_status": str(item.get("apply_status") or ""),
                "block_reason": str(item.get("block_reason") or ""),
            }
        )
    return actions


def _backup_path(db: Path, backup_dir: Path, generated_at: str) -> Path:
    return backup_dir / f"{db.name}.posterior-ssot-repair-{_safe_stamp(generated_at)}.bak"


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_event_fact_summary_cache": False,
        "writes_auto_trade_decisions": False,
        "writes_daily_weather_reviews": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "restarts_launchd": False,
        "executes_sql_writes": False,
    }


def _build_final_lock_route(
    db: Path,
    *,
    slug: str,
    event_date: str,
    is_latest_fact: bool,
    backup_dir: Path,
) -> dict[str, Any]:
    if not is_latest_fact:
        return {
            "available": False,
            "reason": "candidate_not_current_latest_final_lock_promote_not_allowed",
            "scanned": 0,
            "eligible_count": 0,
        }
    try:
        import promote_final_bucket_lock_facts as promote

        plan = promote.build_plan(db, slug=slug, event_date=event_date, limit=10)
    except Exception as exc:  # pragma: no cover - defensive operator packet guard
        return {
            "available": False,
            "reason": "final_bucket_lock_route_probe_failed",
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    eligible = [
        item
        for item in plan.get("candidates") or []
        if str(item.get("slug") or "") == slug
        and str(item.get("event_date") or "") == event_date
        and bool(item.get("eligible"))
    ]
    if not eligible:
        return {
            "available": False,
            "reason": "no_final_bucket_lock_promote_candidate",
            "scanned": plan.get("scanned"),
            "eligible_count": plan.get("eligible_count"),
        }
    return {
        "available": True,
        "reason": "existing_final_bucket_lock_promote_apply_entrypoint_available",
        "confirm_phrase": promote.CONFIRM_PHRASE,
        "candidate_count": len(eligible),
        "apply_command": (
            f"python3 scripts/promote_final_bucket_lock_facts.py --db {_q(db)} "
            f"--slug {_q(slug)} --event-date {_q(event_date)} --limit 10 "
            f"--backup-dir {_q(backup_dir)} --apply --confirm-apply {promote.CONFIRM_PHRASE} --json"
        ),
    }


def _build_generic_posterior_repair_route(
    db: Path,
    *,
    fact_id: int,
    slug: str,
    event_date: str,
    is_latest_fact: bool,
    backup_dir: Path,
) -> dict[str, Any]:
    if not is_latest_fact:
        return {
            "available": False,
            "reason": "candidate_not_current_latest_apply_entrypoint_not_allowed",
            "confirm_phrase": CONFIRMATION_PHRASE,
            "fact_id": int(fact_id or 0),
            "apply_command": "",
        }
    return {
        "available": True,
        "reason": "generic_posterior_ssot_projection_apply_entrypoint_available",
        "confirm_phrase": CONFIRMATION_PHRASE,
        "fact_id": int(fact_id or 0),
        "apply_command": (
            f"python3 scripts/apply_posterior_ssot_fact_repair.py --db {_q(db)} "
            f"--fact-id {int(fact_id or 0)} --slug {_q(slug)} --event-date {_q(event_date)} "
            f"--backup-dir {_q(backup_dir)} --apply --confirm {CONFIRMATION_PHRASE} "
            f"--writer-quiescent --json"
        ),
    }


def build_confirmation_packet(
    db_path: Path | str = DEFAULT_DB,
    *,
    fact_id: str = "",
    fact_ids: str | Sequence[int] | None = None,
    slug: str = "",
    event_date: str = "",
    since_hours: float = 168.0,
    limit: int = 100,
    latest_only: bool = True,
    max_items_output: int = 20,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated = generated_at or _now_utc()
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    backup = _backup_path(db, backup_root, generated)
    explicit_fact_ids = _parse_fact_ids([*_parse_fact_ids(fact_id), *_parse_fact_ids(fact_ids)])
    effective_latest_only = bool(latest_only) and not explicit_fact_ids
    plan = remat_plan.build_plan(
        db_path=db,
        fact_id=fact_id,
        fact_ids=explicit_fact_ids,
        slug=slug,
        event_date=event_date,
        since_hours=float(since_hours),
        limit=int(limit),
        latest_only=effective_latest_only,
        max_items_output=max(int(max_items_output), int(limit)),
    )
    candidate_rows: list[dict[str, Any]] = []
    packet_blockers: list[str] = []
    with _connect_ro(db) as conn:
        for candidate in list(plan.get("candidates") or [])[: max(0, int(max_items_output))]:
            row = _load_fact_row(conn, int(candidate.get("fact_id") or 0))
            if row is None:
                packet_blockers.append("candidate_fact_row_missing")
                continue
            payload = _safe_json(row["fact_json"])
            if not isinstance(payload, Mapping):
                packet_blockers.append("candidate_fact_json_invalid")
                continue
            candidate_slug = str(row["slug"] or "")
            candidate_event_date = str(row["event_date"] or "")
            is_latest_fact = bool(candidate.get("is_latest_fact"))
            projection = _writer_apply_projection(row, payload, db_path=db)
            writer_plan = _as_mapping(projection.get("plan"))
            projected_payload = _as_mapping(projection.get("projected_payload"))
            projected_audit = _as_mapping(projection.get("projected_audit"))
            projection_meta = _as_mapping(projection.get("projection"))
            if is_latest_fact and not bool(projected_audit.get("ok")):
                packet_blockers.append("candidate_writer_projection_not_ok")
            candidate_rows.append(
                {
                    "fact_id": int(row["id"] or 0),
                    "latest_fact_id": int(candidate.get("latest_fact_id") or 0),
                    "is_latest_fact": is_latest_fact,
                    "slug": candidate_slug,
                    "event_date": candidate_event_date,
                    "fact_as_of": str(row["fact_as_of"] or ""),
                    "composer_version": str(row["composer_version"] or ""),
                    "current_issues": list(candidate.get("current_issues") or []),
                    "issue_family": str(candidate.get("issue_family") or ""),
                    "current_target_bucket": candidate.get("current_target_bucket"),
                    "current_target_probability": candidate.get("current_target_probability"),
                    "projected_target_bucket": projected_audit.get("target_bucket"),
                    "projected_target_probability": projected_audit.get("target_probability"),
                    "projected_ok": bool(projected_audit.get("ok")),
                    "projected_issues": list(projected_audit.get("issues") or []),
                    "surface_probability_diff": _surface_diff(payload, projected_payload),
                    "writer_plan_schema": writer_plan.get("schema"),
                    "writer_plan_ok": bool(writer_plan.get("ok")),
                    "writer_plan_apply_supported": bool(writer_plan.get("apply_supported")),
                    "writer_plan_write_latest": bool(writer_plan.get("write_latest")),
                    "writer_projection_mode": str(projection_meta.get("projection_mode") or ""),
                    "writer_projection_context": dict(_as_mapping(projection_meta.get("context"))),
                    "writer_projection_issues": list(projection_meta.get("issues") or []),
                    "writer_plan_actions": _plan_actions(writer_plan),
                    "posterior_ssot_apply_contract": dict(
                        _as_mapping(writer_plan.get("posterior_ssot_apply_contract"))
                    ),
                    "impact": dict(_as_mapping(candidate.get("impact"))),
                    "final_bucket_lock_route": _build_final_lock_route(
                        db,
                        slug=candidate_slug,
                        event_date=candidate_event_date,
                        is_latest_fact=is_latest_fact,
                        backup_dir=backup_root,
                    ),
                    "generic_posterior_repair_route": _build_generic_posterior_repair_route(
                        db,
                        fact_id=int(row["id"] or 0),
                        slug=candidate_slug,
                        event_date=candidate_event_date,
                        is_latest_fact=is_latest_fact,
                        backup_dir=backup_root,
                    ),
                }
            )
    backup_cmd = f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup)}\""
    release_gate_cmd = (
        f"python3 scripts/hierarchical_probability_release_gate.py --summary "
        f"--limit {int(limit)} --max-items-output 3 --skip-production-uniqueness"
    )
    coverage_cmd = (
        f"python3 scripts/posterior_ssot_fact_coverage_audit.py --latest-only "
        f"--project-forward --limit {int(limit)} --max-items-output {int(max_items_output)}"
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": not packet_blockers,
        "generated_at": generated,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "posterior_ssot_repair_confirmation_no_apply",
        "db": str(db),
        "scope": {
            "fact_id": str(fact_id or ""),
            "fact_ids": explicit_fact_ids,
            "slug": str(slug or ""),
            "event_date": str(event_date or ""),
            "since_hours": float(since_hours),
            "limit": int(limit),
            "latest_only_requested": bool(latest_only),
            "latest_only_effective": bool(effective_latest_only),
        },
        "guards": _guards(),
        "confirmation_phrase": CONFIRMATION_PHRASE,
        "backup_path": str(backup),
        "backup_exists": backup.exists(),
        "source_plan_summary": {
            "schema": plan.get("schema"),
            "candidate_count": len(plan.get("candidates") or []),
            "forward_write_acceptance": dict(plan.get("forward_write_acceptance") or {}),
            "historical_rematerialization_plan": dict(plan.get("historical_rematerialization_plan") or {}),
        },
        "candidate_count": len(candidate_rows),
        "candidates": candidate_rows,
        "candidates_truncated": len(plan.get("candidates") or []) > len(candidate_rows),
        "packet_blockers": sorted(set(packet_blockers)),
        "operator_preflight": [
            {
                "step": "create_fresh_archive_backup",
                "command": backup_cmd,
                "writes": True,
                "writes_database": False,
                "required": bool(candidate_rows),
            },
            {
                "step": "review_no_apply_projection",
                "command": coverage_cmd,
                "writes": False,
                "required": True,
            },
            {
                "step": "run_release_gate_after_apply",
                "command": release_gate_cmd,
                "writes": False,
                "required": True,
            },
        ],
        "apply_boundary": {
            "requires_explicit_user_confirmation": True,
            "requires_fresh_backup": True,
            "requires_writer_quiescence": True,
            "requires_post_apply_release_gate": True,
            "generic_apply_command_emitted": any(
                bool(_as_mapping(row.get("generic_posterior_repair_route")).get("apply_command"))
                for row in candidate_rows
            ),
            "generic_apply_command_reason": (
                "current_latest_candidate_generic_posterior_repair_route_available"
                if any(
                    bool(_as_mapping(row.get("generic_posterior_repair_route")).get("apply_command"))
                    for row in candidate_rows
                )
                else "no_generic_apply_command_without_candidate"
            ),
        },
        "next_boundary": (
            "operator_review_confirmation_packet_then_choose_candidate_apply_route"
            if candidate_rows
            else "no_posterior_ssot_repair_candidates_in_scope"
        ),
    }


def compact_confirmation_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    candidates = [dict(_as_mapping(item)) for item in packet.get("candidates") or []]
    source_plan = _as_mapping(packet.get("source_plan_summary"))
    remat = _as_mapping(source_plan.get("historical_rematerialization_plan"))
    apply_ready = [
        row
        for row in candidates
        if bool(_as_mapping(row.get("generic_posterior_repair_route")).get("apply_command"))
    ]
    candidate_items: list[dict[str, Any]] = []
    for row in candidates:
        impact = _as_mapping(row.get("impact"))
        route = _as_mapping(row.get("generic_posterior_repair_route"))
        candidate_items.append(
            {
                "fact_id": int(row.get("fact_id") or 0),
                "slug": row.get("slug"),
                "event_date": row.get("event_date"),
                "is_latest_fact": bool(row.get("is_latest_fact")),
                "latest_fact_id": int(row.get("latest_fact_id") or 0),
                "issue_family": row.get("issue_family"),
                "current_issues": list(row.get("current_issues") or []),
                "projected_ok": bool(row.get("projected_ok")),
                "projected_issues": list(row.get("projected_issues") or []),
                "projected_target_bucket": row.get("projected_target_bucket"),
                "projected_target_probability": row.get("projected_target_probability"),
                "production_latest_impact": impact.get("production_latest_impact"),
                "daily_review_impact": impact.get("daily_review_impact"),
                "generic_apply_route_available": bool(route.get("available")),
                "generic_apply_route_reason": route.get("reason"),
            }
        )
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": packet.get("schema"),
        "ok": bool(packet.get("ok")),
        "read_only": bool(packet.get("read_only")),
        "dry_run": bool(packet.get("dry_run")),
        "execution_mode": "posterior_ssot_repair_confirmation_summary_no_apply",
        "db": packet.get("db"),
        "scope": dict(_as_mapping(packet.get("scope"))),
        "guards": dict(_as_mapping(packet.get("guards"))),
        "confirmation_phrase": packet.get("confirmation_phrase"),
        "backup_path": packet.get("backup_path"),
        "backup_exists": bool(packet.get("backup_exists")),
        "source_plan_summary": {
            "schema": source_plan.get("schema"),
            "candidate_count": int(source_plan.get("candidate_count") or 0),
            "historical_rematerialization_plan": dict(remat),
        },
        "candidate_count": len(candidates),
        "candidate_fact_ids": [int(row.get("fact_id") or 0) for row in candidates],
        "current_latest_candidate_count": sum(1 for row in candidates if bool(row.get("is_latest_fact"))),
        "historical_only_candidate_count": sum(1 for row in candidates if not bool(row.get("is_latest_fact"))),
        "projected_ok_count": sum(1 for row in candidates if bool(row.get("projected_ok"))),
        "projected_issue_count": sum(1 for row in candidates if not bool(row.get("projected_ok"))),
        "apply_ready_candidate_count": len(apply_ready),
        "candidate_items": candidate_items,
        "candidates_truncated": bool(packet.get("candidates_truncated")),
        "packet_blockers": list(packet.get("packet_blockers") or []),
        "apply_boundary": dict(_as_mapping(packet.get("apply_boundary"))),
        "next_boundary": packet.get("next_boundary"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--fact-id", default="")
    parser.add_argument("--fact-ids", default="", help="Comma-separated event_fact_history ids")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--since-hours", type=float, default=168.0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--all-history", action="store_true")
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    packet = build_confirmation_packet(
        db_path=args.db,
        fact_id=str(args.fact_id or ""),
        fact_ids=_parse_fact_ids(str(args.fact_ids or "")),
        slug=str(args.slug or ""),
        event_date=str(args.event_date or ""),
        since_hours=float(args.since_hours),
        limit=int(args.limit),
        latest_only=not bool(args.all_history),
        max_items_output=int(args.max_items_output),
        backup_dir=args.backup_dir,
    )
    payload = compact_confirmation_packet(packet) if bool(args.summary) else packet
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if packet.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
