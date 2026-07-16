#!/usr/bin/env python3
"""Persist one immutable, complete official-NWP daily-peak revision.

Default mode is read-only.  ``--apply`` writes only a content-addressed
revision below the isolated official curve root; it never writes archive,
posterior, decision, or order state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_daily_curve_plan_runtime import build_official_nwp_daily_curve_plan  # noqa: E402
from official_nwp_daily_peak_revision_runtime import build_official_nwp_daily_peak_revision  # noqa: E402


CONFIRMATION_PHRASE = "PERSIST_OFFICIAL_NWP_DAILY_PEAK_REVISION"


def _load_rows(history_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for path in sorted(history_dir.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            unreadable.append(str(path))
            continue
        if isinstance(item, dict):
            rows.append(item)
        else:
            unreadable.append(str(path))
    return rows, unreadable


def _atomic_immutable_json(path: Path, value: dict[str, Any]) -> tuple[bool, str]:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == rendered:
                return True, "idempotent"
        except OSError:
            pass
        return False, "official_nwp_daily_peak_revision_identity_collision"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True, "persisted"


def build_contract(*, provider: str, run_at_utc: str, target_event_date: str, curve_root: Path, contract_path: Path) -> dict[str, Any]:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    plan = build_official_nwp_daily_curve_plan(
        contract=contract, provider_id=provider, run_at_utc=run_at_utc, target_event_date=target_event_date
    )
    history_dir = curve_root / provider / "history"
    rows, unreadable = _load_rows(history_dir) if history_dir.is_dir() else ([], [str(history_dir)])
    revision = build_official_nwp_daily_peak_revision(
        provider_id=provider,
        point_snapshots=rows,
        target_event_date=target_event_date,
        target_timezone=str(contract.get("target_timezone") or ""),
        expected_valid_times_utc=list(plan.get("expected_valid_times_utc") or []),
    ) if plan.get("ok") is True else {"ok": False, "state": "blocked", "block_reasons": list(plan.get("blockers") or [])}
    revision_id = str(revision.get("source_revision_id") or "")
    output_path = curve_root / provider / "daily_peak_revisions" / f"{revision_id}.json" if revision_id else None
    return {
        "schema": "official_nwp_daily_peak_revision_persist_contract_v1",
        "ok": revision.get("ok") is True and not unreadable,
        "state": "ready_to_persist" if revision.get("ok") is True and not unreadable else "blocked",
        "plan": plan,
        "revision": revision,
        "history_dir": str(history_dir.resolve()),
        "history_unreadable": unreadable,
        "output_path": str(output_path.resolve()) if output_path else "",
        "confirmation_phrase_required": CONFIRMATION_PHRASE,
        "boundary": {"writes_archive_db": False, "writes_posterior": False, "places_orders": False},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=("ecmwf", "icon", "gfs", "jma"))
    parser.add_argument("--run-at-utc", required=True)
    parser.add_argument("--target-event-date", required=True)
    parser.add_argument("--curve-root", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_curve_sources_v2")
    parser.add_argument("--contract", type=Path, default=ROOT / "config" / "forecast_sources" / "zuuu_official_daily_curve_run_contract_v1.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    result = build_contract(provider=args.provider, run_at_utc=args.run_at_utc, target_event_date=args.target_event_date, curve_root=args.curve_root.expanduser().resolve(), contract_path=args.contract.expanduser().resolve())
    if args.apply:
        if args.confirm != CONFIRMATION_PHRASE:
            result.update({"ok": False, "state": "blocked", "blockers": ["confirmation_phrase_required"]})
        elif result.get("ok") is not True:
            result["blockers"] = list(result.get("revision", {}).get("block_reasons") or result.get("history_unreadable") or ["official_nwp_daily_peak_revision_not_ready"])
        else:
            ok, action = _atomic_immutable_json(Path(result["output_path"]), dict(result["revision"]))
            result.update({"ok": ok, "state": action if ok else "blocked", "persistence_action": action})
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
