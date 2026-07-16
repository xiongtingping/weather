#!/usr/bin/env python3
"""Read-only post-apply acceptance audit for persisted daily reviews.

This is the single acceptance surface after schema apply, lineage repair, and
daily review persistence. It composes the existing coverage, SSOT replay, and
UI snapshot lineage audits, then checks that review, fact, posterior, and UI
lineage point at the same immutable fact snapshot.

It never writes archive.db, backfills history, fetches external weather
sources, changes parameters, restarts launchd, or places orders.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import daily_review_coverage_audit, ui_snapshot_lineage_audit


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "daily_review_post_apply_acceptance_audit_v1"


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


def _load_lineage_decision_manifest_file(path: Path | str | None) -> Mapping[str, Any]:
    if path is None:
        return {}
    manifest_path = Path(path)
    if not str(manifest_path).strip():
        return {}
    text = manifest_path.expanduser().read_text(encoding="utf-8")
    payload = json.loads(text)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _date_only(value: Any) -> str:
    return str(value or "").strip()[:10]


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _review_table_row_count(db: Path) -> int | None:
    try:
        with _connect_readonly(db) as conn:
            if not _table_exists(conn, "daily_weather_reviews"):
                return None
            row = conn.execute("SELECT COUNT(*) AS c FROM daily_weather_reviews").fetchone()
            return int(row["c"] or 0)
    except Exception:
        return None


def _ok_gate(
    *,
    gate: str,
    ok: bool,
    blockers: Sequence[str] = (),
    warnings: Sequence[str] = (),
    summary: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "gate": gate,
        "ok": bool(ok),
        "blockers": sorted({str(item) for item in blockers if str(item or "").strip()}),
        "warnings": sorted({str(item) for item in warnings if str(item or "").strip()}),
        "summary": dict(summary or {}),
    }


def _coverage_gate(coverage: Mapping[str, Any]) -> dict[str, Any]:
    blockers = list(coverage.get("blockers") or [])
    return _ok_gate(
        gate="daily_review_coverage_gate",
        ok=bool(coverage.get("ok")),
        blockers=blockers,
        summary={
            "raw_settled_event_total_count": int(coverage.get("raw_settled_event_total_count") or 0),
            "raw_settled_event_scanned_count": int(coverage.get("raw_settled_event_scanned_count") or 0),
            "settled_event_total_count": int(coverage.get("settled_event_total_count") or 0),
            "settled_event_count": int(coverage.get("settled_event_count") or 0),
            "settled_event_scanned_count": int(coverage.get("settled_event_scanned_count") or 0),
            "excluded_settled_event_count": int(coverage.get("excluded_settled_event_count") or 0),
            "excluded_settled_event_scanned_count": int(
                coverage.get("excluded_settled_event_scanned_count") or 0
            ),
            "coverage_scope_complete": bool(coverage.get("coverage_scope_complete")),
            "coverage_scope_truncated_count": int(coverage.get("coverage_scope_truncated_count") or 0),
            "reviewed_event_count": int(coverage.get("reviewed_event_count") or 0),
            "missing_review_count": int(coverage.get("missing_review_count") or 0),
            "coverage_ratio": coverage.get("coverage_ratio"),
            "chain_ok_count": int(coverage.get("chain_ok_count") or 0),
            "chain_blocked_count": int(coverage.get("chain_blocked_count") or 0),
        },
    )


def _item_blocker_samples(items: Sequence[Mapping[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    visible_limit = max(0, int(limit))
    if visible_limit <= 0:
        return samples
    for raw in items:
        item = _as_mapping(raw)
        blockers = [str(value) for value in item.get("blockers") or [] if str(value or "").strip()]
        if not blockers:
            continue
        latest_fact = _as_mapping(item.get("latest_fact"))
        replay = _as_mapping(item.get("review_replay"))
        posterior_lineage = _as_mapping(item.get("posterior_lineage"))
        samples.append(
            {
                "slug": item.get("slug"),
                "event_date": item.get("event_date"),
                "review_id": item.get("review_id"),
                "first_bad_link": item.get("first_bad_link"),
                "blockers": sorted(set(blockers)),
                "review_fact_snapshot_id": item.get("review_fact_snapshot_id"),
                "review_posterior_run_id": item.get("review_posterior_run_id"),
                "latest_fact": {
                    "available": bool(latest_fact.get("available")),
                    "reason": latest_fact.get("reason"),
                    "fact_id": latest_fact.get("fact_id"),
                    "fact_snapshot_id": latest_fact.get("fact_snapshot_id"),
                    "posterior_run_id": latest_fact.get("posterior_run_id"),
                    "posterior_ssot_fact_present": bool(latest_fact.get("posterior_ssot_fact_present")),
                },
                "posterior_lineage": {
                    "ok": posterior_lineage.get("ok"),
                    "status": posterior_lineage.get("status"),
                    "posterior_target_source": posterior_lineage.get("posterior_target_source"),
                    "fact_snapshot_id": posterior_lineage.get("fact_snapshot_id"),
                    "posterior_run_id": posterior_lineage.get("posterior_run_id"),
                },
                "review_replay": {
                    "ok": replay.get("ok"),
                    "source_counts": dict(_as_mapping(replay.get("source_counts"))),
                    "anchor_state": dict(_as_mapping(replay.get("anchor_state"))),
                    "blockers": list(replay.get("blockers") or [])[:10],
                    "warnings": list(replay.get("warnings") or [])[:10],
                },
            }
        )
        if len(samples) >= visible_limit:
            break
    return samples


def _review_replay_fact_source_required(replay: Mapping[str, Any]) -> bool:
    if not bool(replay.get("ok")):
        return True
    anchor_state = _as_mapping(replay.get("anchor_state"))
    if bool(anchor_state.get("fact_anchor_available")):
        return False
    if anchor_state.get("hard_source_signal_expected") is False:
        return False
    return True


def _review_replay_gate(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    replayed = 0
    replay_ok = 0
    failed_reviews: list[str] = []
    for item in items:
        review_id = str(item.get("review_id") or "").strip()
        replay = _as_mapping(item.get("review_replay"))
        if not review_id:
            continue
        replayed += 1
        if replay.get("ok"):
            replay_ok += 1
        else:
            failed_reviews.append(review_id)
            blockers.append("review_replay_failed")
        source_counts = _as_mapping(replay.get("source_counts"))
        if int(source_counts.get("event_fact_history") or 0) <= 0 and _review_replay_fact_source_required(replay):
            blockers.append("review_replay_fact_missing")
            if review_id not in failed_reviews:
                failed_reviews.append(review_id)
        for warning in replay.get("warnings") or []:
            warnings.append(f"{review_id}:{warning}")
    return _ok_gate(
        gate="review_to_ssot_replay_gate",
        ok=not blockers,
        blockers=blockers,
        warnings=warnings,
        summary={
            "reviewed_items_checked": replayed,
            "replay_ok_count": replay_ok,
            "failed_review_ids": failed_reviews[:20],
        },
    )


def _single_lineage_gate(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    blockers: list[str] = []
    checked: list[dict[str, Any]] = []
    for item in items:
        if not item.get("review_id"):
            continue
        slug = str(item.get("slug") or "").strip()
        event_date = _date_only(item.get("event_date"))
        review_fact_snapshot_id = str(item.get("review_fact_snapshot_id") or "").strip()
        latest_fact = _as_mapping(item.get("latest_fact"))
        latest_fact_snapshot_id = str(latest_fact.get("fact_snapshot_id") or "").strip()
        review_posterior_run_id = str(item.get("review_posterior_run_id") or "").strip()
        latest_posterior_run_id = str(item.get("latest_fact_posterior_run_id") or "").strip()
        posterior_lineage = _as_mapping(item.get("posterior_lineage"))
        item_blockers: list[str] = []
        if not bool(item.get("review_fact_snapshot_matches_latest")):
            item_blockers.append("review_fact_snapshot_id_not_latest_fact")
        if latest_posterior_run_id and not bool(item.get("review_posterior_run_matches_latest")):
            item_blockers.append("review_posterior_run_id_not_latest_fact")
        if not latest_fact.get("posterior_ssot_fact_present"):
            item_blockers.append("latest_fact_posterior_ssot_fact_missing")
        if str(posterior_lineage.get("posterior_target_source") or "") != "posterior_ssot_fact":
            item_blockers.append("review_posterior_lineage_not_from_posterior_ssot_fact")
        if not review_fact_snapshot_id:
            item_blockers.append("review_fact_snapshot_id_missing")
        if latest_fact_snapshot_id and review_fact_snapshot_id and latest_fact_snapshot_id != review_fact_snapshot_id:
            item_blockers.append("review_fact_snapshot_id_mismatch")
        if latest_posterior_run_id and review_posterior_run_id and latest_posterior_run_id != review_posterior_run_id:
            item_blockers.append("review_posterior_run_id_mismatch")
        blockers.extend(item_blockers)
        checked.append(
            {
                "slug": slug,
                "event_date": event_date,
                "review_id": str(item.get("review_id") or ""),
                "fact_snapshot_id": review_fact_snapshot_id or latest_fact_snapshot_id,
                "posterior_run_id": review_posterior_run_id or latest_posterior_run_id,
                "ok": not item_blockers,
                "blockers": sorted(set(item_blockers)),
            }
        )
    return _ok_gate(
        gate="single_fact_snapshot_lineage_gate",
        ok=not blockers,
        blockers=blockers,
        summary={
            "checked_review_count": len(checked),
            "ok_review_count": sum(1 for item in checked if item.get("ok")),
            "items": checked[:20],
        },
    )


def _load_ui_payload(
    *,
    db: Path,
    payload_json: Optional[Path],
    mode: str,
    run_date: str,
    limit: int,
    city_filter: str,
    force_rebuild: bool,
) -> tuple[Mapping[str, Any] | None, str]:
    if payload_json:
        payload = _safe_json(payload_json.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            return payload, ""
        return None, "ui_snapshot_payload_json_not_object"
    if db.resolve() != DEFAULT_DB.resolve():
        return None, "ui_snapshot_payload_required_for_non_default_db"
    payload = ui_snapshot_lineage_audit.load_ui_snapshot(
        mode=mode,
        run_date=run_date,
        limit=limit,
        city_filter=city_filter,
        force_rebuild=force_rebuild,
    )
    return payload, ""


def _ui_fact_key(item: Mapping[str, Any]) -> tuple[str, str]:
    return str(item.get("slug") or "").strip(), _date_only(item.get("event_date"))


def _payload_ui_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()

    def add_item(raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        item = dict(raw)
        key = (
            str(item.get("slug") or "").strip(),
            _date_only(item.get("event_date")),
            str(item.get("fact_id") or "").strip(),
            str(item.get("fact_snapshot_id") or "").strip(),
            str(item.get("posterior_run_id") or "").strip(),
        )
        if key in seen:
            return
        seen.add(key)
        out.append(item)

    lineage_items = _as_mapping(payload.get("lineage_state")).get("items")
    if isinstance(lineage_items, list):
        for item in lineage_items:
            add_item(item)
    fact_items = _as_mapping(payload.get("facts")).get("items")
    if isinstance(fact_items, list):
        for item in fact_items:
            add_item(item)
    return out


def _ui_gate(
    *,
    db: Path,
    coverage_items: Sequence[Mapping[str, Any]],
    include_ui_snapshot_audit: bool,
    ui_payload_json: Optional[Path],
    ui_snapshot_mode: str,
    ui_snapshot_run_date: str,
    ui_snapshot_limit: int,
    ui_snapshot_city_filter: str,
    force_rebuild_ui_snapshot: bool,
    require_ui_visible: bool,
    since_hours: float,
    replay_limit: int,
    max_items_output: int,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    if not include_ui_snapshot_audit:
        return (
            _ok_gate(
                gate="ui_snapshot_lineage_gate",
                ok=True,
                warnings=["ui_snapshot_lineage_audit_skipped"],
                summary={"status": "skipped"},
            ),
            {},
        )
    payload, load_reason = _load_ui_payload(
        db=db,
        payload_json=ui_payload_json,
        mode=ui_snapshot_mode,
        run_date=ui_snapshot_run_date,
        limit=ui_snapshot_limit,
        city_filter=ui_snapshot_city_filter,
        force_rebuild=force_rebuild_ui_snapshot,
    )
    if payload is None:
        blocker_or_warning = [load_reason]
        return (
            _ok_gate(
                gate="ui_snapshot_lineage_gate",
                ok=not require_ui_visible,
                blockers=blocker_or_warning if require_ui_visible else [],
                warnings=[] if require_ui_visible else blocker_or_warning,
                summary={"status": "not_loaded", "reason": load_reason},
            ),
            {},
        )
    ui_audit = ui_snapshot_lineage_audit.audit_payload(
        payload,
        db_path=db,
        run_replay=False,
        since_hours=since_hours,
        replay_limit=replay_limit,
        max_items_output=max_items_output,
    )
    blockers = list(ui_audit.get("blockers") or [])
    warnings = list(ui_audit.get("warnings") or [])
    fact_results = [
        dict(item)
        for item in ui_audit.get("fact_results") or []
        if isinstance(item, Mapping)
    ]
    ui_items = _payload_ui_items(payload)
    if not ui_items:
        ui_items = fact_results
    ui_by_key: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in ui_items:
        ui_by_key.setdefault(_ui_fact_key(item), []).append(item)

    matched_review_count = 0
    missing_review_ui_count = 0
    join_results: list[dict[str, Any]] = []
    for item in coverage_items:
        if not item.get("review_id"):
            continue
        key = (str(item.get("slug") or "").strip(), _date_only(item.get("event_date")))
        ui_items = ui_by_key.get(key, [])
        review_fact_snapshot_id = str(item.get("review_fact_snapshot_id") or "").strip()
        review_posterior_run_id = str(item.get("review_posterior_run_id") or "").strip()
        item_blockers: list[str] = []
        if not ui_items:
            missing_review_ui_count += 1
            if require_ui_visible:
                item_blockers.append("ui_snapshot_review_fact_missing")
        else:
            matched_review_count += 1
            fact_snapshot_ids = {str(ui.get("fact_snapshot_id") or "").strip() for ui in ui_items}
            posterior_run_ids = {str(ui.get("posterior_run_id") or "").strip() for ui in ui_items}
            if review_fact_snapshot_id and review_fact_snapshot_id not in fact_snapshot_ids:
                item_blockers.append("ui_snapshot_review_fact_snapshot_id_mismatch")
            if review_posterior_run_id and posterior_run_ids and review_posterior_run_id not in posterior_run_ids:
                item_blockers.append("ui_snapshot_review_posterior_run_id_mismatch")
        blockers.extend(item_blockers)
        join_results.append(
            {
                "slug": key[0],
                "event_date": key[1],
                "review_id": str(item.get("review_id") or ""),
                "review_fact_snapshot_id": review_fact_snapshot_id,
                "review_posterior_run_id": review_posterior_run_id,
                "ui_fact_count": len(ui_items),
                "ok": not item_blockers,
                "blockers": sorted(set(item_blockers)),
            }
        )
    if coverage_items and matched_review_count <= 0:
        warnings.append("ui_snapshot_no_review_scope_intersection")
    return (
        _ok_gate(
            gate="ui_snapshot_lineage_gate",
            ok=not blockers,
            blockers=blockers,
            warnings=warnings,
            summary={
                "ui_audit_ok": bool(ui_audit.get("ok")),
                "checked_facts": int(_as_mapping(ui_audit.get("counts")).get("checked_facts") or 0),
                "matched_review_count": matched_review_count,
                "missing_review_ui_count": missing_review_ui_count,
                "require_ui_visible": bool(require_ui_visible),
                "join_results": join_results[:20],
            },
        ),
        ui_audit,
    )


def build_report(
    db_path: Path | str = DEFAULT_DB,
    *,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 20,
    since_hours: float = 24 * 90,
    replay_limit: int = 500,
    include_ui_snapshot_audit: bool = True,
    ui_payload_json: Optional[Path] = None,
    ui_snapshot_mode: str = "summary",
    ui_snapshot_run_date: str = "",
    ui_snapshot_limit: int = 120,
    ui_snapshot_city_filter: str = "",
    force_rebuild_ui_snapshot: bool = False,
    require_ui_visible: bool = False,
    lineage_decision_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = Path(db_path)
    effective_max_items = max(int(max_items_output), min(int(limit), 100))
    coverage = daily_review_coverage_audit.build_report(
        db,
        since_days=since_days,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
        max_items_output=effective_max_items,
        since_hours=since_hours,
        replay_limit=replay_limit,
        lineage_decision_manifest=lineage_decision_manifest,
    )
    coverage_items = [
        dict(item)
        for item in coverage.get("items") or []
        if isinstance(item, Mapping)
    ]
    gates = [
        _coverage_gate(coverage),
        _review_replay_gate(coverage_items),
        _single_lineage_gate(coverage_items),
    ]
    ui_gate, ui_audit = _ui_gate(
        db=db,
        coverage_items=coverage_items,
        include_ui_snapshot_audit=include_ui_snapshot_audit,
        ui_payload_json=ui_payload_json,
        ui_snapshot_mode=ui_snapshot_mode,
        ui_snapshot_run_date=ui_snapshot_run_date,
        ui_snapshot_limit=ui_snapshot_limit,
        ui_snapshot_city_filter=ui_snapshot_city_filter,
        force_rebuild_ui_snapshot=force_rebuild_ui_snapshot,
        require_ui_visible=require_ui_visible,
        since_hours=since_hours,
        replay_limit=replay_limit,
        max_items_output=max_items_output,
    )
    gates.append(ui_gate)
    blockers = sorted({blocker for gate in gates for blocker in gate.get("blockers", [])})
    warnings = sorted({warning for gate in gates for warning in gate.get("warnings", [])})
    return {
        "ok": not blockers,
        "schema": SCHEMA_VERSION,
        "read_only": True,
        "db": str(db),
        "since_date": coverage.get("since_date"),
        "market_scope": market_scope,
        "slug": slug,
        "event_date": event_date,
        "daily_weather_reviews_row_count": _review_table_row_count(db),
        "coverage": {
            "schema": str(coverage.get("schema") or ""),
            "ok": bool(coverage.get("ok")),
            "requested_limit": int(coverage.get("requested_limit") or 0),
            "raw_settled_event_total_count": int(coverage.get("raw_settled_event_total_count") or 0),
            "raw_settled_event_scanned_count": int(coverage.get("raw_settled_event_scanned_count") or 0),
            "settled_event_total_count": int(coverage.get("settled_event_total_count") or 0),
            "settled_event_count": int(coverage.get("settled_event_count") or 0),
            "settled_event_scanned_count": int(coverage.get("settled_event_scanned_count") or 0),
            "excluded_settled_event_count": int(coverage.get("excluded_settled_event_count") or 0),
            "excluded_settled_event_scanned_count": int(
                coverage.get("excluded_settled_event_scanned_count") or 0
            ),
            "excluded_samples": list(coverage.get("excluded_samples") or [])[:20],
            "lineage_decision_manifest_audit": dict(
                _as_mapping(coverage.get("lineage_decision_manifest_audit"))
            ),
            "coverage_scope_complete": bool(coverage.get("coverage_scope_complete")),
            "coverage_scope_truncated_count": int(coverage.get("coverage_scope_truncated_count") or 0),
            "reviewed_event_count": int(coverage.get("reviewed_event_count") or 0),
            "missing_review_count": int(coverage.get("missing_review_count") or 0),
            "coverage_ratio": coverage.get("coverage_ratio"),
            "chain_ok_count": int(coverage.get("chain_ok_count") or 0),
            "chain_blocked_count": int(coverage.get("chain_blocked_count") or 0),
            "blockers": list(coverage.get("blockers") or [])[:20],
            "item_blocker_counts": dict(_as_mapping(coverage.get("item_blocker_counts"))),
            "review_field_capability_matrix": coverage.get("review_field_capability_matrix"),
        },
        "item_blocker_samples": _item_blocker_samples(coverage_items, limit=max_items_output),
        "ui_snapshot_lineage": {
            "schema": str(ui_audit.get("schema") or "") if isinstance(ui_audit, Mapping) else "",
            "ok": bool(ui_audit.get("ok")) if isinstance(ui_audit, Mapping) else None,
            "counts": dict(_as_mapping(ui_audit.get("counts"))) if isinstance(ui_audit, Mapping) else {},
            "snapshot": dict(_as_mapping(ui_audit.get("snapshot"))) if isinstance(ui_audit, Mapping) else {},
            "blockers": list(ui_audit.get("blockers") or [])[:20] if isinstance(ui_audit, Mapping) else [],
            "warnings": list(ui_audit.get("warnings") or [])[:20] if isinstance(ui_audit, Mapping) else [],
        },
        "gates": gates,
        "blockers": blockers,
        "warnings": warnings,
        "guards": {
            "writes_archive_db": False,
            "writes_daily_weather_reviews": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "changes_parameters": False,
        },
        "next_boundary": (
            "apply_daily_review_schema_then_persist_reviews"
            if "daily_weather_reviews_missing_planned_table" in blockers
            else "fix_post_apply_acceptance_first_blocker"
            if blockers
            else "wire_daily_review_post_apply_acceptance_into_phase8_closeout"
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since-days", type=int, default=45)
    parser.add_argument("--since-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--since-hours", type=float, default=24 * 90)
    parser.add_argument("--replay-limit", type=int, default=500)
    parser.add_argument("--skip-ui-snapshot-audit", action="store_true")
    parser.add_argument("--ui-payload-json", type=Path, default=None)
    parser.add_argument("--ui-snapshot-mode", choices=("full", "summary"), default="summary")
    parser.add_argument("--ui-snapshot-run-date", default="")
    parser.add_argument("--ui-snapshot-limit", type=int, default=120)
    parser.add_argument("--ui-snapshot-city-filter", default="")
    parser.add_argument("--force-rebuild-ui-snapshot", action="store_true")
    parser.add_argument("--require-ui-visible", action="store_true")
    parser.add_argument("--lineage-decision-manifest", type=Path, default=None)
    args = parser.parse_args(argv)
    lineage_decision_manifest = _load_lineage_decision_manifest_file(args.lineage_decision_manifest)

    result = build_report(
        args.db,
        since_days=int(args.since_days),
        since_date=str(args.since_date or ""),
        slug=str(args.slug or ""),
        event_date=str(args.event_date or ""),
        market_scope=str(args.market_scope or ""),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        since_hours=float(args.since_hours),
        replay_limit=int(args.replay_limit),
        include_ui_snapshot_audit=not bool(args.skip_ui_snapshot_audit),
        ui_payload_json=args.ui_payload_json,
        ui_snapshot_mode=str(args.ui_snapshot_mode or "summary"),
        ui_snapshot_run_date=str(args.ui_snapshot_run_date or ""),
        ui_snapshot_limit=int(args.ui_snapshot_limit),
        ui_snapshot_city_filter=str(args.ui_snapshot_city_filter or ""),
        force_rebuild_ui_snapshot=bool(args.force_rebuild_ui_snapshot),
        require_ui_visible=bool(args.require_ui_visible),
        lineage_decision_manifest=lineage_decision_manifest,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
