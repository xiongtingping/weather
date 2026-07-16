#!/usr/bin/env python3
"""Build a read-only daily weather review report.

The report loads one fact snapshot from archive.db, attaches read-only SSOT/UI
lineage audits, and delegates first-bad-link attribution to
daily_weather_review_runtime. It never writes to the database, never fetches
external weather sources, and never places orders.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daily_weather_review_runtime import build_daily_weather_review
from daily_review_persistence_writer import (
    build_daily_review_persistence_plan,
    compact_daily_review_persistence_plan,
    daily_review_persistence_schema_status,
)
from scripts import phase0_apply_confirmation_packet, ssot_chain_replay_audit, ui_snapshot_lineage_audit


DEFAULT_DB = ROOT / "data" / "archive.db"


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


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(conn, name):
        return set()
    return {str(row["name"] or "") for row in conn.execute(f"PRAGMA table_info({name})").fetchall()}


def _date_only(raw: Any) -> str:
    text = str(raw or "").strip()
    return text[:10] if len(text) >= 10 else text


def _row_to_dict(row: sqlite3.Row, *, keys: Sequence[str]) -> Dict[str, Any]:
    row_keys = set(row.keys())
    return {key: row[key] for key in keys if key in row_keys}


def _load_fact_row(
    conn: sqlite3.Connection,
    *,
    fact_id: str = "",
    slug: str = "",
    event_date: str = "",
    run_date: str = "",
) -> Optional[sqlite3.Row]:
    if not _table_exists(conn, "event_fact_history"):
        return None
    try:
        fact_id_int = int(fact_id or 0)
    except Exception:
        fact_id_int = 0
    if fact_id_int > 0:
        return conn.execute("SELECT * FROM event_fact_history WHERE id = ? LIMIT 1", (fact_id_int,)).fetchone()

    def query(where: Sequence[str], params: Sequence[Any]) -> Optional[sqlite3.Row]:
        clause = "WHERE " + " AND ".join(where) if where else ""
        return conn.execute(
            f"""
            SELECT *
            FROM event_fact_history
            {clause}
            ORDER BY datetime(fact_as_of) DESC, id DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()

    where: List[str] = []
    params: List[Any] = []
    if slug:
        where.append("slug = ?")
        params.append(slug)
    if event_date:
        where.append("event_date = ?")
        params.append(_date_only(event_date))
    if run_date:
        where.append("run_date = ?")
        params.append(_date_only(run_date))
    row = query(where, params)
    if row is not None:
        return row
    if run_date and not event_date:
        fallback_where: List[str] = []
        fallback_params: List[Any] = []
        if slug:
            fallback_where.append("slug = ?")
            fallback_params.append(slug)
        fallback_where.append("event_date = ?")
        fallback_params.append(_date_only(run_date))
        return query(fallback_where, fallback_params)
    return None


def _load_decisions(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "auto_trade_decisions"):
        return []
    rows = conn.execute(
        """
        SELECT id, run_id, run_date, slug, event_date, outcome, side, action,
               decided_state, will_order, reason_codes_json, reasons_json,
               details_json, created_at
        FROM auto_trade_decisions
        WHERE slug = ? AND event_date = ?
        ORDER BY datetime(created_at) ASC, id ASC
        LIMIT ?
        """,
        (slug, _date_only(event_date), int(limit)),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = _row_to_dict(
            row,
            keys=(
                "id",
                "run_id",
                "run_date",
                "slug",
                "event_date",
                "outcome",
                "side",
                "action",
                "decided_state",
                "will_order",
                "reason_codes_json",
                "reasons_json",
                "created_at",
            ),
        )
        details = _safe_json(row["details_json"])
        if isinstance(details, Mapping):
            item["details"] = dict(details)
        out.append(item)
    return out


def _load_orders(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "auto_orders"):
        return []
    columns = _table_columns(conn, "auto_orders")
    keys = [
        key
        for key in (
            "id",
            "order_id",
            "slug",
            "event_date",
            "outcome",
            "side",
            "action",
            "price",
            "size",
            "strategy",
            "created_at",
            "order_status",
            "order_checked_at",
        )
        if key in columns
    ]
    rows = conn.execute(
        f"""
        SELECT {", ".join(keys)}
        FROM auto_orders
        WHERE slug = ? AND event_date = ?
        ORDER BY datetime(created_at) ASC, id ASC
        LIMIT ?
        """,
        (slug, _date_only(event_date), int(limit)),
    ).fetchall()
    return [_row_to_dict(row, keys=keys) for row in rows]


def _settlement_from_fact(fact_payload: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in ("settlement_actual", "settlement_actual_c", "actual", "actual_c"):
        value = fact_payload.get(key)
        if value not in (None, ""):
            out["settlement_actual"] = value
            out["source"] = "fact_snapshot"
            break
    for key in ("settlement_actual_source", "settlement_source"):
        value = fact_payload.get(key)
        if value not in (None, ""):
            out["settlement_actual_source"] = value
            break
    return out


def _compact_audit(name: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "schema": str(payload.get("schema") or ""),
        "ok": payload.get("ok") is not False,
        "read_only": payload.get("read_only") is not False,
        "blockers": list(payload.get("blockers") or [])[:20],
        "warnings": list(payload.get("warnings") or [])[:20],
    }
    for key in ("counts", "snapshot", "source_counts", "warning_counts", "blocker_counts", "chain_coverage"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            out[key] = dict(value)
    if name == "ui_snapshot_lineage":
        for key in ("fact_results", "replay_results"):
            value = payload.get(key)
            if isinstance(value, list):
                out[key] = value[:3]
    if "signal_count" in payload:
        out["signal_count"] = payload.get("signal_count")
    return out


def _compact_operator_apply_gate_summary(payload: Mapping[str, Any]) -> Dict[str, Any]:
    summary = payload.get("operator_apply_gate_summary")
    summary_map = summary if isinstance(summary, Mapping) else {}
    return {
        "schema": str(summary_map.get("schema") or ""),
        "read_only": summary_map.get("read_only") is not False,
        "execution_mode": str(summary_map.get("execution_mode") or ""),
        "ready_now": bool(summary_map.get("ready_now")),
        "ready_after_operator_preflight": bool(summary_map.get("ready_after_operator_preflight")),
        "first_blocked_gate": str(summary_map.get("first_blocked_gate") or ""),
        "actionable_blocked_gates": list(summary_map.get("actionable_blocked_gates") or [])[:10],
        "gates": [
            {
                "gate": str(gate.get("gate") or ""),
                "applicable": bool(gate.get("applicable")),
                "ready_now": bool(gate.get("ready_now")),
                "ready_after_operator_preflight": bool(gate.get("ready_after_operator_preflight")),
                "blocked": bool(gate.get("blocked")),
                "blockers_now": list(gate.get("blockers_now") or [])[:10],
                "reason": str(gate.get("reason") or ""),
                "source": str(gate.get("source") or ""),
            }
            for gate in summary_map.get("gates") or []
            if isinstance(gate, Mapping)
        ][:5],
        "source_packet_schema": str(payload.get("schema") or ""),
    }


def _ui_snapshot_item_matches_fact(
    item: Mapping[str, Any],
    *,
    fact_id: int,
    slug: str,
    event_date: str,
) -> bool:
    try:
        item_fact_id = int(item.get("fact_id") or 0)
    except Exception:
        item_fact_id = 0
    if fact_id > 0 and item_fact_id == fact_id:
        return True
    return str(item.get("slug") or "").strip() == slug and _date_only(item.get("event_date")) == _date_only(event_date)


def _target_ui_snapshot_payload(
    payload: Mapping[str, Any],
    *,
    fact_id: int,
    slug: str,
    event_date: str,
) -> tuple[Dict[str, Any], bool]:
    out = dict(payload)
    facts = payload.get("facts")
    facts_map = dict(facts) if isinstance(facts, Mapping) else {}
    fact_items = facts_map.get("items")
    matched_fact_items = [
        dict(item)
        for item in fact_items or []
        if isinstance(item, Mapping)
        and _ui_snapshot_item_matches_fact(item, fact_id=fact_id, slug=slug, event_date=event_date)
    ]
    lineage_state = payload.get("lineage_state")
    lineage_map = dict(lineage_state) if isinstance(lineage_state, Mapping) else {}
    lineage_items = lineage_map.get("items")
    matched_lineage_items = [
        dict(item)
        for item in lineage_items or []
        if isinstance(item, Mapping)
        and _ui_snapshot_item_matches_fact(item, fact_id=fact_id, slug=slug, event_date=event_date)
    ]
    if not matched_fact_items and not matched_lineage_items:
        return out, False
    facts_map["items"] = matched_fact_items
    facts_map["total"] = len(matched_fact_items)
    facts_map["page_size"] = len(matched_fact_items)
    out["facts"] = facts_map
    source_fact_ids: List[int] = []
    fact_snapshot_ids: List[str] = []
    source_event_ids: List[str] = []
    posterior_run_ids: List[str] = []
    signal_ids: List[str] = []
    for item in matched_lineage_items:
        try:
            item_fact_id = int(item.get("fact_id") or 0)
        except Exception:
            item_fact_id = 0
        if item_fact_id > 0 and item_fact_id not in source_fact_ids:
            source_fact_ids.append(item_fact_id)
        for key, target in (
            ("fact_snapshot_id", fact_snapshot_ids),
            ("posterior_run_id", posterior_run_ids),
            ("signal_id", signal_ids),
        ):
            value = str(item.get(key) or "").strip()
            if value and value not in target:
                target.append(value)
        for source_event_id in item.get("source_event_ids") or []:
            text = str(source_event_id or "").strip()
            if text and text not in source_event_ids:
                source_event_ids.append(text)
    if not source_fact_ids and fact_id > 0:
        source_fact_ids = [fact_id]
    lineage_map["items"] = matched_lineage_items
    lineage_map["source_fact_ids"] = source_fact_ids
    lineage_map["fact_snapshot_ids"] = fact_snapshot_ids
    lineage_map["posterior_run_ids"] = posterior_run_ids
    lineage_map["source_event_ids"] = source_event_ids
    lineage_map["signal_ids"] = signal_ids
    lineage_map["fact_lineage_complete"] = all(
        item.get("fact_id") and item.get("fact_snapshot_id") for item in matched_lineage_items
    )
    out["lineage_state"] = lineage_map
    out["source_fact_ids"] = source_fact_ids
    out["fact_snapshot_ids"] = fact_snapshot_ids
    out["posterior_run_ids"] = posterior_run_ids
    out["source_event_ids"] = source_event_ids
    out["signal_ids"] = signal_ids
    return out, True


def build_report(
    db_path: Path | str = DEFAULT_DB,
    *,
    run_date: str = "",
    slug: str = "",
    event_date: str = "",
    fact_id: str = "",
    include_ui_snapshot_audit: bool = True,
    include_replay_audit: bool = True,
    ui_snapshot_mode: str = "summary",
    ui_snapshot_city_filter: str = "",
    ui_snapshot_payload: Optional[Mapping[str, Any]] = None,
    settlement_override: Optional[Mapping[str, Any]] = None,
    include_operator_gate_audit: bool = False,
    since_hours: float = 72.0,
    limit: int = 100,
) -> Dict[str, Any]:
    db = Path(db_path)
    blockers: List[str] = []
    warnings: List[str] = []
    with _connect_readonly(db) as conn:
        fact_row = _load_fact_row(
            conn,
            fact_id=fact_id,
            slug=slug,
            event_date=event_date,
            run_date=run_date,
        )
        if fact_row is None:
            return {
                "ok": False,
                "schema": "daily_weather_review_report_v1",
                "read_only": True,
                "db": str(db),
                "blockers": ["fact_snapshot_not_found"],
                "warnings": [],
                "guards": _guards(),
                "next_boundary": "select_valid_fact_snapshot_or_wait_for_fact_writer",
            }
        fact_payload_raw = _safe_json(fact_row["fact_json"])
        fact_payload = dict(fact_payload_raw) if isinstance(fact_payload_raw, Mapping) else {}
        effective_slug = str(fact_row["slug"] or fact_payload.get("slug") or slug or "")
        effective_event_date = _date_only(fact_row["event_date"] or fact_payload.get("event_date") or event_date or "")
        effective_run_date = _date_only(fact_row["run_date"] or fact_payload.get("run_date") or run_date or effective_event_date)
        decisions = _load_decisions(conn, slug=effective_slug, event_date=effective_event_date, limit=limit)
        orders = _load_orders(conn, slug=effective_slug, event_date=effective_event_date, limit=limit)
        fact_meta = _row_to_dict(
            fact_row,
            keys=("id", "slug", "event_date", "run_date", "fact_as_of", "created_at", "composer_version"),
        )
        fact_payload.setdefault("fact_id", fact_meta.get("id"))
        fact_payload.setdefault("fact_as_of", fact_meta.get("fact_as_of"))
        fact_payload.setdefault("created_at", fact_meta.get("created_at"))
        fact_payload.setdefault("composer_version", fact_meta.get("composer_version"))

    audit_attachments: Dict[str, Any] = {}
    if include_ui_snapshot_audit:
        if ui_snapshot_payload is None:
            payload = ui_snapshot_lineage_audit.load_ui_snapshot(
                mode=ui_snapshot_mode,
                run_date=effective_run_date,
                limit=min(max(int(limit), 1), 80),
                city_filter=ui_snapshot_city_filter,
                force_rebuild=False,
            )
        else:
            payload = dict(ui_snapshot_payload)
        payload, target_fact_visible = _target_ui_snapshot_payload(
            payload,
            fact_id=int(fact_meta.get("id") or 0),
            slug=effective_slug,
            event_date=effective_event_date,
        )
        if not target_fact_visible:
            warnings.append("ui_snapshot_target_fact_not_displayed")
        ui_audit = ui_snapshot_lineage_audit.audit_payload(
            payload,
            db_path=db,
            run_replay=False,
            since_hours=since_hours,
            max_items_output=5,
        )
        audit_attachments["ui_snapshot_lineage"] = _compact_audit("ui_snapshot_lineage", ui_audit)
        if not ui_audit.get("ok"):
            blockers.append("ui_snapshot_lineage_audit_failed")
    if include_replay_audit:
        replay = ssot_chain_replay_audit.audit(
            db_path=db,
            fact_id=str(fact_meta.get("id") or ""),
            since_hours=since_hours,
            limit=max(int(limit), 100),
            max_signals_output=0,
        )
        audit_attachments["ssot_chain_replay"] = _compact_audit("ssot_chain_replay", replay)
        if not replay.get("ok"):
            blockers.append("ssot_chain_replay_audit_failed")
        for warning in replay.get("warnings") or []:
            text = str(warning or "").strip()
            if text:
                warnings.append(f"ssot_chain_replay:{text}")
    if include_operator_gate_audit:
        try:
            gate_packet = phase0_apply_confirmation_packet.build_confirmation_packet(db)
            audit_attachments["operator_apply_gate_summary"] = _compact_operator_apply_gate_summary(gate_packet)
        except Exception as exc:  # pragma: no cover - defensive audit attachment guard
            audit_attachments["operator_apply_gate_summary"] = {
                "schema": "phase0_operator_apply_gate_summary_compact_v1",
                "ok": False,
                "read_only": True,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
            blockers.append("operator_apply_gate_summary_audit_failed")

    settlement = _settlement_from_fact(fact_payload)
    if settlement_override:
        settlement = {**settlement, **dict(settlement_override)}
    review = build_daily_weather_review(
        run_date=effective_run_date,
        slug=effective_slug,
        event_date=effective_event_date,
        fact_snapshot=fact_payload,
        decisions=decisions,
        orders=orders,
        settlement=settlement,
        source_health={"ok": True, "source": "daily_weather_review_report"},
        timing=dict(fact_payload.get("signal_to_order_timing") or {}),
        audit_attachments=audit_attachments,
    )
    persistence_plan = build_daily_review_persistence_plan(review)
    with _connect_readonly(db) as conn:
        persistence_schema_status = daily_review_persistence_schema_status(conn)
    review_blocked = review.get("first_bad_link") in {"fact_write", "daily_review"} and bool(blockers)
    return {
        "ok": not blockers,
        "schema": "daily_weather_review_report_v1",
        "read_only": True,
        "db": str(db),
        "fact": fact_meta,
        "review": review,
        "daily_review_persistence_plan": compact_daily_review_persistence_plan(persistence_plan),
        "daily_review_persistence_schema_status": persistence_schema_status,
        "audit_attachments": audit_attachments,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": _guards(),
        "coverage": {
            "decision_count": len(decisions),
            "order_count": len(orders),
            "audit_attachment_count": len(audit_attachments),
            "review_blocked_by_attachment": bool(review_blocked),
        },
        "next_boundary": "persist_daily_review_after_schema_apply_and_user_confirmation",
    }


def _guards() -> Dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_daily_weather_reviews": False,
        "writes_bias_correction_candidates": False,
        "writes_parameter_change_events": False,
        "writes_auto_trade_decisions": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "fetches_external_weather_sources": False,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--run-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--fact-id", default="")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--since-hours", type=float, default=72.0)
    parser.add_argument("--ui-snapshot-mode", choices=("full", "summary"), default="summary")
    parser.add_argument("--ui-snapshot-city-filter", default="")
    parser.add_argument("--skip-ui-snapshot-audit", action="store_true")
    parser.add_argument("--skip-replay-audit", action="store_true")
    parser.add_argument("--include-operator-gate-audit", action="store_true")
    args = parser.parse_args(argv)
    report = build_report(
        args.db,
        run_date=args.run_date,
        slug=args.slug,
        event_date=args.event_date,
        fact_id=args.fact_id,
        include_ui_snapshot_audit=not args.skip_ui_snapshot_audit,
        include_replay_audit=not args.skip_replay_audit,
        ui_snapshot_mode=args.ui_snapshot_mode,
        ui_snapshot_city_filter=args.ui_snapshot_city_filter,
        include_operator_gate_audit=bool(args.include_operator_gate_audit),
        since_hours=float(args.since_hours),
        limit=int(args.limit),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
