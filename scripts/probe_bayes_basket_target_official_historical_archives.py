#!/usr/bin/env python3
"""Probe official historical forecast archive metadata without downloading data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.official_archive_probe import probe_official_archives  # noqa: E402
from official_nwp_https_transport import open_official_https  # noqa: E402


# Keep one dated canonical receipt.  The former undated path was a directory-
# only report and remained discoverable beside the exact-file evidence,
# causing later audits to replay stale archive conclusions.
DEFAULT_OUTPUT = ROOT / "output" / "audits" / "bayes_basket_target_official_historical_archive_probe_20260725_dwd.json"


def _open_probe_url(url: str, *, method: str, timeout_sec: float):
    request = Request(url, method=method, headers={"User-Agent": "bayes-basket-target-archive-probe/1"})
    return open_official_https(request, timeout_sec=timeout_sec)


def _write_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-date", action="append", required=True, help="UTC model run date, YYYY-MM-DD; repeatable")
    parser.add_argument(
        "--run-cycle",
        action="append",
        default=None,
        choices=("00", "06", "12", "18"),
        help="UTC model cycle; repeatable (default: 00)",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-sec", type=float, default=15.0)
    args = parser.parse_args()
    audit = probe_official_archives(
        args.run_date,
        run_cycles=args.run_cycle or ["00"],
        opener=_open_probe_url,
        timeout_sec=args.timeout_sec,
    )
    _write_atomic(args.output.expanduser().resolve(), audit)
    print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
