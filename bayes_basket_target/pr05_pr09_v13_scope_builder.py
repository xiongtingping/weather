"""PR05--PR09 coverage to V13 model-OOS scope terminals.

The frozen coverage matrix is an inventory, not a V13 OOS identity.  It
therefore cannot be renamed into an expected key.  This builder makes that
gap explicit once per ``station × product × lead`` coverage row.  A future
per-product owner may replace exactly one terminal only by providing every
field of the V13 identity and its historical receipt; this module never
guesses either.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .model_product_identity import build_model_product_identity_receipt
from .model_product_identity import REGISTRY_CONTENT_SHA256
from .model_system_coverage import validate_coverage_matrix
from .station_registry import coordinate_registry_manifest


SCHEMA = "bayes_basket_target_pr05_pr09_v13_model_oos_scope_bundle_v1"
TERMINAL_SCHEMA = "bayes_basket_target_pr05_pr09_v13_model_oos_scope_terminal_v1"
BUILDER_ID = "pr05-pr09.v13_model_oos_scope_builder.v1"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"

# These fields exist in the V13 event/cohort business identity but cannot be
# derived one-to-one from a v1 coverage row.  ``ensemble_system_id`` is
# nullable in the event contract, but an absent coverage value is still not a
# proof that the intended canonical value is null.
UNPROVEN_V13_IDENTITY_FIELDS = (
    "model_id",
    "ensemble_system_id",
    "provider_product_id",
    "authority_scope",
    "settlement_timezone",
    "lead_bucket_scope",
    "run_cycle_scope",
    "event_oos_protocol_version",
)
UNPROVEN_V13_EVENT_FIELDS = (
    "event_date",
    "run_init_utc",
    "forecast_decision_at_utc",
    "model_available_at_utc",
    "forecast_value_c",
    "raw_object_sha256",
    "model_input_content_sha256",
    "model_receipt",
    "truth_day_bounds_content_sha256",
)
_COVERAGE_SCOPE_FIELDS = (
    "station_id",
    "product_id",
    "provider_id",
    "model_family",
    "independence_family_id",
    "source_class",
    "lead_bucket",
    "run_cycle",
)


class PR05PR09V13ScopeError(ContractError):
    """A V13 scope cannot be declared without a one-to-one identity."""


def _utc_z(value: object, field: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PR05PR09V13ScopeError(f"pr05_pr09_v13_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PR05PR09V13ScopeError(f"pr05_pr09_v13_{field}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha(value: object, field: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise PR05PR09V13ScopeError(f"pr05_pr09_v13_{field}_invalid")
    return result


def _coverage_scope(row: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only fields already carried by this exact coverage row."""

    return {field: row[field] for field in _COVERAGE_SCOPE_FIELDS}


def _terminal(
    row: Mapping[str, Any], *, as_of_utc: str, matrix_content_sha256: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    coverage_scope = _coverage_scope(row)
    product_identity, missing_identity_fields = build_model_product_identity_receipt(
        row
    )
    identity_ready = product_identity is not None
    identity = {
        "schema": TERMINAL_SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": as_of_utc,
        "state": "BLOCKED",
        # The coverage row has no one-to-one V13 product, timezone, protocol,
        # or lead/run-scope adapter.  This is a lineage contract failure, not
        # a permission to infer a default from registry metadata.
        "blocker": "DATA_MISSING" if identity_ready else "LINEAGE_MISMATCH",
        "authority_scope": AUTHORITY_SCOPE,
        "coverage_matrix_content_sha256": matrix_content_sha256,
        "coverage_row_content_sha256": _sha(
            row.get("content_sha256"), "coverage_row_content_sha256"
        ),
        "coverage_scope": coverage_scope,
        "upstream_coverage_state": str(row["state"]),
        "upstream_coverage_blocker": str(row["blockers"][0]),
        "unproven_v13_identity_fields": list(missing_identity_fields),
        "unproven_v13_event_fields": (
            list(UNPROVEN_V13_EVENT_FIELDS) if identity_ready else []
        ),
        "model_product_identity_receipt": product_identity,
        "expected_key_emitted": identity_ready,
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    terminal = {**identity, "content_sha256": content_sha256(identity)}
    expected = None
    if product_identity is not None:
        expected = {
            key: product_identity[key]
            for key in (
                "product_id", "station_id", "model_id",
                "ensemble_system_id", "model_family",
                "provider_id", "provider_product_id", "independence_family_id",
                "source_class", "authority_scope", "settlement_timezone",
                "lead_bucket", "lead_bucket_scope", "run_cycle",
                "run_cycle_scope", "event_oos_protocol_version",
            )
        }
        expected["model_product_identity_content_sha256"] = product_identity[
            "content_sha256"
        ]
    return terminal, expected


def build_pr05_pr09_v13_scope_bundle(
    coverage_matrix: Mapping[str, Any], *, as_of_utc: str
) -> dict[str, Any]:
    """Return one exact V13 terminal per frozen coverage row.

    The only permitted output for a v1 coverage row is a terminal: V13's
    additional fields are absent by construction.  This is intentional.  It
    prevents a family-level row from silently claiming a provider product,
    settlement day, protocol, or model system that it does not carry.
    """

    matrix = validate_coverage_matrix(coverage_matrix)
    cutoff = _utc_z(as_of_utc, "as_of_utc")
    source_as_of = _utc_z(matrix["as_of_utc"], "coverage_matrix_as_of_utc")
    if cutoff != source_as_of:
        raise PR05PR09V13ScopeError("pr05_pr09_v13_as_of_mismatch")
    registry = coordinate_registry_manifest()
    station_ids = list(registry["station_ids"])
    if len(station_ids) != 45 or station_ids != sorted(set(station_ids)):
        raise PR05PR09V13ScopeError("pr05_pr09_v13_registry_not_exact_45")

    matrix_sha = _sha(matrix["content_sha256"], "coverage_matrix_content_sha256")
    pairs = [
        _terminal(row, as_of_utc=cutoff, matrix_content_sha256=matrix_sha)
        for row in matrix["rows"]
    ]
    terminals = [item[0] for item in pairs]
    expected_scopes = [item[1] for item in pairs if item[1] is not None]
    terminals.sort(
        key=lambda item: json.dumps(
            item["coverage_scope"], ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
    )
    expected_scopes.sort(
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    )
    terminal_tokens = [item["content_sha256"] for item in terminals]
    coverage_tokens = [item["coverage_row_content_sha256"] for item in terminals]
    if len(terminal_tokens) != len(set(terminal_tokens)):
        raise PR05PR09V13ScopeError("pr05_pr09_v13_terminal_duplicate")
    if len(coverage_tokens) != len(set(coverage_tokens)):
        raise PR05PR09V13ScopeError("pr05_pr09_v13_coverage_receipt_duplicate")
    if len(terminals) != int(matrix["expected_scope_count"]):
        raise PR05PR09V13ScopeError("pr05_pr09_v13_terminal_count_mismatch")
    expected_scope_tokens = [content_sha256(item) for item in expected_scopes]
    if len(expected_scope_tokens) != len(set(expected_scope_tokens)):
        raise PR05PR09V13ScopeError("pr05_pr09_v13_expected_scope_duplicate")

    identity = {
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": cutoff,
        "state": "BLOCKED",
        "release_state": "BLOCKED",
        "authority_scope": AUTHORITY_SCOPE,
        "coverage_matrix_content_sha256": matrix_sha,
        "canonical_registry_sha256": _sha(
            registry["registry_sha256"], "registry_content_sha256"
        ),
        "canonical_product_registry_content_sha256": _sha(
            REGISTRY_CONTENT_SHA256, "product_registry_content_sha256"
        ),
        "station_ids": station_ids,
        "coverage_scope_count": len(matrix["rows"]),
        "v13_expected_scope_count": len(expected_scopes),
        "v13_expected_scopes": expected_scopes,
        "identity_ready_event_data_missing_count": len(expected_scopes),
        "lineage_mismatch_count": len(terminals) - len(expected_scopes),
        "terminal_scope_count": len(terminals),
        "terminal_scopes": terminals,
        "unresolved_input_receipts": terminal_tokens,
        "future_data_counts_as_oos": False,
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "production_no_live": True,
        "consumable": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "AUTHORITY_SCOPE",
    "BUILDER_ID",
    "PR05PR09V13ScopeError",
    "SCHEMA",
    "TERMINAL_SCHEMA",
    "UNPROVEN_V13_IDENTITY_FIELDS",
    "build_pr05_pr09_v13_scope_bundle",
]
