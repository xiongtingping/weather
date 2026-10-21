#!/usr/bin/env python3
"""Station labels and the chosen-basket readings the live board prints.

The page that used to live here -- every station's daily seat read against
the market off the paper ledger -- retired with that ledger (2026-09-25; the
board has read live money since the 2026-09-16 ruling).  What stays is what
the live readers import: the readable station names, the odds a mean ticket
price implies, and the two chosen-basket readings ``live_selection`` feeds
with settled live chains.
"""

from __future__ import annotations

import math
import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.delta_v2_runtime import (  # noqa: E402
    CHAIN_MAX_BUCKETS,
    SEAT_MAX_BUCKETS,
    SEAT_MIN_BUCKETS,
)

# How many settled chains a width needs before its hit rate gets a colour.
# Under that the rate is one or two days wearing a percentage: on
# 2026-09-12 the four-bucket row was a single chain that missed, and the
# funnel painted it a red 0% beside a three-bucket row built on 36.  Same
# number governs the trend halves below -- one threshold, not two.
BASKET_HIT_MIN_CHAINS = 8
# The registry's city_label is empty for every station, so the readable name
# lives here.  The ICAO code stays the key everywhere -- it is the stable
# identifier -- and the label is only ever for the page.
STATION_LABELS = {
    "CYYZ": "多伦多", "EDDM": "慕尼黑", "EFHK": "赫尔辛基",
    "EGLC": "伦敦", "EHAM": "阿姆斯特丹", "EPWA": "华沙",
    "FACT": "开普敦", "KATL": "亚特兰大", "KAUS": "奥斯汀",
    "KBKF": "丹佛", "KDEN": "丹佛", "KDAL": "达拉斯", "KHOU": "休斯敦",
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


def _odds_from_mean_cost(costs: Sequence[float | None]) -> float | None:
    """Net odds implied by an average ticket price, never the mean of odds.

    ``(1-c)/c`` is convex: the mean of it is set by whichever basket was
    cheapest, not by the money at stake.  Measured over this book on
    2026-09-10 the per-row mean was 14.7x while the money actually bought
    1.53x, and a single day's row printed 32.8x.  Average the price, then
    invert once.
    """

    priced = [float(c) for c in costs if c is not None and float(c) > 0]
    if not priced:
        return None
    mean = sum(priced) / len(priced)
    return (1.0 - mean) / mean if mean > 0 else None


def chosen_basket_hit_trend(
    chains: Sequence[Mapping[str, Any]], sizes: Sequence[int] | None = None
) -> dict[str, Any]:
    """Hit rate of the basket the objective *chose*, per chosen width.

    ``chains`` is one record per settled station-day -- ``chosen`` and
    ``bought`` bucket sets, the ``landed`` bucket, the ``claimed`` mass and
    the ``event_date`` -- from ``live_selection.settled_chains``, the live
    fills the board reads since 2026-09-16.

    Grouping on the width that came back after the market sold us part of
    the ladder counts a three-rung choice the book cut to one as a one-rung
    basket, and the three-rung row silently loses its hardest cases.  The
    question "is a three-bucket choice any good" is about the objective's
    own pick, so the base is the chain-level union of chosen legs.

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

    The widths reported are the runtime's own, never a list restated here:
    ``SEAT_MIN_BUCKETS`` up to ``CHAIN_MAX_BUCKETS``, so a charter that
    moves the seat moves this table with it.  Charter 131 seats 2-3 and
    lets a chain's union reach 4 when P0-M or P1 adds a rung, which is why
    the range runs past the seat maximum rather than stopping at it.  The
    hardcoded ``(3, 4)`` this replaced was clause 50's retired 3-4 seat: it
    had no row for the two-bucket skew-day seat that became legal on
    2026-09-12, and its four-bucket row was one legacy chain painted red.
    """

    wanted = {
        int(size) for size in (
            range(SEAT_MIN_BUCKETS, CHAIN_MAX_BUCKETS + 1)
            if sizes is None else sizes
        )
    }
    by_size: dict[int, list[dict[str, Any]]] = {size: [] for size in wanted}
    for chain in chains:
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
        movable = (
            earlier is not None
            and later is not None
            and earlier["settled_count"] >= BASKET_HIT_MIN_CHAINS
            and later["settled_count"] >= BASKET_HIT_MIN_CHAINS
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
            # Whether the rate itself carries a reading, before any
            # question of trend.  A width the book has only tried once
            # still gets its row -- a missing row answers a different
            # question than a thin one -- but it does not get a verdict.
            "rate_is_readable": (
                overall["settled_count"] >= BASKET_HIT_MIN_CHAINS
            ),
            "min_chains": BASKET_HIT_MIN_CHAINS,
            "seat_legal": SEAT_MIN_BUCKETS <= size <= SEAT_MAX_BUCKETS,
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


def chosen_miss_offsets(chains: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Where the truth lands when it misses the chosen basket, in buckets.

    ``chains`` as for ``chosen_basket_hit_trend``.

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
    for chain in chains:
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
