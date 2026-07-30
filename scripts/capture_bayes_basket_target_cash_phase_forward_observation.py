#!/usr/bin/env python3
"""Capture one bounded cash-phase forward/no-live observation.

Dry-run is the default.  ``--apply`` atomically writes only the explicitly
provided target-local JSON path; this CLI has no database or gateway code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.cash_phase_forward_observation import (  # noqa: E402
    ForwardObservationError,
    append_settlement_labels,
    build_forward_observation_packet,
    write_forward_observation_packet,
)
from bayes_basket_target.resource_bounds import load_bounded_json  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle-receipt", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--settlement-labels", type=Path)
    parser.add_argument("--apply", action="store_true", help="atomically write the target-local output")
    return parser


def _load(path: Path) -> dict:
    value = load_bounded_json(path)
    if not isinstance(value, dict):
        raise ForwardObservationError(f"input_mapping_required:{path.name}")
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        packet = build_forward_observation_packet(
            cycle_receipt=_load(args.cycle_receipt),
            registry=_load(args.registry),
            existing=_load(args.output) if args.output.exists() else None,
        )
        if args.settlement_labels is not None:
            labels = _load(args.settlement_labels)
            packet = append_settlement_labels(packet, labels.get("settlement_labels", labels.get("labels", [])))
        wrote = write_forward_observation_packet(args.output, packet) if args.apply else False
        result = {**packet, "dry_run": not args.apply, "written": wrote}
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (ForwardObservationError, OSError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
