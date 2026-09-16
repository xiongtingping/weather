"""The live book: what the exchange filled, settled, in the paper book's shape.

User ruling 2026-09-16: the paper ledger is a rule comparator from today --
it answers "would another rule do better or worse" and nothing else.  Every
reading that says whether the account, a station or a day made money reads
the live fills instead.  Before this module those readers were paper-only
(the money board, the daily table, the station-day roster, the go-live gates,
the automatic bar), and the one live P&L the board had was computed inside
the dashboard where nothing else could reach it.

One loader, three products:

``legs``
    every live intent (``live_submit_authorized=1``) with its station-day,
    the stage of the decision that minted it, what the exchange filled, and
    the probability the decision was bound to for that bucket -- read from
    the exact envelope named by the decision's forward evidence binding,
    never re-derived.
``settled``
    the settled bucket per station-day, from the settler's own row where the
    paper book seated the day and otherwise from the truth lane through the
    settler's own two functions.  Moved here from the dashboard unchanged.
``settlements``
    one row per (station, event day, stage) that filled anything and has
    settled, carrying the paper settlement table's columns -- stake, payout,
    pnl, hit, intended_hit, claimed_win_probability, basket_all_in_cost -- so
    the board's portfolio arithmetic runs on either book without a second
    copy of itself.

Uneven baskets.  A paper seat buys one share count across its window, so its
all-in cost is stake / shares and a hit pays that count.  Live legs fill
unevenly.  The columns keep the identity the portfolio arithmetic rests on --
``stake x (claim / cost - 1)`` is the claimed EV -- by taking the share unit
as the probability-weighted share count ``S = sum(p_b s_b) / sum(p_b)`` and
``cost = stake / S``.  Then ``S x (claim - cost) = sum(p_b s_b) - stake``,
which is exactly what the held legs were claimed to return, and a basket with
one share count reduces to the paper definition.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any

from paper_trading_ledger import (
    # The settler's own three, never a copy of them.  Deriving which rung a
    # value landed on means knowing the per-station grid (a degree Celsius
    # at ZBAA, two Fahrenheit at KMIA), and an audit that restates a rule
    # invents its own findings rather than reporting production's.
    _settled_bucket as _ledger_settled_bucket,
    _settlement_ladder as _ledger_settlement_ladder,
    _truth as _ledger_truth,
)

STAGES = ("P0", "P0-M", "P1")

# One build per connection per database state.  The board reads the live
# book from four sections in one render; the loader parses every live
# decision's binding and seeks one envelope each, which is cheap once and
# pointless four times.  ``PRAGMA data_version`` moves whenever another
# connection commits and ``total_changes`` whenever this one does, so a
# long-lived connection never serves a stale book.  The connection itself is
# held in the entry and compared by identity: keyed on ``id()`` alone, a
# closed connection's id is reused by the next one and the next one is
# handed the last one's book.
_BUILD_CACHE: dict[tuple[int, int, int], dict[str, Any]] = {}


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def settled_buckets(
    conn: sqlite3.Connection, keys: set[tuple[str, str]]
) -> dict[tuple[str, str], tuple[str, str]]:
    """Which of these station-days have settled, on which bucket, said by whom.

    Two sources, in this order.  The paper ledger's settlement rows come
    first: that label was written by the production settler, and a day it
    carries needs nothing derived.  It is a truth label, not money, which is
    why reading it survives the 2026-09-16 ruling.  Every key it does not
    carry is resolved here from the truth lane and the station-day's own
    committed ladder, through the settler's own two functions -- ``_truth``
    for the revision that stands (charter 44) and ``_settled_bucket`` for the
    rung a value falls in.  The per-station grid is never restated: the
    rungs come back out of ``basket_probability_bridge_*`` by the ledger's
    own reader.

    The paper book is a different book.  It seats the station-days its own
    rule admits, and the live lane mints intents for days it never seated:
    2026-09-08 carried eleven live station-days against three paper
    settlements, while the truth lane held an ACCEPTED value for
    forty-eight stations that day.  Reading the paper table alone, the
    panel called the other eight 未结算 four days after the fact.  The same
    gap opens on every rule change -- the ledger re-derives its whole
    history, both paper tables sit empty for the better part of an hour,
    and a live basket that had a verdict yesterday loses it.  Truth does
    not move when a seating rule does, so the fallback does not either.

    Verified 2026-09-12 against the settler's own output: over the 3,368
    candidate windows it has scored, this reproduces ``settled_bucket``
    exactly, with zero disagreements.  And the ladder is a property of the
    station-day rather than of the seat -- across every COMMITTED bridge
    row in the archive, no station-day carries two distinct ladders -- so
    the newest committed decision's rungs are the day's own grid.

    The source travels with the label because the two are different
    claims: one says the paper book settled this seat, the other says the
    truth lane settled this day and no paper seat exists.
    """

    out: dict[tuple[str, str], tuple[str, str]] = {}
    if not keys:
        return out
    if table_exists(conn, "paper_trade_settlement"):
        try:
            for station, day, bucket in conn.execute(
                "SELECT station_id, event_date, MAX(settled_bucket)"
                " FROM paper_trade_settlement WHERE settled_bucket IS NOT NULL"
                " GROUP BY station_id, event_date"
            ):
                key = (str(station), str(day))
                if key in keys:
                    out[key] = (str(bucket), "paper_ledger")
        except sqlite3.Error:
            out = {}
    missing = sorted(keys - set(out))
    if not missing:
        return out
    for name in ("calibration_station_daily_high_truth",
                 "basket_probability_bridge_history",
                 "basket_probability_bridge_bucket_probability"):
        if not table_exists(conn, name):
            return out
    try:
        truth = _ledger_truth(conn)
    except sqlite3.Error:
        return out
    for key in missing:
        standing = truth.get(key)
        if standing is None:
            continue
        # One index seek per station-day, not a scan: the leading columns
        # of ``basket_probability_bridge_history_asof_idx`` are exactly
        # (station_id, event_date, state, decision_at_utc), so the newest
        # committed decision is the last entry of a seeked range.
        try:
            head = conn.execute(
                "SELECT stage, decision_at_utc"
                " FROM basket_probability_bridge_history"
                " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                " ORDER BY decision_at_utc DESC LIMIT 1",
                (key[0], key[1]),
            ).fetchone()
            if not head:
                continue
            ladder = _ledger_settlement_ladder(
                conn, key[0], key[1], str(head[0]), str(head[1])
            )
        except sqlite3.Error:
            continue
        if not ladder:
            continue
        landed = _ledger_settled_bucket(list(ladder), standing.tmax_c)
        # A truth outside every rung the day was priced against is not a
        # settlement this book can score, and saying so by leaving the day
        # open beats naming a bucket that was never on the ladder.
        if landed is None:
            continue
        out[key] = (str(landed[0]), "truth_lane")
    return out


def _decision_meta(
    conn: sqlite3.Connection, decisions: dict[str, tuple[str, str, str]]
) -> dict[str, dict[str, Any]]:
    """Stage and bound probabilities per decision, read back, never derived.

    ``decisions`` maps decision id to (station, event day, details_json).
    The stage is the decision signature's; the probabilities are the exact
    envelope the forward evidence binding names by content hash.  The seek
    is a prefix range on the as-of index rather than an equality on the
    timestamp, because the binding and the envelope do not always spell the
    same instant the same way (``.697Z`` against ``.697000Z``), and the hash
    then picks the one row that is the artifact.  Measured 2026-09-16: all
    710 live decisions found their envelope this way, in under two seconds;
    an equality seek found 16 of 100, and a lookup on the hash alone is a
    scan of a 38 GB table per decision.
    """

    out: dict[str, dict[str, Any]] = {}
    has_envelope = table_exists(conn, "basket_probability_envelope")
    for decision_id, (station, day, details_json) in decisions.items():
        try:
            details = json.loads(details_json or "{}") or {}
        except (TypeError, ValueError):
            details = {}
        signature = details.get("decision_signature") or {}
        binding = details.get("forward_evidence_binding") or {}
        stage = str(
            signature.get("stage") or binding.get("probability_stage") or ""
        )
        meta: dict[str, Any] = {
            "stage": stage if stage in STAGES else "?",
            "decision_at_utc": str(binding.get("decision_at_utc") or ""),
            "probabilities": None,
        }
        at = str(binding.get("probability_decision_at_utc") or "")
        sha = str(binding.get("probability_artifact_sha256") or "")
        probability_stage = str(binding.get("probability_stage") or stage)
        if has_envelope and len(at) >= 19 and sha:
            try:
                for content, probabilities in conn.execute(
                    "SELECT content_sha256, probabilities_json"
                    " FROM basket_probability_envelope"
                    " WHERE station_id=? AND event_date=?"
                    "   AND state='COMMITTED'"
                    "   AND decision_at_utc>=? AND decision_at_utc<=?"
                    "   AND stage=?",
                    (station, day, at[:19], at[:19] + "z", probability_stage),
                ):
                    if str(content) != sha:
                        continue
                    meta["probabilities"] = {
                        str(bucket): float(value)
                        for bucket, value in (
                            json.loads(probabilities or "{}") or {}
                        ).items()
                    }
                    break
            except (sqlite3.Error, TypeError, ValueError):
                pass
        out[decision_id] = meta
    return out


def load_legs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every live intent with its station-day, stage, fill and bound claim.

    An intent whose decision row is missing has no station-day to count its
    money under and is left out here; the order listing still draws it.
    """

    needed = ("basket_target_order_intents", "auto_trade_decisions")
    if not all(table_exists(conn, name) for name in needed):
        return []
    has_reconciliations = table_exists(
        conn, "basket_target_order_reconciliations"
    )
    fill_columns = (
        " COALESCE(r.filled_size,0), COALESCE(r.filled_notional_usdc,0)"
        if has_reconciliations else " 0, 0"
    )
    fill_join = (
        " LEFT JOIN basket_target_order_reconciliations r"
        "   ON r.order_intent_id=i.order_intent_id"
        if has_reconciliations else ""
    )
    rows = conn.execute(
        "SELECT i.order_intent_id, i.decision_id, d.station_id, d.event_date,"
        " d.details_json, i.bucket, i.limit_price, i.requested_size,"
        " i.requested_notional_usdc, i.state, i.created_at," + fill_columns +
        " FROM basket_target_order_intents i"
        " JOIN auto_trade_decisions d ON d.decision_id=i.decision_id"
        + fill_join +
        " WHERE i.live_submit_authorized=1"
        " ORDER BY i.created_at, i.order_intent_id"
    ).fetchall()
    decisions = {
        str(row[1]): (str(row[2]), str(row[3]), row[4]) for row in rows
    }
    meta = _decision_meta(conn, decisions)
    legs: list[dict[str, Any]] = []
    for (intent, decision, station, day, _details, bucket, price, size,
         notional, state, created, filled_size, filled_notional) in rows:
        info = meta.get(str(decision)) or {}
        probabilities = info.get("probabilities")
        legs.append({
            "order_intent_id": str(intent),
            "decision_id": str(decision),
            "station_id": str(station),
            "event_date": str(day),
            "stage": info.get("stage") or "?",
            "decision_at_utc": info.get("decision_at_utc") or "",
            "bucket": str(bucket),
            "limit_price": float(price or 0.0),
            "requested_size": float(size or 0.0),
            "requested_notional_usdc": float(notional or 0.0),
            "state": str(state or ""),
            "created_at": str(created or ""),
            "filled_size": float(filled_size or 0.0),
            "filled_notional_usdc": float(filled_notional or 0.0),
            "claimed_probability": (
                None if probabilities is None
                else float(probabilities.get(str(bucket), 0.0))
            ),
        })
    return legs


def settlement_rows(
    legs: list[dict[str, Any]],
    settled: dict[tuple[str, str], tuple[str, str]],
) -> list[dict[str, Any]]:
    """One row per settled (station, day, stage) that filled anything.

    Shaped like ``paper_trade_settlement`` so the portfolio arithmetic reads
    either book.  The basket a stage *held* is its filled legs; the basket it
    *intended* is every leg its decisions posted, filled or not -- the same
    pair the paper book scores as ``hit`` and ``intended_hit``.
    """

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for leg in legs:
        groups[(leg["station_id"], leg["event_date"], leg["stage"])].append(leg)
    out: list[dict[str, Any]] = []
    for (station, day, stage), group in sorted(groups.items()):
        landed = settled.get((station, day))
        filled = [leg for leg in group if leg["filled_size"] > 0]
        if landed is None or not filled:
            continue
        truth, source = landed
        shares: dict[str, float] = defaultdict(float)
        cash: dict[str, float] = defaultdict(float)
        # A bucket bought by two decisions of one stage at different claims
        # is held at the share-weighted claim of the two.
        claim_mass: dict[str, float] = defaultdict(float)
        claim_known = True
        for leg in filled:
            bucket = leg["bucket"]
            shares[bucket] += leg["filled_size"]
            cash[bucket] += leg["filled_notional_usdc"]
            if leg["claimed_probability"] is None:
                claim_known = False
            else:
                claim_mass[bucket] += (
                    leg["claimed_probability"] * leg["filled_size"]
                )
        stake = sum(cash.values())
        payout = shares.get(truth, 0.0)
        intended = sorted({leg["bucket"] for leg in group})
        claim = None
        cost = None
        claimed_ev = None
        if claim_known:
            per_bucket = {
                bucket: claim_mass[bucket] / shares[bucket]
                for bucket in shares if shares[bucket] > 0
            }
            claim = sum(per_bucket.values())
            weighted = sum(per_bucket[b] * shares[b] for b in per_bucket)
            claimed_ev = weighted - stake
            if claim > 0 and weighted > 0:
                cost = stake / (weighted / claim)
        out.append({
            "station_id": station,
            "event_date": day,
            "stage": stage,
            "settled_bucket": truth,
            "settled_source": source,
            "hit": 1 if truth in shares else 0,
            "intended_hit": 1 if truth in intended else 0,
            "stake_usdc": stake,
            "payout_usdc": payout,
            "pnl_usdc": payout - stake,
            "claimed_win_probability": claim,
            "basket_all_in_cost": cost,
            "claimed_ev_usdc": claimed_ev,
            "filled_legs": len(filled),
            "held_buckets": sorted(shares),
            "intended_buckets": intended,
        })
    return out


def build(conn: sqlite3.Connection) -> dict[str, Any]:
    """Legs, settled buckets and settlement rows, once per database state."""

    try:
        version = int(conn.execute("PRAGMA data_version").fetchone()[0])
    except sqlite3.Error:
        version = -1
    key = (id(conn), version, conn.total_changes)
    cached = _BUILD_CACHE.get(key)
    if cached is not None and cached["connection"] is conn:
        return cached["book"]
    try:
        legs = load_legs(conn)
    except sqlite3.Error:
        legs = []
    settled = settled_buckets(
        conn, {(leg["station_id"], leg["event_date"]) for leg in legs}
    )
    book = {
        "legs": legs,
        "settled": settled,
        "settlements": settlement_rows(legs, settled),
    }
    _BUILD_CACHE.clear()
    _BUILD_CACHE[key] = {"connection": conn, "book": book}
    return book
