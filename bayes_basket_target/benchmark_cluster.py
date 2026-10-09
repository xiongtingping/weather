"""对标簇 -- the benchmark cluster's fills on the buckets a lane holds (charter 258).

User rulings 2026-10-09: 「proper线增加一条规则：不做对标账号的对手盘」, then, told
that Proper YES rungs had been filled by the cluster's NO buys at the exact
complement of our price and size (EGLC 10-07 17°C: our 0.41 bid, 16.09
shares; ``0x8fbd7cf5`` bought 16 NO at 0.59 that second), 「先卖出，反向买no，和
跟单线不冲突！可以重叠」.

* **who**: ``strategy_lanes.BENCHMARK_CLUSTER_WALLETS``, the eight wallets
  charter 257 follows -- one list.
* **what is read**: per wallet, the public data API's ``/trades`` with
  ``takerOnly=false`` (maker fills are the cluster's direction as much as its
  takes), filtered to the condition ids asked for, newest first, every page.
  The wallets are read at once, under one deadline; a wallet whose read
  fails, or whose pages reach the API's offset cap, leaves every market of
  its read ``unreadable`` -- absence of an opposite fill is never assumed.
* **opposite**: against a YES leg, a BUY of the NO token or a SELL of the
  YES token; against a NO leg, the mirror.  Only at an implied YES price
  inside ``[OPPOSITE_IMPLIED_MIN, OPPOSITE_IMPLIED_MAX]``: a 0.998 sale of a
  settled winner (the cluster's recycle) or a sub-cent NO is not a view.
* **the verdict** (``SEAL_CRITERION``, user rulings 2026-10-09 「改封桶口径」 and
  「不做对手盘：要区分yes和no的份额和时间轴」): the cluster's position on the
  bucket, its YES shares and its NO shares apart, walked fill by fill in time
  order; equal amounts are locked pairs, the excess is its stance, and the
  stance as it stands at the read decides.  One opposite fill does not: on
  EGLC 10-09 20°C the lead wallet's one maker NO fill (31 shares, inside its
  own 183.7-share YES take of the same second) stood against 1,044 YES.

Pure apart from the HTTP read; nothing here writes.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any

from .strategy_lanes import BENCHMARK_CLUSTER_WALLETS

DATA_API_HOST = os.environ.get("POLY_DATA_API_HOST", "https://data-api.polymarket.com")
HTTP_TIMEOUT_SECONDS = 10.0
#: One pass's read of every wallet, all at once, must be back inside this.
READ_DEADLINE_SECONDS = 25.0
PAGE_SIZE = 500
#: The data API refuses offsets much past this; a read still full here is
#: truncated, and truncated is unreadable.
MAX_OFFSET = 2500
#: Condition ids per request (each 66 characters in the query string).
MARKETS_PER_REQUEST = 40
OPPOSITE_IMPLIED_MIN = 0.02
OPPOSITE_IMPLIED_MAX = 0.98
SIDE_YES = "YES"
SIDE_NO = "NO"

Fetch = Callable[[str, Mapping[str, Any]], Any]


def _http_get(path: str, params: Mapping[str, Any]) -> Any:
    import httpx  # noqa: PLC0415 -- the resident's HTTP client, proxy-aware

    response = httpx.get(
        DATA_API_HOST.rstrip("/") + path, params=dict(params), timeout=HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": "weather-proper-benchmark-seal"},
    )
    response.raise_for_status()
    return response.json()


#: The fetch a pass uses when none is handed in, looked up at the call.  The
#: test suite replaces it (conftest): no test reads the live API.
DEFAULT_FETCH: Fetch = _http_get


def wallet_fills(wallet: str, condition_ids: Sequence[str], *, fetch: Fetch) -> list[dict[str, Any]]:
    """Every fill of ``wallet`` on the markets ``condition_ids`` (one request
    family per chunk of markets, every page).  Raises on any failure or on a
    read the offset cap cut short."""

    rows: list[dict[str, Any]] = []
    for start in range(0, len(condition_ids), MARKETS_PER_REQUEST):
        chunk = ",".join(condition_ids[start:start + MARKETS_PER_REQUEST])
        offset = 0
        while True:
            page = fetch("/trades", {"user": wallet, "market": chunk, "takerOnly": "false",
                                     "limit": PAGE_SIZE, "offset": offset})
            if not isinstance(page, list):
                raise ValueError(f"benchmark_trades_not_a_list:{str(page)[:120]}")
            rows.extend(dict(r, _wallet=wallet) for r in page if isinstance(r, Mapping))
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            if offset > MAX_OFFSET:
                raise ValueError(f"benchmark_read_truncated:{wallet[:10]}")
    return rows


def read_cluster(
    condition_ids: Iterable[str],
    *,
    fetch: Fetch | None = None,
    wallets: Sequence[str] = BENCHMARK_CLUSTER_WALLETS,
    deadline_seconds: float = READ_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """``{"fills": {condition_id: [fill, ...]}, "unreadable": {condition_id, ...},
    "errors": [...], "seconds": ...}`` for every condition id asked.

    A market is readable only when every wallet's read of it came back."""

    conds = sorted({str(c) for c in condition_ids if str(c or "").strip()})
    out: dict[str, Any] = {"fills": {c: [] for c in conds}, "unreadable": set(), "errors": [],
                           "seconds": 0.0, "wallets": len(wallets), "markets": len(conds)}
    if not conds:
        return out
    use = fetch or DEFAULT_FETCH
    began = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=max(1, len(wallets)), thread_name_prefix="benchmark_read")
    try:
        futures = {pool.submit(wallet_fills, w, conds, fetch=use): w for w in wallets}
        done, pending = wait(futures, timeout=float(deadline_seconds))
        for future in pending:
            future.cancel()
            out["errors"].append(f"{futures[future][:10]}:deadline")
        for future in done:
            try:
                rows = future.result()
            except Exception as exc:  # noqa: BLE001 -- unreadable, never assumed clear
                out["errors"].append(f"{futures[future][:10]}:{type(exc).__name__}:{exc}"[:160])
                continue
            for row in rows:
                cond = str(row.get("conditionId") or "")
                if cond in out["fills"]:
                    out["fills"][cond].append(row)
        if out["errors"]:
            out["unreadable"] = set(conds)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    out["seconds"] = round(time.monotonic() - began, 3)
    return out


def implied_yes_price(fill: Mapping[str, Any], *, yes_token: str, no_token: str) -> float | None:
    """The YES price a fill stands for: its own price on the YES token, the
    complement on the NO token; None for a token of neither."""

    asset = str(fill.get("asset") or "")
    try:
        price = float(fill.get("price"))
    except (TypeError, ValueError):
        return None
    if asset == yes_token:
        return price
    if asset == no_token:
        return 1.0 - price
    return None


def is_opposite(fill: Mapping[str, Any], *, side: str, yes_token: str, no_token: str) -> bool:
    """Whether ``fill`` takes the other side of a ``side`` leg on this bucket."""

    asset = str(fill.get("asset") or "")
    action = str(fill.get("side") or "").strip().upper()
    if side == SIDE_YES:
        against = (asset == no_token and action == "BUY") or (asset == yes_token and action == "SELL")
    elif side == SIDE_NO:
        against = (asset == yes_token and action == "BUY") or (asset == no_token and action == "SELL")
    else:
        raise ValueError(f"benchmark_side_invalid:{side!r}")
    if not against:
        return False
    implied = implied_yes_price(fill, yes_token=yes_token, no_token=no_token)
    return implied is not None and OPPOSITE_IMPLIED_MIN <= implied <= OPPOSITE_IMPLIED_MAX


SEAL_CRITERION = "cluster_position_v3"


def cluster_position(
    fills: Iterable[Mapping[str, Any]], *, yes_token: str, no_token: str,
) -> dict[str, Any]:
    """The cluster's position on one bucket, YES and NO apart, walked in time
    order (user ruling 2026-10-09 「不做对手盘：要区分yes和no的份额和时间轴」).

    ``yes_shares`` and ``no_shares`` are each token's buys less its sells, all
    eight wallets, inside the price band.  Equal amounts of the two are a
    locked pair -- worth a dollar whatever the weather -- and say nothing;
    ``net_yes = yes_shares - no_shares`` is the stance (above zero long YES,
    below zero short).  ``since_ts`` is the fill at which the stance last took
    its present sign, so a stance and how long it has held are read together:
    EHAM 10-09 17°C was short from 10-08 13:00Z (421 NO on 19 YES) and long
    again from 22:00Z (650 YES), its 444 NO from then on locked against YES."""

    yes = no = 0.0
    sign = 0
    since: int | None = None
    count = 0
    for fill in sorted(fills, key=lambda f: (int(f.get("timestamp") or 0), str(f.get("transactionHash") or ""))):
        implied = implied_yes_price(fill, yes_token=yes_token, no_token=no_token)
        if implied is None or not OPPOSITE_IMPLIED_MIN <= implied <= OPPOSITE_IMPLIED_MAX:
            continue
        size = float(fill.get("size") or 0.0) * (1.0 if str(fill.get("side") or "").upper() == "BUY" else -1.0)
        if str(fill.get("asset") or "") == yes_token:
            yes += size
        else:
            no += size
        count += 1
        net = yes - no
        now_sign = (net > 1e-9) - (net < -1e-9)
        if now_sign != sign:
            sign, since = now_sign, int(fill.get("timestamp") or 0)
    return {"yes_shares": round(yes, 4), "no_shares": round(no, 4), "paired_shares": round(max(min(yes, no), 0.0), 4),
            "net_yes": round(yes - no, 4), "since_ts": since if sign else None, "fills": count}


def opposite_evidence(
    fills: Iterable[Mapping[str, Any]], *, side: str, yes_token: str, no_token: str,
) -> dict[str, Any] | None:
    """The cluster's position when its stance is against a ``side`` leg --
    short (more NO than YES) against YES, long against NO -- with the latest
    fill that moved it that way; None otherwise (``SEAL_CRITERION``)."""

    if side not in (SIDE_YES, SIDE_NO):
        raise ValueError(f"benchmark_side_invalid:{side!r}")
    rows = list(fills)
    position = cluster_position(rows, yes_token=yes_token, no_token=no_token)
    against = position["net_yes"] < 0.0 if side == SIDE_YES else position["net_yes"] > 0.0
    if not against:
        return None
    movers = [f for f in rows if is_opposite(f, side=side, yes_token=yes_token, no_token=no_token)]
    last = max(movers, key=lambda f: (int(f.get("timestamp") or 0), str(f.get("transactionHash") or "")))
    return {
        "criterion": SEAL_CRITERION,
        **position,
        "wallet": str(last.get("_wallet") or last.get("proxyWallet") or ""),
        "fill_ts": int(last.get("timestamp") or 0),
        "fill_tx": str(last.get("transactionHash") or ""),
        "fill_token_side": SIDE_YES if str(last.get("asset") or "") == yes_token else SIDE_NO,
        "fill_action": str(last.get("side") or "").upper(),
        "fill_price": float(last.get("price")),
        "fill_size": float(last.get("size") or 0.0),
        "implied_price": round(float(implied_yes_price(last, yes_token=yes_token, no_token=no_token)), 6),
        "opposite_fills": len(movers),
    }


__all__ = [
    "DEFAULT_FETCH",
    "SEAL_CRITERION",
    "cluster_position",
    "OPPOSITE_IMPLIED_MAX",
    "OPPOSITE_IMPLIED_MIN",
    "implied_yes_price",
    "is_opposite",
    "opposite_evidence",
    "read_cluster",
    "wallet_fills",
]
