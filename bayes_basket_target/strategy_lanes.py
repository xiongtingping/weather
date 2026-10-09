"""Operator switches, one per strategy lane (user ruling 2026-09-18).

The ruling: put the tiebreak lane (「秤砣线」, ``tiebreak_lane``) live and
pause every other strategy -- close them, do not delete them.  Before this
file the only stops were global: the arming flag and ``live_paused.flag``
hold every send, and the runtime contract's ``live_submit_authorized`` is
minted by a migration.  Nothing could stop one lane and leave another
running, so "pause the basket, run the tiebreak" had no switch to throw.

The lanes known here, each the name the daemon and the submitter use
for it:

* ``basket`` -- every BUY the basket compile mints on a cycle: the seat, a
  top-up, a charter 143 completion.  Read by ``order_submission`` at the
  send, exactly where the arming flag and the station bar are read, so a
  paused lane still compiles, reserves, records its decision and writes the
  evidence the ledger fits on; only the send is held, and the intent ends
  ``failed`` with ``strategy_lane_paused:basket`` on its event.
* ``certain_win_recycle`` -- the 0.999 sell of a won position (charter 264; 0.998 before).
* ``mis_bought_leg_exit`` -- charter 144's sell-back of legs bought outside
  the completion rule.
* ``tiebreak`` -- the lane this ruling puts live.  Its settings (stake, the
  market-share threshold) live beside its switch.
* ``p1_tail`` / ``p1_consensus`` -- charters 166 and 203.
* ``proper_bet`` -- charter 231's Proper 下注线, which replaced the basket's
  betting rule on 2026-09-26.
* ``copy_follow`` -- charter 256's 跟单线: one Polymarket account's weather
  trades, mirrored at our scale.  Off unless the file switches it on.

Read fresh on every call, never cached: a cached answer keeps sending after
the operator has thrown the switch, which is the failure the arming flag
already closed once.  A MISSING file means the defaults -- every lane
enabled but the 跟单线, the tiebreak lane at its measured settings -- so a
test or a rebuilt checkout runs the system as designed; a file that exists but cannot
be read is not a ruling anyone can act on, and reads as every lane paused,
because this file authorises spending and a switch nobody can read is not
permission.  ``PM_STRATEGY_LANES_CONFIG`` points a test at its own copy.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
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
LANE_P1_CONSENSUS = "p1_consensus"
LANE_PROPER_BET = "proper_bet"
LANE_TAIL_SHORT = "tail_short"
LANE_COPY_FOLLOW = "copy_follow"
KNOWN_LANES: tuple[str, ...] = (
    LANE_BASKET,
    LANE_CERTAIN_WIN_RECYCLE,
    LANE_MIS_BOUGHT_LEG_EXIT,
    LANE_TIEBREAK,
    LANE_P1_TAIL,
    LANE_P1_CONSENSUS,
    LANE_PROPER_BET,
    LANE_TAIL_SHORT,
    LANE_COPY_FOLLOW,
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
    # v2.3 (charter 175, user ruling 2026-09-21 "把价带上限改成按实付价
    # ≤0.60"): v2.2 with the band's ceiling on the rung's own price
    # (``max_paid_price``) instead of the normalised mid; the floor stays on
    # the mid.  ``max_market_share_on_model_mode`` is read only by v2/v2.1/v2.2.
    "rule": "model_mode_midband_spread_maker_paid_cap_v2_3",
    "min_market_share_on_model_mode": 0.40,
    "max_market_share_on_model_mode": 0.60,
    "max_paid_price": 0.60,
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
    # Charter 229 (user rulings 2026-09-26 「p0m和p1仅限秤砣线策略开仓」 and
    # 「P0 只留篮子」): the stages whose first decision may open.  A P0 first
    # decision is still scored into the evaluation table -- the forward
    # sample stays the whole population -- and a fired one is refused
    # ``stage_not_opening:P0``, for good.
    "opening_stages": ["P0-M", "P1"],
}

#: Every stage a lane may be told to open on.
KNOWN_STAGES: tuple[str, ...] = ("P0", "P0-M", "P1")

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
    # The bin, read off the envelope's own likelihood receipt, on the hours
    # left to the settlement clock -- the axis the edge was measured on.
    # "clock_hours" reads the receipt's clock instead (remaining hours plus
    # the station's peak offset, charter 157), as the lane did until
    # 2026-09-21.  See ``p1_tail_lane.HOURS_AXIS_REMAINING``.
    "min_remaining_hours": 6.0,
    "max_remaining_hours": 10.0,
    "hours_axis": "remaining_hours",
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
    # How far above the best ask a take may be authorised, in ticks.  The
    # top ask level's median depth on this book is 10 shares and its p25 is
    # 5.7, so a 5 USDC take is bigger than the whole top level most of the
    # time and a FOK held to the ask is killed: three live sends, three
    # kills.  One tick carried every sampled level at 0.72 and 91% at 0.50.
    # The lane pays for it by testing the hurdle against the WALKED price.
    "walk_ticks": 1,
}

#: The P1 consensus lane's settings (charter 203, user ruling 2026-09-23
#: 「上线，一个站日可以买多次！」; rule A, no yielding to the tiebreak lane,
#: 5 USDC a take).  On a P1 decision 6-10 h from the settlement clock where
#: the model's and the market's mode and mean buckets are one bucket and its
#: best ask is in [0.50, 0.70), cross FOK at the ask.
#:
#: Measured 2026-09-23 over 07-28..09-22 on the first book after the decision
#: was committed: 117 takes / 90 station-days, hit 71.8%, +15.3% [+4.8%,
#: +25.3%]; the 27 from 09-20 +10.6%; priced 15 minutes later +12.2%.  See
#: ``p1_consensus_lane`` for what that does and does not support.
P1_CONSENSUS_DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "stake_usdc": 5.0,
    # v2 (user ruling 2026-09-23 「吃单改为post only。best买一+1，挂单周期改为
    # 最短，没成交继续挂！」): a post-only GTD rung at bid+1, signed at the
    # venue's minimum horizon, re-posted while the belief stands.  v1 crossed.
    "rule": "p1_four_centre_consensus_maker_v2",
    # The window, on the hours left to the settlement clock at the decision
    # instant (``settlement_time.hours_to_settlement``), half-open.
    "min_remaining_hours": 6.0,
    "max_remaining_hours": 10.0,
    # The band, on the consensus bucket's best ask, half-open ...
    "min_ask": 0.50,
    "max_ask": 0.70,
    # ... and the most a rung may be priced at (bid+1 sits under the ask, so
    # this binds only if the band is widened).
    "max_paid_price": 0.70,
    # Not a speed trade (+12% priced fifteen minutes late), so a decision is
    # judged up to this old -- long enough to span a slow cycle's head/tail.
    "fact_fresh_seconds": 1200.0,
    # What may still be scored, and what a rung may be priced on.
    "book_fresh_seconds": 900.0,
    "send_book_fresh_seconds": 60.0,
    "live_ws_book": True,
    # A rung that expired or was cancelled unfilled, or an egress-held send,
    # is not a take.  User ruling 2026-09-23 「每站日上限 2 笔，单桶上限 1 笔」: two takes a
    # station-day, one per bucket (this lane's own takes).
    "max_buys_per_station_day": 2,
    "max_buys_per_bucket": 1,
    # Every rung posted counts, filled or not: a rung lives ~3.5 minutes and
    # is re-posted while unfilled, so this is a bound on a runaway, not a
    # budget (a four-hour window re-posted every ~4 minutes is ~60).
    "max_sends_per_station_day": 120,
    # User ruling 2026-09-23 「加 20 分钟间隔」: consecutive takes on one
    # station-day at least this far apart in decision time.  The replay's sat
    # 6-91 minutes apart (most 15-40) because decisions came that slowly;
    # with cycles at ~70 s, LTAC 2026-09-23 took four in four minutes.
    "min_minutes_between_buys": 20.0,
}

#: Proper 下注线 (charter 231, user rulings 2026-09-26 「调整下注规则」 and
#: 「实盘上线，p＝天气模型」): the betting rule of arXiv 2607.06166 in place of
#: the basket's.  At a stage's first decision, the weather model's served
#: vector ``p`` against the market's normalised mid ladder ``q``: the Log
#: proper position ``s = log(p / q)`` on every bucket, YES where ``s > 0``,
#: NO where ``s < 0``, each a resting rung sized ``lambda * |s|``.
#: See ``proper_bet_lane`` for the band, the sizing and what was measured.
PROPER_BET_DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    # v2 (user ruling 2026-09-26 「吃单改为挂单，post-only，best bid+1，挂单周期
    # 最短，重新挂单，直至成交，但是挂单价格要符合下注规则」): each leg a
    # post-only GTD rung at bid+1t on its own side's book, signed at the
    # venue's minimum horizon and re-posted until filled, every price under
    # the leg's claim (YES under p, NO under 1 - p).
    #
    # v3 (charter 249, user rulings 2026-10-03 「重开Proper 线并放开 YES，只开YES，
    # 市场众数桶，挂单」「限p0」): the same resting rung, on one leg a
    # station-day -- YES on the market's modal bucket.
    "rule": "log_proper_maker_market_mode_yes_v3",
    # 「预算提高到最低要求」 left lambda at the smallest the venue allows for
    # the whole position (the smallest leg at the five-share minimum), capped
    # by the free cash.  Charter 252 (user ruling 2026-10-04 「还有Proper 下注线
    # 的单笔预算调整按钮」): the position is filled up to ``stake_usdc``, a
    # board dial, never under that minimum and still capped by the cash.  At
    # 1 USDC it is the minimum rule wherever the leg is priced at 0.20 or
    # more; under 0.20 the plan carries the dollar charter 83 used to add at
    # the send.
    "budget_rule": "stake_full_position",
    "stake_usdc": 1.0,
    # 「仅p0阶段下单」 / 「限p0」: only a station-day that is in P0 opens, and the
    # plan is posted while it still is.
    "opening_stages": ["P0"],
    # Charter 249: which decision opens the station-day.  ``latest_decision``
    # judges its newest P0 decision on every pass until a plan is made;
    # ``first_decision`` (charter 231) judged only the first, inside its first
    # hour, when a ladder two days out is rarely quoted whole -- 170 of 173
    # station-days were refused ``first_decision_stale`` over 2026-09-26..30.
    "entry": "latest_decision",
    # Charter 249 (十), user ruling 2026-10-03 「按信封有效期」: the decision a
    # plan is made from is fresh while the P0 envelope it read is inside its
    # own validity, not only for an hour after it was made
    # (``decision_age``, charter 231) -- P0 decisions two days out come in
    # bursts with the model runs, hours apart.
    "freshness": "envelope_validity",
    # 「按照>36h」: posted only while the settlement clock is more than this
    # many hours away -- the band the L4 verdict found P0 beat the market on.
    "min_hours_to_settlement": 36.0,
    # A decision older than this is not planned on (under ``first_decision``
    # it is recorded stale; under ``latest_decision`` the station-day waits
    # for its next one).
    "first_decision_fresh_seconds": 3600.0,
    # A rung is priced on a book at most this old; the WS publisher's own
    # ladder first, the committed snapshot otherwise.
    "send_book_fresh_seconds": 60.0,
    "live_ws_book": True,
    # Charter 249 (九), user ruling 2026-10-03 「加长」: how long a rung rests
    # before it expires and is re-posted.  0 is the venue's minimum (charter
    # 231's 「挂单周期最短」, about 270 s); at that length a rung was on the book
    # for about four minutes of a production cycle that ran six to fifteen on
    # the afternoon of 2026-10-03.  Never past the 36-hour line.
    "rung_horizon_seconds": 900.0,
    # 「Proper 线的 YES 腿停掉」 (charter 235, user ruling 2026-09-28): the
    # sides a leg may take.  The measurement behind the ruling had the rule's
    # YES legs at -29.0% [-37.3%, -19.2%] at the ask and its NO legs flat
    # (p0_differential_edge_20260927, the control reading).  A YES leg is
    # still quoted and recorded, never posted; the whole position's lambda
    # is the NO legs' own.
    #
    # Charter 249 (2026-10-03) turns the switch the other way: YES only.  The
    # -29.0% was the YES legs of the WHOLE ladder bought at the ask; by rungs
    # from the market's mode it is the YES legs two or more rungs out that
    # lose under every execution tried, and the YES leg on the modal bucket
    # that does not (proper_conditional_edge_20261003, section 14).
    "sides": ["YES"],
    # Charter 249: the buckets a leg may sit on -- ``market_mode`` (the
    # market's own modal bucket, the lower rung on a tie) or ``all``.
    "buckets": "market_mode",
    # Charter 258 (user rulings 2026-10-09 「proper线增加一条规则：不做对标账号的
    # 对手盘」, 「买no线交给跟单线」, 「proper线以对标账号为筛子，不做对标账号的对手盘！」,
    # 「是对标账号的对手盘的yes全部卖掉」): the benchmark cluster
    # (``BENCHMARK_CLUSTER_WALLETS``) is the lane's filter.  ``seal``: a bucket
    # the cluster has traded the other side of gets no plan and no further
    # rung, and a resting rung is cancelled; ``sell`` also sells a YES the
    # lane holds there, the lane paused or not; ``off`` restores the lane
    # before the ruling.  The NO is the copy-follow lane's to buy.
    "benchmark_counterparty": "sell",
}

#: 远尾做空线 (charter 234, user rulings 2026-09-28 「上线：只按市场自己的价格结构
#: 做空远尾桶」 and 「上线：H1 的纯市场版」): the market's own far tails, bought NO
#: as a taker.  The rule is the one measured (p0_differential_edge_20260927,
#: H1's market-only arm); the sizing and the limits are the lane's own.
#: Rule v2 (charter 251, user ruling 2026-10-04 「改规则，把模型当过滤器」) adds
#: one condition: the served P0 p of the bucket under the YES bid; v1 is the
#: market-only lane.  Rule v3 (charter 251 (六), user ruling 2026-10-07 「改」)
#: reads that p on the served vector moved to the market's centre.
TAIL_SHORT_DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "rule": "market_far_tail_no_taker_model_centred_below_bid_v3",
    # The window measured: P0, more than 36 hours to the settlement clock.
    "opening_stages": ["P0"],
    "min_hours_to_settlement": 36.0,
    # The leg measured: the market's own price at or under 5%, three or more
    # rungs from its modal bucket, a YES bid of at least 2 cents.
    "max_market_price": 0.05,
    "min_rungs_from_mode": 3,
    "min_yes_bid": 0.02,
    # Per take, in USDC at the NO ask; the depth at the bid caps it.
    "stake_usdc": 10.0,
    "max_legs_per_station_day": 4,
    # Sends a bucket gets that reached the venue (a killed FOK is one) ...
    "max_attempts_per_bucket": 3,
    # ... and orders it may mint whatever became of them, so a send the
    # gateway keeps refusing costs a bounded number of rows; one that never
    # reached the venue waits this long before the next.
    "max_orders_per_bucket": 8,
    "unsent_retry_seconds": 300.0,
    "max_daily_cost_usdc": 150.0,
    # Polymarket's weather taker fee rate: fee = shares x rate x p x (1 - p).
    "taker_fee_rate": 0.05,
    "send_book_fresh_seconds": 60.0,
    "live_ws_book": True,
    "lookback_days": 1,
}

#: The benchmark account (charter 256's badatmath., ``0x8fbd7cf5``) is one
#: wallet of an eight-wallet bot cluster that trades on one clock
#: (docs/research/benchmark_account_0x8fbd_20261008, round 6).  The one list:
#: the 跟单线 follows it (charter 257), the Proper 下注线 never takes its other
#: side (charter 258).
BENCHMARK_CLUSTER_WALLETS: tuple[str, ...] = (
    "0x8fbd7cf5f806f563080864694415829f7229a959",
    "0x6fdd2bda5c031d551f588f176535f061a29a0b07",
    "0x5bae7577f470244ce8e678d20693eee2b8ffed25",
    "0x30200b893530920f253846199a153e6ac725df2b",
    "0x0e0e8174e213ff2c2ccb30c5652a7b7d77c72d75",
    "0x7076cb961e87bd22199445d02fe22973510cd0d9",
    "0x7d640ebd20cf136bee351100438bba44ca2e07f8",
    "0xf4d89f7e8c2fc8d2303e0f6b63cdff37fe304c91",
)

#: Charter 256's 跟单线 (user rulings 2026-10-08 「不用研究，开始做跟单策略」「按照等比例
#: 缩小，我资金没有他多」「按他已投入仓位算」).  Off by default: a missing lane
#: file must not start spending on another account's trades.
COPY_FOLLOW_DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "rule": "source_positions_value_ratio_at_source_price_v1",
    # The account followed (badatmath., the copy lane of July's v1 too).
    "source_user": "0x8fbd7cf5f806f563080864694415829f7229a959",
    # Our scale against theirs: our account equity over the marked value of
    # the source's open positions (its idle cash is not counted).
    "ratio_basis": "source_positions_value",
    # Source trades before this instant are history, not orders to follow;
    # unset, the lane's first pass stamps it.
    "follow_from_utc": None,
    # A source trade older than this when first seen is recorded, not followed.
    "max_source_age_seconds": 21600.0,
    "sides": ["YES", "NO"],
    "mirror_sells": True,
    # How far past the source's own price a BUY may pay (and a SELL may
    # accept under it), in price units.
    "max_slippage": 0.02,
    "min_order_shares": 5.0,
    # A resting rung (and a resting sell) lives this long, then is re-posted.
    "rung_horizon_seconds": 900.0,
    "send_book_fresh_seconds": 120.0,
    "live_ws_book": True,
    # No daily cap (user ruling 2026-10-09 「40 U 日预算改为不设置上限」); a positive
    # value would be the UTC day's budget.  Cash stays the hard stop.
    "max_daily_cost_usdc": None,
    "max_sends_per_pass": 8,
    # An order that never reached the venue waits this long before the next.
    "unsent_retry_seconds": 300.0,
    "source_value_refresh_seconds": 300.0,
    # Charter 257 (rule ``cluster_taker_first_filtered_fixed_stake_v2``; user
    # rulings 2026-10-09 「开实盘，利用现有的跟单策略，但是我的资金量有限，如何跟单」): the
    # eight-wallet cluster the research found behind the account, followed on
    # the FIRST taker buy of a bucket episode only, at a fixed stake, and only
    # where our own served probability agrees by ``model_margin``.  Unused by v1.
    "source_users": list(BENCHMARK_CLUSTER_WALLETS),
    "stake_usdc": 5.0,
    "model_margin": 0.05,
    "lead_hours": [12.0, 48.0],
    "station_units": ["C"],
    # Charter 260: once a P1 stands it is the belief, so the lane may read it.
    "model_stages": ["P0", "P0-M", "P1"],
    "episode_gap_seconds": 3600.0,
    # Charter 257 (2026-10-09 「3+5」): take or nothing -- a follow is a FOK at
    # or under the cap within ``take_window_seconds`` of the episode's buy, or
    # no order at all; never a resting rung.  Off here, set in the lane file.
    "take_only": False,
    "take_window_seconds": 1800.0,
}

#: The lanes whose per-trade ``stake_usdc`` is a board dial -- the tiebreak
#: lane's since charter 146, the far-tail and Proper lanes' since charter 252
#: (user rulings 2026-10-04 「顶部增加：远尾做空 H1 的单笔预算调整」「还有Proper 下注线
#: 的单笔预算调整按钮」) -- each with its bounds and the board's step.  The
#: server refuses a value outside the bounds; the lane reads the file.
STAKE_DIALS: dict[str, dict[str, Any]] = {
    LANE_TIEBREAK: {"bounds": (1.0, 100.0), "step": 1.0},
    LANE_TAIL_SHORT: {"bounds": (1.0, 100.0), "step": 1.0},
    LANE_PROPER_BET: {"bounds": (1.0, 100.0), "step": 1.0},
    LANE_COPY_FOLLOW: {"bounds": (1.0, 50.0), "step": 1.0},
}

DEFAULT_LANES: dict[str, dict[str, Any]] = {
    LANE_BASKET: {"enabled": True},
    LANE_CERTAIN_WIN_RECYCLE: {"enabled": True},
    LANE_MIS_BOUGHT_LEG_EXIT: {"enabled": True},
    LANE_TIEBREAK: dict(TIEBREAK_DEFAULT_SETTINGS),
    LANE_P1_TAIL: dict(P1_TAIL_DEFAULT_SETTINGS),
    LANE_P1_CONSENSUS: dict(P1_CONSENSUS_DEFAULT_SETTINGS),
    LANE_PROPER_BET: dict(PROPER_BET_DEFAULT_SETTINGS),
    LANE_TAIL_SHORT: dict(TAIL_SHORT_DEFAULT_SETTINGS),
    LANE_COPY_FOLLOW: dict(COPY_FOLLOW_DEFAULT_SETTINGS),
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


def opening_stages(settings: Mapping[str, Any]) -> frozenset[str]:
    """The stages a lane's ``opening_stages`` setting names.

    A list of known stage names, possibly empty (a lane that opens nowhere).
    Anything else is not a ruling anyone can act on, and raises: a lane that
    guessed would be deciding where to spend on its own.
    """

    declared = settings.get("opening_stages")
    if not isinstance(declared, (list, tuple)):
        raise ValueError(f"opening_stages_not_a_list:{declared!r}")
    stages = frozenset(str(stage).strip().upper() for stage in declared)
    unknown = sorted(stages - set(KNOWN_STAGES))
    if unknown:
        raise ValueError(f"opening_stages_unknown:{','.join(unknown)}")
    return stages


def validate_lane_stake(lane: str, value: Any) -> float:
    """A stake a dialled lane may be set to: finite, inside its bounds, to
    the cent."""

    name = str(lane or "").strip()
    if name not in STAKE_DIALS:
        raise ValueError(f"stake_dial_unknown:{name}")
    try:
        stake = round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}_stake_not_a_number") from exc
    if not math.isfinite(stake):
        raise ValueError(f"{name}_stake_not_finite")
    low, high = STAKE_DIALS[name]["bounds"]
    if not (low <= stake <= high):
        raise ValueError(f"{name}_stake_out_of_bounds:{low}..{high}")
    return stake


__all__ = [
    "DEFAULT_LANES",
    "BENCHMARK_CLUSTER_WALLETS",
    "STAKE_DIALS",
    "validate_lane_stake",
    "DEFAULT_PATH",
    "ENV_VAR",
    "KNOWN_LANES",
    "KNOWN_STAGES",
    "COPY_FOLLOW_DEFAULT_SETTINGS",
    "LANE_BASKET",
    "LANE_COPY_FOLLOW",
    "LANE_CERTAIN_WIN_RECYCLE",
    "LANE_MIS_BOUGHT_LEG_EXIT",
    "LANE_P1_CONSENSUS",
    "LANE_P1_TAIL",
    "LANE_PROPER_BET",
    "LANE_TAIL_SHORT",
    "LANE_TIEBREAK",
    "P1_CONSENSUS_DEFAULT_SETTINGS",
    "PROPER_BET_DEFAULT_SETTINGS",
    "P1_TAIL_DEFAULT_SETTINGS",
    "SCHEMA",
    "TAIL_SHORT_DEFAULT_SETTINGS",
    "TIEBREAK_DEFAULT_SETTINGS",
    "config_path",
    "lane_enabled",
    "lane_settings",
    "load_strategy_lanes",
    "opening_stages",
]
