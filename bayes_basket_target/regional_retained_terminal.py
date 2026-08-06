"""Seal retained regional Open-Meteo evidence as canonical scope terminals.

The retained 28-day packets prove that forecast values and raw objects exist.
They do not contain the V13 historical availability/decision receipt required
to create a ``model_oos_event``.  This builder therefore binds the packets to
the existing canonical product identity and emits only ``LINEAGE_MISMATCH``
terminals.  It never adapts local-day maxima, invents arrival clocks, or writes
SSOT.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .model_product_identity import PRODUCT_BY_ID, REGISTRY_CONTENT_SHA256
from .resource_bounds import MAX_JSON_INPUT_BYTES, load_bounded_json


SCHEMA = "bayes_basket_target_pr05_pr09_regional_retained_terminal_bundle_v1"
TERMINAL_SCHEMA = "bayes_basket_target_regional_retained_scope_terminal_v1"
EVIDENCE_SCHEMA = "bayes_basket_target_historical_regional_exact_run_evidence_v1"
COVERAGE_SCHEMA = "bayes_basket_target_pr05_pr09_v13_model_oos_scope_bundle_v1"
BUILDER_ID = "pr05-pr09.regional_retained_terminal.v1"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
STATE = "MATERIALIZED_UNPUBLISHED"


class RegionalRetainedTerminalError(ContractError):
    """Retained regional evidence cannot be bound without guessing."""


def _utc(value: Any, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RegionalRetainedTerminalError(f"regional_retained_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RegionalRetainedTerminalError(
            f"regional_retained_{label}_timezone_required"
        )
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _verified(raw: Mapping[str, Any], label: str) -> str:
    supplied = str(raw.get("content_sha256") or "").lower()
    if supplied != content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    ):
        raise RegionalRetainedTerminalError(
            f"regional_retained_{label}_content_sha256_mismatch"
        )
    return supplied


def _file_sha(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > MAX_JSON_INPUT_BYTES:
        raise RegionalRetainedTerminalError("regional_retained_raw_object_invalid")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_regional_retained_terminal_bundle(
    *, as_of_utc: str, coverage_scope_path: Path | str,
    evidence_paths: Sequence[Path | str],
) -> dict[str, Any]:
    cutoff = _utc(as_of_utc, "as_of_utc")
    coverage_path = Path(coverage_scope_path).expanduser().resolve(strict=True)
    coverage = load_bounded_json(coverage_path)
    if not isinstance(coverage, Mapping):
        raise RegionalRetainedTerminalError("regional_retained_coverage_mapping_required")
    coverage = dict(coverage)
    coverage_sha = _verified(coverage, "coverage")
    if (
        coverage.get("schema") != COVERAGE_SCHEMA
        or coverage.get("canonical_product_registry_content_sha256")
        != REGISTRY_CONTENT_SHA256
        or coverage.get("authority_scope") != AUTHORITY_SCOPE
        or coverage.get("release_state") != "BLOCKED"
        or coverage.get("consumable") is not False
        or _utc(coverage.get("as_of_utc"), "coverage_as_of_utc") > cutoff
    ):
        raise RegionalRetainedTerminalError("regional_retained_coverage_semantics_invalid")

    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in coverage.get("v13_expected_scopes") or []:
        if raw.get("model_family") != "regional" or raw.get("lead_bucket") != "d1":
            continue
        key = (str(raw.get("product_id") or ""), str(raw.get("station_id") or ""))
        if key in expected:
            raise RegionalRetainedTerminalError("regional_retained_expected_scope_duplicate")
        expected[key] = dict(raw)

    terminals: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_products: set[str] = set()
    for supplied in evidence_paths:
        source_path = Path(supplied).expanduser().resolve(strict=True)
        raw = load_bounded_json(source_path)
        if not isinstance(raw, Mapping):
            raise RegionalRetainedTerminalError("regional_retained_evidence_mapping_required")
        evidence = dict(raw)
        evidence_sha = _verified(evidence, "evidence")
        product_id = str(evidence.get("model_id") or "")
        product = PRODUCT_BY_ID.get(product_id)
        source = evidence.get("source")
        rows = evidence.get("model_rows")
        inputs = evidence.get("inputs")
        if (
            product_id in seen_products
            or product is None
            or product.get("model_family") != "regional"
            or evidence.get("schema") != EVIDENCE_SCHEMA
            or evidence.get("provider_id") != product.get("provider_product_id")
            or not isinstance(source, Mapping)
            or source.get("source_class") != "aggregator_republisher"
            or source.get("source_url")
            != "https://single-runs-api.open-meteo.com/v1/forecast"
            or source.get("run_day_offset") != 1
            or source.get("native_publication_time_available") is not False
            or not isinstance(rows, list)
            or not isinstance(inputs, list)
            or _utc(evidence.get("as_of_utc"), "evidence_as_of_utc") > cutoff
        ):
            raise RegionalRetainedTerminalError("regional_retained_evidence_semantics_invalid")
        seen_products.add(product_id)

        raw_shas: set[str] = set()
        for item in inputs:
            raw_path = Path(str(item.get("raw_object_ref") or "")).expanduser().resolve(
                strict=True
            )
            supplied_sha = str(item.get("raw_message_sha256") or "").lower()
            if supplied_sha != _file_sha(raw_path):
                raise RegionalRetainedTerminalError("regional_retained_raw_hash_mismatch")
            raw_shas.add(supplied_sha)

        rows_by_station: dict[str, list[Mapping[str, Any]]] = {}
        for item in rows:
            station = str(item.get("station_id") or "").upper()
            if (
                item.get("model_id") != product_id
                or item.get("provider_id") != product.get("provider_product_id")
                or item.get("source_class") != "aggregator_republisher"
                or item.get("native_arrival_claimed") is not False
                or item.get("native_publication_claimed") is not False
                or item.get("future_data_used") is not False
                or str(item.get("raw_message_sha256") or "").lower() not in raw_shas
            ):
                raise RegionalRetainedTerminalError("regional_retained_row_semantics_invalid")
            rows_by_station.setdefault(station, []).append(item)

        sources.append({
            "product_id": product_id,
            "source_evidence_content_sha256": evidence_sha,
            "retained_row_count": len(rows),
            "retained_raw_object_count": len(raw_shas),
        })
        for station, station_rows in rows_by_station.items():
            scope = expected.get((product_id, station))
            if scope is None:
                raise RegionalRetainedTerminalError("regional_retained_canonical_scope_missing")
            event_dates = sorted({str(item.get("event_date") or "") for item in station_rows})
            terminal_identity = {
                "schema": TERMINAL_SCHEMA,
                **{
                    field: scope[field]
                    for field in (
                        "station_id", "model_id", "ensemble_system_id", "model_family",
                        "provider_id", "provider_product_id", "independence_family_id",
                        "source_class", "authority_scope", "settlement_timezone",
                        "lead_bucket", "lead_bucket_scope", "run_cycle",
                        "run_cycle_scope", "event_oos_protocol_version",
                    )
                },
                "product_id": product_id,
                "model_product_identity_content_sha256": scope[
                    "model_product_identity_content_sha256"
                ],
                "source_evidence_content_sha256": evidence_sha,
                "retained_event_dates": event_dates,
                "retained_row_count": len(station_rows),
                "blocker": "LINEAGE_MISMATCH",
                "diagnostic": (
                    "retained_raw_exists_but_v13_model_available_at_decision_receipt_"
                    "and_et_day_contract_are_missing"
                ),
                "missing_canonical_fields": [
                    "forecast_decision_at_utc", "model_available_at_utc",
                    "availability_clock_source", "historical_asof_basis_sha256",
                ],
                "state": "BLOCKED",
                "release_state": "BLOCKED",
                "consumable": False,
            }
            terminals.append({
                **terminal_identity,
                "content_sha256": content_sha256(terminal_identity),
            })

    terminals.sort(key=lambda item: (item["product_id"], item["station_id"]))
    sources.sort(key=lambda item: item["product_id"])
    identity = {
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": cutoff,
        "state": STATE,
        "release_state": "BLOCKED",
        "authority_scope": AUTHORITY_SCOPE,
        "canonical_product_registry_content_sha256": REGISTRY_CONTENT_SHA256,
        "coverage_scope_content_sha256": coverage_sha,
        "source_artifacts": sources,
        "terminal_count": len(terminals),
        "terminals": terminals,
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "future_data_counts_as_oos": False,
        "consumable": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "AUTHORITY_SCOPE", "BUILDER_ID", "RegionalRetainedTerminalError", "SCHEMA",
    "TERMINAL_SCHEMA", "build_regional_retained_terminal_bundle",
]
