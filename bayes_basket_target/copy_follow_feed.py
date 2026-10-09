"""跟单线的快速读取通道 -- the cluster's taker fills, polled off the writer (charter 257 补记三).

User ruling 2026-10-09 「提高跟单线的权重！加一个快速读取通道」: the lane read
the cluster's trades only at the head and the tail of a production cycle, and
a cycle runs four minutes at the median and twenty-plus after a restart --
the first cycle after 12:00:46Z read a 12:08:46Z buy at 12:25:34Z, and a cycle
past thirty minutes loses the episode to the lane's own age bound.

This module only reads.  The resident polls it from its own single-worker
executor (the single-writer law: read-only work never takes a writer turn),
every wallet in parallel, newest page first, and keeps each wallet's newest
page in memory.  The lane, on the writer connection, takes a wallet's fills
from here when the page is fresh and reaches back past its cursor, and reads
the API itself otherwise -- the feed can only make the read faster, never
make it wrong.  ``version`` moves whenever a page brought a fill not seen
before, which is what the writer's mid-cycle checkpoint keys on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

#: The resident's poll cadence and how stale a page may be before the lane
#: reads the API itself.
POLL_INTERVAL_SECONDS = 15.0
FRESH_SECONDS = 90.0
#: Newest fills per wallet per poll.  At a 15-second cadence a page this deep
#: has always reached back past the cursor; when it has not, the lane reads.
PAGE_SIZE = 200

Fetch = Callable[[str, Mapping[str, Any]], Any]


def _identity(row: Mapping[str, Any]) -> tuple:
    return (row.get("transactionHash"), row.get("asset"), row.get("side"), row.get("size"),
            row.get("price"), row.get("timestamp"))


class ClusterTradeFeed:
    """Each wallet's newest page of taker fills, as last polled."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pages: dict[str, dict[str, Any]] = {}
        self._seen: set[tuple] = set()
        self.version = 0

    def poll(self, users: Sequence[str], *, fetch: Fetch | None = None,
             clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
        """Read every wallet's newest page in parallel; never raises."""

        if fetch is None:
            from .copy_follow_lane import _http_get as fetch  # noqa: PLC0415
        wallets = [str(u).strip().lower() for u in users if str(u).strip()]
        started = clock()

        def one(user: str) -> tuple[str, Any]:
            try:
                page = fetch("/trades", {"user": user, "takerOnly": "true", "limit": PAGE_SIZE, "offset": 0})
            except Exception as exc:  # noqa: BLE001 -- one wallet's failure is its own receipt
                return user, f"{type(exc).__name__}:{exc}"[:160]
            if not isinstance(page, list):
                return user, f"not_a_list:{str(page)[:80]}"
            return user, [dict(r) for r in page if isinstance(r, Mapping)]

        receipt: dict[str, Any] = {"wallets": len(wallets), "ok": 0, "failed": {}, "new_fills": 0}
        if not wallets:
            return receipt
        with ThreadPoolExecutor(max_workers=min(8, len(wallets)), thread_name_prefix="copy-follow-feed") as pool:
            results = list(pool.map(one, wallets))
        polled = clock()
        with self._lock:
            for user, page in results:
                if isinstance(page, str):
                    receipt["failed"][user] = page
                    continue
                receipt["ok"] += 1
                new = [r for r in page if _identity(r) not in self._seen]
                self._seen.update(_identity(r) for r in new)
                receipt["new_fills"] += len(new)
                self._pages[user] = {"polled_mono": polled, "rows": page, "full": len(page) >= PAGE_SIZE}
            if receipt["new_fills"]:
                self.version += 1
            if len(self._seen) > 200_000:
                # Bounded: only the pages in hand can ever be new again.
                self._seen = {_identity(r) for p in self._pages.values() for r in p["rows"]}
        receipt["seconds"] = round(polled - started, 2)
        receipt["version"] = self.version
        return receipt

    def rows_since(self, user: str, start_ts: int, *,
                   clock: Callable[[], float] = time.monotonic) -> list[dict[str, Any]] | None:
        """The wallet's fills at or after ``start_ts`` oldest first, or None when
        the page in hand is stale or may not reach back that far."""

        with self._lock:
            page = self._pages.get(str(user).strip().lower())
            if page is None or clock() - float(page["polled_mono"]) > FRESH_SECONDS:
                return None
            rows = list(page["rows"])
            full = bool(page["full"])
        oldest = min((int(r.get("timestamp") or 0) for r in rows), default=None)
        if full and (oldest is None or oldest >= int(start_ts)):
            return None
        kept = [dict(r) for r in rows if int(r.get("timestamp") or 0) >= int(start_ts)]
        kept.sort(key=lambda r: (int(r.get("timestamp") or 0), str(r.get("transactionHash") or "")))
        return kept


#: The resident's one feed: its executor polls it, the lane reads it.
FEED = ClusterTradeFeed()


__all__ = ["FEED", "FRESH_SECONDS", "PAGE_SIZE", "POLL_INTERVAL_SECONDS", "ClusterTradeFeed"]
