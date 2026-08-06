#!/usr/bin/env python3
"""Materialize the PR04 immutable no-live current-root release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AUDIT_ROOT = (ROOT / "output" / "audits").resolve()
CANONICAL_RELEASE_ROOT = (AUDIT_ROOT / "canonical_activation_releases").resolve()
CANONICAL_CURRENT_POINTER = (AUDIT_ROOT / "oos_gap_ledger_45station_current.json").resolve()
CANONICAL_PREVIEW_RECEIPT = (
    AUDIT_ROOT / "canonical_activation_release_preview_current.json"
).resolve()
CANONICAL_APPLY_RECEIPT = (
    AUDIT_ROOT / "canonical_activation_release_apply_receipt_current.json"
).resolve()

from bayes_basket_target.canonical_activation_release import (
    audit_contract_inputs,
    build_blocked_audit_release,
    build_release_artifacts,
    build_truth_gap_release,
    load_contracts,
    load_db_activation_release_snapshot,
    load_db_audit_snapshot,
    load_inner_snapshot,
    load_expected_scope_manifest,
    load_typed_scope_blocked_receipts,
    load_typed_scope_envelopes,
    load_truth_gap_evidence,
    publish_release,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.resource_bounds import write_bounded_json
from bayes_basket_target.station_registry import coordinate_registry_manifest


CONFIRM = "materialize-canonical-activation-production-no-live"


def _validate_publish_paths(*, release_root: Path, current_pointer: Path) -> None:
    """Keep the only mutable publication edge on the canonical audit paths."""

    if release_root.expanduser().resolve() != CANONICAL_RELEASE_ROOT:
        raise ContractError("activation_release_release_root_outside_canonical_audit_path")
    if current_pointer.expanduser().resolve() != CANONICAL_CURRENT_POINTER:
        raise ContractError("activation_release_current_pointer_outside_canonical_audit_path")


def _validate_output_path(*, output: Path, apply: bool) -> None:
    expected = CANONICAL_APPLY_RECEIPT if apply else CANONICAL_PREVIEW_RECEIPT
    candidate = output.expanduser()
    if candidate.is_symlink() or candidate.resolve() != expected:
        raise ContractError("activation_release_output_outside_canonical_audit_path")


def _assert_preview_identity(preview: dict, expected_preview_sha256: str) -> None:
    expected = str(expected_preview_sha256 or "").strip().lower()
    if expected != preview.get("content_sha256"):
        raise ContractError("activation_release_preview_apply_drift")


def _assert_expected_scope_coverage(preview: dict) -> None:
    if preview.get("typed_scope_coverage_complete") is not True:
        raise ContractError("activation_release_expected_scope_coverage_incomplete")


def _build_apply_receipt(
    *, preview: dict, pointer: dict, current_pointer: Path
) -> dict:
    receipt_identity = {
        **{key: value for key, value in preview.items() if key != "content_sha256"},
        "schema": "bayes_basket_target_activation_release_apply_receipt_v1",
        "preview_content_sha256": preview["content_sha256"],
        "filesystem_release_applied": True,
        "release_published": True,
        "current_pointer_content_sha256": pointer["content_sha256"],
        "current_pointer_path": str(current_pointer.resolve()),
        "release_id": pointer["release_id"],
    }
    return {**receipt_identity, "content_sha256": content_sha256(receipt_identity)}


def build(args: argparse.Namespace) -> dict:
    inner = load_inner_snapshot(args.inner_oos_snapshot)
    expected_scope_manifest = None
    expected_scope_evidence = None
    expected_scope_path = getattr(args, "expected_scope_manifest", None)
    if expected_scope_path is not None:
        registry = coordinate_registry_manifest()
        expected_scope_manifest, expected_scope_evidence = load_expected_scope_manifest(
            expected_scope_path,
            release_as_of_utc=args.as_of_utc,
            canonical_station_ids=inner["canonical_station_ids"],
            canonical_registry_sha256=registry["registry_sha256"],
        )
    release_mode = getattr(args, "release_mode", "truth-gap")
    typed_scope_envelopes, typed_scope_evidence = load_typed_scope_envelopes(
        getattr(args, "typed_scope_envelope", ()) or (),
        release_as_of_utc=args.as_of_utc,
    )
    typed_scope_blocked_receipts, blocked_receipt_evidence = (
        load_typed_scope_blocked_receipts(
            getattr(args, "typed_scope_blocked_receipt", ()) or (),
            release_as_of_utc=args.as_of_utc,
        )
    )
    typed_scope_evidence = [*typed_scope_evidence, *blocked_receipt_evidence]
    source_mode = "CANONICAL_CONTRACT"
    supersession_projection: dict = {}
    supersession_facts: list[dict] = []
    try:
        contracts, contract_evidence = load_contracts(args.canonical_contract)
    except ContractError:
        # Missing/changed canonical semantics are evidence, not fields that may
        # be defaulted.  Materialize the independently valid model facts as a
        # blocked audit ledger and preserve the exact contract parse failure.
        source_mode = "INVALID_CONTRACT_BLOCKED_AUDIT"
        contract_evidence = audit_contract_inputs(args.canonical_contract)
        models, activations, truths, projection = load_db_audit_snapshot(
            args.db,
            as_of_utc=args.as_of_utc,
        )
        ledger, index, binding = build_blocked_audit_release(
            models=models,
            activation_facts=activations,
            truths=truths,
            canonical_projection=projection,
            contract_evidence=contract_evidence,
            inner_snapshot=inner,
            as_of_utc=args.as_of_utc,
            train_count=args.train_count,
            holdout_count=args.holdout_count,
            taf_revision_complete=args.taf_revision_complete,
        )
    else:
        (
            contract_activations,
            models,
            truths,
            supersession_facts,
            contract_projection,
            projection,
            supersession_projection,
        ) = load_db_activation_release_snapshot(
            args.db,
            as_of_utc=args.as_of_utc,
            contracts=contracts,
        )
        if release_mode == "activation":
            ledger, index, binding = build_release_artifacts(
                contracts=contracts,
                contract_evidence=contract_evidence,
                activation_facts=contract_activations,
                supersession_facts=supersession_facts,
                canonical_projection=projection,
                inner_snapshot=inner,
                as_of_utc=args.as_of_utc,
                train_count=args.train_count,
                holdout_count=args.holdout_count,
                taf_revision_complete=args.taf_revision_complete,
                typed_scope_envelopes=typed_scope_envelopes,
                typed_scope_evidence=typed_scope_evidence,
                typed_scope_blocked_receipts=typed_scope_blocked_receipts,
                expected_scope_manifest=expected_scope_manifest,
                expected_scope_evidence=expected_scope_evidence,
            )
            source_mode = "CANONICAL_CONTRACT_ACTIVATION"
        else:
            truth_gap = load_truth_gap_evidence(
                args.truth_gap_evidence,
                release_as_of_utc=args.as_of_utc,
                canonical_station_ids=inner["canonical_station_ids"],
                contract_evidence=contract_evidence,
            )
            ledger, index, binding = build_truth_gap_release(
                contracts=contracts,
                models=models,
                activation_facts=contract_activations,
                truths=truths,
                canonical_projection=projection,
                contract_evidence=contract_evidence,
                truth_gap_evidence=truth_gap,
                inner_snapshot=inner,
                as_of_utc=args.as_of_utc,
                train_count=args.train_count,
                holdout_count=args.holdout_count,
                taf_revision_complete=args.taf_revision_complete,
                typed_scope_envelopes=typed_scope_envelopes,
                typed_scope_evidence=typed_scope_evidence,
                typed_scope_blocked_receipts=typed_scope_blocked_receipts,
                expected_scope_manifest=expected_scope_manifest,
                expected_scope_evidence=expected_scope_evidence,
            )
            source_mode = "CANONICAL_CONTRACT_TRUTH_GAP"
    record_blocker_counts: dict[str, int] = {}
    for record in ledger["records"]:
        for blocker in record.get("blockers", []):
            record_blocker_counts[str(blocker)] = record_blocker_counts.get(str(blocker), 0) + 1
    builder_blocker_counts: dict[str, int] = {}
    for status in ledger["builder_scope_statuses"]:
        for blocker in status.get("blockers", []):
            builder_blocker_counts[str(blocker)] = builder_blocker_counts.get(str(blocker), 0) + 1
    preview_identity = {
        "schema": "bayes_basket_target_activation_release_preview_v1",
        "state": binding["state"],
        "source_mode": source_mode,
        "as_of_utc": ledger["as_of_utc"],
        "ledger_content_sha256": ledger["content_sha256"],
        "activation_index_content_sha256": index["content_sha256"],
        "binding_content_sha256": binding["content_sha256"],
        "canonical_projection_content_sha256": projection["content_sha256"],
        "contract_projection_content_sha256": (
            contract_projection["content_sha256"]
            if source_mode.startswith("CANONICAL_CONTRACT_") else ""
        ),
        "supersession_projection_content_sha256": (
            supersession_projection["content_sha256"]
            if source_mode.startswith("CANONICAL_CONTRACT_") else ""
        ),
        "truth_gap_evidence_content_sha256": (
            truth_gap["content_sha256"]
            if source_mode == "CANONICAL_CONTRACT_TRUTH_GAP" else ""
        ),
        "record_count": ledger["summary"]["record_count"],
        "activation_ready_count": ledger["summary"]["activation_ready_count"],
        "oos_ready_count": ledger["summary"]["oos_ready_count"],
        "builder_scope_count": ledger["summary"]["builder_scope_count"],
        "builder_blocked_count": ledger["summary"]["builder_blocked_count"],
        "record_blocker_counts": dict(sorted(record_blocker_counts.items())),
        "builder_blocker_counts": dict(sorted(builder_blocker_counts.items())),
        "typed_scope_coverage_complete": ledger.get("typed_scope_coverage_complete") is True,
        "expected_scope_manifest_content_sha256": ledger.get(
            "expected_scope_manifest_content_sha256", ""
        ),
        "expected_scope_coverage": ledger.get("expected_scope_coverage", []),
        "typed_scope_envelope_count": len(typed_scope_envelopes),
        "activation_scope_count": index["entry_count"],
        "activation_index_state": index["state"],
        "blockers": binding["blockers"],
        "consumable": (
            binding["state"] != "BLOCKED"
            and index["state"] != "BLOCKED"
            and index["entry_count"] > 0
            and ledger.get("typed_scope_coverage_complete") is True
        ),
        "production_no_live": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "writes_production_db": False,
        "writes_target_database": False,
        "filesystem_release_applied": False,
    }
    preview = {**preview_identity, "content_sha256": content_sha256(preview_identity)}
    if not args.apply:
        return preview
    if args.confirm != CONFIRM:
        raise SystemExit(f"--confirm must equal {CONFIRM!r}")
    _validate_publish_paths(
        release_root=args.release_root,
        current_pointer=args.current_pointer,
    )
    _assert_preview_identity(preview, args.expect_preview_sha)
    _assert_expected_scope_coverage(preview)
    expected = None if args.expect_current_sha == "ABSENT" else args.expect_current_sha
    pointer = publish_release(
        ledger=ledger,
        activation_index=index,
        binding=binding,
        canonical_projection=projection,
        contract_evidence=ledger["source_contracts"],
        release_root=args.release_root,
        current_pointer=args.current_pointer,
        expected_current_content_sha256=expected,
    )
    return _build_apply_receipt(
        preview=preview,
        pointer=pointer,
        current_pointer=args.current_pointer,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--canonical-contract", type=Path, action="append", required=True)
    parser.add_argument("--inner-oos-snapshot", type=Path, required=True)
    parser.add_argument(
        "--release-mode", choices=("truth-gap", "activation"), default="truth-gap"
    )
    parser.add_argument("--truth-gap-evidence", type=Path)
    parser.add_argument("--typed-scope-envelope", type=Path, action="append", default=[])
    parser.add_argument(
        "--typed-scope-blocked-receipt", type=Path, action="append", default=[]
    )
    parser.add_argument("--expected-scope-manifest", type=Path)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--train-count", type=int, required=True)
    parser.add_argument("--holdout-count", type=int, required=True)
    parser.add_argument("--taf-revision-complete", action="store_true")
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--current-pointer", type=Path)
    parser.add_argument("--expect-current-sha")
    parser.add_argument("--expect-preview-sha")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.release_mode == "truth-gap" and args.truth_gap_evidence is None:
        parser.error("--release-mode truth-gap requires --truth-gap-evidence")
    if args.release_mode == "activation" and args.truth_gap_evidence is not None:
        parser.error("--truth-gap-evidence is forbidden in activation mode")
    if args.apply and (
        args.release_root is None
        or args.current_pointer is None
        or args.expect_current_sha is None
        or args.expect_preview_sha is None
    ):
        parser.error(
            "--apply requires --release-root, --current-pointer, "
            "--expect-current-sha, and --expect-preview-sha"
        )
    if not args.apply and any(
        value is not None
        for value in (
            args.release_root,
            args.current_pointer,
            args.expect_current_sha,
            args.expect_preview_sha,
            args.confirm,
        )
    ):
        parser.error("release output arguments are valid only with --apply")
    _validate_output_path(output=args.output, apply=args.apply)
    result = build(args)
    write_bounded_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
