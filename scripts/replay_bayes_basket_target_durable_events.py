#!/usr/bin/env python3
"""Run the bounded, offline durable event replay for the target lane.

Input is a small JSON array (or ``{"events": [...]}``) of already captured
target evidence.  With no ``--db`` the SQLite store is temporary and isolated;
with ``--db`` the path is checked against the resident database boundary.  The
command never imports a worker, weather source, market client, or gateway.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.durable_replay import (  # noqa: E402
    DEFAULT_LIMITS,
    ReplayError,
    ReplayLimits,
    ResourceBoundError,
    build_blocked_receipt,
    build_generated_events,
    replay_durable_events,
)
from bayes_basket_target.resource_bounds import bounded_json_bytes  # noqa: E402


def _load_json(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReplayError("replay_events_input_read_failed") from exc
    if len(raw) > DEFAULT_LIMITS.max_total_event_bytes:
        raise ResourceBoundError("replay_events_input_bytes_exceeded")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayError("replay_events_input_invalid_json") from exc
    if isinstance(value, dict):
        value = value.get("events")
    if not isinstance(value, list):
        raise ReplayError("replay_events_input_list_required")
    return value


def _load_registry(path: Path | None) -> list[dict[str, Any]] | None:
    if path is None:
        return None
    value = _load_json(path)
    if not all(isinstance(row, dict) for row in value):
        raise ReplayError("replay_registry_rows_invalid")
    return value


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.events is not None and args.generate:
        raise ReplayError("replay_events_and_generate_are_mutually_exclusive")
    events = (
        _load_json(args.events)
        if args.events is not None
        else build_generated_events(
            seed=args.seed,
            station_id=args.station_id,
            settlement_timezone=args.settlement_timezone,
            anchor_date=args.anchor_date,
            days=args.generated_days,
        )
        if args.generate
        else None
    )
    if events is None:
        raise ReplayError("replay_events_input_required")
    registry = _load_registry(args.registry)
    limits = ReplayLimits(
        max_events=args.max_events,
        max_event_bytes=args.max_event_bytes,
        max_total_event_bytes=args.max_total_bytes,
        max_payload_depth=args.max_payload_depth,
        max_payload_nodes=args.max_payload_nodes,
        max_receipt_bytes=DEFAULT_LIMITS.max_receipt_bytes,
        max_sqlite_bytes=DEFAULT_LIMITS.max_sqlite_bytes,
        max_wal_bytes=DEFAULT_LIMITS.max_wal_bytes,
        max_rss_bytes=DEFAULT_LIMITS.max_rss_bytes,
        min_free_bytes=DEFAULT_LIMITS.min_free_bytes,
    )
    if args.db is not None:
        return replay_durable_events(
            events,
            db_path=args.db,
            as_of_utc=args.as_of_utc,
            registry_rows=registry,
            limits=limits,
            window_anchor_date=args.anchor_date,
        )
    with tempfile.TemporaryDirectory(prefix="bayes-basket-durable-replay-") as temp_dir:
        return replay_durable_events(
            events,
            db_path=Path(temp_dir) / "events.sqlite",
            as_of_utc=args.as_of_utc,
            registry_rows=registry,
            limits=limits,
            window_anchor_date=args.anchor_date,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--events", type=Path, help="bounded JSON array or object containing events")
    source.add_argument("--generate", action="store_true", help="generate a bounded deterministic property corpus")
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--db", type=Path, help="isolated target SQLite path; omitted means temporary")
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--write-receipt", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--generated-days", type=int, default=55)
    parser.add_argument("--station-id", default="RKPK")
    parser.add_argument("--settlement-timezone", default="Asia/Seoul")
    parser.add_argument("--anchor-date", default="2026-07-27")
    parser.add_argument("--max-events", type=int, default=DEFAULT_LIMITS.max_events)
    parser.add_argument("--max-event-bytes", type=int, default=DEFAULT_LIMITS.max_event_bytes)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_LIMITS.max_total_event_bytes)
    parser.add_argument("--max-payload-depth", type=int, default=DEFAULT_LIMITS.max_payload_depth)
    parser.add_argument("--max-payload-nodes", type=int, default=DEFAULT_LIMITS.max_payload_nodes)
    args = parser.parse_args(argv)
    try:
        receipt = run(args)
        output = bounded_json_bytes(receipt, limit=DEFAULT_LIMITS.max_receipt_bytes)
    except (ReplayError, ResourceBoundError, OSError, ValueError) as exc:
        receipt = build_blocked_receipt(f"{type(exc).__name__}:{exc}", as_of_utc=args.as_of_utc)
        output = bounded_json_bytes(receipt, limit=DEFAULT_LIMITS.max_receipt_bytes)
    if args.write_receipt is not None:
        destination = args.write_receipt.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_bytes(output)
        temporary.replace(destination)
    sys.stdout.write(output.decode("utf-8"))
    return 0 if receipt.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
