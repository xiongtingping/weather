"""Frozen authorization identity for the report-defined-X successor epoch."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "production_probability_runtime_activation_v3_reported_x"
DEFAULT_PATH = (
    Path(__file__).resolve().parent
    / "config"
    / "production_probability_runtime_activation.reported_x_candidate.json"
)
PRODUCTION_PATH = (
    Path(__file__).resolve().parent
    / "config"
    / "production_probability_runtime_activation.json"
)
BAYES_FORMULA_ID = (
    "p0_prior_z_weak_once_then_canonical_cumulative_evidence_replacement_delta_log_lr_"
    "then_weak_revision_replacement_then_reported_x_projection_v3"
)
COMPOSER_SCHEMA = "sequential_bayes_filter_reported_x_v5_cumulative_replacement"
CHECKPOINT_SCHEMA = "posterior_checkpoint_v3_reported_x"
STRONG_UPDATE_SCHEMA = "strong_observation_update_v5_cumulative_replacement_speci_soft"
PUBLIC_SSOT_SCHEMA = "sequential_temperature_posterior_ssot_v3_reported_x"
PROJECTION_SCHEMA = "reported_physical_x_projection_v1"
LATENT_TYPE = "continuous_scheduled_metar_slot_daily_high_z_c"
TARGET_TYPE = "amsc_scheduled_regular_metar_reported_daily_max_c"


class SuccessorActivationError(ValueError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def activation_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical(
            {key: item for key, item in _mapping(value).items() if key != "activation_contract_sha256"}
        ).encode("utf-8")
    ).hexdigest()


def _sha256(value: Any) -> bool:
    text = _text(value).lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def validate_successor_activation(value: Mapping[str, Any]) -> dict[str, Any]:
    contract = dict(_mapping(value))
    exact = {
        "schema": SCHEMA_VERSION,
        "state": "production_enabled",
        "bayes_formula_id": BAYES_FORMULA_ID,
        "composer_schema": COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
        "projection_schema": PROJECTION_SCHEMA,
        "latent_type": LATENT_TYPE,
        "target_type": TARGET_TYPE,
    }
    issues = [f"identity_mismatch_{key}" for key, expected in exact.items() if _text(contract.get(key)) != expected]
    required_true = (
        "production_transaction_b_authorized",
        "unique_probability_mainline",
        "legacy_probability_writer_disabled",
        "no_order_validation_accepted",
        "speci_soft_evidence_only",
        "regular_metar_only_x_hard_bound",
        "bridge_consumes_reported_x_only",
        "public_ssot_exposes_reported_x_only",
    )
    issues.extend(f"required_{key}" for key in required_true if contract.get(key) is not True)
    hash_fields = (
        "prior_model_artifact_sha256",
        "weak_likelihood_artifact_sha256",
        "strong_lr_artifact_sha256",
        "runtime_manifest_sha256",
        "legacy_writer_retirement_receipt_sha256",
    )
    issues.extend(f"invalid_sha256_{key}" for key in hash_fields if not _sha256(contract.get(key)))
    try:
        effective = datetime.fromisoformat(_text(contract.get("activation_effective_at")).replace("Z", "+00:00"))
        if effective.tzinfo is None:
            issues.append("activation_effective_at_invalid")
    except ValueError:
        issues.append("activation_effective_at_invalid")
    if not _text(contract.get("cutover_approval_id")):
        issues.append("cutover_approval_id_missing")
    if contract.get("places_orders") is not False or contract.get("authorizes_clob") is not False:
        issues.append("activation_must_not_authorize_orders")
    if issues:
        raise SuccessorActivationError(
            "production_probability_successor_activation_invalid:" + ",".join(sorted(set(issues)))
        )
    contract["activation_contract_sha256"] = activation_sha256(contract)
    return contract


def load_successor_activation(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SuccessorActivationError(
            f"production_probability_successor_activation_unreadable:{type(exc).__name__}"
        ) from exc
    return validate_successor_activation(_mapping(value))


__all__ = [
    "BAYES_FORMULA_ID",
    "CHECKPOINT_SCHEMA",
    "COMPOSER_SCHEMA",
    "DEFAULT_PATH",
    "LATENT_TYPE",
    "PROJECTION_SCHEMA",
    "PRODUCTION_PATH",
    "PUBLIC_SSOT_SCHEMA",
    "SCHEMA_VERSION",
    "STRONG_UPDATE_SCHEMA",
    "SuccessorActivationError",
    "TARGET_TYPE",
    "activation_sha256",
    "load_successor_activation",
    "validate_successor_activation",
]
