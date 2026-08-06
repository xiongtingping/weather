"""Exact canonical product-to-independence-family registry.

Unknown products fail closed.  Adding a model lane requires an explicit row;
callers may never supply a free-form family label as authority.
"""

from __future__ import annotations

from typing import Any

from .contracts import ContractError, content_sha256
from .model_product_identity import (
    PRODUCT_ROWS,
)


REGISTRY_VERSION = "CANONICAL_MODEL_INDEPENDENCE_FAMILY_REGISTRY_V2"
_LEGACY_EXACT_ROWS = (
    {
        "model_id": "AIFS",
        "provider_id": "OPEN_METEO",
        "provider_product_id": "ecmwf_aifs025_single",
        "source_class": "aggregator_republisher",
        "independence_family_id": "ECMWF",
    },
    {
        "model_id": "ICON",
        "provider_id": "OPEN_METEO",
        "provider_product_id": "dwd_icon_global",
        "source_class": "aggregator_republisher",
        "independence_family_id": "DWD",
    },
)
REGISTRY_ROWS = (*_LEGACY_EXACT_ROWS, *(
    {
        "product_id": row["product_id"],
        "model_id": row["model_id"],
        "provider_id": row["provider_id"],
        "provider_product_id": row["provider_product_id"],
        "source_class": row["source_class"],
        "independence_family_id": row["independence_family_id"],
    }
    for row in PRODUCT_ROWS
))
_REGISTRY_EXACT_KEYS = tuple(
    (
        row["model_id"], row["provider_id"], row["provider_product_id"],
        row["source_class"],
    )
    for row in REGISTRY_ROWS
)
if len(_REGISTRY_EXACT_KEYS) != len(set(_REGISTRY_EXACT_KEYS)):
    raise RuntimeError("canonical_model_family_registry_exact_key_duplicate")
REGISTRY_CONTENT_SHA256 = content_sha256({
    "version": REGISTRY_VERSION,
    "rows": list(REGISTRY_ROWS),
})


def canonical_independence_family_id(
    *, model_id: Any, provider_id: Any, provider_product_id: Any, source_class: Any
) -> str:
    key = (
        str(model_id or "").strip().upper(),
        str(provider_id or "").strip(),
        str(provider_product_id or "").strip(),
        str(source_class or "").strip(),
    )
    matches = [
        row for row in REGISTRY_ROWS
        if (
            row["model_id"], row["provider_id"], row["provider_product_id"],
            row["source_class"],
        ) == key
    ]
    if len(matches) != 1:
        raise ContractError("canonical_model_family_registry_product_not_unique")
    return str(matches[0]["independence_family_id"])


def assert_canonical_independence_family(**values: Any) -> None:
    expected = canonical_independence_family_id(
        model_id=values.get("model_id"),
        provider_id=values.get("provider_id"),
        provider_product_id=values.get("provider_product_id"),
        source_class=values.get("source_class"),
    )
    if str(values.get("independence_family_id") or "").strip() != expected:
        raise ContractError("canonical_model_family_registry_mismatch")


__all__ = [
    "REGISTRY_CONTENT_SHA256", "REGISTRY_ROWS", "REGISTRY_VERSION",
    "assert_canonical_independence_family", "canonical_independence_family_id",
]
