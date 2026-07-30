#!/usr/bin/env python3
"""Build a bounded, non-promotable quality receipt for historical proxy data."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


DERIVED = (
    ROOT
    / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/derived"
)
CORE = DERIVED / "order_decision_core_v1"
DEFAULT_OUTPUT = (
    ROOT
    / "output/audits/"
    "bayes_basket_target_strict_delta_proxy_quality_v1.json"
)
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ROWS_PER_SOURCE = 1_000_000
TRAIN_START = "2026-07-11"
TRAIN_END = "2026-07-15"
HOLDOUT_START = "2026-07-16"
HOLDOUT_END = "2026-07-17"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _open_csv(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return path.open("r", encoding="utf-8", newline="")


def _inventory(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise RuntimeError(f"proxy_source_size_limit:{path.name}")
    with _open_csv(path) as handle:
        reader = csv.DictReader(handle)
        rows = 0
        for _ in reader:
            rows += 1
            if rows > MAX_ROWS_PER_SOURCE:
                raise RuntimeError(f"proxy_source_row_limit:{path.name}")
        columns = list(reader.fieldnames or [])
    return {
        "path": str(path.resolve()),
        "compressed_or_file_bytes": size,
        "rows": rows,
        "columns": columns,
        "sha256": _sha(path),
    }


def _fixed_event_split(path: Path) -> dict[str, Any]:
    train_rows = 0
    holdout_rows = 0
    train_events: set[str] = set()
    holdout_events: set[str] = set()
    with _open_csv(path) as handle:
        reader = csv.DictReader(handle)
        required = {
            "event_day", "event_slug", "chain_filled_shares", "side",
        }
        if not required.issubset(set(reader.fieldnames or [])):
            raise RuntimeError("proxy_signed_order_split_columns_missing")
        for row_no, row in enumerate(reader, start=1):
            if row_no > MAX_ROWS_PER_SOURCE:
                raise RuntimeError("proxy_signed_order_split_row_limit")
            day = str(row["event_day"] or "")
            event = str(row["event_slug"] or "")
            if not event:
                raise RuntimeError("proxy_signed_order_event_missing")
            try:
                filled = float(row["chain_filled_shares"] or 0.0)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("proxy_signed_order_fill_invalid") from exc
            if filled <= 0.0 or str(row["side"] or "").upper() != "BUY":
                continue
            if TRAIN_START <= day <= TRAIN_END:
                train_rows += 1
                train_events.add(event)
            elif HOLDOUT_START <= day <= HOLDOUT_END:
                holdout_rows += 1
                holdout_events.add(event)
    overlap = sorted(train_events & holdout_events)
    return {
        "split_unit": "event_slug",
        "train_window": {
            "start": TRAIN_START,
            "end": TRAIN_END,
            "row_count": train_rows,
            "event_count": len(train_events),
        },
        "holdout_window": {
            "start": HOLDOUT_START,
            "end": HOLDOUT_END,
            "row_count": holdout_rows,
            "event_count": len(holdout_events),
        },
        "event_overlap_count": len(overlap),
        "event_overlap_sample": overlap[:10],
        "event_level_disjoint": not overlap,
    }


def build_report() -> dict[str, Any]:
    sources = (
        CORE / "signed_order_level_features.csv.gz",
        CORE / "final_allocation_bucket_features.csv",
        CORE / "event_bucket_proxy_probabilities.csv",
        CORE / "initial_bucket_choice_candidates.csv",
    )
    prior_gate = DERIVED / "adaptive_basket_candidate_replay_gate_v1.json"
    behavioral = DERIVED / "bayes_basket_behavioral_moments_oos_v1.json"
    for path in (*sources, prior_gate, behavioral):
        if not path.is_file():
            raise RuntimeError(f"proxy_source_missing:{path}")
    total_bytes = sum(path.stat().st_size for path in (*sources, prior_gate, behavioral))
    if total_bytes > MAX_TOTAL_BYTES:
        raise RuntimeError("proxy_sources_total_size_limit")
    inventory = {
        path.name: _inventory(path)
        for path in sources
    }
    split = _fixed_event_split(sources[0])
    prior = json.loads(prior_gate.read_text(encoding="utf-8"))
    identity = {
        "schema": "strict_delta_proxy_quality_report_v1",
        "status": "NO_LIVE_REPLAY_CANDIDATE",
        "authority_scope": "NO_LIVE_REPLAY_CANDIDATE",
        "promotion_permitted": False,
        "formal_oos_artifact_permitted": False,
        "source_inventory": inventory,
        "source_total_bytes": total_bytes,
        "fixed_event_split": split,
        "prior_gate": {
            "path": str(prior_gate.resolve()),
            "sha256": _sha(prior_gate),
            "status": prior.get("status"),
            "eligible_train_events": prior.get("eligible_train_events"),
            "eligible_holdout_events": prior.get("eligible_holdout_events"),
            "promotion_permitted": prior.get("promotion_permitted"),
            "promotion_blockers": list(prior.get("promotion_blockers") or []),
        },
        "behavioral_moments_receipt": {
            "path": str(behavioral.resolve()),
            "sha256": _sha(behavioral),
            "allowed_use": "behavioral_candidate_screening_only",
        },
        "present_but_non_authoritative": [
            "public filled-order rows",
            "settled final allocation rows",
            "proxy ECMWF/GFS/ICON probabilities",
            "reference prices near visible filled orders",
        ],
        "missing_formal_decision_point_evidence": [
            "exact committed P0/P0-M/P1 envelope and posterior version",
            "official exclusion-mask version",
            "official CLOB WS multi-level snapshot at each decision",
            "confirmed inventory version",
            "complete pending/order-state version",
            "strict-delta and station-policy activation vector",
            "complete no-live decision and intent-set identity",
        ],
        "permitted_uses": [
            "candidate family screening",
            "feature sanity checks",
            "non-authoritative sensitivity analysis",
        ],
        "forbidden_uses": [
            "strict-delta promotion",
            "formal OOS score claim",
            "production activation",
            "probability calibration",
            "live or no-live order authorization",
        ],
        "boundary": {
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "stores_tick_history": False,
            "resource_bounded": True,
            "max_source_bytes": MAX_SOURCE_BYTES,
            "max_total_bytes": MAX_TOTAL_BYTES,
            "max_rows_per_source": MAX_ROWS_PER_SOURCE,
        },
    }
    identity["content_sha256"] = content_sha256(identity)
    return identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build_report()
    write_bounded_json(args.output, report)
    print(json.dumps({
        "schema": report["schema"],
        "status": report["status"],
        "promotion_permitted": report["promotion_permitted"],
        "event_level_disjoint": report["fixed_event_split"]["event_level_disjoint"],
        "output": str(args.output.expanduser().resolve()),
        "content_sha256": report["content_sha256"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
