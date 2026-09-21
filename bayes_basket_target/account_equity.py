"""Charter 141: the real account, read from the venue, as the book's equity.

Until 2026-09-17 every capital number in the target lane -- the Kelly
bankroll, the station-day allowance, the allocator's bankroll, the exposure
bounds, the drawdown breaker -- read ``basket_target_capital_pool``.  That
row is a counter: it was seeded with a paper balance (514.29 + 20 in the
no-live era) and only ever subtracts the cost of a fill.  Settlement payouts
are never added back and nothing reconciles it with the exchange, so on
2026-09-17 it read 297.98 while the account held 55.28 USDC of cash and
44.52 of open positions -- 99.80.  The breaker read the same counter through
a history table that was never created, so it could not trip.

This module is the one place the real number comes from:

* cash -- the local gateway's ``/health`` (``collateral_balance_usdc``), the
  balance the exchange itself reports and the gateway already refreshes;
* positions -- Polymarket's data API ``/value`` for the funder address, the
  marked value of every open position (settled winners not yet redeemed
  included);
* resting buys -- this ledger's own live orders not yet filled.

``capture_account_equity`` appends one row per read (the resident does it at
most once a minute); ``load_account_equity`` answers "what did the account
hold as of t" and returns None when the newest read is older than the
freshness bound, which production turns into a refusal rather than a guess.
Open exposure for the bounds is measured at cost from the ledger's fills on
station-days that have not settled, plus resting buys.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from .contracts import ContractError, content_sha256

SCHEMA = "basket_target_account_equity_v1"
TABLE = "basket_target_account_equity"
ACCOUNT_EQUITY_DDL_STATEMENTS = (
    f"CREATE TABLE IF NOT EXISTS {TABLE} ("
    " equity_id TEXT PRIMARY KEY,"
    " observed_at_utc TEXT NOT NULL,"
    " cash_usdc REAL NOT NULL CHECK(cash_usdc>=0),"
    " cash_observed_at_utc TEXT,"
    " positions_value_usdc REAL NOT NULL CHECK(positions_value_usdc>=0),"
    " equity_usdc REAL NOT NULL CHECK(equity_usdc>=0),"
    " resting_buy_usdc REAL NOT NULL CHECK(resting_buy_usdc>=0),"
    " source_json TEXT NOT NULL,"
    " content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),"
    " created_at_utc TEXT NOT NULL)",
    f"CREATE INDEX IF NOT EXISTS {TABLE}_observed_idx ON {TABLE}(observed_at_utc)",
    f"CREATE TRIGGER IF NOT EXISTS {TABLE}_no_update BEFORE UPDATE ON {TABLE}"
    " BEGIN SELECT RAISE(ABORT,'append_only'); END",
    f"CREATE TRIGGER IF NOT EXISTS {TABLE}_no_delete BEFORE DELETE ON {TABLE}"
    " BEGIN SELECT RAISE(ABORT,'append_only'); END",
)

# A read older than this is not the account any more.  The resident reads at
# the head of a cycle and again before each loop that sizes (at most once a
# minute); cycles have run to 21 minutes plus a writer wait, so thirty
# minutes is one slow cycle's worth, and a gateway or data-API outage longer
# than that stops new orders rather than letting them size off a stale
# balance.
ACCOUNT_EQUITY_MAX_AGE_SECONDS = float(
    os.environ.get("PM_ACCOUNT_EQUITY_MAX_AGE_SECONDS", "") or 1800.0
)
ACCOUNT_EQUITY_CAPTURE_INTERVAL_SECONDS = float(
    os.environ.get("PM_ACCOUNT_EQUITY_CAPTURE_INTERVAL_SECONDS", "") or 60.0
)
# The gateway refreshes its collateral cache continuously; a cached balance
# older than this is the gateway's outage, not a balance.
GATEWAY_CASH_MAX_AGE_SECONDS = 300.0
# Charter 184 (user, 2026-09-21: 「挪后台」): the positions read -- one data
# API round trip through the proxy, 7-12s of a cycle at 11:45-11:51Z -- is
# taken on a background thread and handed to the capture.  A read no older
# than the capture interval is what the capture already used: it reads at
# most once a minute, so its positions number was up to a minute old when
# sizing read it.  Anything older is read in line, as before.
POSITIONS_PREFETCH_INTERVAL_SECONDS = float(
    os.environ.get("PM_ACCOUNT_POSITIONS_PREFETCH_SEC", "") or 45.0
)
POSITIONS_PREFETCH_MAX_AGE_SECONDS = ACCOUNT_EQUITY_CAPTURE_INTERVAL_SECONDS
DEFAULT_GATEWAY_URL = os.environ.get(
    "PM_CLOB_ORDER_SERVICE_URL", "http://127.0.0.1:8765"
)
DEFAULT_DATA_API_HOST = os.environ.get(
    "POLY_DATA_API_HOST", "https://data-api.polymarket.com"
)
# A station-day's cost stops counting as open exposure this long after its
# local day ends: the truth publishes and the market resolves inside it, and
# counting a little too long is the conservative side of an exposure bound.
OPEN_EXPOSURE_SETTLE_GRACE_HOURS = 24.0
_LIVE_RECONCILIATION_STATES = ("pending", "unknown")


def _iso(value: Any) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ensure_account_equity_table(conn: sqlite3.Connection) -> None:
    """Statement by statement, never ``executescript``: that commits whatever
    transaction the caller holds, and the resident calls this inside one."""

    for statement in ACCOUNT_EQUITY_DDL_STATEMENTS:
        conn.execute(statement)


def _table_present(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,),
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def read_gateway_cash(
    *, gateway_url: str = DEFAULT_GATEWAY_URL, timeout_seconds: float = 5.0,
    as_of_utc: str,
) -> tuple[float, str]:
    """The exchange cash balance the local gateway holds, and when it read it."""

    request = Request(gateway_url.rstrip("/") + "/health", method="GET")
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 -- loopback
        payload = json.loads(response.read().decode("utf-8"))
    cash = payload.get("collateral_balance_usdc")
    observed = payload.get("collateral_cache_observed_at")
    if cash is None or observed in (None, ""):
        raise ContractError("account_equity_gateway_cash_unavailable")
    cash = float(cash)
    if not math.isfinite(cash) or cash < 0.0:
        raise ContractError("account_equity_gateway_cash_invalid")
    age = (_iso(as_of_utc) - _iso(observed)).total_seconds()
    if age > GATEWAY_CASH_MAX_AGE_SECONDS:
        raise ContractError("account_equity_gateway_cash_stale")
    return cash, _z(_iso(observed))


def read_positions_value(
    *, user: str, data_api_host: str = DEFAULT_DATA_API_HOST,
    timeout_seconds: float = 8.0,
) -> float:
    """The marked value of every open position the funder address holds."""

    if not str(user or "").strip():
        raise ContractError("account_equity_funder_address_missing")
    import httpx  # noqa: PLC0415 -- the resident's HTTP client, proxy-aware

    response = httpx.get(
        data_api_host.rstrip("/") + "/value",
        params={"user": str(user).strip()},
        timeout=timeout_seconds,
        headers={"User-Agent": "weather-basket-account-equity"},
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload if isinstance(payload, list) else [payload]
    if not rows or not isinstance(rows[0], Mapping) or "value" not in rows[0]:
        raise ContractError("account_equity_positions_value_missing")
    value = float(rows[0]["value"])
    if not math.isfinite(value) or value < 0.0:
        raise ContractError("account_equity_positions_value_invalid")
    return value


class _PositionsPrefetch:
    """One background reader of ``read_positions_value`` per process."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.key: tuple[str, str] | None = None
        self.value: float | None = None
        self.fetched_monotonic: float | None = None
        self.fetched_at_utc: str = ""
        self.error: str = ""
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()

    def ensure(
        self, *, user: str, data_api_host: str, interval: float,
        reader: Callable[..., float] | None = None,
    ) -> bool:
        key = (str(user).strip(), str(data_api_host))
        if not key[0]:
            return False
        with self.lock:
            if self.thread is not None and self.thread.is_alive() and self.key == key:
                return True
            self.key = key
            self.value = None
            self.fetched_monotonic = None
            self.stop = threading.Event()
            stop = self.stop
            self.thread = threading.Thread(
                target=self._loop,
                args=(key, max(5.0, float(interval)), stop, reader or read_positions_value),
                name="account_positions_prefetch",
                daemon=True,
            )
            self.thread.start()
        return True

    def _loop(self, key, interval, stop, reader) -> None:
        while not stop.is_set():
            try:
                value = float(reader(user=key[0], data_api_host=key[1]))
                if not math.isfinite(value) or value < 0.0:
                    raise ContractError("account_equity_positions_value_invalid")
                with self.lock:
                    if self.key == key:
                        self.value = value
                        self.fetched_monotonic = time.monotonic()
                        self.fetched_at_utc = _z(datetime.now(timezone.utc))
                        self.error = ""
            except Exception as exc:  # noqa: BLE001 -- a failed read keeps the last one
                with self.lock:
                    self.error = f"{type(exc).__name__}:{exc}"[:200]
            stop.wait(interval)

    def fresh(
        self, *, user: str, data_api_host: str, max_age: float,
    ) -> tuple[float, str] | None:
        key = (str(user).strip(), str(data_api_host))
        with self.lock:
            if (
                self.key != key
                or self.value is None
                or self.fetched_monotonic is None
                or time.monotonic() - self.fetched_monotonic > float(max_age)
            ):
                return None
            return self.value, self.fetched_at_utc

    def shutdown(self) -> None:
        with self.lock:
            self.stop.set()
            self.thread = None
            self.key = None
            self.value = None
            self.fetched_monotonic = None


_POSITIONS_PREFETCH = _PositionsPrefetch()


def ensure_positions_prefetch(
    *, user: str | None = None, data_api_host: str = DEFAULT_DATA_API_HOST,
    interval: float = POSITIONS_PREFETCH_INTERVAL_SECONDS,
    reader: Callable[..., float] | None = None,
) -> bool:
    """Start the background positions reader (idempotent); False without a
    funder address."""

    return _POSITIONS_PREFETCH.ensure(
        user=user if user is not None else funder_address(),
        data_api_host=data_api_host, interval=interval, reader=reader,
    )


def resting_buy_notional_usdc(conn: sqlite3.Connection) -> float:
    """What this ledger's live buys still have resting on the exchange."""

    placeholders = ",".join("?" for _ in _LIVE_RECONCILIATION_STATES)
    row = conn.execute(
        "SELECT COALESCE(SUM(MAX(0.0, i.requested_notional_usdc"
        " - r.filled_notional_usdc)), 0.0)"
        " FROM basket_target_order_reconciliations r"
        " JOIN basket_target_order_intents i"
        "   ON i.order_intent_id = r.order_intent_id"
        f" WHERE r.state IN ({placeholders}) AND i.order_action = 'BUY'",
        _LIVE_RECONCILIATION_STATES,
    ).fetchone()
    return max(0.0, float((row[0] if row else 0.0) or 0.0))


def funder_address() -> str:
    return str(
        os.environ.get("POLY_DATA_API_USER")
        or os.environ.get("PM_FUNDER_ADDRESS")
        or ""
    ).strip()


def capture_account_equity(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    write: Callable[[], Any],
    gateway_url: str = DEFAULT_GATEWAY_URL,
    data_api_host: str = DEFAULT_DATA_API_HOST,
    user: str | None = None,
    cash_reader: Callable[..., tuple[float, str]] | None = None,
    positions_reader: Callable[..., float] | None = None,
    budget_usdc: float | None = None,
) -> dict[str, Any]:
    """Read the account once and append it.  Never raises: a failed read is a
    receipt, and production refuses on the stale row it leaves behind.

    In the same transaction the reservation pool is re-based on the account
    ("资金池用余额"): ``balance_usdc`` becomes the budget-capped equity plus
    what is already reserved, so the room a new reservation sees is the real
    book.  The pool used to be a paper balance that only ever subtracted
    fills; at the book's pace it would have refused every reservation within
    about two weeks (``capital_pool_insufficient``) while the account still
    held money.  Every drift check the ledger makes still holds: nothing is
    consumed beyond a reservation, and reserved never exceeds the balance.
    """

    receipt: dict[str, Any] = {"schema": SCHEMA, "as_of_utc": as_of_utc}
    try:
        cash, cash_observed = (cash_reader or read_gateway_cash)(
            gateway_url=gateway_url, as_of_utc=as_of_utc,
        )
        positions_user = user if user is not None else funder_address()
        prefetched = (
            _POSITIONS_PREFETCH.fresh(
                user=positions_user, data_api_host=data_api_host,
                max_age=POSITIONS_PREFETCH_MAX_AGE_SECONDS,
            )
            if positions_reader is None
            else None
        )
        if prefetched is not None:
            positions, positions_observed = prefetched
        else:
            positions = float((positions_reader or read_positions_value)(
                user=positions_user,
                data_api_host=data_api_host,
            ))
            positions_observed = as_of_utc
        resting = resting_buy_notional_usdc(conn)
    except (ContractError, URLError, OSError, ValueError, KeyError) as exc:
        receipt.update({
            "state": "read_failed",
            "reason": f"{type(exc).__name__}:{exc}"[:200],
        })
        return receipt
    except Exception as exc:  # noqa: BLE001 -- httpx errors, never fatal here
        receipt.update({
            "state": "read_failed",
            "reason": f"{type(exc).__name__}:{exc}"[:200],
        })
        return receipt
    equity = cash + positions
    identity = {
        "schema": SCHEMA,
        "observed_at_utc": as_of_utc,
        "cash_usdc": round(cash, 6),
        "cash_observed_at_utc": cash_observed,
        "positions_value_usdc": round(positions, 6),
        "equity_usdc": round(equity, 6),
        "resting_buy_usdc": round(resting, 6),
        "source": {
            "cash": "clob_order_service./health.collateral_balance_usdc",
            "positions": "data_api./value",
            "resting": "basket_target_order_reconciliations(pending,unknown)",
            # Charter 184: when the positions number was read -- the
            # capture's own clock for an in-line read, the background
            # reader's for a prefetched one.
            "positions_observed_at_utc": positions_observed,
        },
    }
    sha = content_sha256(identity)
    pool_receipt: dict[str, Any] | None = None
    try:
        with write():
            ensure_account_equity_table(conn)
            pool_receipt = _rebase_pool_on_account(
                conn,
                equity_usdc=budget_capped_equity(equity, budget_usdc),
                at_utc=as_of_utc,
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {TABLE}(equity_id,observed_at_utc,"
                "cash_usdc,cash_observed_at_utc,positions_value_usdc,"
                "equity_usdc,resting_buy_usdc,source_json,content_sha256,"
                "created_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "acct-" + sha[:32], as_of_utc, identity["cash_usdc"],
                    cash_observed, identity["positions_value_usdc"],
                    identity["equity_usdc"], identity["resting_buy_usdc"],
                    json.dumps(identity["source"], sort_keys=True), sha,
                    as_of_utc,
                ),
            )
    except sqlite3.Error as exc:
        receipt.update({
            "state": "write_failed",
            "reason": f"{type(exc).__name__}:{exc}"[:200],
        })
        return receipt
    receipt.update({"state": "recorded", **identity, "pool": pool_receipt})
    return receipt


def _rebase_pool_on_account(
    conn: sqlite3.Connection, *, equity_usdc: float, at_utc: str,
) -> dict[str, Any] | None:
    """Set the pool's balance to the account plus what is reserved (CAS)."""

    try:
        row = conn.execute(
            "SELECT balance_usdc,reserved_usdc,version FROM"
            " basket_target_capital_pool WHERE pool_id=1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    balance, reserved, version = float(row[0]), float(row[1]), int(row[2])
    target = round(max(0.0, reserved) + max(0.0, float(equity_usdc)), 6)
    if abs(target - balance) <= 1e-6:
        return {"balance_before_usdc": balance, "balance_after_usdc": balance,
                "reserved_usdc": reserved, "moved": False}
    changed = conn.execute(
        "UPDATE basket_target_capital_pool SET balance_usdc=?,"
        "version=version+1,updated_at=? WHERE pool_id=1 AND version=?",
        (target, at_utc, version),
    ).rowcount
    return {"balance_before_usdc": balance, "balance_after_usdc": target,
            "reserved_usdc": reserved, "moved": bool(changed == 1)}


def load_account_equity(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    max_age_seconds: float = ACCOUNT_EQUITY_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    """The newest account read at or before ``as_of_utc``, if still fresh.

    None when the table is absent, empty, or its newest read is older than
    ``max_age_seconds``.  Callers decide what None means; production refuses.
    """

    if not _table_present(conn):
        return None
    as_of = _iso(as_of_utc)
    row = conn.execute(
        f"SELECT observed_at_utc,cash_usdc,positions_value_usdc,equity_usdc,"
        f"resting_buy_usdc FROM {TABLE} WHERE observed_at_utc<=?"
        " ORDER BY observed_at_utc DESC LIMIT 1",
        (_z(as_of),),
    ).fetchone()
    if row is None:
        return None
    observed = _iso(row[0])
    age = (as_of - observed).total_seconds()
    if age > float(max_age_seconds):
        return None
    return {
        "observed_at_utc": str(row[0]),
        "age_seconds": round(age, 3),
        "cash_usdc": float(row[1]),
        "positions_value_usdc": float(row[2]),
        "equity_usdc": float(row[3]),
        "resting_buy_usdc": float(row[4]),
    }


def table_present(conn: sqlite3.Connection) -> bool:
    return _table_present(conn)


def open_exposure_by_station_day(
    conn: sqlite3.Connection, *, as_of_utc: str,
) -> list[tuple[str, str, str, float]]:
    """(station, event_date, timezone, usdc) the book still has at risk.

    Filled cost on station-days whose local day has not ended more than
    ``OPEN_EXPOSURE_SETTLE_GRACE_HOURS`` ago, plus resting buys (pending or
    unknown on the exchange), both from this ledger.  At cost, which is the
    conservative side of an exposure bound.
    """

    from zoneinfo import ZoneInfo  # noqa: PLC0415

    as_of = _iso(as_of_utc)
    earliest = (as_of - timedelta(days=3)).date().isoformat()
    rows = conn.execute(
        "SELECT d.station_id, d.event_date, d.settlement_timezone,"
        " SUM(CASE WHEN r.state IN ('pending','unknown')"
        "     THEN MAX(r.filled_notional_usdc, i.requested_notional_usdc)"
        "     ELSE r.filled_notional_usdc END)"
        " FROM basket_target_order_reconciliations r"
        " JOIN basket_target_order_intents i ON i.order_intent_id = r.order_intent_id"
        " JOIN auto_trade_decisions d ON d.decision_id = i.decision_id"
        " WHERE i.order_action = 'BUY' AND d.event_date >= ?"
        "   AND (r.filled_size > 0 OR r.state IN ('pending','unknown'))"
        " GROUP BY d.station_id, d.event_date, d.settlement_timezone",
        (earliest,),
    ).fetchall()
    out: list[tuple[str, str, str, float]] = []
    for station, event_date, tz, usdc in rows:
        value = float(usdc or 0.0)
        if value <= 0.0:
            continue
        try:
            local_end = datetime.fromisoformat(str(event_date) + "T00:00:00").replace(
                tzinfo=ZoneInfo(str(tz))
            ) + timedelta(days=1)
        except (ValueError, KeyError):
            local_end = as_of + timedelta(days=1)
        if local_end + timedelta(hours=OPEN_EXPOSURE_SETTLE_GRACE_HOURS) <= as_of:
            continue
        out.append((str(station), str(event_date), str(tz), value))
    return out


def open_exposure_usdc(conn: sqlite3.Connection, *, as_of_utc: str) -> float:
    return sum(row[3] for row in open_exposure_by_station_day(conn, as_of_utc=as_of_utc))


def budget_capped_equity(equity_usdc: float, budget_usdc: float | None) -> float:
    """Charter 141: the operator's budget dial is the most the book may use."""

    equity = max(0.0, float(equity_usdc))
    if budget_usdc is None:
        return equity
    budget = float(budget_usdc)
    if not math.isfinite(budget) or budget <= 0.0:
        return equity
    return min(equity, budget)


def equity_peak_usdc(
    conn: sqlite3.Connection, *, since_utc: str, until_utc: str,
) -> float | None:
    """The highest account equity read in a window (the breaker's peak)."""

    if not _table_present(conn):
        return None
    row = conn.execute(
        f"SELECT MAX(equity_usdc) FROM {TABLE}"
        " WHERE observed_at_utc>=? AND observed_at_utc<=?",
        (since_utc, until_utc),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


__all__ = [
    "ACCOUNT_EQUITY_CAPTURE_INTERVAL_SECONDS",
    "ACCOUNT_EQUITY_DDL_STATEMENTS",
    "ACCOUNT_EQUITY_MAX_AGE_SECONDS",
    "SCHEMA",
    "TABLE",
    "budget_capped_equity",
    "capture_account_equity",
    "ensure_account_equity_table",
    "equity_peak_usdc",
    "funder_address",
    "load_account_equity",
    "open_exposure_by_station_day",
    "open_exposure_usdc",
    "read_gateway_cash",
    "read_positions_value",
    "resting_buy_notional_usdc",
    "table_present",
]
