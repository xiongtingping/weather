#!/usr/bin/env python3
"""Build a bounded M3-12 probability promotion review candidate.

The input is a JSON object containing ``parent_artifact``, ``scope``,
``m3_11_equivalence_receipt``, ``windows``, ``as_of_utc`` and optionally
``oos_rows``, ``candidate_artifact`` or ``candidate_parameters``.  The output
is an immutable offline review receipt.  It never opens SQLite, starts a
service, writes an activation, or calls an order gateway.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.probability_promotion import (  # noqa: E402
    build_probability_promotion_candidate,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)


def _load(path: Path) -> dict[str, Any]:
    value = load_bounded_json(path.expanduser().resolve(), limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise ContractError("promotion_input_mapping_required")
    return dict(value)


def build_from_payload(payload: Mapping[str, Any], *, as_of_override: str | None = None) -> dict[str, Any]:
    raw = dict(payload)
    as_of = as_of_override or raw.get("as_of_utc")
    if as_of in (None, ""):
        raise ContractError("promotion_input_as_of_missing")
    return build_probability_promotion_candidate(
        parent_artifact=raw.get("parent_artifact"),
        scope=raw.get("scope"),
        m3_11_equivalence_receipt=raw.get("m3_11_equivalence_receipt") or raw.get("equivalence_receipt"),
        windows=raw.get("windows"),
        as_of_utc=str(as_of),
        oos_rows=raw.get("oos_rows") or [],
        candidate_artifact=raw.get("candidate_artifact"),
        candidate_parameters=raw.get("candidate_parameters"),
        artifact_id=str(raw.get("artifact_id") or "probability-candidate-m3-12"),
        cross_season_evidence=raw.get("cross_season_evidence"),
        drift_evidence=raw.get("drift_evidence"),
        version_history=raw.get("version_history"),
        limits=raw.get("limits"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of-utc", default=None)
    args = parser.parse_args(argv)
    try:
        result = build_from_payload(_load(args.input), as_of_override=args.as_of_utc)
        write_bounded_json(
            args.output.expanduser().resolve(),
            result,
            limit=MAX_JSON_OUTPUT_BYTES,
        )
    except (ContractError, OSError, ValueError) as exc:
        print(json.dumps({"state": "CANDIDATE_NO_LIVE_REVIEW", "error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({
        "schema": result["schema"],
        "state": result["state"],
        "evidence_state": result["evidence_state"],
        "blocker_count": len(result["blockers"]),
        "content_sha256": result["content_sha256"],
        "output": str(args.output.expanduser().resolve()),
        "writes_database": False,
        "writes_authority": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
