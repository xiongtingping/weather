#!/usr/bin/env python3
"""Every station's daily seat, read against what the market was charging.

Two questions, one page.  Before settlement: where did we sit relative to
the book -- above it, below it, or on it?  After settlement: did the basket
contain the truth, what did the paper book make, at what odds, and which
quotes were not real prices.

The lean is measured as the gap between the model's centre of mass and the
market's, both computed on the same buckets from the same snapshot, in
bucket widths so Fahrenheit and Celsius books read alike:

    lean = model_centre_ordinal - market_centre_ordinal

The market's centre is its own implied distribution -- each bucket's all-in
ask normalised to sum to one.  That is what "the market thinks" means when
the market speaks in prices.

Read-only, and it reads the paper ledger rather than recomputing anything,
so what it shows is exactly what was scored.

Data source only -- the single frontend is scripts/calibration_dashboard.py,
which imports ``build_payload`` from here and renders every section.

    python3 scripts/selection_market_dashboard.py --json   # inspect payload
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

# The ledger owns the rule set; the page only reports it.  Importing the
# label rather than restating it keeps the two from drifting apart, which
# is the whole point of naming a rule at all.
from paper_trading_ledger import SELECTION_RULE_LABEL  # noqa: E402
from bayes_basket_target.delta_v2_runtime import (  # noqa: E402
    _skew_extension_sign,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
INITIAL_CAPITAL_USDC = 1000.0
# Below a one-cent basket cost the fill is almost always a minimum-tick
# offer with nothing bid against it; the paper ledger records these and
# reports them apart rather than letting them set the headline.
DUST_COST_FLOOR = 0.01
# A lean smaller than this is the same view, not a disagreement.
LEAN_TOLERANCE_BUCKETS = 0.35
# Everything before this event date belongs to a different strategy: the
# ladder floor, the dispersion cap and the probability-following rebalance
# all landed on 2026-08-15, so earlier seats were chosen by rules that no
# longer run and averaging them in would describe a system that no longer
# exists.
STATS_START_EVENT_DATE = "2026-08-15"
# The registry's city_label is empty for every station, so the readable name
# lives here.  The ICAO code stays the key everywhere -- it is the stable
# identifier -- and the label is only ever for the page.
STATION_LABELS = {
    "CYYZ": "多伦多", "EDDM": "慕尼黑", "EFHK": "赫尔辛基",
    "EGLC": "伦敦", "EHAM": "阿姆斯特丹", "EPWA": "华沙",
    "FACT": "开普敦", "KATL": "亚特兰大", "KAUS": "奥斯汀",
    "KBKF": "丹佛", "KDAL": "达拉斯", "KHOU": "休斯敦",
    "KLAX": "洛杉矶", "KLGA": "纽约", "KMIA": "迈阿密",
    "KORD": "芝加哥", "KSEA": "西雅图", "KSFO": "旧金山",
    "LEMD": "马德里", "LFPB": "巴黎", "LIMC": "米兰",
    "LTAC": "安卡拉", "MMMX": "墨西哥城", "MPMG": "巴拿马城",
    "NZWN": "惠灵顿", "OEJN": "吉达", "OPKC": "卡拉奇",
    "RCSS": "台北", "RJTT": "东京", "RKPK": "釜山",
    "RKSI": "首尔", "RPLL": "马尼拉", "SAEZ": "布宜诺斯艾利斯",
    "SBGR": "圣保罗", "VILK": "勒克瑙", "WMKK": "吉隆坡",
    "WSSS": "新加坡", "ZBAA": "北京", "ZGGG": "广州",
    "ZGSZ": "深圳", "ZHHH": "武汉", "ZSPD": "上海",
    "ZSQD": "青岛", "ZUCK": "重庆", "ZUUU": "成都",
}


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _centre_of_mass(
    weights: dict[str, float], ordinals: dict[str, int]
) -> float | None:
    total = sum(max(0.0, v) for v in weights.values())
    if total <= 0:
        return None
    return sum(
        ordinals[b] * max(0.0, v)
        for b, v in weights.items()
        if b in ordinals
    ) / total


def _entry_fill(
    *,
    posted_json: str,
    intended_json: str,
    kept_json: str,
    rungs_json: str,
    intended_stake: float | None,
    stake: float,
    window: list[str],
) -> dict[str, Any]:
    """The order as posted against the position that came back, leg by leg.

    Entry is progressive: every rung of every leg rests as its own order, so
    the recorded window is the part of the basket the book chose to sell.
    Read from ``window_buckets_json`` alone that difference is invisible --
    a seat that posted four legs and bought one looks identical to a seat
    that only ever wanted one, and the charter's three-rung floor is a rule
    about the first number.

    Two completion rates, because they answer different questions.  Rungs
    say how much of the ladder the market came down to; shares and stake say
    how much of the intended position that bought, which is smaller whenever
    the legs that missed were the ones carrying weight.
    """

    def _load(payload: str, default: Any) -> Any:
        try:
            value = json.loads(payload) if payload else default
        except (TypeError, ValueError):
            return default
        return value if isinstance(value, type(default)) else default

    posted: list[str] = _load(posted_json, []) or list(window)
    intended: dict[str, float] = _load(intended_json, {})
    kept: dict[str, float] = _load(kept_json, {})
    rungs: dict[str, str] = _load(rungs_json, {})
    legs: list[dict[str, Any]] = []
    got_rungs = 0
    posted_rungs = 0
    for bucket in posted:
        want = float(intended.get(bucket, 0.0) or 0.0)
        have = float(kept.get(bucket, 0.0) or 0.0)
        filled_rungs: int | None = None
        leg_rungs: int | None = None
        price: float | None = None
        record = str(rungs.get(bucket, "") or "")
        if "@" in record and "/" in record:
            fraction, _, tail = record.partition("@")
            head, _, total = fraction.partition("/")
            try:
                filled_rungs, leg_rungs = int(head), int(total)
                price = float(tail)
            except ValueError:
                filled_rungs = leg_rungs = None
                price = None
        if filled_rungs is not None and leg_rungs:
            got_rungs += filled_rungs
            posted_rungs += leg_rungs
        legs.append({
            "bucket": bucket,
            "intended_shares": want,
            "filled_shares": have,
            "share_fill": (have / want) if want > 0 else None,
            "filled_rungs": filled_rungs,
            "posted_rungs": leg_rungs,
            "fill_price": price,
            "empty": have <= 0.0,
        })
    want_shares = sum(leg["intended_shares"] for leg in legs)
    have_shares = sum(leg["filled_shares"] for leg in legs)
    want_stake = (
        float(intended_stake) if intended_stake is not None else None
    )
    return {
        "posted_buckets": posted,
        "posted_leg_count": len(posted),
        "filled_leg_count": sum(1 for leg in legs if not leg["empty"]),
        "empty_leg_count": sum(1 for leg in legs if leg["empty"]),
        "legs": legs,
        "intended_shares": want_shares,
        "filled_shares": have_shares,
        "share_completion": (
            have_shares / want_shares if want_shares > 0 else None
        ),
        "rung_completion": (
            got_rungs / posted_rungs if posted_rungs else None
        ),
        "intended_stake_usdc": want_stake,
        # What the position cost, carried beside what it was meant to cost so
        # a reader never has to reconstruct one from the other.
        "filled_stake_usdc": stake,
        "stake_shortfall_usdc": (
            None if want_stake is None else want_stake - stake
        ),
        "stake_completion": (
            None
            if want_stake is None or want_stake <= 0
            else stake / want_stake
        ),
        # A leg missing from the middle of the ladder, which selection
        # cannot produce -- the support is contiguous by construction, so a
        # gap is always the book's doing.
        "gapped": bool(
            len(window) > 1
            and any(
                bucket not in window
                for bucket in posted[
                    posted.index(window[0]):posted.index(window[-1]) + 1
                ]
            )
            if window and window[0] in posted and window[-1] in posted
            else False
        ),
    }


def _rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    query = conn.execute(
        "SELECT p.station_id, p.event_date, p.stage, p.decision_at_utc,"
        " p.window_buckets_json, p.ladder_center_bucket, p.win_probability,"
        " p.all_in_cost, p.edge, p.stake_usdc, p.probabilities_json,"
        " p.prices_json, p.window_size, p.selection_rule_label,"
        " s.hit, s.settled_tmax_c, s.settled_bucket, s.pnl_usdc,"
        " s.bucket_distance, s.center_error_c,"
        " p.posted_buckets_json, p.intended_legs_json, p.leg_shares_json,"
        " p.resting_fill_json, p.intended_stake_usdc,"
        " p.chain_shares_json, p.chain_stake_usdc, s.stage,"
        " s.basket_all_in_cost"
        " FROM paper_trade_position p"
        # Joined on the station-day, not the position: the three stages are
        # one position and settle once, so every top-up in a chain reads the
        # outcome the chain got.  Joined by position id, only the stage that
        # happened to act last would show a settlement and the other rows
        # would read as open forever.
        " LEFT JOIN paper_trade_settlement s"
        " ON s.station_id = p.station_id AND s.event_date = p.event_date"
        " ORDER BY p.event_date DESC, p.station_id, p.stage"
    )
    for r in query:
        if str(r[1]) < STATS_START_EVENT_DATE:
            continue
        probabilities = json.loads(r[10])
        prices = json.loads(r[11])
        ordinals = {
            bucket: index
            for index, bucket in enumerate(probabilities.keys())
        }
        model_centre = _centre_of_mass(probabilities, ordinals)
        # The day's skew reading, the same predicate the charter-55 gate
        # runs: sign of (mass-weighted mean minus heaviest bucket), gated
        # at half a bucket.  Index ordinals are uniform, so the half-step
        # threshold is exact here too.
        skew_sign = _skew_extension_sign(probabilities, ordinals)
        market_centre = _centre_of_mass(prices, ordinals)
        lean = (
            None
            if model_centre is None or market_centre is None
            else model_centre - market_centre
        )
        if lean is None:
            side = "unknown"
        elif lean > LEAN_TOLERANCE_BUCKETS:
            side = "above"
        elif lean < -LEAN_TOLERANCE_BUCKETS:
            side = "below"
        else:
            side = "aligned"
        cost = float(r[7])
        win = float(r[6])
        # What the seat covers, what it cost, and what that pair is worth.
        # Coverage is the calibrated probability summed over the seated
        # buckets; the cost is what the basket was actually filled at.
        #
        # It used to be the sum of the seated buckets' *top-of-book* offers,
        # which is a different number the moment a clip outruns the level
        # quoting it -- ZGSZ 2026-08-15 showed 0.004 here beside a P&L
        # computed on the 0.01771 it really paid, so the page reported a
        # return no price on it could produce.  One number, the paid one.
        # The CHOSEN basket's crossing price: the intended legs summed at
        # the entry ask map.  Summed over the bought width it silently
        # shrank whenever the maker walk was still part-filled -- a
        # two-of-three fill read as a cheaper basket, not an unfinished
        # one -- and the number the page calls 买价和 must be a property
        # of the choice, not of how much of it the market has sold us yet.
        try:
            _intended_for_ask = list((json.loads(r[21]) or {}).keys())
        except (TypeError, ValueError, AttributeError):
            _intended_for_ask = []
        seat_top_of_book_total = sum(
            max(0.0, prices.get(b, 0.0))
            for b in (_intended_for_ask or json.loads(r[4]))
        )
        seat_ask_total = cost
        # EV per USDC staked.  A basket costing C pays 1 when the truth lands
        # inside it, so a unit of stake buys 1/C of payout and returns
        # p/C - 1 in expectation.  This is the same quantity the edge (p - C)
        # describes, restated per unit of capital, which is what makes two
        # seats at different price levels comparable.
        ev_per_usdc = (win / cost - 1.0) if cost > 0 else None
        stake = float(r[9])
        out.append({
            "seat_ask_total": seat_ask_total,
            "seat_top_of_book_total": seat_top_of_book_total,
            "ev_per_usdc": ev_per_usdc,
            "ev_usdc": (
                None if ev_per_usdc is None else stake * ev_per_usdc
            ),
            "station_id": r[0],
            "station_label": STATION_LABELS.get(str(r[0]), ""),
            "event_date": r[1],
            "stage": r[2],
            "decision_at_utc": r[3],
            "window": json.loads(r[4]),
            "window_size": int(r[12]),
            # Which rule set chose this basket.  Two days on the page can be
            # compiled under different rules -- the ledger re-derives a scope
            # when the fingerprint moves, but only inside the window it can
            # reach -- so a basket has to say which one it is rather than
            # letting the reader assume they are comparable.
            "selection_rule_label": str(r[13] or ""),
            "centre_bucket": r[5],
            "win_probability": float(r[6]),
            "all_in_cost": cost,
            "edge": float(r[8]),
            "odds": (1.0 - cost) / cost if cost > 0 else None,
            "stake_usdc": float(r[9]),
            "lean_buckets": lean,
            "side": side,
            "skew_sign": skew_sign,
            "dust_offer": cost < DUST_COST_FLOOR,
            # The chain's outcome belongs to the chain, and it is carried by
            # the stage that last topped the position up.  Every stage row
            # can see it -- the join is on the station-day -- but only the
            # tail row *is* it, so an average over stage rows does not count
            # one settled position two or three times.  That double count is
            # the three-book reading this ledger was corrected out of, and it
            # would come straight back through a page that sums stage rows.
            "chain_tail": bool(r[27]) and str(r[27]) == str(r[2]),
            "chain_stake_usdc": (
                float(r[26]) if r[26] is not None else stake
            ),
            "chain_all_in_cost": (
                float(r[28]) if r[28] is not None else cost
            ),
            "chain_shares": {
                str(bucket): float(value)
                for bucket, value in (
                    json.loads(r[25] or "{}") or {}
                ).items()
            },
            "settled": r[14] is not None,
            "hit": None if r[14] is None else int(r[14]),
            "settled_tmax_c": r[15],
            "settled_bucket": r[16],
            "pnl_usdc": r[17],
            "bucket_distance": r[18],
            "center_error_c": r[19],
            "entry": _entry_fill(
                posted_json=str(r[20] or ""),
                intended_json=str(r[21] or ""),
                kept_json=str(r[22] or ""),
                rungs_json=str(r[23] or ""),
                intended_stake=r[24],
                stake=stake,
                window=json.loads(r[4]),
            ),
        })
    return out



def _basket_hit_by_size(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Did the truth land inside the basket, by how many rungs it had.

    The stage tables answer "was this seat right"; this answers "does buying
    a wider window buy a proportionally better chance".  Only credible fills
    count, for the same reason everything else on the page does.
    """

    out: list[dict[str, Any]] = []
    for size, settled, hits, cost, claimed, staked, pnl in conn.execute(
        "SELECT p.window_size, COUNT(*), SUM(s.hit), AVG(p.all_in_cost),"
        " AVG(s.claimed_win_probability), SUM(s.stake_usdc),"
        " SUM(s.pnl_usdc)"
        " FROM paper_trade_settlement s"
        " JOIN paper_trade_position p USING(position_id)"
        " WHERE p.all_in_cost>=?"
        " GROUP BY p.window_size ORDER BY p.window_size",
        (DUST_COST_FLOOR,),
    ):
        count = int(settled or 0)
        if not count:
            continue
        out.append({
            "window_size": int(size),
            "settled_count": count,
            "hit_count": int(hits or 0),
            "hit_rate": (hits or 0) / count,
            "all_in_cost_mean": float(cost or 0.0),
            "claimed_win_probability_mean": float(claimed or 0.0),
            "staked_usdc": float(staked or 0.0),
            "pnl_usdc": float(pnl or 0.0),
        })
    return out


CONSOLIDATION_LIGHTS = ("stable", "sharp", "quoted")


def _consolidation_lights(conn: sqlite3.Connection) -> dict[str, Any]:
    """The three consolidation lights, chain by chain, with their strata.

    Chain level on purpose: the stake tilt the lights drive is decided by
    the P0 opener and inherited by the whole chain, so the strata that
    justify it must be counted on chains, not on stage rows.  Every group
    carries the same two honesty checks (ex-best, ex-best-two): a stratum
    whose sign needs its best chain is a lottery.

    ``quoted`` is shown but flagged: it fires on nearly every settled day,
    so alone it mostly says whether there was a book at all.  Its earlier
    exclusion from the per-signal split (the six-day +220% lottery stratum)
    is why the combo table separates it rather than pooling it away.
    """

    try:
        opener_rows = conn.execute(
            "SELECT station_id, event_date, consolidation_json"
            " FROM paper_trade_position"
            " WHERE stage='P0' AND consolidation_json<>''"
        ).fetchall()
        settle_rows = conn.execute(
            "SELECT station_id, event_date, hit, stake_usdc, pnl_usdc"
            " FROM paper_trade_settlement"
        ).fetchall()
        open_rows = conn.execute(
            "SELECT p.station_id, p.event_date, SUM(p.stake_usdc)"
            " FROM paper_trade_position p"
            " WHERE NOT EXISTS (SELECT 1 FROM paper_trade_settlement s"
            "  WHERE s.station_id=p.station_id"
            "    AND s.event_date=p.event_date)"
            " GROUP BY p.station_id, p.event_date"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"available": False}

    lights: dict[tuple[str, str], dict[str, Any]] = {}
    for sid, day, payload in opener_rows:
        try:
            flags = json.loads(payload) or {}
        except (TypeError, ValueError):
            continue
        if isinstance(flags, Mapping):
            lights[(str(sid), str(day))] = {
                name: flags.get(name) is True
                for name in CONSOLIDATION_LIGHTS
            }

    chains: dict[tuple[str, str], dict[str, Any]] = {}
    for sid, day, hit, stake, pnl in settle_rows:
        key = (str(sid), str(day))
        chain = chains.setdefault(key, {
            "station": key[0], "event_date": key[1],
            "stake": 0.0, "pnl": 0.0, "hit": False,
            "flags": lights.get(key),
        })
        chain["stake"] += float(stake or 0.0)
        chain["pnl"] += float(pnl or 0.0)
        chain["hit"] = chain["hit"] or bool(hit)

    def stats(group: list[dict[str, Any]]) -> dict[str, Any]:
        staked = sum(c["stake"] for c in group)
        pnl = sum(c["pnl"] for c in group)
        wins = sorted((c["pnl"] for c in group if c["pnl"] > 0),
                      reverse=True)
        return {
            "chains": len(group),
            "hit_chains": sum(1 for c in group if c["hit"]),
            "staked": staked,
            "pnl": pnl,
            "roi": pnl / staked if staked else None,
            "roi_ex_best": (
                (pnl - (wins[0] if wins else 0.0)) / staked
                if staked else None
            ),
            "roi_ex_best2": (
                (pnl - sum(wins[:2])) / staked if staked else None
            ),
        }

    flagged = [c for c in chains.values() if c["flags"] is not None]
    combos = []
    for stable in (False, True):
        for sharp in (False, True):
            for quoted in (False, True):
                group = [
                    c for c in flagged
                    if c["flags"]["stable"] == stable
                    and c["flags"]["sharp"] == sharp
                    and c["flags"]["quoted"] == quoted
                ]
                count = int(stable) + int(sharp) + int(quoted)
                combos.append({
                    "stable": stable, "sharp": sharp, "quoted": quoted,
                    "count": count,
                    "tilted": count >= _tilt_rule()["min_signals"],
                    **stats(group),
                })

    counts = []
    for count in (0, 1, 2, 3):
        group = [c for c in flagged
                 if sum(c["flags"].values()) == count]
        counts.append({"count": count, **stats(group)})

    singles = []
    for name in CONSOLIDATION_LIGHTS:
        for lit in (False, True):
            group = [c for c in flagged if c["flags"][name] is lit]
            singles.append({"signal": name, "lit": lit, **stats(group)})

    open_chains = sorted(
        (
            {
                "station": str(sid), "event_date": str(day),
                "stake": float(stake or 0.0),
                "flags": lights.get((str(sid), str(day))),
            }
            for sid, day, stake in open_rows
        ),
        key=lambda c: (c["event_date"], c["station"]),
        reverse=True,
    )

    return {
        "available": bool(flagged or open_chains),
        "rule": _tilt_rule(),
        "combos": combos,
        "counts": counts,
        "singles": singles,
        "settled_chains": len(chains),
        "flagged_chains": len(flagged),
        "open_chains": open_chains,
    }


def _tilt_rule() -> dict[str, Any]:
    """The shipped tilt rule, read from the ledger itself, never restated."""

    import paper_trading_ledger as ledger  # noqa: PLC0415

    return {
        "min_signals": ledger.CONSOLIDATION_TILT_MIN_SIGNALS,
        "factor": ledger.CONSOLIDATION_TILT_FACTOR,
        "signals": list(ledger.CONSOLIDATION_TILT_SIGNALS),
    }


def _settled_chain_baskets(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """One record per settled chain: what the objective chose, what was
    bought, and where the truth landed.

    ``chosen`` is the union of ``intended_legs_json`` across the chain's
    stages -- the legs the objective asked for, hedge rungs included.  A
    chain is one book, so a leg wanted at any stage was chosen.  Rows from
    before the intended ledger existed fall back to the posted ladder, then
    to the bought width, which are the nearest records that survive.
    ``bought`` is the union of ``window_buckets_json`` -- the width that
    came back after the fill walk.
    """

    rows = conn.execute(
        "SELECT s.station_id, s.event_date, s.settled_bucket,"
        " s.claimed_win_probability, p.intended_legs_json,"
        " p.posted_buckets_json, p.window_buckets_json"
        " FROM paper_trade_settlement s"
        " JOIN paper_trade_position p USING(station_id, event_date)"
        " WHERE s.settled_bucket IS NOT NULL"
        " ORDER BY s.event_date",
    ).fetchall()
    chains: dict[tuple[str, str], dict[str, Any]] = {}
    for station, event_date, landed, claimed, ij, pj, wj in rows:
        chain = chains.setdefault((str(station), str(event_date)), {
            "event_date": str(event_date),
            "chosen": set(),
            "bought": set(),
            "landed": str(landed),
            "claimed": float(claimed or 0.0),
        })
        chosen: list[str] = []
        try:
            payload = json.loads(ij) if ij else {}
            chosen = [str(b) for b in payload.keys()]
        except (TypeError, ValueError, AttributeError):
            chosen = []
        if not chosen:
            for fallback in (pj, wj):
                try:
                    chosen = [str(b) for b in json.loads(fallback or "[]")]
                except (TypeError, ValueError):
                    chosen = []
                if chosen:
                    break
        chain["chosen"].update(chosen)
        try:
            chain["bought"].update(
                str(b) for b in json.loads(wj or "[]")
            )
        except (TypeError, ValueError):
            pass
    return list(chains.values())


def _posted_basket_hit_trend(
    conn: sqlite3.Connection, sizes: Sequence[int] = (3, 4)
) -> dict[str, Any]:
    """Hit rate of the basket the objective *chose*, per chosen width.

    ``_basket_hit_by_size`` groups on ``window_size``, the width that came
    back after the market sold us part of the ladder -- so a three-rung
    choice the book cut to one is counted as a one-rung basket and the
    three-rung row silently loses its hardest cases.  Grouping on the
    posted ladder (this function until 2026-08-24) still lost the legs a
    value clamp left unpostable.  The question "is a three-bucket choice
    any good" is about ``intended_legs_json`` -- the objective's own pick
    -- so the base is now the chain-level union of intended legs (see
    ``_settled_chain_baskets``).

    Both readings are kept per row: ``hit_rate`` is whether the truth
    landed in what was chosen, ``filled_hit_rate`` whether it landed in
    what was actually bought.  Measured 2026-08-24 over 74 settled chains
    the two were 48.6% and 20.3% -- the gap is the market declining to
    sell the legs that went on to win, and it is the single largest term
    in why the page's realised hit rate looks low.  ``claimed_mean`` is
    the settlement's recorded entry claim, so the ratio compares the claim
    the book was sized on against the choice it described.

    Trend splits the settled days in half by date rather than fitting
    anything: with samples this size a slope is noise wearing a number,
    and "the last half against the first" is the comparison a reader makes
    anyway.  ``None`` when either half is too thin to say.
    """

    wanted = {int(size) for size in sizes}
    by_size: dict[int, list[dict[str, Any]]] = {size: [] for size in wanted}
    for chain in _settled_chain_baskets(conn):
        size = len(chain["chosen"])
        if size not in wanted:
            continue
        by_size[size].append({
            "event_date": chain["event_date"],
            "hit": int(chain["landed"] in chain["chosen"]),
            "filled_hit": int(chain["landed"] in chain["bought"]),
            "claimed": chain["claimed"],
        })

    def account(items: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        if not items:
            return None
        n = len(items)
        hit = sum(int(item["hit"]) for item in items)
        claim = sum(float(item["claimed"]) for item in items) / n
        return {
            "settled_count": n,
            "hit_rate": hit / n,
            "filled_hit_rate": (
                sum(int(item["filled_hit"]) for item in items) / n
            ),
            "claimed_mean": claim,
            "realised_over_claimed": (hit / n) / claim if claim else None,
        }

    out: dict[str, Any] = {"sizes": []}
    for size in sorted(wanted):
        items = by_size[size]
        overall = account(items)
        if overall is None:
            continue
        # Split on the day, not the row: a station-day's stages must not
        # land on opposite sides of the cut.  Cut where the *settlements*
        # are even rather than at the middle date -- early days carry far
        # fewer seats, so a calendar midpoint put 14 rows against 66 and
        # called the noisier side "before".
        counts: dict[str, int] = defaultdict(int)
        for item in items:
            counts[item["event_date"]] += 1
        days = sorted(counts)
        target = len(items) / 2.0
        running = 0
        half = 0
        for index, day in enumerate(days):
            if running >= target:
                break
            running += counts[day]
            half = index + 1
        early_days = set(days[:half])
        earlier = account(
            [item for item in items if item["event_date"] in early_days]
        )
        later = account(
            [item for item in items if item["event_date"] not in early_days]
        )
        MIN_HALF = 8
        movable = (
            earlier is not None
            and later is not None
            and earlier["settled_count"] >= MIN_HALF
            and later["settled_count"] >= MIN_HALF
        )
        # Whether the move is even distinguishable from noise.  A hit rate
        # is a proportion, so the two halves' standard errors combine; under
        # two of those the difference is a coin landing differently, and
        # printing it as a direction would manufacture a signal.  Measured
        # 2026-08-20 both widths sat at |delta|/SE ~ 0.4.
        delta = later["hit_rate"] - earlier["hit_rate"] if movable else None
        stderr = None
        if movable:
            stderr = math.sqrt(
                earlier["hit_rate"] * (1.0 - earlier["hit_rate"])
                / earlier["settled_count"]
                + later["hit_rate"] * (1.0 - later["hit_rate"])
                / later["settled_count"]
            )
        out["sizes"].append({
            "chosen_size": size,
            **overall,
            "earlier": earlier if movable else None,
            "later": later if movable else None,
            "delta": delta,
            "delta_stderr": stderr,
            "delta_is_readable": (
                bool(stderr) and delta is not None
                and abs(delta) >= 2.0 * stderr
            ),
            "trend_window": (
                {"earlier_days": days[:half], "later_days": days[half:]}
                if movable else None
            ),
        })
    return out


_BUCKET_ORDINAL_RE = re.compile(r"^\s*(-?\d+)(?:-(-?\d+))?")


def _bucket_ordinal(label: str) -> int | None:
    """The low edge of a bucket label: '26°C' -> 26, '82-83°F' -> 82."""

    match = _BUCKET_ORDINAL_RE.match(str(label))
    return int(match.group(1)) if match else None


def _bucket_step(label: str) -> int:
    """The width one bucket spans in its own label vocabulary.

    '82-83°F' spans two degrees; '26°C' spans one.  The step turns a raw
    degree offset into "how many buckets away", which is the unit the
    charter's windows are built in.
    """

    match = _BUCKET_ORDINAL_RE.match(str(label))
    if match and match.group(2) is not None:
        return max(1, int(match.group(2)) - int(match.group(1)) + 1)
    return 1


def _chosen_miss_offsets(conn: sqlite3.Connection) -> dict[str, Any]:
    """Where the truth lands when it misses the chosen basket, in buckets.

    Distance is measured from the chosen set's own EDGE -- the nearest
    chosen bucket -- never from the centre bucket: a truth one step below
    a four-wide choice is -1 however far the centre sits.  Offsets are
    signed on the temperature axis: negative means the truth settled
    *below* the chosen set (the choice ran too hot), positive *above* it
    (too cold).  Distance is in the station's own bucket steps, so one
    bucket out on a 2°F station is one, not two.

    One bucket out (``adjacent``) means the truth stopped in the very next
    bucket past the edge -- one more bucket on that side would have covered
    it; two or more (``far``) means the whole side was misread and one more
    bucket buys nothing.  The two point at different fixes, which is why
    the split is on the page.

    ``inside_gap`` counts truths that landed between chosen buckets of a
    non-contiguous cross-stage union; they are misses without a side.
    """

    hist: dict[int, int] = defaultdict(int)
    left = right = adjacent = far = 0
    hits = misses = unparsed = inside_gap = 0
    for chain in _settled_chain_baskets(conn):
        if chain["landed"] in chain["chosen"]:
            hits += 1
            continue
        misses += 1
        ordinals = sorted({
            o for o in (_bucket_ordinal(b) for b in chain["chosen"])
            if o is not None
        })
        landed_ordinal = _bucket_ordinal(chain["landed"])
        if landed_ordinal is None or not ordinals:
            unparsed += 1
            continue
        step = _bucket_step(chain["landed"])
        if landed_ordinal < ordinals[0]:
            offset = -max(1, round((ordinals[0] - landed_ordinal) / step))
        elif landed_ordinal > ordinals[-1]:
            offset = max(1, round((landed_ordinal - ordinals[-1]) / step))
        else:
            inside_gap += 1
            continue
        clamped = max(-3, min(3, offset))
        hist[clamped] += 1
        if offset < 0:
            left += 1
        else:
            right += 1
        if abs(offset) == 1:
            adjacent += 1
        else:
            far += 1
    labels = {-3: "≤-3", -2: "-2", -1: "-1", 1: "+1", 2: "+2", 3: "≥+3"}
    return {
        "settled": hits + misses,
        "hits": hits,
        "misses": misses,
        "unparsed": unparsed,
        "inside_gap": inside_gap,
        "below": left,
        "above": right,
        "adjacent": adjacent,
        "far": far,
        "histogram": [
            {"offset": labels[key], "count": hist[key]}
            for key in (-3, -2, -1, 1, 2, 3)
            if hist[key]
        ],
    }


def _resting_entry_trend(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The same fill account as ``_resting_entry_stats``, one row per day.

    The pooled figure answers "does resting work"; it cannot answer "is it
    getting better or worse", and that is the question after every change to
    how long an order rests or how often it is re-posted.  A window-wide
    average absorbs the change instead of showing it.

    Still per stage, for the same reason the pooled figure is: P0 rests
    overnight on a book that barely trades, P1 rests inside the session on
    the busiest book of the day, and an average over the two describes
    neither.
    """

    try:
        rows = conn.execute(
            "SELECT event_date, stage, resting_fill_json"
            " FROM paper_trade_position"
            " WHERE resting_fill_json NOT IN ('', '{}')"
            " ORDER BY event_date DESC",
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    by_day: dict[str, dict[str, dict[str, float]]] = {}
    for event_date, stage, payload in rows:
        try:
            record = json.loads(payload)
        except (TypeError, ValueError):
            continue
        slot = by_day.setdefault(str(event_date), {}).setdefault(
            str(stage),
            {
                "baskets": 0, "legs": 0, "empty_legs": 0,
                "posted_rungs": 0, "filled_rungs": 0,
                "price_sum": 0.0, "price_legs": 0,
            },
        )
        slot["baskets"] += 1
        for value in record.values():
            try:
                fraction, price = str(value).split("@")
                got, posted = (int(part) for part in fraction.split("/"))
            except ValueError:
                continue
            slot["legs"] += 1
            slot["posted_rungs"] += posted
            slot["filled_rungs"] += got
            slot["empty_legs"] += int(got == 0)
            if got:
                slot["price_sum"] += float(price)
                slot["price_legs"] += 1
    out: list[dict[str, Any]] = []
    for event_date in sorted(by_day, reverse=True):
        stages: dict[str, Any] = {}
        for stage, slot in by_day[event_date].items():
            legs = slot["legs"]
            stages[stage] = {
                "baskets": int(slot["baskets"]),
                "legs": int(legs),
                "rung_completion_rate": (
                    slot["filled_rungs"] / slot["posted_rungs"]
                    if slot["posted_rungs"] else None
                ),
                "leg_fill_rate": (
                    (legs - slot["empty_legs"]) / legs if legs else None
                ),
                "mean_filled_price": (
                    slot["price_sum"] / slot["price_legs"]
                    if slot["price_legs"] else None
                ),
            }
        out.append({"event_date": event_date, "stages": stages})
    return out


def _resting_entry_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Per stage, how much of what was posted actually got bought.

    Never summed across stages (user ruling, 2026-08-17).  P0 rests twelve
    hours before the event on a book that barely trades, P1 rests inside the
    session on the busiest book of the day, and P0-M rests once, on whatever
    the market happens to be doing when the TAF lands.  A pooled fill rate
    is an average over three different markets and describes none of them.

    Entry is progressive: each rung of each leg is its own order, so a
    basket can be bought in part.  "Fill" therefore has two meanings and
    both are reported -- what fraction of the posted rungs bought anything,
    and how often a leg bought nothing at all.
    """

    stages: dict[str, dict[str, Any]] = {}
    for count, stage in conn.execute(
        "SELECT COUNT(*), stage FROM paper_trade_position GROUP BY stage"
    ):
        stages.setdefault(str(stage), {})["seated_count"] = int(count)
    try:
        rows = conn.execute(
            # Both codes mean a posted order that bought nothing, which is
            # what this tally counts; the split between them is read from
            # the refusal table in section 13.
            "SELECT COUNT(*), stage FROM paper_trade_refusal"
            " WHERE reason IN ('resting_unfilled','belief_withdrew_order')"
            " GROUP BY stage"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for count, stage in rows:
        stages.setdefault(str(stage), {})["nothing_filled_count"] = int(count)
    try:
        ladders = conn.execute(
            "SELECT stage, window_size, resting_fill_json,"
            "       leg_shares_json, intended_legs_json, prices_json,"
            "       stake_usdc, intended_stake_usdc, event_date,"
            "       ladder_center_bucket, station_id, decision_at_utc,"
            "       posted_buckets_json, held_before_json"
            " FROM paper_trade_position"
            " WHERE resting_fill_json NOT IN ('', '{}')"
            # rowid: accumulation order, so a later-stage row with an earlier
            # cycle's decision timestamp cannot be shadowed by wall clock.
            " ORDER BY station_id, event_date, rowid"
        ).fetchall()
    except sqlite3.OperationalError:
        ladders = []
    # A day that has not resolved is still filling: its orders are on the
    # book, not missed.  The 90% target is judged on the days whose books
    # have closed, or the verdict would flap red every morning and clear
    # itself by night.
    today = datetime.now(timezone.utc).date().isoformat()
    # What a top-up stage was trying to buy, split two ways.  A later stage
    # opens nothing on its own -- it adds to the position P0 opened (user
    # correction, 2026-08-22) -- but within that chain it does two different
    # things, and one pooled fill rate hides both.  A *carried* rung is one
    # an earlier stage posted and the market never came down to, so the
    # later stage is retrying a leg the value line already refused once; a
    # *fresh* rung is a bucket the chain has never held, which the evidence
    # has newly moved onto.  Measured 2026-08-22: of P0-M's posted legs, 41
    # were carried and 26 fresh, and 94% of its "whole basket missed" days
    # were days P0 had also left a leg unbought -- so judging P0-M by the
    # pooled number is largely judging it on P0's leftovers.
    posted_before: dict[tuple[str, str], set[str]] = {}
    unfilled_before: dict[tuple[str, str], set[str]] = {}
    for (stage, entered_legs, payload, legs_payload, intended_payload,
         prices_payload, stake, intended_stake, event_date,
         centre_bucket, station_id, _decided, posted_payload,
         _held_before) in ladders:
        try:
            record = json.loads(payload)
        except (TypeError, ValueError):
            continue
        slot = stages.setdefault(str(stage), {})
        detail = slot.setdefault("ladder", {
            "posted_legs": 0, "empty_legs": 0, "full_legs": 0,
            "posted_rungs": 0, "filled_rungs": 0,
            "price_sum": 0.0, "price_legs": 0,
            "partial_baskets": 0, "baskets": 0,
            "by_posted_rungs": {},
            "intended_shares": 0.0, "filled_shares": 0.0,
            "closed_intended_shares": 0.0, "closed_filled_shares": 0.0,
            "core_want": 0.0, "core_got": 0.0,
            "hedge_want": 0.0, "hedge_got": 0.0,
            "carried_want": 0.0, "carried_got": 0.0,
            "fresh_want": 0.0, "fresh_got": 0.0,
            "stake": 0.0, "intended_stake": 0.0,
            "taker_equivalent": 0.0,
        })
        chain_key = (str(station_id), str(event_date))
        detail["baskets"] += 1
        detail["partial_baskets"] += int(len(record) > int(entered_legs))
        # The share account: of the size that was posted, how much was
        # bought -- and what the bought part would have cost crossing at
        # the entry ask instead.  These three are what the 90% target and
        # the "does resting still save money" verdict are read from.
        detail["stake"] += float(stake or 0.0)
        detail["intended_stake"] += float(intended_stake or 0.0)
        try:
            filled_legs = json.loads(legs_payload) if legs_payload else {}
            intended_legs = (
                json.loads(intended_payload) if intended_payload else {}
            )
            entry_asks = json.loads(prices_payload) if prices_payload else {}
        except (TypeError, ValueError):
            filled_legs, intended_legs, entry_asks = {}, {}, {}
        closed = str(event_date) < today
        for bucket, want in intended_legs.items():
            got = float(filled_legs.get(bucket, 0.0))
            detail["intended_shares"] += float(want)
            detail["filled_shares"] += got
            if closed:
                detail["closed_intended_shares"] += float(want)
                detail["closed_filled_shares"] += got
                # Core is the centre leg, the exposure the basket exists
                # to hold; everything else is the hedge.  The two run
                # different execution rules, so one pooled rate hides
                # which rule is failing (user redesign, 2026-08-21).
                if str(bucket) == str(centre_bucket or ""):
                    detail["core_want"] += float(want)
                    detail["core_got"] += got
                else:
                    detail["hedge_want"] += float(want)
                    detail["hedge_got"] += got
                # Carried vs fresh, for the top-up stages.  P0 has no
                # earlier stage, so every rung of its is fresh by
                # construction and the split says nothing about it.
                if str(stage) != "P0":
                    seen = unfilled_before.get(chain_key, set())
                    if str(bucket) in seen:
                        detail["carried_want"] += float(want)
                        detail["carried_got"] += got
                    elif str(bucket) not in posted_before.get(
                        chain_key, set()
                    ):
                        detail["fresh_want"] += float(want)
                        detail["fresh_got"] += got
            detail["taker_equivalent"] += got * float(
                entry_asks.get(bucket) or 0.0
            )
        try:
            posted_now = set(json.loads(posted_payload or "[]"))
        except (TypeError, ValueError):
            posted_now = set()
        filled_now = {
            str(b) for b, q in (filled_legs or {}).items()
            if float(q or 0.0) > 0.0
        }
        posted_before.setdefault(chain_key, set()).update(posted_now)
        # A rung counts as carried only while it is still unbought: once a
        # later stage fills it, it stops being the leg the value line
        # refused.
        carried = unfilled_before.setdefault(chain_key, set())
        carried |= {str(b) for b in posted_now} - filled_now
        carried -= filled_now
        for value in record.values():
            try:
                fraction, price = str(value).split("@")
                got, posted = (int(part) for part in fraction.split("/"))
            except ValueError:
                continue
            detail["posted_legs"] += 1
            detail["posted_rungs"] += posted
            detail["filled_rungs"] += got
            detail["empty_legs"] += int(got == 0)
            detail["full_legs"] += int(got == posted)
            if got:
                detail["price_sum"] += float(price)
                detail["price_legs"] += 1
            depth = detail["by_posted_rungs"].setdefault(
                posted, {"legs": 0, "filled": 0}
            )
            depth["legs"] += 1
            depth["filled"] += got
    out = []
    for stage in ("P0", "P0-M", "P1"):
        entry = stages.get(stage)
        if not entry:
            continue
        detail = entry.get("ladder") or {}
        posted_rungs = int(detail.get("posted_rungs") or 0)
        posted_legs = int(detail.get("posted_legs") or 0)
        price_legs = int(detail.get("price_legs") or 0)
        out.append({
            "stage": stage,
            "seated_count": int(entry.get("seated_count") or 0),
            "nothing_filled_count": int(entry.get("nothing_filled_count") or 0),
            "partial_basket_count": int(detail.get("partial_baskets") or 0),
            "basket_count": int(detail.get("baskets") or 0),
            "leg_count": posted_legs,
            "empty_leg_rate": (
                detail.get("empty_legs", 0) / posted_legs
                if posted_legs else None
            ),
            "full_ladder_rate": (
                detail.get("full_legs", 0) / posted_legs
                if posted_legs else None
            ),
            "rung_completion_rate": (
                detail.get("filled_rungs", 0) / posted_rungs
                if posted_rungs else None
            ),
            "mean_filled_price": (
                detail.get("price_sum", 0.0) / price_legs
                if price_legs else None
            ),
            # Of the shares posted, how many were bought.  This is the
            # figure the 90% fill target is judged on: rung counts weight a
            # one-share leg the same as a hundred-share leg, and stake
            # ratios move when the price moves, not only when the fill does.
            "share_fill_rate": (
                detail.get("filled_shares", 0.0)
                / detail["intended_shares"]
                if detail.get("intended_shares") else None
            ),
            # The same rate over resolved days only -- what the 90% target
            # is judged on.  An unresolved day's orders are still on the
            # book; counting them as misses fails the morning and passes
            # the evening.
            "closed_share_fill_rate": (
                detail.get("closed_filled_shares", 0.0)
                / detail["closed_intended_shares"]
                if detail.get("closed_intended_shares") else None
            ),
            # The one pooled rate splits by role (user redesign,
            # 2026-08-21): the core leg is the exposure the basket exists
            # to hold and answers to a hard floor; the hedge legs answer
            # to the value red line first, so their rate is a reading,
            # not a target -- a clamped hedge leg the market never came
            # down to is the rule working.
            "core_fill_rate": (
                detail.get("core_got", 0.0) / detail["core_want"]
                if detail.get("core_want") else None
            ),
            "hedge_fill_rate": (
                detail.get("hedge_got", 0.0) / detail["hedge_want"]
                if detail.get("hedge_want") else None
            ),
            # Only meaningful for the top-up stages; None on P0, which has
            # no earlier stage to carry anything from.
            "carried_fill_rate": (
                detail.get("carried_got", 0.0) / detail["carried_want"]
                if detail.get("carried_want") else None
            ),
            "fresh_fill_rate": (
                detail.get("fresh_got", 0.0) / detail["fresh_want"]
                if detail.get("fresh_want") else None
            ),
            "stake_fill_rate": (
                detail.get("stake", 0.0) / detail["intended_stake"]
                if detail.get("intended_stake") else None
            ),
            # Positive means the filled shares cost less than crossing at
            # the entry ask would have -- resting is still being paid the
            # spread rather than paying it.
            "maker_saving_vs_taker": (
                1.0 - detail.get("stake", 0.0) / detail["taker_equivalent"]
                if detail.get("taker_equivalent") else None
            ),
            "by_posted_rungs": [
                {
                    "posted_rungs": depth,
                    "leg_count": slot["legs"],
                    "mean_filled_rungs": slot["filled"] / slot["legs"],
                }
                for depth, slot in sorted(
                    (detail.get("by_posted_rungs") or {}).items()
                )
            ],
        })
    return {"stages": out}


REFUSAL_LABELS = {
    "no_book_at_decision": (
        "决策时没有盘口",
        "那一刻这个站日还没有已提交的市场快照，价格无从谈起。",
    ),
    "incomplete_book": (
        "盘口不完整",
        "有桶没有报价，篮子的赔付几何算不出来。",
    ),
    "selection_failed": (
        "选桶本身没跑出来",
        "内核没给出可用的支持窗口，跟市场无关。",
    ),
    # 这四条是「枚举完没坐上」拆出来的，四个不同的问题、四个不同的责任人。
    # 2026-08-20 之前前三种全被并进 no_priced_window，配的说明还写着"市场比
    # 模型贵"；那天的 P0 账本实测，316 行里 316 行都带着结构性拒绝计数，没有
    # 一行是市场真的开过价的。原来那段合并说明里的比例，现在各归各位。
    # 每一行记的都是当天走到最深的那一轮问价，不是收盘。
    "no_legal_window": (
        "没有合法窗口",
        "结构闸拿走了整批枚举：断桶、不含中心桶、或长度不够。"
        "跟价格无关——这些窗口没走到看报价那一步。"
        "约四成的「没坐上」是这一类：篮子要从中心桶起连续取满 3 格，"
        "而中心桶或它的紧邻没有像样的报价（中心桶自己低于 2 分的占 34%）。",
    ),
    "no_priced_window": (
        "窗口定不出价",
        "有窗口过了全部结构闸，但里面有腿根本没人挂卖，篮子的成本算不出来。"
        "缺的是盘口，不是便宜（detail 会点名是哪几条腿）。",
    ),
    "negative_edge_window": (
        "边际不敌摩擦",
        "窗口过了全部闸也报得出价，模型胜率减去全包成本仍盖不住逐腿的最小跳动。"
        "这一条才是「市场比模型贵」：约六成的「没坐上」凑得出合法篮子，"
        "但最好的一篮边际中位数也有 −0.25，扣掉手续费更不值。",
    ),
    "leg_below_floor": (
        "有腿报价低于下限",
        "我们自己的门槛：低于两分的报价基本是单边挂的尾部盘，不当价格用。",
    ),
    "resting_unfilled": (
        "挂单一档都没成交",
        "价格没等到。规模是够的，市场没走到我们这里来。单子不是挂到结算——"
        "每 60 分钟按当时盘口重挂，但过了本站达峰时刻就收摊（中位 19:20 本地，"
        "最早 16:53），detail 里写明这一单实际挂到几点。",
    ),
    "belief_withdrew_order": (
        "信念转负，自己撤单",
        "不是市场没来，是我们自己走的：每轮重挂会拿最新概率重算整篮期望，"
        "转负就把还挂着的档全撤且不再挂。这是策略在保护自己，"
        "该查的是模型为什么改主意，不是执行。",
    ),
    "no_positive_kelly": (
        "目标函数看过，不下注",
        "篮子凑出来了、价也定了，赢面还是低于要价，买下去会亏对数财富。"
        "这是策略在工作，不是故障。detail 里的成本是本车道自己的价"
        "（挂单模式下是挂出去的价，括号里的吃单价是上界）。",
    ),
    "certainty_gate_lit": (
        "市场已定价完毕，主动等下一轮",
        "中心桶连着三次决策没动过，或者头两个桶已经占了六成概率——"
        "两者任一亮起，就说明我们的证据和市场的证据已经收敛，"
        "而收敛的那份溢价市场早就收过了。这一轮什么都不做，"
        "走到下一个决策时刻重新看。注意它跟「赔率不划算」是相反的两件事："
        "那一条是目标函数算过嫌贵，这一条是目标函数压根没被问到。"
        "2026-08-24 之前这两件事共用一个代号，"
        "看板把每一次主动等待都写成了「赔率不划算」。",
    ),
    "unquoted_ideal_window": (
        "理想窗口没人报价",
        "中心桶及其左右各一档里，能凑够八成概率质量的桶都没有像样的卖价。"
        "市场不在这几个桶上认真挂单，往往不是疏忽，是它已经判定这些桶不会发生。"
        "这时候看着便宜的篮子，买的是别人不要的东西。默认关闭，"
        "由前向验证攒够样本后自动启用。",
    ),
    "cost_exceeds_payout": (
        "整篮报价高过满额赔付",
        "这一篮买下来比它赢了能赔的 1.00 还贵，多高的概率都救不回来，"
        "所以模型压根没被问到。每条腿都是真有人挂卖的实价，"
        "只是几条腿加起来的抽水超过了满额赔付——是书的问题，不是策略在挑。"
        "（腿上没人挂卖的那种不进这一行，算在「窗口定不出价」里。）"
        "detail 里的成本是「本车道自己的价」：挂单模式下是这几条腿要挂的价"
        "合计，不是吃单价——2026-08-26 之前这里印的一直是吃单价，"
        "KHOU 2026-08-27 因此被写成 1.100，而这一篮实际会挂在 0.876。"
        "吃单价现在跟在后面括号里，作为上界留着。",
    ),
    "no_depth": (
        "盘口深度不够",
        "价格可以，但那一档挂的量撑不起要买的规模。",
    ),
    "chain_never_opened": (
        "P0 没建仓，本阶段无仓可加",
        "一个站日只由 P0 开仓，后面两阶段都是往这一仓上加。P0 没坐下，"
        "这里就没有仓可加——不是这一阶段拒绝了什么，是它根本没被叫到。"
        "要看真正的原因，去上面 P0 那张表。",
    ),
    "no_top_up_needed": (
        "想加的已经在仓里",
        "这一阶段选中的梯级，P0 早就买下了。一本账下后续阶段是加仓，"
        "目标已在仓内就是按兵不动，不是拒绝交易。",
    ),
}


STAGE_REQUIRING_TAF = "P0-M"


def _taf_temperature_stations(conn: sqlite3.Connection) -> set[str]:
    """Stations whose TAF actually states a maximum temperature.

    P0-M is the TAF confirmation stage, so a station whose TAF carries no
    temperature group can never reach it.  Counting those station-days in the
    denominator scores the stage against work it was never able to do: 17 of
    the 45 carry a TX group and the US stations carry none, so the other 28
    were dragging the seat rate down with days that had no TAF to confirm.

    An empty set means the capture tables are not there to ask, in which case
    the filter is skipped rather than silently emptying the section.
    """

    stations: set[str] = set()
    for table, predicate in (
        ("calibration_forward_taf_capture", " WHERE tx_available=1"),
        ("calibration_historical_taf_capture", ""),
    ):
        try:
            rows = conn.execute(
                "SELECT DISTINCT station_id FROM %s%s" % (table, predicate)
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        stations |= {
            str(row[0]).strip().upper()
            for row in rows
            if str(row[0] or "").strip()
        }
    return stations


def _refusal_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Why a station-day ended with no position, counted per stage.

    Not pooled across stages, for the same reason the fill rate is not: the
    three stages meet three different books, and a reason that dominates one
    can be absent from another.

    The denominator is every scope the sweep reached -- seats plus refusals
    -- so the two together are a funnel rather than two unrelated tallies.
    An absent row used to be the only signal here, and it could say nothing
    beyond "no seat", which is true of one of these reasons and wrong about
    every other.

    P0-M is counted only over stations whose TAF states a temperature.  The
    stage confirms a TAF, so a station that never gets one cannot reach it,
    and 28 of the 45 were sitting in its denominator as days it had no way to
    serve.

    What each row reports is the deepest refusal that station-day reached,
    not its last -- the ledger keeps depth rather than recency, because the
    final cycle is the converged end-of-day book.
    """

    taf_stations = _taf_temperature_stations(conn)

    def in_scope(stage: str, station_id: str) -> bool:
        if stage != STAGE_REQUIRING_TAF or not taf_stations:
            return True
        return station_id in taf_stations

    try:
        rows = conn.execute(
            "SELECT stage, reason, station_id, detail FROM paper_trade_refusal"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"stages": []}
    tally: dict[tuple[str, str], list[Any]] = {}
    for stage, reason, station_id, detail in rows:
        if not in_scope(str(stage), str(station_id or "").strip().upper()):
            continue
        slot = tally.setdefault((str(stage), str(reason)), [0, ""])
        slot[0] += 1
        # Same example the grouped ``MAX(detail)`` used to pick.
        slot[1] = max(slot[1], str(detail or ""))
    seated: dict[str, int] = {}
    for stage, station_id in conn.execute(
        "SELECT stage, station_id FROM paper_trade_position"
    ):
        if not in_scope(str(stage), str(station_id or "").strip().upper()):
            continue
        seated[str(stage)] = seated.get(str(stage), 0) + 1
    by_stage: dict[str, list[dict[str, Any]]] = {}
    for (stage, reason), (count, detail) in tally.items():
        by_stage.setdefault(stage, []).append({
            "reason": reason,
            "label": REFUSAL_LABELS.get(reason, (reason, ""))[0],
            "note": REFUSAL_LABELS.get(reason, ("", ""))[1],
            "count": int(count),
            "example": str(detail or ""),
        })
    out = []
    for stage in ("P0", "P0-M", "P1"):
        reasons = sorted(
            by_stage.get(stage, []), key=lambda row: -row["count"]
        )
        refused = sum(row["count"] for row in reasons)
        scopes = refused + seated.get(stage, 0)
        if not scopes:
            continue
        for row in reasons:
            row["share"] = row["count"] / scopes
        out.append({
            "stage": stage,
            "seated_count": seated.get(stage, 0),
            "refused_count": refused,
            "scope_count": scopes,
            "seat_rate": seated.get(stage, 0) / scopes,
            "reasons": reasons,
            "scope_restricted_to": (
                "有 TAF 温度组的 %d 站" % len(taf_stations)
                if stage == STAGE_REQUIRING_TAF and taf_stations
                else ""
            ),
        })
    return {"stages": out}


def _maker_probe(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the resting-order probe has learned so far.

    It places nothing -- each row is an intent stamped before the market
    moved, decided later by whether the live book traded through it.  So
    "fill rate" here is per order, not per share: the probe carries no size,
    which is why there is no filled-quantity ratio to report.  Saying that
    plainly beats printing a number that would have to be invented.
    """

    try:
        conn.execute("SELECT 1 FROM maker_shadow_order LIMIT 1")
    except sqlite3.OperationalError:
        return {"available": False}
    cells: list[dict[str, Any]] = []
    band = (
        "CASE WHEN lead_hours >= 24 THEN 'open_gt24h'"
        " WHEN lead_hours >= 12 THEN 'open_12_24h'"
        " WHEN lead_hours >= 0 THEN 'pre_event_0_12h'"
        " ELSE 'event_day' END"
    )
    for rule, horizon, lead, total, filled, price, ask in conn.execute(
        f"SELECT price_rule, horizon_minutes, {band}, COUNT(*),"
        " SUM(CASE WHEN state='FILLED' THEN 1 ELSE 0 END),"
        " AVG(limit_price), AVG(reference_ask)"
        " FROM maker_shadow_order WHERE state<>'OPEN'"
        f" GROUP BY price_rule, horizon_minutes, {band}"
    ):
        decided = int(total or 0)
        if decided < 20:
            continue
        cells.append({
            "price_rule": str(rule),
            "horizon_minutes": int(horizon),
            "lead_band": str(lead),
            "decided_count": decided,
            "filled_count": int(filled or 0),
            "fill_rate": (filled or 0) / decided,
            # An order that never traded is a bucket the basket wanted and
            # did not get -- the "empty rung" the ruling asks about.
            "unfilled_rate": 1.0 - (filled or 0) / decided,
            "limit_price_mean": float(price or 0.0),
            "reference_ask_mean": float(ask or 0.0),
            "spread_saved_mean": float(ask or 0.0) - float(price or 0.0),
        })
    open_count = conn.execute(
        "SELECT COUNT(*) FROM maker_shadow_order WHERE state='OPEN'"
    ).fetchone()[0]
    settled = conn.execute(
        "SELECT COUNT(*), SUM(hit), AVG(limit_price), AVG(pnl_per_share)"
        " FROM maker_shadow_settlement"
    ).fetchone()
    return {
        "available": True,
        "open_count": int(open_count or 0),
        "cells": sorted(
            cells,
            key=lambda c: (
                c["lead_band"], c["horizon_minutes"], c["price_rule"]
            ),
        ),
        "settled": {
            "count": int(settled[0] or 0),
            "hit_count": int(settled[1] or 0),
            "hit_rate": (
                (settled[1] or 0) / settled[0] if settled[0] else None
            ),
            "limit_price_mean": settled[2],
            "pnl_per_share_mean": settled[3],
        },
    }


def build_payload(db: Path) -> dict[str, Any]:
    # The connection lives exactly one payload and is closed on every path.
    # Leaking it kept a read snapshot open on a WAL database and starved
    # checkpointing -- see the sibling note in
    # calibration_dashboard.build_payload.
    conn = _connect(db)
    try:
        return _build_payload(conn, db)
    finally:
        conn.close()


def _build_payload(conn: sqlite3.Connection, db: Path) -> dict[str, Any]:
    rows = _rows(conn)
    by_side: dict[str, int] = defaultdict(int)
    for row in rows:
        by_side[row["side"]] += 1

    def _slice(subset: list[dict[str, Any]]) -> dict[str, Any]:
        # One row per chain: a settled position is counted where it
        # settled, not once per stage that topped it up.
        settled = [r for r in subset if r["settled"] and r["chain_tail"]]
        if not settled:
            return {"settled_count": 0, "position_count": len(subset)}
        hits = sum(int(r["hit"]) for r in settled)
        staked = sum(r["chain_stake_usdc"] for r in settled)
        pnl = sum(float(r["pnl_usdc"] or 0.0) for r in settled)
        claimed = sum(r["win_probability"] for r in settled) / len(settled)
        odds = [r["odds"] for r in settled if r["odds"] is not None]
        return {
            "position_count": len(subset),
            "settled_count": len(settled),
            "hit_count": hits,
            "hit_rate": hits / len(settled),
            "claimed_win_probability_mean": claimed,
            "calibration_ratio": (
                (hits / len(settled)) / claimed if claimed > 0 else None
            ),
            "odds_mean": sum(odds) / len(odds) if odds else None,
            "staked_usdc": staked,
            "pnl_usdc": pnl,
            "return_on_stake": pnl / staked if staked > 0 else None,
        }

    stages: dict[str, Any] = {}
    for stage in ("P0", "P0-M", "P1"):
        subset = [r for r in rows if r["stage"] == stage]
        credible = [r for r in subset if not r["dust_offer"]]
        dust = [r for r in subset if r["dust_offer"]]
        stages[stage] = {
            "all": _slice(subset),
            "credible": _slice(credible),
            "dust_offer": _slice(dust),
            "sides": {
                side: sum(1 for r in subset if r["side"] == side)
                for side in ("above", "below", "aligned", "unknown")
            },
        }
    # Quotes that are not really prices, listed so they can be chased rather
    # than quietly carried in the headline.
    anomalies = [
        {
            "station_id": r["station_id"],
            "event_date": r["event_date"],
            "stage": r["stage"],
            "window": r["window"],
            "all_in_cost": r["all_in_cost"],
            "win_probability": r["win_probability"],
            "kind": (
                "basket_under_one_cent"
                if r["dust_offer"]
                else "claim_over_market_by_10x"
            ),
        }
        for r in rows
        if r["dust_offer"]
        or (r["all_in_cost"] > 0 and r["win_probability"] / r["all_in_cost"] > 10.0)
    ]
    # Every registered station on every event day it has evidence for, not
    # just the latest.  A station missing from the ledger on a day is the
    # more interesting row -- the objective looked and did not sit, or never
    # got a priced book -- so absence is rendered, not skipped.
    registered = [
        str(row[0])
        for row in conn.execute(
            "SELECT station_id FROM calibration_station_registry"
            " ORDER BY station_id"
        )
    ]
    # The unit its market settles in.  Buckets are named in it ("96-97°F")
    # while every truth on the page is Celsius, so a row that shows both
    # without saying which is which reads as a 60-degree error.  One value
    # per station -- checked: no station's markets disagree.
    settlement_units = {
        str(station): str(unit or "C")
        for station, unit in conn.execute(
            "SELECT station_id, settlement_unit"
            " FROM basket_target_market_registry"
            " GROUP BY station_id"
        )
    }
    seated = {
        (r["station_id"], r["event_date"], r["stage"]): r for r in rows
    }
    taf_tx: dict[tuple[str, str], float] = {}
    for station, day, value in conn.execute(
        "SELECT station_id, event_date, tx_c"
        " FROM calibration_forward_taf_capture"
        " WHERE event_date >= ? AND tx_available=1 AND tx_c IS NOT NULL"
        " GROUP BY station_id, event_date HAVING MAX(issued_at_utc)",
        (STATS_START_EVENT_DATE,),
    ):
        taf_tx[(str(station), str(day))] = float(value)
    # Why the ledger did not seat, in its own words rather than the page's
    # guess.  Absence used to be read off two existence checks, so every scope
    # with a probability and a book was reported as "the objective looked and
    # did not sit" -- true of one refusal and wrong about the four others,
    # including the thirteen 2026-08-17 stations whose every admissible window
    # priced above the model and the three whose book never reached the table.
    refusals: dict[tuple[str, str, str], dict[str, Any]] = {}
    try:
        for station, day, stage, reason, detail, cycles in conn.execute(
            "SELECT station_id, event_date, stage, reason, detail,"
            " cycles_examined FROM paper_trade_refusal WHERE event_date >= ?",
            (STATS_START_EVENT_DATE,),
        ):
            refusals[(str(station), str(day), str(stage))] = {
                "reason": str(reason),
                "detail": str(detail or ""),
                "cycles_examined": int(cycles or 0),
            }
    except sqlite3.DatabaseError:
        # The table arrives with the ledger's next sweep; until then the page
        # falls back to the existence checks below.
        refusals = {}
    from bayes_basket_target.probability_bridge import (  # noqa: PLC0415
        history_envelope_payload,
    )

    # P1 re-solves as observations arrive, so its rung moves during the day.
    p1_history: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for station, day, decided, stored, checkpoint, sha in conn.execute(
        "SELECT station_id, event_date, decision_at_utc, envelope_json,"
        " checkpoint_id, envelope_content_sha256"
        " FROM basket_probability_bridge_history"
        " WHERE stage='P1' AND event_date >= ?"
        " ORDER BY station_id, event_date, decision_at_utc",
        (STATS_START_EVENT_DATE,),
    ):
        lineage = history_envelope_payload(
            conn,
            checkpoint_id=str(checkpoint),
            envelope_content_sha256=str(sha),
            stored=str(stored) if stored else None,
        ).get("lineage") or {}
        try:
            trajectory_strength = float(
                lineage.get("metar_trajectory_applied_strength")
            )
        except (TypeError, ValueError):
            trajectory_strength = None
        p1_history[(str(station), str(day))].append({
            "decision_at_utc": str(decided),
            "centre_bucket": lineage.get("model_center_bucket"),
            "centre_c": lineage.get("model_center_c"),
            # Which conditioner produced this recompute, and at what applied
            # strength -- the page labels a physical-path prediction as such
            # rather than presenting every P1 centre as the same authority.
            "likelihood_mode": lineage.get("observation_likelihood_mode"),
            "trajectory_strength": trajectory_strength,
        })
    # An event day is any day the fleet has evidence for; on such a day every
    # registered station gets a row, whether or not it has evidence of its
    # own.  Keying the roster off the stations with evidence made the two
    # emptiest cases -- no probability computed, no book ever captured --
    # vanish from the page instead of reading as the gap they are: 2026-08-17
    # rendered 43 stations under a heading that promised all 45.  Pairs with
    # evidence are unioned in, so a station absent from the registry still
    # shows up rather than being silently dropped.
    evidence_days = (
        {(r["station_id"], r["event_date"]) for r in rows}
        | {(station, day) for station, day, _ in refusals}
        | {
            (str(station), str(day)) for station, day in conn.execute(
                "SELECT DISTINCT station_id, event_date FROM"
                " basket_probability_bridge_history WHERE event_date >= ?",
                (STATS_START_EVENT_DATE,),
            )
        }
    )
    scope_days: set[tuple[str, str]] = evidence_days | {
        (station, day)
        for day in {d for _, d in evidence_days}
        for station in registered
    }
    roster = []
    for station, day in sorted(
        scope_days, key=lambda kv: (kv[1], kv[0]), reverse=True
    ):
        entry: dict[str, Any] = {
            "station_id": station,
            "station_label": STATION_LABELS.get(station, ""),
            "settlement_unit": settlement_units.get(station, "C"),
            "event_date": day,
            "taf_tx_c": taf_tx.get((station, day)),
            "p1_history": p1_history.get((station, day), [])[-12:],
            "stages": {},
        }
        entry["refusals"] = {
            stage: refusals.get((station, day, stage))
            for stage in ("P0", "P0-M", "P1")
        }
        for stage in ("P0", "P0-M", "P1"):
            row = seated.get((station, day, stage))
            entry["stages"][stage] = (
                None
                if row is None
                else {
                    "window": row["window"],
                    "window_size": row["window_size"],
                    "selection_rule_label": row.get(
                        "selection_rule_label", ""
                    ),
                    "centre_bucket": row["centre_bucket"],
                    "win_probability": row["win_probability"],
                    "all_in_cost": row["all_in_cost"],
                    # Charter 54/55/58 read directly off the cell: the mean
                    # centre is the rung, the skew sign is the gate's own
                    # reading, the edge is the whole-basket EV the price
                    # gate admitted on, and the ask total is what crossing
                    # the book would have paid for the same window.
                    "edge": row["edge"],
                    "skew_sign": row["skew_sign"],
                    "ask_total": row["seat_top_of_book_total"],
                    "odds": row["odds"],
                    "side": row["side"],
                    "lean_buckets": row["lean_buckets"],
                    "stake_usdc": row["stake_usdc"],
                    "hit": row["hit"],
                    "pnl_usdc": row["pnl_usdc"],
                    "dust_offer": row["dust_offer"],
                    "settled_tmax_c": row["settled_tmax_c"],
                    # The chain's own numbers, carried on every stage of it
                    # so a cell can say what the whole position cost, and
                    # flagged on the one stage that settled it so nothing
                    # sums the same outcome twice.
                    "chain_tail": row["chain_tail"],
                    "chain_stake_usdc": row["chain_stake_usdc"],
                    "chain_all_in_cost": row["chain_all_in_cost"],
                    # The order as posted beside the position that came
                    # back.  It belongs in the cell the seat is drawn in:
                    # read apart from the window it fills, "posted three,
                    # bought one" is a fact about a different table than the
                    # one showing the single bucket.
                    "entry": row["entry"],
                }
            )
        entry["seated_stage_count"] = sum(
            1 for v in entry["stages"].values() if v is not None
        )
        settled = [
            v for v in entry["stages"].values()
            if v is not None and v.get("hit") is not None
        ]
        entry["settled_tmax_c"] = next(
            (v["settled_tmax_c"] for v in settled), None
        )
        # The station-day realised one number, whatever the chain did on the
        # way there.  Summing the stage cells would report a day that opened
        # at P0 and was topped up twice as three separate results.
        tail = next((v for v in settled if v.get("chain_tail")), None)
        entry["realized_usdc"] = (
            None if tail is None else float(tail["pnl_usdc"] or 0.0)
        )
        entry["chain_stake_usdc"] = (
            None if tail is None else float(tail["chain_stake_usdc"] or 0.0)
        )
        if entry["seated_stage_count"] == 0:
            recorded = entry["refusals"].get("P0") or next(
                (v for v in entry["refusals"].values() if v), None
            )
            if recorded is not None:
                entry["absent_reason"] = recorded["reason"]
                entry["absent_detail"] = recorded["detail"]
            else:
                has_probability = conn.execute(
                    "SELECT 1 FROM basket_probability_bridge_history"
                    " WHERE station_id=? AND event_date=? LIMIT 1",
                    (station, day),
                ).fetchone() is not None
                has_book = conn.execute(
                    "SELECT 1 FROM basket_official_ws_snapshot"
                    " WHERE station_id=? AND event_date=? LIMIT 1",
                    (station, day),
                ).fetchone() is not None
                entry["absent_reason"] = (
                    "no_probability" if not has_probability
                    else ("no_market_book" if not has_book
                          else "no_seat_taken")
                )
        roster.append(entry)

    # Aggregates over credible fills only; a sub-one-cent basket has an EV
    # per USDC in the hundreds and would set every average by itself.
    credible_rows = [r for r in rows if not r["dust_offer"]]

    def _mean(values: list[float]) -> float | None:
        clean = [v for v in values if v is not None]
        return sum(clean) / len(clean) if clean else None

    settled_credible = [
        r for r in credible_rows if r["settled"] and r["chain_tail"]
    ]
    expected_usdc = sum(
        float(r["ev_usdc"] or 0.0) for r in settled_credible
    )
    realized_usdc = sum(
        float(r["pnl_usdc"] or 0.0) for r in settled_credible
    )
    coverage_stats = {
        "position_count": len(credible_rows),
        "model_coverage_mean": _mean(
            [r["win_probability"] for r in credible_rows]
        ),
        "window_size_mean": _mean(
            [float(r["window_size"]) for r in credible_rows]
        ),
        # The all-in ask already carries the fee, so the basket's ask total
        # and its cost are the same number under two names.  Report it once.
        "seat_ask_total_mean": _mean(
            [r["seat_ask_total"] for r in credible_rows]
        ),
        "ev_per_usdc_mean": _mean([r["ev_per_usdc"] for r in credible_rows]),
        "staked_usdc": sum(
            r["chain_stake_usdc"] for r in settled_credible
        ),
        "expected_usdc": expected_usdc,
        "realized_usdc": realized_usdc,
        # The gap between what the model said the book was worth and what it
        # returned.  A persistent shortfall is the calibration ratio showing
        # up in money rather than in hit rate.
        "realization_ratio": (
            realized_usdc / expected_usdc
            if abs(expected_usdc) > 1e-9 else None
        ),
        "settled_count": len(settled_credible),
    }

    # The same three statistics per station.  A fleet average hides the
    # thing worth acting on: stations do not share a book, a climate or a
    # bucket width, so a coverage gap that is real at one and an artifact at
    # another average into a number that describes neither.
    per_station: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in credible_rows:
        per_station[row["station_id"]].append(row)
    station_stats = []
    for station in sorted(per_station):
        subset = per_station[station]
        settled = [
            r for r in subset if r["settled"] and r["chain_tail"]
        ]
        hits = sum(int(r["hit"]) for r in settled)
        claimed = _mean([r["win_probability"] for r in settled])
        expected = sum(float(r["ev_usdc"] or 0.0) for r in settled)
        realized = sum(float(r["pnl_usdc"] or 0.0) for r in settled)
        station_stats.append({
            "station_id": station,
            "station_label": STATION_LABELS.get(station, ""),
            "position_count": len(subset),
            "settled_count": len(settled),
            "window_size_mean": _mean(
                [float(r["window_size"]) for r in subset]
            ),
            # Rungs posted against rungs bought.  The window mean above is
            # the filled figure alone, and read on its own it says a station
            # chose narrow baskets when what happened is the book only sold
            # it part of a wide one.
            "posted_leg_total": sum(
                int(r["entry"]["posted_leg_count"]) for r in subset
            ),
            "filled_leg_total": sum(
                int(r["entry"]["filled_leg_count"]) for r in subset
            ),
            "posted_leg_mean": _mean(
                [float(r["entry"]["posted_leg_count"]) for r in subset]
            ),
            "filled_leg_mean": _mean(
                [float(r["entry"]["filled_leg_count"]) for r in subset]
            ),
            "model_coverage_mean": _mean(
                [r["win_probability"] for r in subset]
            ),
            "seat_ask_total_mean": _mean(
                [r["seat_ask_total"] for r in subset]
            ),
            "ev_per_usdc_mean": _mean([r["ev_per_usdc"] for r in subset]),
            "odds_mean": _mean([r["odds"] for r in subset]),
            "sides": {
                side: sum(1 for r in subset if r["side"] == side)
                for side in ("above", "aligned", "below")
            },
            "hit_count": hits,
            "hit_rate": hits / len(settled) if settled else None,
            "claimed_win_probability_mean": claimed,
            "calibration_ratio": (
                (hits / len(settled)) / claimed
                if settled and claimed else None
            ),
            "staked_usdc": sum(
                r["chain_stake_usdc"] for r in settled
            ),
            "expected_usdc": expected,
            "realized_usdc": realized,
            "realization_ratio": (
                realized / expected if abs(expected) > 1e-9 else None
            ),
        })
    # ``station_stats`` is deliberately not attached to the roster rows.  A
    # roster row is a station-day and these averages span the whole window,
    # so carrying them there put a number on every date that belonged to
    # none of them -- ZUUU's 08-16 fill was reported as its 08-17 ask total
    # on a day with no book.  The page reads the two collections apart.
    days = sorted({r["event_date"] for r in rows}, reverse=True)
    return {
        "schema": "selection_market_dashboard_v1",
        # Stated once so the page can flag anything compiled under a
        # different rule instead of repeating the same string on every cell.
        "current_selection_rule_label": SELECTION_RULE_LABEL,
        "basket_hit_by_size": _basket_hit_by_size(conn),
        "chosen_basket_hit": _posted_basket_hit_trend(conn),
        "chosen_miss_offsets": _chosen_miss_offsets(conn),
        "consolidation_lights": _consolidation_lights(conn),
        "maker_probe": _maker_probe(conn),
        "resting_entry": _resting_entry_stats(conn),
        "resting_entry_trend": _resting_entry_trend(conn),
        "refusal_stats": _refusal_stats(conn),
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        "initial_capital_usdc": INITIAL_CAPITAL_USDC,
        "position_count": len(rows),
        "day_count": len(days),
        "latest_day": days[0] if days else None,
        "sides": dict(by_side),
        "stages": stages,
        "anomalies": anomalies[:40],
        "coverage_stats": coverage_stats,
        "station_stats": station_stats,
        "station_roster": roster,
        "roster_event_dates": sorted({e["event_date"] for e in roster}, reverse=True),
        "stats_start_event_date": STATS_START_EVENT_DATE,
        "rows": rows[:400],
    }


# This module renders nothing.  There is exactly one frontend (user ruling,
# 2026-08-15): every section lives in scripts/calibration_dashboard.py, which
# imports build_payload from here.  A second page meant reading one thing in
# two places, so the serving and templating that used to live here are gone
# rather than merely unused.


def main() -> int:
    """Print the payload.  Rendering belongs to the one dashboard."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--json", action="store_true", default=True)
    args = parser.parse_args()
    print(json.dumps(
        build_payload(args.db.expanduser().resolve(strict=True)),
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
