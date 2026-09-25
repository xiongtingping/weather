#!/usr/bin/env python3
"""The paper ledger, decided by production itself.

User ruling 2026-09-24: 「别用账本，账本和生产没有对齐，先对齐」 then 「账本对齐」.

Every basket decision in this ledger is a call into production's own compile
entries -- ``p0_preselection.compile_p0_preselection`` for a forward day
(P0/P0-M on the recorded forward book) and
``production_io.compile_production_cycle_from_ssot`` for a started one (P1 on
the recorded resident book) -- at the instant production itself compiled, on
the evidence production itself read.  Production's own ``prepare_production_
candidate`` turns the compiled plan into the decision details and the order
intents, exactly as the atomic commit would write them.  Nothing here selects
a bucket or sizes a leg.

What the ledger swaps in is only what it simulates, through one object with
the same questions ``production_io.SsotChainState`` answers from the SSOT:

* the chain's history -- its own decisions (core, direction, charter 77's
  signature), the legs it sent (charter 143), the identity it last bought
  under (71(a)), the opener's tilt, the orders that ended unfilled (135 一);
* its holdings and resting orders, from its own simulated fills;
* in ``--capital paper``, its own account (charter 141's equity read).

The venue is the one thing that has to be modelled, and only its answer is:
a fill-or-kill crosses the recorded book at the decision (all or nothing at
the cap); a resting maker order fills at the time the calibrated fill curves
draw for it (``scripts/paper_ledger_fill_curves.py``: Kaplan-Meier time to
first fill of production's real basket maker orders, by lead band and by
whether the bucket settled), unless production's own rule
(``resting_rung_keep_or_cancel``) cancels it or its GTD horizon ends it
first.  The recorded books cannot answer a maker directly -- a seller hitting
a resting bid never shows as the ask crossing it, and the store keeps no
trade tape; the crossing rule predicted 0 of 52 real fills (2026-09-25).
Settlement is production's label (``basket_station_day_settlement_label``).

``--capital production`` (the default) hands the compiles production's own
account read and recorded station budgets, so with nothing filled the ledger
must reproduce production decision for decision: that is the alignment
check, and ``matches_production`` records it on every row.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target import production_worker as W  # noqa: E402
from bayes_basket_target.activation import load_activation  # noqa: E402
from bayes_basket_target.atomic_ledger import chain_last_action_identity  # noqa: E402
from bayes_basket_target.basket_kernel.delta_v2 import ORDER_END_EVENT_TYPES  # noqa: E402
from bayes_basket_target.consolidation_chain import opener_tilt_from_plan  # noqa: E402
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.market_materialization import (  # noqa: E402
    build_confirmed_inventory_snapshot,
    build_order_state_snapshot,
)
from bayes_basket_target.p0_preselection import (  # noqa: E402
    UNPERSISTED_MARKET_SNAPSHOT_KEY,
    compile_p0_preselection,
    forward_book_from_snapshot_row,
)
from bayes_basket_target.production import prepare_production_candidate  # noqa: E402
from bayes_basket_target.production_io import (  # noqa: E402
    CHAIN_SEAT_EVENT_TYPES,
    COMPLETION_EXECUTION_CANDIDATE,
    SsotChainState,
    account_capital_from_equity_row,
    compile_production_cycle_from_ssot,
    prior_core_from_details,
    select_last_decision_signature,
)
from bayes_basket_target.account_equity import ACCOUNT_EQUITY_DDL_STATEMENTS, TABLE as ACCOUNT_EQUITY_TABLE  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.portfolio_allocator import load_regional_blocks  # noqa: E402
from bayes_basket_target.portfolio_inventory import (  # noqa: E402
    build_portfolio_inventory,
    load_portfolio_capital_policy,
)
from bayes_basket_target.quote_freshness import QuoteFreshnessPolicy  # noqa: E402
from bayes_basket_target.resting_ladder_lifecycle import (  # noqa: E402
    RESTING_BOOK_STALE_SECONDS,
    resting_rung_keep_or_cancel,
)

import paper_ledger_fill_curves as F  # noqa: E402

SSOT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
MAKER_FILL_CURVES = ROOT / "config" / "paper_ledger" / "maker_fill_curves_v1.json"
ARCHIVE_DB = ROOT / "data" / "archive.db"
LEDGER_DB = ROOT / "data" / "paper_ledger_aligned.db"
SCHEMA = "paper_ledger_aligned_v1"

# The recorded plan and the compiled plan differ, by construction, in what the
# atomic commit stamps after the compile: its own ids, the admission deadline
# and the opening stage's evidence root.  Everything else is the decision.
COMMIT_STAMPED_KEYS = frozenset({
    "production_cycle_sha256", "target_execution_hard_deadline_at",
    "upstream_plan_id", "plan_id", "delta_v2_required",
    "live_submit_authorized", "content_sha256",
    "stage_evidence_root_id", "stage_evidence_content_sha256",
    "probability_checkpoint_id", "soft_bound_id",
})
FORWARD_BOOK_PREFIX = "p0_ws_"
EPS = 1e-9


def _instant(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _before(left: Any, right: Any) -> bool:
    return _instant(left) < _instant(right)


def _at_or_before(left: Any, right: Any) -> bool:
    return _instant(left) <= _instant(right)


# --------------------------------------------------------------------------
# The simulated venue's orders.
# --------------------------------------------------------------------------


@dataclass
class PaperOrder:
    order_id: str
    station_id: str
    event_date: str
    bucket: str
    token_id: str
    candidate: str
    limit_price: float
    size: float
    placed_at: str
    expires_at: str | None
    ladder_rung: bool
    decision_created_at: str
    events: list[tuple[str, str, float, float]] = field(default_factory=list)
    settlement_timezone: str = "UTC"
    fill_at: str | None = None
    state: str = "resting"
    ended_at: str | None = None
    end_reason: str = ""
    last_book_at: str | None = None

    def filled_by(self, at: str | None = None) -> tuple[float, float]:
        shares = cost = 0.0
        for when, kind, size, notional in self.events:
            if kind not in {"FILL", "PARTIAL_FILL"}:
                continue
            if at is not None and not _at_or_before(when, at):
                continue
            shares += size
            cost += notional
        return shares, cost

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled_by()[0])

    def event(self, when: str, kind: str, size: float = 0.0, notional: float = 0.0) -> None:
        self.events.append((when, kind, float(size), float(notional)))

    @property
    def was_sent(self) -> bool:
        """Charter 136 (五)'s "sent": an exchange answer on the order."""

        return any(kind in CHAIN_SEAT_EVENT_TYPES for _w, kind, _s, _n in self.events)

    def ends_by(self, at: str | None) -> int:
        return sum(
            1 for when, kind, _s, _n in self.events
            if kind in ORDER_END_EVENT_TYPES
            and (at is None or _at_or_before(when, at))
        )

    def resting_at(self, at: str) -> bool:
        if self.candidate != "MAKER" or not _before(self.placed_at, at):
            return False
        return self.ended_at is None or _before(at, self.ended_at)


@dataclass
class ChainDay:
    station_id: str
    event_date: str
    decisions: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    orders: list[PaperOrder] = field(default_factory=list)
    tilt: tuple[str, float] | None = None
    last_action: tuple[str, str] | None = None


# --------------------------------------------------------------------------
# The chain state production's compiles read, answered from the simulation.
# --------------------------------------------------------------------------


class PaperChainState:
    """``SsotChainState``'s questions, answered from what this ledger did.

    One instance per run; ``before_utc`` is set to each decision's clock
    before its compile, exactly as a replay sets ``SsotChainState``'s.  The
    store reads that are evidence -- books, probabilities, claim tables --
    stay the SSOT's and are bounded by the same clock.
    """

    def __init__(
        self,
        ssot: sqlite3.Connection,
        *,
        capital: str,
        paper_account: "PaperAccount | None" = None,
    ) -> None:
        self.ssot = ssot
        self.capital = capital
        self.paper_account = paper_account
        self.before_utc: str | None = None
        self.days: dict[tuple[str, str], ChainDay] = {}
        self._ssot_state = SsotChainState(ssot)

    # -- clocks ------------------------------------------------------------
    @property
    def tables_as_of_utc(self) -> str | None:
        return self.before_utc

    @property
    def store_before_utc(self) -> str | None:
        return self.before_utc

    def day(self, station_id: str, event_date: str) -> ChainDay:
        key = (str(station_id), str(event_date))
        if key not in self.days:
            self.days[key] = ChainDay(*key)
        return self.days[key]

    # -- holdings ----------------------------------------------------------
    def _fills(self, station_id: str, event_date: str, *, at: str | None, strict: bool) -> Iterator[tuple[PaperOrder, float, float]]:
        for order in self.day(station_id, event_date).orders:
            shares = cost = 0.0
            for when, kind, size, notional in order.events:
                if kind not in {"FILL", "PARTIAL_FILL"}:
                    continue
                if at is not None and (
                    not _before(when, at) if strict else not _at_or_before(when, at)
                ):
                    continue
                shares += size
                cost += notional
            if shares > EPS:
                yield order, shares, cost

    def holdings(self, *, station_id: str, event_date: str, observed_at_utc: str) -> tuple[dict[str, float], dict[str, float]]:
        at = observed_at_utc
        if self.before_utc is not None and _before(self.before_utc, observed_at_utc):
            at = self.before_utc
        filled: dict[str, float] = {}
        cost: dict[str, float] = {}
        for order, shares, notional in self._fills(station_id, event_date, at=at, strict=False):
            filled[order.bucket] = filled.get(order.bucket, 0.0) + shares
            cost[order.bucket] = cost.get(order.bucket, 0.0) + notional
        return filled, cost

    def token_holdings(self, *, station_id: str, event_date: str) -> dict[str, Any]:
        totals: dict[str, dict[str, Any]] = {}
        for order, shares, notional in self._fills(station_id, event_date, at=self.before_utc, strict=True):
            entry = totals.setdefault(order.token_id, {"shares": 0.0, "cost_usdc": 0.0, "fill_count": 0})
            entry["shares"] += shares
            entry["cost_usdc"] += notional
            entry["fill_count"] += 1
        return {
            "schema": "bayes_basket_ledger_holdings_v1",
            "station_id": station_id,
            "event_date": event_date,
            "holdings": {token: dict(entry) for token, entry in sorted(totals.items())},
            "fill_content_sha256": [],
        }

    def book_holdings(self, *, station_id: str, event_date: str, observed_at_utc: str, account_filled: Mapping[str, float]) -> tuple[dict[str, float], dict[str, float]]:
        # The paper account is the basket's alone: no other lane's sell can
        # have taken a leg out of it, so the account ceiling does not apply.
        filled, cost = self.holdings(station_id=station_id, event_date=event_date, observed_at_utc=observed_at_utc)
        buckets = set(map(str, account_filled)) | set(filled)
        return (
            {bucket: float(filled.get(bucket, 0.0)) for bucket in buckets},
            {bucket: float(cost.get(bucket, 0.0)) for bucket in buckets},
        )

    def pending_orders(self, *, station_id: str, event_date: str, account_pending: Mapping[str, Any], account_pending_notional: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
        at = self.before_utc
        pending: dict[str, float] = {str(b): 0.0 for b in account_pending}
        notional: dict[str, float] = {str(b): 0.0 for b in account_pending_notional}
        if at is None:
            return pending, notional
        for order in self.day(station_id, event_date).orders:
            if not order.resting_at(at):
                continue
            left = max(0.0, order.size - order.filled_by(at)[0])
            pending[order.bucket] = pending.get(order.bucket, 0.0) + left
            notional[order.bucket] = notional.get(order.bucket, 0.0) + left * order.limit_price
        return pending, notional

    # -- history -----------------------------------------------------------
    def sent_shares(self, *, station_id: str, event_date: str) -> dict[str, float]:
        sent: dict[str, float] = {}
        for order in sorted(self.day(station_id, event_date).orders, key=lambda o: (o.placed_at, o.order_id)):
            if self.before_utc is not None and not _before(order.placed_at, self.before_utc):
                continue
            if order.candidate == COMPLETION_EXECUTION_CANDIDATE:
                continue
            if not order.was_sent:
                continue
            sent[order.bucket] = max(0.0, float(order.size))
        return sent

    def _decisions_before(self, station_id: str, event_date: str) -> list[tuple[str, dict[str, Any]]]:
        rows = self.day(station_id, event_date).decisions
        if self.before_utc is None:
            return list(rows)
        return [row for row in rows if _before(row[0], self.before_utc)]

    def prior_core(self, *, station_id: str, event_date: str, canonical_buckets: Sequence[str]) -> tuple[tuple[str, ...], str | None]:
        rows = self._decisions_before(station_id, event_date)
        return prior_core_from_details(rows[-1][1] if rows else None, canonical_buckets)

    def _order_ends(self, station_id: str, event_date: str, at: str | None) -> int:
        return sum(order.ends_by(at) for order in self.day(station_id, event_date).orders)

    def last_decision_signature(self, *, station_id: str, event_date: str, stage: str) -> dict[str, Any] | None:
        rows = self._decisions_before(station_id, event_date)

        def _ends_at_last_moment() -> int:
            moments = [
                when for when, details in rows
                if isinstance(details.get("decision_signature"), Mapping)
                and str(details["decision_signature"].get("stage") or "") == str(stage)
                and details["decision_signature"].get("is_decision_moment") is True
            ]
            return self._order_ends(station_id, event_date, max(moments, key=_instant)) if moments else 0

        return select_last_decision_signature(
            (details for _when, details in reversed(rows[-24:])),
            stage=stage,
            ends_now=self._order_ends(station_id, event_date, self.before_utc),
            ends_at_last_moment=_ends_at_last_moment,
        )

    def last_acted_probability_identity(self, *, station_id: str, event_date: str) -> str | None:
        action = self.day(station_id, event_date).last_action
        if action is None:
            return None
        if self.before_utc is not None and not _before(action[0], self.before_utc):
            return None
        return action[1]

    def chain_tilt(self, *, station_id: str, event_date: str) -> float | None:
        tilt = self.day(station_id, event_date).tilt
        if tilt is None:
            return None
        if self.before_utc is not None and not _before(tilt[0], self.before_utc):
            return None
        return tilt[1]

    # -- the account -------------------------------------------------------
    def account_capital(self, *, evaluation_at_utc: str) -> dict[str, Any] | None:
        if self.capital == "production" or self.paper_account is None:
            return self._ssot_state.account_capital(evaluation_at_utc=evaluation_at_utc)
        return account_capital_from_equity_row(
            self.paper_account.equity_row(self, at=evaluation_at_utc)
        )

    def remaining_budget_usdc(self, *, station_id: str, event_date: str, recorded_usdc: float) -> float:
        # The resident book records the operator's budget dial; paper and
        # production read the same dial.
        return float(recorded_usdc)


@dataclass
class PaperAccount:
    """The simulated account (``--capital paper``): cash, positions, resting buys."""

    opening_cash_usdc: float
    marks: "BookStore"
    settlements: Mapping[tuple[str, str], Mapping[str, Any]]

    def equity_row(self, state: PaperChainState, *, at: str) -> dict[str, Any]:
        cash = float(self.opening_cash_usdc)
        positions = 0.0
        resting = 0.0
        for (station, day), chain in state.days.items():
            held: dict[str, float] = {}
            for order in chain.orders:
                shares, cost = order.filled_by(at)
                cash -= cost
                if shares > EPS:
                    held[order.bucket] = held.get(order.bucket, 0.0) + shares
                if order.resting_at(at):
                    resting += (order.size - shares) * order.limit_price
            label = self.settlements.get((station, day))
            if label is not None and _at_or_before(label["known_at_utc"], at):
                cash += float(held.get(str(label["settled_bucket"]), 0.0))
                continue
            for bucket, shares in held.items():
                positions += shares * self.marks.mark(station, day, bucket, at=at)
        return {
            "observed_at_utc": at,
            "cash_usdc": cash,
            "positions_value_usdc": positions,
            "equity_usdc": cash + positions,
            "resting_buy_usdc": resting,
        }


class PaperPortfolio:
    """Production's portfolio machinery, run on the paper account.

    The forward loop sizes each station by a quota: a breached Tier-1 bound
    (charter 141) gives nothing, otherwise the allocator splits the
    budget-capped bankroll by each station's last edge.  Production reads the
    real account for it; here the same functions read an in-memory store that
    holds only the paper account -- its equity reads in production's own
    table, its orders in the three tables the exposure query joins -- so the
    bounds, the drawdown breaker and the split are production's, applied to
    what the ledger holds.
    """

    def __init__(self, ssot: sqlite3.Connection) -> None:
        self.ssot = ssot
        self.store = sqlite3.connect(":memory:")
        for ddl in ACCOUNT_EQUITY_DDL_STATEMENTS:
            self.store.execute(ddl)
        self.store.executescript(
            "CREATE TABLE auto_trade_decisions(decision_id TEXT PRIMARY KEY,"
            " station_id TEXT, event_date TEXT, settlement_timezone TEXT);"
            "CREATE TABLE basket_target_order_intents(order_intent_id TEXT PRIMARY KEY,"
            " decision_id TEXT, order_action TEXT, requested_notional_usdc REAL);"
            "CREATE TABLE basket_target_order_reconciliations(order_intent_id TEXT PRIMARY KEY,"
            " state TEXT, filled_size REAL, filled_notional_usdc REAL);"
        )
        self.blocks_by_station, _ = load_regional_blocks()
        self.policy, _ = load_portfolio_capital_policy()
        row = ssot.execute(
            "SELECT artifact_json FROM basket_delta_v2_policy_artifact ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.minimum_clip_usdc = float(min(json.loads(str(row[0]))["cash_clip_grid"])) if row else 0.0
        self.last_forward_plan: dict[str, Mapping[str, Any]] = {}
        self._timezones: dict[tuple[str, str], str] = {}

    def _timezone(self, station_id: str, event_date: str) -> str:
        key = (station_id, event_date)
        if key not in self._timezones:
            row = self.ssot.execute(
                "SELECT settlement_timezone FROM basket_target_market_registry"
                " WHERE station_id=? AND event_date=? LIMIT 1", key,
            ).fetchone()
            self._timezones[key] = str(row[0]) if row else "UTC"
        return self._timezones[key]

    def record_equity(self, row: Mapping[str, Any]) -> None:
        if float(row["cash_usdc"]) < -1e-6:
            raise ContractError("paper_account_cash_negative")
        identity = {
            "schema": "paper_ledger_aligned_account_v1",
            "observed_at_utc": str(row["observed_at_utc"]),
            "cash_usdc": round(max(0.0, float(row["cash_usdc"])), 6),
            "positions_value_usdc": round(float(row["positions_value_usdc"]), 6),
            "equity_usdc": round(max(0.0, float(row["equity_usdc"])), 6),
            "resting_buy_usdc": round(float(row["resting_buy_usdc"]), 6),
        }
        sha = content_sha256(identity)
        self.store.execute(
            f"INSERT OR IGNORE INTO {ACCOUNT_EQUITY_TABLE}(equity_id,observed_at_utc,"
            "cash_usdc,cash_observed_at_utc,positions_value_usdc,equity_usdc,"
            "resting_buy_usdc,source_json,content_sha256,created_at_utc)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("paper-" + sha[:32], identity["observed_at_utc"], identity["cash_usdc"],
             identity["observed_at_utc"], identity["positions_value_usdc"],
             identity["equity_usdc"], identity["resting_buy_usdc"], "{}", sha,
             identity["observed_at_utc"]),
        )

    def sync_orders(self, state: "PaperChainState", *, at: str) -> None:
        self.store.execute("DELETE FROM auto_trade_decisions")
        self.store.execute("DELETE FROM basket_target_order_intents")
        self.store.execute("DELETE FROM basket_target_order_reconciliations")
        for (station, day), chain in state.days.items():
            if not chain.orders:
                continue
            decision_id = f"{station}|{day}"
            self.store.execute(
                "INSERT INTO auto_trade_decisions VALUES(?,?,?,?)",
                (decision_id, station, day, self._timezone(station, day)),
            )
            for order in chain.orders:
                if not _at_or_before(order.placed_at, at):
                    continue
                shares, cost = order.filled_by(at)
                live = order.resting_at(at) or order.placed_at == at and order.state == "resting"
                self.store.execute(
                    "INSERT INTO basket_target_order_intents VALUES(?,?,?,?)",
                    (order.order_id, decision_id, "BUY", order.size * order.limit_price),
                )
                self.store.execute(
                    "INSERT INTO basket_target_order_reconciliations VALUES(?,?,?,?)",
                    (order.order_id, "pending" if live else "resolved", shares, cost),
                )

    def forward_budget_usdc(self, state: "PaperChainState", account: "PaperAccount", *, station_id: str, at: str) -> float:
        """The forward loop's station budget, from the paper book."""

        self.record_equity(account.equity_row(state, at=at))
        self.sync_orders(state, at=at)
        planning_budget = float(W._resolve_target_remaining_budget())
        inventory = build_portfolio_inventory(
            self.store, as_of_utc=at, blocks_by_station=self.blocks_by_station,
            policy=self.policy, budget_usdc=planning_budget,
        )

        def _edges() -> list[dict[str, Any]]:
            plan = self.last_forward_plan.get(station_id)
            edge = None if plan is None else W.station_edge_from_plan(
                plan, station_id=station_id, minimum_clip_usdc=self.minimum_clip_usdc,
            )
            return [] if edge is None else [edge]

        quota, _allocation = W.portfolio_quota_from(
            stations=[station_id], inventory=inventory, edges_for=_edges,
            blocks_by_station=self.blocks_by_station, policy=self.policy,
        )
        return max(0.0, float(quota.get(station_id, planning_budget)))


# --------------------------------------------------------------------------
# The recorded books, per scope, for the venue's answers and the marks.
# --------------------------------------------------------------------------


class BookStore:
    def __init__(self, ssot: sqlite3.Connection) -> None:
        self.ssot = ssot
        self._cache: dict[tuple[str, str], list[tuple[str, dict[str, dict[str, Any]]]]] = {}
        self._instants: dict[tuple[str, str], list[datetime]] = {}

    def books(self, station_id: str, event_date: str) -> list[tuple[str, dict[str, dict[str, Any]]]]:
        key = (station_id, event_date)
        if key not in self._cache:
            rows = []
            for observed_at, buckets_json in self.ssot.execute(
                "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
                " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                " ORDER BY observed_at, rowid",
                key,
            ):
                by_bucket: dict[str, dict[str, Any]] = {}
                for entry in json.loads(buckets_json or "[]"):
                    if not isinstance(entry, Mapping):
                        continue
                    asks = sorted(
                        (
                            (float(level["price"]), float(level["shares"]))
                            for level in entry.get("ask_levels") or []
                            if float(level.get("shares") or 0.0) > 0.0
                        ),
                    )
                    by_bucket[str(entry.get("bucket") or "")] = {
                        "asks": asks,
                        "bid": float(entry.get("bid_price") or 0.0),
                        "ask": asks[0][0] if asks else 0.0,
                        "tick": float(entry.get("price_tick_size") or 0.001),
                        "observed_at": str(observed_at),
                    }
                rows.append((str(observed_at), by_bucket))
            self._cache[key] = rows
            self._instants[key] = [_instant(observed) for observed, _book in rows]
        return self._cache[key]

    def after(self, station_id: str, event_date: str, after: str) -> list[tuple[str, dict[str, dict[str, Any]]]]:
        """The books strictly after ``after``, in order."""

        rows = self.books(station_id, event_date)
        index = bisect.bisect_right(self._instants[(station_id, event_date)], _instant(after))
        return rows[index:]

    def latest(self, station_id: str, event_date: str, at: str) -> dict[str, dict[str, Any]] | None:
        rows = self.books(station_id, event_date)
        index = bisect.bisect_right(self._instants[(station_id, event_date)], _instant(at))
        return rows[index - 1][1] if index else None

    def mark(self, station_id: str, event_date: str, bucket: str, *, at: str) -> float:
        book = self.latest(station_id, event_date, at) or {}
        row = book.get(bucket)
        if not row:
            return 0.0
        bid, ask = float(row["bid"]), float(row["ask"])
        if bid > 0.0 and ask > 0.0:
            return (bid + ask) / 2.0
        return bid or ask


# --------------------------------------------------------------------------
# The venue.
# --------------------------------------------------------------------------


class PaperVenue:
    """Answers the orders: FOK against the decision book, makers over time.

    A fill-or-kill crosses the recorded book at the decision, all or nothing
    at its cap.  A resting maker order is answered by the calibrated curves
    (``scripts/paper_ledger_fill_curves.py``): at posting it draws, once and
    deterministically, the time the curve says a seller hits it; until then
    it stands, and production's own resting rule may cancel it or its GTD
    horizon end it first.  The curve is chosen by lead band and by whether
    the order's bucket settled -- the adverse selection -- which only this
    class can see.  ``fill_model="none"`` never fills a maker (a lower
    bound).
    """

    def __init__(
        self,
        *,
        books: "BookStore",
        settlements: Mapping[tuple[str, str], Mapping[str, Any]],
        fill_model: str = "empirical",
        curves: Mapping[str, Any] | None = None,
    ) -> None:
        if fill_model not in {"empirical", "none"}:
            raise ValueError(f"unknown fill model: {fill_model}")
        self.books = books
        self.settlements = settlements
        self.fill_model = fill_model
        if fill_model == "empirical" and curves is None:
            curves = json.loads(MAKER_FILL_CURVES.read_text())
        self.curves = dict((curves or {}).get("curves") or {})
        self.curves_sha256 = str((curves or {}).get("content_sha256") or "")

    def submit_fok(self, order: PaperOrder, *, book_buckets: Sequence[Mapping[str, Any]]) -> None:
        row = next((b for b in book_buckets if str(b.get("bucket")) == order.bucket), None)
        levels = sorted(
            (float(level["price"]), float(level["shares"]))
            for level in (row or {}).get("ask_levels") or []
            if float(level.get("price") or 0.0) <= order.limit_price + EPS
        )
        need, cost = order.size, 0.0
        for price, shares in levels:
            take = min(need, shares)
            cost += take * price
            need -= take
            if need <= EPS:
                break
        if need <= EPS:
            order.event(order.placed_at, "FILL", order.size, cost)
            order.state = "filled"
        else:
            order.event(order.placed_at, "SUBMIT_REFUSED")
            order.state = "refused"
        order.ended_at = order.placed_at

    def maker_curve_key(self, order: PaperOrder) -> str:
        band = F.lead_band(F.lead_hours(order.placed_at, order.event_date, order.settlement_timezone))
        label = self.settlements.get((order.station_id, order.event_date))
        if label is None:
            return f"{band}|any"
        return f"{band}|{'won' if str(label['settled_bucket']) == order.bucket else 'lost'}"

    def post_maker(self, order: PaperOrder) -> None:
        order.event(order.placed_at, "POST_ACK")
        order.last_book_at = order.placed_at
        if self.fill_model != "empirical":
            return
        curve = self.curves.get(self.maker_curve_key(order))
        if not curve:
            return
        seconds = F.fill_seconds_for_draw(
            curve,
            F.order_draw(order.order_id, order.station_id, order.event_date, order.bucket, "%.6f" % order.limit_price),
        )
        if seconds is not None:
            order.fill_at = _iso(_instant(order.placed_at) + timedelta(seconds=seconds))

    def _fill_due(self, order: PaperOrder, at: str) -> bool:
        return (
            order.fill_at is not None
            and _at_or_before(order.fill_at, at)
            and (order.expires_at is None or _before(order.fill_at, order.expires_at))
        )

    def _fill(self, order: PaperOrder) -> None:
        size = order.remaining
        order.event(str(order.fill_at), "FILL", size, size * order.limit_price)
        order.state, order.ended_at = "filled", str(order.fill_at)

    def advance(self, order: PaperOrder, *, until: str, dead_buckets: Sequence[str] = (), running_high: float | None = None) -> None:
        """Walk a resting maker order through the books up to ``until``."""

        if order.state != "resting" or order.candidate != "MAKER":
            return
        horizon = order.expires_at
        for observed_at, book in self.books.after(order.station_id, order.event_date, order.last_book_at or order.placed_at):
            if not _at_or_before(observed_at, until):
                break
            if self._fill_due(order, observed_at):
                self._fill(order)
                return
            if horizon is not None and not _before(observed_at, horizon):
                break
            order.last_book_at = observed_at
            row = book.get(order.bucket)
            verdict = resting_rung_keep_or_cancel(
                intent={
                    "bucket": order.bucket, "order_action": "BUY",
                    "limit_price": order.limit_price, "posted_at_utc": order.placed_at,
                },
                dead_buckets=(), running_high=None,
                load_book=lambda row=row: row,
                as_of_utc=observed_at,
                stale_book_seconds=RESTING_BOOK_STALE_SECONDS,
            )
            if verdict["action"] == "cancel":
                self._end(order, observed_at, "CANCEL_ACK", str(verdict.get("reason") or ""))
                return
        if self._fill_due(order, until):
            self._fill(order)
            return
        # The pass at ``until``: the order's own horizon, the day's
        # thermometer, then the book's age.
        if horizon is not None and not _before(until, horizon):
            self._end(order, horizon, "EXPIRED", "gtd_horizon")
            return
        latest = self.books.latest(order.station_id, order.event_date, until) or {}
        verdict = resting_rung_keep_or_cancel(
            intent={
                "bucket": order.bucket, "order_action": "BUY",
                "limit_price": order.limit_price, "posted_at_utc": order.placed_at,
            },
            dead_buckets=tuple(dead_buckets), running_high=running_high,
            load_book=lambda: latest.get(order.bucket),
            as_of_utc=until,
            stale_book_seconds=RESTING_BOOK_STALE_SECONDS,
        )
        if verdict["action"] == "cancel":
            self._end(order, until, "CANCEL_ACK", str(verdict.get("reason") or ""))

    @staticmethod
    def _end(order: PaperOrder, when: str, kind: str, reason: str) -> None:
        order.event(when, kind)
        order.state = "cancelled" if kind == "CANCEL_ACK" else "expired"
        order.ended_at = when
        order.end_reason = reason


# --------------------------------------------------------------------------
# The walk.
# --------------------------------------------------------------------------


def _normalized(value: Any) -> Any:
    value = json.loads(json.dumps(value, default=str))

    def strip(item: Any) -> Any:
        if isinstance(item, dict):
            return {k: strip(v) for k, v in item.items() if k not in COMMIT_STAMPED_KEYS}
        if isinstance(item, list):
            return [strip(v) for v in item]
        return item

    return strip(value)


def _first_difference(a: Any, b: Any, path: str = "") -> str | None:
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            found = _first_difference(a.get(key, "<absent>"), b.get(key, "<absent>"), f"{path}.{key}")
            if found:
                return found
        return None
    return None if a == b else path or "."


def _load_settlements(ssot: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for station, day, bucket, known_at, revision in ssot.execute(
        "SELECT station_id, event_date, settled_bucket, known_at_utc, truth_revision_no"
        " FROM basket_station_day_settlement_label l"
        " WHERE truth_revision_no=(SELECT MAX(truth_revision_no)"
        "  FROM basket_station_day_settlement_label x"
        "  WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"
    ):
        out[(str(station), str(day))] = {
            "settled_bucket": str(bucket), "known_at_utc": str(known_at), "revision": int(revision),
        }
    return out


def _production_decisions(
    ssot: sqlite3.Connection,
    *,
    since_rowid: int = 0,
    until_rowid: int | None = None,
    stations: Sequence[str] = (),
    from_event_date: str | None = None,
    to_event_date: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Production's basket decisions, whole station-days at a time.

    A station-day is walked from its first decision, whatever the range: the
    chain's history is the ledger's own, and a day entered half-way would
    decide its first row against an empty history production never had.
    The scopes are those with a decision after ``since_rowid`` (and up to
    ``until_rowid``), narrowed to the event dates asked for.

    Only the light columns are read up front; each decision's details and
    training fact are read when it is walked, so no statement stays open on
    the SSOT across the run (an open read pins the WAL the resident writes).
    """

    clauses = ["rowid>?", "substr(plan_id,1,5)='plan_'"]
    params: list[Any] = [since_rowid]
    if until_rowid is not None:
        clauses.append("rowid<=?")
        params.append(until_rowid)
    if stations:
        clauses.append("station_id IN (" + ",".join("?" * len(stations)) + ")")
        params.extend(stations)
    if from_event_date:
        clauses.append("event_date>=?")
        params.append(from_event_date)
    if to_event_date:
        clauses.append("event_date<=?")
        params.append(to_event_date)
    scopes = {
        (str(r[0]), str(r[1]))
        for r in ssot.execute(
            "SELECT DISTINCT station_id, event_date FROM auto_trade_decisions"
            " WHERE " + " AND ".join(clauses),
            params,
        ).fetchall()
    }
    if not scopes:
        return
    first_date = min(day for _s, day in scopes)
    last_date = max(day for _s, day in scopes)
    light = [
        row for row in ssot.execute(
            "SELECT rowid, decision_id, cycle_id, station_id, event_date, created_at,"
            " settlement_timezone FROM auto_trade_decisions"
            " WHERE substr(plan_id,1,5)='plan_' AND event_date>=? AND event_date<=?"
            + ("" if until_rowid is None else " AND rowid<=?") +
            " ORDER BY created_at, rowid",
            [first_date, last_date] + ([] if until_rowid is None else [until_rowid]),
        ).fetchall()
        if (str(row[3]), str(row[4])) in scopes
    ]
    for rowid, decision_id, cycle_id, station, day, created, tz in light:
        decided_state, reason_codes, details = ssot.execute(
            "SELECT decided_state, reason_codes_json, details_json"
            " FROM auto_trade_decisions WHERE rowid=?", (rowid,),
        ).fetchone()
        fact = ssot.execute(
            "SELECT stage, decision_at_utc, feature_json FROM basket_policy_training_fact"
            " WHERE decision_id=? AND cycle_id=?", (decision_id, cycle_id),
        ).fetchone()
        yield {
            "rowid": int(rowid), "decision_id": str(decision_id), "station_id": str(station),
            "event_date": str(day), "created_at": str(created), "decided_state": str(decided_state),
            "reason_codes": json.loads(reason_codes or "[]"), "details": json.loads(details or "{}"),
            "stage": None if fact is None else fact[0],
            "decision_at_utc": None if fact is None else fact[1],
            "feature": json.loads(fact[2]) if fact is not None and fact[2] else None,
            "in_range": int(rowid) > since_rowid,
            "settlement_timezone": str(tz or "UTC"),
        }


class AlignedLedger:
    def __init__(self, *, ssot: sqlite3.Connection, archive: sqlite3.Connection | None, capital: str, fill_model: str, opening_cash_usdc: float) -> None:
        self.ssot = ssot
        self.archive = archive
        self.books = BookStore(ssot)
        self.settlements = _load_settlements(ssot)
        self.account = PaperAccount(opening_cash_usdc, self.books, self.settlements) if capital == "paper" else None
        self.portfolio = PaperPortfolio(ssot) if capital == "paper" else None
        self.state = PaperChainState(ssot, capital=capital, paper_account=self.account)
        self.venue = PaperVenue(books=self.books, settlements=self.settlements, fill_model=fill_model)
        activation = load_activation(W.DEFAULT_TARGET_ACTIVATION)
        self.scope = dict(activation["target_scope"])
        self.authority_scope = str(activation["policy_authority_scope"])
        self.quote_freshness_sha = QuoteFreshnessPolicy.from_mapping(
            activation.get("quote_freshness_policy")
        ).content_sha256
        self.rows: list[dict[str, Any]] = []

    # -- one production decision -------------------------------------------
    def running_high(self, station_id: str, event_date: str, at: str) -> float | None:
        return W.observed_running_high_c(self.ssot, self.archive, station_id=station_id, event_date=event_date, as_of_utc=at)

    def advance(self, station_id: str, event_date: str, until: str, *, all_scopes: bool) -> None:
        scopes = list(self.state.days) if all_scopes else [(station_id, event_date)]
        for scope in scopes:
            chain = self.state.days.get(scope)
            if chain is None or not any(o.state == "resting" for o in chain.orders):
                continue
            dead: tuple[str, ...] = ()
            high: float | None = None
            if scope == (station_id, event_date):
                dead, high = W.resting_dead_buckets(
                    self.ssot, station_id=scope[0], event_date=scope[1],
                    running_high=self.running_high(scope[0], scope[1], until),
                )
            for order in chain.orders:
                self.venue.advance(order, until=until, dead_buckets=dead, running_high=high)

    def decide(self, row: Mapping[str, Any]) -> dict[str, Any]:
        station, day, created = row["station_id"], row["event_date"], row["created_at"]
        chain = self.state.day(station, day)
        self.advance(station, day, created, all_scopes=self.account is not None)
        self.state.before_utc = created
        out: dict[str, Any] = {
            "production_rowid": row["rowid"], "production_decision_id": row["decision_id"],
            "station_id": station, "event_date": day, "created_at": created,
            "stage": row["stage"], "entry": "", "status": "", "reason_codes": [],
            "intents": [], "matches_production": None, "first_difference": None,
            "in_range": bool(row.get("in_range", True)),
        }
        feature = row["feature"]
        if feature is None:
            # Production recorded a decision it never compiled to a plan --
            # a refusal of the store ("database is locked") or of the stage
            # gate before the compile.  It is on the chain's history (the
            # next decision's prior core reads it), so it is on the ledger's.
            chain.decisions.append((created, dict(row["details"])))
            out.update(entry="production_record", status=row["decided_state"], reason_codes=row["reason_codes"])
            return out
        recorded_plan = dict(feature.get("plan") or {})
        quote_version = str(recorded_plan.get("quote_version") or "")
        at = str(row["decision_at_utc"] or created)
        high = self.running_high(station, day, at)
        try:
            if quote_version.startswith(FORWARD_BOOK_PREFIX):
                out["entry"] = "forward"
                quote_row = self.ssot.execute(
                    "SELECT * FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
                    " AND quote_version=? AND state='COMMITTED' ORDER BY rowid LIMIT 1",
                    (station, day, quote_version),
                ).fetchone()
                if quote_row is None:
                    raise ContractError("paper_ledger_forward_book_missing")
                quote_row = dict(quote_row)
                book = forward_book_from_snapshot_row(quote_row)
                if recorded_plan.get("observed_at"):
                    book["quote_observed_at"] = str(recorded_plan["observed_at"])
                planning_budget = (
                    float(quote_row["remaining_budget_usdc"]) if self.portfolio is None
                    else self.portfolio.forward_budget_usdc(self.state, self.account, station_id=station, at=at)
                )
                out["planning_budget_usdc"] = planning_budget
                preselection = compile_p0_preselection(
                    self.ssot, station_id=station, event_date=day, evaluation_at_utc=at,
                    season_bin=self.scope["season_bin"], maturity_bin=self.scope["maturity_bin"],
                    liquidity_bin=self.scope["liquidity_bin"], strategy_version=self.scope["strategy_version"],
                    planning_budget_usdc=planning_budget,
                    observed_running_high_c=high,
                    base_policy=self.scope.get("base_policy") if isinstance(self.scope.get("base_policy"), Mapping) else None,
                    chain_state=self.state, forward_book=book,
                )
                compiled = preselection["compiled_plan"]
                if self.portfolio is not None:
                    # The allocator's edge for this station is its last plan.
                    self.portfolio.last_forward_plan[station] = compiled
                market = preselection[UNPERSISTED_MARKET_SNAPSHOT_KEY]
                quote_raw, inventory_raw, order_raw = W._forward_market_evidence_rows(
                    market, quote_freshness_policy_sha256=self.quote_freshness_sha,
                )
                quote_raw["inventory_sha256"] = build_confirmed_inventory_snapshot(inventory_raw)["content_sha256"]
                quote_raw["order_state_sha256"] = build_order_state_snapshot(order_raw)["content_sha256"]
                commit_snapshot = quote_raw
            else:
                out["entry"] = "in_day"
                compiled = compile_production_cycle_from_ssot(
                    self.ssot, station_id=station, event_date=day, evaluation_at_utc=at,
                    season_bin=self.scope["season_bin"], maturity_bin=self.scope["maturity_bin"],
                    liquidity_bin=self.scope["liquidity_bin"], strategy_version=self.scope["strategy_version"],
                    policy_authority_scope=self.authority_scope, observed_running_high_c=high,
                    chain_state=self.state,
                    pinned_versions={
                        key: str(recorded_plan.get(key) or "")
                        for key in ("quote_version", "inventory_version", "order_state_version")
                    },
                )
                commit_snapshot = compiled["production_market_snapshot"]
            # Which of production's three writers records this compile: a
            # blocked plan goes to the blocked-scope writer (details without a
            # core or a signature, no orders); an in-day P0/P0-M to the
            # stage-zero writer (the same, reason p1_final_authority_pending);
            # everything else through the candidate commit, whose prepare
            # step is production's own.
            probability = compiled.get("probability_envelope")
            compiled_stage = str(
                (probability.get("stage") if isinstance(probability, Mapping) else None)
                or row["stage"] or ""
            ).strip().upper()
            if str(compiled.get("status") or "") == "blocked":
                path = "blocked"
            elif out["entry"] == "in_day" and compiled_stage in {"P0", "P0-M"}:
                path = "stage_zero"
            else:
                path = "candidate"
            prepared = (
                prepare_production_candidate(
                    compiled_plan=compiled, market_snapshot=commit_snapshot, created_at_utc=created,
                )
                if path == "candidate" else None
            )
        except Exception as exc:  # noqa: BLE001 -- a refusal is a decision, named
            reason = f"{type(exc).__name__}:{exc}"[:200]
            chain.decisions.append((created, {"schema": "paper_ledger_compile_refusal_v1", "reason": reason}))
            out.update(status="blocked", reason_codes=[reason], matches_production=False, first_difference="compile_raised")
            return out
        plan = compiled.get("plan") or {}
        if prepared is not None:
            decision = prepared["decision"]
            details = dict(decision["details"])
            status, reasons = str(decision["decided_state"]), list(decision["reason_codes"])
            intents = [] if prepared["blocked"] else list(prepared["intents"])
            plan_payload = prepared["plan_payload"]
        elif path == "stage_zero":
            details = {
                "schema": "paper_ledger_stage_zero_v1",
                "stage_zero_execution_version": 1,
                "probability_stage": compiled_stage,
            }
            status, reasons, intents, plan_payload = "blocked", ["p1_final_authority_pending"], [], plan
        else:
            reasons = [str(code) for code in compiled.get("reason_codes") or ()] or [
                "compiled_plan_blocked"
            ]
            details = {
                "schema": "paper_ledger_blocked_scope_v1",
                "blocked_scope_decision_version": 1,
                "reason_codes": reasons,
            }
            status, intents, plan_payload = "blocked", [], plan
        chain.decisions.append((created, details))
        diff = _first_difference(_normalized(recorded_plan), _normalized(plan))
        out.update(
            status=status, reason_codes=reasons, commit_path=path,
            matches_production=diff is None, first_difference=diff,
            target_shares={
                str(t.get("bucket")): float(t.get("target_shares") or t.get("target_quantity") or 0.0)
                for t in plan.get("targets") or [] if isinstance(t, Mapping)
            },
        )
        # Charter 71(a) and the opener's tilt, by production's own rules: the
        # in-day loop records the opener after a commit, never after a
        # blocked compile.
        if out["entry"] == "in_day" and path != "blocked" and chain.tilt is None:
            tilt = opener_tilt_from_plan(compiled)
            if tilt is not None:
                chain.tilt = (created, float(tilt.get("factor") or 1.0))
        identity = chain_last_action_identity(plan=plan_payload, intents=intents)
        if identity:
            chain.last_action = (created, identity)
        for index, intent in enumerate(intents):
            body = dict(intent.get("intent_json") or {})
            expiration = body.get("expiration")
            order = PaperOrder(
                order_id=hashlib.sha256(f"{row['decision_id']}|{index}".encode()).hexdigest()[:24],
                station_id=station, event_date=day, bucket=str(intent["bucket"]),
                token_id=str(intent["token_id"]), candidate=str(body.get("execution_candidate") or ""),
                limit_price=float(intent["limit_price"]), size=float(intent["requested_size"]),
                placed_at=created,
                expires_at=(
                    _iso(datetime.fromtimestamp(int(expiration), timezone.utc))
                    if expiration not in (None, "") else None
                ),
                ladder_rung=bool(body.get("post_only")), decision_created_at=created,
                settlement_timezone=str(row.get("settlement_timezone") or "UTC"),
            )
            if order.candidate == "TAKER_FOK":
                self.venue.submit_fok(order, book_buckets=commit_snapshot.get("buckets") or [])
            else:
                self.venue.post_maker(order)
            chain.orders.append(order)
            out["intents"].append({
                "order_id": order.order_id, "bucket": order.bucket, "candidate": order.candidate,
                "limit_price": order.limit_price, "size": order.size, "expires_at": order.expires_at,
            })
        return out

    def settle(self, until: str) -> list[dict[str, Any]]:
        for (station, day), chain in self.state.days.items():
            for order in chain.orders:
                self.venue.advance(order, until=until)
        out = []
        for (station, day), chain in sorted(self.state.days.items()):
            legs: dict[str, list[float]] = {}
            for order in chain.orders:
                shares, cost = order.filled_by(None)
                if shares > EPS:
                    leg = legs.setdefault(order.bucket, [0.0, 0.0])
                    leg[0] += shares
                    leg[1] += cost
            if not legs:
                continue
            label = self.settlements.get((station, day))
            cost = sum(v[1] for v in legs.values())
            payout = None if label is None else float(legs.get(label["settled_bucket"], [0.0])[0])
            out.append({
                "station_id": station, "event_date": day,
                "settled_bucket": None if label is None else label["settled_bucket"],
                "cost_usdc": round(cost, 6), "payout_usdc": payout,
                "pnl_usdc": None if payout is None else round(payout - cost, 6),
                "legs": {b: {"shares": round(v[0], 4), "cost_usdc": round(v[1], 6)} for b, v in sorted(legs.items())},
            })
        return out


# --------------------------------------------------------------------------
# Storage and the command line.
# --------------------------------------------------------------------------

LEDGER_DDL = (
    "CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, schema TEXT, started_at TEXT,"
    " finished_at TEXT, code_head TEXT, params_json TEXT, summary_json TEXT)",
    "CREATE TABLE IF NOT EXISTS decisions(run_id TEXT, seq INTEGER, production_rowid INTEGER,"
    " production_decision_id TEXT, station_id TEXT, event_date TEXT, created_at TEXT, stage TEXT,"
    " entry TEXT, status TEXT, reason_codes_json TEXT, intents_json TEXT,"
    " matches_production INTEGER, first_difference TEXT, target_shares_json TEXT,"
    " commit_path TEXT, planning_budget_usdc REAL,"
    " PRIMARY KEY(run_id, seq))",
    "CREATE TABLE IF NOT EXISTS orders(run_id TEXT, order_id TEXT, station_id TEXT, event_date TEXT,"
    " bucket TEXT, candidate TEXT, limit_price REAL, size REAL, placed_at TEXT, expires_at TEXT,"
    " state TEXT, filled_size REAL, filled_cost_usdc REAL, ended_at TEXT, end_reason TEXT,"
    " events_json TEXT, PRIMARY KEY(run_id, order_id))",
    "CREATE TABLE IF NOT EXISTS settlements(run_id TEXT, station_id TEXT, event_date TEXT,"
    " settled_bucket TEXT, cost_usdc REAL, payout_usdc REAL, pnl_usdc REAL, legs_json TEXT,"
    " PRIMARY KEY(run_id, station_id, event_date))",
)


def _code_head() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def summarize(rows: Sequence[Mapping[str, Any]], settlements: Sequence[Mapping[str, Any]], orders: Sequence[PaperOrder]) -> dict[str, Any]:
    compiled_all = [r for r in rows if r["entry"] in {"forward", "in_day"}]
    compiled = [r for r in compiled_all if r.get("in_range", True)]
    matched = [r for r in compiled if r["matches_production"]]
    # Alignment is owed until the ledger's own chain can differ from
    # production's: per station-day, up to its first simulated fill.
    first_fill: dict[tuple[str, str], str] = {}
    for order in orders:
        for when, kind, _s, _n in order.events:
            if kind in {"FILL", "PARTIAL_FILL"}:
                key = (order.station_id, order.event_date)
                if key not in first_fill or _before(when, first_fill[key]):
                    first_fill[key] = when
    owed = [
        r for r in compiled
        if (r["station_id"], r["event_date"]) not in first_fill
        or not _before(first_fill[(r["station_id"], r["event_date"])], r["created_at"])
    ]
    settled = [s for s in settlements if s["pnl_usdc"] is not None]
    cost = sum(s["cost_usdc"] for s in settled)
    pnl = sum(s["pnl_usdc"] for s in settled)
    return {
        "production_decisions_walked": len(rows),
        "production_decisions_in_range": sum(1 for r in rows if r.get("in_range", True)),
        "compiled_in_range": len(compiled),
        "compiled_before_range": len(compiled_all) - len(compiled),
        "matches_before_range": sum(1 for r in compiled_all if not r.get("in_range", True) and r["matches_production"]),
        "production_records_carried": sum(1 for r in rows if r["entry"] == "production_record"),
        "matches_production": len(matched),
        "aligned_before_first_fill": len(owed),
        "aligned_before_first_fill_matching": sum(1 for r in owed if r["matches_production"]),
        "mismatch_first_difference": _count(r["first_difference"] for r in compiled if not r["matches_production"]),
        "orders": len(orders),
        "orders_by_state": _count(o.state for o in orders),
        "station_days_with_fills": len(settlements),
        "settled_station_days": len(settled),
        "settled_cost_usdc": round(cost, 4),
        "settled_pnl_usdc": round(pnl, 4),
        "settled_return_on_cost": None if cost <= 0 else round(pnl / cost, 6),
    }


def _count(values: Iterator[Any] | Sequence[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return dict(sorted(out.items(), key=lambda item: -item[1]))


def run(args: argparse.Namespace) -> dict[str, Any]:
    ssot = sqlite3.connect(f"file:{args.ssot}?mode=ro", uri=True, timeout=60)
    ssot.row_factory = sqlite3.Row
    archive = sqlite3.connect(f"file:{args.archive}?mode=ro", uri=True, timeout=60) if Path(args.archive).exists() else None
    ledger = AlignedLedger(
        ssot=ssot, archive=archive, capital=args.capital, fill_model=args.fill_model,
        opening_cash_usdc=float(args.opening_cash_usdc),
    )
    started = time.time()
    last = None
    for index, row in enumerate(_production_decisions(
        ssot, since_rowid=args.since_rowid, until_rowid=args.until_rowid,
        stations=[s.strip().upper() for s in (args.stations or "").split(",") if s.strip()],
        from_event_date=args.from_event_date, to_event_date=args.to_event_date,
    )):
        if args.limit and index >= args.limit:
            break
        ledger.rows.append(ledger.decide(row))
        last = row["created_at"]
        if args.progress and index and index % args.progress == 0:
            print(f"[{time.time() - started:7.1f}s] {index} decisions, last {last}", file=sys.stderr, flush=True)
    settlements = ledger.settle(last or datetime.now(timezone.utc).isoformat())
    orders = [o for chain in ledger.state.days.values() for o in chain.orders]
    summary = summarize(ledger.rows, settlements, orders)
    summary["seconds"] = round(time.time() - started, 1)
    summary["fill_model"] = args.fill_model
    summary["maker_fill_curves_sha256"] = ledger.venue.curves_sha256
    if not args.dry_run:
        _store(args, ledger, settlements, orders, summary)
    return summary


def _store(args: argparse.Namespace, ledger: AlignedLedger, settlements: Sequence[Mapping[str, Any]], orders: Sequence[PaperOrder], summary: Mapping[str, Any]) -> None:
    out = sqlite3.connect(args.ledger_db)
    for ddl in LEDGER_DDL:
        out.execute(ddl)
    present = {str(row[1]) for row in out.execute("PRAGMA table_info(decisions)")}
    for column, kind in (("commit_path", "TEXT"), ("planning_budget_usdc", "REAL")):
        if column not in present:
            out.execute(f"ALTER TABLE decisions ADD COLUMN {column} {kind}")
    run_id = "run_" + hashlib.sha256(f"{time.time()}|{vars(args)}".encode()).hexdigest()[:16]
    params = {k: v for k, v in vars(args).items() if k not in {"ssot", "archive", "ledger_db"}}
    with out:
        out.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
            (run_id, SCHEMA, None, datetime.now(timezone.utc).isoformat(), _code_head(),
             json.dumps(params, sort_keys=True), json.dumps(summary, sort_keys=True)),
        )
        out.executemany(
            "INSERT INTO decisions(run_id,seq,production_rowid,production_decision_id,"
            "station_id,event_date,created_at,stage,entry,status,reason_codes_json,"
            "intents_json,matches_production,first_difference,target_shares_json,"
            "commit_path,planning_budget_usdc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (run_id, seq, r["production_rowid"], r["production_decision_id"], r["station_id"],
                 r["event_date"], r["created_at"], r["stage"], r["entry"], r["status"],
                 json.dumps(r["reason_codes"]), json.dumps(r["intents"]),
                 None if r["matches_production"] is None else int(bool(r["matches_production"])),
                 r["first_difference"], json.dumps(r.get("target_shares") or {}),
                 r.get("commit_path"), r.get("planning_budget_usdc"))
                for seq, r in enumerate(ledger.rows)
            ],
        )
        out.executemany(
            "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (run_id, o.order_id, o.station_id, o.event_date, o.bucket, o.candidate, o.limit_price,
                 o.size, o.placed_at, o.expires_at, o.state, *o.filled_by(None), o.ended_at,
                 o.end_reason, json.dumps(o.events))
                for o in orders
            ],
        )
        out.executemany(
            "INSERT INTO settlements VALUES(?,?,?,?,?,?,?,?)",
            [
                (run_id, s["station_id"], s["event_date"], s["settled_bucket"], s["cost_usdc"],
                 s["payout_usdc"], s["pnl_usdc"], json.dumps(s["legs"]))
                for s in settlements
            ],
        )
    summary_run = dict(summary)
    summary_run["run_id"] = run_id
    print(json.dumps({"run_id": run_id}), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--since-rowid", type=int, default=0,
                        help="first production decision rowid (exclusive)")
    parser.add_argument("--until-rowid", type=int, default=None)
    parser.add_argument("--stations", default="", help="comma-separated ICAO filter")
    parser.add_argument("--from-event-date", default=None, help="first event date (inclusive)")
    parser.add_argument("--to-event-date", default=None, help="last event date (inclusive)")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--capital", choices=("production", "paper"), default="production")
    parser.add_argument("--opening-cash-usdc", type=float, default=120.0)
    parser.add_argument("--fill-model", choices=("empirical", "none"), default="empirical",
                        help="maker venue: calibrated fill curves, or never (a lower bound)")
    parser.add_argument("--ssot", default=str(SSOT_DB))
    parser.add_argument("--archive", default=str(ARCHIVE_DB))
    parser.add_argument("--ledger-db", default=str(LEDGER_DB))
    parser.add_argument("--progress", type=int, default=200)
    parser.add_argument("--label", default="", help="a name readers select runs by (the daily pass: daily_sweep)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(run(args), indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
