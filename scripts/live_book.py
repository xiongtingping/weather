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

Mixed decisions.  One stage can hold buckets bought by different decisions,
each bound to its own envelope.  Each ``p_b`` above used to be the claim of
the decision that bought ``b``, so one basket's claim added up masses from
different envelopes: KLAX's 2026-09-15 P0 basket came to 1.35 and EFHK's
2026-09-10 to 1.11, while ZGGG's 2026-09-09 P0-M -- three completion legs,
each cheap on its own bucket -- came to 0.72 under three envelopes that each
put 0.98 on the three together.  Every reading of 命中/声称 divided by those.
The held basket as a whole was judged by one decision only: the last one to
fill, which planned against the inventory the earlier ones left (charters
84, 136).  So the claim, the claimed EV and the cost all read that
decision's envelope (``basket_belief``); a stage with one decision reads
exactly as before.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
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
    conn: sqlite3.Connection, keys: set[tuple[str, str]],
    truth: dict[tuple[str, str], Any] | None = None,
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
    if truth is None:
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
    # The centre the decision stood on, from the same envelope row: the
    # lineage's ``model_center_c``, which is what the paper book records as
    # ``center_c`` for its seats.  Read in the one seek that already finds
    # the row, and only when the column exists -- a store without it still
    # has claims to read.
    centre_column = "NULL"
    if has_envelope and any(
        row[1] == "lineage_json"
        for row in conn.execute("PRAGMA table_info(basket_probability_envelope)")
    ):
        centre_column = "json_extract(lineage_json, '$.model_center_c')"
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
            "center_c": None,
        }
        at = str(binding.get("probability_decision_at_utc") or "")
        sha = str(binding.get("probability_artifact_sha256") or "")
        probability_stage = str(binding.get("probability_stage") or stage)
        if has_envelope and len(at) >= 19 and sha:
            try:
                for content, probabilities, centre in conn.execute(
                    "SELECT content_sha256, probabilities_json, "
                    + centre_column +
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
                    if centre is not None:
                        meta["center_c"] = float(centre)
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
            # The decision's whole envelope, one dict shared by its legs:
            # a basket's claim needs its newest decision's mass on buckets
            # earlier decisions bought (``basket_belief``).
            "decision_probabilities": probabilities,
            "center_c": info.get("center_c"),
        })
    return legs


def basket_belief(
    legs: Iterable[Mapping[str, Any]], *, strict: bool = True
) -> Mapping[str, float] | None:
    """The one envelope a basket made of ``legs`` is claimed on.

    The newest decision behind the legs: it planned against what the earlier
    ones had already bought, so it is the only one that judged the basket as
    a whole.  Adding each bucket at the claim of the decision that bought it
    read several envelopes as one and could pass 1.

    A leg carries its decision's envelope as ``decision_probabilities``; one
    built without that key falls back to what its decision's own legs
    claimed, the buckets it posted and nothing else.  When the newest
    decision's envelope is unknown the answer is ``None`` if ``strict``, and
    otherwise the newest decision whose envelope is known.
    """

    order: dict[str, tuple[str, str, str]] = {}
    envelopes: dict[str, Mapping[str, float] | None] = {}
    posted: dict[str, dict[str, float]] = {}
    for leg in legs:
        decision = str(leg["decision_id"])
        created = str(leg.get("created_at") or "")
        at = (str(leg.get("decision_at_utc") or created), created, decision)
        order[decision] = max(order.get(decision, at), at)
        if "decision_probabilities" in leg:
            envelopes[decision] = leg["decision_probabilities"]
        elif leg.get("claimed_probability") is None:
            envelopes[decision] = None
        else:
            posted.setdefault(decision, {})[str(leg["bucket"])] = float(
                leg["claimed_probability"]
            )
    for decision, claims in posted.items():
        envelopes.setdefault(decision, claims)
    for decision in sorted(order, key=order.get, reverse=True):
        envelope = envelopes.get(decision)
        if envelope is not None or strict:
            return envelope
    return None


def stage_summary(group: list[dict[str, Any]]) -> dict[str, Any]:
    """What one stage of one station-day posted, held, paid and claimed.

    ``group`` is every live leg of that stage, filled or not.  Settled or
    not: the settlement rows add the truth to this, and the station-day
    roster reads it for days still open.
    """

    filled = [leg for leg in group if leg["filled_size"] > 0]
    shares: dict[str, float] = defaultdict(float)
    cash: dict[str, float] = defaultdict(float)
    claim_known = True
    for leg in filled:
        bucket = leg["bucket"]
        shares[bucket] += leg["filled_size"]
        cash[bucket] += leg["filled_notional_usdc"]
        if leg["claimed_probability"] is None:
            claim_known = False
    stake = sum(cash.values())
    # The centre the held basket stood on: each filled leg's decision centre,
    # weighted by the shares it bought.  Legs whose envelope carried no
    # centre are left out rather than read as zero.
    centred = [
        leg for leg in filled if leg.get("center_c") is not None
    ]
    centre_shares = sum(leg["filled_size"] for leg in centred)
    center_c = (
        sum(leg["center_c"] * leg["filled_size"] for leg in centred)
        / centre_shares
        if centre_shares > 0 else None
    )
    claim = None
    cost = None
    claimed_ev = None
    held_claims: dict[str, float] = {}
    # One envelope for the held basket, not each bucket at the claim of the
    # decision that bought it (see "Mixed decisions" above).
    belief = basket_belief(filled) if filled and claim_known else None
    if belief is not None:
        held_claims = {
            bucket: float(belief.get(bucket, 0.0))
            for bucket in shares if shares[bucket] > 0
        }
        claim = sum(held_claims.values())
        weighted = sum(held_claims[b] * shares[b] for b in held_claims)
        claimed_ev = weighted - stake
        if claim > 0 and weighted > 0:
            cost = stake / (weighted / claim)
    posted: dict[str, dict[str, Any]] = {}
    for leg in group:
        slot = posted.setdefault(leg["bucket"], {
            "bucket": leg["bucket"], "posted_size": 0.0, "legs": 0,
            "filled_size": 0.0, "filled_notional_usdc": 0.0,
            "claimed_probability": leg["claimed_probability"],
        })
        slot["posted_size"] += leg["requested_size"]
        slot["legs"] += 1
        slot["filled_size"] += leg["filled_size"]
        slot["filled_notional_usdc"] += leg["filled_notional_usdc"]
        # The newest decision's claim for a bucket nothing filled; the held
        # claim for one that did.
        if leg["claimed_probability"] is not None:
            slot["claimed_probability"] = leg["claimed_probability"]
    for bucket, slot in posted.items():
        if shares.get(bucket) and bucket in held_claims:
            slot["claimed_probability"] = held_claims[bucket]
    return {
        "baskets": len({leg["decision_id"] for leg in group}),
        "posted_legs": len(group),
        "filled_legs": len(filled),
        "stake_usdc": stake,
        "claimed_win_probability": claim,
        "basket_all_in_cost": cost,
        "claimed_ev_usdc": claimed_ev,
        "center_c": center_c,
        "held_shares": dict(shares),
        "held_buckets": sorted(shares),
        "intended_buckets": sorted(posted),
        "buckets": list(posted.values()),
    }


def station_days(book: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One record per station-day with a live leg, and its two basket verdicts.

    A *station-day* is one station on one event day, whatever stages it
    traded.  ``basket_hit`` is truth in any bucket production posted that
    day, filled or not; ``held_hit`` is truth in a bucket that filled, and
    exists only on a day something did (user ruling, 2026-09-14).  Both are
    ``None`` until the truth lands.

    Decided here once: 每日统计 and 逐站逐日明细 count these records by day,
    the go-live table's hit columns count them by station, and a day's
    verdict cannot come out one way in one table and another in the next.
    """

    settled = book.get("settled") or {}
    by_station_day: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for leg in book.get("legs") or []:
        by_station_day[(leg["station_id"], leg["event_date"])].append(leg)
    out: list[dict[str, Any]] = []
    for (station, day), day_legs in sorted(by_station_day.items()):
        landed = settled.get((station, day))
        truth = landed[0] if landed else None
        stages: dict[str, Any] = {}
        stake = 0.0
        payout = 0.0
        held: set[str] = set()
        posted: set[str] = set()
        # A leg whose decision named no stage still spent money on this
        # station-day; it is counted under its own key, never dropped.
        extra = sorted({leg["stage"] for leg in day_legs} - set(STAGES))
        for stage in list(STAGES) + extra:
            group = [leg for leg in day_legs if leg["stage"] == stage]
            if not group:
                continue
            summary = stage_summary(group)
            stages[stage] = summary
            stake += summary["stake_usdc"]
            held |= set(summary["held_buckets"])
            posted |= set(summary["intended_buckets"])
            if truth is not None:
                payout += summary["held_shares"].get(truth, 0.0)
        filled = sum(1 for leg in day_legs if leg["filled_size"] > 0)
        out.append({
            "station_id": station,
            "event_date": day,
            "settled_bucket": truth,
            "settled_source": landed[1] if landed else None,
            "stages": stages,
            "posted_legs": len(day_legs),
            "filled_legs": filled,
            "staked_usdc": stake,
            "payout_usdc": payout if truth is not None else None,
            "pnl_usdc": (payout - stake) if truth is not None and filled else None,
            "held_hit": (truth in held) if truth is not None and filled else None,
            "basket_hit": (truth in posted) if truth is not None else None,
        })
    return out


def settlement_rows(
    legs: list[dict[str, Any]],
    settled: dict[tuple[str, str], tuple[str, str]],
    truth_c: dict[tuple[str, str], float] | None = None,
) -> list[dict[str, Any]]:
    """One row per settled (station, day, stage) that filled anything.

    Shaped like ``paper_trade_settlement`` so the portfolio arithmetic reads
    either book.  The basket a stage *held* is its filled legs; the basket it
    *intended* is every leg its decisions posted, filled or not -- the same
    pair the paper book scores as ``hit`` and ``intended_hit``.

    ``truth_c`` is the standing settled value per station-day; with it the
    row carries ``settled_tmax_c`` and ``center_error_c`` (held centre minus
    truth), the paper settlement's own two columns.
    """

    out: list[dict[str, Any]] = []
    for (station, day, stage), group in sorted(group_legs(legs).items()):
        landed = settled.get((station, day))
        if landed is None:
            continue
        summary = stage_summary(group)
        if not summary["filled_legs"]:
            continue
        truth, source = landed
        stake = summary["stake_usdc"]
        payout = summary["held_shares"].get(truth, 0.0)
        tmax_c = (truth_c or {}).get((station, day))
        centre = summary["center_c"]
        out.append({
            "station_id": station,
            "event_date": day,
            "stage": stage,
            "settled_bucket": truth,
            "settled_source": source,
            "hit": 1 if truth in summary["held_shares"] else 0,
            "intended_hit": 1 if truth in summary["intended_buckets"] else 0,
            "stake_usdc": stake,
            "payout_usdc": payout,
            "pnl_usdc": payout - stake,
            "claimed_win_probability": summary["claimed_win_probability"],
            "basket_all_in_cost": summary["basket_all_in_cost"],
            "claimed_ev_usdc": summary["claimed_ev_usdc"],
            "center_c": centre,
            "settled_tmax_c": tmax_c,
            "center_error_c": (
                centre - tmax_c
                if centre is not None and tmax_c is not None else None
            ),
            "filled_legs": summary["filled_legs"],
            "held_buckets": summary["held_buckets"],
            "intended_buckets": summary["intended_buckets"],
        })
    return out


#: Where each quality dimension stops being noise (the paper-era matrix's
#: thresholds, kept when it moved onto live fills on 2026-09-16).
CLAIM_FLOOR = 0.75          # hit rate under three quarters of the claim
CENTRE_LIMIT_C = 0.8        # most of a bucket, after every correction
COVERAGE_FLOOR = 0.45       # the basket missed more often than not


def station_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per station, what its live settlements say about each layer.

    User ruling 2026-09-16: the go-live table's gates and its five-dimension
    read (过闸、未过项、不行、不行的维度) all read the live book.  ``rows`` is
    ``settlement_rows`` -- one per settled (station, day, stage) that filled.

    Two grains, each where its question lives.  Claim and realisation are
    per stage row: each stage held its own basket at its own claim.  Coverage,
    centre and the two halves are per station-day: truth is one value a day,
    so three stages holding the same day are one observation of it.

    * ``calibration_ratio`` -- hit rate over the mean claim, rows whose claim
      is known.  The gate wants it in [0.88, 1.18]; under ``CLAIM_FLOOR`` is
      the 声称 dimension.
    * ``realization_ratio`` -- realised PnL over claimed EV, same rows.
      Negative is the 实现 dimension.
    * ``centre_residual_c`` -- mean over days of the held centre minus truth.
      The gate wants |x| <= 0.8; past ``CENTRE_LIMIT_C`` is 中心.
    * ``basket_coverage`` -- share of days whose posted buckets held truth.
    * the halves split at the fleet's median settled day; losing in both is
      持续.

    A station with no settled live row has no entry: no evidence, not a pass
    and not a failure.
    """

    days = sorted({str(row["event_date"]) for row in rows})
    cut = days[len(days) // 2] if days else ""
    per: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        per[str(row["station_id"])].append(row)
    out: dict[str, dict[str, Any]] = {}
    for station, group in sorted(per.items()):
        claimed = [
            row for row in group
            if row.get("claimed_win_probability") is not None
        ]
        hits = sum(int(row["hit"]) for row in claimed)
        claim_mean = (
            sum(float(row["claimed_win_probability"]) for row in claimed)
            / len(claimed) if claimed else None
        )
        calibration = (
            (hits / len(claimed)) / claim_mean
            if claimed and claim_mean else None
        )
        expected = sum(
            float(row["claimed_ev_usdc"]) for row in claimed
            if row.get("claimed_ev_usdc") is not None
        )
        realised_claimed = sum(float(row["pnl_usdc"]) for row in claimed)
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in group:
            by_day[str(row["event_date"])].append(row)
        covered = [
            1 if any(int(row.get("intended_hit") or 0) for row in day_rows)
            else 0
            for day_rows in by_day.values()
        ]
        residuals = []
        for day_rows in by_day.values():
            errors = [
                float(row["center_error_c"]) for row in day_rows
                if row.get("center_error_c") is not None
            ]
            if errors:
                residuals.append(sum(errors) / len(errors))
        halves = [0.0, 0.0]
        counts = [0, 0]
        for day, day_rows in by_day.items():
            index = 0 if day < cut else 1
            halves[index] += sum(float(row["pnl_usdc"]) for row in day_rows)
            counts[index] += 1
        ratio = (
            realised_claimed / expected if abs(expected) > 1e-9 else None
        )
        centre = sum(residuals) / len(residuals) if residuals else None
        coverage = sum(covered) / len(covered) if covered else None
        flags = {
            "realization": ratio is not None and ratio < 0,
            "claim": calibration is not None and calibration < CLAIM_FLOOR,
            "centre": centre is not None and abs(centre) > CENTRE_LIMIT_C,
            "coverage": coverage is not None and coverage < COVERAGE_FLOOR,
            "persistence": bool(
                counts[0] and counts[1] and halves[0] < 0 and halves[1] < 0
            ),
        }
        out[station] = {
            "station_id": station,
            "settled_count": len(group),
            "settled_days": len(by_day),
            "hit_count": hits,
            "claimed_win_probability_mean": claim_mean,
            "calibration_ratio": calibration,
            "expected_usdc": expected,
            "realized_usdc": sum(float(row["pnl_usdc"]) for row in group),
            "realization_ratio": ratio,
            "centre_residual_c": centre,
            "basket_coverage": coverage,
            "early_pnl_usdc": halves[0],
            "late_pnl_usdc": halves[1],
            "early_chains": counts[0],
            "late_chains": counts[1],
            "flags": flags,
            "flag_count": sum(1 for value in flags.values() if value),
        }
    return {
        "split_event_date": cut,
        "thresholds": {
            "claim_floor": CLAIM_FLOOR,
            "centre_limit_c": CENTRE_LIMIT_C,
            "coverage_floor": COVERAGE_FLOOR,
        },
        "stations": out,
    }


def group_legs(
    legs: list[dict[str, Any]],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Legs by (station, event day, stage)."""

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for leg in legs:
        groups[(leg["station_id"], leg["event_date"], leg["stage"])].append(leg)
    return groups


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
    # The standing truth, read once: the resolver needs it for the days the
    # paper book never seated, and the settlement rows need the value itself
    # to score the held centre.  The settler's own reader (charter 44), so
    # "which revision stands" is not restated here.
    truth = None
    if legs and table_exists(conn, "calibration_station_daily_high_truth"):
        try:
            truth = _ledger_truth(conn)
        except sqlite3.Error:
            truth = None
    settled = settled_buckets(
        conn, {(leg["station_id"], leg["event_date"]) for leg in legs},
        truth=truth,
    )
    truth_c = {
        key: float(truth[key].tmax_c)
        for key in settled if truth and key in truth
    }
    book = {
        "legs": legs,
        "settled": settled,
        "settlements": settlement_rows(legs, settled, truth_c),
    }
    _BUILD_CACHE.clear()
    _BUILD_CACHE[key] = {"connection": conn, "book": book}
    return book
