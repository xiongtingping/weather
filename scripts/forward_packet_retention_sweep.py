"""Keep the forward packets a P0 recompile would need, before they are lost.

The compiler is driven by a packet the collector drops in an inbox file, and
that file is overwritten every cycle.  So the exact input P0 was compiled
from on any past day no longer exists -- which is why an upstream parameter
(the bias half-life, the climatology blend, the spread cap) has never been
scorable on the book: the paper ledger reads compiled envelopes and cannot
produce them, and nothing else could reproduce them either.

This retains each packet once, keyed by its own content hash, so a later
recompile can be handed exactly what production was handed.  Content-keyed
rather than time-keyed because the inbox is rewritten far more often than it
changes: a cycle that re-reads the same captures writes the same packet, and
storing it again would buy nothing.

Stored on disk rather than in the SSOT on purpose.  The target database sits
at 27 of 29 GiB and crossing that ceiling halts every writer in the system;
twelve megabytes a day of packet text belongs nowhere near it.
"""

from __future__ import annotations

import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402

RUNTIME = ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
ARCHIVE = RUNTIME / "forward_packet_archive"
INBOXES = (
    RUNTIME / "official_native_ensemble_forward_inbox.json",
    RUNTIME / "native_ensemble_forward_inbox.json",
)
# Long enough to replay a regime episode and the weeks either side of it, and
# short enough that the archive stays a rounding error on a disk this full.
RETAIN_DAYS = 45


def _packets(payload: object) -> list[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("packets") or []
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _identity(packet: dict) -> str:
    """The packet's own hash when it carries one, else one over its body.

    An official packet already states ``snapshot_content_sha256`` and that is
    the identity the compiler checks, so it is the right key.  A packet that
    states none is hashed whole -- the point is only that re-storing an
    unchanged packet is a no-op.
    """

    stated = packet.get("snapshot_content_sha256")
    if isinstance(stated, str) and len(stated) == 64:
        return stated
    return content_sha256(packet)


def main() -> int:
    now = datetime.now(timezone.utc)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    stored = 0
    seen = 0
    skipped = 0
    for inbox in INBOXES:
        if not inbox.is_file():
            continue
        try:
            payload = json.loads(inbox.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  {inbox.name}: unreadable ({exc})")
            continue
        for packet in _packets(payload):
            seen += 1
            station = str(packet.get("station_id") or "UNKNOWN")
            event_date = str(packet.get("event_date") or "0000-00-00")[:10]
            digest = _identity(packet)
            day_dir = ARCHIVE / event_date
            target = day_dir / f"{station}-{digest[:16]}.json.gz"
            if target.exists():
                skipped += 1
                continue
            day_dir.mkdir(parents=True, exist_ok=True)
            body = json.dumps(
                packet, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            # Written beside and renamed, so a reader never sees a half file.
            temporary = target.with_suffix(".tmp")
            with gzip.open(temporary, "wb") as handle:
                handle.write(body)
            temporary.replace(target)
            stored += 1

    floor = (now - timedelta(days=RETAIN_DAYS)).date().isoformat()
    pruned = 0
    for day_dir in sorted(ARCHIVE.glob("[0-9]" * 4 + "-*")):
        if not day_dir.is_dir() or day_dir.name >= floor:
            continue
        for path in day_dir.iterdir():
            path.unlink()
            pruned += 1
        day_dir.rmdir()

    days = sorted(p.name for p in ARCHIVE.iterdir() if p.is_dir())
    total = sum(1 for _ in ARCHIVE.rglob("*.json.gz"))
    size = sum(p.stat().st_size for p in ARCHIVE.rglob("*.json.gz"))
    print(
        f"  seen {seen}, stored {stored}, already held {skipped},"
        f" pruned {pruned}"
    )
    print(
        f"  archive: {total} packets over {len(days)} event days"
        f" ({size/1048576:.1f} MB)"
        + (f", {days[0]}..{days[-1]}" if days else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
