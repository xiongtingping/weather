#!/usr/bin/env python3
"""One read-only status surface for the weather-system rebuild.

This audit distinguishes structural readiness from prospective evidence
accumulation.  In particular, strict-as-of/OOS/final-label gaps are promotion
evidence blockers, never a reason to discard a live AMSC source fact.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_prewarm_readiness import audit_official_nwp_prewarm_readiness  # noqa: E402
from p10_shadow_preparation_readiness import audit_p10_shadow_preparation_readiness  # noqa: E402
from resolution_final_label_shadow_registry_runtime import build_resolution_final_label_shadow_registry_audit  # noqa: E402
from resolution_final_source_runtime import PHYSICAL_ROLE, PHYSICAL_SOURCE_ID  # noqa: E402
from resolution_final_source_shadow_writer import SOURCE_TABLE  # noqa: E402
from scripts.production_trade_gate_zero_audit import run_audit as production_trade_gate_zero_audit  # noqa: E402


SCHEMA_VERSION = "weather_rebuild_readiness_audit_v1"


def _curve_history_counts(root: Path) -> dict[str, int]:
    return {provider: len(list((root / provider / "history").glob("*.json"))) for provider in ("ecmwf", "icon", "gfs", "jma")}


def _physical_x_training_label_counts(path: Path) -> dict[str, Any]:
    """Count physical X truth separately from market settlement labels."""

    result = {"physical_final_x_sources": 0, "strict_asof_training_eligible_physical_x": 0, "non_strict_physical_x": 0, "unreadable_rows": 0}
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SOURCE_TABLE,)).fetchone()
            if exists is None:
                result["na_reason"] = "physical_final_source_table_missing"
                return result
            rows = conn.execute(f"SELECT observation_json FROM {SOURCE_TABLE} WHERE source_id=? AND role=?", (PHYSICAL_SOURCE_ID, PHYSICAL_ROLE)).fetchall()
    except sqlite3.Error as exc:
        result["na_reason"] = f"physical_final_source_registry_open_failed:{type(exc).__name__}"
        return result
    for (raw_json,) in rows:
        try:
            row = json.loads(str(raw_json or ""))
        except (TypeError, ValueError):
            result["unreadable_rows"] += 1
            continue
        payload = row.get("payload") if isinstance(row, dict) and isinstance(row.get("payload"), dict) else {}
        if not isinstance(row, dict) or payload.get("label_finality") != "final":
            continue
        result["physical_final_x_sources"] += 1
        if payload.get("strict_asof_training_eligible") is True:
            result["strict_asof_training_eligible_physical_x"] += 1
        else:
            result["non_strict_physical_x"] += 1
    return result


def build_weather_rebuild_readiness_audit(
    *, prewarm_cache: Path, source_catalog: Path, p10_db: Path, final_label_db: Path, curve_root: Path,
) -> dict[str, Any]:
    official = audit_official_nwp_prewarm_readiness(cache_path=prewarm_cache, catalog_path=source_catalog)
    try:
        with sqlite3.connect(f"file:{p10_db.resolve()}?mode=ro", uri=True) as conn:
            p10 = audit_p10_shadow_preparation_readiness(conn, final_label_db_path=final_label_db)
    except sqlite3.Error:
        p10 = {"state": "blocked", "blockers": ["p10_shadow_db_unreadable"], "offline_promotion_blockers": [], "strong_lr_artifact": {"runtime": {"ok": False}}}
    labels = build_resolution_final_label_shadow_registry_audit(db_path=final_label_db)
    physical_x = _physical_x_training_label_counts(final_label_db)
    trade = production_trade_gate_zero_audit()
    history = _curve_history_counts(curve_root)
    evidence_blockers: list[str] = []
    if sum(history.values()) == 0:
        evidence_blockers.append("prospective_official_curve_history_empty")
    if int((labels.get("counts") or {}).get("final_labels") or 0) == 0:
        evidence_blockers.append("forward_final_resolution_labels_empty")
    if not (p10.get("strong_lr_artifact") or {}).get("runtime", {}).get("ok"):
        evidence_blockers.append("strong_lr_artifact_not_yet_promoted")
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(trade.get("ok") and official.get("ok")),
        "state": "structural_ready_evidence_accumulating" if trade.get("ok") and official.get("ok") else "structural_blocked",
        "official_nwp_generation": {
            "state": official.get("state"),
            "blockers": official.get("blockers") or [],
            "available_provider_ids": ((official.get("generation_validation") or {}).get("bundle") or {}).get("available_provider_ids") or [],
            "missing_provider_ids": ((official.get("generation_validation") or {}).get("bundle") or {}).get("missing_provider_ids") or [],
        },
        "curve_capture": {"history_counts": history, "prospective_evidence_blockers": evidence_blockers},
        "physical_x_training_labels": {
            "state": "available_for_strict_training" if int(physical_x.get("strict_asof_training_eligible_physical_x") or 0) > 0 else "accumulating_or_non_strict_only",
            "counts": physical_x,
            "boundary": "physical_x_is_independent_of_market_finality_s_and_bridge",
        },
        "resolution_final_labels": {"state": labels.get("state"), "counts": labels.get("counts") or {}},
        "p10": {"state": p10.get("state"), "runtime_blockers": p10.get("blockers") or [], "offline_promotion_blockers": p10.get("offline_promotion_blockers") or []},
        "production_trade_gate_zero": {"ok": trade.get("ok"), "blockers": trade.get("blockers") or []},
        "boundary": {
            "read_only": True,
            "strict_asof_oos_final_labels_are_promotion_evidence_not_live_amsc_ingest_gate": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prewarm-cache", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_multimodel_zuuu_v1" / "prewarm" / "current.json")
    parser.add_argument("--source-catalog", type=Path, default=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json")
    parser.add_argument("--p10-db", type=Path, default=ROOT / "data" / "shadow" / "p10_runtime.db")
    parser.add_argument("--final-label-db", type=Path, default=ROOT / "data" / "shadow" / "resolution_final_labels.db")
    parser.add_argument("--curve-root", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_curve_sources_v2")
    args = parser.parse_args(argv)
    result = build_weather_rebuild_readiness_audit(prewarm_cache=args.prewarm_cache, source_catalog=args.source_catalog, p10_db=args.p10_db, final_label_db=args.final_label_db, curve_root=args.curve_root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
