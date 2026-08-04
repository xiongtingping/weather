#!/usr/bin/env python3
"""Build an append-only blocker ledger for native activation repair.

This is deliberately a read-only evidence join.  It does not recalculate an
OOS score, mutate the target database, alter an immutable manifest, fetch raw
data, create an artifact, or create an activation.  Its purpose is to turn the
small set of repair-plan blockers into an actionable ledger without treating a
bootstrap/forward receipt as historical strict OOS evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    _candidate_cycle_mapping,
    _selection_identity,
)


SCHEMA = "native_ensemble_activation_blocker_ledger_v1"
MISSING = "BLOCKED_MISSING_EXACT_EVALUATION"
REJECTED = "BLOCKED_LATEST_REJECTED"
PASS = "PRODUCTION_NO_LIVE_OOS_PASS"
BOOTSTRAP_PROFILES = {
    "PRODUCTION_NO_LIVE_BOOTSTRAP_2_TRAIN_1_FORWARD_HOLDOUT",
    "PRODUCTION_NO_LIVE_BOOTSTRAP_2_TRAIN_1_HISTORICAL_HOLDOUT",
}
FORWARD_CLASSES = {"FORWARD_LOCAL_RECEIPT", "FORWARD_OBSERVATION"}
HISTORICAL_SCOPE = "CLOSED_HISTORICAL_AWC_TRUTH"


def _load(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"json_object_required:{path}")
    expected = content_sha256({k: v for k, v in payload.items() if k != "content_sha256"})
    if str(payload.get("content_sha256") or "") != expected:
        raise ValueError(f"content_sha256_mismatch:{path}")
    return payload


def _ids(raw: Any) -> tuple[str, ...]:
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        value = []
    return tuple(sorted(str(item) for item in value)) if isinstance(value, list) else ()


def _candidate_signature(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(candidate.get("station_id") or "").upper(),
        str(candidate.get("season_bin") or ""),
        str(candidate.get("lead_bucket") or ""),
        str(candidate.get("run_cycle") or ""),
        tuple(sorted(str(item) for item in candidate.get("selected_system_ids") or [])),
        str(candidate.get("settlement_definition") or ""),
    )


def _activation_signature(item: Mapping[str, Any]) -> tuple[Any, ...]:
    scope = item.get("scope") or {}
    return (
        str(item.get("station_id") or scope.get("station_id") or "").upper(),
        str(scope.get("season_bin") or ""),
        str(scope.get("lead_bucket") or ""),
        str(scope.get("run_cycle") or ""),
        _ids(scope.get("ensemble_system_ids_json")),
        str(scope.get("settlement_definition") or ""),
    )


def _source_facts(candidate: Mapping[str, Any]) -> dict[str, Any]:
    evaluations = candidate.get("system_evaluations")
    evaluations = evaluations if isinstance(evaluations, Mapping) else {}
    classes = sorted({
        str(value.get("holdout_source_class") or "")
        for value in evaluations.values()
        if isinstance(value, Mapping)
    } - {""})
    transports = candidate.get("system_transport_provider_ids")
    transports = transports if isinstance(transports, Mapping) else {}
    temporal_scope = str(candidate.get("oos_event_temporal_scope") or "")
    try:
        future_count = int(candidate.get("future_event_count", 0) or 0)
    except (TypeError, ValueError):
        future_count = -1
    try:
        selection_sha = content_sha256(_selection_identity(candidate))
    except Exception as exc:  # legacy bootstrap rows omit cycle mapping
        selection_sha = None
        selection_error = type(exc).__name__ + ":" + str(exc)
    else:
        selection_error = None
    return {
        "candidate_state": str(candidate.get("state") or ""),
        "threshold_profile": str(candidate.get("threshold_profile") or ""),
        "min_train_days": int(candidate.get("min_train_days") or 0),
        "min_holdout_days": int(candidate.get("min_holdout_days") or 0),
        "train_start_date": candidate.get("train_start_date"),
        "train_end_date": candidate.get("train_end_date"),
        "holdout_start_date": candidate.get("holdout_start_date"),
        "holdout_end_date": candidate.get("holdout_end_date"),
        "holdout_source_classes": classes,
        "transport_provider_ids": dict(sorted((str(k), str(v)) for k, v in transports.items())),
        "oos_event_temporal_scope": temporal_scope or None,
        "future_event_count": future_count,
        "selection_evidence_sha256_recomputed": selection_sha,
        "selection_identity_recompute_error": selection_error,
    }


def _evidence_for(item: Mapping[str, Any], manifests: Sequence[tuple[Path, Mapping[str, Any]]]) -> list[dict[str, Any]]:
    signature = _activation_signature(item)
    evidence: list[dict[str, Any]] = []
    for path, manifest in manifests:
        for report in manifest.get("reports") or []:
            if not isinstance(report, Mapping):
                continue
            for candidate in report.get("cross_candidates") or []:
                if not isinstance(candidate, Mapping) or _candidate_signature(candidate) != signature:
                    continue
                facts = _source_facts(candidate)
                evidence.append({
                    "manifest": str(path),
                    "manifest_content_sha256": str(manifest.get("content_sha256")),
                    "report_content_sha256": str(report.get("content_sha256") or ""),
                    "candidate_content_sha256": content_sha256(dict(candidate)),
                    **facts,
                })
    # Keep a deterministic, compact evidence set; the first occurrence of an
    # immutable candidate is enough to prove its lineage class.
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in evidence:
        key = (
            row["manifest_content_sha256"],
            row["candidate_content_sha256"],
            row["candidate_state"],
        )
        unique.setdefault(key, row)
    return sorted(unique.values(), key=lambda row: (row["manifest_content_sha256"], row["candidate_content_sha256"]))


def _classify(item: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    action = str(item.get("action") or "")
    if action == MISSING:
        classes = sorted({c for row in evidence for c in row.get("holdout_source_classes") or []})
        has_forward = bool(set(classes) & FORWARD_CLASSES)
        has_legacy = any(row.get("selection_identity_recompute_error") for row in evidence)
        reasons = ["exact_selection_evaluation_absent_from_ssot"]
        if has_legacy:
            reasons.append("legacy_manifest_selection_identity_not_reproducible_missing_cycle_mapping")
        if has_forward:
            reasons.append("forward_local_receipt_is_not_historical_oos")
        if any(row.get("min_train_days", 0) < 20 or row.get("min_holdout_days", 0) < 8 for row in evidence):
            reasons.append("bootstrap_sample_below_formal_20_train_8_holdout")
        if any(not row.get("transport_provider_ids") for row in evidence):
            reasons.append("native_transport_lineage_missing")
        return {
            "classification": "BLOCKED_LEGACY_BOOTSTRAP_OR_LINEAGE_INCOMPLETE",
            "appendable": False,
            "reasons": sorted(set(reasons)),
            "next_artifact": "signed_closed_historical_native_oos_manifest_v1_with_exact_selection_identity_and_transport_receipts",
        }
    if action == REJECTED:
        return {
            "classification": "BLOCKED_LATEST_EXACT_OOS_REJECTED",
            "appendable": False,
            "reasons": ["latest_exact_oos_rejected_cannot_revive_prior_pass"],
            "next_artifact": "new_signed_closed_historical_oos_manifest_v1_for_a_distinct_asof_selection",
        }
    raise ValueError(f"unsupported_blocker_action:{action}")


def build_ledger(plan: Mapping[str, Any], manifests: Sequence[tuple[Path, Mapping[str, Any]]]) -> dict[str, Any]:
    blockers = [item for item in plan.get("activation_plans") or [] if item.get("action") in {MISSING, REJECTED}]
    entries: list[dict[str, Any]] = []
    for item in blockers:
        evidence = _evidence_for(item, manifests)
        entries.append({
            "activation_id": item.get("activation_id"),
            "artifact_id": item.get("artifact_id"),
            "station_id": item.get("station_id"),
            "generation": item.get("generation"),
            "action": item.get("action"),
            "state": item.get("state"),
            "reason_from_plan": item.get("reason"),
            "activation_selection_evidence_sha256": item.get("activation_selection_evidence_sha256"),
            "scope": item.get("scope"),
            "evidence": evidence,
            **_classify(item, evidence),
        })
    identity = {
        "schema": SCHEMA,
        "source_plan": plan.get("manifest"),
        "source_plan_content_sha256": plan.get("content_sha256"),
        "source_manifests": [
            {"path": str(path), "content_sha256": manifest.get("content_sha256")}
            for path, manifest in manifests
        ],
        "as_of_utc": plan.get("as_of_utc"),
        "entry_count": len(entries),
        "action_summary": dict(sorted(Counter(str(e["action"]) for e in entries).items())),
        "classification_summary": dict(sorted(Counter(str(e["classification"]) for e in entries).items())),
        "appendable_entry_count": sum(bool(e["appendable"]) for e in entries),
        "entries": entries,
        "boundary": {
            "database_mode": "read_only_query_only",
            "writes_target_database": False,
            "calls_network": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "raw_refetch": False,
            "future_data_used_as_oos": False,
            "manifest_mutation": False,
            "activation_mutation": False,
        },
        "policy": {
            "strict_oos_source": "closed historical strict-as-of AWC truth only",
            "forward_policy": "observational_candidate_not_oos",
            "missing_exact": "append only after a signed historical exact evaluation is available",
            "rejected_latest": "do not revive or overwrite prior PASS",
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=ROOT / "output/audits/native_ensemble_activation_repair_plan_20260802_latest.json")
    parser.add_argument("--manifest", type=Path, action="append", dest="manifests")
    parser.add_argument("--output", type=Path, default=ROOT / "output/audits/native_ensemble_activation_blocker_ledger_20260803.json")
    parser.add_argument(
        "--expected-entry-count",
        type=int,
        default=None,
        help="Optional guard for a caller that expects a fixed blocker count; omitted means use the plan's current count.",
    )
    args = parser.parse_args()
    plan_path = args.plan.expanduser().resolve(strict=True)
    plan = _load(plan_path)
    paths = [Path(p).expanduser().resolve(strict=True) for p in (args.manifests or [])]
    if not paths:
        paths = [
            Path(str(plan.get("manifest"))).resolve(strict=True),
            ROOT / "output/audits/native_ensemble_oos/native_ensemble_45station_cross_family_2plus1_smoke_20260731.json",
            ROOT / "output/audits/native_ensemble_oos/OEJN_bootstrap_after_activation_replay.json",
            ROOT / "output/audits/native_ensemble_historical_oos_rebuild_post_contract_20260802.json",
        ]
    manifests = [(path, _load(path)) for path in paths]
    ledger = build_ledger(plan, manifests)
    if args.expected_entry_count is not None and ledger["entry_count"] != args.expected_entry_count:
        raise RuntimeError(f"expected_{args.expected_entry_count}_blockers:{ledger['entry_count']}")
    if ledger["appendable_entry_count"] != 0:
        raise RuntimeError("unexpected_appendable_blocker")
    args.output.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    args.output.expanduser().resolve().write_text(json.dumps(ledger, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: ledger[k] for k in ("schema", "content_sha256", "entry_count", "action_summary", "classification_summary", "appendable_entry_count")}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
