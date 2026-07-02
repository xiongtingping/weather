#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = ROOT / "data" / "reports"
DEFAULT_FLOW = DEFAULT_REPORT_DIR / "settlement_validation_apply_flow_latest.json"
DEFAULT_OUT_JSON = DEFAULT_REPORT_DIR / "settlement_confirmation_packet_latest.json"
DEFAULT_OUT_MD = DEFAULT_REPORT_DIR / "settlement_confirmation_packet_latest.md"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _actual_items(flow: Mapping[str, Any]) -> list[dict[str, Any]]:
    actual = flow.get("actual_apply") if isinstance(flow.get("actual_apply"), Mapping) else {}
    review = actual.get("review_packet") if isinstance(actual.get("review_packet"), Mapping) else {}
    rows: list[dict[str, Any]] = []
    for item in review.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        rows.append(
            {
                "city": item.get("city"),
                "slug": item.get("slug"),
                "event_date": item.get("event_date"),
                "actual": item.get("actual"),
                "market_unit": item.get("market_unit"),
                "station_id": item.get("station_id"),
                "source_station_id": item.get("source_station_id"),
                "station_source_match": bool(item.get("station_source_match")),
                "resolution_source": item.get("resolution_source"),
                "review_checksum": item.get("review_checksum"),
                "apply_eligible": bool(item.get("apply_eligible")),
                "blockers": list(item.get("blockers") or []),
            }
        )
    return rows


def build_packet(flow_path: Path = DEFAULT_FLOW) -> dict[str, Any]:
    flow = _load_json(flow_path)
    actual = flow.get("actual_apply") if isinstance(flow.get("actual_apply"), Mapping) else {}
    label_plan = flow.get("label_plan") if isinstance(flow.get("label_plan"), Mapping) else {}
    simulation = (
        flow.get("after_actual_apply_simulation")
        if isinstance(flow.get("after_actual_apply_simulation"), Mapping)
        else {}
    )
    post_validation = (
        flow.get("post_label_apply_validation")
        if isinstance(flow.get("post_label_apply_validation"), Mapping)
        else {}
    )
    commands = post_validation.get("commands") if isinstance(post_validation.get("commands"), Mapping) else {}
    actual_review = (
        actual.get("review_packet") if isinstance(actual.get("review_packet"), Mapping) else {}
    )
    actual_items = _actual_items(flow)
    station_mismatch = [
        item for item in actual_items if item.get("station_source_match") is not True
    ]
    actual_ready = _as_int(actual.get("ready_count"))
    sim_labels = _as_int(simulation.get("label_planned_after_actual_apply"))
    sim_inserted = _as_int(simulation.get("label_apply_inserted_count"))
    sim_closed = bool(simulation.get("label_apply_closed_loop_ok"))
    blockers: list[str] = []
    if not flow:
        blockers.append("apply_flow_report_missing_or_unreadable")
    if actual_ready <= 0:
        blockers.append("settlement_actuals_not_ready")
    if station_mismatch:
        blockers.append("resolution_station_mismatch_in_actual_packet")
    if sim_labels <= 0:
        blockers.append("simulated_labels_after_actual_apply_missing")
    if not sim_closed:
        blockers.append("simulated_label_apply_closed_loop_not_proved")
    return {
        "ok": not blockers,
        "read_only": True,
        "generated_at": _utc_now(),
        "flow_path": str(flow_path),
        "flow_generated_at": flow.get("generated_at"),
        "actual_ready": actual_ready,
        "actual_candidate_count": _as_int(actual.get("candidate_count")),
        "would_insert_event_run_rows": _as_int(
            actual_review.get("would_insert_event_run_rows")
        ),
        "actual_items": actual_items,
        "label_plan_before_actual_apply": {
            "planned_labels": _as_int(label_plan.get("planned_labels")),
            "decisions_scanned": _as_int(label_plan.get("decisions_scanned")),
            "skipped": dict(label_plan.get("skipped") or {}),
            "settlement_skipped": dict(label_plan.get("settlement_skipped") or {}),
        },
        "after_actual_apply_simulation": {
            "actual_inserted_count": _as_int(simulation.get("actual_inserted_count")),
            "label_planned_after_actual_apply": sim_labels,
            "label_apply_inserted_count": sim_inserted,
            "label_planned_after_label_apply": _as_int(
                simulation.get("label_planned_after_label_apply")
            ),
            "label_apply_closed_loop_ok": sim_closed,
        },
        "write_safety": {
            "backup_required": True,
            "backup_reason": "apply_flow_creates_sqlite_backup_before_confirmed_apply",
            "actual_write_policy": actual_review.get("write_policy")
            or "append_only_event_runs_settlement_actual_rows",
        },
        "confirmation": {
            "actual_required_phrase": actual.get("required_confirm_apply")
            or actual_review.get("required_confirm_apply")
            or "APPLY_SETTLEMENT_ACTUALS",
            "label_required_phrase": "APPLY_SETTLEMENT_LABELS",
            "actual_only_command": actual_review.get("confirm_command")
            or ".venv/bin/python scripts/apply_settlement_validation_flow.py --since-days 42 --market-scope highest_temperature --apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS",
            "actuals_labels_validate_command": ".venv/bin/python scripts/apply_settlement_validation_flow.py --since-days 42 --market-scope highest_temperature --apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS --apply-labels --confirm-labels APPLY_SETTLEMENT_LABELS --run-post-validation",
            "post_label_validation_commands": dict(commands),
        },
        "blockers": blockers,
        "next_action": (
            "review_packet_then_confirm_actuals_labels_validate"
            if not blockers
            else "repair_settlement_confirmation_packet_inputs"
        ),
    }


def format_markdown(packet: Mapping[str, Any]) -> str:
    lines = [
        "# Settlement Confirmation Packet",
        "",
        f"- generated_at: {packet.get('generated_at')}",
        f"- ok: {bool(packet.get('ok'))}",
        f"- actual_ready: {int(packet.get('actual_ready') or 0)}",
        f"- would_insert_event_run_rows: {int(packet.get('would_insert_event_run_rows') or 0)}",
    ]
    sim = packet.get("after_actual_apply_simulation") if isinstance(packet.get("after_actual_apply_simulation"), Mapping) else {}
    lines.extend(
        [
            f"- simulated_labels_after_actual_apply: {int(sim.get('label_planned_after_actual_apply') or 0)}",
            f"- simulated_label_apply_inserted: {int(sim.get('label_apply_inserted_count') or 0)}",
            f"- simulated_closed_loop_ok: {bool(sim.get('label_apply_closed_loop_ok'))}",
            "",
            "## Actuals",
            "",
            "| city | date | actual | station | source station | source ok | checksum |",
            "|---|---|---:|---|---|---|---|",
        ]
    )
    for item in packet.get("actual_items") or []:
        if not isinstance(item, Mapping):
            continue
        actual = item.get("actual")
        unit = str(item.get("market_unit") or "")
        actual_text = f"{actual}{unit}" if actual is not None else "-"
        lines.append(
            "| {city} | {day} | {actual} | {station} | {source_station} | {ok} | {checksum} |".format(
                city=item.get("city") or "",
                day=item.get("event_date") or "",
                actual=actual_text,
                station=item.get("station_id") or "",
                source_station=item.get("source_station_id") or "",
                ok="yes" if item.get("station_source_match") is True else "no",
                checksum=item.get("review_checksum") or "",
            )
        )
    confirmation = packet.get("confirmation") if isinstance(packet.get("confirmation"), Mapping) else {}
    lines.extend(
        [
            "",
            "## Confirm Commands",
            "",
            f"- actual_only: `{confirmation.get('actual_only_command') or ''}`",
            f"- actuals_labels_validate: `{confirmation.get('actuals_labels_validate_command') or ''}`",
        ]
    )
    blockers = [str(x) for x in packet.get("blockers") or []]
    if blockers:
        lines.extend(["", "## Blockers", "", *[f"- {item}" for item in blockers]])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a read-only confirmation packet for settlement actual/label apply."
    )
    parser.add_argument("--flow", default=str(DEFAULT_FLOW))
    parser.add_argument("--out-json", default=str(DEFAULT_OUT_JSON))
    parser.add_argument("--out-md", default=str(DEFAULT_OUT_MD))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    packet = build_packet(Path(args.flow))
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(
            json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.out_md:
        out_md = Path(args.out_md)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(format_markdown(packet) + "\n", encoding="utf-8")
    print(json.dumps(packet, ensure_ascii=False, indent=None if args.json else 2, sort_keys=not args.json))
    return 0 if packet.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
