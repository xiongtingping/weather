"""跟单线 -- one Polymarket account's weather trades, mirrored at our scale (charter 256).

User rulings 2026-10-08: 「研究对标账户，跟单策略」 (the account
``0x8fbd7cf5f806f563080864694415829f7229a959``, badatmath.), then 「不用研究，
开始做跟单策略」, 「按照等比例缩小，我资金没有他多」 and, asked which capital the
ratio is taken on, 「按他已投入仓位算」.

* **source**: the account's TRADE activity on Polymarket's public data API,
  read forward from a cursor at the head and the tail of every production
  cycle.  A trade is admitted when its event is a weather market of the fleet
  (the event slug's city is in the settlement-source contract and the token
  is on that station-day's registry row), its outcome side is in ``sides``,
  it happened at or after the lane's follow start and it is no older than
  ``max_source_age_seconds`` when first seen.  Everything else is recorded
  with its refusal and never followed.  MERGE / REDEEM / rebates are not
  trades: a merge turns a YES+NO pair into a dollar the pair would pay at
  settlement anyway, so the copy keeps its pair.
* **ratio**: our account equity over the marked value of the source's open
  positions (``ratio_basis: source_positions_value``; the source's idle cash
  is not counted).  Stamped on each trade when it is first seen, so a later
  change of either account never re-sizes a trade already followed.
* **target**: per token, the source's admitted trades in time order, each
  ``ratio x size`` -- a BUY adds, a SELL removes, never below zero (a sale
  out of inventory the source held before the follow start is not ours to
  mirror).  ``gap = target - held``, where ``held`` is this lane's own fills
  net of its own sales on that token.
* **order**: never pay more than the source paid plus ``max_slippage``, never
  sell for less than it got less ``max_slippage``.  A gap of at least
  ``min_order_shares`` BUYs under the cap -- the source's newest buy price
  plus the slippage, on the cent grid: an ask at or under the cap is taken
  (FOK, capped there, within the depth resting at or under the cap, the
  venue's dollar minimum met); otherwise a post-only GTD rung rests at the
  best bid plus one tick, under the cap and the ask, for
  ``rung_horizon_seconds``; a bid already above the cap waits.  A surplus of
  at least ``min_order_shares`` (with ``mirror_sells``) SELLs at the floor --
  the source's newest sell price less the slippage -- GTD for the same
  horizon, never more than the lane holds or the wallet shows.  The slippage
  exists because the source's buys run away from its price: on its two hours
  before the lane shipped, all three tokens our scale could follow traded
  4-6 cents above the source's price, and a cap at the source's own price
  would fill only the trades the market then proved wrong.
* **limits**: one open order per token; at most ``max_sends_per_pass`` sends
  a pass; this lane's BUYs cost at most ``max_daily_cost_usdc`` a UTC day
  (open orders at their full notional, filled ones at what they filled); a
  BUY needs the account's free cash.

Nothing is read or written while ``data/live_paused.flag`` is up or the
arming flag is down.  Every planned order and every changed refusal is a row
of ``copy_follow_lane_evaluation``; every source trade seen is a row of
``copy_follow_source_trades``.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import lane_orders
from .contracts import content_sha256
from .delta_v2_runtime import EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC
from .exit_orders import Transport, default_position_reader, send_exit_sell
from .lane_orders import send_lane_buy
from .p1_tail_lane import (
    DEPTH_HEADROOM_FRACTION, _latest_snapshot, _sendable_shares, _ws_manifest_root, live_ws_snapshot,
)
from .strategy_lanes import COPY_FOLLOW_DEFAULT_SETTINGS, LANE_COPY_FOLLOW, lane_settings
from .tiebreak_lane import _cash_refusal, resting_order_fields, settle_stuck_lane_cycles

LANE = LANE_COPY_FOLLOW
LANE_TITLE = "跟单线"
STRATEGY_ID = "bayes_basket_target_v1"
SCHEMA = "copy_follow_lane_plan_v1"
RULE_V1 = "source_positions_value_ratio_at_source_price_v1"
#: Charter 257: the first taker buy of a bucket episode by any wallet of the
#: cluster, followed at a fixed stake where our served probability agrees.
RULE_V2 = "cluster_taker_first_filtered_fixed_stake_v2"
RULE_VERSIONS = {RULE_V1: "copy_follow_lane_v1", RULE_V2: "copy_follow_lane_v2"}
RATIO_BASES = frozenset({"source_positions_value"})
SOURCE_TABLE = "copy_follow_source_trades"
EVALUATION_TABLE = "copy_follow_lane_evaluation"
STATE_TABLE = "copy_follow_state"
INTENT_PREFIX = "copyfollow"
PAUSE_FLAG_PATH = Path(__file__).resolve().parents[1] / "data" / "live_paused.flag"
CITY_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "config" / "trading" / "awc_truth_ingress_v1.json"
DATA_API_HOST = os.environ.get("POLY_DATA_API_HOST", "https://data-api.polymarket.com")
#: The data API refuses an activity offset past this.
MAX_ACTIVITY_OFFSET = 5000
ACTIVITY_PAGE_SIZE = 500
#: ``/trades?takerOnly=true`` pages newest first; a pass reads back to its cursor.
TRADES_PAGE_SIZE = 500
MAX_TRADES_OFFSET = 2000
#: Charter 257: the extra columns v2 stamps on each source trade and plan.
SOURCE_V2_COLUMNS = (("taker", "INTEGER"), ("model_stage", "TEXT"), ("our_p", "REAL"),
                     ("price_cap", "REAL"), ("model_margin", "REAL"))
EVALUATION_V2_COLUMNS = (("our_p", "REAL"), ("price_cap", "REAL"), ("model_stage", "TEXT"),
                         ("model_margin", "REAL"))
HTTP_TIMEOUT_SECONDS = 10.0
#: The gateway signs a BUY at its default tick (the payload carries none), so
#: a price off the cent grid is one nobody quoted -- see ``tail_short_lane``.
SIGNING_TICK = 0.01
#: The highest price a BUY is ever sent at.
MAX_PRICE = 0.99
#: An open intent: its outcome is not known yet.
OPEN_INTENT_STATES = frozenset({"prepared", "submitting", "submitted", "reconcile_required"})
_EVENT_SLUG = re.compile(
    r"^highest-temperature-in-(?P<city>[a-z0-9-]+?)-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$"
)
_MONTHS = {name: i for i, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), start=1)}

Fetch = Callable[[str, Mapping[str, Any]], Any]
PositionReader = Callable[[str], Mapping[str, Any]]

SOURCE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {SOURCE_TABLE} ("
    " source_key TEXT PRIMARY KEY,"
    " source_user TEXT NOT NULL,"
    " source_ts INTEGER NOT NULL,"
    " source_at_utc TEXT NOT NULL,"
    " transaction_hash TEXT,"
    " token_id TEXT NOT NULL,"
    " condition_id TEXT,"
    " order_action TEXT NOT NULL,"
    " outcome_side TEXT,"
    " size REAL NOT NULL,"
    " price REAL NOT NULL,"
    " usdc_size REAL,"
    " event_slug TEXT,"
    " title TEXT,"
    " station_id TEXT,"
    " event_date TEXT,"
    " bucket TEXT,"
    " settlement_timezone TEXT,"
    " ratio REAL,"
    " our_equity_usdc REAL,"
    " source_value_usdc REAL,"
    " scaled_shares REAL,"
    " refusal TEXT,"
    " ingested_at_utc TEXT NOT NULL"
    ")"
)
SOURCE_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {SOURCE_TABLE}_user_ts_idx ON {SOURCE_TABLE}(source_user, source_ts)",
    f"CREATE INDEX IF NOT EXISTS {SOURCE_TABLE}_token_idx ON {SOURCE_TABLE}(source_user, event_date, token_id)",
)
EVALUATION_DDL = (
    f"CREATE TABLE IF NOT EXISTS {EVALUATION_TABLE} ("
    " evaluation_id TEXT PRIMARY KEY,"
    " source_user TEXT NOT NULL,"
    " token_id TEXT NOT NULL,"
    " station_id TEXT,"
    " event_date TEXT,"
    " bucket TEXT,"
    " side TEXT,"
    " evaluated_at_utc TEXT NOT NULL,"
    " rule_version TEXT NOT NULL,"
    " order_action TEXT,"
    " target_shares REAL,"
    " held_shares REAL,"
    " gap_shares REAL,"
    " source_price REAL,"
    " book_source TEXT,"
    " book_observed_at TEXT,"
    " book_age_seconds REAL,"
    " bid REAL,"
    " ask REAL,"
    " order_kind TEXT,"
    " limit_price REAL,"
    " shares REAL,"
    " notional_usdc REAL,"
    " refusal TEXT,"
    " order_intent_id TEXT,"
    " sent INTEGER NOT NULL DEFAULT 0,"
    " filled_shares REAL NOT NULL DEFAULT 0,"
    " send_json TEXT"
    ")"
)
EVALUATION_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {EVALUATION_TABLE}_token_idx"
    f" ON {EVALUATION_TABLE}(token_id, evaluated_at_utc)"
)
STATE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {STATE_TABLE} ("
    " source_user TEXT PRIMARY KEY,"
    " follow_from_ts INTEGER NOT NULL,"
    " follow_from_utc TEXT NOT NULL,"
    " created_at_utc TEXT NOT NULL"
    ")"
)


def ensure_lane_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SOURCE_DDL)
    for statement in SOURCE_INDEX_DDL:
        conn.execute(statement)
    conn.execute(EVALUATION_DDL)
    conn.execute(EVALUATION_INDEX_DDL)
    conn.execute(STATE_DDL)
    for table, columns in ((SOURCE_TABLE, SOURCE_V2_COLUMNS), (EVALUATION_TABLE, EVALUATION_V2_COLUMNS)):
        present = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
        for column, decl in columns:
            if column not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# ---------------------------------------------------------------------------
# Small arithmetic.


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _floor_grid(price: float, tick: float = SIGNING_TICK) -> float:
    digits = max(0, int(round(-math.log10(tick))))
    return round(math.floor(float(price) / tick + 1e-9) * tick, digits)


def _ceil_grid(price: float, tick: float = SIGNING_TICK) -> float:
    digits = max(0, int(round(-math.log10(tick))))
    return round(math.ceil(float(price) / tick - 1e-9) * tick, digits)


def _floor_shares(value: float) -> float:
    return math.floor(float(value) * 100.0 + 1e-9) / 100.0


def _levels(raw: Any) -> list[tuple[float, float]]:
    out = []
    for level in raw or ():
        try:
            price = float(level.get("price"))
            shares = float(level.get("shares") or 0.0)
        except (AttributeError, TypeError, ValueError):
            continue
        if math.isfinite(price) and 0.0 < price < 1.0 and shares > 0.0:
            out.append((price, shares))
    return out


def side_quote(book_row: Mapping[str, Any] | None, side: str) -> dict[str, Any]:
    """The book of the outcome leg bought or sold: YES as it stands, NO read
    off the YES book it is the complement of (NO bid = 1 - YES ask, NO ask =
    1 - YES bid, the shares resting there)."""

    row = book_row or {}
    yes_bids = _levels(row.get("bid_levels"))
    yes_asks = _levels(row.get("ask_levels")) if row.get("ask_available", True) else []
    if str(side).upper() == "NO":
        bids = [(round(1.0 - p, 6), s) for p, s in yes_asks]
        asks = [(round(1.0 - p, 6), s) for p, s in yes_bids]
    else:
        bids, asks = yes_bids, yes_asks
    bids.sort(key=lambda level: -level[0])
    asks.sort(key=lambda level: level[0])
    return {
        "bid": bids[0][0] if bids else None,
        "ask": asks[0][0] if asks else None,
        "bids": bids,
        "asks": asks,
    }


# ---------------------------------------------------------------------------
# The source account.


def _http_get(path: str, params: Mapping[str, Any]) -> Any:
    import httpx  # noqa: PLC0415 -- the resident's HTTP client, proxy-aware

    response = httpx.get(
        DATA_API_HOST.rstrip("/") + path, params=dict(params), timeout=HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": "weather-copy-follow-lane"},
    )
    response.raise_for_status()
    return response.json()


def fetch_source_trades(user: str, *, start_ts: int, fetch: Fetch = _http_get) -> list[dict[str, Any]]:
    """The source's TRADE activity at or after ``start_ts``, oldest first, as
    far as the data API pages (``MAX_ACTIVITY_OFFSET``); the rest is read
    from the advanced cursor on the next pass."""

    rows: list[dict[str, Any]] = []
    offset = 0
    while offset <= MAX_ACTIVITY_OFFSET:
        page = fetch("/activity", {
            "user": user, "type": "TRADE", "start": int(start_ts), "sortBy": "TIMESTAMP",
            "sortDirection": "ASC", "limit": ACTIVITY_PAGE_SIZE, "offset": offset,
        })
        if not isinstance(page, list):
            raise ValueError(f"copy_follow_activity_not_a_list:{str(page)[:120]}")
        rows.extend(r for r in page if isinstance(r, Mapping))
        if len(page) < ACTIVITY_PAGE_SIZE:
            break
        offset += ACTIVITY_PAGE_SIZE
    return [dict(r) for r in rows]


def fetch_source_positions_value(user: str, *, fetch: Fetch = _http_get) -> float:
    """The marked value of the source's open positions (``/value``)."""

    payload = fetch("/value", {"user": user})
    rows = payload if isinstance(payload, list) else [payload]
    if not rows or not isinstance(rows[0], Mapping) or rows[0].get("value") is None:
        raise ValueError("copy_follow_source_value_missing")
    value = float(rows[0]["value"])
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"copy_follow_source_value_invalid:{value}")
    return value


def fetch_source_taker_trades(user: str, *, start_ts: int, fetch: Fetch = _http_get) -> list[dict[str, Any]]:
    """The wallet's TAKER fills at or after ``start_ts`` (``/trades`` with
    ``takerOnly=true`` pages newest first; the read stops at the first page
    that reaches back past the cursor).  Charter 257: a maker fill of the
    source is somebody else's decision to trade with it, not its own."""

    rows: list[dict[str, Any]] = []
    offset = 0
    while offset <= MAX_TRADES_OFFSET:
        page = fetch("/trades", {"user": user, "takerOnly": "true", "limit": TRADES_PAGE_SIZE, "offset": offset})
        if not isinstance(page, list):
            raise ValueError(f"copy_follow_trades_not_a_list:{str(page)[:120]}")
        kept = [dict(r) for r in page if isinstance(r, Mapping) and int(r.get("timestamp") or 0) >= int(start_ts)]
        rows.extend(kept)
        if len(page) < TRADES_PAGE_SIZE or len(kept) < len(page):
            break
        offset += TRADES_PAGE_SIZE
    rows.sort(key=lambda r: (int(r.get("timestamp") or 0), str(r.get("transactionHash") or "")))
    return rows


def served_side_probability(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, bucket: str, side: str,
    stages: Sequence[str], now_utc: str,
) -> dict[str, Any]:
    """Our newest committed served probability of ``bucket`` on ``side`` from a
    stage in ``stages`` (charter 257): ``{"state": "ok", "p", "stage",
    "decision_at_utc"}``, else a named state.  Normalised over the ladder and
    floored at 1e-3 the way the lanes read it."""

    wanted = [str(x) for x in stages]
    if not wanted:
        return {"state": "no_stage_allowed"}
    marks = ",".join("?" for _ in wanted)
    row = conn.execute(
        "SELECT history_id, stage, decision_at_utc FROM basket_probability_bridge_history"
        f" WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage IN ({marks}) AND decision_at_utc<=?"
        " ORDER BY decision_at_utc DESC LIMIT 1",
        (station_id, event_date, *wanted, now_utc),
    ).fetchone()
    if row is None:
        return {"state": "no_served_probability"}
    probs = {str(b): float(p) for b, p in conn.execute(
        "SELECT bucket, probability FROM basket_probability_bridge_bucket_probability WHERE history_id=?", (row[0],),
    )}
    total = sum(probs.values())
    if not probs or not (total > 0.0 and math.isfinite(total)):
        return {"state": "served_probability_unreadable"}
    floored = {b: max(v / total, 1e-3) for b, v in probs.items()}
    scale = sum(floored.values())
    p_yes = floored.get(str(bucket))
    if p_yes is None:
        return {"state": "bucket_not_served"}
    p_yes /= scale
    return {"state": "ok", "p": p_yes if side == "YES" else 1.0 - p_yes, "stage": str(row[1]),
            "decision_at_utc": str(row[2])}


def lead_hours(event_date: str, settlement_timezone: str, now_utc: str) -> float | None:
    """Hours from ``now_utc`` to the event day's local noon (the research
    clock the charter 257 window is stated on)."""

    from zoneinfo import ZoneInfo  # noqa: PLC0415

    try:
        zone = ZoneInfo(settlement_timezone or "UTC")
        noon = datetime.combine(date.fromisoformat(event_date), datetime.min.time(), tzinfo=zone).replace(hour=12)
    except (ValueError, KeyError):
        return None
    return (noon.astimezone(timezone.utc) - _utc(now_utc)).total_seconds() / 3600.0


def source_key(trade: Mapping[str, Any], occurrence: int) -> str:
    """One activity row, the same on every read of it: the fields the API
    returns for a fill, and its place among identical rows of one read."""

    return "src_" + content_sha256({
        "tx": str(trade.get("transactionHash") or ""), "asset": str(trade.get("asset") or ""),
        "side": str(trade.get("side") or ""), "size": trade.get("size"), "price": trade.get("price"),
        "ts": trade.get("timestamp"), "n": int(occurrence),
    })[:40]


# ---------------------------------------------------------------------------
# Mapping a source token onto the fleet's registry.


_CITY_CACHE: dict[str, Any] = {}


def city_stations(path: Path = CITY_CONTRACT_PATH) -> dict[str, str]:
    """``city_slug -> station_id`` from the settlement-source contract, the
    fleet's one list of cities (reread when the file changes)."""

    stamp = path.stat().st_mtime_ns
    if _CITY_CACHE.get("stamp") != stamp:
        document = json.loads(path.read_text(encoding="utf-8"))
        cities = (document.get("settlement_source_contract") or {}).get("cities") or {}
        _CITY_CACHE.update({
            "stamp": stamp,
            "map": {str(k): str(v.get("station_id") or "") for k, v in cities.items() if isinstance(v, Mapping)},
        })
    return dict(_CITY_CACHE["map"])


def parse_event_slug(slug: str) -> tuple[str, str] | None:
    """``(city_slug, event_date)`` of a daily-high event slug, else None."""

    match = _EVENT_SLUG.match(str(slug or "").strip().lower())
    if match is None or match.group("month") not in _MONTHS:
        return None
    try:
        day = date(int(match.group("year")), _MONTHS[match.group("month")], int(match.group("day")))
    except ValueError:
        return None
    return match.group("city"), day.isoformat()


_TOKEN_CACHE: dict[str, dict[str, Any]] = {}


def map_source_token(
    conn: sqlite3.Connection, *, event_slug: str, token_id: str, cities: Mapping[str, str],
) -> dict[str, Any]:
    """The fleet's registry row the source traded, or ``{"refusal": ...}``.

    Matched on the station-day the slug names (the registry's scope index)
    and the token itself, YES or NO; never guessed from the title."""

    token = str(token_id or "").strip()
    if token in _TOKEN_CACHE:
        return dict(_TOKEN_CACHE[token])
    parsed = parse_event_slug(event_slug)
    if parsed is None:
        return {"refusal": "not_a_daily_high_event"}
    city, event_date = parsed
    station = cities.get(city)
    if not station:
        return {"refusal": f"city_not_in_fleet:{city}"}
    row = conn.execute(
        "SELECT outcome, yes_token, no_token, settlement_timezone, settlement_unit FROM basket_target_market_registry"
        " WHERE station_id=? AND event_date=? AND slug=? AND (yes_token=? OR no_token=?)"
        " ORDER BY rowid DESC LIMIT 1",
        (station, event_date, str(event_slug), token, token),
    ).fetchone()
    if row is None:
        return {"refusal": "token_not_on_registry"}
    mapped = {
        "station_id": station, "event_date": event_date, "bucket": str(row[0]),
        "side": "YES" if str(row[1]) == token else "NO",
        "settlement_timezone": str(row[3] or ""),
        "settlement_unit": str(row[4] or ""),
    }
    _TOKEN_CACHE[token] = mapped
    return dict(mapped)


# ---------------------------------------------------------------------------
# Ingesting the source's trades.


def follow_start(conn: sqlite3.Connection, *, user: str, cfg: Mapping[str, Any], now_utc: str) -> int:
    """The instant this lane follows from, as epoch seconds: the setting when
    given, else the first pass's own clock, stamped once."""

    declared = cfg.get("follow_from_utc")
    if declared not in (None, ""):
        return int(_utc(str(declared)).timestamp())
    row = conn.execute(f"SELECT follow_from_ts FROM {STATE_TABLE} WHERE source_user=?", (user,)).fetchone()
    if row is not None:
        return int(row[0])
    start = int(_utc(now_utc).timestamp())
    conn.execute(
        f"INSERT OR IGNORE INTO {STATE_TABLE}(source_user, follow_from_ts, follow_from_utc, created_at_utc)"
        " VALUES(?,?,?,?)",
        (user, start, _z(_utc(now_utc)), now_utc),
    )
    return start


def source_cursor(conn: sqlite3.Connection, *, user: str, floor_ts: int) -> int:
    row = conn.execute(f"SELECT MAX(source_ts) FROM {SOURCE_TABLE} WHERE source_user=?", (user,)).fetchone()
    newest = int(row[0]) if row and row[0] is not None else None
    # Re-read the newest second: the API can deliver a second's fills in parts.
    return max(int(floor_ts), newest) if newest is not None else int(floor_ts)


def copy_ratio(
    conn: sqlite3.Connection, *, user: str, cfg: Mapping[str, Any], now_utc: str,
    fetch: Fetch, cache: dict[str, Any],
) -> dict[str, Any]:
    """Our scale against the source's: ``{"ratio", "our_equity_usdc",
    "source_value_usdc"}`` or ``{"refusal": ...}``.  The source's value is
    re-read at most every ``source_value_refresh_seconds``."""

    from .account_equity import load_account_equity, table_present  # noqa: PLC0415

    basis = str(cfg.get("ratio_basis") or "")
    if basis not in RATIO_BASES:
        return {"refusal": f"ratio_basis_unknown:{basis}"}
    if not table_present(conn):
        return {"refusal": "account_equity_unavailable"}
    ours = load_account_equity(conn, as_of_utc=now_utc)
    if ours is None:
        # The resident reads the account at the head of its cycle, and a long
        # cycle outlives the read: read it again, the way the tiebreak lane's
        # cash check does, before refusing.
        _cash_refusal(conn, now_utc=now_utc, needed=0.0)
        ours = load_account_equity(conn, as_of_utc=now_utc)
    if ours is None or float(ours.get("equity_usdc") or 0.0) <= 0.0:
        return {"refusal": "account_equity_stale"}
    now = _utc(now_utc).timestamp()
    refresh = float(cfg.get("source_value_refresh_seconds") or 300.0)
    held = cache.get(user)
    if held is None or now - float(held["at"]) > refresh:
        try:
            held = {"value": fetch_source_positions_value(user, fetch=fetch), "at": now}
        except Exception as exc:  # noqa: BLE001 -- no source value, no ratio
            return {"refusal": f"source_value_unavailable:{type(exc).__name__}:{exc}"[:160]}
        cache[user] = held
    equity = float(ours["equity_usdc"])
    value = float(held["value"])
    return {"ratio": equity / value, "our_equity_usdc": equity, "source_value_usdc": value}


def ingest_source_trades(
    conn: sqlite3.Connection, *, cfg: Mapping[str, Any], now_utc: str, fetch: Fetch,
    ratio_cache: dict[str, Any],
) -> dict[str, Any]:
    """Read the source's new trades and write each one, admitted or refused."""

    user = str(cfg["source_user"]).strip().lower()
    start = follow_start(conn, user=user, cfg=cfg, now_utc=now_utc)
    cursor = source_cursor(conn, user=user, floor_ts=start)
    trades = fetch_source_trades(user, start_ts=cursor, fetch=fetch)
    known = {
        str(r[0]) for r in conn.execute(
            f"SELECT source_key FROM {SOURCE_TABLE} WHERE source_user=? AND source_ts>=?", (user, cursor),
        )
    }
    seen: dict[tuple, int] = {}
    fresh: list[tuple[str, dict[str, Any]]] = []
    for trade in trades:
        if str(trade.get("type") or "TRADE").upper() != "TRADE":
            continue
        identity = (trade.get("transactionHash"), trade.get("asset"), trade.get("side"),
                    trade.get("size"), trade.get("price"), trade.get("timestamp"))
        occurrence = seen.get(identity, 0)
        seen[identity] = occurrence + 1
        key = source_key(trade, occurrence)
        if key not in known:
            fresh.append((key, trade))
    if not fresh:
        return {"state": "no_new_trades", "cursor_ts": cursor, "fetched": len(trades), "ingested": 0}
    ratio = copy_ratio(conn, user=user, cfg=cfg, now_utc=now_utc, fetch=fetch, cache=ratio_cache)
    if ratio.get("refusal"):
        # Nothing written: the trades stay ahead of the cursor and are read
        # again once the scale can be stated.
        return {"state": "ratio_unavailable", "refusal": ratio["refusal"], "cursor_ts": cursor,
                "fetched": len(trades), "ingested": 0}
    cities = city_stations()
    sides = {str(s).strip().upper() for s in (cfg.get("sides") or ())}
    max_age = float(cfg.get("max_source_age_seconds") or 0.0)
    now = _utc(now_utc).timestamp()
    admitted = 0
    for key, trade in fresh:
        try:
            ts = int(trade["timestamp"])
            size = float(trade["size"])
            price = float(trade["price"])
        except (KeyError, TypeError, ValueError):
            continue
        action = str(trade.get("side") or "").strip().upper()
        token = str(trade.get("asset") or "").strip()
        mapped = map_source_token(conn, event_slug=str(trade.get("eventSlug") or ""), token_id=token, cities=cities)
        refusal = mapped.get("refusal")
        if refusal is None and action not in {"BUY", "SELL"}:
            refusal = f"action_unknown:{action}"
        if refusal is None and mapped["side"] not in sides:
            refusal = f"side_not_followed:{mapped['side']}"
        if refusal is None and ts < start:
            refusal = "before_follow_start"
        if refusal is None and max_age > 0.0 and now - ts > max_age:
            refusal = "source_trade_stale"
        if refusal is None and not (size > 0.0 and 0.0 < price < 1.0):
            refusal = "trade_invalid"
        scaled = round(size * float(ratio["ratio"]), 6) if refusal is None else None
        admitted += int(refusal is None)
        conn.execute(
            f"INSERT OR IGNORE INTO {SOURCE_TABLE}(source_key, source_user, source_ts, source_at_utc,"
            " transaction_hash, token_id, condition_id, order_action, outcome_side, size, price, usdc_size,"
            " event_slug, title, station_id, event_date, bucket, settlement_timezone, ratio, our_equity_usdc,"
            " source_value_usdc, scaled_shares, refusal, ingested_at_utc)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, user, ts, _z(datetime.fromtimestamp(ts, timezone.utc)), str(trade.get("transactionHash") or ""),
             token, str(trade.get("conditionId") or ""), action, mapped.get("side"), size, price,
             trade.get("usdcSize"), str(trade.get("eventSlug") or ""), str(trade.get("title") or ""),
             mapped.get("station_id"), mapped.get("event_date"), mapped.get("bucket"),
             mapped.get("settlement_timezone"), float(ratio["ratio"]), float(ratio["our_equity_usdc"]),
             float(ratio["source_value_usdc"]), scaled, refusal, now_utc),
        )
    return {"state": "ingested", "cursor_ts": cursor, "fetched": len(trades), "ingested": len(fresh),
            "admitted": admitted, "ratio": ratio}


def ingest_cluster_taker_trades(
    conn: sqlite3.Connection, *, cfg: Mapping[str, Any], now_utc: str, fetch: Fetch,
) -> dict[str, Any]:
    """Charter 257: read every cluster wallet's new TAKER fills and write each
    one, admitted (the first buy of a bucket episode, inside the lead window,
    on a station in ``station_units``, where our served probability from a
    stage in ``model_stages`` clears the source's price by ``model_margin``)
    or refused by name.  An admitted trade's ``scaled_shares`` is the fixed
    ``stake_usdc`` worth of shares at the price cap."""

    users = [str(u).strip().lower() for u in (cfg.get("source_users") or ()) if str(u).strip()]
    cities = city_stations()
    sides = {str(x).strip().upper() for x in (cfg.get("sides") or ())}
    units = {str(x).strip().upper() for x in (cfg.get("station_units") or ())}
    stages = [str(x) for x in (cfg.get("model_stages") or ())]
    window = list(cfg.get("lead_hours") or (0.0, 0.0))
    lead_lo, lead_hi = float(window[0]), float(window[1])
    gap = float(cfg.get("episode_gap_seconds") or 0.0)
    stake = float(cfg.get("stake_usdc") or 0.0)
    margin = float(cfg.get("model_margin") or 0.0)
    slippage = max(0.0, float(cfg.get("max_slippage") or 0.0))
    max_age = float(cfg.get("max_source_age_seconds") or 0.0)
    now = _utc(now_utc).timestamp()
    receipt: dict[str, Any] = {"state": "ingested", "users": {}, "fetched": 0, "ingested": 0, "admitted": 0}
    if not (stake > 0.0):
        return {**receipt, "state": "stake_not_positive"}
    for user in users:
        start = follow_start(conn, user=user, cfg=cfg, now_utc=now_utc)
        cursor = source_cursor(conn, user=user, floor_ts=start)
        try:
            trades = fetch_source_taker_trades(user, start_ts=cursor, fetch=fetch)
        except Exception as exc:  # noqa: BLE001 -- one wallet's failed read does not stop the others
            receipt["users"][user] = {"state": "read_failed", "reason": f"{type(exc).__name__}:{exc}"[:160]}
            continue
        known = {str(r[0]) for r in conn.execute(
            f"SELECT source_key FROM {SOURCE_TABLE} WHERE source_user=? AND source_ts>=?", (user, cursor))}
        seen: dict[tuple, int] = {}
        fresh: list[tuple[str, dict[str, Any]]] = []
        for trade in trades:
            identity = (trade.get("transactionHash"), trade.get("asset"), trade.get("side"),
                        trade.get("size"), trade.get("price"), trade.get("timestamp"))
            occurrence = seen.get(identity, 0)
            seen[identity] = occurrence + 1
            key = source_key(trade, occurrence)
            if key not in known:
                fresh.append((key, trade))
        admitted = 0
        for key, trade in fresh:
            try:
                ts = int(trade["timestamp"])
                size = float(trade["size"])
                price = float(trade["price"])
            except (KeyError, TypeError, ValueError):
                continue
            action = str(trade.get("side") or "").strip().upper()
            token = str(trade.get("asset") or "").strip()
            mapped = map_source_token(conn, event_slug=str(trade.get("eventSlug") or ""), token_id=token, cities=cities)
            refusal = mapped.get("refusal")
            served: dict[str, Any] = {}
            cap = None
            if refusal is None and action != "BUY":
                refusal = f"action_not_followed:{action}"
            if refusal is None and mapped["side"] not in sides:
                refusal = f"side_not_followed:{mapped['side']}"
            if refusal is None and ts < start:
                refusal = "before_follow_start"
            if refusal is None and max_age > 0.0 and now - ts > max_age:
                refusal = "source_trade_stale"
            if refusal is None and not (size > 0.0 and 0.0 < price < 1.0):
                refusal = "trade_invalid"
            if refusal is None and str(mapped.get("settlement_unit") or "").upper() not in units:
                refusal = f"station_unit_not_followed:{mapped.get('settlement_unit')}"
            if refusal is None:
                lead = lead_hours(str(mapped["event_date"]), str(mapped.get("settlement_timezone") or ""), now_utc)
                if lead is None:
                    refusal = "lead_unknown"
                elif not (lead_lo <= lead < lead_hi):
                    refusal = f"lead_outside_window:{lead:.1f}h"
            if refusal is None and gap > 0.0:
                # The episode is the token's admitted buys within ``gap`` of each
                # other, whichever was READ first owns it: the cluster's wallets
                # fill a second apart and the public tape shows them in any
                # order (OEJN 2026-10-08 19:01Z: the second wallet's fill, a
                # second EARLIER, arrived five minutes later, found no admitted
                # trade before it, and doubled the leg to two stakes).
                within = conn.execute(
                    f"SELECT 1 FROM {SOURCE_TABLE} WHERE token_id=? AND refusal IS NULL"
                    " AND source_ts>=? AND source_ts<=? LIMIT 1",
                    (token, ts - int(gap), ts + int(gap)),
                ).fetchone()
                if within is not None:
                    refusal = "episode_already_followed"
            if refusal is None:
                served = served_side_probability(
                    conn, station_id=str(mapped["station_id"]), event_date=str(mapped["event_date"]),
                    bucket=str(mapped["bucket"]), side=str(mapped["side"]), stages=stages, now_utc=now_utc,
                )
                if served.get("state") != "ok":
                    refusal = f"model:{served.get('state')}"
                else:
                    cap = min(price + slippage, float(served["p"]) - margin, MAX_PRICE)
                    if cap < price - 1e-9:
                        refusal = f"model_margin_short:{float(served['p']):.3f}<{price + margin:.3f}"
            scaled = round(stake / cap, 6) if refusal is None and cap else None
            admitted += int(refusal is None)
            conn.execute(
                f"INSERT OR IGNORE INTO {SOURCE_TABLE}(source_key, source_user, source_ts, source_at_utc,"
                " transaction_hash, token_id, condition_id, order_action, outcome_side, size, price, usdc_size,"
                " event_slug, title, station_id, event_date, bucket, settlement_timezone, ratio, our_equity_usdc,"
                " source_value_usdc, scaled_shares, refusal, ingested_at_utc, taker, model_stage, our_p, price_cap,"
                " model_margin)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, user, ts, _z(datetime.fromtimestamp(ts, timezone.utc)), str(trade.get("transactionHash") or ""),
                 token, str(trade.get("conditionId") or ""), action, mapped.get("side"), size, price,
                 trade.get("usdcSize", round(size * price, 6)), str(trade.get("eventSlug") or ""),
                 str(trade.get("title") or ""), mapped.get("station_id"), mapped.get("event_date"),
                 mapped.get("bucket"), mapped.get("settlement_timezone"), None, None, None, scaled, refusal, now_utc,
                 1, served.get("stage"), served.get("p"), cap,
                 (float(served["p"]) - price) if served.get("state") == "ok" else None),
            )
        receipt["users"][user] = {"state": "ingested", "cursor_ts": cursor, "fetched": len(trades),
                                  "ingested": len(fresh), "admitted": admitted}
        receipt["fetched"] += len(trades)
        receipt["ingested"] += len(fresh)
        receipt["admitted"] += admitted
    return receipt


def cluster_targets(
    conn: sqlite3.Connection, *, users: Sequence[str], since_event_date: str, episode_gap_seconds: float = 0.0,
) -> dict[str, dict[str, Any]]:
    """Charter 257: per token, the admitted cluster buys since ``since_event_date``
    -- the fixed stake's shares, the newest source price, our probability and
    the cap stamped at admission, and the margin the pass ranks on.

    One stake per episode: admitted rows on the same token within
    ``episode_gap_seconds`` of the episode's first add nothing (the ingest
    refuses them by name now; rows admitted before 2026-10-09 can still carry
    two)."""

    out: dict[str, dict[str, Any]] = {}
    if not users:
        return out
    gap = max(0.0, float(episode_gap_seconds or 0.0))
    episode_start: dict[str, int] = {}
    marks = ",".join("?" for _ in users)
    for (token, user, station, day, bucket, side, tz, scaled, price, ts, our_p, cap, margin, stage) in conn.execute(
        f"SELECT token_id, source_user, station_id, event_date, bucket, outcome_side, settlement_timezone,"
        f" scaled_shares, price, source_ts, our_p, price_cap, model_margin, model_stage FROM {SOURCE_TABLE}"
        f" WHERE source_user IN ({marks}) AND event_date>=? AND refusal IS NULL AND order_action='BUY'"
        f" ORDER BY source_ts, source_key",
        (*users, since_event_date),
    ):
        entry = out.setdefault(str(token), {
            "token_id": str(token), "station_id": str(station), "event_date": str(day), "bucket": str(bucket),
            "side": str(side), "settlement_timezone": str(tz or ""), "target": 0.0, "buy_price": None,
            "sell_price": None, "last_ts": None, "source_user": str(user), "our_p": None, "price_cap": None,
            "model_margin": None, "model_stage": None,
        })
        first = episode_start.get(str(token))
        if gap > 0.0 and first is not None and int(ts) - first <= gap:
            continue
        episode_start[str(token)] = int(ts)
        entry["target"] = entry["target"] + float(scaled or 0.0)
        entry.update(buy_price=float(price), last_ts=int(ts), source_user=str(user), our_p=our_p, price_cap=cap,
                     model_margin=margin, model_stage=stage)
    return out


# ---------------------------------------------------------------------------
# What the lane wants and holds.


def source_targets(conn: sqlite3.Connection, *, user: str, since_event_date: str) -> dict[str, dict[str, Any]]:
    """Per token: the scaled target (BUY adds, SELL removes, never below zero,
    in time order) and the source's newest price on each side."""

    out: dict[str, dict[str, Any]] = {}
    for token, station, day, bucket, side, tz, action, scaled, price, ts in conn.execute(
        f"SELECT token_id, station_id, event_date, bucket, outcome_side, settlement_timezone,"
        f" order_action, scaled_shares, price, source_ts FROM {SOURCE_TABLE}"
        f" WHERE source_user=? AND event_date>=? AND refusal IS NULL"
        f" ORDER BY source_ts, source_key",
        (user, since_event_date),
    ):
        entry = out.setdefault(str(token), {
            "token_id": str(token), "station_id": str(station), "event_date": str(day),
            "bucket": str(bucket), "side": str(side), "settlement_timezone": str(tz or ""),
            "target": 0.0, "buy_price": None, "sell_price": None, "last_ts": None,
        })
        delta = float(scaled or 0.0)
        if str(action) == "BUY":
            entry["target"] = entry["target"] + delta
            entry["buy_price"] = float(price)
        else:
            entry["target"] = max(0.0, entry["target"] - delta)
            entry["sell_price"] = float(price)
        entry["last_ts"] = int(ts)
    return out


def lane_position(conn: sqlite3.Connection, *, station_id: str, event_date: str, token_id: str) -> dict[str, Any]:
    """This lane's fills on the token, net of its sales, and whether one of its
    orders there is still open."""

    bought = sold = 0.0
    open_order = None
    for intent_id, action, state, r_state, filled in conn.execute(
        "SELECT i.order_intent_id, i.order_action, i.state, COALESCE(r.state,''), COALESCE(r.filled_size,0)"
        " FROM basket_target_order_intents i"
        " LEFT JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id"
        " WHERE i.decision_id=? AND i.token_id=?",
        (lane_orders.lane_decision_id(LANE, station_id, event_date), token_id),
    ):
        if str(action) == "SELL":
            sold += float(filled or 0.0)
        else:
            bought += float(filled or 0.0)
        if str(state) in OPEN_INTENT_STATES and not str(r_state).startswith("resolved_"):
            open_order = str(intent_id)
    return {"held": round(bought - sold, 6), "bought": bought, "sold": sold, "open_order": open_order}


def lane_cost_today(conn: sqlite3.Connection, *, now_utc: str) -> float:
    """USDC this lane's BUYs spent, or may still spend, on the UTC day: an
    open order at its full notional, an answered one at what it filled."""

    day = _utc(now_utc).date().isoformat()
    low = f"{LANE}_decision_"
    total = 0.0
    for state, r_state, requested, filled_notional in conn.execute(
        "SELECT i.state, COALESCE(r.state,''), i.requested_notional_usdc, COALESCE(r.filled_notional_usdc,0)"
        " FROM basket_target_order_intents i"
        " LEFT JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id"
        " WHERE i.decision_id>=? AND i.decision_id<? AND i.order_action='BUY' AND substr(i.created_at,1,10)=?",
        (low, low[:-1] + chr(ord(low[-1]) + 1), day),
    ):
        if str(state) in OPEN_INTENT_STATES and not str(r_state).startswith("resolved_"):
            total += float(requested or 0.0)
        else:
            total += float(filled_notional or 0.0)
    return round(total, 6)


def fresh_book(
    conn: sqlite3.Connection, root: Mapping[str, Any] | None, *, station_id: str, event_date: str,
    now_utc: str, fresh_seconds: float, live: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """The freshest ladder -- the WS publisher's own, else the committed one."""

    now = _utc(now_utc)

    def fresh(book: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            age = (now - _utc(book["observed_at"])).total_seconds()
        except (TypeError, ValueError, KeyError):
            return None
        return {**book, "age_seconds": round(age, 1)} if age <= float(fresh_seconds) else None

    seen = False
    if live:
        book = live_ws_snapshot(conn, root, station_id=station_id, event_date=event_date,
                                now_utc=now_utc, include_listing_days=True)
        if book is not None:
            seen = True
            taken = fresh(book)
            if taken is not None:
                return taken, None
    committed = _latest_snapshot(conn, station_id, event_date)
    if committed is not None:
        seen = True
        taken = fresh({**committed, "book_source": "committed_snapshot"})
        if taken is not None:
            return taken, None
    return None, ("book_stale" if seen else "book_unavailable")


# ---------------------------------------------------------------------------
# The order a gap asks for.


def plan_buy(quote: Mapping[str, Any], *, price_cap: float, gap: float, min_shares: float) -> dict[str, Any]:
    """Never above the cap (the source's price plus ``max_slippage``): take an
    ask at or under it, else rest a post-only rung at bid+1 under it and under
    the ask, else wait."""

    limit = _floor_grid(price_cap)
    if limit < SIGNING_TICK:
        return {"refusal": "source_price_under_signing_tick"}
    asks = list(quote.get("asks") or ())
    bid = quote.get("bid")
    ask = quote.get("ask")
    if ask is not None and float(ask) <= limit + 1e-9:
        depth = sum(s for p, s in asks if p <= limit + 1e-9)
        # A FOK's two amounts must both sit on the venue's grid (cost on two
        # decimals, shares on four): 15.62 x 0.32 = 4.9984 was refused on
        # 2026-10-08 17:01Z (「invalid amounts」), so the take is sized the way
        # the far-tail lane sizes its FOKs -- whole shares first.
        shares = _sendable_shares(limit, min(float(gap), depth * DEPTH_HEADROOM_FRACTION)) or 0.0
        if shares >= float(min_shares) and shares * float(ask) >= EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC - 1e-9:
            return {"order_kind": "take", "limit_price": limit, "fok_price_cap": limit, "shares": shares,
                    "notional_usdc": round(shares * limit, 6)}
    rest = limit
    if bid is not None:
        rest = min(rest, _floor_grid(float(bid) + SIGNING_TICK + 1e-9))
    if ask is not None:
        rest = min(rest, _floor_grid(float(ask) - SIGNING_TICK + 1e-9))
    if bid is not None and rest < float(bid) - 1e-9:
        return {"refusal": "market_above_price_cap" if limit < float(bid) - 1e-9 else "no_room_under_ask"}
    if rest < SIGNING_TICK:
        return {"refusal": "no_room_under_ask"}
    shares = _floor_shares(gap)
    if shares < float(min_shares):
        return {"refusal": "below_venue_minimum"}
    return {"order_kind": "rest", "limit_price": rest, "shares": shares, "notional_usdc": round(shares * rest, 6)}


def plan_sell(*, price_floor: float, surplus: float, sellable: float, min_shares: float) -> dict[str, Any]:
    """Never under the floor (the source's price less ``max_slippage``): a GTD
    limit there, which crosses any bid at or above it and rests the
    remainder."""

    price = _ceil_grid(price_floor)
    if price >= 1.0:
        return {"refusal": "source_price_over_signing_grid"}
    shares = _floor_shares(min(float(surplus), float(sellable)))
    if shares < float(min_shares):
        return {"refusal": "below_venue_minimum"}
    return {"order_kind": "sell", "limit_price": price, "shares": shares, "notional_usdc": round(shares * price, 6)}


# ---------------------------------------------------------------------------
# Execution.


def _last_refusal(conn: sqlite3.Connection, token_id: str) -> str | None:
    row = conn.execute(
        f"SELECT refusal, order_intent_id FROM {EVALUATION_TABLE} WHERE token_id=?"
        " ORDER BY evaluated_at_utc DESC, rowid DESC LIMIT 1",
        (token_id,),
    ).fetchone()
    if row is None or row[1]:
        return None
    return str(row[0] or "")


def _unsent_recently(conn: sqlite3.Connection, token_id: str, *, now_utc: str, seconds: float) -> bool:
    """Whether this lane's newest order on the token never reached the venue
    (another lane's open order, a gate, a refusal) under ``seconds`` ago --
    the next try waits, so a standing obstacle costs a row every few
    minutes, not every pass."""

    if seconds <= 0.0:
        return False
    row = conn.execute(
        f"SELECT evaluated_at_utc, sent FROM {EVALUATION_TABLE}"
        " WHERE token_id=? AND order_intent_id IS NOT NULL"
        " ORDER BY evaluated_at_utc DESC, rowid DESC LIMIT 1",
        (token_id,),
    ).fetchone()
    if row is None or int(row[1] or 0):
        return False
    return (_utc(now_utc) - _utc(str(row[0]))).total_seconds() < seconds


def _record(conn: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(row)
    body.setdefault("sent", 0)
    body.setdefault("filled_shares", 0.0)
    body["evaluation_id"] = f"{INTENT_PREFIX}_eval_" + content_sha256({
        k: body.get(k) for k in ("token_id", "evaluated_at_utc", "refusal", "order_intent_id")
    })[:32]
    conn.execute(
        f"INSERT OR IGNORE INTO {EVALUATION_TABLE}({','.join(body)}) VALUES({','.join('?' for _ in body)})",
        tuple(body.values()),
    )
    return body


def execute_copy_follow(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None = None,
    *,
    transport: Transport,
    now_utc: str,
    live_submit_authorized: bool,
    settings: Mapping[str, Any] | None = None,
    fetch: Fetch = _http_get,
    position_reader: PositionReader = default_position_reader,
    ws_manifest_path: Any = None,
    ratio_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read the source's new trades, then close every token's gap it can."""

    del canonical_conn  # the registry this lane maps onto is the target's
    receipt: dict[str, Any] = {"schema": "copy_follow_lane_receipt_v1", "lane": LANE, "title": LANE_TITLE}
    stopped = ("live_paused" if PAUSE_FLAG_PATH.exists()
               else "live_intent_disarmed" if not lane_orders.live_intent_armed() else None)
    if stopped:
        return {**receipt, "state": stopped, "planned": 0, "submitted": 0, "filled": 0}
    cfg = {**COPY_FOLLOW_DEFAULT_SETTINGS, **dict(settings or lane_settings(LANE))}
    if str(cfg.get("rule")) not in RULE_VERSIONS:
        raise ValueError(f"copy_follow_rule_unknown:{cfg.get('rule')}")
    rule_version = RULE_VERSIONS[str(cfg["rule"])]
    cluster_rule = str(cfg["rule"]) == RULE_V2
    if cluster_rule:
        users = [str(u).strip().lower() for u in (cfg.get("source_users") or ()) if str(u).strip()]
        for candidate in users:
            if not re.fullmatch(r"0x[0-9a-f]{40}", candidate):
                raise ValueError(f"copy_follow_source_user_invalid:{candidate}")
        if not users:
            raise ValueError("copy_follow_source_users_empty")
        cfg["source_users"] = users
        user = users[0]
    else:
        user = str(cfg.get("source_user") or "").strip().lower()
        if not re.fullmatch(r"0x[0-9a-f]{40}", user):
            raise ValueError(f"copy_follow_source_user_invalid:{user}")
        cfg["source_user"] = user
    ensure_lane_schema(target_conn)
    cycles_settled = settle_stuck_lane_cycles(target_conn)
    cache = _RATIO_CACHE if ratio_cache is None else ratio_cache
    since = (_utc(now_utc) - timedelta(days=1)).date().isoformat()
    if cluster_rule:
        try:
            ingest = ingest_cluster_taker_trades(target_conn, cfg=cfg, now_utc=now_utc, fetch=fetch)
        except Exception as exc:  # noqa: BLE001 -- a failed read follows what is already in hand
            ingest = {"state": "source_read_failed", "reason": f"{type(exc).__name__}:{exc}"[:200]}
        targets = cluster_targets(target_conn, users=cfg["source_users"], since_event_date=since,
                                  episode_gap_seconds=float(cfg.get("episode_gap_seconds") or 0.0))
    else:
        try:
            ingest = ingest_source_trades(target_conn, cfg=cfg, now_utc=now_utc, fetch=fetch, ratio_cache=cache)
        except Exception as exc:  # noqa: BLE001 -- a failed read follows what is already in hand
            ingest = {"state": "source_read_failed", "reason": f"{type(exc).__name__}:{exc}"[:200]}
        targets = source_targets(target_conn, user=user, since_event_date=since)
    margin = float(cfg.get("model_margin") or 0.0)
    stages = [str(x) for x in (cfg.get("model_stages") or ())]
    min_shares = float(cfg.get("min_order_shares") or 5.0)
    slippage = max(0.0, float(cfg.get("max_slippage") or 0.0))
    horizon = float(cfg.get("rung_horizon_seconds") or 0.0)
    live = cfg.get("live_ws_book", True) is True
    manifest: dict[str, Any] = {}

    def manifest_root() -> Mapping[str, Any] | None:
        if "root" not in manifest:
            manifest["root"] = _ws_manifest_root(ws_manifest_path) if live else None
        return manifest["root"]

    sends = 0
    submitted = filled = 0
    receipts: list[dict[str, Any]] = []
    refusals: dict[str, int] = {}
    budget_left = float(cfg.get("max_daily_cost_usdc") or 0.0) - lane_cost_today(target_conn, now_utc=now_utc)
    ranking = ((lambda kv: -(kv[1].get("model_margin") or 0.0)) if cluster_rule
               else (lambda kv: -(kv[1]["last_ts"] or 0)))
    for token, want in sorted(targets.items(), key=ranking):
        station, day, bucket, side = want["station_id"], want["event_date"], want["bucket"], want["side"]
        position = lane_position(target_conn, station_id=station, event_date=day, token_id=token)
        target = round(float(want["target"]), 6)
        gap = round(target - float(position["held"]), 6)
        row: dict[str, Any] = {
            "source_user": want.get("source_user") or user, "token_id": token, "station_id": station, "event_date": day,
            "bucket": bucket, "side": side, "evaluated_at_utc": now_utc, "rule_version": rule_version,
            "target_shares": target, "held_shares": float(position["held"]), "gap_shares": gap,
        }
        action = "BUY" if gap >= min_shares else "SELL" if -gap >= min_shares else None
        plan: dict[str, Any] = {}
        if action is None:
            continue
        row["order_action"] = action
        if position["open_order"]:
            plan = {"refusal": "order_open"}
        elif action == "SELL" and (cluster_rule or cfg.get("mirror_sells", True) is not True):
            plan = {"refusal": "sells_not_mirrored"}
        elif action == "SELL" and want["sell_price"] is None:
            plan = {"refusal": "no_source_sell_price"}
        elif sends >= int(cfg.get("max_sends_per_pass") or 0):
            plan = {"refusal": "pass_send_limit"}
        elif _unsent_recently(target_conn, token, now_utc=now_utc,
                              seconds=float(cfg.get("unsent_retry_seconds") or 0.0)):
            plan = {"refusal": "unsent_retry_wait"}
        else:
            book, book_refusal = fresh_book(
                target_conn, manifest_root(), station_id=station, event_date=day, now_utc=now_utc,
                fresh_seconds=float(cfg.get("send_book_fresh_seconds") or 60.0), live=live,
            )
            if book is None:
                plan = {"refusal": str(book_refusal)}
            else:
                book_row = next((b for b in book.get("buckets") or () if str(b.get("bucket")) == bucket), None)
                quote = side_quote(book_row, side)
                row.update({"book_source": book.get("book_source") or "committed_snapshot",
                            "book_observed_at": book.get("observed_at"), "book_age_seconds": book.get("age_seconds"),
                            "bid": quote["bid"], "ask": quote["ask"]})
                if book_row is None:
                    plan = {"refusal": "bucket_not_on_book"}
                elif action == "BUY":
                    row["source_price"] = want["buy_price"]
                    cap = min(float(want["buy_price"]) + slippage, MAX_PRICE)
                    if cluster_rule:
                        # Charter 257: judged again at the send -- our newest served probability
                        # must still clear the cap by the margin, else the follow waits.
                        served = served_side_probability(
                            target_conn, station_id=station, event_date=day, bucket=bucket, side=side,
                            stages=stages, now_utc=now_utc,
                        )
                        if served.get("state") != "ok":
                            plan = {"refusal": f"model:{served.get('state')}"}
                        else:
                            cap = min(cap, float(served["p"]) - margin)
                            row.update(our_p=float(served["p"]), model_stage=served.get("stage"),
                                       model_margin=float(served["p"]) - float(want["buy_price"]), price_cap=cap)
                            if cap < float(want["buy_price"]) - 1e-9:
                                plan = {"refusal": f"model_margin_short:{float(served['p']):.3f}<{float(want['buy_price']) + margin:.3f}"}
                    if not plan:
                        plan = plan_buy(quote, price_cap=cap, gap=gap, min_shares=min_shares)
                    if not plan.get("refusal") and float(plan["notional_usdc"]) > budget_left + 1e-9:
                        plan = {"refusal": f"daily_cost_limit:{max(budget_left, 0.0):.2f}"}
                    if not plan.get("refusal"):
                        cash = _cash_refusal(target_conn, now_utc=now_utc, needed=float(plan["notional_usdc"]))
                        if cash:
                            plan = {"refusal": cash}
                else:
                    row["source_price"] = want["sell_price"]
                    wallet = position_reader(token)
                    if not wallet.get("ok"):
                        plan = {"refusal": f"position_unreadable:{wallet.get('reason') or wallet.get('error') or ''}"[:120]}
                    else:
                        sellable = min(float(position["held"]), float(wallet.get("shares") or 0.0))
                        plan = plan_sell(price_floor=max(float(want["sell_price"]) - slippage, SIGNING_TICK),
                                         surplus=-gap, sellable=sellable, min_shares=min_shares)
        if plan.get("refusal"):
            refusal = str(plan["refusal"])
            refusals[refusal.split(":")[0]] = refusals.get(refusal.split(":")[0], 0) + 1
            if _last_refusal(target_conn, token) != refusal:
                _record(target_conn, {**row, "refusal": refusal})
            continue
        intent_id = f"{INTENT_PREFIX}_" + content_sha256({"token": token, "at": now_utc, "action": action,
                                                           "shares": plan["shares"]})[:32]
        row.update({"order_kind": plan["order_kind"], "limit_price": plan["limit_price"], "shares": plan["shares"],
                    "notional_usdc": plan["notional_usdc"], "order_intent_id": intent_id})
        _record(target_conn, row)
        plan_body = {**{k: row.get(k) for k in ("token_id", "station_id", "event_date", "bucket", "side",
                                                 "target_shares", "held_shares", "gap_shares", "source_price",
                                                 "book_source", "book_observed_at", "bid", "ask", "our_p",
                                                 "price_cap", "model_stage", "model_margin")},
                     **plan, "source_user": row["source_user"], "rule": rule_version}
        sends += 1
        if action == "BUY":
            venue: dict[str, Any] = {}
            if plan["order_kind"] == "rest":
                until = _z(_utc(now_utc) + timedelta(seconds=horizon)) if horizon > 0.0 else now_utc
                venue = resting_order_fields(now_utc=now_utc, limit_price=float(plan["limit_price"]),
                                             belief_fresh_until_utc=until)
            details = {
                "schema": "copy_follow_lane_decision_v1", "lane": LANE, "title": LANE_TITLE, "rule": rule_version,
                "decision_signature": {"stage": "COPY", "lane": LANE, "rule": rule_version},
                "forward_evidence_binding": {"decision_at_utc": now_utc, "source_user": user,
                                             "ws_snapshot_id": row.get("book_observed_at")},
                "plan": plan_body,
            }
            entry = send_lane_buy(
                target_conn, lane=LANE, intent_id=intent_id, station_id=station, event_date=day,
                settlement_timezone=want["settlement_timezone"], bucket=bucket, token_id=token,
                price=float(plan["limit_price"]), shares=float(plan["shares"]), side=side,
                execution_candidate="TAKER_FOK" if plan["order_kind"] == "take" else "MAKER",
                fok_price_cap=plan.get("fok_price_cap"), expiration=venue.get("expiration"),
                intent_body={"schema": SCHEMA, "rule": rule_version, "strategy_id": STRATEGY_ID,
                             "order_type": "FOK" if plan["order_kind"] == "take" else venue.get("order_type"),
                             "post_only": plan["order_kind"] == "rest",
                             **({"expiration_basis": "rung_horizon_seconds"} if venue else {}),
                             "plan": plan_body},
                transport=transport, live_submit_authorized=live_submit_authorized, decision_details=details,
            )
            budget_left -= float(plan["notional_usdc"])
        else:
            expiry = resting_order_fields(
                now_utc=now_utc, limit_price=float(plan["limit_price"]),
                belief_fresh_until_utc=_z(_utc(now_utc) + timedelta(seconds=max(horizon, 0.0))),
            )["expiration"]
            entry = send_exit_sell(
                target_conn, lane=LANE, intent_id=intent_id, station_id=station, event_date=day,
                settlement_timezone=want["settlement_timezone"], bucket=bucket, token_id=token,
                price=float(plan["limit_price"]), shares=float(plan["shares"]), order_type="GTD",
                post_only=False, expiration=int(expiry), tick_size=SIGNING_TICK, outcome_side=side,
                intent_body={"schema": SCHEMA, "rule": rule_version, "strategy_id": STRATEGY_ID,
                             "plan": plan_body},
                transport=transport, live_submit_authorized=live_submit_authorized,
            )
        sent = bool(entry.get("exchange_post_started"))
        matched = float(entry.get("matched_size") or 0.0)
        submitted += int(sent)
        filled += int(matched > 0.0)
        outcome = None if sent else f"send:{entry.get('reason') or entry.get('state')}"
        send = {"state": entry.get("state"), "event_type": entry.get("event_type"), "reason": entry.get("reason"),
                "matched_size": matched, "exchange_post_started": sent, "at": now_utc}
        target_conn.execute(
            f"UPDATE {EVALUATION_TABLE} SET sent=?, filled_shares=?, refusal=?, send_json=? WHERE order_intent_id=?",
            (int(sent), matched, outcome, json.dumps(send, ensure_ascii=False, sort_keys=True), intent_id),
        )
        receipts.append({"station_id": station, "event_date": day, "bucket": bucket, "side": side,
                         "action": action, "kind": plan["order_kind"], "price": plan["limit_price"],
                         "shares": plan["shares"], "order_intent_id": intent_id, **send})
    return {
        **receipt,
        "state": "ran",
        "rule": rule_version,
        "source_user": user,
        **({"source_users": cfg["source_users"]} if cluster_rule else {}),
        "ingest": ingest,
        "tokens": len(targets),
        "planned": len(receipts),
        "submitted": submitted,
        "filled": filled,
        "refusals": refusals,
        "cycles_settled": cycles_settled,
        "receipts": receipts,
    }


#: The source's marked value, kept across passes of one resident process.
_RATIO_CACHE: dict[str, Any] = {}


__all__ = [
    "EVALUATION_TABLE",
    "LANE",
    "LANE_TITLE",
    "RULE_V1",
    "RULE_V2",
    "cluster_targets",
    "fetch_source_taker_trades",
    "ingest_cluster_taker_trades",
    "lead_hours",
    "served_side_probability",
    "SOURCE_TABLE",
    "copy_ratio",
    "ensure_lane_schema",
    "execute_copy_follow",
    "fetch_source_trades",
    "ingest_source_trades",
    "lane_cost_today",
    "lane_position",
    "map_source_token",
    "parse_event_slug",
    "plan_buy",
    "plan_sell",
    "side_quote",
    "source_targets",
]
