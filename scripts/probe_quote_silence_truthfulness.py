#!/usr/bin/env python3
"""Measure whether WS silence on a token means the book did not move.

The production quote lease treats ``book_observed_at`` age as staleness, so
a book nobody touched for longer than the lease disqualifies its whole
scope.  That conflates two states an order-book feed keeps distinct: the
book did not change, and we stopped seeing the book.  Before any freshness
contract is loosened to lean on connection liveness instead, this probe
asks the falsifiable version of the question:

    when a token comes back after N seconds of silence, had its book
    actually changed?

Read-only.  Reads the resident publisher's manifest file, writes one JSON
report, touches no database, places no orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402


SCHEMA = "quote_silence_truthfulness_probe_v1"
# Buckets straddle the 60s production lease and the publisher's own ~91s
# re-observation cadence, so the report can separate "inside the lease",
# "the band the lease currently rejects", and "beyond one publisher cycle".
SILENCE_BUCKETS = ((0.0, 30.0), (30.0, 60.0), (60.0, 90.0), (90.0, 1e9))


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _book_identity(quote: dict) -> str:
    """Content hash of the economic book only -- never its timestamps."""

    return content_sha256({
        "asks": [
            [float(level["price"]), float(level["size"])]
            for level in (quote.get("asks") or [])
        ],
        "bids": [
            [float(level["price"]), float(level["size"])]
            for level in (quote.get("bids") or [])
        ],
    })


def _top_of_book(quote: dict) -> tuple[float | None, float | None]:
    asks = quote.get("asks") or []
    bids = quote.get("bids") or []
    return (
        float(asks[0]["price"]) if asks else None,
        float(bids[-1]["price"]) if bids else None,
    )


def _bucket_label(seconds: float) -> str:
    for low, high in SILENCE_BUCKETS:
        if low <= seconds < high:
            return f"{low:g}-{high:g}s" if high < 1e9 else f"{low:g}s+"
    return "unbucketed"


def run(*, manifest_path: Path, duration_sec: float, poll_sec: float) -> dict:
    deadline = time.monotonic() + duration_sec
    # token -> (book_observed_at, book_sha, best_ask, best_bid)
    last: dict[str, tuple[str, str, float | None, float | None]] = {}
    events: list[dict] = []
    poll_count = 0
    read_failures = 0
    started_at = datetime.now(timezone.utc)
    while time.monotonic() < deadline:
        try:
            root = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            read_failures += 1
            time.sleep(poll_sec)
            continue
        poll_count += 1
        for scope_key, scope in (root.get("scope_manifests") or {}).items():
            for token, quote in (scope.get("quotes_by_token") or {}).items():
                if not quote.get("asks") and not quote.get("bids"):
                    continue
                stamp = str(quote.get("book_observed_at") or "")
                if not stamp:
                    continue
                sha = _book_identity(quote)
                ask, bid = _top_of_book(quote)
                previous = last.get(token)
                last[token] = (stamp, sha, ask, bid)
                if previous is None or previous[0] == stamp:
                    continue
                silence = (_parse(stamp) - _parse(previous[0])).total_seconds()
                events.append({
                    "scope_key": scope_key,
                    "token": token[:24],
                    "silence_sec": round(silence, 3),
                    "silence_bucket": _bucket_label(silence),
                    "book_changed": sha != previous[1],
                    "best_ask_before": previous[2],
                    "best_ask_after": ask,
                    "best_bid_before": previous[3],
                    "best_bid_after": bid,
                    "best_ask_delta": (
                        None
                        if ask is None or previous[2] is None
                        else round(ask - previous[2], 6)
                    ),
                    "best_bid_delta": (
                        None
                        if bid is None or previous[3] is None
                        else round(bid - previous[3], 6)
                    ),
                })
        time.sleep(poll_sec)
    summary: dict[str, dict] = {}
    for low, high in SILENCE_BUCKETS:
        label = f"{low:g}-{high:g}s" if high < 1e9 else f"{low:g}s+"
        rows = [item for item in events if item["silence_bucket"] == label]
        changed = [item for item in rows if item["book_changed"]]
        top_moved = [
            item for item in rows
            if (item["best_ask_delta"] not in (None, 0.0))
            or (item["best_bid_delta"] not in (None, 0.0))
        ]
        ask_moves = [
            abs(item["best_ask_delta"])
            for item in rows
            if item["best_ask_delta"] is not None
        ]
        summary[label] = {
            "reobservation_count": len(rows),
            "book_changed_count": len(changed),
            "book_changed_fraction": (
                round(len(changed) / len(rows), 4) if rows else None
            ),
            "top_of_book_moved_count": len(top_moved),
            "top_of_book_moved_fraction": (
                round(len(top_moved) / len(rows), 4) if rows else None
            ),
            "best_ask_abs_delta_mean": (
                round(sum(ask_moves) / len(ask_moves), 6) if ask_moves else None
            ),
            "best_ask_abs_delta_max": (
                round(max(ask_moves), 6) if ask_moves else None
            ),
        }
    worst = sorted(
        (item for item in events if item["best_ask_delta"] is not None),
        key=lambda item: -abs(item["best_ask_delta"]),
    )[:15]
    identity = {
        "schema": SCHEMA,
        "started_at_utc": started_at.isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "finished_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "manifest_path": str(manifest_path),
        "poll_sec": poll_sec,
        "duration_sec": duration_sec,
        "poll_count": poll_count,
        "read_failure_count": read_failures,
        "tracked_token_count": len(last),
        "reobservation_event_count": len(events),
        "by_silence_bucket": summary,
        "largest_top_of_book_moves": worst,
        "boundary": {
            "reads_database": False,
            "writes_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "data" / "bayes_basket_target_official_ws_manifest.json",
    )
    parser.add_argument("--duration-sec", type=float, default=1800.0)
    parser.add_argument("--poll-sec", type=float, default=3.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(
        manifest_path=args.manifest.expanduser().resolve(),
        duration_sec=args.duration_sec,
        poll_sec=args.poll_sec,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["by_silence_bucket"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
