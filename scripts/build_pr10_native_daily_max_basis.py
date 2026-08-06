#!/usr/bin/env python3
"""Build PR10 native daily-max manifests or exact terminals without writes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.native_daily_max_manifest import (  # noqa: E402
    build_decode_evidence_terminal_bundle,
    build_native_identity_index_from_decode_evidence,
    verify_arrival_transport_bindings,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decode-evidence", action="append", type=Path, required=True)
    parser.add_argument("--arrival-evidence", action="append", type=Path, default=[])
    parser.add_argument("--native-lead-bucket")
    parser.add_argument("--native-run-cycle")
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidences = [
        load_bounded_json(path.expanduser().resolve(strict=True))
        for path in args.decode_evidence
    ]
    arrival_evidences = [
        load_bounded_json(path.expanduser().resolve(strict=True))
        for path in args.arrival_evidence
    ]
    transport_bindings = {}
    for evidence in arrival_evidences:
        current = verify_arrival_transport_bindings(evidence)
        overlap = set(transport_bindings) & set(current)
        if overlap:
            raise RuntimeError("native_daily_max_transport_binding_overlap")
        transport_bindings.update(current)
    identity_receipts = {}
    if args.native_lead_bucket or args.native_run_cycle:
        if not args.native_lead_bucket or not args.native_run_cycle:
            raise RuntimeError("native_daily_max_identity_scope_pair_required")
        identity_receipts = build_native_identity_index_from_decode_evidence(
            evidences, lead_bucket=args.native_lead_bucket,
            run_cycle=args.native_run_cycle,
        )
    bundle = build_decode_evidence_terminal_bundle(
        evidences, as_of_utc=args.as_of_utc,
        arrival_evidences=arrival_evidences,
        transport_bindings=transport_bindings,
        identity_receipts=identity_receipts,
    )
    write_bounded_json_if_changed(
        args.output.expanduser().resolve(), bundle,
        limit=64 * 1024 * 1024, max_nodes=1_000_000,
    )
    print(json.dumps({
        key: bundle[key]
        for key in (
            "state", "scope_count", "manifest_count", "terminal_count",
            "daily_max_data_complete_scope_count", "blocker_counts",
            "content_sha256",
        )
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
