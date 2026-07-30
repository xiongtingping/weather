#!/usr/bin/env python3
"""Read-only canonical 45-station expansion readiness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.batch_expansion_gate import (  # noqa: E402
    MAX_PACKET_BYTES,
    evaluate_45station_no_live_expansion,
)


def apply_runtime_no_live_guards(
    result: dict[str, object],
    *,
    activation: dict[str, object],
    live_pause_present: bool,
) -> dict[str, object]:
    guarded = dict(result)
    blockers = [
        str(value)
        for value in guarded.get("blockers", [])
    ]
    if (
        activation.get("execution_mode") != "production_no_live"
        or activation.get("state") != "production_no_live"
        or activation.get("live_submit_authorized") is not False
        or activation.get("authorizes_order") is not False
    ):
        blockers.append("active_no_live_boundary_invalid")
    if not live_pause_present:
        blockers.append("live_pause_missing")
    guarded["blockers"] = sorted(set(blockers))
    guarded["state"] = (
        "READY" if not guarded["blockers"] else "BLOCKED_FAIL_CLOSED"
    )
    guarded["active_execution_mode"] = activation.get(
        "execution_mode"
    )
    guarded["live_pause_present"] = bool(live_pause_present)
    return guarded


def readiness_exit_code(
    result: dict[str, object],
    *,
    require_ready: bool,
) -> int:
    if not require_ready:
        return 0
    return 0 if result.get("state") == "READY" else 3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db", type=Path, default=ROOT / "data/bayes_basket_target_ssot.db"
    )
    parser.add_argument(
        "--forward-store",
        type=Path,
        default=(
            ROOT
            / "data/runtime/bayes_basket_target_cash_phase"
            / "forward_observations.sqlite3"
        ),
    )
    parser.add_argument(
        "--candidate-packet",
        type=Path,
        default=(
            ROOT
            / "output/audits"
            / "bayes_basket_target_no_live_candidate_artifacts_v2.json"
        ),
    )
    parser.add_argument(
        "--activation",
        type=Path,
        default=(
            ROOT
            / "config/trading/"
            "bayes_basket_target_production_no_live_activation_v1.json"
        ),
    )
    parser.add_argument(
        "--live-pause",
        type=Path,
        default=ROOT / "data/live_paused.flag",
    )
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help=(
            "exit 3 unless the exact no-live expansion gate is READY; "
            "use this flag before any mutating expansion step"
        ),
    )
    args = parser.parse_args()
    packet_path = args.candidate_packet.expanduser().resolve(strict=True)
    if packet_path.stat().st_size > MAX_PACKET_BYTES:
        raise SystemExit("batch_expansion_candidate_packet_bytes_exceeded")
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    raw_activation_path = args.activation.expanduser()
    if raw_activation_path.is_symlink():
        raise SystemExit("batch_expansion_activation_file_invalid")
    activation_path = raw_activation_path.resolve(strict=True)
    if (
        activation_path.stat().st_size > 1024 * 1024
    ):
        raise SystemExit("batch_expansion_activation_file_invalid")
    activation = json.loads(
        activation_path.read_text(encoding="utf-8")
    )
    if not isinstance(activation, dict):
        raise SystemExit("batch_expansion_activation_mapping_required")
    raw_pause_path = args.live_pause.expanduser()
    pause_path = raw_pause_path.resolve()
    live_pause_present = (
        not raw_pause_path.is_symlink() and pause_path.is_file()
    )
    db = args.db.expanduser().resolve(strict=True)
    store = args.forward_store.expanduser().resolve(strict=True)
    # Both files are resident-owned WAL databases.  immutable=1 is only safe
    # for a quiescent snapshot: on a live database it ignores WAL state and
    # can expose an internally inconsistent main file as falsely malformed.
    with sqlite3.connect(
        f"file:{db}?mode=ro", uri=True, timeout=30
    ) as ssot, sqlite3.connect(
        f"file:{store}?mode=ro", uri=True, timeout=30
    ) as forward:
        ssot.execute("PRAGMA query_only=ON")
        forward.execute("PRAGMA query_only=ON")
        result = evaluate_45station_no_live_expansion(
            ssot, forward, packet
        )
    result = apply_runtime_no_live_guards(
        result,
        activation=activation,
        live_pause_present=live_pause_present,
    )
    print(json.dumps(result, sort_keys=True))
    return readiness_exit_code(
        result,
        require_ready=bool(args.require_ready),
    )


if __name__ == "__main__":
    raise SystemExit(main())
