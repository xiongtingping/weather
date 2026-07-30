#!/usr/bin/env python3
"""Dispatch one forward-P0 append through ``TargetProductionWorker`` only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.production_worker import (  # noqa: E402
    DEFAULT_TARGET_ACTIVATION,
    TargetProductionWorker,
    open_target_writer_if_enabled,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--activation", type=Path, default=DEFAULT_TARGET_ACTIVATION)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--ws-manifest", type=Path, required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--evaluation-at-utc", required=True)
    args = parser.parse_args()
    worker = None
    writer = None
    try:
        writer = open_target_writer_if_enabled(
            args.db,
            activation_path=args.activation,
        )
        if writer is None:
            raise RuntimeError("target_worker_write_authority_not_enabled")
        worker = TargetProductionWorker(
            args.db,
            commit_conn=writer,
            activation_path=args.activation,
        )
        result = worker.materialize_forward_p0_preselections(
            registry_snapshot_path=args.registry,
            ws_manifest_path=args.ws_manifest,
            event_date=str(args.event_date),
            evaluation_at_utc=str(args.evaluation_at_utc),
        )
    except Exception as exc:
        result = {
            "schema": "bayes_basket_target_worker_p0_batch_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "write_authority": "TargetProductionWorker",
            "intents_written": 0,
            "reservations_written": 0,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
    finally:
        if worker is not None:
            worker.close()
            writer = None
        if writer is not None:
            writer.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") == "COMMITTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
