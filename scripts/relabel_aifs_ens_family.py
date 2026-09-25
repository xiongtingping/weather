#!/usr/bin/env python3
"""Charter 217: move ECMWF AIFS ENS's registry row to its own family, once.

Run after the restart that deploys charter 217, never before: the resident
that compiles P0 must already carry ``native_ensemble_p0.served_family_map``,
or every scope AIFS sits in fails closed on the pre-split artifacts' caps.
Without ``--commit`` it only reports.  The edit is
``ecmwf_aifs_ens_archive.relabel_pre_split_registration``: the exact
pre-split row, family and content sha only, the no-update guard put back
verbatim, one short write transaction.  A second run finds nothing to do.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import ecmwf_aifs_ens_archive as aifs  # noqa: E402
from bayes_basket_target.band_reliability_store import _begin_immediate  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def _row(conn: sqlite3.Connection) -> dict[str, str] | None:
    row = conn.execute(
        "SELECT independence_family_id,content_sha256 "
        "FROM calibration_native_ensemble_system WHERE ensemble_system_id=?",
        (aifs.SYSTEM_ID,),
    ).fetchone()
    return None if row is None else {"family": str(row[0]), "content_sha256": str(row[1])}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    reader = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60.0)
    reader.execute("PRAGMA query_only=1")
    before = _row(reader)
    reader.close()
    receipt: dict[str, object] = {
        "at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "system": aifs.SYSTEM_ID,
        "before": before,
        "pre_split_sha": aifs.PRE_SPLIT_REGISTRY_CONTENT_SHA256,
        "target_family": aifs.INDEPENDENCE_FAMILY_ID,
    }
    if not args.commit:
        receipt["would_relabel"] = bool(
            before and before["content_sha256"] == aifs.PRE_SPLIT_REGISTRY_CONTENT_SHA256
        )
        print(json.dumps(receipt, ensure_ascii=False))
        return 0
    writer = sqlite3.connect(str(DB), timeout=60.0, isolation_level=None)
    writer.execute("PRAGMA busy_timeout=60000")
    receipt["lock_attempts"] = _begin_immediate(writer)
    try:
        receipt["relabelled"] = aifs.relabel_pre_split_registration(writer)
        writer.execute("COMMIT")
    except Exception:
        writer.execute("ROLLBACK")
        raise
    receipt["after"] = _row(writer)
    receipt["guard_present"] = writer.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' "
        "AND name='calibration_native_system_no_update'"
    ).fetchone() is not None
    writer.close()
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
