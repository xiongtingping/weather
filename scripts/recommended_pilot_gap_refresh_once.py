#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_PLAN = ROOT / "data" / "reports" / "recommended_pilot_gap_scan_plan_latest.json"
DEFAULT_OUT = ROOT / "data" / "reports" / "recommended_pilot_gap_refresh_once_latest.json"
DEFAULT_HEALTH = ROOT / "data" / "reports" / "recommended_pilot_gap_refresh_health_latest.json"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _market_rows(plan: Mapping[str, Any], *, max_slugs: int) -> list[dict[str, str]]:
    by_slug: dict[str, dict[str, str]] = {}
    for item in plan.get("plan") or []:
        if not isinstance(item, Mapping):
            continue
        for market in item.get("open_markets") or []:
            if not isinstance(market, Mapping):
                continue
            slug = str(market.get("slug") or "").strip()
            event_date = str(market.get("event_date") or "").split("T", 1)[0]
            if not slug or not event_date or slug in by_slug:
                continue
            by_slug[slug] = {"slug": slug, "event_date": event_date}
    out: list[dict[str, str]] = []
    ordered_slugs = [
        str(slug or "").strip()
        for slug in plan.get("priority_slugs") or []
        if str(slug or "").strip()
    ]
    ordered_slugs.extend(slug for slug in by_slug if slug not in ordered_slugs)
    for slug in ordered_slugs:
        row = by_slug.get(slug)
        if row is None:
            continue
        out.append(row)
        if len(out) >= max(0, int(max_slugs)):
            return out
    return out


def _health_blocked_phase_map(path: Path) -> dict[str, set[str]]:
    health = _load_json(path)
    out: dict[str, set[str]] = {}
    for row in health.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        phases: set[str] = set()
        for code in row.get("blockers") or []:
            text = str(code or "")
            if text.startswith("tick_"):
                phases.add("market")
            if text.startswith("model_") or text.startswith("fact_"):
                phases.add("model")
        if phases:
            out[slug] = phases
    return out


def _apply_health_blocked_filter(
    rows: list[dict[str, str]],
    phase_map: Mapping[str, set[str]],
) -> list[dict[str, str]]:
    if not phase_map:
        return rows
    out: list[dict[str, str]] = []
    for row in rows:
        slug = str(row.get("slug") or "").strip()
        phases = set(phase_map.get(slug) or set())
        if not slug or not phases:
            continue
        enriched = dict(row)
        enriched["refresh_market"] = "1" if "market" in phases else "0"
        enriched["refresh_model"] = "1" if "model" in phases else "0"
        out.append(enriched)
    return out


def _run(cmd: list[str], *, cwd: Path, dry_run: bool, timeout_sec: int) -> dict[str, Any]:
    if dry_run:
        return {"cmd": cmd, "returncode": 0, "dry_run": True}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=max(1, int(timeout_sec or 1)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {
            "cmd": cmd,
            "returncode": 124,
            "timed_out": True,
            "timeout_sec": max(1, int(timeout_sec or 1)),
            "output_tail": str(output or "")[-4000:],
        }
    except OSError as exc:
        return {
            "cmd": cmd,
            "returncode": 127,
            "startup_error": type(exc).__name__,
            "output_tail": str(exc)[-4000:],
        }
    return {
        "cmd": cmd,
        "returncode": int(proc.returncode),
        "timeout_sec": max(1, int(timeout_sec or 1)),
        "output_tail": (proc.stdout or "")[-4000:],
    }


def build_commands(
    *,
    python_bin: str,
    rows: list[dict[str, str]],
    market_refresh: bool,
    model_refresh: bool,
    materialize_facts: bool,
    facts_date: str,
) -> list[tuple[str, str, list[str]]]:
    commands: list[tuple[str, str, list[str]]] = []
    for row in rows:
        slug = row["slug"]
        event_date = row["event_date"]
        event_url = f"https://polymarket.com/event/{slug}"
        row_market_refresh = market_refresh and str(row.get("refresh_market", "1")) != "0"
        row_model_refresh = model_refresh and str(row.get("refresh_model", "1")) != "0"
        if row_market_refresh:
            commands.append(
                (
                    slug,
                    "market_refresh",
                    [
                        python_bin,
                        "scan_weather_market.py",
                        "--event-url",
                        event_url,
                        "--date",
                        event_date,
                        "--tz",
                        "America/New_York",
                        "--market-refresh-only",
                        "--no-auto-trade",
                    ],
                )
            )
        if row_model_refresh:
            commands.append(
                (
                    slug,
                    "model_refresh",
                    [
                        python_bin,
                        "scan_weather_market.py",
                        "--event-url",
                        event_url,
                        "--date",
                        event_date,
                        "--tz",
                        "America/New_York",
                        "--model-refresh-only",
                        "--force-model-refresh",
                        "--no-auto-trade",
                    ],
                )
            )
        if row_market_refresh and row_model_refresh:
            commands.append(
                (
                    slug,
                    "post_model_market_refresh",
                    [
                        python_bin,
                        "scan_weather_market.py",
                        "--event-url",
                        event_url,
                        "--date",
                        event_date,
                        "--tz",
                        "America/New_York",
                        "--market-refresh-only",
                        "--no-auto-trade",
                    ],
                )
            )
    if materialize_facts and rows:
        commands.append(
            (
                "facts",
                "materialize_facts",
                [
                    python_bin,
                    "ui/server.py",
                    "--materialize-facts",
                    "--facts-date",
                    facts_date,
                    "--facts-days",
                    "1",
                ],
            )
        )
    return commands


def _default_facts_date() -> str:
    try:
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return datetime.utcnow().date().isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh current recommended pilot gap slugs once; no live orders."
    )
    parser.add_argument("--plan", default=str(DEFAULT_PLAN))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--health", default=str(DEFAULT_HEALTH))
    parser.add_argument("--python", default=str(ROOT / ".venv" / "bin" / "python"))
    parser.add_argument("--max-slugs", type=int, default=2)
    parser.add_argument("--market-refresh", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--model-refresh", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--materialize-facts", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--facts-date", default="")
    parser.add_argument("--command-timeout-sec", type=int, default=45)
    parser.add_argument("--only-health-blocked", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    plan = _load_json(Path(args.plan))
    rows = _market_rows(plan, max_slugs=max(0, int(args.max_slugs)))
    phase_map = (
        _health_blocked_phase_map(Path(args.health))
        if bool(args.only_health_blocked)
        else {}
    )
    rows = _apply_health_blocked_filter(rows, phase_map)
    commands = build_commands(
        python_bin=str(args.python),
        rows=rows,
        market_refresh=bool(args.market_refresh),
        model_refresh=bool(args.model_refresh),
        materialize_facts=bool(args.materialize_facts),
        facts_date=str(args.facts_date or _default_facts_date()),
    )
    results = []
    for slug, phase, cmd in commands:
        result = _run(
            cmd,
            cwd=ROOT,
            dry_run=bool(args.dry_run),
            timeout_sec=max(1, int(args.command_timeout_sec or 1)),
        )
        result["slug"] = slug
        result["phase"] = phase
        results.append(result)
    failed = [item for item in results if int(item.get("returncode") or 0) != 0]
    payload = {
        "ok": not failed,
        "read_only": False,
        "no_live_orders": True,
        "plan_path": str(Path(args.plan)),
        "health_path": str(Path(args.health)),
        "only_health_blocked": bool(args.only_health_blocked),
        "health_blocked_slug_count": len(phase_map),
        "row_count": len(rows),
        "command_count": len(commands),
        "results": results,
        "blockers": [
            f"{item.get('slug')}:{item.get('phase')}:rc{item.get('returncode')}"
            for item in failed
        ],
        "next_action": (
            "recommended_pilot_gap_refresh_once_ok"
            if not failed
            else "inspect_recommended_pilot_gap_refresh_once_failures"
        ),
    }
    out_path = Path(args.out) if str(args.out or "").strip() else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(payload, ensure_ascii=False, indent=None if args.json else 2, sort_keys=not args.json))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
