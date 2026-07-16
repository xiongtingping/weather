"""Frozen authorization contract for the sole production probability writer.

The contract is intentionally independent of trading activation.  It grants
only Transaction-B public-posterior persistence; no caller may infer order or
CLOB permission from it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "production_probability_runtime_activation_v2"
DEFAULT_PATH = Path(__file__).resolve().parent / "config" / "production_probability_runtime_activation.json"
BAYES_FORMULA_ID = "p0_prior_x_weak_once_then_canonical_event_raw_log_lr_then_weak_revision_replacement_log_lr_v1"
COMPOSER_SCHEMA = "sequential_bayes_filter_v3"
CHECKPOINT_SCHEMA = "posterior_checkpoint_v2"
STRONG_UPDATE_SCHEMA = "strong_observation_update_v3"
PUBLIC_SSOT_SCHEMA = "sequential_temperature_posterior_ssot_v2"


class ProductionProbabilityActivationError(ValueError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def activation_contract_sha256(value: Mapping[str, Any]) -> str:
    payload = {
        key: item for key, item in _mapping(value).items()
        if key != "activation_contract_sha256"
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _sha256_text(value: Any) -> bool:
    text = _text(value).lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def validate_production_probability_activation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return an enabled frozen contract or fail before any DB mutation."""

    contract = dict(_mapping(value))
    hash_fields = (
        "prior_model_artifact_sha256",
        "weak_likelihood_artifact_sha256",
        "strong_lr_artifact_sha256",
        "runtime_manifest_sha256",
        "legacy_writer_retirement_receipt_sha256",
    )
    missing = [
        key for key in ("cutover_approval_id", "activation_effective_at", *hash_fields)
        if not _text(contract.get(key))
    ]
    if _text(contract.get("schema")) != SCHEMA_VERSION:
        raise ProductionProbabilityActivationError("production_probability_activation_schema_invalid")
    if _text(contract.get("state")) != "production_enabled":
        raise ProductionProbabilityActivationError("production_probability_activation_not_enabled")
    required_true = (
        "production_transaction_b_authorized", "unique_probability_mainline",
        "legacy_probability_writer_disabled", "no_order_validation_accepted",
    )
    false_flags = [key for key in required_true if contract.get(key) is not True]
    invalid_hashes = [key for key in hash_fields if _text(contract.get(key)) and not _sha256_text(contract.get(key))]
    try:
        effective_at = datetime.fromisoformat(_text(contract.get("activation_effective_at")).replace("Z", "+00:00"))
        effective_at_valid = effective_at.tzinfo is not None
    except ValueError:
        effective_at_valid = False
    exact_fields = {
        "bayes_formula_id": BAYES_FORMULA_ID,
        "composer_schema": COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
    }
    identity_mismatches = [key for key, expected in exact_fields.items() if _text(contract.get(key)) != expected]
    if missing or false_flags or invalid_hashes or identity_mismatches:
        detail = ",".join([
            *(f"missing_{key}" for key in missing),
            *(f"required_{key}" for key in false_flags),
            *(f"invalid_sha256_{key}" for key in invalid_hashes),
            *(() if effective_at_valid else ("activation_effective_at_invalid",)),
            *(f"identity_mismatch_{key}" for key in identity_mismatches),
        ])
        raise ProductionProbabilityActivationError("production_probability_activation_incomplete:" + detail)
    if contract.get("places_orders") is not False or contract.get("authorizes_clob") is not False:
        raise ProductionProbabilityActivationError("production_probability_activation_must_not_authorize_orders")
    contract["activation_contract_sha256"] = activation_contract_sha256(contract)
    return contract


def load_production_probability_activation(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        parsed = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProductionProbabilityActivationError(
            f"production_probability_activation_unreadable:{type(exc).__name__}"
        ) from exc
    return validate_production_probability_activation(_mapping(parsed))


__all__ = [
    "BAYES_FORMULA_ID", "CHECKPOINT_SCHEMA", "COMPOSER_SCHEMA", "DEFAULT_PATH",
    "PUBLIC_SSOT_SCHEMA", "SCHEMA_VERSION", "STRONG_UPDATE_SCHEMA",
    "ProductionProbabilityActivationError",
    "activation_contract_sha256", "load_production_probability_activation",
    "validate_production_probability_activation",
]
