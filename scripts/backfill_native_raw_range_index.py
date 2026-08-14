"""Build the raw store's range index from receipts already on disk.

The content-addressed store answers "do I have these bytes?" only once the
digest is known, which for a fresh capture is after the download.  The range
index answers the question a capture can actually ask -- "have I fetched this
object range before?" -- and is written as each range is stored.

Objects fetched before that index existed are therefore invisible to it: at the
time of writing, 3.9GiB of ECMWF member fields sat in the store and every
capture re-downloaded them anyway.  Their coordinates were never lost, only
unindexed -- each archive's ``field_receipts`` carries the ``object_key``, the
byte range and the ``message_sha256`` for exactly those bytes.  This walks the
retained archives and writes the missing aliases.

Only ranges whose object is actually present in the store are indexed, so the
index never promises bytes the store cannot produce.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Iterator, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.native_raw_object_store import (  # noqa: E402
    find_content_addressed_message,
    raw_sha256,
    store_range_alias,
)

DEFAULT_ARCHIVE_ROOTS = (
    Path("data/runtime/bayes_basket_target_nwp/official_native_capture"),
    Path("data/runtime/bayes_basket_target_nwp"),
    Path("output/audits/native_ensemble_history"),
)
DEFAULT_STORE = Path("data/runtime/official_nwp_sources/ecmwf_pf50/raw")


def _receipts(payload: Any) -> Iterator[Mapping[str, Any]]:
    """Yield every field receipt in an archive, whatever nests it."""

    if isinstance(payload, Mapping):
        rows = payload.get("field_receipts")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, Mapping):
                    yield row
        for value in payload.values():
            if isinstance(value, (list, Mapping)):
                yield from _receipts(value)
    elif isinstance(payload, list):
        for value in payload:
            if isinstance(value, (list, Mapping)):
                yield from _receipts(value)


def backfill(
    *, archive_roots: tuple[Path, ...], store: Path, commit: bool
) -> dict[str, Any]:
    store = store.expanduser().resolve()
    seen: set[tuple[str, int, int]] = set()
    indexed = 0
    absent = 0
    scanned_files = 0
    for root in archive_roots:
        directory = (ROOT / root).resolve() if not root.is_absolute() else root
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            scanned_files += 1
            for receipt in _receipts(payload):
                object_key = str(receipt.get("object_key") or "").strip()
                if not object_key:
                    continue
                try:
                    start = int(receipt["range_start"])
                    end = int(receipt["range_end"])
                    digest = raw_sha256(receipt.get("message_sha256"))
                except (KeyError, TypeError, ValueError, ContractError):
                    continue
                key = (object_key, start, end)
                if key in seen:
                    continue
                seen.add(key)
                # An alias for bytes the store does not hold would promise a
                # hit it cannot serve; the lookup would fall back to the
                # network anyway, so skipping keeps the index honest.
                if find_content_addressed_message(digest, (store,)) is None:
                    absent += 1
                    continue
                indexed += 1
                if commit:
                    store_range_alias(
                        object_key=object_key,
                        range_start=start,
                        range_end=end,
                        message_sha256=digest,
                        root=store,
                        last_modified=str(receipt.get("last_modified") or ""),
                    )
    return {
        "schema": "native_raw_range_index_backfill_v1",
        "store": str(store),
        "scanned_archive_files": scanned_files,
        "distinct_ranges": len(seen),
        "indexed": indexed,
        "absent_from_store": absent,
        "committed": bool(commit),
        "calls_order_gateway": False,
        "places_orders": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, action="append", default=[])
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args(argv)
    roots = tuple(args.archive_root) or DEFAULT_ARCHIVE_ROOTS
    result = backfill(
        archive_roots=roots, store=args.store, commit=bool(args.commit)
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
