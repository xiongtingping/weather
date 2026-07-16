#!/usr/bin/env python3
"""Append raw final X/S calibration pairs into the isolated shadow registry."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolution_observed_x_s_pair_capture_runtime import run_resolution_observed_x_s_pair_capture_cycle  # noqa: E402


CONFIRMATION_PHRASE = "CAPTURE_FORWARD_OBSERVED_X_S_PAIR"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shadow-db", type=Path, default=ROOT / "data" / "shadow" / "resolution_final_labels.db")
    parser.add_argument("--max-targets", type=int, default=4)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if not args.apply or args.confirm != CONFIRMATION_PHRASE:
        print(json.dumps({"ok": False, "state": "blocked", "block_reasons": ["observed_x_s_pair_capture_requires_explicit_apply_confirmation"], "confirmation_phrase_required": CONFIRMATION_PHRASE, "boundary": {"shadow_only": True, "writes_archive_db": False, "places_orders": False}}, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    captured_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    result = run_resolution_observed_x_s_pair_capture_cycle(
        shadow_db_path=args.shadow_db, captured_at=captured_at, max_targets=max(1, int(args.max_targets))
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
