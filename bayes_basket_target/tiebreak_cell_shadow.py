"""The tiebreak cell's replay, and the hypotheses registered against it.

Charter 164 (user ruling 2026-09-20 「落」).  The lane's own trades cannot
decide anything about a feature and never will: it fills about eight a day,
so charter 146's thirty event days are roughly 240 trades, which split in
two leave about 120 a side and a day-clustered interval half a dozen points
wide either way.  A five-point feature is not readable there, and a
three-trade bin is not a measurement at all -- on 2026-09-20 the board's
「没动或远离」 bin read −17.5 points, which was 1 中 2 空, while the same bin
over the replay was +6.6.

So the sample the features are judged on is the RULE, replayed over every
first decision the store holds, scored against the truth lane.  On
2026-09-20 that is 450 station-day-stages / 388 station-days over 37 event
days -- fifty times the trades -- and it grows by itself every day.  The
replay calls the lane's own ``evaluate_rule``, the kernel's own
``market_reference_v1`` and the settler's own bucket resolution: nothing
here restates a production rule, because an audit that reimplements the
rule invents its own findings.

What the replay is for.  Searching it is nearly worthless on its own: with
122 candidate cells, labels shuffled inside their event day produce a
"confirmed in both halves" cell 100% of the time, and the weaker side of
that random cell reaches +24.0% ROI at the 95th percentile -- better than
anything really in the data.  The threshold falls with the search: +15.3%
for ONE cell fixed in advance, +17.5% for five, +20.6% for twenty.  That is
the whole reason this file exists: a hypothesis written down BEFORE it is
measured is worth about ten points of ROI against one found by looking.

``HYPOTHESES`` is therefore append-only and each entry carries the day it
was registered, and the POPULATION it is read on (``pool``): the slot the
lane buys, every firing row, or the station-days where the choice between
two stages actually exists.  A stage contrast read on the slot grain is
mostly the selection -- the slot reaches P1 only when nothing earlier
fired -- so the pool is part of the hypothesis, not an implementation
detail.  Every refresh reports two readings: the whole replay, and
the FORWARD-only slice -- event days after registration -- which is the one
that decides.  The full-history number is context, and is already spent.
"""

from __future__ import annotations

import json
import random
import sqlite3
import statistics as st
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .basket_kernel.valuation_v1 import market_reference_v1
from .candidate_window_evidence import standing_labels
from .contracts import content_sha256
from . import tiebreak_lane as lane

_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SSOT_DB = _ROOT / "data" / "bayes_basket_target_ssot.db"
#: Its own file, not the SSOT: the store sits 4.95 GiB under a 60 GiB cap and
#: burns about 2.6 GiB a day, and charter 155 already moved the paper ledger
#: out for the same reason.  This one is a few hundred kilobytes, but a
#: research artifact that grows without an owner is how the cap is reached.
SHADOW_DB = _ROOT / "data" / "tiebreak_cell_shadow.db"
SHADOW_SCHEMA = "tiebreak_shadow"
REPLAY_TABLE = "tiebreak_cell_replay"
HYPOTHESIS_TABLE = "tiebreak_cell_hypothesis"
#: The replay reaches back to the first event day the store can settle.
DEFAULT_FLOOR_EVENT_DATE = "2026-07-01"
#: Bootstrap draws, and a fixed seed: the reading is re-derived every day and
#: a wandering interval would look like news.
BOOTSTRAP_DRAWS = 4000
BOOTSTRAP_SEED = 164


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------
# The registered hypotheses.


def _converging(row: Mapping[str, Any]) -> bool:
    """The market moved at least three points onto the model's bucket since
    the station-day's previous decision -- which, because the lane only
    scores a stage's FIRST decision, is always the previous STAGE's."""

    move = row.get("market_move_on_model_mode")
    return move is not None and float(move) * 100.0 >= 3.0


#: The population a hypothesis is read on (charter 164, 2026-09-20 second
#: pass).  The lane buys one station-day at a time, so the default is the
#: slot grain -- but a contrast between two STAGES cannot be read there.
#: "The slot went to P1" means, by construction, that neither P0 nor P0-M
#: fired that station-day: the arms are disjoint populations and the contrast
#: carries that selection as well as the stage.  Measured 2026-09-20: on the
#: grain P0 +3.9% / P0-M +7.9% / P1 +15.3%, over every firing row the same
#: three read +3.9% / +3.6% / +7.5%.  The gradient is mostly the selection.
#:   ``grain``        -- the earliest firing stage of each station-day: what
#:                       the lane actually buys, and the only pool a POLICY
#:                       question belongs in;
#:   ``firings``      -- every (station, day, stage) the rule fires on: the
#:                       unconditional stage question;
#:   ``contested_p1`` -- the station-days where P1 fired AND an earlier stage
#:                       fired, so the row the lane took and the P1 row it
#:                       passed over sit on the SAME weather.  Paired, which
#:                       is the only way to price the slot going later.
POOLS = ("grain", "firings", "contested_p1")


def pools_of(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The three populations, each row carrying ``is_slot`` and
    ``stage_count`` so a hypothesis can name the row the slot went to."""

    by_day: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("best_ask") is None:
            continue
        by_day[(str(row["station_id"]), str(row["event_date"]))].append(row)
    firings: list[dict[str, Any]] = []
    grain: list[dict[str, Any]] = []
    contested: list[dict[str, Any]] = []
    for key in sorted(by_day):
        day = sorted(by_day[key], key=lambda r: str(r["decision_at_utc"]))
        marked = [
            dict(row, is_slot=(index == 0), stage_count=len(day))
            for index, row in enumerate(day)
        ]
        firings.extend(marked)
        grain.append(marked[0])
        stages = {str(row["stage"]) for row in marked}
        if len(marked) > 1 and "P1" in stages and stages - {"P1"}:
            contested.extend(marked)
    return {"grain": grain, "firings": firings, "contested_p1": contested}


#: Each hypothesis is a CONTRAST between two arms of the replay, a direction
#: predicted in advance, and the day it was written down.  ``arms`` returns
#: (treatment, control); a control of None means "is this arm's ROI above
#: zero", which is how the cell itself is registered.
HYPOTHESES: tuple[dict[str, Any], ...] = (
    {
        "hypothesis_id": "cell_itself_v1",
        "title": "格子本身：模型众数桶被市场定在 0.40–0.60 且两侧窄",
        "registered_at_utc": "2026-09-20",
        "prediction": "ROI > 0",
        # 2026-09-20, whole replay: 388 station-days, +7.9% [+2.2%, +13.7%],
        # P(<=0)=0.01.  Registered so the forward sample keeps checking the
        # lane's own premise, not because it is in doubt.
        "at_registration": "+7.9% [+2.2%, +13.7%], P(<=0)=0.01, n=388",
        "treatment": lambda r: True,
        "control": None,
    },
    {
        "hypothesis_id": "stage_gradient_v1",
        "title": "阶段梯度：离结算越近越赚（P1 高于 P0）",
        "registered_at_utc": "2026-09-20",
        "prediction": "ROI(P1) - ROI(P0) > 0",
        # Read on the GRAIN, which makes it a question about the slot and
        # not about the stage: the P1 arm is the station-days where nothing
        # earlier fired.  ``stage_gradient_unconditional_v1`` below asks the
        # stage question on every firing row, and ``slot_to_p1_contested_v1``
        # asks the policy question paired.  All three stay: this one is what
        # was written down first and its forward reading is not retracted.
        # 2026-09-20: P0 +3.9%, P0-M +7.9%, P1 +15.3% -- monotone, and the
        # same shape the 2026-09-17 reading found independently (the model's
        # information share over the market rises from 0.16 to 0.32 inside
        # 36 hours).  The contrast is +11.4 points, p=0.123 by permutation:
        # not significant, and the only candidate with a mechanism behind it.
        "at_registration": "+11.4 点, p=0.123 (P0 +3.9% / P0-M +7.9% / P1 +15.3%)",
        "treatment": lambda r: r["stage"] == "P1",
        "control": lambda r: r["stage"] == "P0",
    },
    {
        "hypothesis_id": "p1_converging_v1",
        "title": "对照组：P1 内部，市场靠拢≥3点 是否胜过不靠拢",
        "registered_at_utc": "2026-09-20",
        "prediction": "ROI(P1 且靠拢) - ROI(P1 且非靠拢) > 0",
        # Registered as the CONTROL for the one above, not as a candidate.
        # The momentum axis is confounded with the stage schedule by
        # construction -- the lane scores a stage's first decision, so the
        # reference is always the previous stage's, 47 minutes back for P0-M
        # and 739 for P1 -- and 「靠拢≥3点」 is, to first order, 「this is a P1
        # row」.  2026-09-20: +25.9 points, p=0.320.  If the stage gradient is
        # what pays, this contrast goes to zero while the one above holds.
        "at_registration": "+25.9 点, p=0.320",
        "treatment": lambda r: r["stage"] == "P1" and _converging(r),
        "control": lambda r: r["stage"] == "P1" and not _converging(r),
    },
    {
        "hypothesis_id": "band_top_slice_is_priced_out_v1",
        "title": "价带顶档：归一化中价 0.55–0.60 那一刀是不是买贵了",
        "registered_at_utc": "2026-09-20",
        "prediction": "ROI(0.40–0.55) - ROI(>=0.55) > 0",
        # Found while asking why 2026-09-19's five P0-M trades lost three:
        # two of the three were bought at 0.61 and 0.62.  The band's ceiling
        # is applied to the market's NORMALISED mid while the money leaves at
        # the raw ask, which sits a median 3.0 cents higher -- so "normalised
        # 0.55-0.60" is paid at 0.58-0.62, and a 54% event bought at 0.60
        # needs 60% to break even.  Registered, NOT acted on: the contrast is
        # +17.2 points where this module's own measurement puts a searched
        # cell's chance threshold at +15.3 for one cell fixed in advance and
        # +24.0 at the 95th percentile, so the sample that suggested it does
        # not clear its own bar.  The forward window decides.
        "at_registration": "+17.2 点, p=0.690 (0.40-0.55 +7.1% n=391 / >=0.55"
                            " -10.1% n=59, 顶档前后半 -11.0%/-9.8%, 27 事件日)",
        "pool": "firings",
        "treatment": lambda r: (
            r.get("market_share") is not None and float(r["market_share"]) < 0.55
        ),
        "control": lambda r: (
            r.get("market_share") is not None and float(r["market_share"]) >= 0.55
        ),
    },
    {
        "hypothesis_id": "stage_gradient_unconditional_v1",
        "title": "阶段梯度（去掉名额选择）：每个触发行都算，P1 是否高于 P0",
        "registered_at_utc": "2026-09-20",
        "prediction": "ROI(P1) - ROI(P0) > 0",
        # The same question as stage_gradient_v1 without the slot's selection
        # in it.  Registered 2026-09-20 because the user asked whether the
        # lane should open at P0 and P0-M at all, and the grain cannot answer
        # it: the grain's +11.4 points
        # (p=0.109) become +3.6 [-14.1%, +22.9%], p=0.273, over every firing row.
        "at_registration": "+3.6 点 [-14.1%,+22.9%], p=0.273 (P0 +3.9% n=169 /"
                            " P0-M +3.6% n=153 / P1 +7.5% n=128, 37 事件日)",
        "pool": "firings",
        "treatment": lambda r: r["stage"] == "P1",
        "control": lambda r: r["stage"] == "P0",
    },
    {
        "hypothesis_id": "slot_to_p1_contested_v1",
        "title": "名额让给 P1 值不值：同站日 P1 与更早阶段都触发时，P1 那条对名额实际拿到的那条",
        "registered_at_utc": "2026-09-20",
        "prediction": "ROI(P1) - ROI(名额拿到的那条) > 0",
        # The policy question, paired on the same weather: these are the only
        # station-days where "buy at P1 instead" is even a choice.  A positive
        # forward reading is what would justify holding the slot back for P1;
        # 2026-09-20 it reads the
        # other way, which is why the lane keeps opening at P0-M.
        "at_registration": "-16.6 点 [-43.3%,+11.6%], p=0.801 (名额拿到的 +5.7% /"
                            " 同站日 P1 -10.9%, 38 对 / 23 事件日) -- 与预测反向",
        "pool": "contested_p1",
        "treatment": lambda r: r["stage"] == "P1",
        "control": lambda r: bool(r.get("is_slot")) and r["stage"] != "P1",
    },
)


# --------------------------------------------------------------------------
# Schema.


def _replay_ddl(prefix: str) -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS {prefix}{REPLAY_TABLE}("
        " station_id TEXT NOT NULL,"
        " event_date TEXT NOT NULL,"
        " stage TEXT NOT NULL,"
        " decision_at_utc TEXT NOT NULL,"
        " rule_version TEXT NOT NULL,"
        " bucket TEXT NOT NULL,"
        " settled_bucket TEXT NOT NULL,"
        " hit INTEGER NOT NULL CHECK(hit IN (0,1)),"
        " market_share REAL, model_share REAL, market_mode_share REAL,"
        " mode_distance INTEGER, buckets_in_common INTEGER,"
        " best_ask REAL, best_bid REAL, spread REAL, ask_depth_shares REAL,"
        " market_move_on_model_mode REAL, prev_gap_minutes REAL,"
        " prev_market_share REAL, prev_model_mode TEXT,"
        " content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),"
        " refreshed_at_utc TEXT NOT NULL,"
        " PRIMARY KEY(station_id,event_date,stage)"
        ")"
    )


def _replay_index_ddl(prefix: str) -> str:
    return (
        f"CREATE INDEX IF NOT EXISTS {prefix}{REPLAY_TABLE}_day_stage_idx"
        f" ON {REPLAY_TABLE}(event_date,stage)"
    )


def _hypothesis_ddl(prefix: str) -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS {prefix}{HYPOTHESIS_TABLE}("
        " hypothesis_id TEXT NOT NULL,"
        " as_of_date TEXT NOT NULL,"
        " registered_at_utc TEXT NOT NULL,"
        " title TEXT NOT NULL,"
        " prediction TEXT NOT NULL,"
        " at_registration TEXT NOT NULL,"
        " reading_window TEXT NOT NULL CHECK(reading_window IN ('all','forward')),"
        " n_treatment INTEGER NOT NULL,"
        " n_control INTEGER NOT NULL,"
        " event_days INTEGER NOT NULL,"
        " roi_treatment REAL, roi_control REAL, contrast REAL,"
        " ci_lo REAL, ci_hi REAL, p_at_or_below_zero REAL, p_permutation REAL,"
        " content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),"
        " computed_at_utc TEXT NOT NULL,"
        " PRIMARY KEY(hypothesis_id,as_of_date,reading_window)"
        ")"
    )


def open_shadow_connection(
    *, writable: bool, ssot_path: str | Path | None = None,
    shadow_path: str | Path | None = None, timeout: float = 120.0,
) -> sqlite3.Connection:
    """SSOT read-only with the shadow file attached.  The replay never writes
    to the store it reads."""

    ssot = Path(ssot_path or PRODUCTION_SSOT_DB)
    shadow = Path(shadow_path or SHADOW_DB)
    conn = sqlite3.connect(f"file:{ssot}?mode=ro", uri=True, timeout=timeout)
    try:
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        if not writable and not shadow.is_file():
            raise FileNotFoundError(str(shadow))
        conn.execute(f"ATTACH DATABASE ? AS {SHADOW_SCHEMA}", (str(shadow),))
        if writable:
            conn.execute(f"PRAGMA {SHADOW_SCHEMA}.journal_mode=WAL")
            ensure_shadow_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def ensure_shadow_schema(conn: sqlite3.Connection, *, prefix: str | None = None) -> None:
    """Statement by statement, never ``executescript``: the caller may be
    inside a transaction.  ``prefix`` lets a fixture own the tables in its
    own ``main`` instead of the attached file."""

    where = f"{SHADOW_SCHEMA}." if prefix is None else prefix
    conn.execute(_replay_ddl(where))
    conn.execute(_hypothesis_ddl(where))
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {where}{REPLAY_TABLE}_day_stage_idx"
        f" ON {REPLAY_TABLE}(event_date,stage)"
    )


def attach_shadow(conn: sqlite3.Connection, *, shadow_path: str | Path | None = None) -> bool:
    """Make the shadow tables readable on an existing SSOT connection."""

    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return False
    if any(str(row[1]) == SHADOW_SCHEMA for row in rows):
        return True
    shadow = Path(shadow_path or SHADOW_DB)
    if not shadow.is_file():
        return False
    try:
        conn.execute(f"ATTACH DATABASE ? AS {SHADOW_SCHEMA}", (str(shadow),))
    except sqlite3.Error:
        return False
    return True


# --------------------------------------------------------------------------
# The replay.


def settled_buckets(
    conn: sqlite3.Connection, *, floor_event_date: str = DEFAULT_FLOOR_EVENT_DATE,
) -> dict[tuple[str, str], str]:
    """Which station-days have settled, on which rung, by the settler's own
    two functions.  The materialised label table lags -- on 2026-09-20 it
    carried 18 of 09-19's 45 stations while the truth lane held all 49 -- so
    the resolver comes first and the table only fills what it cannot see."""

    out: dict[tuple[str, str], str] = {}
    rows, _counts = standing_labels(
        conn, floor_event_date=floor_event_date, only_unlabelled=False,
    )
    for station, day, _rev, bucket, *_rest in rows:
        if bucket:
            out[(str(station), str(day))] = str(bucket)
    try:
        for station, day, bucket in conn.execute(
            "SELECT station_id,event_date,settled_bucket"
            " FROM basket_station_day_settlement_label WHERE event_date>=?",
            (floor_event_date,),
        ):
            if bucket:
                out.setdefault((str(station), str(day)), str(bucket))
    except sqlite3.Error:
        pass
    return out


def replay_cell(
    conn: sqlite3.Connection, *,
    floor_event_date: str = DEFAULT_FLOOR_EVENT_DATE,
    rule: str = lane.RULE_V21,
    min_share: float = 0.40, max_share: float = 0.60, max_spread: float = 0.031,
) -> list[dict[str, Any]]:
    """Every FIRST decision of a stage that the rule fires on, scored.

    The lane only ever judges a stage's first decision, so the replay does
    too; anything else would be a different population from the one the
    trades come from.  ``rule`` defaults to v2.1's predicate, which v2.2
    shares -- the two differ in how the order is placed, which the replay
    does not model, so the price recorded is the book's, not a fill.
    """

    labels = settled_buckets(conn, floor_event_date=floor_event_date)
    firsts = conn.execute(
        "SELECT station_id,event_date,stage,MIN(decision_at_utc),probability_envelope_id,ws_snapshot_id"
        " FROM basket_policy_training_fact WHERE event_date>=?"
        " GROUP BY station_id,event_date,stage",
        (floor_event_date,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for station, day, stage, decided, envelope_id, snapshot_id in firsts:
        settled = labels.get((str(station), str(day)))
        if settled is None:
            continue
        envelope = lane._envelope(conn, str(envelope_id))
        snapshot = lane._snapshot(conn, str(snapshot_id))
        if not envelope or not envelope.get("probabilities") or not snapshot or not snapshot.get("buckets"):
            continue
        market = market_reference_v1(snapshot["buckets"])
        if market.get("state") != "complete":
            continue
        pre = lane.evaluate_rule(
            envelope["probabilities"], envelope["ordinals"], market["vector"],
            min_market_share=min_share, max_market_share=max_share, rule=lane.RULE_V2,
        )
        mode = pre.get("model_mode")
        if not mode:
            continue
        book = lane.book_features(
            next((r for r in snapshot["buckets"] if str(r.get("bucket")) == str(mode)), None)
        )
        reading = lane.evaluate_rule(
            envelope["probabilities"], envelope["ordinals"], market["vector"],
            min_market_share=min_share, max_market_share=max_share, rule=rule,
            spread=book["spread"], max_spread=max_spread,
        )
        if not reading["fired"]:
            continue
        move = lane.market_move_features(
            conn, {"station_id": str(station), "event_date": str(day)},
            {"decision_at_utc": str(decided)}, envelope["ordinals"], str(mode),
            reading.get("market_share_on_model_mode"),
        )
        row = {
            "station_id": str(station), "event_date": str(day), "stage": str(stage),
            "decision_at_utc": str(decided), "rule_version": lane.rule_version_for(rule),
            "bucket": str(mode), "settled_bucket": settled, "hit": int(settled == str(mode)),
            "market_share": reading.get("market_share_on_model_mode"),
            "model_share": reading.get("model_share_on_model_mode"),
            "market_mode_share": reading.get("market_mode_share"),
            "mode_distance": reading.get("mode_distance"),
            "buckets_in_common": reading.get("buckets_in_common"),
            "best_ask": book["best_ask"], "best_bid": book["best_bid"],
            "spread": book["spread"], "ask_depth_shares": book["ask_depth_shares"],
            "market_move_on_model_mode": move.get("market_move_on_model_mode"),
            "prev_gap_minutes": move.get("prev_gap_minutes"),
            "prev_market_share": move.get("prev_market_share_on_model_mode"),
            "prev_model_mode": move.get("prev_model_mode"),
        }
        row["content_sha256"] = content_sha256(row)
        out.append(row)
    return out


def one_per_station_day(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The grain the lane buys on: the earliest firing stage of a station-day.

    A chain is a station-day (2026-09-09's ruling); counting P0, P0-M and P1
    of one day as three would weight a day that fires three times triple.
    """

    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: (r["station_id"], r["event_date"], r["decision_at_utc"])):
        key = (row["station_id"], row["event_date"])
        if key in seen or row.get("best_ask") is None:
            continue
        seen.add(key)
        out.append(dict(row))
    return out


# --------------------------------------------------------------------------
# The readings.


def _roi(rows: Sequence[Mapping[str, Any]]) -> float | None:
    values = [
        (float(r["hit"]) - float(r["best_ask"])) / float(r["best_ask"])
        for r in rows if r.get("best_ask")
    ]
    return st.mean(values) if values else None


def day_clustered_interval(
    rows: Sequence[Mapping[str, Any]], *, control: Sequence[Mapping[str, Any]] | None = None,
    draws: int = BOOTSTRAP_DRAWS, seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """A 90% interval on the ROI, or on the contrast against ``control``,
    resampling EVENT DAYS.

    Stations of one day share their weather, so resampling rows would call a
    day's worth of correlated outcomes independent evidence -- the reading
    that pooling stations manufactures.  Fewer than three event days gets no
    interval at all rather than a narrow one.
    """

    by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_day[str(row["event_date"])].append(row)
    ctrl_by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in control or ():
        ctrl_by_day[str(row["event_date"])].append(row)
    days = sorted(set(by_day) | set(ctrl_by_day))
    point = _roi(rows)
    if control is not None:
        other = _roi(control)
        point = None if (point is None or other is None) else point - other
    out: dict[str, Any] = {"point": point, "event_days": len(days), "ci_lo": None,
                           "ci_hi": None, "p_at_or_below_zero": None}
    if len(days) < 3 or point is None:
        return out
    rng = random.Random(seed)
    got: list[float] = []
    for _ in range(draws):
        picked = [rng.choice(days) for _ in days]
        pool = [r for d in picked for r in by_day.get(d, ())]
        value = _roi(pool)
        if value is None:
            continue
        if control is not None:
            other_pool = [r for d in picked for r in ctrl_by_day.get(d, ())]
            other = _roi(other_pool)
            if other is None:
                continue
            value = value - other
        got.append(value)
    if len(got) < draws // 10:
        return out
    got.sort()
    out["ci_lo"] = got[int(0.05 * len(got))]
    out["ci_hi"] = got[int(0.95 * len(got))]
    out["p_at_or_below_zero"] = sum(1 for x in got if x <= 0.0) / len(got)
    return out


def permutation_p(
    treatment: Sequence[Mapping[str, Any]], control: Sequence[Mapping[str, Any]],
    *, draws: int = BOOTSTRAP_DRAWS, seed: int = BOOTSTRAP_SEED,
) -> float | None:
    """How often chance alone reaches this contrast.

    The hit labels are shuffled INSIDE their event day, so a day's own
    difficulty is held fixed and only the assignment to the two arms moves.
    This is the statistic a contrast has to answer to: the day-clustered
    bootstrap asks whether the treatment arm makes money, which a subset of
    a profitable cell does by inheritance.  On 2026-09-20 the two disagreed
    on 「P1 且靠拢」 -- bootstrap 0.10, permutation 0.320 -- and the
    permutation is the one about the split.
    """

    if not treatment or not control:
        return None
    by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    arm: dict[int, int] = {}
    for row in treatment:
        by_day[str(row["event_date"])].append(row); arm[id(row)] = 1
    for row in control:
        by_day[str(row["event_date"])].append(row); arm[id(row)] = 0
    # Shuffling inside a day can only move a label between the arms on days
    # where BOTH are present.  Arms that never share an event day leave every
    # draw equal to the observed contrast and the test returns 1.00, which
    # reads as "certainly nothing" when it means "this test has no power
    # here".  Say so instead.
    if not any(
        any(arm[id(r)] for r in rows) and any(not arm[id(r)] for r in rows)
        for rows in by_day.values()
    ):
        return None
    observed = (_roi(treatment) or 0.0) - (_roi(control) or 0.0)
    rng = random.Random(seed)
    reached = 0
    for _ in range(draws):
        a_num = a_den = b_num = b_den = 0.0
        for rows in by_day.values():
            hits = [float(r["hit"]) for r in rows]
            rng.shuffle(hits)
            for row, hit in zip(rows, hits):
                ask = float(row["best_ask"])
                value = (hit - ask) / ask
                if arm[id(row)]:
                    a_num += value; a_den += 1.0
                else:
                    b_num += value; b_den += 1.0
        if a_den and b_den and (a_num / a_den - b_num / b_den) >= observed:
            reached += 1
    return reached / draws


def hypothesis_readings(
    rows: Sequence[Mapping[str, Any]], *, hypotheses: Sequence[Mapping[str, Any]] = HYPOTHESES,
) -> list[dict[str, Any]]:
    """Every registered hypothesis, read twice: over the whole replay, and
    over the event days AFTER it was registered.

    The forward reading is the one that decides.  The full-history one is
    what the hypothesis was written from, and a number cannot both suggest a
    hypothesis and confirm it.
    """

    pools = pools_of(rows)
    out: list[dict[str, Any]] = []
    for spec in hypotheses:
        registered = str(spec["registered_at_utc"])[:10]
        population = pools[str(spec.get("pool") or "grain")]
        for window, pool in (
            ("all", population),
            ("forward", [r for r in population if str(r["event_date"]) > registered]),
        ):
            treat = [r for r in pool if spec["treatment"](r)]
            ctrl = [r for r in pool if spec["control"](r)] if spec["control"] else []
            stats = day_clustered_interval(
                treat, control=(ctrl if spec["control"] else None),
            )
            out.append({
                "hypothesis_id": str(spec["hypothesis_id"]),
                "registered_at_utc": registered,
                "title": str(spec["title"]),
                "prediction": str(spec["prediction"]),
                "at_registration": str(spec["at_registration"]),
                "reading_window": window,
                "n_treatment": len(treat), "n_control": len(ctrl),
                "event_days": stats["event_days"],
                "roi_treatment": _roi(treat),
                "roi_control": (_roi(ctrl) if spec["control"] else None),
                "contrast": stats["point"],
                "ci_lo": stats["ci_lo"], "ci_hi": stats["ci_hi"],
                "p_at_or_below_zero": stats["p_at_or_below_zero"],
                "p_permutation": (
                    permutation_p(treat, ctrl) if spec["control"] else None
                ),
            })
    return out


# --------------------------------------------------------------------------
# The refresh.


def refresh(
    conn: sqlite3.Connection, *, now_utc: str | None = None,
    floor_event_date: str = DEFAULT_FLOOR_EVENT_DATE, prefix: str | None = None,
) -> dict[str, Any]:
    """Replay the rule, store it, and score every registered hypothesis.

    The replay is upserted on its own key: a station-day settles once, and
    re-deriving it every day must not append.  The hypothesis readings ARE
    dated -- one row per hypothesis per day per window -- because watching
    the forward interval close is the whole point.
    """

    stamp = now_utc or utc_now()
    where = f"{SHADOW_SCHEMA}." if prefix is None else prefix
    ensure_shadow_schema(conn, prefix=prefix)
    rows = replay_cell(conn, floor_event_date=floor_event_date)
    columns = [
        "station_id", "event_date", "stage", "decision_at_utc", "rule_version", "bucket",
        "settled_bucket", "hit", "market_share", "model_share", "market_mode_share",
        "mode_distance", "buckets_in_common", "best_ask", "best_bid", "spread",
        "ask_depth_shares", "market_move_on_model_mode", "prev_gap_minutes",
        "prev_market_share", "prev_model_mode", "content_sha256",
    ]
    marks = ",".join("?" * (len(columns) + 1))
    conn.executemany(
        f"INSERT INTO {where}{REPLAY_TABLE}({','.join(columns)},refreshed_at_utc)"
        f" VALUES({marks})"
        " ON CONFLICT(station_id,event_date,stage) DO UPDATE SET"
        "  settled_bucket=excluded.settled_bucket, hit=excluded.hit,"
        "  content_sha256=excluded.content_sha256, refreshed_at_utc=excluded.refreshed_at_utc"
        " WHERE " + f"{REPLAY_TABLE}.content_sha256<>excluded.content_sha256",
        [tuple(row[c] for c in columns) + (stamp,) for row in rows],
    )
    readings = hypothesis_readings(rows)
    as_of = stamp[:10]
    for reading in readings:
        body = {k: v for k, v in reading.items() if k != "content_sha256"}
        conn.execute(
            f"INSERT INTO {where}{HYPOTHESIS_TABLE}("
            " hypothesis_id,as_of_date,registered_at_utc,title,prediction,at_registration,"
            " reading_window,n_treatment,n_control,event_days,roi_treatment,roi_control,"
            " contrast,ci_lo,ci_hi,p_at_or_below_zero,p_permutation,"
            " content_sha256,computed_at_utc)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(hypothesis_id,as_of_date,reading_window) DO UPDATE SET"
            "  n_treatment=excluded.n_treatment, n_control=excluded.n_control,"
            "  event_days=excluded.event_days, roi_treatment=excluded.roi_treatment,"
            "  roi_control=excluded.roi_control, contrast=excluded.contrast,"
            "  ci_lo=excluded.ci_lo, ci_hi=excluded.ci_hi,"
            "  p_at_or_below_zero=excluded.p_at_or_below_zero,"
            "  p_permutation=excluded.p_permutation,"
            "  content_sha256=excluded.content_sha256, computed_at_utc=excluded.computed_at_utc",
            (reading["hypothesis_id"], as_of, reading["registered_at_utc"], reading["title"],
             reading["prediction"], reading["at_registration"], reading["reading_window"],
             reading["n_treatment"], reading["n_control"], reading["event_days"],
             reading["roi_treatment"], reading["roi_control"], reading["contrast"],
             reading["ci_lo"], reading["ci_hi"], reading["p_at_or_below_zero"],
             reading["p_permutation"], content_sha256(body), stamp),
        )
    conn.commit()
    grain = one_per_station_day(rows)
    return {
        "schema": "tiebreak_cell_shadow_refresh_v1",
        "as_of_date": as_of, "refreshed_at_utc": stamp,
        "replayed": len(rows), "station_days": len(grain),
        "event_days": len({r["event_date"] for r in rows}),
        "hypotheses": len(HYPOTHESES), "readings": len(readings),
    }


__all__ = [
    "DEFAULT_FLOOR_EVENT_DATE", "HYPOTHESES", "HYPOTHESIS_TABLE", "REPLAY_TABLE",
    "POOLS", "SHADOW_DB", "SHADOW_SCHEMA", "attach_shadow", "day_clustered_interval",
    "ensure_shadow_schema", "hypothesis_readings", "one_per_station_day",
    "permutation_p", "pools_of",
    "open_shadow_connection", "refresh", "replay_cell", "settled_buckets", "utc_now",
]
