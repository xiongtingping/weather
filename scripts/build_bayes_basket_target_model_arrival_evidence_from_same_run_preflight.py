#!/usr/bin/env python3
"""Build non-authoritative model-arrival evidence from a verified preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import event_day_window  # noqa: E402
from bayes_basket_target.model_arrival import load_model_arrival_receipts  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402


SCHEMA = "bayes_basket_target_model_arrival_evidence_v1"
PREFLIGHT_SCHEMA = "bayes_basket_target_same_run_retained_raw_preflight_v1"
# A same-run preflight is an explicitly scoped diagnostic.  Do not bake a
# calendar date into its defaults: an accidental default must never create or
# promote a future target day.
DEFAULT_INPUT = ROOT / "output/audits/bayes_basket_target_same_run_retained_raw_preflight.json"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_model_arrival_evidence.json"
MAX_INPUT_BYTES = 64 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _date_boundary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Classify the target day before adapting any forward evidence.

    A preflight may intentionally inspect a not-yet-started market, but that
    object is never historical input.  Persisting the classification in the
    derived evidence prevents downstream readers from treating a forward
    smoke test as a calibration-ready arrival set.
    """

    target_local_date = str(payload.get("target_local_date") or "").strip()
    target_timezone = str(payload.get("target_timezone") or "").strip()
    decision_at_utc = str(payload.get("decision_at_utc") or "").strip()
    if not target_local_date or not target_timezone or not decision_at_utc:
        raise ContractError("same_run_preflight_date_boundary_missing")
    try:
        window = event_day_window(
            target_local_date,
            as_of_utc=decision_at_utc,
            target_timezone=target_timezone,
        )
    except Exception as exc:
        raise ContractError("same_run_preflight_date_boundary_invalid") from exc
    state = str(window.get("state") or "")
    if state not in {"not_started", "open", "closed"}:
        raise ContractError("same_run_preflight_date_boundary_invalid")
    forward_only = state == "not_started"
    return {
        "target_local_date": target_local_date,
        "target_timezone": target_timezone,
        "decision_at_utc": decision_at_utc,
        "event_day_state": state,
        "forward_only": forward_only,
        "scope": "FORWARD_DIAGNOSTIC_ONLY" if forward_only else "PROSPECTIVE_EVIDENCE_ONLY",
        "historical_ssot_allowed": False if forward_only else state == "closed",
        "formal_calibration_allowed": False,
        "formal_decision_allowed": False,
        "reason": "target_settlement_day_not_started" if forward_only else "evidence_adapter_never_authorizes_formal_use",
    }


def build(*, input_path: Path, output_path: Path) -> dict[str, Any]:
    source = input_path.expanduser().resolve()
    payload = load_bounded_json(source, limit=MAX_INPUT_BYTES)
    if not isinstance(payload, Mapping) or payload.get("schema") != PREFLIGHT_SCHEMA:
        raise ContractError("same_run_preflight_schema_invalid")
    checks = payload.get("checks")
    arrivals = payload.get("model_arrivals")
    if not isinstance(checks, Mapping) or not isinstance(arrivals, list):
        raise ContractError("same_run_preflight_arrival_shape_invalid")
    date_boundary = _date_boundary(payload)
    for key in (
        "one_run_only",
        "official_provider_only",
        "raw_sha256_verified",
        "model_arrival_index_complete",
        "all_model_arrivals_prospective",
        "all_model_arrival_publication_sources_native",
    ):
        if checks.get(key) is not True:
            raise ContractError("same_run_preflight_arrival_required_check_failed")
    normalized, audit = load_model_arrival_receipts(
        [item for item in arrivals if isinstance(item, Mapping)],
        decision_at_utc=str(payload.get("decision_at_utc") or ""),
    )
    identity = {
        "schema": SCHEMA,
        "as_of_utc": payload.get("decision_at_utc"),
        "target_local_date": date_boundary["target_local_date"],
        "date_boundary": date_boundary,
        "arrivals": normalized,
        "blocked_captures": [],
        "input_artifacts": [{"path": str(source), "sha256": _sha256(source)}],
        "audit": audit,
        "checks": {
            "arrival_count_positive": bool(normalized),
            "all_arrivals_prospective": audit["all_prospective"],
            "all_publication_sources_native": bool(normalized) and all(item["publication_time_source"] == "provider_http_last_modified" for item in normalized),
            "duplicate_key_free": audit["accepted_count"] == len(normalized),
            "promotion_allowed": False,
            "production_authority": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
        "boundary": {
            "target_only": True,
            "offline_evidence_assembly": True,
            "derived_from_same_run_preflight": True,
            "writes_production_db": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
            "protected_production_line_touched": False,
        },
    }
    result = {**identity, "evidence_sha256": content_sha256(identity)}
    result["receipt_sha256"] = content_sha256(result)
    write_bounded_json(output_path.expanduser().resolve(), result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = build(input_path=args.input, output_path=args.output)
    print(json.dumps({
        "schema": result["schema"],
        "output": str(args.output.expanduser().resolve()),
        "accepted_count": result["audit"]["accepted_count"],
        "blocked_count": result["audit"]["blocked_count"],
        "promotion_allowed": result["checks"]["promotion_allowed"],
        "receipt_sha256": result["receipt_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
