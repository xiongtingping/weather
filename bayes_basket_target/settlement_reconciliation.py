"""Charter 207: our settled bucket against the bucket the market paid.

The truth lane settles each station-day on the AWC METAR/SPECI maximum and
places it on the day's ladder with the settler's own rule
(``candidate_window_evidence.standing_labels``).  Polymarket settles the same
day on the page its rules name (charter 205) and pays one bucket.  Nothing
compared the two until 2026-09-23, when a first pass over 08-15..09-22 found
26 of 1,754 station-days apart -- ten of them because our own evidence had a
four-to-five-hour afternoon hole the truth lane accepted.

This module only reads and classifies.  A disagreement is graded by where
its cause most plausibly lies, from evidence either side already holds:

* ``OUR_EVIDENCE_GAP`` -- our standing evidence has a hole of more than 90
  minutes inside the local 10:00-19:00 window.  The maximum we settled on is
  the maximum of the wrong part of the day; ours is the side to fix.
* ``MARKET_LOWEST_BRACKET`` -- the market paid its lowest bracket while ours
  is higher.  That is the rules' "no data" outcome (or a resolver error),
  not a temperature.
* ``MARKET_BELOW`` / ``MARKET_ABOVE`` -- both sides complete by our reading
  and still apart.  Below is what a page missing hours produces (NOAA had no
  ZBAA reports 11:00-15:30 local on 2026-09-20); above is what a page
  counting readings we do not (sub-hourly rows, a different station feed)
  produces.

The physical maximum (charter 1's ``X``) and the market's settlement (its
``S``) are different objects; a disagreement is not by itself an error on
either side, and nothing here rewrites a truth or a label.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from weather_basket_yes_runtime import normalize_market_buckets


SCHEMA = "settlement_truth_reconciliation_v1"
AGREE = "AGREE"
PENDING = "PENDING"
NO_TRUTH = "NO_TRUTH"
OUR_EVIDENCE_GAP = "OUR_EVIDENCE_GAP"
MARKET_LOWEST_BRACKET = "MARKET_LOWEST_BRACKET"
MARKET_BELOW = "MARKET_BELOW"
MARKET_ABOVE = "MARKET_ABOVE"
DISAGREEMENTS = (OUR_EVIDENCE_GAP, MARKET_LOWEST_BRACKET, MARKET_BELOW, MARKET_ABOVE)
# The window the day's maximum lives in, and the longest hole a complete
# hourly record can have inside it: one missed routine report reads as a
# two-hour gap, the half-hourly and SPECI-dense stations sit far below.
WARM_WINDOW_LOCAL_HOURS = (10, 19)
MAX_WARM_WINDOW_GAP = timedelta(minutes=90)
_SLUG_DAY_RE = re.compile(r"-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$")


def event_date_of(slug: str) -> str:
    match = _SLUG_DAY_RE.search(str(slug or ""))
    if match is None:
        raise ValueError("highest_temperature_slug_date_invalid")
    return datetime.strptime(
        f"{match.group('month')} {match.group('day')} {match.group('year')}",
        "%B %d %Y",
    ).date().isoformat()


def market_resolution(event: Mapping[str, Any]) -> dict[str, Any]:
    """The bucket one event paid, read off its markets' own prices."""

    markets = [row for row in event.get("markets") or [] if isinstance(row, Mapping)]
    labels = [str(row.get("groupItemTitle") or "").strip() for row in markets]
    try:
        ladder = normalize_market_buckets([{"outcome": label} for label in labels])
    except ValueError:
        ladder = []
    order = {row["outcome"]: index for index, row in enumerate(
        sorted(ladder, key=lambda row: (
            float("-inf") if row["lower"] is None else float(row["lower"])
        ))
    )}
    winners: list[str] = []
    statuses: set[str] = set()
    for row, label in zip(markets, labels):
        statuses.add(str(row.get("umaResolutionStatus") or ""))
        try:
            prices = json.loads(row.get("outcomePrices") or "[]")
        except (TypeError, ValueError):
            prices = []
        if prices and str(prices[0]) in ("1", "1.0"):
            winners.append(label)
    lowest = min(order, key=order.get) if order else None
    if len(winners) == 1 and winners[0] in order:
        state = "RESOLVED"
    elif not winners:
        state = "PENDING"
    else:
        state = "AMBIGUOUS"
    return {
        "state": state,
        "winner": winners[0] if state == "RESOLVED" else None,
        "lowest_bracket": lowest,
        "order": order,
        "uma_statuses": sorted(status for status in statuses if status),
        "closed_time": event.get("closedTime"),
    }


def warm_window_gap(evidence: Mapping[str, Any] | None, *, event_date: str) -> dict[str, Any] | None:
    """The longest hole in our standing evidence inside the local warm window.

    ``evidence`` is the truth row's ``observation_ids_json`` as the canonical
    AWC settlement lane writes it; older label formats carry no timestamps
    and return ``None`` (unknown, not complete).
    """

    if not isinstance(evidence, Mapping):
        return None
    observations = evidence.get("observations")
    timezone_name = evidence.get("settlement_timezone")
    if not isinstance(observations, list) or not observations or not timezone_name:
        return None
    zone = ZoneInfo(str(timezone_name))
    stamps = sorted(
        datetime.fromisoformat(str(item["valid_at_utc"]).replace("Z", "+00:00")).astimezone(zone)
        for item in observations
        if isinstance(item, Mapping) and item.get("valid_at_utc")
    )
    day = datetime.fromisoformat(event_date).replace(tzinfo=zone)
    start = day + timedelta(hours=WARM_WINDOW_LOCAL_HOURS[0])
    end = day + timedelta(hours=WARM_WINDOW_LOCAL_HOURS[1])
    # The window's own edges count as the ends of a hole: no report between
    # 10:00 and 12:30 is a missing morning even if 09:50 was reported.
    points = [start] + [stamp for stamp in stamps if start < stamp < end] + [end]
    widest = max(zip(points, points[1:]), key=lambda pair: pair[1] - pair[0])
    return {
        "gap_minutes": round((widest[1] - widest[0]).total_seconds() / 60.0),
        "gap_local": f"{widest[0]:%H:%M}-{widest[1]:%H:%M}",
        "complete": widest[1] - widest[0] <= MAX_WARM_WINDOW_GAP,
    }


def classify(
    ours: Mapping[str, Any] | None,
    market: Mapping[str, Any],
    *,
    gap: Mapping[str, Any] | None,
) -> str:
    if market.get("state") != "RESOLVED":
        return PENDING
    if ours is None:
        return NO_TRUTH
    if ours.get("bucket") == market.get("winner"):
        return AGREE
    if gap is not None and not gap.get("complete"):
        return OUR_EVIDENCE_GAP
    if market.get("winner") == market.get("lowest_bracket"):
        return MARKET_LOWEST_BRACKET
    order = market.get("order") or {}
    mine, theirs = order.get(ours.get("bucket")), order.get(market.get("winner"))
    if mine is not None and theirs is not None and theirs < mine:
        return MARKET_BELOW
    return MARKET_ABOVE


def reconcile(
    *,
    events: Iterable[Mapping[str, Any]],
    ours: Mapping[tuple[str, str], Mapping[str, Any]],
    city_stations: Mapping[str, str],
    evidence_of: Any,
) -> list[dict[str, Any]]:
    """One row per fleet station-day the market has listed.

    ``ours`` maps ``(station_id, event_date)`` to the standing label
    (``bucket``, ``tmax_c``, ``revision``); ``evidence_of(station, day)``
    returns that truth's evidence document, and is called only for rows that
    disagree, so the full evidence set is never loaded.
    """

    rows: list[dict[str, Any]] = []
    for event in events:
        slug = str(event.get("slug") or "")
        city = slug.removeprefix("highest-temperature-in-").rsplit("-on-", 1)[0]
        station = city_stations.get(city)
        if station is None:
            continue
        day = event_date_of(slug)
        market = market_resolution(event)
        label = ours.get((station, day))
        gap = None
        if (
            label is not None
            and market["state"] == "RESOLVED"
            and label.get("bucket") != market["winner"]
        ):
            gap = warm_window_gap(evidence_of(station, day), event_date=day)
        verdict = classify(label, market, gap=gap)
        rows.append({
            "event_date": day,
            "city_slug": city,
            "station_id": station,
            "verdict": verdict,
            "our_bucket": label.get("bucket") if label else None,
            "our_tmax_c": label.get("tmax_c") if label else None,
            "our_revision": label.get("revision") if label else None,
            "market_bucket": market["winner"],
            "market_state": market["state"],
            "market_closed_time": market["closed_time"],
            "warm_window_gap": gap,
        })
    rows.sort(key=lambda row: (row["event_date"], row["station_id"]))
    return rows


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    decided = sum(counts.get(verdict, 0) for verdict in (AGREE, *DISAGREEMENTS))
    return {
        "counts": dict(sorted(counts.items())),
        "decided": decided,
        "agree_share": round(counts.get(AGREE, 0) / decided, 4) if decided else None,
        "disagreements": [dict(row) for row in rows if row["verdict"] in DISAGREEMENTS],
    }


__all__ = [
    "AGREE", "DISAGREEMENTS", "MARKET_ABOVE", "MARKET_BELOW", "MARKET_LOWEST_BRACKET",
    "NO_TRUTH", "OUR_EVIDENCE_GAP", "PENDING", "SCHEMA", "classify", "event_date_of",
    "market_resolution", "reconcile", "summarize", "warm_window_gap",
]
