"""L1 authority for a sample-free, soft X→S→B production projection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "resolution_operational_projection_authority_audit_v1"
CONTRACT_SCHEMA = "resolution_operational_projection_authority_contract_v1"
CONTRACT_ID = "zuuu_x_s_soft_projection_operational_v1"
SOURCE_AUTHORITY_CONTRACT_ID = "zuuu_amsc_wunderground_daily_high_v4"
DEFAULT_PATH = Path(__file__).resolve().parent / "config/resolution_authorities/zuuu_x_s_soft_projection_operational_v1.json"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def contract_sha256(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_operational_projection_authority(value: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(_mapping(value))
    scope = _mapping(record.get("scope"))
    prohibitions = _mapping(record.get("prohibitions"))
    baseline = _mapping(record.get("operational_baseline"))
    failure = _mapping(record.get("failure_boundary"))
    blockers: list[str] = []
    if _text(record.get("schema")) != CONTRACT_SCHEMA:
        blockers.append("operational_projection_authority_schema_invalid")
    if _text(record.get("contract_id")) != CONTRACT_ID:
        blockers.append("operational_projection_authority_id_invalid")
    if _text(record.get("authority_state")) != "production_soft_projection_accepted":
        blockers.append("operational_projection_authority_state_invalid")
    if _text(record.get("source_authority_contract_id")) != SOURCE_AUTHORITY_CONTRACT_ID:
        blockers.append("operational_projection_source_authority_invalid")
    required_true = {
        "sample_free_operational_kernel_allowed": scope,
        "soft_settlement_distribution_allowed": scope,
        "soft_market_bucket_probability_allowed": scope,
        "production_ssot_projection_allowed": scope,
        "eligible_for_separately_approved_yes_strategy": scope,
        "calibrated_successor_replaces_baseline_atomically": scope,
        "does_not_change_physical_x": prohibitions,
        "does_not_claim_calibration": prohibitions,
        "does_not_authorize_hard_residual_envelope": prohibitions,
        "does_not_authorize_settlement_exclusion_certificate": prohibitions,
        "does_not_directly_authorize_orders_or_clob": prohibitions,
        "does_not_use_market_price_ev_edge_or_lcb": prohibitions,
        "amsc_metar_speci_ingress_continues": failure,
        "market_projection_fails_closed": failure,
    }
    for field, owner in required_true.items():
        if owner.get(field) is not True:
            blockers.append(f"operational_projection_authority_{field}_required")
    if scope.get("sample_or_oos_required_for_live_projection") is not False:
        blockers.append("operational_projection_samples_must_not_gate_live")
    if failure.get("bridge_or_context_failure_blocks_physical_x") is not False:
        blockers.append("operational_projection_must_not_block_physical_x")
    if (
        _text(baseline.get("kernel_kind"))
        != "same_station_sample_free_operational_residual_prior_v1"
        or baseline.get("sample_count") != 0
        or _text(baseline.get("residual_support_semantics"))
        != "probabilistic_support_not_authority_hard_envelope"
        or baseline.get("hard_exclusion_certificates_authorized") is not False
    ):
        blockers.append("operational_projection_baseline_contract_invalid")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "accepted" if not blockers else "blocked",
        "contract_id": _text(record.get("contract_id")),
        "contract_sha256": contract_sha256(record),
        "blockers": sorted(set(blockers)),
        "soft_projection_authorized": not blockers,
        "hard_exclusion_authorized": False,
        "direct_order_authorized": False,
        "sample_or_oos_live_gate": False,
        "blocks_physical_x": False,
        "read_only": True,
        "places_orders": False,
    }


def load_operational_projection_authority(path: Path | str = DEFAULT_PATH) -> tuple[dict[str, Any], dict[str, Any]]:
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(record, Mapping):
        raise ValueError("operational_projection_authority_not_mapping")
    validation = validate_operational_projection_authority(record)
    if validation["ok"] is not True:
        raise ValueError("operational_projection_authority_invalid:" + ",".join(validation["blockers"]))
    return dict(record), validation


__all__ = [
    "CONTRACT_ID", "CONTRACT_SCHEMA", "DEFAULT_PATH", "SCHEMA_VERSION",
    "SOURCE_AUTHORITY_CONTRACT_ID", "contract_sha256",
    "load_operational_projection_authority", "validate_operational_projection_authority",
]
