"""Operator switches, one per strategy lane (user ruling 2026-09-18).

The ruling: put the tiebreak lane (「秤砣线」, ``tiebreak_lane``) live and
pause every other strategy -- close them, do not delete them.  Before this
file the only stops were global: the arming flag and ``live_paused.flag``
hold every send, and the runtime contract's ``live_submit_authorized`` is
minted by a migration.  Nothing could stop one lane and leave another
running, so "pause the basket, run the tiebreak" had no switch to throw.

Four lanes are known here, each the name the daemon and the submitter use
for it:

* ``basket`` -- every BUY the basket compile mints on a cycle: the seat, a
  top-up, a charter 143 completion.  Read by ``order_submission`` at the
  send, exactly where the arming flag and the station bar are read, so a
  paused lane still compiles, reserves, records its decision and writes the
  evidence the ledger fits on; only the send is held, and the intent ends
  ``failed`` with ``strategy_lane_paused:basket`` on its event.
* ``certain_win_recycle`` -- the 0.998 sell of a won position.
* ``mis_bought_leg_exit`` -- charter 144's sell-back of legs bought outside
  the completion rule.
* ``tiebreak`` -- the lane this ruling puts live.  Its settings (stake, the
  market-share threshold) live beside its switch.

Read fresh on every call, never cached: a cached answer keeps sending after
the operator has thrown the switch, which is the failure the arming flag
already closed once.  A MISSING file means the defaults -- every lane
enabled, the tiebreak lane at its measured settings -- so a test or a
rebuilt checkout runs the system as designed; a file that exists but cannot
be read is not a ruling anyone can act on, and reads as every lane paused,
because this file authorises spending and a switch nobody can read is not
permission.  ``PM_STRATEGY_LANES_CONFIG`` points a test at its own copy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "config" / "trading" / "strategy_lanes_v1.json"
ENV_VAR = "PM_STRATEGY_LANES_CONFIG"
SCHEMA = "basket_strategy_lanes_v1"

LANE_BASKET = "basket"
LANE_CERTAIN_WIN_RECYCLE = "certain_win_recycle"
LANE_MIS_BOUGHT_LEG_EXIT = "mis_bought_leg_exit"
LANE_TIEBREAK = "tiebreak"
LANE_P1_TAIL = "p1_tail"
KNOWN_LANES: tuple[str, ...] = (
    LANE_BASKET,
    LANE_CERTAIN_WIN_RECYCLE,
    LANE_MIS_BOUGHT_LEG_EXIT,
    LANE_TIEBREAK,
    LANE_P1_TAIL,
)

#: The tiebreak lane's measured settings (33 event days to 2026-09-17, 42
#: station-day-stages): the market's normalised mid on the model's mode
#: bucket at or above 0.40, one adjacent-bucket disagreement, 20 USDC a
#: trade at the ask.  See ``tiebreak_lane`` for the reading -- and note that
#: the reading was taken at the ask, while charter 163 now bids at bid+1t.
TIEBREAK_DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    # 20 USDC in the reading; 5 USDC by user ruling 2026-09-18 ("单笔金额改为5u").
    "stake_usdc": 5.0,
    # v2 (user ruling 2026-09-18, "升级到v2"): the model's mode bucket priced
    # 0.40-0.60 by the market's normalised mid, whether or not the market's
    # own mode is that bucket.  v1 (``adjacent_tie_v1``) required the two
    # modes to differ by one rung; it is the 148-station-day subset of v2.
    # v2.1 (user ruling 2026-09-18 "升级到v2.1,并记录特征"): v2, and the
    # model's bucket must be quoted both sides at most 3 cents wide.
    # v2.2 (charter 163, user ruling 2026-09-20 "秤砣线从吃单改为post only，挂
    # best买一+1"): v2.1's four conditions, posted as a resting post-only rung
    # at the best bid plus one tick instead of crossed at the walked ask.
    "rule": "model_mode_midband_spread_maker_v2_2",
    "min_market_share_on_model_mode": 0.40,
    "max_market_share_on_model_mode": 0.60,
    "max_spread_on_model_mode": 0.031,
    "first_decision_fresh_seconds": 3600.0,
    # User ruling 2026-09-18 "加重试": a fired decision refused for cash or a
    # stale account read is re-asked on later cycles' latest decision.
    "retry_operational_refusals": True,
    # A refused or killed send is re-sent on a newer book, at most this many
    # sends per station-day; a fill or an open order ends it.
    "max_sends_per_station_day": 3,
    # A retry is priced only on a book at most this old (seconds).
    "retry_book_fresh_seconds": 900.0,
    # User ruling 2026-09-20 "改": a send the VENUE turned away because the
    # market was not trading ("trading is disabled") is not a verdict on the
    # book.  It spends none of the budget above -- counted, it ended NZWN
    # 2026-09-21 and MMMX 2026-09-20 in six and thirteen minutes on
    # 2026-09-19, three sends each, before those markets opened -- and has
    # its own budget and an hour between tries instead.
    "max_venue_disabled_sends_per_station_day": 6,
    "venue_disabled_cooldown_seconds": 3600.0,
}

#: The P1 tail lane's settings (charter 166, user ruling 2026-09-20 「上线尾段
#: 车道 p1_tail 策略」).  The belief is the market's own mid ladder under the
#: hard observed bound times the served P1 observation likelihood; the lane
#: crosses at the ask when that belief clears the ask by ``hurdle``.
#:
#: Measured 2026-09-20 over 09-05..09-19 (9,436 reports, 648 station-days):
#: in the 6-10h remaining bin, +0.166/$ at the report plus two minutes and
#: 8c, 13 of 15 days positive; +0.138 at t+5; +0.077 at t+8 with a lower
#: bound through zero.  Outside the bin it is negative (>=10h: -0.033) or
#: absent (<6h: eight buys in fifteen days).  See ``p1_tail_lane`` for what
#: that measurement does and does not support.
P1_TAIL_DEFAULT_SETTINGS: dict[str, Any] = {
    # Live small: the bin was chosen after seeing the result and only two of
    # the fifteen days are days no selection saw.
    "enabled": True,
    "stake_usdc": 5.0,
    "rule": "market_prior_x_p1_likelihood_taker_v1",
    # 5c / 8c / 12c measured +0.129 / +0.166 / +0.195 per dollar at t+2 with
    # 12 / 13 / 14 of 15 days positive.  Eight is where the lower bound
    # cleared zero on every delay this lane will trade at.
    "hurdle": 0.08,
    # The bin, read off the envelope's own likelihood receipt.
    "min_remaining_hours": 6.0,
    "max_remaining_hours": 10.0,
    # The edge keeps 83% of itself at five minutes and its lower bound
    # crosses zero by eight, so an envelope older than this is not a slower
    # trade -- it is a different one.
    "envelope_fresh_seconds": 300.0,
    # What may still be SCORED (the evaluation row is evidence) ...
    "book_fresh_seconds": 900.0,
    # ... and what may be BOUGHT.  A FOK is priced on the snapshot and
    # arrives later; both of the lane's first sends were priced on a book
    # 171 and 328 seconds old, and the second died because the level it
    # measured had been taken.
    "send_book_fresh_seconds": 60.0,
    # One bucket is bought once ever; this bounds the whole station-day.
    "max_sends_per_station_day": 2,
}

#: The per-trade stake is a board dial (charter 146); the server refuses a
#: value outside this range, and the lane reads whatever the file says.
TIEBREAK_STAKE_BOUNDS = (1.0, 100.0)
TIEBREAK_STAKE_STEP = 1.0

DEFAULT_LANES: dict[str, dict[str, Any]] = {
    LANE_BASKET: {"enabled": True},
    LANE_CERTAIN_WIN_RECYCLE: {"enabled": True},
    LANE_MIS_BOUGHT_LEG_EXIT: {"enabled": True},
    LANE_TIEBREAK: dict(TIEBREAK_DEFAULT_SETTINGS),
    LANE_P1_TAIL: dict(P1_TAIL_DEFAULT_SETTINGS),
}


def config_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    override = os.environ.get(ENV_VAR, "").strip()
    return Path(override) if override else DEFAULT_PATH


def load_strategy_lanes(path: Path | str | None = None) -> dict[str, Any]:
    """The lane table, read fresh.  See the module docstring for the three
    outcomes: the file (``source: file``), no file (``source: defaults``),
    an unreadable file (``source: unreadable``, every lane paused)."""

    target = config_path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {
            "schema": SCHEMA,
            "source": "defaults",
            "path": str(target),
            "lanes": {name: dict(value) for name, value in DEFAULT_LANES.items()},
        }
    except OSError as exc:
        return _unreadable(target, f"{type(exc).__name__}:{exc}")
    try:
        document = json.loads(raw)
    except ValueError as exc:
        return _unreadable(target, f"json:{exc}")
    if not isinstance(document, dict) or not isinstance(document.get("lanes"), dict):
        return _unreadable(target, "lanes_missing")
    lanes: dict[str, dict[str, Any]] = {}
    for name in KNOWN_LANES:
        declared = document["lanes"].get(name)
        merged = dict(DEFAULT_LANES[name])
        if isinstance(declared, dict):
            merged.update(declared)
        elif declared is not None:
            # A bare value where a table is expected is not a ruling.
            return _unreadable(target, f"lane_not_a_table:{name}")
        lanes[name] = merged
    return {
        "schema": SCHEMA,
        "source": "file",
        "path": str(target),
        "updated_at_utc": document.get("updated_at_utc"),
        "lanes": lanes,
    }


def _unreadable(target: Path, reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "source": "unreadable",
        "reason": reason,
        "path": str(target),
        "lanes": {
            name: {**dict(DEFAULT_LANES[name]), "enabled": False}
            for name in KNOWN_LANES
        },
    }


def lane_settings(name: str, path: Path | str | None = None) -> dict[str, Any]:
    lane = str(name or "").strip()
    if lane not in KNOWN_LANES:
        raise ValueError(f"strategy_lane_unknown:{lane}")
    return dict(load_strategy_lanes(path)["lanes"][lane])


def lane_enabled(name: str, path: Path | str | None = None) -> bool:
    """Whether the operator has this lane switched on.  Only ``True`` is on."""

    return lane_settings(name, path).get("enabled") is True


def validate_tiebreak_stake(value: Any) -> float:
    """A stake the lane may be set to: finite, inside the bounds, to the cent."""

    try:
        stake = round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError("tiebreak_stake_not_a_number") from exc
    if not (stake == stake) or stake in (float("inf"), float("-inf")):
        raise ValueError("tiebreak_stake_not_finite")
    low, high = TIEBREAK_STAKE_BOUNDS
    if not (low <= stake <= high):
        raise ValueError(f"tiebreak_stake_out_of_bounds:{low}..{high}")
    return stake


__all__ = [
    "DEFAULT_LANES",
    "TIEBREAK_STAKE_BOUNDS",
    "TIEBREAK_STAKE_STEP",
    "validate_tiebreak_stake",
    "DEFAULT_PATH",
    "ENV_VAR",
    "KNOWN_LANES",
    "LANE_BASKET",
    "LANE_CERTAIN_WIN_RECYCLE",
    "LANE_MIS_BOUGHT_LEG_EXIT",
    "LANE_P1_TAIL",
    "LANE_TIEBREAK",
    "P1_TAIL_DEFAULT_SETTINGS",
    "SCHEMA",
    "TIEBREAK_DEFAULT_SETTINGS",
    "config_path",
    "lane_enabled",
    "lane_settings",
    "load_strategy_lanes",
]
