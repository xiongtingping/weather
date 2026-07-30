#!/usr/bin/env python3
"""Build bounded resident TAF inputs consumed by the P0-M worker."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resident_taf_producer import (  # noqa: E402
    refresh_resident_taf_inputs,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


DEFAULT_ARTIFACT_SOURCE = (
    ROOT / "config" / "artifacts"
    / "bayes_basket_target_taf_revision_active.json"
)
DEFAULT_ARTIFACT_OUTPUT = DEFAULT_ARTIFACT_SOURCE
DEFAULT_INBOX_OUTPUT = (
    ROOT / "data" / "bayes_basket_target_taf_revision_inbox.json"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-inbox",
        action="append",
        type=Path,
        default=[],
        help="Bounded forward TAF inbox; repeat for disjoint source batches.",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=DEFAULT_ARTIFACT_SOURCE,
    )
    parser.add_argument(
        "--output-inbox",
        type=Path,
        default=DEFAULT_INBOX_OUTPUT,
    )
    parser.add_argument(
        "--output-artifact",
        type=Path,
        default=DEFAULT_ARTIFACT_OUTPUT,
    )
    parser.add_argument("--as-of-utc", default=None)
    parser.add_argument("--evidence-output", type=Path, required=True)
    args = parser.parse_args()

    result = refresh_resident_taf_inputs(
        source_inbox_paths=args.source_inbox,
        artifact_path=args.artifact,
        output_inbox_path=args.output_inbox,
        output_artifact_path=args.output_artifact,
        as_of_utc=args.as_of_utc or _now(),
    )
    write_bounded_json(args.evidence_output, result, limit=1024 * 1024)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
