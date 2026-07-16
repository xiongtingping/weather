#!/usr/bin/env python3
"""Append qualified forecast captures into P1 source_observations, explicitly.

Default mode is a read-only proposal scan.  Apply mode is deliberately
separate from forecast capture, requires both confirmation and writer
quiescence, and appends only Tier-A evidence.  It never writes a posterior,
fact/latest pointer, trading decision, intent, or order.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.strict_asof_forecast_capture_readiness import _normalized_forecast  # noqa: E402
from strict_asof_forecast_capture_runtime import TIER_A, classify_strict_asof_forecast_capture  # noqa: E402
from strict_asof_forecast_p1_ingress_writer import (  # noqa: E402
    StrictForecastP1IngressError,
    persist_tier_a_forecast_p1_proposal,
    verify_strict_forecast_p1_ingress_schema,
)
from strict_asof_forecast_source_observation_runtime import (  # noqa: E402
    build_tier_a_forecast_source_observation_proposal,
)


SCHEMA_VERSION = "strict_asof_forecast_p1_ingress_v1"
CONFIRMATION_PHRASE = "APPEND_STRICT_ASOF_FORECAST_P1_INGRESS"
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_ROOT = ROOT / "data" / "research" / "forecast_source_skill" / "raw"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _read_gzip_json(path: Path) -> Mapping[str, Any] | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _raw_ref(root: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return ""
    return f"research://forecast_source_skill/raw/{relative.as_posix()}"


def collect_tier_a_forecast_p1_proposals(
    *,
    root: Path,
    station_id: str,
) -> dict[str, Any]:
    """Build P1 ingress proposals without opening a writable database."""

    base = Path(root).expanduser().resolve()
    eligible: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    unreadable: list[str] = []
    tier_counts = {"A": 0, "B": 0, "C": 0}
    for path in sorted(base.glob("**/*.json.gz")):
        envelope = _read_gzip_json(path)
        if envelope is None:
            unreadable.append(str(path))
            continue
        capture = classify_strict_asof_forecast_capture(
            raw_envelope=envelope,
            normalized_forecast=_normalized_forecast(envelope),
        )
        tier = _text(capture.get("evidence_tier"))
        if tier == TIER_A:
            tier_counts["A"] += 1
        elif tier.startswith("B_"):
            tier_counts["B"] += 1
        else:
            tier_counts["C"] += 1
        if tier != TIER_A:
            continue
        proposal = build_tier_a_forecast_source_observation_proposal(
            capture=capture,
            normalized_forecast=_normalized_forecast(envelope),
            station_id=station_id,
            raw_ref=_raw_ref(base, path),
        )
        if proposal.get("ok") is not True:
            blocked.append(
                {
                    "path": str(path),
                    "source_id": _text(capture.get("source_id")),
                    "block_reasons": list(proposal.get("block_reasons") or []),
                }
            )
            continue
        eligible.append(
            {
                "path": str(path),
                "capture_id": _text(capture.get("forecast_capture_id")),
                "source_id": _text(capture.get("source_id")),
                "event_date": _text(capture.get("event_date")),
                "proposal": proposal,
            }
        )
    return {
        "schema": SCHEMA_VERSION,
        "root": str(base),
        "station_id": _text(station_id),
        "tier_counts": tier_counts,
        "eligible": eligible,
        "blocked": blocked,
        "unreadable": unreadable,
        "read_only": True,
        "guards": {
            "writes_archive_db": False,
            "writes_research_files": False,
            "creates_posterior": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def build_ingress_contract(
    *,
    db_path: Path | str = DEFAULT_DB,
    root: Path = DEFAULT_ROOT,
    station_id: str = "ZUUU",
    apply: bool = False,
    confirm: str = "",
    writer_quiescent: bool = False,
) -> dict[str, Any]:
    """Build an explicit ingress plan; no state changes occur here."""

    scan = collect_tier_a_forecast_p1_proposals(root=Path(root), station_id=station_id)
    blockers: list[str] = []
    if not scan["eligible"]:
        blockers.append("no_tier_a_forecast_proposals_available")
    if scan["unreadable"]:
        blockers.append("forecast_capture_files_unreadable")
    db = Path(db_path).expanduser().resolve()
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            schema = verify_strict_forecast_p1_ingress_schema(conn)
    except sqlite3.Error:
        schema = {"ok": False, "state": "db_unreadable"}
    if schema.get("ok") is not True:
        blockers.append("p1_ingress_schema_not_ready")
    if apply and _text(confirm) != CONFIRMATION_PHRASE:
        blockers.append("confirmation_phrase_required")
    if apply and not writer_quiescent:
        blockers.append("writer_quiescent_assertion_required")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "ready_to_apply" if apply and not blockers else "planned" if not blockers else "blocked",
        "db": str(db),
        "apply_requested": bool(apply),
        "confirmation_phrase_required": CONFIRMATION_PHRASE,
        "writer_quiescent_required": True,
        "blockers": sorted(set(blockers)),
        "schema_verification": schema,
        "scan": scan,
        "quarantined_tier_a_capture_count": len(scan["blocked"]),
        "guards": {
            "writes_archive_db": False,
            "writes_research_files": False,
            "updates_existing_rows": False,
            "creates_posterior": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "read_only": True,
    }


def apply_ingress_contract(
    *,
    db_path: Path | str,
    root: Path,
    station_id: str,
    confirm: str,
    writer_quiescent: bool,
) -> dict[str, Any]:
    """Append all current Tier-A proposals atomically or leave no partial rows."""

    contract = build_ingress_contract(
        db_path=db_path,
        root=root,
        station_id=station_id,
        apply=True,
        confirm=confirm,
        writer_quiescent=writer_quiescent,
    )
    if contract["ok"] is not True:
        contract["execution_mode"] = "blocked"
        return contract
    db = Path(db_path).expanduser().resolve()
    results: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(db, timeout=30) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for item in contract["scan"]["eligible"]:
                result = persist_tier_a_forecast_p1_proposal(conn, item["proposal"])
                results.append({"path": item["path"], **result})
            conn.commit()
    except (sqlite3.Error, StrictForecastP1IngressError) as exc:
        contract.update(
            {
                "execution_mode": "rolled_back",
                "error": type(exc).__name__,
                "guards": {**contract["guards"], "writes_archive_db": False},
            }
        )
        return contract
    contract.update(
        {
            "ok": True,
            "state": "applied",
            "read_only": False,
            "execution_mode": "applied",
            "append_results": results,
            "guards": {**contract["guards"], "writes_archive_db": True},
        }
    )
    return contract


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--writer-quiescent", action="store_true")
    args = parser.parse_args(argv)
    if args.apply:
        result = apply_ingress_contract(
            db_path=args.db,
            root=Path(args.root),
            station_id=args.station_id,
            confirm=args.confirm,
            writer_quiescent=bool(args.writer_quiescent),
        )
    else:
        result = build_ingress_contract(
            db_path=args.db,
            root=Path(args.root),
            station_id=args.station_id,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True or not args.apply else 2


if __name__ == "__main__":
    raise SystemExit(main())
