#!/usr/bin/env python3
"""Read-only readiness audit before creating a resident P10 shadow cycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from p10_shadow_preparation_readiness import audit_p10_shadow_preparation_readiness  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shadow-db", default=str(ROOT / "data" / "shadow" / "p10_runtime.db"))
    parser.add_argument("--target-identity", default="")
    parser.add_argument("--final-label-db", default=str(ROOT / "data" / "shadow" / "resolution_final_labels.db"))
    parser.add_argument("--inbox-dir", default=str(ROOT / "data" / "shadow" / "p10_amsc_inbox"))
    parser.add_argument("--source-status-path", default=str(ROOT / "data" / "shadow" / "p10_amsc_source_status_latest.json"))
    parser.add_argument("--source-raw-archive-dir", default=str(ROOT / "data" / "shadow" / "p10_amsc_raw"))
    args = parser.parse_args(argv)
    shadow = Path(args.shadow_db).expanduser().resolve()
    if shadow.name == "archive.db":
        print(json.dumps({"ok": False, "state": "blocked", "error": "production_archive_db_forbidden", "read_only": True}))
        return 2
    try:
        conn = sqlite3.connect(f"file:{shadow}?mode=ro", uri=True)
        try:
            result = audit_p10_shadow_preparation_readiness(
                conn,
                target_identity=args.target_identity,
                final_label_db_path=Path(args.final_label_db).expanduser().resolve() if args.final_label_db else None,
                inbox_dir=Path(args.inbox_dir).expanduser().resolve() if args.inbox_dir else None,
                source_status_path=Path(args.source_status_path).expanduser().resolve() if args.source_status_path else None,
                source_raw_archive_dir=Path(args.source_raw_archive_dir).expanduser().resolve() if args.source_raw_archive_dir else None,
            )
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        result = {"ok": False, "state": "blocked", "error": type(exc).__name__, "read_only": True}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
