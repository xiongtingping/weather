"""PR06 AIFS Single historical ET terminal builder.

The retained official AIFS Single run has enough native raw evidence to
describe two closed AWC dates, but neither date can become strict historical
OOS evidence: the complete 2026-08-01 curve arrived after the declared D1
decision, while the 2026-08-02 curve is missing three required native slots.

This builder records those two outcomes once per canonical AWC station-day.
It is deliberately terminal-only: it neither creates a model OOS event nor
falls back to a legacy/local-timezone curve.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping

from .awc_observation_contract import CanonicalAWCObservationContract
from .contracts import ContractError, content_sha256
from .model_product_identity import (
    PRODUCT_BY_ID,
    REGISTRY_CONTENT_SHA256,
    REGISTRY_VERSION,
)


SCHEMA = "bayes_basket_target_pr06_aifs_single_historical_terminal_bundle_v1"
TERMINAL_SCHEMA = "bayes_basket_target_pr06_aifs_single_historical_terminal_v1"
BUILDER_ID = "pr06.aifs_single.et_historical_terminal.v1"
SETTLEMENT_TIMEZONE = "America/New_York"
PRODUCT_ID = "ECMWF_AIFS_SINGLE_0P25"
PROVIDER_ID = "ecmwf_aifs025_official"

# This is an explicit historical decision/valid-time policy for the one
# retained 2026-07-31 18Z run.  It must not be derived from retrieval time,
# file mtime, or a station-local calendar.  A new run/date needs a new policy
# entry and cannot silently reuse this one.
SCOPE_POLICY: dict[str, dict[str, Any]] = {
    "2026-08-01": {
        "lead_bucket": "d1",
        "run_cycle": "18Z",
        "strict_decision_at_utc": "2026-08-01T03:00:00.000Z",
        "required_valid_time_utcs": [
            "2026-08-01T06:00:00.000Z",
            "2026-08-01T12:00:00.000Z",
            "2026-08-01T18:00:00.000Z",
            "2026-08-02T00:00:00.000Z",
        ],
        "terminal_blocker": "LINEAGE_MISMATCH",
        "terminal_diagnostic": "OFFICIAL_NATIVE_AIFS_RECEIPT_AFTER_D1_DECISION",
    },
    "2026-08-02": {
        "lead_bucket": "d1",
        "run_cycle": "18Z",
        "strict_decision_at_utc": "2026-08-02T03:00:00.000Z",
        "required_valid_time_utcs": [
            "2026-08-02T06:00:00.000Z",
            "2026-08-02T12:00:00.000Z",
            "2026-08-02T18:00:00.000Z",
            "2026-08-03T00:00:00.000Z",
        ],
        "terminal_blocker": "DATA_MISSING",
        "terminal_diagnostic": "OFFICIAL_NATIVE_AIFS_ET_SLOT_MISSING",
    },
}


def _sha(value: Any, label: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"pr06_aifs_terminal_{label}_invalid")
    return result


def _verified_hash(raw: Mapping[str, Any], label: str) -> str:
    supplied = _sha(raw.get("content_sha256"), label)
    if supplied != content_sha256({
        key: value for key, value in raw.items() if key != "content_sha256"
    }):
        raise ContractError(f"pr06_aifs_terminal_{label}_mismatch")
    return supplied


def _utc(value: Any, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"pr06_aifs_terminal_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"pr06_aifs_terminal_{label}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _seal(raw: Mapping[str, Any]) -> dict[str, Any]:
    identity = dict(raw)
    return {**identity, "content_sha256": content_sha256(identity)}


def _policy(date_text: str) -> dict[str, Any]:
    raw = SCOPE_POLICY.get(date_text)
    if raw is None:
        raise ContractError("pr06_aifs_terminal_truth_scope_unregistered")
    required = [_utc(item, "policy_valid_time") for item in raw["required_valid_time_utcs"]]
    if len(required) != 4 or len(set(required)) != 4:
        raise ContractError("pr06_aifs_terminal_policy_slot_vector_invalid")
    decision = _utc(raw["strict_decision_at_utc"], "policy_decision")
    if raw["lead_bucket"] != "d1" or raw["run_cycle"] != "18Z":
        raise ContractError("pr06_aifs_terminal_policy_scope_invalid")
    return {
        **raw,
        "strict_decision_at_utc": decision,
        "required_valid_time_utcs": required,
        "content_sha256": content_sha256({
            "event_date": date_text,
            "lead_bucket": raw["lead_bucket"],
            "run_cycle": raw["run_cycle"],
            "strict_decision_at_utc": decision,
            "required_valid_time_utcs": required,
            "terminal_blocker": raw["terminal_blocker"],
            "terminal_diagnostic": raw["terminal_diagnostic"],
        }),
    }


def _validate_capture(raw: Mapping[str, Any], *, file_sha256: str | None) -> str:
    capture_sha = _verified_hash(raw, "capture_content_sha256")
    if (
        raw.get("schema") != "bayes_basket_target_aifs_forward_capture_scheduler_v1"
        or raw.get("state") != "CAPTURED_NATIVE_FORWARD_CANDIDATE"
        or raw.get("product_id") != PRODUCT_ID
        or raw.get("provider_id") != PROVIDER_ID
        or raw.get("run_initialization_utc") != "2026-07-31T18:00:00Z"
        or raw.get("authority", {}).get("source_role") != "official_native"
        or raw.get("authority", {}).get("formal_p0_eligible_without_strict_oos")
        is not False
        or int(raw.get("blocked_count", -1)) != 0
    ):
        raise ContractError("pr06_aifs_terminal_capture_semantics_invalid")
    if file_sha256 is not None:
        _sha(file_sha256, "capture_file_sha256")
    return capture_sha


def _raw_object_index(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) != 7:
        raise ContractError("pr06_aifs_terminal_raw_object_count_invalid")
    result: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ContractError("pr06_aifs_terminal_raw_object_mapping_required")
        valid_time = _utc(item.get("valid_time_utc"), "raw_valid_time")
        received = _utc(item.get("received_at_utc"), "raw_received_at")
        published = _utc(item.get("published_at_utc"), "raw_published_at")
        receipts = item.get("transport_receipt_sha256s")
        if (
            valid_time in result
            or published > received
            or not isinstance(receipts, list)
            or len(receipts) != 1
        ):
            raise ContractError("pr06_aifs_terminal_raw_object_scope_invalid")
        result[valid_time] = {
            "valid_time_utc": valid_time,
            "raw_object_sha256": _sha(item.get("raw_message_sha256"), "raw_object_sha256"),
            "transport_receipt_sha256": _sha(receipts[0], "transport_receipt_sha256"),
            "published_at_utc": published,
            "received_at_utc": received,
        }
    return result


def _validate_decode(
    raw: Mapping[str, Any], *, capture_sha: str, capture_file_sha256: str | None
) -> tuple[str, dict[str, dict[str, Any]]]:
    decode_sha = _verified_hash(raw, "decode_content_sha256")
    capture_evidence = raw.get("capture_evidence")
    if not isinstance(capture_evidence, Mapping):
        raise ContractError("pr06_aifs_terminal_capture_evidence_missing")
    if (
        raw.get("schema") != "bayes_basket_target_aifs_station_candidate_evidence_v1"
        or raw.get("state") != "OFFICIAL_NATIVE_AIFS_SINGLE_45_STATION_CANDIDATE_COMPLETE"
        or raw.get("product_id") != PRODUCT_ID
        or raw.get("provider_id") != PROVIDER_ID
        # The source decoder's old ``model_id=ECMWF`` is retained as a source
        # label only.  Canonical model_id comes solely from PRODUCT_BY_ID.
        or raw.get("model_id") != "ECMWF"
        or raw.get("run_initialization_utc") != "2026-07-31T18:00:00Z"
        or capture_evidence.get("content_sha256") != capture_sha
        or (
            capture_file_sha256 is not None
            and capture_evidence.get("sha256") != capture_file_sha256
        )
        or raw.get("authority", {}).get("source_role") != "official_native"
        or raw.get("authority", {}).get("native_arrival_index_eligible") is not True
        or raw.get("authority", {}).get("formal_p0_eligible") is not False
        or raw.get("checks", {}).get("canonical_station_count") != 45
        or raw.get("checks", {}).get("all_station_curves_complete") is not True
    ):
        raise ContractError("pr06_aifs_terminal_decode_semantics_invalid")
    return decode_sha, _raw_object_index(raw.get("raw_objects"))


def build_pr06_aifs_single_historical_terminal_bundle(
    *,
    official_capture: Mapping[str, Any],
    official_decode: Mapping[str, Any],
    awc_contract: Mapping[str, Any],
    as_of_utc: str,
    official_capture_file_sha256: str | None = None,
) -> dict[str, Any]:
    """Return exactly one terminal for every current AWC closed-day scope."""

    cutoff = _utc(as_of_utc, "as_of_utc")
    capture_sha = _validate_capture(
        official_capture, file_sha256=official_capture_file_sha256
    )
    decode_sha, raw_by_valid_time = _validate_decode(
        official_decode,
        capture_sha=capture_sha,
        capture_file_sha256=official_capture_file_sha256,
    )
    truth = CanonicalAWCObservationContract.from_mapping(awc_contract)
    if truth.as_of_utc > cutoff:
        raise ContractError("pr06_aifs_terminal_truth_after_asof")
    product = PRODUCT_BY_ID[PRODUCT_ID]
    if (
        product["model_id"] != "AIFS"
        or product["ensemble_system_id"] is not None
        or product["model_family"] != "global_deterministic"
        or product["provider_id"] != PROVIDER_ID
        or product["source_class"] != "native_official"
    ):
        raise ContractError("pr06_aifs_terminal_product_registry_invalid")

    terminals: list[dict[str, Any]] = []
    truth_scope_keys: set[tuple[str, str]] = set()
    for bound_fact in truth.day_bounds_facts:
        bound = bound_fact.raw
        event_date = str(bound["event_date"])
        policy = _policy(event_date)
        station = str(bound["station_id"])
        key = (station, event_date)
        if key in truth_scope_keys:
            raise ContractError("pr06_aifs_terminal_truth_scope_duplicate")
        truth_scope_keys.add(key)
        if (
            bound["settlement_timezone"] != SETTLEMENT_TIMEZONE
            or bound["day_end_utc"] > cutoff
            or bound["closed_day"] is not True
        ):
            raise ContractError("pr06_aifs_terminal_truth_scope_invalid")
        required = policy["required_valid_time_utcs"]
        present = [raw_by_valid_time[item] for item in required if item in raw_by_valid_time]
        missing = [item for item in required if item not in raw_by_valid_time]
        after_decision = [
            item for item in present
            if item["received_at_utc"] > policy["strict_decision_at_utc"]
        ]
        diagnostic = str(policy["terminal_diagnostic"])
        if diagnostic == "OFFICIAL_NATIVE_AIFS_RECEIPT_AFTER_D1_DECISION":
            if missing or len(after_decision) != len(required):
                raise ContractError("pr06_aifs_terminal_receipt_after_decision_evidence_invalid")
        elif diagnostic == "OFFICIAL_NATIVE_AIFS_ET_SLOT_MISSING":
            if not missing:
                raise ContractError("pr06_aifs_terminal_slot_missing_evidence_invalid")
        else:  # pragma: no cover - protects edits to the static policy.
            raise ContractError("pr06_aifs_terminal_policy_diagnostic_invalid")
        terminals.append(_seal({
            "schema": TERMINAL_SCHEMA,
            "builder_id": BUILDER_ID,
            "as_of_utc": cutoff,
            "state": "BLOCKED",
            "release_state": "BLOCKED",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "product_id": PRODUCT_ID,
            "model_id": product["model_id"],
            "source_legacy_model_label": official_decode["model_id"],
            "ensemble_system_id": product["ensemble_system_id"],
            "model_family": product["model_family"],
            "provider_id": product["provider_id"],
            "provider_product_id": product["provider_product_id"],
            "independence_family_id": product["independence_family_id"],
            "source_class": product["source_class"],
            "run_init_utc": official_capture["run_initialization_utc"],
            "lead_bucket": policy["lead_bucket"],
            "run_cycle": policy["run_cycle"],
            "strict_decision_at_utc": policy["strict_decision_at_utc"],
            "required_valid_time_utcs": required,
            "present_raw_object_vector": present,
            "missing_valid_time_utcs": missing,
            "receipt_after_decision_vector": after_decision,
            "source_capture_content_sha256": capture_sha,
            "source_capture_file_sha256": official_capture_file_sha256,
            "source_decode_content_sha256": decode_sha,
            "truth_day_bounds_content_sha256": bound["content_sha256"],
            "truth_observation_revision_vector_sha256": bound[
                "observation_revision_vector_sha256"
            ],
            "scope_policy_content_sha256": policy["content_sha256"],
            "model_product_registry_version": REGISTRY_VERSION,
            "model_product_registry_content_sha256": REGISTRY_CONTENT_SHA256,
            "blocker": policy["terminal_blocker"],
            "diagnostic": diagnostic,
            "writes_database": False,
            "publishes_pr04_pointer": False,
            "starts_services": False,
            "releases_live_pause": False,
            "orders_submitted": 0,
            "production_no_live": True,
            "consumable": False,
        }))
    if not terminals:
        raise ContractError("pr06_aifs_terminal_truth_scope_missing")
    blockers = Counter(item["blocker"] for item in terminals)
    diagnostics = Counter(item["diagnostic"] for item in terminals)
    return _seal({
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": cutoff,
        "state": "BLOCKED",
        "release_state": "BLOCKED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "source_capture_content_sha256": capture_sha,
        "source_capture_file_sha256": official_capture_file_sha256,
        "source_decode_content_sha256": decode_sha,
        "truth_contract_content_sha256": truth.content_sha256,
        "expected_scope_count": len(terminals),
        "fact_count": 0,
        "terminal_count": len(terminals),
        "blocker_counts": dict(sorted(blockers.items())),
        "diagnostic_counts": dict(sorted(diagnostics.items())),
        "terminals": sorted(terminals, key=lambda item: (item["event_date"], item["station_id"])),
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "orders_submitted": 0,
        "production_no_live": True,
        "consumable": False,
    })


__all__ = ["BUILDER_ID", "build_pr06_aifs_single_historical_terminal_bundle"]
