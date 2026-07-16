#!/usr/bin/env python3
"""Read-only integrity audit for the P10 direct AMSC shadow inbox."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from p10_amsc_source_sidecar_acceptance import audit_p10_amsc_source_sidecar  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-archive-dir", default=str(ROOT / "data" / "shadow" / "p10_amsc_raw"))
    parser.add_argument("--inbox-dir", default=str(ROOT / "data" / "shadow" / "p10_amsc_inbox"))
    parser.add_argument("--status-path", default=str(ROOT / "data" / "shadow" / "p10_amsc_source_status_latest.json"))
    args = parser.parse_args(argv)
    result = audit_p10_amsc_source_sidecar(
        raw_archive_dir=Path(args.raw_archive_dir).expanduser().resolve(),
        inbox_dir=Path(args.inbox_dir).expanduser().resolve(),
        status_path=Path(args.status_path).expanduser().resolve(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
