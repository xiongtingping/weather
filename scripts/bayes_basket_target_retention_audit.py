#!/usr/bin/env python3
"""Read-only bounded retention audit; this command has no deletion option."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.retention_manifest import (  # noqa: E402
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_HASH_BYTES,
    DEFAULT_MIN_FREE_BYTES,
    RetentionManifestError,
    build_retention_manifest,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="explicit files or directories to inspect")
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--reference", action="append", default=[], type=Path)
    parser.add_argument("--reference-sha256", action="append", default=[])
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-hash-bytes", type=int, default=DEFAULT_MAX_HASH_BYTES)
    parser.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = build_retention_manifest(
            args.paths,
            project_root=args.project_root,
            referenced_paths=args.reference,
            referenced_hashes=args.reference_sha256,
            max_files=args.max_files,
            max_bytes=args.max_bytes,
            max_hash_bytes=args.max_hash_bytes,
            min_free_bytes=args.min_free_bytes,
        )
    except RetentionManifestError as exc:
        result = {
            "schema": "bayes_basket_target_retention_manifest_v1",
            "state": "FAIL_CLOSED",
            "reason": str(exc),
            "cleanup_performed": False,
            "delete_enabled": False,
            "writes_production_db": False,
            "external_archive_access": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    if args.output:
        write_bounded_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
