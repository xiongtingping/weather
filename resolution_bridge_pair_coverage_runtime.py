"""Read-only coverage gate for raw observed X/S bridge-calibration pairs."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from resolution_observed_x_s_pair_shadow import PAIR_TABLE, SCHEMA_VERSION as PAIR_SCHEMA
from sequential_bayes_public_ssot_shadow_writer import SHADOW_MANIFEST_STATE, SHADOW_MANIFEST_TABLE


SCHEMA_VERSION = "resolution_bridge_pair_coverage_audit_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sha256(value: Any) -> bool:
    text = _text(value).lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _pair_is_structurally_complete(value: Mapping[str, Any]) -> bool:
    """Recheck the minimum raw lineage needed for a bridge-fit sample.

    The registry writer performs the full validation at append time.  This
    read-side check is intentionally narrower: it ensures a manually damaged
    or partially restored registry can never look calibration-ready merely
    because it has many rows.
    """

    physical = value.get("physical_source")
    settlement = value.get("settlement_source")
    return (
        _sha256(value.get("target_contract_sha256"))
        and _text(value.get("authority_contract_id")) == "zuuu_amsc_wunderground_daily_high_v4"
        and _sha256(value.get("authority_contract_sha256"))
        and _text(value.get("target_resolution_source_id")) == "wunderground_zuuu_historical_observation_daily_high_final_v3"
        and bool(_text(value.get("target_identity")))
        and bool(_text(value.get("event_date"))[:10])
        and _finite(value.get("final_physical_high_c")) is not None
        and _finite(value.get("settlement_scalar_c")) is not None
        and isinstance(physical, Mapping)
        and isinstance(settlement, Mapping)
        and bool(_text(physical.get("source_observation_id")))
        and bool(_text(settlement.get("source_observation_id")))
        and _sha256(physical.get("raw_sha256"))
        and _sha256(settlement.get("raw_sha256"))
        and bool(_text(physical.get("raw_ref")))
        and bool(_text(settlement.get("raw_ref")))
    )


def _pair_uses_current_authority(value: Mapping[str, Any]) -> bool:
    return bool(
        _text(value.get("authority_contract_id")) == "zuuu_amsc_wunderground_daily_high_v4"
        and _text(value.get("target_resolution_source_id"))
        == "wunderground_zuuu_historical_observation_daily_high_final_v3"
    )


def build_resolution_bridge_pair_coverage_audit(
    *,
    db_path: str | Path,
    minimum_distinct_event_days: int = 90,
    minimum_distinct_x_values: int = 12,
    minimum_distinct_s_values: int = 12,
) -> dict[str, Any]:
    """Report calibration eligibility; never fit, close, or promote a bridge."""

    path = Path(db_path).expanduser().resolve()
    if not path.exists():
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "missing", "block_reasons": ["bridge_pair_registry_missing"], "read_only": True}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        required = (SHADOW_MANIFEST_TABLE, PAIR_TABLE)
        missing = [table for table in required if not _table_exists(conn, table)]
        sentinel = conn.execute(f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1").fetchone() if not missing else None
        rows: list[dict[str, Any]] = []
        malformed = 0
        structurally_incomplete = 0
        retired_authority_pairs = 0
        if not missing:
            for (raw,) in conn.execute(f"SELECT pair_json FROM {PAIR_TABLE} ORDER BY event_date,target_contract_sha256"):
                try:
                    value = json.loads(str(raw or ""))
                except (TypeError, ValueError):
                    malformed += 1
                    continue
                if isinstance(value, Mapping) and _text(value.get("schema")) == PAIR_SCHEMA and value.get("ok") is True and _text(value.get("state")) == "available_for_bridge_calibration_only":
                    if not _pair_uses_current_authority(value):
                        retired_authority_pairs += 1
                    elif _pair_is_structurally_complete(value):
                        rows.append(dict(value))
                    else:
                        structurally_incomplete += 1
                else:
                    malformed += 1
    blockers: list[str] = []
    if missing:
        blockers.append("bridge_pair_registry_schema_incomplete")
    if sentinel is None or _text(sentinel[0]) != SHADOW_MANIFEST_STATE:
        blockers.append("bridge_pair_registry_manifest_invalid")
    if malformed:
        blockers.append("bridge_pair_registry_row_invalid")
    if structurally_incomplete:
        blockers.append("bridge_pair_registry_pair_integrity_invalid")
    days = {_text(row.get("event_date"))[:10] for row in rows}
    x_values = {_finite(row.get("final_physical_high_c")) for row in rows}
    x_values.discard(None)
    s_values = {_finite(row.get("settlement_scalar_c")) for row in rows}
    s_values.discard(None)
    by_authority = Counter(_text(row.get("target_contract_sha256")) for row in rows)
    if len(days) < int(minimum_distinct_event_days):
        blockers.append("bridge_pair_event_day_coverage_below_minimum")
    if len(x_values) < int(minimum_distinct_x_values):
        blockers.append("bridge_pair_physical_x_support_below_minimum")
    if len(s_values) < int(minimum_distinct_s_values):
        blockers.append("bridge_pair_settlement_s_support_below_minimum")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "ready_for_calibration_review" if not blockers else "accumulating",
        "pair_count": len(rows),
        "structurally_incomplete_pair_count": structurally_incomplete,
        "retired_authority_pair_count": retired_authority_pairs,
        "distinct_event_day_count": len(days),
        "distinct_physical_x_value_count": len(x_values),
        "distinct_settlement_s_value_count": len(s_values),
        "target_contract_pair_counts": dict(sorted(by_authority.items())),
        "minimums": {
            "distinct_event_days": int(minimum_distinct_event_days),
            "distinct_physical_x_values": int(minimum_distinct_x_values),
            "distinct_settlement_s_values": int(minimum_distinct_s_values),
        },
        "block_reasons": sorted(set(blockers)),
        "boundary": {"fits_bridge": False, "closes_bridge": False, "writes_posterior": False, "places_orders": False},
        "read_only": True,
    }


__all__ = ["SCHEMA_VERSION", "build_resolution_bridge_pair_coverage_audit"]
