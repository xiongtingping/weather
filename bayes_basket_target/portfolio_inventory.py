"""What the whole book is holding, and whether it may take on more.

The per-station inventory snapshot answers "what does this station hold".
Nothing answered "what does the account hold", so nothing could notice that
five stations under one air mass had become one large position, or that a
run of losses had crossed the point where continuing is a decision rather
than a default.

This module is that second view.  It is a projection: everything is derived
from the append-only ledger, no new writer, and it grants no authority.  It
reports, and the caller decides.

Exposure is aggregated three ways, because a limit is only as good as the
grouping it is checked against:

- by settlement window, since baskets settling in the same hours cannot
  cover each other -- Asian stations settle before the European ones open,
  so a book that looks diversified across the day can be concentrated within
  any one of its windows;
- by regional block, so cities sharing an air mass count once;
- overall, as concentration.

The drawdown breaker stops new intents and never touches existing ones.
Liquidating would realize the loss it is reacting to at whatever price the
book happens to show; letting baskets settle on their own terms is the same
reasoning the P1 hard bound uses when it constrains additions but not
holdings.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .contracts import ContractError, content_sha256
from .account_equity import (
    budget_capped_equity,
    equity_peak_usdc,
    load_account_equity,
    open_exposure_by_station_day,
)

SCHEMA = "basket_target_portfolio_inventory_v1"
POLICY_SCHEMA = "basket_target_portfolio_capital_policy_v1"
DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parent.parent
    / "config/trading/portfolio_capital_policy_v1.json"
)

PORTFOLIO_INVENTORY_DDL = """
CREATE TABLE IF NOT EXISTS basket_portfolio_inventory_snapshot (
  snapshot_id TEXT PRIMARY KEY,
  as_of_utc TEXT NOT NULL,
  open_cycle_count INTEGER NOT NULL CHECK(open_cycle_count>=0),
  open_notional_usdc REAL NOT NULL CHECK(open_notional_usdc>=0),
  reserved_usdc REAL NOT NULL CHECK(reserved_usdc>=0),
  bankroll_usdc REAL NOT NULL CHECK(bankroll_usdc>=0),
  portfolio_hhi REAL NOT NULL CHECK(portfolio_hhi>=0),
  window_exposure_json TEXT NOT NULL,
  block_exposure_json TEXT NOT NULL,
  station_exposure_json TEXT NOT NULL,
  breach_json TEXT NOT NULL,
  new_intents_allowed INTEGER NOT NULL CHECK(new_intents_allowed IN (0,1)),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS basket_portfolio_inventory_as_of_idx
  ON basket_portfolio_inventory_snapshot(as_of_utc);
CREATE TRIGGER IF NOT EXISTS basket_portfolio_inventory_no_update
  BEFORE UPDATE ON basket_portfolio_inventory_snapshot
  BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_portfolio_inventory_no_delete
  BEFORE DELETE ON basket_portfolio_inventory_snapshot
  BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


def load_portfolio_capital_policy(
    path: Path | str = DEFAULT_POLICY_PATH,
) -> tuple[dict[str, Any], str]:
    """Return the Tier-1 capital policy and its content address."""

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema") != POLICY_SCHEMA:
        raise ContractError("portfolio_capital_policy_schema_invalid")
    for key in (
        "portfolio_risk_fraction", "station_cap_fraction",
        "block_exposure_cap_fraction", "settlement_window_cap_fraction",
    ):
        value = raw.get(key)
        if not isinstance(value, (int, float)) or not (
            0.0 < float(value) <= 1.0
        ):
            raise ContractError(f"portfolio_capital_policy_{key}_invalid")
    breaker = raw.get("drawdown_circuit_breaker")
    if not isinstance(breaker, Mapping):
        raise ContractError("portfolio_capital_policy_breaker_missing")
    if int(breaker.get("rolling_days") or 0) < 1:
        raise ContractError("portfolio_capital_policy_breaker_window_invalid")
    if not (0.0 < float(breaker.get("max_drawdown_fraction") or 0.0) <= 1.0):
        raise ContractError(
            "portfolio_capital_policy_breaker_threshold_invalid"
        )
    if str(breaker.get("action")) != "STOP_NEW_INTENTS_ONLY":
        # The only action this projection can justify.  Anything that closes
        # positions would realize the loss it is reacting to.
        raise ContractError("portfolio_capital_policy_breaker_action_invalid")
    return raw, content_sha256(raw)


def _settlement_window(event_date: str, settlement_timezone: str) -> str:
    """Bucket a station-day by when it settles, in whole UTC hours.

    Two baskets settling in the same hours cannot cover each other, so the
    window -- not the calendar date -- is the unit a concurrency limit has to
    be checked against.
    """

    try:
        from zoneinfo import ZoneInfo

        local_end = datetime.fromisoformat(
            str(event_date) + "T00:00:00"
        ).replace(tzinfo=ZoneInfo(str(settlement_timezone))) + timedelta(
            days=1
        )
        utc_end = local_end.astimezone(timezone.utc)
    except (ValueError, KeyError, TypeError):
        return f"UNKNOWN:{event_date}"
    return utc_end.strftime("%Y-%m-%dT%H:00Z")


def build_portfolio_inventory(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    blocks_by_station: Mapping[str, str],
    policy: Mapping[str, Any],
    budget_usdc: float | None = None,
    default_block: str = "UNBLOCKED_SINGLETON",
) -> dict[str, Any]:
    """Aggregate open exposure and evaluate every Tier-1 bound.

    Charter 141 (2026-09-17): the bankroll is the real account -- exchange
    cash plus the marked value of open positions, read by
    ``account_equity`` and capped by the operator's budget dial -- not the
    pool counter, which only ever subtracted fills from a paper balance and
    read 297.98 against an account of 99.80.  Exposure is what the ledger
    still has at risk: filled cost on station-days that have not settled
    plus resting buys.  The whole-book bound reads the part of the account
    that is not cash (positions plus resting buys), the same number the
    runtime bounds each station-day by.  With no fresh account read the book
    takes on nothing: ``account_equity_unavailable`` is a breach.
    """

    account = load_account_equity(conn, as_of_utc=as_of_utc)
    bankroll = (
        budget_capped_equity(account["equity_usdc"], budget_usdc)
        if account is not None else 0.0
    )
    reserved = float(account["resting_buy_usdc"]) if account is not None else 0.0
    exposure_rows = open_exposure_by_station_day(conn, as_of_utc=as_of_utc)

    by_station: dict[str, float] = {}
    by_window: dict[str, float] = {}
    by_block: dict[str, float] = {}
    for station_id, event_date, settlement_timezone, notional in exposure_rows:
        value = float(notional or 0.0)
        if value <= 0.0:
            continue
        station = str(station_id).strip().upper()
        block = str(
            blocks_by_station.get(station) or f"{default_block}:{station}"
        )
        window = _settlement_window(
            str(event_date), str(settlement_timezone)
        )
        by_station[station] = by_station.get(station, 0.0) + value
        by_block[block] = by_block.get(block, 0.0) + value
        by_window[window] = by_window.get(window, 0.0) + value

    total = sum(by_station.values())
    not_cash = (
        float(account["positions_value_usdc"]) + float(account["resting_buy_usdc"])
        if account is not None else total
    )
    hhi = (
        sum((value / total) ** 2 for value in by_station.values())
        if total > 0.0
        else 0.0
    )

    breaches: list[dict[str, Any]] = []
    if account is None:
        breaches.append({"limit": "account_equity_unavailable"})
    if bankroll > 0.0:
        risk_cap = bankroll * float(policy["portfolio_risk_fraction"])
        if not_cash > risk_cap + 1e-9:
            breaches.append({
                "limit": "portfolio_risk_fraction",
                "observed_usdc": round(not_cash, 6),
                "cap_usdc": round(risk_cap, 6),
            })
        station_cap = bankroll * float(policy["station_cap_fraction"])
        for station, value in sorted(by_station.items()):
            if value > station_cap + 1e-9:
                breaches.append({
                    "limit": "station_cap_fraction",
                    "scope": station,
                    "observed_usdc": round(value, 6),
                    "cap_usdc": round(station_cap, 6),
                })
        block_cap = bankroll * float(policy["block_exposure_cap_fraction"])
        for block, value in sorted(by_block.items()):
            if value > block_cap + 1e-9:
                breaches.append({
                    "limit": "block_exposure_cap_fraction",
                    "scope": block,
                    "observed_usdc": round(value, 6),
                    "cap_usdc": round(block_cap, 6),
                })
        window_cap = (
            bankroll * float(policy["settlement_window_cap_fraction"])
        )
        for window, value in sorted(by_window.items()):
            if value > window_cap + 1e-9:
                breaches.append({
                    "limit": "settlement_window_cap_fraction",
                    "scope": window,
                    "observed_usdc": round(value, 6),
                    "cap_usdc": round(window_cap, 6),
                })

    if account is not None:
        drawdown = evaluate_drawdown_breaker(
            conn, as_of_utc=as_of_utc, policy=policy,
            bankroll_usdc=float(account["equity_usdc"]),
        )
    else:
        # No current read is not a drawdown to 0: the unavailable account is
        # already its own breach, and a peak against nothing measures nothing.
        breaker = policy["drawdown_circuit_breaker"]
        drawdown = {
            "rolling_days": int(breaker["rolling_days"]),
            "max_drawdown_fraction": float(breaker["max_drawdown_fraction"]),
            "peak_usdc": None,
            "current_usdc": None,
            "drawdown_fraction": None,
            "tripped": False,
            "action": str(breaker["action"]),
        }
    if drawdown["tripped"]:
        breaches.append({
            "limit": "drawdown_circuit_breaker",
            "observed_fraction": drawdown["drawdown_fraction"],
            "cap_fraction": drawdown["max_drawdown_fraction"],
        })

    identity = {
        "schema": SCHEMA,
        "as_of_utc": str(as_of_utc),
        "open_cycle_count": len(exposure_rows),
        "open_notional_usdc": round(total, 6),
        "reserved_usdc": round(reserved, 6),
        "bankroll_usdc": round(bankroll, 6),
        "account_not_cash_usdc": round(not_cash, 6),
        "portfolio_hhi": round(hhi, 8),
        "window_exposure_usdc": {
            key: round(value, 6) for key, value in sorted(by_window.items())
        },
        "block_exposure_usdc": {
            key: round(value, 6) for key, value in sorted(by_block.items())
        },
        "station_exposure_usdc": {
            key: round(value, 6) for key, value in sorted(by_station.items())
        },
        "breaches": breaches,
        # Breaching a bound stops the book taking on more; it never closes
        # what is already open.
        "new_intents_allowed": not breaches,
        "drawdown": drawdown,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def evaluate_drawdown_breaker(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    policy: Mapping[str, Any],
    bankroll_usdc: float,
) -> dict[str, Any]:
    """Rolling peak-to-current drawdown of the real account (charter 141).

    The curve is ``basket_target_account_equity``: every account read the
    resident recorded.  It read ``basket_target_capital_pool_history`` until
    2026-09-17, a table no writer ever created, so the breaker abstained on
    every call it was ever asked.  No reads in the window: abstain.
    """

    breaker = policy["drawdown_circuit_breaker"]
    days = int(breaker["rolling_days"])
    threshold = float(breaker["max_drawdown_fraction"])
    try:
        as_of = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
        since = (as_of - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        until = as_of.strftime("%Y-%m-%dT%H:%M:%S.999Z")
    except ValueError as exc:
        raise ContractError("portfolio_drawdown_as_of_invalid") from exc
    peak = equity_peak_usdc(conn, since_utc=since, until_utc=until) or 0.0
    current = float(bankroll_usdc)
    peak = max(peak, current)
    fraction = 0.0 if peak <= 0.0 else max(0.0, (peak - current) / peak)
    return {
        "rolling_days": days,
        "max_drawdown_fraction": threshold,
        "peak_usdc": round(peak, 6),
        "current_usdc": round(current, 6),
        "drawdown_fraction": round(fraction, 6),
        "tripped": bool(fraction > threshold + 1e-12),
        "action": str(breaker["action"]),
    }


def persist_portfolio_inventory(
    conn: sqlite3.Connection,
    snapshot: Mapping[str, Any],
    *,
    created_at_utc: str,
) -> int:
    """Append one projection snapshot; identical evidence replays as no-op."""

    conn.executescript(PORTFOLIO_INVENTORY_DDL)
    sha = str(snapshot["content_sha256"])
    conn.execute(
        "INSERT OR IGNORE INTO basket_portfolio_inventory_snapshot("
        "snapshot_id,as_of_utc,open_cycle_count,open_notional_usdc,"
        "reserved_usdc,bankroll_usdc,portfolio_hhi,window_exposure_json,"
        "block_exposure_json,station_exposure_json,breach_json,"
        "new_intents_allowed,content_sha256,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "portinv-" + sha[:32], str(snapshot["as_of_utc"]),
            int(snapshot["open_cycle_count"]),
            float(snapshot["open_notional_usdc"]),
            float(snapshot["reserved_usdc"]),
            float(snapshot["bankroll_usdc"]),
            float(snapshot["portfolio_hhi"]),
            json.dumps(snapshot["window_exposure_usdc"], sort_keys=True),
            json.dumps(snapshot["block_exposure_usdc"], sort_keys=True),
            json.dumps(snapshot["station_exposure_usdc"], sort_keys=True),
            json.dumps(snapshot["breaches"], sort_keys=True),
            1 if snapshot["new_intents_allowed"] else 0,
            sha, str(created_at_utc),
        ),
    )
    return int(conn.execute("SELECT changes()").fetchone()[0])
