#!/usr/bin/env python3
"""Read-only dashboard for the model -> calibration -> serving chain.

Answers, in one page and in plain language: is each model still arriving,
is each calibration layer fitted and current, what got promoted and what is
still accumulating, where the data has holes, and which link of the chain
is the one holding everything else up.

Read-only by construction: opens the database read-only and writes nothing.

    python3 scripts/calibration_dashboard.py            # serve on :8787
    python3 scripts/calibration_dashboard.py --json     # print the payload
    python3 scripts/calibration_dashboard.py --html-out page.html
"""

from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import importlib.util
import inspect
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
from bisect import bisect_left
from collections import Counter, defaultdict
import random
import sqlite3
import time
import sys
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from collections.abc import Mapping, Sequence
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]

# This file as it stood when the process loaded it, for the section cache's
# fingerprints -- see ``_loaded_source``.  Read once, here, before any render
# work starts.
try:
    _LOADED_SOURCE_LINES: list[str] = Path(__file__).read_text(
        encoding="utf-8"
    ).splitlines(keepends=True)
except OSError:  # pragma: no cover - a module that cannot read itself
    _LOADED_SOURCE_LINES = []
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from paper_trading_ledger import (  # noqa: E402
    DUST_COST_FLOOR,
    INITIAL_CAPITAL_USDC,
)
# The live book (user ruling 2026-09-16: the paper ledger is a rule
# comparator; every reading of whether money was made reads live fills).
# Its settled-bucket resolver used to live here as ``_live_settled_buckets``
# and keeps that name, so the listing and the money sections still call one
# resolver.
import live_book  # noqa: E402
from live_book import settled_buckets as _live_settled_buckets  # noqa: E402
from bayes_basket_target.strategy_lanes import (  # noqa: E402
    load_strategy_lanes as _load_strategy_lanes,
)
# The selection readings -- the top strip's 选桶 row, 逐站汇总（跨日）,
# 没建仓是因为什么, 覆盖 · 价格 · EV, 篮子命中率 -- off production's decisions
# and the live fills (same ruling).  The paper selection payload they read
# until then is no longer built here.
import live_selection  # noqa: E402
from selection_market_dashboard import (  # noqa: E402
    STATION_LABELS,
)
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STATUS_JSON = ROOT / "data" / "weather_basket_yes_status.json"
TRUTH_SWEEP_STATUS = ROOT / "data" / "settlement_truth_sweep_status.json"


def _bridge_envelope_payload(conn, checkpoint_id, sha, stored) -> dict:
    """History rows since 2026-08-26 carry no inline envelope body."""

    from bayes_basket_target.probability_bridge import (  # noqa: PLC0415
        history_envelope_payload,
    )
    return history_envelope_payload(
        conn,
        checkpoint_id=str(checkpoint_id),
        envelope_content_sha256=str(sha),
        stored=str(stored) if stored else None,
    )

# The four native families that can hold a P0 seat, with the archive each
# one is fed from and whether a missed day can ever be recovered.
MEMBER_FAMILIES = (
    ("ECMWF_IFS_ENS_OPEN_PF50", "ECMWF", "ECMWF_OPEN_DATA_S3", True),
    ("NOAA_GEFS_05", "GEFS", "NOAA_GEFS_PDS_S3", True),
    ("ECCC_GEM_GLOBAL_ENS", "ECCC", "ECCC_DD_WEATHER", True),
    ("DWD_ICON_GLOBAL_EPS", "ICON", "DWD_OPENDATA_ICON_EPS", False),
)
STAGE_MEANING = {
    "P0": "开仓：模型融合出的第一版概率，交易据此建仓",
    "P0-M": "确认：当天早上的机场人工预报（TAF）对 P0 的确认或修正",
    "P1": "盯盘：当天实测气温不断进来后对概率的滚动更新",
}

# How far back the model-skill sections score.  Bounded rather than
# open-ended: the member table is append-only and grows by tens of thousands
# of rows a day, and this is the one read on the page that has to touch all
# of it -- an unbounded window makes the render slower every day it runs,
# forever.  Anchored on the newest settled truth rather than on the wall
# clock, so a stalled truth lane shows a thin window instead of an empty one.
SKILL_WINDOW_DAYS = 60
# The lead axis is the hour bucket, derived here from each run's own
# initialisation against each station's own local day start -- never the
# stored d0/d1/d2 label.  Those two disagree badly: measured over this same
# archive, d1 and d2 overlap across 18-31h and d2 alone spans 37 hours, so
# grouping skill by the calendar label mixes runs issued a day apart.
LEAD_LABELS = (
    ("h00_12", "0–12 小时"),
    ("h12_24", "12–24 小时"),
    ("h24_48", "24–48 小时"),
    ("h48_plus", "48 小时以上"),
)


def _odds_from_mean_cost(costs: list[float]) -> float | None:
    """Net odds implied by an average ticket price.

    Never the average of each ticket's own ``(1-c)/c``.  That function is
    convex, so the mean of it is dominated by whichever basket was
    cheapest rather than by the money at stake: over this book on
    2026-09-10 the per-row mean was 14.7x and the money bought 1.53x, and
    one day's row printed 32.8x.  Averaging the price first and inverting
    once gives the odds a reader can multiply their stake by.
    """

    priced = [c for c in costs if c and c > 0]
    if not priced:
        return None
    mean = sum(priced) / len(priced)
    return (1.0 - mean) / mean if mean > 0 else None


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_hours(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        text = str(stamp).replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (_now() - moment).total_seconds() / 3600.0


#: Lane-level failures worth waking someone for, keyed by the ingress the
#: resident publishes them under.  Per-candidate refusals are deliberately
#: NOT here: "no solvable support window" is the objective declining a
#: station-day, which is ordinary business and already has its own section.
#: What belongs here is a lane that could not run at all.
_PRODUCTION_LANES = (
    ("resident_probability_chain", "概率链"),
    ("resident_p0_ingress", "P0"),
    ("resident_p0m_ingress", "P0-M"),
    ("resident_p1_ingress", "P1"),
    ("market_ingress", "行情入口"),
    ("native_ensemble_ingress", "模型入口"),
    ("regime_ingress", "政权入口"),
)


# The NWP rotation advances only when the forecast lane wins a writer turn,
# and a lane that has stopped winning them is the failure this reads for.
# Three consecutive turns missed is not a slow lane, it is a stopped one; six
# is a whole lap's worth of turns lost, and the lap is what has to fit inside
# the P0 freshness window.
NWP_POLL_NOMINAL_SEC = 600.0
NWP_ROTATION_STALLED_SEC = 3.0 * NWP_POLL_NOMINAL_SEC
NWP_ROTATION_STOPPED_SEC = 6.0 * NWP_POLL_NOMINAL_SEC
NWP_ROTATION_STATE = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "rotation_state.json"
)


def _nwp_rotation_health(status: Mapping[str, Any]) -> dict[str, Any]:
    """Whether the official-native rotation is still advancing at all.

    Read from the rotation state file, not from the lane's own receipt.  The
    receipt is published inside ``run_target_forecast_refresh_cycle``, which
    is reached only when the forecast lane wins a writer turn against the
    production cycle and the hosted sweeps -- so the one state it cannot
    report is the one that matters, and it reports it by being absent.
    Measured 2026-09-09: the resident restarted 12:35:22Z and eighteen
    minutes later the lane had taken no turn at all, sweep_health unpublished
    and the cursor unset, while 2026-09-10's 45 packets stayed expired.  The
    same shape as the TAF card two hours earlier, and the same remedy: read
    the file that outlives the process.

    This is the direct measure of the thing that went wrong.  A date's
    packets are re-captured only when the cursor reaches one of its scopes,
    so how long the cursor has been still IS how long re-capture has been
    impossible -- no samples, no median, no contention factor required.
    """

    try:
        rotation = json.loads(NWP_ROTATION_STATE.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(rotation, Mapping):
        return {}
    age_hours = _age_hours(str(rotation.get("updated_at_utc") or ""))
    if age_hours is None:
        return {}
    age_sec = age_hours * 3600.0
    health = (
        "red" if age_sec >= NWP_ROTATION_STOPPED_SEC
        else "amber" if age_sec >= NWP_ROTATION_STALLED_SEC
        else "green"
    )
    note = (
        f"轮转已 {age_sec / 60:.0f} 分钟没有推进"
        f"（名义 {NWP_POLL_NOMINAL_SEC / 60:.0f} 分钟一轮，"
        f"{int(rotation.get('catalog_scope_count') or 0)} 个 scope）"
    )
    # The lane's own reading when it has one: the lap it actually achieves,
    # which the file cannot say because the file only records advances.
    prefetch = status.get("forecast_prefetch")
    prefetch = prefetch if isinstance(prefetch, Mapping) else {}
    capture = prefetch.get("resident_nwp_capture")
    capture = capture if isinstance(capture, Mapping) else {}
    sweep = capture.get("sweep_health")
    if isinstance(sweep, Mapping) and sweep.get("sweep_achieved_hours"):
        # Measured or declared, said in the word.  With no interval samples
        # the reading falls back to the nominal 600s, and calling that "一圈
        # 实测" would present the declaration as the measurement -- which is
        # the exact substitution this whole card was built after.  Samples
        # need two polls inside one process, so every restart starts here.
        measured = int(sweep.get("poll_interval_sample_count") or 0) > 0
        note += (
            f"；一圈{'实测' if measured else '按名义推算'}"
            f" {float(sweep['sweep_achieved_hours']):.1f}h"
            f"（新鲜期 {float(sweep.get('p0_freshness_hours') or 0):.0f}h"
        )
        note += (
            f"，争用 {float(sweep.get('contention_factor') or 0):.1f}×）"
            if measured else "，尚无实测轮次间隔）"
        )
        if (
            measured
            and sweep.get("fits_freshness_window") is False
            and health == "green"
        ):
            health = "amber"
    return {
        "nwp_rotation_health": health,
        "nwp_rotation_note": note,
        "nwp_rotation_age_minutes": round(age_sec / 60.0, 1),
    }


# A capture lane that has produced NOTHING is a different failure from a
# family that is behind, and until 2026-09-10 the page could only show the
# second.  Measured that day: ECCC's newest run read 32.6h old and the card
# said "one family is late" -- but the lane had written no archive for ANY
# family between 09-09T15:23Z and 09-10T02:09Z, ten hours and forty-six
# minutes in which nothing was asked.  Family age cannot say that: each
# family's own cadence absorbs it (ECMWF looked fine at 8.6h because its
# next run had since landed), and the one number that says it out loud is
# how long since the lane last landed ANYTHING.
#
# Against the P0 freshness window, not an invented threshold: a lane idle
# for longer than that window has, by then, let every packet it holds
# expire.  Half the window is the point where the next lap has to start.
NATIVE_CAPTURE_IDLE_STALLED_HOURS = 3.0
NATIVE_CAPTURE_IDLE_STOPPED_HOURS = 6.0


def _official_native_family_coverage(
    status: Mapping[str, Any],
) -> dict[str, Any]:
    """How many of the four families each serving packet actually carries.

    Counted off the inbox the compiler reads, not off the member ledger and
    not off the worker's receipt.  The ledger answers a different question
    (when did this family last land anything, anywhere) and absorbs a
    shortfall inside each family's own cadence; the receipt is published
    from inside a poll whose shape varies -- ``official_native_capture``
    is present on a per-scope poll and absent on a rotation-level one -- so
    a page rendered on the wrong poll would show nothing and read as fine.
    The inbox is a file, it outlives every process, and it is the thing P0
    is actually served from.

    This is the number that was missing on 2026-09-10.  The strip said
    "3/4 families arriving, oldest ECCC 32.6h" while the 2026-09-12 packets
    -- 37 of them, the ones a D+2 decision would read -- carried exactly
    two families, and nothing on the page said so.  The worker had recorded
    who held on every pass since ``families_held_on_earlier_run`` was
    written; no consumer had ever read it.

    Reported, not judged.  A family whose own run for a lead has not been
    issued yet is holding for the right reason -- 2026-09-10T08:41Z, ECCC
    held ``eccc_geps_archive_scope_invalid`` on a 12Z run the clock had not
    reached -- and a D+2 date legitimately runs short of families for most
    of the day.  Colouring the strip on that would light it every morning,
    and a light that is always on is not a light.
    """

    try:
        inbox = json.loads(NATIVE_FORWARD_INBOX.read_text())
    except (OSError, ValueError):
        return {}
    packets = inbox.get("packets") if isinstance(inbox, Mapping) else None
    if not isinstance(packets, list) or not packets:
        return {}
    by_system = {
        system_id: label for system_id, label, _t, _b in MEMBER_FAMILIES
    }
    per_date: dict[str, dict[str, Any]] = {}
    for packet in packets:
        if not isinstance(packet, Mapping):
            continue
        event_date = str(packet.get("event_date") or "")
        if not event_date:
            continue
        row = per_date.setdefault(event_date, {
            "event_date": event_date,
            "packets": 0,
            "families": set(),
            "newest_received_utc": "",
        })
        row["packets"] += 1
        for system in packet.get("systems") or []:
            system_id = str(
                system.get("ensemble_system_id")
                if isinstance(system, Mapping) else system
            )
            # The four ensemble families only.  TAF rides the same packet
            # and is a different kind of evidence; counting it would make
            # a two-family packet read as three.
            if system_id in by_system:
                row["families"].add(by_system[system_id])
        received = str(packet.get("received_at_utc") or "")
        if received > row["newest_received_utc"]:
            row["newest_received_utc"] = received
    expected = [label for _s, label, _t, _b in MEMBER_FAMILIES]
    rows = []
    for event_date in sorted(per_date):
        row = per_date[event_date]
        present = row["families"]
        rows.append({
            "event_date": event_date,
            "packets": row["packets"],
            "family_count": len(present),
            "expected_family_count": len(expected),
            "families": [label for label in expected if label in present],
            "absent": [label for label in expected if label not in present],
            "newest_received_utc": row["newest_received_utc"],
            "received_age_hours": _age_hours(row["newest_received_utc"]),
        })
    # Why the shortest packet is short, when the worker's receipt happens to
    # be the shape that carries it.  Enrichment only: the counts above stand
    # without it, which is the point of counting them off the file.
    prefetch = status.get("forecast_prefetch")
    prefetch = prefetch if isinstance(prefetch, Mapping) else {}
    capture = prefetch.get("resident_nwp_capture")
    capture = capture if isinstance(capture, Mapping) else {}
    official = capture.get("official_native_capture")
    official = official if isinstance(official, Mapping) else {}
    last = official.get("last_completed")
    last = last if isinstance(last, Mapping) else {}
    held_raw = last.get("families_held_on_earlier_run")
    held_raw = held_raw if isinstance(held_raw, Mapping) else {}
    by_transport = {
        transport: label for _s, label, transport, _b in MEMBER_FAMILIES
    }
    held = [
        {
            "label": by_transport.get(str(name), str(name)),
            # The first line is the exception class and its message; the
            # rest is a stack of URL.  The class is what separates "not
            # issued yet" from "the fetch failed", which is the question.
            "reason": str(value).split("\n", 1)[0][:120],
        }
        for name, value in sorted(held_raw.items())
    ]
    # The light, and the one rule it follows.  A D+2 date legitimately runs
    # short for most of its first day -- ECCC's 12Z and ICON's own run land
    # in the evening -- so lighting on the farthest date would light every
    # morning.  A date at D+1 or nearer is past every family's publication
    # hour, and short there means short.  Every date's count is printed
    # either way; only the colour is restricted.
    near_floor = (_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    near_short = [
        row for row in rows
        if row["event_date"] <= near_floor
        and row["family_count"] < row["expected_family_count"]
    ]
    return {
        "native_inbox_coverage": rows,
        "native_inbox_health": "amber" if near_short else "green",
        "native_inbox_short_near_dates": [
            row["event_date"] for row in near_short
        ],
        "native_inbox_near_floor": near_floor,
        "native_inbox_held_event_date": str(last.get("event_date") or ""),
        "native_inbox_held_families": held,
    }


# A TAF round lands every 19 to 33 minutes when the lane is healthy -- the
# rotation module's own measurement, after the writers were serialised.  Two
# missed rounds is a lane worth naming; four is a lane that has stopped.
TAF_ROUND_OVERDUE_SEC = 66.0 * 60.0
TAF_ROUND_STOPPED_SEC = 132.0 * 60.0
NATIVE_FORWARD_INBOX = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "official_native_ensemble_forward_inbox.json"
)
TAF_ROTATION_STATE = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "taf_rotation_state.json"
)


def _taf_rotation_staleness() -> dict[str, Any]:
    """When the TAF poll last completed a round, from the file that outlives it."""

    try:
        rotation = json.loads(TAF_ROTATION_STATE.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(rotation, Mapping):
        return {}
    updated = str(rotation.get("updated_at_utc") or "")
    age_hours = _age_hours(updated)
    if age_hours is None:
        return {}
    age_sec = age_hours * 3600.0
    if age_sec < TAF_ROUND_OVERDUE_SEC:
        return {
            "taf_capture_health": "green",
            "taf_capture_note": f"上一轮 {age_hours:.1f} 小时前（回执待发布）",
        }
    return {
        "taf_capture_health": (
            "red" if age_sec >= TAF_ROUND_STOPPED_SEC else "amber"
        ),
        "taf_capture_note": (
            f"轮询已 {age_hours:.1f} 小时没有完成一轮"
            f"（健康时 19-33 分钟一轮）；常驻回执未发布，"
            f"读的是轮转状态文件"
        ),
    }


#: The capture-lag p90 at which the TAF card turns amber on its own.
TAF_CAPTURE_LAG_AMBER_MINUTES = 180.0


def _taf_capture_health(status: Mapping[str, Any]) -> dict[str, Any]:
    """Whether forecaster bulletins are still arriving, per station-day.

    The board reported the TAF poll -- how many scopes it selected, how it
    classed them, whether the round failed -- and never reported arrivals.
    Those are different questions and they disagreed for three days: the
    rotation selected twelve scopes a round and reported no failures while
    sixteen of forty-three stations received one bulletin a day instead of
    eight to eleven.  The consequence surfaced two layers downstream, as
    ``resident_p0m_replay_required`` on scopes whose newest TAF predated the
    model run their P0 root was built on, and nothing on this page connected
    the two.

    Reads the daemon's own receipt rather than the capture table: the age is
    measured at the poll, against the catalogue the poll actually held.
    """

    prefetch = status.get("forecast_prefetch")
    prefetch = prefetch if isinstance(prefetch, Mapping) else {}
    capture = prefetch.get("forward_taf_capture")
    capture = capture if isinstance(capture, Mapping) else {}
    freshness = capture.get("capture_freshness")
    if not isinstance(freshness, Mapping) or not freshness.get("scope_count"):
        # The receipt lives in the resident's process and is gone the moment
        # it restarts, so keying the card on it means the card DISAPPEARS --
        # not goes green, disappears -- for as long as the first round after
        # a restart takes.  Measured 2026-09-09 09:12Z, the day this card was
        # added: the lane had not completed a round since 07:34Z, two
        # restarts had happened in between, and the page rendered at 07:52Z
        # said green with no alerts at all.
        #
        # The rotation state file outlives the process and stamps every
        # completed round, so it answers the one question the receipt cannot
        # after a restart: when did this lane last run.
        return _taf_rotation_staleness()
    scopes = int(freshness.get("scope_count") or 0)
    stale = int(freshness.get("stale_scope_count") or 0)
    never = int(freshness.get("never_captured_count") or 0)
    after = float(freshness.get("stale_after_hours") or 3.0)
    worst = [
        item for item in (freshness.get("worst_scopes") or [])
        if isinstance(item, Mapping)
    ]
    # Two populations, and the card used to merge them into one sentence.
    # "Stale" is a scope that had bulletins and stopped getting them.
    # "Never captured" is a scope with no age at all -- and most of those
    # are simply a future day that has entered the catalogue, which has
    # never been captured by definition.  Merged, the line said
    # "88/111 個 scope 超過 3 小時沒有新報文（中位 0.5h，最久 1.1h）" on
    # 2026-09-10: a sentence contradicted by its own parentheses, red at
    # 88/111, while the number of scopes that had actually gone three hours
    # without a bulletin was zero.
    measured = freshness.get("measured_scope_count")
    measured = (
        int(measured) if measured is not None else max(0, scopes - never)
    )
    never_by_date = freshness.get("never_captured_by_event_date")
    never_by_date = never_by_date if isinstance(never_by_date, Mapping) else {}
    scopes_by_date = freshness.get("scope_count_by_event_date")
    scopes_by_date = (
        scopes_by_date if isinstance(scopes_by_date, Mapping) else {}
    )
    # The nearest event date in the catalogue is the day being traded now.
    # Its scopes having no bulletin at all is a supply fact; a later day's
    # is a day that has only just entered.
    nearest = min(scopes_by_date) if scopes_by_date else ""
    blind_near = int(never_by_date.get(nearest) or 0)
    near_total = int(scopes_by_date.get(nearest) or 0)
    stale_share = stale / measured if measured else 0.0
    blind_share = blind_near / near_total if near_total else 0.0
    # A third of the population quiet past an issuing cycle is a supply
    # problem, not a slow lap.  Two thirds is the lane having stopped.
    share = max(stale_share, blind_share)
    health = "red" if share >= 0.66 else "amber" if share >= 0.34 else "green"
    # Slow is not silent, and the share above cannot see it: a lane that
    # captures every bulletin hours late never has a scope past the overdue
    # line.  Measured 09-04..09-13 the lag's p90 ran two to four and a half
    # hours; three hours at p90 is a lane routinely a synoptic half-cycle
    # behind the forecaster, which is worth a light of its own.
    lag = freshness.get("capture_lag")
    lag = lag if isinstance(lag, Mapping) else {}
    lag_p90 = lag.get("p90_minutes")
    if (
        health == "green"
        and int(lag.get("sample_count") or 0) > 0
        and lag_p90 is not None
        and float(lag_p90) >= TAF_CAPTURE_LAG_AMBER_MINUTES
    ):
        health = "amber"
    missing_word = (
        "新签发的报文" if freshness.get("age_basis") == "issued" else "新的"
    )
    note = (
        f"{measured} 个 scope 有过报文，其中 {stale} 个超过 {after:.0f} 小时"
        f"没有{missing_word}"
    )
    if freshness.get("median_age_hours") is not None:
        note += f"（中位 {float(freshness['median_age_hours']):.1f}h"
        if freshness.get("max_age_hours") is not None:
            note += f"，最久 {float(freshness['max_age_hours']):.1f}h"
        note += "）"
    if stale and worst:
        note += "：" + "、".join(
            f"{item.get('scope')} {float(item.get('age_hours') or 0):.1f}h"
            for item in worst[:3]
        )
    if near_total:
        note += (
            f"；{nearest} 这一天 {near_total} 个 scope 里 "
            f"{blind_near} 个从未收到过报文"
        )
    later_blind = never - blind_near
    if later_blind > 0:
        note += f"；另有 {later_blind} 个未来日期的 scope 从未收到过（新入目录）"
    if int(lag.get("sample_count") or 0) > 0 and lag.get("p50_minutes") is not None:
        note += (
            f"；过去 {float(lag.get('lookback_hours') or 24):.0f}h 报文发布到入库"
            f"中位 {float(lag['p50_minutes']):.0f} 分钟"
            + (
                f"、p90 {float(lag_p90):.0f} 分钟" if lag_p90 is not None else ""
            )
        )
    return {
        "taf_capture_health": health,
        "taf_capture_note": note,
        "taf_capture_stale_scope_count": stale,
        "taf_capture_measured_scope_count": measured,
        "taf_capture_never_captured_count": never,
        "taf_capture_scope_count": scopes,
    }


def _production_health(status: Mapping[str, Any]) -> dict[str, Any]:
    """Whether the pipeline is producing, not merely whether it is alive.

    The heartbeat only says the process still runs its loop.  On
    2026-08-26 that was true for eleven hours while every ingress reported
    ``database is locked`` and the cycle produced nothing -- one station's
    TAF with no temperature group had taken P0-M to BLOCKED_FAIL_CLOSED,
    and an ungated third writer was holding the SQLite lock.  Both were
    plainly visible in the resident's own status file; nothing read them.
    """

    production = status.get("production")
    inflight = status.get("production_inflight")
    if not isinstance(production, Mapping):
        # A cycle in flight replaces the completed receipt with an
        # ``production_inflight`` marker, so "no receipt" is the ordinary
        # state for most of a cycle, not a fault -- but the marker carries
        # its own overdue flag, which is the stall signal this card exists
        # for.  Reading only the completed receipt made the card report
        # "未发布" on every render taken mid-cycle.
        if isinstance(inflight, Mapping):
            elapsed = inflight.get("elapsed_sec")
            overdue = bool(inflight.get("overdue"))
            seconds = (
                float(elapsed) if isinstance(elapsed, (int, float)) else None
            )
            note = (
                f"本轮进行中 {seconds:.0f} 秒"
                if seconds is not None else "本轮进行中"
            )
            if overdue:
                after = inflight.get("overdue_after_sec")
                note += (
                    f"，已超时（上限 {float(after):.0f} 秒）"
                    if isinstance(after, (int, float)) else "，已超时"
                )
            # The page is a render, not a live view: this card is read off
            # status.json when the build starts and shown until the next
            # render lands, 30-80 minutes later at current render times.
            # On 2026-09-17 "本轮进行中 1151 秒，已超时" stayed on the page
            # for half an hour after that cycle had finished (1197s at
            # 12:35Z) and a restarted resident had run four more.  A counter
            # that says when it was read is a reading; one that does not is
            # an alarm about a cycle that may no longer exist.
            seen_at = status.get("heartbeat_at")
            if isinstance(seen_at, str) and len(seen_at) >= 16:
                note += f"；状态快照 {seen_at[11:16]}Z"
            return {
                "production_health": (
                    "red" if seconds is not None and seconds > 900.0
                    else ("amber" if overdue else "green")
                ),
                "production_note": note,
                "production_state": "in_flight",
                "production_cycle_seconds": (
                    round(seconds, 1) if seconds is not None else None
                ),
                "production_blocked_lanes": [],
            }
        return {
            "production_health": "amber",
            "production_note": "常驻未发布 production receipt",
        }
    # The receipt's own verdict, before any lane is inspected.  A worker
    # that cannot open the SSOT never reaches a lane, so it publishes a
    # receipt with no lane maps at all: `state=blocked_fail_closed`,
    # `ok=false`, a reason, and `cycle_total` of a fraction of a
    # millisecond because it bailed immediately.  Every test below then
    # passes -- no blocked lanes, no stale `worker_checked_at` (the key is
    # absent on this shape), no slow cycle -- and the card printed
    # "周期正常产出" over a pipeline that had produced nothing for
    # nineteen hours.  Measured 2026-08-31: last decision 08-30T17:18Z,
    # every cycle since fail-closed on
    # `target_database_wal_checkpoint_busy`.  This function's docstring is
    # about catching exactly this and it was reading past it.
    state_word = str(production.get("state") or "").upper()
    fail_closed = (
        production.get("ok") is False
        or "BLOCK" in state_word
        or "FAIL" in state_word
    )
    if fail_closed:
        reason = str(production.get("reason") or "").strip()
        recovery = production.get("connection_recovery")
        if not reason and isinstance(recovery, Mapping):
            reason = str(recovery.get("reason") or "")
        note = "生产回执 fail-closed：" + (
            str(production.get("state") or "未命名状态")
        )
        if reason:
            # A colon chain, and a reader needs three things out of it: the
            # condition, the SUBJECT it happened to, and the innermost
            # cause.  Head-and-tail kept the first segment and the last and
            # dropped everything between -- which is exactly where the
            # subject lives.  Measured 2026-09-10, the card read
            #
            #   生产回执 fail-closed：blocked_fail_closed·
            #   official_ws_evidence_complete_scope_missing…no_registry_scope
            #
            # off a reason that ran
            # ``…scope_missing:1 skipped:<station>:ContractError:
            # …scope_missing:no_registry_scope``.  Head and tail were the
            # SAME phrase, so the line spent both its slots saying one
            # thing twice and dropped the only datum naming what to fix.
            #
            # So: drop a segment that merely repeats an earlier one, drop a
            # bare exception class sitting between two conditions, and
            # print what is left.  Head-and-tail survives as the last
            # resort for a chain still too long to show.
            parts = [seg.strip() for seg in reason.split(":") if seg.strip()]
            seen: set[str] = set()
            kept = [
                seg for seg in parts
                if not (seg in seen or seen.add(seg))
            ]
            lean = [seg for seg in kept if not seg.endswith("Error")] or kept
            joined = ":".join(lean)
            note += "·" + (
                joined if len(joined) <= 120
                else f"{lean[0][:40]}…{':'.join(lean[-2:])[:70]}"
            )
        return {
            "production_health": "red",
            "production_note": note,
            "production_state": str(production.get("state") or ""),
            "production_checked_at": production.get("worker_checked_at"),
            "production_cycle_seconds": None,
            "production_blocked_lanes": [note],
        }
    stale_hours = _age_hours(production.get("worker_checked_at"))
    blocked: list[str] = []
    locked = 0
    for key, label in _PRODUCTION_LANES:
        lane = production.get(key)
        if not isinstance(lane, Mapping):
            continue
        state = str(lane.get("state") or "").upper()
        if "BLOCK" not in state and "FAIL" not in state:
            continue
        reason = str(lane.get("reason") or "")
        detail = reason.split(":")[-1][:40] if reason else ""
        if not detail:
            # A scoped halt (P1 correction/revision, one station-day)
            # carries no lane-level reason -- the detail lives only in the
            # ``blockers`` list, and the status file is overwritten every
            # cycle.  The 2026-08-29 16:03 P1 flap was undiagnosable an
            # hour later because this note read just "P1".
            scoped = [
                b for b in lane.get("blockers") or []
                if isinstance(b, Mapping)
            ]
            if scoped:
                first = scoped[0]
                reason = str(first.get("reason") or "")
                detail = (
                    f"{first.get('station_id')}|{first.get('event_date')}"
                    f"·{reason.split(':')[-1][:40]}"
                )
                if len(scoped) > 1:
                    detail += f" 等{len(scoped)}个站日"
        if not detail:
            # The probability chain is the one lane that blocks on behalf of
            # something else.  It carries no reason and no blockers of its
            # own: it names the stage that failed in ``blocked_stages`` and
            # keeps that stage's blockers one level down.  Reading only the
            # top level printed a bare "概率链" over a fully diagnosed P0-M
            # halt -- three station-days named, in the same document.
            stages = [
                str(item) for item in lane.get("blocked_stages") or []
                if str(item or "").strip()
            ]
            for stage in stages:
                nested = lane.get(stage.replace("-", "").lower())
                scoped = [
                    b for b in (
                        nested.get("blockers")
                        if isinstance(nested, Mapping) else None
                    ) or []
                    if isinstance(b, Mapping)
                ]
                if not scoped:
                    continue
                first = scoped[0]
                nested_reason = str(first.get("reason") or "")
                detail = (
                    f"{stage}·{first.get('station_id')}"
                    f"|{first.get('event_date')}"
                    f"·{nested_reason.split(':')[-1][:40]}"
                )
                if len(scoped) > 1:
                    detail += f" 等{len(scoped)}个站日"
                break
            if not detail and stages:
                detail = "、".join(stages)[:40]
        blocked.append(f"{label}{'·' + detail if detail else ''}")
        if "locked" in reason:
            locked += 1
    notes: list[str] = []
    health = "green"
    # Partial commitment is the fleet's normal shape: some scopes always sit
    # in a soft per-station refusal.  Name the split instead of painting the
    # whole cycle red -- committed count first, then the top refusal reasons,
    # so the reader sees "the cycle works, these scopes are waiting" rather
    # than an outage banner over nine committed live decisions.
    blocked_scope_count = int(production.get("blocked_scope_count") or 0)
    committed_scope_count = int(production.get("committed_scope_count") or 0)
    if blocked_scope_count and committed_scope_count:
        reasons = production.get("blocked_scope_reasons") or {}
        top = "、".join(
            f"{BLOCKED_SCOPE_REASON_LABELS.get(name, name)}×{count}"
            for name, count in sorted(
                reasons.items(), key=lambda kv: -kv[1]
            )[:3]
        )
        health = "amber"
        notes.append(
            f"{committed_scope_count} 个 scope 落账，"
            f"{blocked_scope_count} 个逐站阻断"
            + (f"（{top}）" if top else "")
        )
    if stale_hours is not None and stale_hours > 1.0:
        health = "red"
        notes.append(f"周期已 {stale_hours:.1f} 小时没有完成")
    if locked:
        health = "red"
        notes.append(f"{locked} 条车道被写锁挡住（单写者门失效？）")
    elif blocked:
        # A blocked lane with no lock error is a genuine fault too, but the
        # kind that usually names one station -- amber, not red.  Escalate
        # only: a stale cycle already went red above and a single blocked
        # lane must not talk it back down.
        if len(blocked) >= 3:
            health = "red"
        elif health == "green":
            health = "amber"
        notes.append("阻断车道：" + "、".join(blocked[:4]))
    # Blocking, beside latency.  The card below names where the seconds
    # inside a cycle went; until 2026-09-09 nothing on this page said a word
    # about the cycles that could not start.  The two are different faults
    # with the same symptom: a round of 533s against a 300s target is worth
    # one line, and the 35-to-53 minute gaps between those rounds -- one
    # capture sweep holding the single writer for its whole 1800s subprocess
    # timeout -- were worth rather more, and got a staleness note that named
    # nobody.  Waiting is judged against the same declared numbers as
    # running: a lane made to wait longer than the whole cycle is meant to
    # take is late by definition.
    overdue_sec = float(
        (
            production.get("target_cycle_runtime")
            if isinstance(production.get("target_cycle_runtime"), Mapping)
            else {}
        ).get("overdue_seconds") or 720.0
    )
    target_for_wait = float(
        (
            production.get("target_cycle_runtime")
            if isinstance(production.get("target_cycle_runtime"), Mapping)
            else {}
        ).get("target_seconds") or 300.0
    )
    waiting = status.get("production_waiting")
    waited = status.get("production_wait")
    wait_sec = None
    wait_by: list[str] = []
    wait_word = ""
    if isinstance(waiting, Mapping) and isinstance(
        waiting.get("waiting_sec"), (int, float)
    ):
        wait_sec = float(waiting["waiting_sec"])
        wait_by = [str(x) for x in (waiting.get("blocked_by") or [])]
        wait_word = "正在等写入轮次"
    elif isinstance(waited, Mapping) and isinstance(
        waited.get("waited_sec"), (int, float)
    ):
        wait_sec = float(waited["waited_sec"])
        wait_by = [str(x) for x in (waited.get("blocked_by") or [])]
        wait_word = "上轮开始前等了"
    if wait_sec is not None and wait_sec > target_for_wait:
        if wait_sec > overdue_sec:
            health = "red"
        elif health == "green":
            health = "amber"
        notes.append(
            f"{wait_word} {wait_sec:.0f} 秒"
            + (f"（被 {'、'.join(wait_by[:3])} 占住）" if wait_by else "")
        )
    cycle_ms = (production.get("stage_timings_ms") or {}).get("cycle_total")
    # From the receipt, so the board judges against what the running daemon
    # targets rather than a number typed here.  The fallbacks are the values
    # declared 2026-09-01 and only apply to a receipt written before they
    # were published.
    cycle_runtime = production.get("target_cycle_runtime")
    cycle_runtime = (
        cycle_runtime if isinstance(cycle_runtime, Mapping) else {}
    )
    target_sec = float(cycle_runtime.get("target_seconds") or 300.0)
    warn_sec = float(cycle_runtime.get("warn_seconds") or 450.0)
    if isinstance(cycle_ms, (int, float)) and cycle_ms > warn_sec * 1000:
        # Judged against the declared target, not against a number typed here.
        # This line used to read "> 600s（常态约 200-350）" -- a threshold and
        # a normal range that agreed with nothing else in the system, so a
        # cycle drifting from 350s to 850s said nothing until it passed 600.
        # That drift happened on 2026-09-01 and took 21 of 28 scopes with it:
        # the compile loop outran a sixty-second quote lease.
        health = "amber" if health == "green" else health
        # Name where the seconds went, on the same line that says there were
        # too many of them.  An overrun used to report only its total, so
        # every diagnosis started by asking the status file a question it had
        # already answered -- and the answer was usually read instead off the
        # point-in-time statement probe, which names whichever query happened
        # to be running, not whichever stage owns the cycle.  Three weeks of
        # "fixed again" came out of that gap.  ``unattributed`` is in the
        # ranking on purpose: a stage added after the last mark shows up here
        # as the biggest consumer rather than as silence.
        stages = production.get("stage_timings_ms") or {}
        top_stages = "、".join(
            f"{name} {float(value) / 1000:.0f}s"
            for name, value in sorted(
                (
                    (str(k), float(v))
                    for k, v in stages.items()
                    if k != "cycle_total" and isinstance(v, (int, float))
                ),
                key=lambda item: -item[1],
            )[:3]
            if value >= 1000.0
        )
        notes.append(
            f"单轮 {cycle_ms / 1000:.0f} 秒"
            f"（目标 {target_sec:.0f}，告警 {warn_sec:.0f}）"
            + (f"：{top_stages}" if top_stages else "")
        )
    return {
        "production_health": health,
        "production_note": "；".join(notes) or "周期正常产出",
        "production_state": str(production.get("state") or ""),
        "production_checked_at": production.get("worker_checked_at"),
        "production_cycle_seconds": (
            round(cycle_ms / 1000.0, 1)
            if isinstance(cycle_ms, (int, float)) else None
        ),
        "production_blocked_lanes": blocked,
        "production_wait_seconds": wait_sec,
        "production_wait_blocked_by": wait_by,
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


# How many days of runs the cadence read looks back over.  Long enough that a
# family publishing twice a day still yields a handful of intervals, short
# enough that a schedule change from last month does not set the expectation
# for today.
RUN_CADENCE_WINDOW_DAYS = 6


def collect_run_cadence(
    conn: sqlite3.Connection,
) -> dict[str, list[dict[str, Any]]]:
    """Recent runs per family: when each was issued and when it landed.

    One scan of the member table serves all four families.  The table has no
    index on the run column, so a per-family query would cost a full scan
    each -- four scans of nearly a million rows to answer one question.
    """

    since = (
        _now() - timedelta(days=RUN_CADENCE_WINDOW_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    pairs = " OR ".join(
        "(ensemble_system_id=? AND transport_provider_id=?)"
        for _s, _l, _t, _b in MEMBER_FAMILIES
    )
    params: list[str] = []
    for system_id, _label, transport, _backfillable in MEMBER_FAMILIES:
        params.extend((system_id, transport))
    runs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT ensemble_system_id, native_run_init_utc,"
        " COUNT(DISTINCT station_id), MIN(archive_query_at_utc),"
        " MAX(archive_query_at_utc)"
        " FROM calibration_native_ensemble_historical_member_run"
        f" WHERE native_run_init_utc >= ? AND ({pairs})"
        " GROUP BY ensemble_system_id, native_run_init_utc"
        " ORDER BY ensemble_system_id, native_run_init_utc DESC",
        (since, *params),
    ):
        # First capture, not last: the first is when the run arrived, which
        # is what the publication delay is measured against.  The last only
        # says when the newest station of that run was topped up.
        runs[row[0]].append({
            "run_utc": row[1],
            "stations": row[2],
            "capture_utc": row[3],
            "last_capture_utc": row[4],
            "run_age_hours": _age_hours(row[1]),
            "capture_age_hours": _age_hours(row[3]),
        })
    return runs


def collect_members(
    conn: sqlite3.Connection,
    cadence: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Per family: is it still arriving, how deep is its training history.

    "Still arriving" is judged against the family's own observed rhythm, not
    a shared hour count.  The families publish on different schedules and
    land with different delays -- a fixed threshold either lets a six-hourly
    family sleep through a missed cycle or paints a twice-daily one amber
    every afternoon.
    """

    rows = []
    for system_id, label, transport, backfillable in MEMBER_FAMILIES:
        record = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT station_id),"
            " COUNT(DISTINCT target_local_date), MAX(native_run_init_utc),"
            " MAX(archive_query_at_utc)"
            " FROM calibration_native_ensemble_historical_member_run"
            " WHERE ensemble_system_id=? AND transport_provider_id=?",
            (system_id, transport),
        ).fetchone()
        seats = conn.execute(
            "SELECT COUNT(*) FROM"
            " calibration_cross_native_ensemble_selection_evidence"
            " WHERE selected_system_ids_json LIKE ?",
            (f"%{system_id}%",),
        ).fetchone()[0]
        run_age = _age_hours(record[3])
        recent = cadence.get(system_id, [])
        latest = recent[0] if recent else None
        # The gap between issue times is the family's publication rhythm; the
        # gap between issue and capture is how long its archive takes to
        # publish and us to pull.  The next run is only late once both have
        # elapsed.
        gaps = [
            a["run_age_hours"] - b["run_age_hours"]
            for a, b in zip(recent[1:], recent[:-1])
            if a["run_age_hours"] is not None and b["run_age_hours"] is not None
        ][:8]
        lags = [
            r["run_age_hours"] - r["capture_age_hours"]
            for r in recent[:8]
            if r["run_age_hours"] is not None
            and r["capture_age_hours"] is not None
        ]
        cycle_hours = _median([g for g in gaps if g > 0])
        lag_hours = _median([lag for lag in lags if lag >= 0])
        due_in_hours = None
        overdue_hours = None
        if run_age is not None and cycle_hours and lag_hours is not None:
            due_in_hours = cycle_hours + lag_hours - run_age
            overdue_hours = -due_in_hours
        if run_age is None:
            health = "red"
            note = "从未入库"
        elif overdue_hours is None:
            # Not enough history to know the rhythm -- fall back to the hour
            # count, which is all we can say without it.
            health = ("red" if run_age > 36
                      else "amber" if run_age > 18 else "green")
            note = (f"最新 run {run_age:.0f} 小时前"
                    + ("，慢于常态" if run_age > 18 else ""))
        # A run that is late by less than one whole cycle has not cost us a
        # cycle yet -- publication times wander by hours and a light that
        # comes on for that is on most of the day.  Amber is one missed
        # cycle, red is more than two.
        elif overdue_hours > 2.5 * cycle_hours:
            health = "red"
            note = (f"已漏掉 {overdue_hours / cycle_hours:.0f} 轮，"
                    f"最新 run 是 {run_age:.0f} 小时前的")
        elif overdue_hours > cycle_hours:
            health = "amber"
            note = (f"漏了一轮：按 {cycle_hours:.0f} 小时一轮，"
                    f"下一轮已晚 {overdue_hours:.1f} 小时")
        else:
            health = "green"
            note = (f"按 {cycle_hours:.0f} 小时一轮在更新"
                    if overdue_hours <= 0 else
                    f"按 {cycle_hours:.0f} 小时一轮在更新，"
                    f"下一轮晚了 {overdue_hours:.1f} 小时，仍在正常抖动内")
        rows.append({
            "label": label,
            "system_id": system_id,
            "member_rows": record[0],
            "stations": record[1],
            "training_days": record[2],
            "latest_run_utc": record[3],
            "latest_capture_utc": record[4],
            "run_age_hours": run_age,
            "latest_capture_age_hours": (
                latest["capture_age_hours"] if latest
                else _age_hours(record[4])
            ),
            "latest_run_stations": latest["stations"] if latest else None,
            "previous_run_utc": recent[1]["run_utc"] if len(recent) > 1 else None,
            "cycle_hours": cycle_hours,
            "publish_lag_hours": lag_hours,
            "due_in_hours": due_in_hours,
            "overdue_hours": overdue_hours,
            "runs_recent": recent[:6],
            "runs_window_days": RUN_CADENCE_WINDOW_DAYS,
            "seats_won": seats,
            "backfillable": backfillable,
            "health": health,
            "note": note,
        })
    return rows


def collect_correction_and_weights(conn: sqlite3.Connection) -> dict[str, Any]:
    """The two per-station tables P0 serves from."""

    correction = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT station_id), MAX(valid_from_utc)"
        " FROM calibration_native_ensemble_activation"
        " WHERE artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION'"
    ).fetchone()
    weights = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT station_id), MAX(valid_from_utc)"
        " FROM calibration_native_ensemble_activation"
        " WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE'"
    ).fetchone()
    lag = conn.execute(
        "SELECT COUNT(*) FROM ("
        " SELECT s.station_id, s.lead_bucket, s.run_cycle,"
        "  MAX(s.train_end_date) AS corr_end,"
        "  (SELECT MAX(e.train_end_date)"
        "   FROM calibration_cross_native_ensemble_selection_evidence e"
        "   WHERE e.station_id=s.station_id"
        "    AND e.lead_bucket=s.lead_bucket"
        "    AND e.run_cycle=s.run_cycle) AS weight_end"
        " FROM calibration_native_ensemble_system_artifact s"
        " WHERE s.lead_bucket IN ('h00_12','h12_24','h24_48')"
        " GROUP BY s.station_id, s.lead_bucket, s.run_cycle)"
        " WHERE weight_end IS NOT NULL AND julianday(corr_end)"
        " - julianday(weight_end) >= 3"
    ).fetchone()[0]
    return {
        "correction": {
            "activations": correction[0],
            "stations": correction[1],
            "latest_utc": correction[2],
            "age_hours": _age_hours(correction[2]),
        },
        "weights": {
            "activations": weights[0],
            "stations": weights[1],
            "latest_utc": weights[2],
            "age_hours": _age_hours(weights[2]),
        },
        "weight_lag_scopes": lag,
    }


def collect_band_stages(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per stage: served factors, evidence depth, what blocks each band."""

    from bayes_basket_target.band_reliability_store import (
        BAND_STAGES,
        MIN_QUALIFYING_DAYS,
    )

    if not _table_exists(conn, "calibration_band_reliability_table"):
        return []
    # Charter 105: a table carries the axis its cells are keyed on, and a
    # reader that cannot answer which one must not render it -- the two
    # partitions take the same shape ([lo, hi) pairs) and both "work".
    # Read through the one helper, so the knob panel and the audit panel
    # below it cannot end up on different axes for the same stage.
    axis_by_stage = _band_axis_by_stage(conn)
    band_columns = {
        str(col[1])
        for col in conn.execute(
            "PRAGMA table_info(calibration_band_reliability_table)"
        )
    }
    stages = []
    for stage in BAND_STAGES:
        row = conn.execute(
            "SELECT band_table_sha256, factors_json, evidence_json,"
            " fitted_at_utc, serving_day_count"
            " FROM calibration_band_reliability_table"
            " WHERE activated=1 AND stage=?",
            (stage,),
        ).fetchone()
        journal_rows = []
        if _table_exists(conn, "calibration_band_reliability_fit_journal"):
            journal_rows = conn.execute(
                "SELECT fitted_at_utc, outcome, serving_day_count,"
                " qualifying_cell_count, band_table_sha256,"
                " serving_station_day_count, evidence_json"
                " FROM calibration_band_reliability_fit_journal"
                " WHERE stage=? ORDER BY fitted_at_utc DESC LIMIT 8",
                (stage,),
            ).fetchall()
        if row is None:
            # No active row is also what a veto leaves behind until the next
            # refit activates identity (``retire_band_table``), and in that
            # window production serves identity -- unpinned readers find no
            # table, pinned ones read the retired stamp.  "未建表" said the
            # lane had never run.
            retired = conn.execute(
                "SELECT retired_at_utc, axis"
                " FROM calibration_band_reliability_table"
                " WHERE stage=? AND retired_at_utc IS NOT NULL"
                " ORDER BY retired_at_utc DESC LIMIT 1",
                (stage,),
            ).fetchone() if {"retired_at_utc", "axis"} <= band_columns else None
            stages.append({
                "stage": stage,
                "meaning": STAGE_MEANING.get(stage, ""),
                "state": (
                    f"现役 {str(retired[0])[:10]} 被否决退役 · 按恒等服务"
                    if retired else "未建表"
                ),
                "health": "amber",
                "axis": str(retired[1]) if retired else "claim",
                "bands": [],
                "journal": [
                    {
                        "at": j[0],
                        "outcome": j[1],
                        "days": j[2],
                        "cells": j[3],
                        "sha": str(j[4])[:12],
                    }
                    for j in journal_rows
                ],
            })
            continue
        factors = json.loads(row[1])
        evidence = json.loads(row[2])
        fitted_at = row[3]
        days = int(row[4])
        axis = axis_by_stage.get(stage, "claim")
        station_days = evidence.get("serving_station_day_count")
        # The active row is content-addressed by its factors and is only
        # rewritten when they change, so a stage that keeps re-fitting to
        # the same table -- an accumulating stage always does -- freezes
        # its fit time and day counts at whatever the last *change* saw.
        # The journal is the record of what each fit knew, so the evidence
        # side of this panel reads its newest entry for this exact table.
        if journal_rows and str(journal_rows[0][4]) == str(row[0]):
            latest = journal_rows[0]
            fitted_at = latest[0]
            days = int(latest[2])
            station_days = int(latest[5])
            try:
                evidence = json.loads(latest[6])
            except (ValueError, TypeError):
                pass
        accumulating = bool(evidence.get("accumulating"))
        reports = evidence.get("serving_report") or {}
        basis = evidence.get("basis") or {}
        # While accumulating the fit HOLDS the incumbent's factors
        # (``fit_band_reliability_table``) and journals an empty report, so
        # the newest journal row says nothing about why those factors are
        # being served.  The fit that set them does: the newest measured
        # journal row for this exact table, else the table row's own
        # evidence.  Measured 2026-09-15: P0's three and P1's five live
        # factors printed "证据充分，已生效" with a blank ratio column, under
        # a pill reading "攒证据 2/10 天" and a bar promising correction would
        # start at ten days -- while 703 of 720 P0 and all 2,132 P1 decision
        # rows that day were applying them.
        held_from = None
        held_reports: dict[str, Any] = {}
        held_basis: dict[str, Any] = {}
        if accumulating:
            held_row = None
            if _table_exists(conn, "calibration_band_reliability_fit_journal"):
                held_row = conn.execute(
                    "SELECT fitted_at_utc, serving_day_count,"
                    " serving_station_day_count, evidence_json"
                    " FROM calibration_band_reliability_fit_journal"
                    " WHERE stage=? AND band_table_sha256=?"
                    " AND outcome IN ('ACTIVATED','UNCHANGED')"
                    " ORDER BY fitted_at_utc DESC LIMIT 1",
                    (stage, str(row[0])),
                ).fetchone()
            candidates = []
            if held_row is not None:
                candidates.append((
                    held_row[0], held_row[1], held_row[2], held_row[3]
                ))
            candidates.append((row[3], row[4], None, row[2]))
            for held_at, held_days, held_station_days, raw in candidates:
                try:
                    held_evidence = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if held_evidence.get("accumulating") or not (
                    held_evidence.get("serving_report")
                ):
                    continue
                held_reports = held_evidence.get("serving_report") or {}
                held_basis = held_evidence.get("basis") or {}
                held_from = {
                    "fitted_at_utc": held_at,
                    "days": int(held_days or 0),
                    "station_days": int(
                        held_station_days
                        if held_station_days is not None
                        else held_evidence.get("serving_station_day_count")
                        or 0
                    ),
                }
                break
        bands = []
        for cell, factor in factors:
            key = str([float(cell[0]), float(cell[1])])
            held = bool(held_from) and key not in reports
            detail = (held_reports if held else reports).get(key) or {}
            reason = str(basis.get(key) or "")
            bands.append({
                "lower": cell[0],
                "upper": cell[1],
                "factor": factor,
                "measured_ratio": detail.get("ratio"),
                "interval": detail.get("interval"),
                "qualified": bool(detail.get("qualified")),
                "basis": reason,
                "blocked_by": detail.get("blocked_by"),
                # Why the held fit gave this cell its factor, when the ratio
                # and reason above come from it rather than from today's fit.
                "held_basis": str(held_basis.get(key) or "") if held else "",
            })
        active_cells = sum(1 for _c, f in factors if float(f) != 1.0)
        if accumulating:
            state = f"攒证据 {days}/{MIN_QUALIFYING_DAYS} 天" + (
                f" · 沿用 {active_cells} 档" if active_cells else ""
            )
            health = "amber"
        elif active_cells:
            state = f"{active_cells} 档生效"
            health = "green"
        else:
            state = "全档恒等（证据未够格）"
            health = "green"
        stages.append({
            "stage": stage,
            "meaning": STAGE_MEANING.get(stage, ""),
            "state": state,
            "health": health,
            "table_sha": str(row[0])[:12],
            "fitted_at_utc": fitted_at,
            "fit_age_hours": _age_hours(fitted_at),
            "serving_days": days,
            "min_days": MIN_QUALIFYING_DAYS,
            "station_days": station_days,
            "accumulating": accumulating,
            "active_cells": active_cells,
            "held_from": held_from,
            "served_versions": _band_served_versions(
                conn, stage=stage, active_sha=str(row[0])
            ),
            "axis": axis,
            "bands": bands,
            "journal": [
                {
                    "at": j[0],
                    "outcome": j[1],
                    "days": j[2],
                    "cells": j[3],
                    "sha": str(j[4])[:12],
                }
                for j in journal_rows
            ],
        })
    return stages


def _band_served_versions(
    conn: sqlite3.Connection,
    *,
    stage: str,
    active_sha: str,
    hours: int = 24,
) -> dict[str, Any] | None:
    """Which band table the stage's recent decision rows actually applied.

    The card names the ACTIVE table, and for P0-M and P1 that is the table
    every pass loads.  P0 is different: each artifact pins the table its
    holdout was scored under (charter 60), so decisions served from an older
    artifact keep applying an older table.  Measured 2026-09-15 over one UTC
    morning: 703 of 720 P0 rows on the active table, 17 on three older pins,
    14 of them claim-axis tables -- a lag the card could not show.  Read off
    the receipts the serving path writes, never re-derived.
    """

    since = (
        _now() - timedelta(hours=hours)
    ).isoformat().replace("+00:00", "Z")
    try:
        rows = conn.execute(
            "SELECT json_extract(e.lineage_json,'$.band_reliability_json')"
            " FROM basket_probability_bridge_history h"
            " JOIN basket_probability_envelope e"
            "   ON e.envelope_id = h.checkpoint_id"
            " WHERE h.stage=? AND h.decision_at_utc>=?",
            (stage, since),
        ).fetchall()
        axis_by_sha = {
            str(sha): str(axis or "claim")
            for sha, axis in conn.execute(
                "SELECT band_table_sha256, axis"
                " FROM calibration_band_reliability_table"
            )
        }
    except sqlite3.Error:
        return None
    if not rows:
        return None
    active = 0
    unreceipted = 0
    others: Counter = Counter()
    for (raw,) in rows:
        try:
            receipt = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            receipt = {}
        sha = str(receipt.get("band_table_sha256") or "")
        if not sha:
            # An artifact minted before its method carried the band tag
            # serves no table at all, which is not the active one either.
            unreceipted += 1
        elif sha == active_sha:
            # In any receipt state: "no band-eligible bucket" under the
            # active table is still the active table.
            active += 1
        else:
            others[sha] += 1
    return {
        "hours": hours,
        "rows": len(rows),
        "active_rows": active,
        "unreceipted_rows": unreceipted,
        "others": [
            {"sha": sha[:12], "axis": axis_by_sha.get(sha, "?"), "rows": n}
            for sha, n in others.most_common()
        ],
    }


def _band_chain_link(conn: sqlite3.Connection) -> tuple[str | None, str]:
    """When the band lane last ran, and what it concluded, for the chain card.

    The card used to read the newest row of the band TABLE, which is written
    only when a stage's factors change.  A stage that is accumulating
    evidence writes no table on purpose -- the served one stands until ten
    qualifying days are in -- so the card went red over a lane doing exactly
    what it should: measured 2026-09-14, "3.2 天没有新记录" while the refit had
    run the previous morning and journalled every stage ACCUMULATING (P0 3,
    P0-M 3, P1 6 of 10 days).  The journal is written on every run whatever
    the outcome, so its newest row is the lane's heartbeat and the stages'
    latest rows are its verdict.
    """

    if not _table_exists(conn, "calibration_band_reliability_table"):
        return None, "未建表"
    versions = int(conn.execute(
        "SELECT COUNT(*) FROM calibration_band_reliability_table"
    ).fetchone()[0])
    if not _table_exists(conn, "calibration_band_reliability_fit_journal"):
        table_latest = conn.execute(
            "SELECT MAX(fitted_at_utc) FROM calibration_band_reliability_table"
        ).fetchone()[0]
        return table_latest, f"{versions} 个版本"
    from bayes_basket_target.band_reliability_store import (  # noqa: PLC0415
        MIN_QUALIFYING_DAYS,
    )

    rows = conn.execute(
        "SELECT j.stage, j.fitted_at_utc, j.outcome, j.serving_day_count"
        " FROM calibration_band_reliability_fit_journal j"
        " WHERE j.fitted_at_utc = ("
        "   SELECT MAX(k.fitted_at_utc)"
        "   FROM calibration_band_reliability_fit_journal k"
        "   WHERE k.stage = j.stage"
        " )"
        " ORDER BY j.stage"
    ).fetchall()
    if not rows:
        table_latest = conn.execute(
            "SELECT MAX(fitted_at_utc) FROM calibration_band_reliability_table"
        ).fetchone()[0]
        return table_latest, f"{versions} 个版本"
    latest = max(str(row[1]) for row in rows)
    accumulating = [
        f"{row[0]} {int(row[3] or 0)}/{MIN_QUALIFYING_DAYS}"
        for row in rows
        if str(row[2] or "").upper() == "ACCUMULATING"
    ]
    volume = f"{versions} 个版本"
    if accumulating:
        volume += "；攒证据中（现役表照常服务）：" + "、".join(accumulating) + " 天"
    return latest, volume


def collect_chain(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Each link of the live chain, newest evidence and whether it moved."""

    links: list[dict[str, Any]] = []

    def add(
        name: str,
        plain: str,
        latest: str | None,
        volume: str,
        amber_h: float,
        red_h: float,
    ) -> None:
        age = _age_hours(latest)
        if age is None:
            health, note = "red", "无记录"
        elif age > red_h:
            health, note = "red", f"{age / 24:.1f} 天没有新记录"
        elif age > amber_h:
            health, note = "amber", f"{age:.0f} 小时没有新记录"
        else:
            health, note = "green", f"{age:.1f} 小时前有新记录"
        links.append({
            "name": name,
            "plain": plain,
            "latest_utc": latest,
            "age_hours": age,
            "volume": volume,
            "health": health,
            "note": note,
        })

    capture = conn.execute(
        "SELECT MAX(archive_query_at_utc), COUNT(*)"
        " FROM calibration_native_ensemble_historical_member_run"
    ).fetchone()
    add(
        "① 模型采集",
        "四家气象机构的集合预报下载入库",
        capture[0],
        f"{capture[1]:,} 条成员预报",
        18,
        36,
    )
    truth = conn.execute(
        "SELECT MAX(local_date), COUNT(*)"
        " FROM calibration_station_daily_high_truth"
    ).fetchone()
    truth_latest = (
        f"{truth[0]}T23:59:59Z" if truth[0] else None
    )
    add(
        "② 真值结算",
        "各站当天实际最高温入库，用来给预报打分",
        truth_latest,
        f"{truth[1]:,} 个站日",
        48,
        96,
    )
    art = conn.execute(
        "SELECT MAX(valid_from_utc), COUNT(*)"
        " FROM calibration_native_ensemble_activation"
    ).fetchone()
    add(
        "③ 校正与权重",
        "每站每模型的偏差订正，以及各模型的融合权重",
        art[0],
        f"{art[1]:,} 次激活",
        30,
        72,
    )
    latest, volume = _band_chain_link(conn)
    add(
        "④ 概率分档校准",
        "对最终概率数字本身的可信度做修正",
        latest,
        volume,
        30,
        72,
    )
    p0 = conn.execute(
        "SELECT MAX(decision_at_utc), COUNT(*)"
        " FROM basket_probability_bridge_history WHERE stage='P0'"
    ).fetchone()
    add(
        "⑤ P0 概率服务",
        "把校准后的概率投影到市场桶，供交易使用",
        p0[0],
        f"{p0[1]:,} 份快照",
        12,
        36,
    )
    p1 = conn.execute(
        "SELECT MAX(decision_at_utc), COUNT(*)"
        " FROM basket_probability_bridge_history WHERE stage='P1'"
    ).fetchone()
    add(
        "⑥ P1 盘中更新",
        "当天实测进来后滚动更新概率",
        p1[0],
        f"{p1[1]:,} 份快照",
        24,
        72,
    )
    return links


#: Where the resident records what each hosted sweep last did.  This file is
#: the ONLY record that survives a restart: ``status.json``'s own
#: ``hosted_sweeps.results`` is cleared every generation and the launchd logs
#: for the resident are always empty, so a lane that has not run since the
#: last deploy leaves no trace anywhere else.
HOSTED_SWEEP_CLOCKS = ROOT / "data" / "hosted_sweep_clocks.json"
#: Every restart the resident was asked for, one JSON line each.  A restart
#: kills whatever sweep is running, so a morning of them reads on the lanes
#: as starvation with no cause named (2026-09-14: seven between 03:49Z and
#: 07:11Z, and the paper ledger went nine hours without completing).
RESIDENT_RESTART_JOURNAL = (
    ROOT / "data" / "runtime" / "resident_restart" / "requests.jsonl"
)
#: The cycle time the sweep contracts are sized against, from the test that
#: guards them (``test_the_ssot_sweep_queue_fits_its_drain_rate``).  Shown
#: beside the measured cadence so a reader can see which side of it the
#: resident is running on today.
SSOT_QUEUE_REFERENCE_CYCLE_SEC = 600.0


def _hosted_sweep_contracts() -> tuple[list[dict[str, Any]], str]:
    """The sweep table and its write-domain map, from the daemon itself.

    Imported lazily and defensively: this module is read by tooling that has
    no business importing a service, and a board that cannot answer this
    question must still render the other nineteen sections.
    """

    import sys as _sys  # noqa: PLC0415

    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in _sys.path:
        _sys.path.insert(0, scripts_dir)
    try:
        import weather_basket_yes_daemon as daemon  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - a board never fails closed
        return [], f"读不出常驻的扫描表：{exc}"
    exempt = set(getattr(daemon, "WRITER_TURN_EXEMPT_SWEEPS", ()) or ())
    rows = []
    for name, script, interval, timeout in daemon.HOSTED_SWEEPS:
        rows.append({
            "name": str(name),
            "script": str(script),
            "interval_sec": float(interval),
            "timeout_sec": float(timeout),
            "domain": daemon.hosted_sweep_write_domain(str(name)),
            # Downloads outside any write transaction: holds neither the
            # writer nor, since 2026-09-14, the SSOT queue while it runs.
            "download_capture": str(name) in exempt,
        })
    return rows, ""


def _resident_restarts(window_hours: float = 24.0) -> list[dict[str, Any]]:
    """Restarts served in the last ``window_hours``, newest first."""

    # One restart serves every request queued behind it, so requests are
    # grouped on the moment they were served: three lines stamped 04:01 are
    # one restart, and counting them as three overstates the churn.
    served: dict[str, dict[str, Any]] = {}
    try:
        lines = RESIDENT_RESTART_JOURNAL.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines[-200:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        at = row.get("served_at_utc") or row.get("requested_at_utc")
        hours = _age_hours(at)
        if hours is None or hours > window_hours:
            continue
        entry = served.setdefault(str(at), {
            "at_utc": str(at), "age_sec": hours * 3600.0, "who": [], "reasons": [],
        })
        who = str(row.get("who") or "")
        if who and who not in entry["who"]:
            entry["who"].append(who)
        entry["reasons"].append(str(row.get("reason") or "")[:160])
    return sorted(served.values(), key=lambda row: row["age_sec"])


def collect_hosted_sweeps(conn: sqlite3.Connection) -> dict[str, Any]:
    """The resident's maintenance lanes: who is due, who is late, who is stuck.

    Nothing on this page showed these lanes until 2026-09-10, and the day
    that changed three of them were sitting at 2.3x to 2.5x their own
    interval with ``last_state: "ok"`` beside every one.  The clock file is
    not lying -- it has no state for "starved", only for "failed" -- so a
    lane that simply never got a turn is indistinguishable from a healthy
    one unless somebody divides two numbers by hand.  That is what this
    section does.

    Lateness is measured in the lane's OWN intervals, never in hours: the
    contracts here run from ten minutes to a day, so ``calibration_refit``
    nine hours behind is a quarter of the way through its window and
    ``settlement_truth`` nine hours behind has missed three turns.  Reading
    them on one clock is how a normal lane gets reported as a stall.

    Two failure shapes are called apart because they need opposite
    responses.  A lane that has not been ATTEMPTED is starved -- it is
    losing the queue to somebody, usually because a deploy storm keeps
    resetting the generation before its turn comes round.  A lane attempted
    far more recently than it completed is stuck: it is taking the slot and
    failing, and under an exclusive write domain it takes everyone behind
    it down too (2026-09-09: one capture lane held the SSOT domain for 72
    hours this way).
    """

    contracts, error = _hosted_sweep_contracts()
    if error:
        return {"available": False, "reason": error, "lanes": []}
    try:
        clocks = json.loads(HOSTED_SWEEP_CLOCKS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "available": False,
            "reason": f"读不出 {HOSTED_SWEEP_CLOCKS.name}：{exc}",
            "lanes": [],
        }
    status: dict[str, Any] = {}
    if STATUS_JSON.is_file():
        try:
            status = json.loads(STATUS_JSON.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
    hosted = status.get("hosted_sweeps") or {}
    running = {str(name) for name in (hosted.get("running") or [])}
    results = hosted.get("results") or {}
    running_since = hosted.get("started_at") or {}

    def _age(stamp: Any) -> float | None:
        hours = _age_hours(stamp)
        return None if hours is None else hours * 3600.0

    lanes: list[dict[str, Any]] = []
    for contract in contracts:
        clock = clocks.get(contract["name"]) or {}
        completed = clock.get("completed_at_utc")
        attempted = clock.get("attempted_at_utc")
        since_completed = _age(completed)
        since_attempted = _age(attempted)
        interval = contract["interval_sec"]
        overdue = (
            None if since_completed is None or interval <= 0
            else since_completed / interval
        )
        # Attempted much more recently than completed: the lane is getting
        # its turn and failing to finish it.  Half an interval of slack, so
        # a lane that merely started its current round does not read as
        # stuck.
        stuck = (
            since_attempted is not None
            and since_completed is not None
            and since_completed - since_attempted > interval * 0.5
        )
        state = str(clock.get("last_state") or "")
        health = "green"
        note = ""
        # Judged on the number the page PRINTS, not on the one behind it.
        # A lane at 1.995 renders as "2.00x" and would otherwise sit there
        # green beside its own amber threshold, which reads as a bug in the
        # section rather than as a rounding boundary.
        shown = None if overdue is None else round(overdue, 2)
        if state and state != "ok":
            health, note = "red", f"上次尝试 {state}"
        elif stuck:
            health, note = "red", "在轮到自己的回合里失败——占着写域"
        elif shown is not None and shown >= 3.0:
            health, note = "red", "已错过三个回合"
        elif shown is not None and shown >= 2.0:
            health, note = "amber", "已错过两个回合"
        this_round = results.get(contract["name"]) or {}
        is_running = contract["name"] in running
        started = running_since.get(contract["name"]) if is_running else None
        running_elapsed = _age(started) if started else None
        timeout = float(contract.get("timeout_sec") or 0.0)
        timeout_share = (
            None if running_elapsed is None or timeout <= 0
            else running_elapsed / timeout
        )
        if is_running and timeout_share is not None and timeout_share >= 0.8:
            if health == "green":
                health = "amber"
            note = (note + "；" if note else "") + "在跑，快到超时"
        # The writer lease (resident ``WRITER_LEASE_SEC``): what one slot
        # may cost production.  A lane that ran past it held production out
        # for that long; the resident records it rather than killing the
        # sweep, so this is where it is seen.
        last_duration = clock.get("last_duration_sec")
        overrun = clock.get("lease_overrun_sec")
        overrun = (
            float(overrun) if isinstance(overrun, (int, float)) else 0.0
        )
        if overrun > 0.0:
            if health == "green":
                health = "amber"
            note = (note + "；" if note else "") + (
                f"上次跑了 {float(last_duration):.0f}s，超写入租约 {overrun:.0f}s"
                if isinstance(last_duration, (int, float))
                else f"上次超写入租约 {overrun:.0f}s"
            )
        # Past its due time and not running: waiting in its domain's queue.
        # Due is measured from the LATER of the two stamps, which is how the
        # resident seeds it; the rank below mirrors the resident's order.
        since_last = min(
            (value for value in (since_completed, since_attempted)
             if value is not None),
            default=None,
        )
        lateness = (
            None if since_last is None or interval <= 0
            else (since_last - interval) / interval
        )
        lanes.append({
            **contract,
            "download_capture": bool(contract.get("download_capture")),
            "running_since_utc": started,
            "running_elapsed_sec": running_elapsed,
            "running_timeout_share": timeout_share,
            "queue_lateness": lateness,
            "waiting": bool(
                not is_running and lateness is not None and lateness >= 0.0
            ),
            "completed_at_utc": completed,
            "attempted_at_utc": attempted,
            "since_completed_sec": since_completed,
            "since_attempted_sec": since_attempted,
            "overdue_intervals": overdue,
            "last_state": state,
            "last_duration_sec": (
                float(last_duration)
                if isinstance(last_duration, (int, float)) else None
            ),
            "lease_overrun_sec": overrun,
            "stuck": bool(stuck),
            "running_now": contract["name"] in running,
            "this_generation": (
                {
                    "state": str(this_round.get("state") or ""),
                    "duration_sec": this_round.get("duration_sec"),
                    "returncode": this_round.get("returncode"),
                }
                if this_round else None
            ),
            "health": health,
            "note": note,
        })
    # Each domain's start queue, in the resident's order: most intervals late
    # first, table order breaking ties.  A capture competes in the SSOT
    # queue for its start even though, running, it no longer holds it.
    order = {contract["name"]: index for index, contract in enumerate(contracts)}
    queues: dict[str, list[dict[str, Any]]] = {}
    for row in lanes:
        if row["waiting"]:
            queues.setdefault(str(row["domain"]), []).append(row)
    for domain, members in queues.items():
        members.sort(
            key=lambda row: (-(row["queue_lateness"] or 0.0), order[row["name"]])
        )
        for rank, row in enumerate(members, start=1):
            row["queue_rank"] = rank
            row["queue_size"] = len(members)
    lanes.sort(key=lambda row: -(row["overdue_intervals"] or 0.0))

    # The queue's arithmetic, in the same place as its symptoms.  Supply is
    # one exclusive writer per production cycle, so it is the cycle cadence
    # -- start to start, not the compute time -- that sets it.  Demand is
    # what the contracts add up to.  A queue over capacity makes EVERY
    # member permanently late and the ordering only decides who, which is a
    # different repair from waiting out a deploy storm.
    demand = sum(
        3600.0 / row["interval_sec"]
        for row in lanes
        if row["domain"] == "ssot" and row["interval_sec"] > 0
    )
    production = status.get("production") or {}
    runtime = production.get("target_cycle_runtime") or {}
    inflight = status.get("production_inflight") or {}
    # Start to start, because that is what one turn of the queue costs.
    # Between cycles ``production_inflight`` is empty, so the fallback adds
    # the wait the finished cycle sat through to its own elapsed time --
    # the same span, reconstructed.  Either way it is ONE observation, and
    # the section says so: the wait alone was 210s on one round measured
    # 2026-09-10 and 6s on the next.
    wait_block = status.get("production_wait") or {}
    cadence = None
    cadence_basis = ""
    try:
        previous = datetime.fromisoformat(
            str(runtime["started_at_utc"]).replace("Z", "+00:00")
        )
        current = datetime.fromisoformat(
            str(inflight["submitted_at_utc"]).replace("Z", "+00:00")
        )
        span = (current - previous).total_seconds()
        if span > 0:
            cadence, cadence_basis = span, "本轮起点减上轮起点"
    except (KeyError, TypeError, ValueError):
        cadence = None
    if cadence is None:
        try:
            elapsed = float(runtime["elapsed_ms"]) / 1000.0
            waited = float(wait_block["waited_sec"])
            if elapsed > 0:
                cadence = elapsed + max(0.0, waited)
                cadence_basis = "上一轮：等待 + 自身用时"
        except (KeyError, TypeError, ValueError):
            cadence = None
    supply = None if not cadence else 3600.0 / cadence
    started = status.get("started_at")
    return {
        "available": True,
        "lanes": lanes,
        "ssot_demand_per_hour": demand,
        "ssot_supply_per_hour": supply,
        "cycle_cadence_sec": cadence,
        "cycle_cadence_basis": cadence_basis,
        "cycle_elapsed_sec": (
            float(runtime["elapsed_ms"]) / 1000.0
            if runtime.get("elapsed_ms") is not None else None
        ),
        "reference_cycle_sec": SSOT_QUEUE_REFERENCE_CYCLE_SEC,
        "reference_supply_per_hour": (
            3600.0 / SSOT_QUEUE_REFERENCE_CYCLE_SEC
        ),
        "waiting_on": list(wait_block.get("blocked_by") or []),
        "waited_sec": wait_block.get("waited_sec"),
        # How old this generation is.  Every lane's turn is drawn from it,
        # so "nothing has run" one minute after a restart means nothing at
        # all, and the same reading two hours in is the finding.
        "generation_started_at": started,
        "generation_age_sec": _age(started),
        "running_now": sorted(running),
        # The SSOT start queue by name, head first -- who the next slot goes to.
        "ssot_queue": [row["name"] for row in queues.get("ssot", [])],
        "production_inflight_sec": (
            float(inflight["elapsed_sec"])
            if inflight.get("elapsed_sec") is not None else None
        ),
        "production_overdue": bool(inflight.get("overdue")),
        "production_overdue_after_sec": inflight.get("overdue_after_sec"),
        "restarts_24h": _resident_restarts(),
        "red_count": sum(1 for row in lanes if row["health"] == "red"),
        "amber_count": sum(1 for row in lanes if row["health"] == "amber"),
        # The longest contract among the lanes currently reading late.  A
        # generation younger than this cannot have owed any of them a turn,
        # so the page says which number the reader should hold the age
        # against rather than a constant that happens to be an hour.
        "slowest_late_interval_sec": max(
            (
                row["interval_sec"] for row in lanes
                if row["health"] in ("amber", "red") and not row["stuck"]
            ),
            default=None,
        ),
    }


#: Tables worth naming when the store is filling, and the column that says
#: when each row arrived.  Not every table -- the point is which handful is
#: burning the headroom, and a list of 138 answers nothing.
SSOT_GROWTH_TABLES: tuple[tuple[str, str], ...] = (
    ("basket_official_ws_snapshot", "observed_at"),
    ("basket_target_market_registry", "refreshed_at"),
    ("basket_probability_envelope", "decision_at_utc"),
    ("basket_confirmed_inventory_snapshot", "observed_at"),
    ("basket_order_state_snapshot", "observed_at"),
    ("basket_probability_update_proof", "created_at_utc"),
    ("maker_shadow_order", "placed_at_utc"),
    ("basket_probability_bridge_history", "decision_at_utc"),
    ("basket_regime_artifact", "created_at_utc"),
    ("maker_shadow_settlement", "settled_at_utc"),
)
#: Tables the retention lanes delete from again.  Their write rate is churn,
#: not growth, and reporting it beside the append-only tables is how a lane
#: that is doing its job reads as the problem: the market registry writes
#: 46 MB a day and has held flat -- it went from 1.47M rows to 261k when its
#: retention shipped on 2026-09-07.
SSOT_RETAINED_TABLES = frozenset({"basket_target_market_registry"})


def collect_delta_v2_authority(conn: sqlite3.Connection) -> dict[str, Any]:
    """How long production can still resolve what every candidate executes against.

    The fill batch and the policy generations carry validity windows the
    resolvers enforce, and on 2026-09-17 they had 4.5, 10.6 and 14.1 days
    left with nothing renewing them -- an end nobody on this page could see
    coming refuses every candidate at every station.  Read on every build
    (a handful of index rows) because it feeds the banner.
    """

    from bayes_basket_target.delta_v2_authority_clock import (  # noqa: PLC0415
        authority_health,
    )

    try:
        return authority_health(conn, now=_now())
    except sqlite3.Error as exc:
        return {"available": False, "health": "amber", "note": f"读不出：{exc}"}


def collect_ssot_capacity(conn: sqlite3.Connection) -> dict[str, Any]:
    """The store against its own ceiling, and which tables are eating it.

    The ceiling is the one limit that stops the whole pipeline: every writer
    fail-closes on ``target_database_bytes_exceeded`` and every model run
    then reads stale.  It has been raised four times (29, 33, 35, 40, 48
    GiB) and each raise bought days, so the number that matters is not the
    remaining bytes but the days they represent -- and under them, which
    tables are spending them.

    The gauge itself is read from the resident's own status file rather
    than recomputed here.  It is a live number with a warning bound
    attached; a second implementation on this page would eventually
    disagree with the one that actually fail-closes the writers, and the
    board would then be arguing with the guard instead of showing it.

    What this page adds is the composition, which the guard does not carry:
    per-table rows and bytes over the last day, split by whether a
    retention lane deletes them again.  Measured 2026-09-11, that split is
    the whole story -- 72% of the net burn is one append-only table, and
    the single biggest write rate after it belongs to a table that is not
    growing at all.
    """

    status: dict[str, Any] = {}
    if STATUS_JSON.is_file():
        try:
            status = json.loads(STATUS_JSON.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
    guard = dict(status.get("target_ssot_capacity") or {})
    if not guard:
        return {"available": False, "reason": "常驻还没发布容量读数", "tables": []}

    since = (_now() - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    tables: list[dict[str, Any]] = []
    for name, column in SSOT_GROWTH_TABLES:
        if not _table_exists(conn, name):
            continue
        try:
            info = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
            columns = {str(row[1]) for row in info}
            if column not in columns:
                continue
            rows = conn.execute(
                f'SELECT COUNT(*) FROM "{name}" WHERE "{column}" >= ?',
                (since,),
            ).fetchone()[0]
            total = conn.execute(
                f'SELECT COUNT(*) FROM "{name}"'
            ).fetchone()[0]
            # Row width from the newest rows, which is what the next day
            # will be written at.  Older rows are a different size on
            # several of these -- the ws snapshot's grew from 8.9 KB to
            # 20.3 KB as the books deepened -- so an average over the whole
            # table would describe a day that is not coming back.
            width_expr = " + ".join(
                f'COALESCE(LENGTH(CAST("{row[1]}" AS BLOB)),0)'
                for row in info
            )
            width = conn.execute(
                f"SELECT AVG(width) FROM (SELECT ({width_expr}) AS width"
                f' FROM "{name}" ORDER BY rowid DESC LIMIT 200)'
            ).fetchone()[0] or 0.0
        except sqlite3.Error:
            continue
        retained = name in SSOT_RETAINED_TABLES
        tables.append({
            "table": name,
            "rows_total": int(total),
            "rows_per_day": int(rows),
            "row_bytes": float(width),
            "bytes_per_day": float(rows) * float(width),
            "retained": retained,
        })
    tables.sort(key=lambda row: -row["bytes_per_day"])
    appending = [row for row in tables if not row["retained"]]
    net = sum(row["bytes_per_day"] for row in appending)
    churn = sum(
        row["bytes_per_day"] for row in tables if row["retained"]
    )
    largest = appending[0] if appending else None
    return {
        "available": True,
        **guard,
        "measured_tables": len(tables),
        "tables": tables,
        # The payload the append-only tables added in a day.  Always under
        # the guard's own rate, which counts indexes as well -- named apart
        # rather than reconciled, because one is what the rows carry and
        # the other is what the file does.
        "net_payload_bytes_per_day": net,
        "churn_payload_bytes_per_day": churn,
        "largest_table": largest["table"] if largest else "",
        "largest_share": (
            largest["bytes_per_day"] / net if largest and net > 0 else None
        ),
    }


def _market_bucket_index(unit: str, celsius: float) -> int | None:
    """Which rung of the station's own market ladder a temperature sits on.

    The ladder is not the same width everywhere: a Fahrenheit book steps two
    degrees F per rung (1.11C) and a Celsius book one degree C, so a fixed
    temperature tolerance would score the two books against different targets.
    Counting rungs is what makes "off by one bucket" mean the same thing at
    KLGA and at ZUUU.

    Deliberately unbounded at both ends.  A real book caps its ladder with an
    "or below"/"or higher" bucket, but clipping the distance there would
    report a five-rung miss as a one-rung miss purely because the truth
    landed past the last listed rung.
    """

    if unit == "F":
        # Rungs are 80-81F, 82-83F, ...: boundaries fall on the half degree
        # between an odd and the next even degree.
        return int((celsius * 9.0 / 5.0 + 32.0 + 0.5) // 2.0)
    if unit == "C":
        return int((celsius + 0.5) // 1.0)
    return None


def _correction_in_force(
    artifacts: dict[tuple[str, str, str, str], tuple[list[str], list[Any]]],
    system_id: str,
    station_id: str,
    lead_labels: set[str],
    run_cycle: str,
    event_date: str,
) -> tuple[dict[str, Any], float] | None:
    """The bias artifact that was already fitted when this day arrived.

    Strictly before the event date, so this scores the correction the way
    trading met it -- on a day it had never seen.  Using whatever artifact is
    active today would let a fit that trained *through* the day being scored
    mark its own homework, and the whole point of the corrected column is to
    say whether the correction layer earns its keep out of sample.

    Both lead vocabularies are tried because the archive spans the migration:
    a July run was stamped d0/d1/d2 and its artifacts are keyed that way,
    while an August run and its artifacts are keyed by hour bucket.  The run
    cycle still has to match exactly -- serving refuses a cross-cycle
    artifact, and a coverage number that quietly borrows one would overstate
    how much of the fleet is actually corrected.
    """

    best: tuple[dict[str, Any], float] | None = None
    best_end = ""
    for label in lead_labels:
        entry = artifacts.get((system_id, station_id, label, run_cycle))
        if entry is None:
            continue
        ends, rows = entry
        position = bisect_left(ends, event_date)
        if position and ends[position - 1] > best_end:
            best_end = ends[position - 1]
            best = rows[position - 1][1]
    return best


def collect_model_skill(conn: sqlite3.Connection) -> dict[str, Any]:
    """Per model and lead: member spread, and how often the centre lands.

    Three questions the page could not answer before.  How far apart do a
    model's own members sit -- its dispersion, which is the only uncertainty
    it states about itself.  How often does its raw centre land on the
    market's bucket, one rung out, two rungs out, and does that decay with
    lead time.  And does the per-station bias correction the fleet serves
    actually move those numbers.

    Raw and corrected are reported on the *same* station-days.  Only about a
    quarter of scored runs have an artifact fitted before their event date,
    and comparing a full-sample raw rate against a quarter-sample corrected
    one would credit the correction layer for a difference in which days it
    covers rather than in what it does to them.
    """

    from bayes_basket_target.native_archive_lead import (  # noqa: PLC0415
        lead_hour_bucket,
    )

    zone_names = {
        str(station): str(zone)
        for station, zone in conn.execute(
            "SELECT station_id, settlement_timezone"
            " FROM calibration_station_market_timezone_binding"
        )
    }
    units: dict[str, str] = {}
    for station, unit in conn.execute(
        "SELECT station_id, settlement_unit FROM basket_target_market_registry"
        " WHERE station_id IS NOT NULL AND settlement_unit IS NOT NULL"
        " GROUP BY station_id, settlement_unit"
    ):
        units.setdefault(str(station), str(unit))
    truth: dict[tuple[str, str], float] = {}
    for station, day, tmax in conn.execute(
        "SELECT station_id, local_date, official_tmax_c"
        " FROM calibration_station_daily_high_truth"
        " WHERE qc_status='ACCEPTED' ORDER BY revision_no"
    ):
        truth[(str(station), str(day))] = float(tmax)
    if not truth:
        return {"rows": [], "totals": [], "summary": {}}
    newest_truth = max(day for _station, day in truth)
    window_start = str(
        (
            datetime.fromisoformat(newest_truth)
            - timedelta(days=SKILL_WINDOW_DAYS)
        ).date()
    )

    system_ids = tuple(system for system, _l, _t, _b in MEMBER_FAMILIES)
    grouped: dict[tuple[str, str, str, str], list[Any]] = defaultdict(list)
    placeholders = ",".join("?" for _ in system_ids)
    for (
        system_id, station, lead, cycle, end, bias_json, spread_json
    ) in conn.execute(
        "SELECT ensemble_system_id, station_id, lead_bucket, run_cycle,"
        " train_end_date, bias_parameters_json, spread_parameters_json"
        " FROM calibration_native_ensemble_system_artifact"
        f" WHERE ensemble_system_id IN ({placeholders})",
        system_ids,
    ):
        try:
            bias = json.loads(bias_json)
            spread = float(json.loads(spread_json)["spread_factor"])
        except (ValueError, KeyError, TypeError):
            continue
        grouped[
            (str(system_id), str(station), str(lead), str(cycle))
        ].append((str(end), (bias, spread)))
    artifacts = {}
    for key, rows in grouped.items():
        rows.sort(key=lambda row: row[0])
        artifacts[key] = ([row[0] for row in rows], rows)

    # One row per model run per station-day: the members collapse to a centre
    # and a spread here rather than in Python, because the raw member table is
    # the largest thing on the page by an order of magnitude.
    conditions = " OR ".join(
        "(ensemble_system_id=? AND transport_provider_id=?)"
        for _s, _l, _t, _b in MEMBER_FAMILIES
    )
    parameters: list[Any] = [window_start]
    for system_id, _label, transport, _backfillable in MEMBER_FAMILIES:
        parameters.extend((system_id, transport))
    runs = conn.execute(
        "SELECT ensemble_system_id, station_id, target_local_date,"
        " native_run_init_utc, run_cycle, archive_lead_bucket,"
        " AVG(raw_tmax_c), AVG(raw_tmax_c*raw_tmax_c), COUNT(*)"
        " FROM calibration_native_ensemble_historical_member_run"
        " WHERE qc_status='ACCEPTED' AND native_run_init_utc IS NOT NULL"
        " AND target_local_date >= ?"
        f" AND ({conditions})"
        " GROUP BY 1,2,3,4,5,6",
        parameters,
    ).fetchall()

    def blank() -> dict[str, Any]:
        return {
            "runs": 0, "members": 0, "sigma": [], "scored": 0,
            "abs_error": 0.0, "signed_error": 0.0, "square_error": 0.0,
            "raw": [0, 0, 0], "paired": 0, "paired_raw": [0, 0, 0],
            "paired_cal": [0, 0, 0], "paired_square": 0.0,
            "paired_abs": 0.0, "factor": [], "uncorrected": 0,
        }

    cells: dict[tuple[str, str], dict[str, Any]] = defaultdict(blank)
    zones: dict[str, ZoneInfo] = {}
    day_starts: dict[tuple[str, str], datetime] = {}
    unscored: dict[str, int] = defaultdict(int)
    for (
        system_id, station, day, run_init, cycle, stored_lead,
        centre, mean_square, member_count,
    ) in runs:
        system_id, station, day = str(system_id), str(station), str(day)
        zone_name = zone_names.get(station)
        if zone_name is None:
            unscored["no_timezone"] += 1
            continue
        if station not in zones:
            try:
                zones[station] = ZoneInfo(zone_name)
            except (KeyError, ValueError):
                unscored["no_timezone"] += 1
                continue
        scope = (station, day)
        if scope not in day_starts:
            day_starts[scope] = datetime.fromisoformat(day).replace(
                tzinfo=zones[station]
            ).astimezone(timezone.utc)
        try:
            initialised = datetime.fromisoformat(
                str(run_init).replace("Z", "+00:00")
            )
        except ValueError:
            unscored["bad_run_init"] += 1
            continue
        if initialised.tzinfo is None:
            initialised = initialised.replace(tzinfo=timezone.utc)
        lead = lead_hour_bucket(
            (
                day_starts[scope] - initialised.astimezone(timezone.utc)
            ).total_seconds() / 3600.0
        )
        centre = float(centre)
        cell = cells[(system_id, lead)]
        cell["runs"] += 1
        cell["members"] += int(member_count)
        cell["sigma"].append(
            max(0.0, float(mean_square) - centre ** 2) ** 0.5
        )
        observed = truth.get(scope)
        if observed is None:
            unscored["no_truth_yet"] += 1
            continue
        unit = units.get(station, "")
        truth_bucket = _market_bucket_index(unit, observed)
        raw_bucket = _market_bucket_index(unit, centre)
        if truth_bucket is None or raw_bucket is None:
            unscored["no_market_ladder"] += 1
            continue
        cell["scored"] += 1
        cell["abs_error"] += abs(centre - observed)
        cell["signed_error"] += centre - observed
        cell["square_error"] += (centre - observed) ** 2
        raw_distance = abs(raw_bucket - truth_bucket)
        for offset in range(3):
            if raw_distance <= offset:
                cell["raw"][offset] += 1
        correction = _correction_in_force(
            artifacts, system_id, station,
            {lead, str(stored_lead)}, str(cycle), day,
        )
        if correction is None:
            cell["uncorrected"] += 1
            continue
        bias, factor = correction
        # One correction form; the board reports what P0 actually serves.
        corrected = centre + float(bias["bias_c"])
        corrected_bucket = _market_bucket_index(unit, corrected)
        if corrected_bucket is None:
            cell["uncorrected"] += 1
            continue
        cell["paired"] += 1
        cell["factor"].append(factor)
        cell["paired_abs"] += abs(corrected - observed)
        cell["paired_square"] += (corrected - observed) ** 2
        corrected_distance = abs(corrected_bucket - truth_bucket)
        for offset in range(3):
            if raw_distance <= offset:
                cell["paired_raw"][offset] += 1
            if corrected_distance <= offset:
                cell["paired_cal"][offset] += 1

    def emit(cell: dict[str, Any], label: str, system_id: str,
             lead: str, lead_label: str) -> dict[str, Any]:
        scored = cell["scored"]
        paired = cell["paired"]
        sigma = median(cell["sigma"]) if cell["sigma"] else None
        factor = median(cell["factor"]) if cell["factor"] else None
        width = None if sigma is None or factor is None else sigma * factor
        corrected_rmse = (
            (cell["paired_square"] / paired) ** 0.5 if paired else None
        )
        return {
            "label": label,
            "system_id": system_id,
            "lead_bucket": lead,
            "lead_label": lead_label,
            "runs": cell["runs"],
            "members_per_run": (
                cell["members"] / cell["runs"] if cell["runs"] else None
            ),
            "member_sigma_c": sigma,
            "spread_factor": factor,
            "served_width_c": width,
            "scored": scored,
            "raw_rates": [
                cell["raw"][i] / scored if scored else None
                for i in range(3)
            ],
            "mae_c": cell["abs_error"] / scored if scored else None,
            "signed_bias_c": (
                cell["signed_error"] / scored if scored else None
            ),
            "rmse_c": (
                (cell["square_error"] / scored) ** 0.5 if scored else None
            ),
            "paired": paired,
            "paired_raw_rates": [
                cell["paired_raw"][i] / paired if paired else None
                for i in range(3)
            ],
            "paired_calibrated_rates": [
                cell["paired_cal"][i] / paired if paired else None
                for i in range(3)
            ],
            "paired_mae_c": (
                cell["paired_abs"] / paired if paired else None
            ),
            "corrected_rmse_c": corrected_rmse,
            # Above 1 the served cloud is narrower than the errors it has to
            # cover -- the model claims more certainty than it earns.
            "width_ratio": (
                corrected_rmse / width
                if corrected_rmse and width else None
            ),
            "uncorrected": cell["uncorrected"],
        }

    rows: list[dict[str, Any]] = []
    totals: list[dict[str, Any]] = []
    for system_id, label, _transport, _backfillable in MEMBER_FAMILIES:
        combined = blank()
        for lead, lead_label in LEAD_LABELS:
            cell = cells.get((system_id, lead))
            if cell is None or not cell["runs"]:
                continue
            for field in (
                "runs", "members", "scored", "paired", "uncorrected",
            ):
                combined[field] += cell[field]
            for field in (
                "abs_error", "signed_error", "square_error",
                "paired_square", "paired_abs",
            ):
                combined[field] += cell[field]
            for field in ("raw", "paired_raw", "paired_cal"):
                for offset in range(3):
                    combined[field][offset] += cell[field][offset]
            combined["sigma"].extend(cell["sigma"])
            combined["factor"].extend(cell["factor"])
            rows.append(emit(cell, label, system_id, lead, lead_label))
        if combined["runs"]:
            totals.append(
                emit(combined, label, system_id, "all", "全部提前量")
            )
    return {
        "rows": rows,
        "totals": totals,
        "summary": {
            "window_days": SKILL_WINDOW_DAYS,
            "window_start": window_start,
            "window_end": newest_truth,
            "run_count": sum(r["runs"] for r in totals),
            "scored_count": sum(r["scored"] for r in totals),
            "paired_count": sum(r["paired"] for r in totals),
            "unscored": dict(unscored),
        },
    }


TRUTH_LANDING_SINCE = "2026-08-15"
# Five-percent bands capped at 50%.  The first cell used to be cut again at
# the 0.03 leg floor (user ruling, 2026-08-25) so the boundary the floor is
# ruled on stayed visible; that cut is withdrawn (user ruling, 2026-09-11)
# and the grid is a plain 5% ruler -- 0-3% and 3-5% are one 0-5% cell, every
# other band unchanged, on both margins and both cross-tab axes.  Section
# six's band table is on the same grid (charter 60), so the two rulers now
# agree everywhere rather than only from 5% upward.
TRUTH_LANDING_PRICE_BANDS = tuple(
    (round(step * 0.05, 2), round(step * 0.05 + 0.05, 2))
    for step in range(0, 10)
) + ((0.50, 1.01),)
# Who was right when the model and the market disagreed (user ruling,
# 2026-09-15: the price x claim cross-tab was unreadable -- replace it with
# something simple that carries its own conclusion).  Each priced bucket is
# placed by how many points the model's claim sat above (+) or below (-) the
# market's entry ask; the table then asks, per group, whether the truth rate
# landed nearer the model or the market.  Buckets both sides called nearly
# impossible are set aside and counted, because thousands of 1-cent,
# 1-percent buckets would otherwise make "agree" mean "both said no".
TRUTH_LANDING_GAP_GROUPS = (
    ("model_much", "模型比市场高 10 点以上", 0.10, None),
    ("model_bit", "模型比市场高 3–10 点", 0.03, 0.10),
    ("agree", "两边相差不到 3 点", -0.03, 0.03),
    ("market_bit", "市场比模型高 3–10 点", -0.10, -0.03),
    ("market_much", "市场比模型高 10 点以上", None, -0.10),
)
TRUTH_LANDING_UNLIKELY = 0.05


# The bias layer's half-life change, and the instant it began being fitted.
# Both are facts about a past deployment, so they are stated rather than
# derived -- the same two constants ``h5_ledger_readiness_check`` states.
H5_FROM_UTC = "2026-08-27T06:48:00Z"
# The half-lives the section compares (user rulings, 2026-09-17: 增加 h15 的
# 对比, 测试 H3): the one that serves and one either side of it.
BIAS_HALF_LIFE_ARMS: tuple[float, ...] = (3.0, 5.0, 15.0)
# The scored window: this many of the most recent settled event days.
BIAS_HALF_LIFE_WINDOW_DAYS = 31
BIAS_HALF_LIFE_BOOTSTRAP = 2000


def collect_bias_half_life(conn: sqlite3.Connection) -> dict[str, Any]:
    """Which bias half-life corrects closest to truth, with nothing else changed.

    Every settled event day in the window is corrected once per half-life,
    each time refitted from scratch on the cases strictly before that day
    inside the rolling train window, through production's own case loader,
    fit and centre resolver (``native_ensemble_oos``) -- only the half-life
    constant differs between arms.  Absolute error is averaged over a
    station-day's cells before anything is compared.

    Why a refit and not the stored artifacts.  The section read the served
    tables until 2026-09-17: H5 as served each day against the last H15 table
    fitted before the switch.  That control froze on 2026-08-27, so by
    mid-September it was three weeks stale and most of H5's 0.116C lead was
    freshness, not half-life -- refitted daily, the same days put H5 ahead of
    H15 by 0.008C.  (It had also been correcting the wrong way round.)

    Two tests, both on the station-day: a sign vote, and an interval on the
    mean difference that resamples whole event days, because every station
    on one day shares that day's weather.  Loading the cases is the cost --
    about five minutes for the fleet -- so the section is cached for a day.
    """

    from bayes_basket_target import native_ensemble_oos as OOS  # noqa: PLC0415
    from bayes_basket_target.contracts import ContractError  # noqa: PLC0415

    try:
        stations = [
            str(row[0]) for row in conn.execute(
                "SELECT station_id FROM calibration_station_registry"
                " ORDER BY station_id"
            )
        ]
    except sqlite3.Error:
        return {"available": False, "reason": "station_registry_missing"}
    as_of = _now().isoformat().replace("+00:00", "Z")
    by_station: dict[str, dict[Any, list[dict[str, Any]]]] = {}
    for station in stations:
        try:
            by_scope = OOS._load_cases(conn, station_id=station, as_of_utc=as_of)
        except (ContractError, sqlite3.Error, ValueError, KeyError):
            continue
        by_station[station] = {
            scope: sorted(
                (
                    case for case in cases
                    if case.get("values")
                    and case.get("actual_tmax_c") is not None
                ),
                key=lambda case: str(case["event_date"])[:10],
            )
            for scope, cases in by_scope.items()
        }
    days_seen = sorted({
        str(case["event_date"])[:10]
        for scopes in by_station.values()
        for cases in scopes.values() for case in cases
    })
    if not days_seen:
        return {"available": False, "reason": "no_settled_cases"}
    window_to = days_seen[-1]
    window_from = (
        date.fromisoformat(window_to)
        - timedelta(days=BIAS_HALF_LIFE_WINDOW_DAYS - 1)
    ).isoformat()

    serving = float(OOS.BIAS_HALF_LIFE_DAYS)
    arms = tuple(sorted(set(BIAS_HALF_LIFE_ARMS) | {serving}))
    errors: dict[tuple[str, str], dict[float, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    cells = 0
    try:
        for station, scopes in by_station.items():
            for cases in scopes.values():
                for case in cases:
                    day = str(case["event_date"])[:10]
                    if not window_from <= day <= window_to:
                        continue
                    floor = (
                        date.fromisoformat(day)
                        - timedelta(days=OOS.ROLLING_TRAIN_WINDOW_DAYS)
                    ).isoformat()
                    history = [
                        prior for prior in cases
                        if floor <= str(prior["event_date"])[:10] < day
                    ]
                    if len({
                        str(prior["event_date"])[:10] for prior in history
                    }) < OOS.DEFAULT_MIN_TRAIN_DAYS:
                        continue
                    values = [float(value) for value in case["values"]]
                    mean = sum(values) / len(values)
                    actual = float(case["actual_tmax_c"])
                    misses: dict[float, float] = {}
                    try:
                        for arm in arms:
                            OOS.BIAS_HALF_LIFE_DAYS = arm
                            parameters = OOS._fit_parameters(history)
                            misses[arm] = abs(
                                OOS._corrected_center(mean, parameters) - actual
                            )
                    except (ContractError, KeyError, TypeError, ValueError):
                        continue
                    finally:
                        OOS.BIAS_HALF_LIFE_DAYS = serving
                    for arm, miss in misses.items():
                        errors[(station, day)][arm].append(miss)
                    cells += 1
    finally:
        OOS.BIAS_HALF_LIFE_DAYS = serving
    if not errors:
        return {"available": False, "reason": "no_scoreable_cells"}

    station_days = {
        key: {arm: statistics.mean(values) for arm, values in by_arm.items()}
        for key, by_arm in errors.items()
    }
    keys = sorted(station_days)

    def compare(arm: float) -> dict[str, Any]:
        diffs = [station_days[key][arm] - station_days[key][serving] for key in keys]
        arm_closer = sum(1 for diff in diffs if diff < 0)
        serving_closer = sum(1 for diff in diffs if diff > 0)
        decided = arm_closer + serving_closer
        by_day: dict[str, list[float]] = defaultdict(list)
        for (_station, day), diff in zip(keys, diffs):
            by_day[day].append(diff)
        days = sorted(by_day)
        rng = random.Random(7)
        means = sorted(
            statistics.mean([
                diff for pick in (rng.choice(days) for _ in days)
                for diff in by_day[pick]
            ])
            for _ in range(BIAS_HALF_LIFE_BOOTSTRAP)
        )
        tail = int(round(0.025 * BIAS_HALF_LIFE_BOOTSTRAP))
        return {
            "half_life_days": arm,
            "mean_diff_c": round(statistics.mean(diffs), 4),
            "ci_low_c": round(means[tail], 4),
            "ci_high_c": round(means[-tail - 1], 4),
            "arm_closer": arm_closer,
            "serving_closer": serving_closer,
            "sign_z": round(
                (arm_closer - decided / 2) / math.sqrt(decided / 4), 2
            ) if decided else 0.0,
            "days_arm_better": sum(
                1 for day in days if statistics.mean(by_day[day]) < 0
            ),
            "days": len(days),
        }

    return {
        "available": True,
        "measured_at_utc": as_of,
        "window_from": window_from,
        "window_to": window_to,
        "cells": cells,
        "station_days": len(keys),
        "serving_half_life_days": serving,
        "arms": [
            {
                "half_life_days": arm,
                "mae_c": round(statistics.mean(
                    station_days[key][arm] for key in keys
                ), 4),
            }
            for arm in arms
        ],
        "comparisons": [compare(arm) for arm in arms if arm != serving],
    }


def collect_h5_readiness(conn: sqlite3.Connection) -> dict[str, Any]:
    """Whether the bias half-life change can yet be scored against truth.

    The question the section answers is "has the fleet finished turning
    over onto H5", and it is asked of the DECISIONS rather than of the
    artifact pool.  Each P0 envelope records the artifact fingerprint that
    served each of its systems, so the answer is exact.  Two pool proxies
    were tried first and both lied by a wide margin -- artifacts carry a
    thirty-day validity window against a daily refit, so a scope holds
    about twenty-one superseded generations that no decision will ever
    read, and counting them put 2026-08-29 at 18% when the decisions say
    100%.  Provisional members are excluded, not counted against: they
    have no artifact of their own by design.

    A day is scoreable once the fleet served it under H5 and its truth has
    landed.  It used to ask the paper book for settled chains instead; the
    paper ledger is a rule comparator since 2026-09-16, and whether a day
    can be scored is a property of the truth, not of which seats a replay
    happened to take -- a rule change empties the paper tables for an hour
    and every day read unscoreable meanwhile.
    """

    truth_days = {
        str(day): int(count or 0)
        for day, count in conn.execute(
            "SELECT local_date, COUNT(DISTINCT station_id)"
            " FROM calibration_station_daily_high_truth"
            " WHERE qc_status='ACCEPTED' AND local_date>=?"
            " GROUP BY local_date",
            (H5_FROM_UTC[:10],),
        )
    }
    known = {
        str(row[0]) for row in conn.execute(
            "SELECT content_sha256"
            " FROM calibration_native_ensemble_system_artifact"
        )
    }
    fitted_under_h5 = {
        str(row[0]) for row in conn.execute(
            "SELECT content_sha256"
            " FROM calibration_native_ensemble_system_artifact"
            " WHERE valid_from_utc>=?",
            (H5_FROM_UTC,),
        )
    }
    days: list[dict[str, Any]] = []
    for (day,) in conn.execute(
        "SELECT DISTINCT event_date FROM basket_probability_envelope"
        " WHERE stage='P0' AND state='COMMITTED' AND event_date>=?"
        " ORDER BY event_date",
        (H5_FROM_UTC[:10],),
    ):
        decisions = 0
        under_h5 = 0
        provisional_only = 0
        for (lineage_json,) in conn.execute(
            "SELECT lineage_json FROM basket_probability_envelope"
            " WHERE stage='P0' AND state='COMMITTED' AND event_date=?",
            (str(day),),
        ):
            try:
                lineage = json.loads(lineage_json)
            except (TypeError, ValueError):
                continue
            raw = lineage.get("system_artifact_shas_json")
            if not raw:
                continue
            try:
                shas = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                continue
            if not isinstance(shas, dict) or not shas:
                continue
            calibrated = [v for v in shas.values() if v in known]
            if not calibrated:
                provisional_only += 1
                continue
            decisions += 1
            if all(v in fitted_under_h5 for v in calibrated):
                under_h5 += 1
        if not decisions:
            continue
        days.append({
            "event_date": str(day),
            "decisions": decisions,
            "served_under_h5": under_h5,
            "share": round(under_h5 / decisions, 3),
            "provisional_only": provisional_only,
            "truth_stations": truth_days.get(str(day), 0),
        })
    scored = [
        item for item in days
        if item["share"] >= 0.9 and item["truth_stations"] > 0
    ]
    return {
        "from_utc": H5_FROM_UTC,
        "days": days,
        "scoreable_days": [item["event_date"] for item in scored],
    }


def three_knob_fit(
    rows: Sequence[tuple[str, float, float, int]],
    *,
    since: str,
    draws: int = 200,
    seed: int = 20260917,
) -> dict[str, Any]:
    """The three knobs of a belief in ``(p, m)`` on the bucket proxy (user
    ruling, 2026-09-17: 把三旋钮度数放在一排对比): ``hit - m = alpha + beta *
    (p - m) + gamma * (m - m0)`` by the beta lane's own fitter, so the row
    sits beside the lane's in the same arithmetic, with a by-day bootstrap
    90% interval on each knob.  ``rows`` are ``(day, p, m, hit)`` -- the
    served claim, the entry ask normalised over the ladder, the market's
    adjudication.  A proxy, not a lane: buckets not windows, the 4h ask not
    the mid; it is here because the lanes read nothing until their evidence
    settles and the reader still needs a number to compare against.
    """

    from bayes_basket_target.beta_edge_store import _ols3  # noqa: PLC0415

    out: dict[str, Any] = {"since": since, "rows": len(rows), "days": 0}
    if not rows:
        return out
    centre = sum(r[2] for r in rows) / len(rows)

    def fit(sample: Sequence[tuple[str, float, float, int]]) -> tuple[float, float, float] | None:
        return _ols3(
            [r[1] - r[2] for r in sample], [r[2] - centre for r in sample],
            [r[3] - r[2] for r in sample],
        )

    point = fit(rows)
    by_day: dict[str, list[tuple[str, float, float, int]]] = {}
    for r in rows:
        by_day.setdefault(r[0], []).append(r)
    out["days"] = len(by_day)
    out["market_centre"] = round(centre, 4)
    if point is None:
        return out
    keys = sorted(by_day)
    rng = random.Random(seed)
    boot: list[tuple[float, float, float]] = []
    for _ in range(int(draws)):
        sample = [r for k in rng.choices(keys, k=len(keys)) for r in by_day[k]]
        got = fit(sample)
        if got is not None:
            boot.append(got)
    for index, name in enumerate(("alpha", "beta", "gamma")):
        out[name] = round(point[index], 4)
        values = sorted(b[index] for b in boot)
        out[f"{name}_ci"] = (
            [round(values[min(len(values) - 1, int(0.05 * len(values)))], 4),
             round(values[max(0, int(0.95 * len(values)) - 1)], 4)]
            if values else [None, None]
        )
    return out


def collect_truth_landing(conn: sqlite3.Connection) -> dict[str, Any]:
    """Where truth settles, by market price and by model claim.

    Truth here is the market's own adjudication -- the bucket whose last
    recorded bid converged to 0.80 or better -- because three hand-built
    truth vocabularies in one evening each disagreed with the market's
    closing prices, and the market is the party that actually pays.
    Validated 60 of 62 against the settlement pipeline's settled_bucket the
    day it was built (the two dissents are ZGSZ truth-revision days).
    Station-days whose capture was truncated before convergence are
    excluded and counted, never guessed.

    Price is the entry the seat actually faced: the median best ask over
    the four hours after the station-day's first P0 decision, at least two
    quotes.  The claim is that same first P0 envelope's served probability.
    A day whose local calendar has not finished is excluded -- an unfinished
    day's "truth" is whatever the afternoon has not yet overwritten, which
    is how the first version of this table put 45 winners under a cent.
    """

    if not _table_exists(conn, "basket_official_ws_snapshot"):
        return {}
    now = datetime.now(timezone.utc)
    # 48 rows, not 148,000.  This map is station -> timezone and nothing
    # else, and it was being built by DISTINCT over every envelope ever
    # written: the predicate is on a NOT NULL column so it never excludes a
    # row, no index carries both columns in a shape the planner will pick,
    # and the chosen index does not cover settlement_timezone -- so it read
    # 147,971 rows off a 38 GB store to return 45.  Measured 2026-09-09:
    # 127.6 seconds, inside a render whose whole budget is 30 minutes.
    # ``calibration_station_market_timezone_binding`` is the authority for
    # exactly this question -- station_id is UNIQUE there and a CHECK holds
    # market_registry_timezone_count to 1 -- and on the day this changed its
    # 48 rows covered all 45 stations the envelopes name, with no timezone
    # disagreeing.  The fallback is deliberately NOT the old scan: a station
    # missing from the binding gets no zone, day_closed answers False, and
    # the day is excluded -- the same answer the old query gave for a
    # station whose envelopes carried no timezone.
    zones: dict[str, str] = {}
    if _table_exists(conn, "calibration_station_market_timezone_binding"):
        zones = {
            str(station): str(zone)
            for station, zone in conn.execute(
                "SELECT station_id, settlement_timezone"
                " FROM calibration_station_market_timezone_binding"
            )
        }

    def day_closed(station: str, event_date: str) -> bool:
        zone = zones.get(station)
        if not zone:
            return False
        try:
            end = datetime.fromisoformat(
                event_date + "T00:00:00"
            ).replace(tzinfo=ZoneInfo(zone)) + timedelta(days=1)
        except Exception:
            return False
        return now >= end.astimezone(timezone.utc)

    first_decision: dict[tuple[str, str], str] = {}
    claims: dict[tuple[str, str], dict[str, float]] = {}
    # One row per station-day, chosen in SQL, instead of 147,971 sorted in a
    # temp B-tree and all but 1,215 thrown away in Python.
    # ``MIN(decision_at_utc)`` with bare columns beside it is SQLite's
    # documented min/max behaviour: the bare columns come from the row the
    # minimum was found on, which is precisely the first decision.
    #
    # Worth 69.1s -> 47.5s measured 2026-09-09, and the modest size is the
    # point: the scan was never the cost.  Broken down, the covering index
    # scan is 2.2s and the 1,215 overflow-page reads of probabilities_json
    # are 45.3s -- so the 122:1 row ratio buys 31%, not an order of
    # magnitude, and an index here would have targeted the 2.2s.  What is
    # left is the irreducible cost of reading 1,215 claims.
    for station, event, probabilities_json, decided in conn.execute(
        "SELECT station_id, event_date, probabilities_json,"
        " MIN(decision_at_utc)"
        " FROM basket_probability_envelope"
        " WHERE event_date >= ? AND stage='P0'"
        " GROUP BY station_id, event_date",
        (TRUTH_LANDING_SINCE,),
    ):
        key = (str(station), str(event))
        # The GROUP BY already made the key unique, so the only test left is
        # whether the local day has finished.
        if not day_closed(*key):
            continue
        first_decision[key] = str(decided)
        try:
            claims[key] = {
                str(bucket): float(value)
                for bucket, value in json.loads(probabilities_json).items()
            }
        except (ValueError, TypeError):
            first_decision.pop(key, None)

    entry_asks: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    last_bid: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    # Which observation each surviving bid came from, so the ORDER BY can
    # go.  Sorting was doing one job -- letting the assignment below
    # overwrite in time order so the LAST non-zero bid wins -- and it was
    # charging the whole result set for it: ``ORDER BY observed_at`` has no
    # index to ride, so SQLite built a temp B-tree over every row, and the
    # row it has to carry through that sort is ``buckets_json`` -- 2.05 GB
    # of it across ~105k snapshots as of 2026-09-02.  The order was never
    # needed globally either; only within a station-day-bucket.  Taking the
    # max explicitly keeps the same winner, drops the sort, and lets the
    # scan stay on ``day_expiry_idx`` where ``event_date >=`` can seek.
    #
    # The comparison is on the ISO-8601 string, which is what the freshness
    # test four lines down already does; both are UTC with a trailing Z, so
    # lexical order is time order.
    last_bid_at: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for station, event, observed, buckets_json in conn.execute(
        "SELECT station_id, event_date, observed_at, buckets_json"
        " FROM basket_official_ws_snapshot WHERE event_date >= ?",
        (TRUTH_LANDING_SINCE,),
    ):
        key = (str(station), str(event))
        decided = first_decision.get(key)
        if decided is None:
            continue
        try:
            rows = json.loads(buckets_json)
        except (ValueError, TypeError):
            continue
        if not isinstance(rows, list):
            continue
        in_entry = False
        if str(observed) >= decided:
            try:
                opened = datetime.fromisoformat(
                    decided.replace("Z", "+00:00")
                )
                seen = datetime.fromisoformat(
                    str(observed).replace("Z", "+00:00")
                )
                in_entry = (seen - opened).total_seconds() <= 4 * 3600
            except ValueError:
                in_entry = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            bucket = str(row.get("bucket"))
            levels = row.get("ask_levels") or []
            ask = levels[0].get("price") if levels else None
            bid = row.get("bid_price") or 0
            bid_levels = row.get("bid_levels") or []
            if not bid and bid_levels:
                bid = bid_levels[0].get("price") or 0
            if in_entry and ask and float(ask) > 0:
                # Order-independent: these are collected to take a median.
                entry_asks[(key[0], key[1], bucket)].append(float(ask))
            if bid and float(bid) > 0:
                seen_at = str(observed)
                if seen_at >= last_bid_at[key].get(bucket, ""):
                    last_bid[key][bucket] = float(bid)
                    last_bid_at[key][bucket] = seen_at

    winners: dict[tuple[str, str], str] = {}
    for key, bids in last_bid.items():
        bucket, top = max(bids.items(), key=lambda item: item[1])
        if top >= 0.80:
            winners[key] = bucket

    def banded(
        bands: tuple[tuple[float, float], ...],
        rows: list[tuple[int, float]],
    ) -> list[dict[str, Any]]:
        cells: dict[tuple[float, float], list[float]] = {
            band: [0.0, 0.0, 0.0] for band in bands
        }
        for hit, value in rows:
            for low, high in bands:
                if low <= value < high:
                    cell = cells[(low, high)]
                    cell[0] += hit
                    cell[1] += 1
                    cell[2] += value
                    break
        total_truth = sum(cell[0] for cell in cells.values()) or 1.0
        return [
            {
                "band": [low, high],
                "truth_count": int(cell[0]),
                "bucket_count": int(cell[1]),
                "truth_share": cell[0] / total_truth,
                "hit_rate": (cell[0] / cell[1]) if cell[1] else None,
                "value_mean": (cell[2] / cell[1]) if cell[1] else None,
            }
            for (low, high), cell in cells.items()
        ]

    price_rows: list[tuple[int, float]] = []
    for (station, event, bucket), asks in entry_asks.items():
        key = (station, event)
        if key not in winners or len(asks) < 2:
            continue
        price_rows.append(
            (1 if winners[key] == bucket else 0, median(asks))
        )
    claim_rows: list[tuple[int, float]] = []
    for key, claimed in claims.items():
        if key not in winners:
            continue
        for bucket, value in claimed.items():
            claim_rows.append((1 if winners[key] == bucket else 0, value))

    # [buckets, hits, sum of claims, sum of prices] per group.
    gaps: dict[str, list[float]] = {
        key: [0, 0, 0.0, 0.0] for key, *_ in TRUTH_LANDING_GAP_GROUPS
    }
    unlikely = [0, 0]
    # The acceptance sample's baseline (user ruling, 2026-09-17): the buckets
    # the model claims at 0.45 or more while the market asks under 0.30 --
    # the shape the band table cannot see, on which the lanes that read the
    # market are judged first.  [buckets, hits, sum of claims, sum of prices].
    from bayes_basket_target.candidate_window_evidence import (  # noqa: PLC0415
        ACCEPTANCE_CLAIM_FLOOR,
        ACCEPTANCE_MARKET_CEILING,
        in_acceptance_class,
    )
    acceptance = [0, 0, 0.0, 0.0]
    # The ladder's ask mass per station-day, so the three-knob proxy can
    # read the market as a normalised belief the way the lanes do.
    ladder_ask: dict[tuple[str, str], float] = defaultdict(float)
    for (station, event, bucket), asks in entry_asks.items():
        if (station, event) in winners and len(asks) >= 2:
            ladder_ask[(station, event)] += median(asks)
    knob_rows: list[tuple[str, float, float, int]] = []
    for (station, event, bucket), asks in entry_asks.items():
        key = (station, event)
        if key not in winners or len(asks) < 2:
            continue
        claim = claims.get(key, {}).get(bucket)
        if claim is None:
            continue
        price = median(asks)
        hit = 1 if winners[key] == bucket else 0
        if ladder_ask.get(key, 0.0) > 0:
            knob_rows.append((event, claim, price / ladder_ask[key], hit))
        if in_acceptance_class(claim, price):
            acceptance[0] += 1
            acceptance[1] += hit
            acceptance[2] += claim
            acceptance[3] += price
        if claim < TRUTH_LANDING_UNLIKELY and price < TRUTH_LANDING_UNLIKELY:
            unlikely[0] += 1
            unlikely[1] += hit
            continue
        gap = claim - price
        for name, _, low, high in TRUTH_LANDING_GAP_GROUPS:
            if (low is None or gap >= low) and (high is None or gap < high):
                cell = gaps[name]
                cell[0] += 1
                cell[1] += hit
                cell[2] += claim
                cell[3] += price
                break

    return {
        "since": TRUTH_LANDING_SINCE,
        "closed_station_days": len(first_decision),
        "adjudicated_station_days": len(winners),
        "capture_truncated_days": len(first_decision) - len(winners),
        "by_price": banded(TRUTH_LANDING_PRICE_BANDS, price_rows),
        "by_claim": banded(TRUTH_LANDING_PRICE_BANDS, claim_rows),
        "three_knob_proxy": three_knob_fit(knob_rows, since=TRUTH_LANDING_SINCE),
        "disagreement": {
            "unlikely_below": TRUTH_LANDING_UNLIKELY,
            "unlikely_buckets": unlikely[0],
            "unlikely_hits": unlikely[1],
            "acceptance_baseline": {
                "claim_floor": ACCEPTANCE_CLAIM_FLOOR,
                "market_ceiling": ACCEPTANCE_MARKET_CEILING,
                "buckets": acceptance[0],
                "hits": acceptance[1],
                "hit_rate": (acceptance[1] / acceptance[0]) if acceptance[0] else None,
                "claim_mean": (acceptance[2] / acceptance[0]) if acceptance[0] else None,
                "price_mean": (acceptance[3] / acceptance[0]) if acceptance[0] else None,
            },
            "groups": [
                {
                    "key": name, "label": label,
                    "buckets": int(gaps[name][0]),
                    "hits": int(gaps[name][1]),
                    "claim_mean": (
                        gaps[name][2] / gaps[name][0] if gaps[name][0] else None
                    ),
                    "price_mean": (
                        gaps[name][3] / gaps[name][0] if gaps[name][0] else None
                    ),
                    "hit_rate": (
                        gaps[name][1] / gaps[name][0] if gaps[name][0] else None
                    ),
                }
                for name, label, _, _ in TRUTH_LANDING_GAP_GROUPS
            ],
        },
    }


def collect_truth_materialization(conn: sqlite3.Connection) -> dict[str, Any]:
    """Whether every settled station-day has been turned into a truth row.

    The distinction this section exists to draw: a missing truth row almost
    never means a missing observation.  On 2026-08-17 the table held 13 of 45
    stations for 08-11 while the archive held all 45 that day -- the rows had
    simply never been asked for, and nothing retries a day once it passes.
    Everything downstream is gated on settled days, so a hole here does not
    cost one scope one day, it costs every cohort that would have shared it.
    """

    coverage = [
        {
            "event_date": str(day),
            "eligible": int(eligible),
            "materialized": int(materialized or 0),
            "missing": max(0, int(eligible) - int(materialized or 0)),
        }
        for day, eligible, materialized in conn.execute(
            "SELECT r.event_date, COUNT(DISTINCT r.station_id),"
            " (SELECT COUNT(DISTINCT t.station_id)"
            "  FROM calibration_station_daily_high_truth t"
            "  WHERE t.local_date=r.event_date AND t.qc_status='ACCEPTED')"
            " FROM basket_target_market_registry r"
            " WHERE r.source_eligible=1"
            "  AND r.event_date>=date('now','-21 days')"
            " GROUP BY r.event_date ORDER BY r.event_date DESC"
        )
    ]
    sweep: dict[str, Any] = {}
    if TRUTH_SWEEP_STATUS.is_file():
        try:
            sweep = json.loads(TRUTH_SWEEP_STATUS.read_text())
        except (ValueError, OSError):
            sweep = {}
    # A day still open somewhere is not a gap.  The fleet spans every
    # timezone, so an event date stays unsettleable for up to a day and a half
    # after it begins in UTC, and counting those rows as missing would make a
    # healthy lane look permanently a day behind.  Which days those are is not
    # computed from dates here: any rule this page invented would be wrong for
    # some station at some hour.  The sweep asks the writer itself, scope by
    # scope, and reports back how many it declined as still open on each day.
    open_by_day = sweep.get("open_by_day") or {}
    for row in coverage:
        row["settling"] = int(open_by_day.get(row["event_date"], 0)) > 0
    still_open = int(sweep.get("open_day_skipped") or 0)
    refused = int(sweep.get("refused") or 0)
    deferred = int(sweep.get("deferred_locked") or 0)
    outstanding = max(
        0,
        int(sweep.get("backlog_after") or 0)
        - still_open - refused - deferred,
    )
    age = _age_hours(sweep.get("swept_at_utc"))
    # The cycle materializes the live edge itself now (2026-08-30), so the
    # sweep's clock alone no longer decides whether anything would retry a
    # missing day.  Freshness is whichever engine asked most recently, and
    # the alarm is graded on actual harm: red means settled station-days
    # with no truth row and nothing fresh retrying; a stale engine with a
    # complete book is a scheduling observation, not a data emergency --
    # that conflation cried wolf on every deploy day.
    worker_truth: dict[str, Any] = {}
    resident_started_at: str | None = None
    try:
        status_document = json.loads(STATUS_JSON.read_text())
        worker_truth = (
            status_document.get("production", {})
            .get("truth_materialization") or {}
        )
        resident_started_at = status_document.get("started_at") or None
    except (OSError, ValueError, AttributeError):
        worker_truth = {}
    cycle_age = _age_hours(worker_truth.get("checked_at_utc"))
    # The status document is rebuilt from nothing at every boot and the
    # production block lands in it only when the first cycle completes, so
    # for the ten-odd minutes after a deploy the cycle engine has no
    # receipt to show.  That is not an engine that stopped: it is one that
    # has not yet come round once.  Measured 2026-09-02 20:30: the board
    # rendered five minutes after a restart, read no receipt, and graded a
    # complete book amber on the sweep's clock alone -- the same false
    # alarm this section was rewritten on 2026-08-30 to stop crying.  A
    # just-started resident is fresh as of its start; if it never completes
    # a cycle, that clock ages into a real alarm on its own.
    boot_age = (
        _age_hours(resident_started_at) if cycle_age is None else None
    )
    fresh_age = min(
        (
            value for value in (age, cycle_age, boot_age)
            if value is not None
        ),
        default=None,
    )
    if cycle_age is not None:
        cycle_engine = f"周期增量 {cycle_age:.1f}h 前"
    elif boot_age is not None:
        cycle_engine = (
            f"周期增量：常驻 {boot_age * 60:.0f} 分钟前重启，首轮未到"
        )
    else:
        cycle_engine = "周期增量还没有回执"
    engines = "；".join((
        f"清扫 {age:.1f}h 前" if age is not None else "清扫从未跑过",
        cycle_engine,
    ))
    if outstanding and (fresh_age is None or fresh_age > 3):
        health, note = "red", (
            f"{outstanding} 个已结算站日缺真值，且两台重试引擎都停了"
            f"（{engines}）"
        )
    elif outstanding:
        health, note = "amber", (
            f"{outstanding} 个已结算站日还没有真值行（{engines}）"
        )
    elif fresh_age is None or fresh_age > 3:
        health, note = "amber", f"覆盖完整，但重试引擎陈旧（{engines}）"
    elif deferred:
        health, note = "green", f"{deferred} 个站日让给了常驻写入，下一轮再问"
    elif refused:
        # Naming the permanent share keeps this line from reading as a backlog
        # that is about to clear.  Nine of these predate their own AWC capture
        # and sit outside the endpoint's thirty-day retention: they are the
        # answer, not a queue.
        expired = sum(
            int(item.get("expired_count") or 0)
            for item in (sweep.get("refusals") or [])
            if isinstance(item, dict)
        )
        health, note = "green", (
            f"已结算的都已物化；{refused} 个站日被证据本身挡住"
            + (f"，其中 {expired} 个源头已过期、补不回来" if expired else "")
        )
    else:
        health, note = "green", "已结算的站日全部有真值行"
    return {
        "coverage": coverage,
        "sweep": sweep,
        "health": health,
        "note": note,
        "sweep_age_hours": age,
        "cycle_age_hours": cycle_age,
        "resident_boot_age_hours": boot_age,
        "outstanding": outstanding,
        "still_open": still_open,
        "refused": refused,
        "deferred": deferred,
        "refusals": sweep.get("refusals") or [],
    }


def collect_oos_scores(conn: sqlite3.Connection) -> dict[str, Any]:
    """The gate's own scores, against the baseline that makes them readable.

    A CRPS of 0.80 means nothing alone -- it depends on the station, the
    season and how wide the buckets are.  What makes it a judgement is the
    same number for climatology on the same days: the gate admits a
    combination only when it beats that, and the ratio is the one figure that
    is comparable across scopes.
    """

    newest: dict[tuple[str, str, str], tuple[str, Any, Any, str, str]] = {}
    for (
        station, lead, cycle, evaluated, state, profile, metrics, baseline
    ) in conn.execute(
        "SELECT station_id, lead_bucket, run_cycle, evaluated_at_utc,"
        " oos_state, threshold_profile, oos_metrics_json,"
        " climatology_baseline_metrics_json"
        " FROM calibration_cross_native_ensemble_oos_evaluation"
        " WHERE evaluated_at_utc>=datetime('now','-7 days')"
        " ORDER BY evaluated_at_utc"
    ):
        newest[(str(station), str(lead), str(cycle))] = (
            str(evaluated), metrics, baseline, str(state), str(profile)
        )
    cells: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "scopes": 0, "passed": 0,
        "crps": [], "brier": [], "nll": [],
        "crps_base": [], "brier_base": [], "nll_base": [],
        "crps_skill": [], "brier_skill": [], "profiles": Counter(),
    })
    latest = ""
    for (_station, lead, _cycle), row in newest.items():
        evaluated, metrics_json, baseline_json, state, profile = row
        try:
            metrics = json.loads(metrics_json)
            baseline = json.loads(baseline_json)
        except (ValueError, TypeError):
            continue
        cell = cells[lead]
        cell["scopes"] += 1
        cell["profiles"][profile] += 1
        if state == "PRODUCTION_NO_LIVE_OOS_PASS":
            cell["passed"] += 1
        latest = max(latest, evaluated)
        for name in ("crps", "brier", "nll"):
            value = metrics.get(name)
            reference = baseline.get(name)
            if value is None or reference is None:
                continue
            cell[name].append(float(value))
            cell[f"{name}_base"].append(float(reference))
            if float(reference) > 0 and name in ("crps", "brier"):
                cell[f"{name}_skill"].append(
                    1.0 - float(value) / float(reference)
                )
    order = {name: index for index, (name, _l) in enumerate(LEAD_LABELS)}
    rows = []
    for lead in sorted(cells, key=lambda name: order.get(name, 9)):
        cell = cells[lead]
        rows.append({
            "lead_bucket": lead,
            "lead_label": dict(LEAD_LABELS).get(lead, lead),
            "scopes": cell["scopes"],
            "pass_rate": (
                cell["passed"] / cell["scopes"] if cell["scopes"] else None
            ),
            "profile": (
                cell["profiles"].most_common(1)[0][0]
                if cell["profiles"] else ""
            ),
            **{
                key: (median(cell[key]) if cell[key] else None)
                for key in (
                    "crps", "brier", "nll", "crps_base", "brier_base",
                    "nll_base", "crps_skill", "brier_skill",
                )
            },
        })
    return {
        "rows": rows,
        "scope_count": len(newest),
        "latest_evaluated_at_utc": latest or None,
        "latest_age_hours": _age_hours(latest or None),
    }


def collect_serving_weights(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every active P0 blend, spelled out: which models, at what weight.

    Section three counts the weight tables; this reads the weights
    themselves off the serving path -- newest activation pointer per scope,
    the artifact it binds, the selection evidence whose contest produced the
    weights -- so the page shows what P0 actually blends, not a census of
    the tables it is stored in.  The holdout end rides along because the
    fleet rebinds gradually after a re-selection: a scope still serving an
    old contest is visible here as an old window, not as a mystery.
    """

    labels = {system: label for system, label, _t, _b in MEMBER_FAMILIES}
    rows: list[dict[str, Any]] = []
    for (
        station, season, lead, cycle, weights_json,
        candidates_json, selected_json, holdout_end, train_end,
    ) in conn.execute(
        "SELECT p.station_id, p.season_bin, p.lead_bucket, p.run_cycle,"
        " se.selected_weights_json, se.candidate_system_ids_json,"
        " se.selected_system_ids_json, se.holdout_end_date,"
        " se.train_end_date"
        " FROM (SELECT station_id, season_bin, lead_bucket, run_cycle,"
        "   artifact_id, MAX(generation)"
        "  FROM calibration_native_ensemble_activation"
        "  WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE'"
        # Charter 62: only rows in the station-local vocabulary are the
        # serving path now.  Without this filter the legacy calendar rows
        # (which nothing queries but which stay valid for up to 30 days)
        # render as a second column per scope and pollute the seat counts.
        "  AND season_bin IN"
        "   ('PEAK','DESCENT','TROUGH','ASCENT','ALLYEAR')"
        "  AND valid_until_utc > strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        "  GROUP BY station_id, season_bin, lead_bucket, run_cycle) p"
        " JOIN calibration_cross_native_ensemble_artifact ar"
        "  ON ar.cross_ensemble_artifact_id = p.artifact_id"
        " JOIN calibration_cross_native_ensemble_selection_evidence se"
        "  ON se.content_sha256 = ar.selection_evidence_sha256"
    ):
        try:
            weights = json.loads(weights_json)
            candidates = json.loads(candidates_json)
            selected = json.loads(selected_json)
        except (ValueError, TypeError):
            continue
        rows.append({
            "station": str(station),
            "season": str(season),
            "lead_bucket": str(lead),
            "lead_label": dict(LEAD_LABELS).get(str(lead), str(lead)),
            "run_cycle": str(cycle),
            "weights": {
                labels.get(str(system), str(system)): float(value)
                for system, value in weights.items()
            },
            "candidates": [
                labels.get(str(system), str(system))
                for system in candidates
            ],
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "holdout_end": str(holdout_end),
            "train_end": str(train_end),
        })
    lead_order = {name: i for i, (name, _l) in enumerate(LEAD_LABELS)}
    rows.sort(key=lambda r: (
        r["station"], lead_order.get(r["lead_bucket"], 9), r["run_cycle"]
    ))
    newest_holdout = max((r["holdout_end"] for r in rows), default="")
    stale_cut = ""
    if newest_holdout:
        stale_cut = str(
            (datetime.fromisoformat(newest_holdout) - timedelta(days=2))
            .date()
        )
    summary = []
    for _system, label, _transport, _backfillable in MEMBER_FAMILIES:
        seated = [r for r in rows if label in r["weights"]]
        candidacies = sum(label in r["candidates"] for r in rows)
        by_lead = {}
        for lead, _lead_label in LEAD_LABELS:
            lead_weights = [
                r["weights"][label] for r in seated
                if r["lead_bucket"] == lead
            ]
            by_lead[lead] = {
                "seats": len(lead_weights),
                "median_weight": (
                    median(lead_weights) if lead_weights else None
                ),
            }
        summary.append({
            "label": label,
            "seats": len(seated),
            "candidacies": candidacies,
            "eliminated": candidacies - len(seated),
            "median_weight": (
                median(r["weights"][label] for r in seated)
                if seated else None
            ),
            "by_lead": by_lead,
        })
    return {
        "rows": rows,
        "summary": summary,
        "scope_count": len(rows),
        "newest_holdout_end": newest_holdout or None,
        "stale_cut": stale_cut or None,
        "stale_count": sum(
            1 for r in rows if stale_cut and r["holdout_end"] < stale_cut
        ),
    }


def _band_axis_by_stage(conn: sqlite3.Connection) -> dict[str, str]:
    """Which quantity each activated band table's cells are keyed on.

    Charter 105 put the axis on the table, and the two partitions take the
    same shape -- a reader that guesses would render one table through the
    other's grid and be wrong in a way that still looks like a table.  The
    store owns ``band_table_axis``, but it calls ``ensure_band_table_schema``
    first and this page holds a read-only connection, so the column is read
    here and the *rule* (which cells, which key) still comes from the store.
    One helper rather than one per panel: a retired axis with two readers is
    how half the page keeps rendering the old one.
    """

    from bayes_basket_target.band_reliability_store import BAND_AXIS_CLAIM

    if not _table_exists(conn, "calibration_band_reliability_table"):
        return {}
    # The column is absent on a database written before 2026-09-07, where
    # every table was fitted on the claim.
    has_axis = any(
        str(col[1]) == "axis"
        for col in conn.execute(
            "PRAGMA table_info(calibration_band_reliability_table)"
        )
    )
    if not has_axis:
        return {}
    # The axis is a property of the TABLE, so a stage whose table was vetoed
    # has no activated row to read it from -- and every caller's fallback is
    # ``claim``, which is how a distance-keyed stage silently gets binned on
    # the 5% claim grid again (2026-09-15: P0's table was retired at 18:26Z
    # and this returned nothing for P0).  The newest row for the stage still
    # remembers which quantity its cells were keyed on, retired or not, and
    # that is the axis the rows being read were served through.
    axes = {
        str(stage): str(axis or BAND_AXIS_CLAIM)
        for stage, axis in conn.execute(
            "SELECT stage, axis FROM calibration_band_reliability_table"
            " WHERE activated=1"
        )
    }
    for stage, axis in conn.execute(
        "SELECT stage, axis FROM calibration_band_reliability_table"
        " WHERE stage NOT IN (SELECT stage FROM"
        "  calibration_band_reliability_table WHERE activated=1)"
        " GROUP BY stage HAVING fitted_at_utc = MAX(fitted_at_utc)"
    ):
        axes.setdefault(str(stage), str(axis or BAND_AXIS_CLAIM))
    return axes


def collect_band_residual_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    """Post-correction residuals per stage, on the band table's own ruler.

    Same lane, same clock, same window as the knob table above it: the
    band store's own ``serving_lane_by_day`` -- each station-day's FIRST
    decision (the 2026-08-24 fit-clock ruling) since the stage's served-
    arithmetic anchor, the fit's own evidence floor, per stage -- with
    exactly one difference, stated by the flag: the claims are kept as
    served, factors included, where the fit inverts them back to pre-band.
    The per-station court is the exception and reads the whole history.  So the two tables are the same population read on either
    side of the correction: the knob row says what the raw claim realised
    and what factor that earned; this row says what remains after the
    factor.  An earlier version of this audit read the training lane's
    OOS predictions (different population, no stage, lead vocabulary) and
    then a 3-hourly anchored sample (different clock and window); each
    mismatch surfaced as a fake contradiction on the page.
    """

    from bayes_basket_target.band_reliability_store import (
        BAND_AXIS_CENTRE_DISTANCE,
        BAND_AXIS_CLAIM,
        _record_keys,
        cells_for_axis,
        serving_lane_by_station_day,
    )
    from bayes_basket_target.center_width_store import (
        _truth,
        served_arithmetic_anchor,
    )

    truth = _truth(conn)
    axis_by_stage = _band_axis_by_stage(conn)

    def cell_labels(axis: str, cells) -> list[str]:
        """Row names for a partition, worded as the knob table words them."""

        if axis == BAND_AXIS_CENTRE_DISTANCE:
            out = []
            for low, high in cells:
                step = int(round(low))
                out.append(
                    "\u5cf0\u503c\u6876" if step == 0
                    else (f"\u79bb\u5cf0 \u2265{step} \u683c" if high >= 90
                          else f"\u79bb\u5cf0 {step} \u683c")
                )
            return out
        return [
            "\u226550%" if low >= 0.5 else f"{low * 100:g}-{high * 100:g}%"
            for low, high in cells
        ]

    def moved_station_days(scored_lane, served_lane):
        """Each station-day a band factor actually moved, both readings."""

        for (station, event), day_rows in served_lane.items():
            scored_rows = scored_lane.get((station, event)) or []
            # A residual "after the correction" may only count rows the
            # correction reached.  Two thirds of P0's window predate the band
            # receipt entirely (the earliest 14 days are 100% uncorrected,
            # the most recent 14 are 100% corrected), and P0-M still skips a
            # sixth of its station-days because the support set holds no
            # band-eligible bucket.  Pooling those in damps every reading
            # toward zero: P1's peak showed +3.6 points mixed and +4.4 on the
            # days the layer actually ran (measured 2026-09-09).
            #
            # The test is the two lanes themselves, not a re-read of the
            # receipt: ``serving_lane_by_station_day`` inverts if and only if
            # a receipt said APPLIED, so a station-day whose served vector
            # equals its pre-band vector is one no factor moved.  Checked
            # against the receipts on 3,729 station-days: 3,728 agree, and
            # the one that does not is an APPLIED table that was identity on
            # that support set -- a row with nothing to audit either way.
            moved = any(
                abs(before[0] - after[0]) > 1e-12
                for scored_row, served_row in zip(scored_rows, day_rows)
                for before, after in zip(scored_row, served_row)
            )
            yield station, event, day_rows, scored_rows, moved

    stages_out = []
    # Per station, pooled across the three stages: claimed centre+-1 mass
    # against whether truth landed there, the promise-conservation ruler.
    # This is the court for "does band need station structure": the band
    # tables are deliberately fleet-pooled, so a station whose ratio sits
    # significantly off 1 after them is upstream evidence, not band's.
    station_cells: dict[str, list[float]] = {}
    for stage in ("P0", "P0-M", "P1"):
        axis = axis_by_stage.get(stage, BAND_AXIS_CLAIM)
        partition = cells_for_axis(axis)
        labels = cell_labels(axis, partition)
        try:
            # Two readings of one population.  The keys come from the
            # PRE-band vector because that is the vector production had
            # when it chose which cell's factor to apply; the claim and the
            # hit come from the served vector, factor included, which is
            # what "what is left after the correction" means.  Reading the
            # cell off the served claim instead moved 7.1% of P0's rungs
            # into a neighbour's row (measured 2026-09-09) -- an audit that
            # scores a rung under a factor it never received.
            scored_lane = serving_lane_by_station_day(
                conn, truth, stage=stage, pre_band=True,
            )
            served_lane = serving_lane_by_station_day(
                conn, truth, stage=stage, pre_band=False,
            )
            # The stage rows are read against the knob card above, so they
            # read the fit's own window: the served-arithmetic anchor the
            # fit and its incumbent veto have both been bounded by since
            # 2026-09-10.  Unbounded they pooled every table the stage had
            # ever served into today's rungs -- measured 2026-09-15, 1,025 of
            # P0's 1,225 audited station-days were corrected by claim-axis
            # tables and only 108 by the table on the card; all 651 of
            # P0-M's were claim-axis, under a card whose table is identity.
            # The station court below keeps the whole history: it is a
            # per-station claim-honesty reading the go-live gate consumes,
            # and a window that restarts at every arithmetic change would
            # leave it a handful of station-days per station.
            anchor = served_arithmetic_anchor(conn, stage=stage)
            if anchor:
                scored_since = serving_lane_by_station_day(
                    conn, truth, stage=stage, pre_band=True,
                    min_decision_at_utc=anchor,
                )
                served_since = serving_lane_by_station_day(
                    conn, truth, stage=stage, pre_band=False,
                    min_decision_at_utc=anchor,
                )
            else:
                scored_since, served_since = scored_lane, served_lane
        except Exception:  # noqa: BLE001 -- an audit must not blank the page
            continue
        for station, _event, day_rows, _scored, moved in moved_station_days(
            scored_lane, served_lane
        ):
            if not moved:
                continue
            for entries in day_rows:
                if entries:
                    distances = _record_keys(
                        [(claim, hit) for claim, hit, _low in entries],
                        BAND_AXIS_CENTRE_DISTANCE,
                    )
                    centre = distances.index(0.0)
                    window = entries[max(0, centre - 1):centre + 2]
                    slot = station_cells.setdefault(station, [0, 0.0, 0])
                    slot[0] += 1
                    slot[1] += sum(claim for claim, _hit, _low in window)
                    slot[2] += 1 if any(
                        hit for _claim, hit, _low in window
                    ) else 0
        cells = {index: [0, 0.0, 0] for index in range(len(partition))}
        days = set()
        seen_station_days = 0
        corrected_station_days = 0
        for _station, event, day_rows, scored_rows, moved in (
            moved_station_days(scored_since, served_since)
        ):
            seen_station_days += 1
            if not moved:
                continue
            corrected_station_days += 1
            days.add(event)
            for position, entries in enumerate(day_rows):
                scored = (
                    scored_rows[position]
                    if position < len(scored_rows)
                    and len(scored_rows[position]) == len(entries)
                    else entries
                )
                # The store owns the mapping from a station-day's rungs to
                # their cell key on either axis; copying it here is how an
                # audit invents findings the layer it audits never made.  It
                # takes (claim, hit) pairs -- the same projection
                # ``serving_lane_by_day`` makes -- and the order is the
                # ordinal the distance axis is read from, so it is kept.
                keys = _record_keys(
                    [(claim, hit) for claim, hit, _low in scored], axis
                )
                for (claim, hit, _lower), value in zip(entries, keys):
                    for index, (low, high) in enumerate(partition):
                        if low <= value < high:
                            slot = cells[index]
                            slot[0] += 1
                            slot[1] += float(claim)
                            slot[2] += int(hit)
                            break
        rows = []
        for index, label in enumerate(labels):
            n, promised, hits = cells[index]
            if not n:
                continue
            rows.append({
                "bin": label,
                "n": n,
                "promised": promised / n,
                "happened": hits / n,
            })
        stages_out.append({
            "stage": stage,
            "since": anchor,
            "axis": axis,
            "day_count": len(days),
            "station_days": corrected_station_days,
            "skipped_station_days": seen_station_days - corrected_station_days,
            "rows": rows,
        })
    stations = []
    for station, (n, promised, hits) in sorted(station_cells.items()):
        if not n or promised <= 0:
            continue
        claimed = promised / n
        realized = hits / n
        # Binomial 2-sigma on the realized rate, read in ratio units; a
        # stratum whose |ratio-1| clears it is marked, not gated.
        sigma = (
            (realized * (1.0 - realized) / n) ** 0.5 / claimed
            if 0.0 < realized < 1.0 else 1.0 / (n * claimed)
        )
        ratio = realized / claimed
        stations.append({
            "station": station,
            "station_label": STATION_LABELS.get(station, ""),
            "n": n,
            "claimed": claimed,
            "realized": realized,
            "ratio": ratio,
            "significant": abs(ratio - 1.0) > 2.0 * sigma,
        })
    stations.sort(key=lambda row: row["ratio"])
    return {"stages": stages_out, "stations": stations}


def collect_band_shadow(conn: sqlite3.Connection) -> dict[str, Any]:
    """The shadow band layer's standing: which dimension earns its keep.

    The served table (section six) is pooled -- one factor per stage per
    claim band, the same for forty-five stations and every bucket in a
    ladder.  The shadow fits that same evidence with two extra dimensions
    and serves none of it; this panel is how the accumulation is read.

    The number that matters is per *level*, never the total.  A fit that
    promotes five fleet-wide position cells and lets two hundred fine
    cells inherit them reads as "209 qualified" on a total, which is the
    one reading that would be false.
    """

    if not _table_exists(conn, "calibration_band_shadow_journal"):
        return {}
    from bayes_basket_target.band_shadow_store import (
        BACKOFF_LEVELS,
        MIN_CELL_CLAIMED_MASS,
        MIN_CELL_DAYS,
        MIN_CELL_ROWS,
        FDR_Q,
    )

    LEVEL_LABEL = {
        "station_position": "站 × 桶位置",
        "station": "站",
        "position": "桶位置",
    }
    GROUP_LABEL = {
        "cool_far": "偏冷远端",
        "cool_near": "偏冷邻桶",
        "centre": "峰值桶",
        "warm_near": "偏暖邻桶",
        "warm_far": "偏暖远端",
    }
    BLOCK_LABEL = {
        "gate_a_insufficient_mass": "样本不够（一门）",
        "gate_b_halves_not_stable": "前后半段不稳（二门）",
        "gate_b_fdr_not_significant": "多重校正后不显著（二门）",
        "gate_c_forward_window_not_improved": "试算反而变差（三门）",
    }
    stages: list[dict[str, Any]] = []
    for stage in ("P0", "P0-M", "P1"):
        rows = conn.execute(
            "SELECT fitted_at_utc, shadow_sha256, serving_day_count,"
            " serving_station_day_count, cell_count, promoted_cell_count,"
            " forward_nll_gain, evidence_json"
            " FROM calibration_band_shadow_journal"
            " WHERE stage=? ORDER BY fitted_at_utc DESC LIMIT 8",
            (stage,),
        ).fetchall()
        if not rows:
            continue
        latest = rows[0]
        try:
            evidence = json.loads(latest[7])
        except (TypeError, ValueError):
            evidence = {}
        levels = evidence.get("levels") or {}
        level_rows = []
        for level in BACKOFF_LEVELS[:-1]:
            report = levels.get(level)
            if not report:
                continue
            blocked = report.get("blocked") or {}
            level_rows.append({
                "level": level,
                "label": LEVEL_LABEL.get(level, level),
                "tested": report.get("tested", 0),
                "eligible": report.get("eligible", 0),
                "promoted": report.get("promoted", 0),
                "blocked": [
                    {"reason": BLOCK_LABEL.get(key, key), "n": value}
                    for key, value in sorted(
                        blocked.items(), key=lambda kv: -kv[1]
                    )
                ],
            })
        promoted = []
        for key, factor in sorted((evidence.get("promoted") or {}).items()):
            try:
                parsed = ast.literal_eval(key)
            except (ValueError, SyntaxError):
                continue
            station, group, band = parsed
            promoted.append({
                "station": None if station == "*" else station,
                "group": group,
                "group_label": GROUP_LABEL.get(group, group),
                "band": [float(band[0]), float(band[1])],
                "factor": float(factor),
            })
        promoted.sort(key=lambda row: (row["band"][0], row["group"]))
        stages.append({
            "stage": stage,
            "meaning": STAGE_MEANING.get(stage, ""),
            # The shadow follows the pooled table's axis (2026-09-09), so a
            # promoted key's band is a distance rung on P0 and P1: printed
            # as a probability it read "100–100%" and "400–100%".
            "axis": str(evidence.get("axis") or "claim"),
            "fitted_at": latest[0],
            "fit_age_hours": _age_hours(latest[0]),
            "sha": str(latest[1])[:12],
            "serving_days": int(latest[2]),
            "station_days": int(latest[3]),
            "cell_count": int(latest[4]),
            "inherited_cell_count": int(latest[5]),
            "forward_nll_gain": float(latest[6]),
            "levels": level_rows,
            "promoted": promoted,
            "journal": [
                {
                    "at": row[0],
                    "sha": str(row[1])[:12],
                    "days": int(row[2]),
                    "promoted": int(row[5]),
                    "gain": float(row[6]),
                }
                for row in rows
            ],
        })
    if not stages:
        return {}
    return {
        "stages": stages,
        "gates": {
            "min_claimed_mass": MIN_CELL_CLAIMED_MASS,
            "min_days": MIN_CELL_DAYS,
            "min_rows": MIN_CELL_ROWS,
            "fdr_q": FDR_Q,
        },
        "distance": collect_band_shadow_distance(conn),
    }


def collect_band_shadow_distance(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """How far the starved levels are from the mass floor, in days.

    A level that promotes nothing reads the same on day one and on the day
    before it qualifies.  The fattest cell's mass against the floor, at
    the rate it has actually been accumulating, is the difference between
    "this is going nowhere" and "this needs three more weeks" -- and the
    ETA is the only actionable number on a panel of zeroes.
    """

    if not _table_exists(conn, "calibration_band_shadow_cell"):
        return []
    from bayes_basket_target.band_shadow_store import MIN_CELL_CLAIMED_MASS

    out: list[dict[str, Any]] = []
    for stage in ("P0", "P0-M", "P1"):
        sha_row = conn.execute(
            "SELECT shadow_sha256, serving_day_count"
            " FROM calibration_band_shadow_journal"
            " WHERE stage=? ORDER BY fitted_at_utc DESC LIMIT 1",
            (stage,),
        ).fetchone()
        if sha_row is None:
            continue
        sha, days = str(sha_row[0]), int(sha_row[1] or 0)
        if days <= 0:
            continue
        # The stored cells are the finest level; the coarser levels are
        # their sums, so both distances come from one scan.
        rows = conn.execute(
            "SELECT station_id, position_group, band_lower, claimed_mass"
            " FROM calibration_band_shadow_cell"
            " WHERE shadow_sha256=? AND stage=?",
            (sha, stage),
        ).fetchall()
        if not rows:
            continue
        fine: dict[tuple[str, str, float], float] = {}
        by_station: dict[tuple[str, float], float] = {}
        for station, group, lower, mass in rows:
            fine[(station, group, float(lower))] = float(mass)
            key = (station, float(lower))
            by_station[key] = by_station.get(key, 0.0) + float(mass)
        for level, masses in (
            ("station", by_station.values()),
            ("station_position", fine.values()),
        ):
            values = sorted(masses, reverse=True)
            if not values:
                continue
            best = values[0]
            reached = sum(1 for m in values if m >= MIN_CELL_CLAIMED_MASS)
            rate = best / days
            out.append({
                "stage": stage,
                "level": level,
                "cells": len(values),
                "best_mass": best,
                "floor": MIN_CELL_CLAIMED_MASS,
                "reached": reached,
                "days_now": days,
                "days_needed": (
                    None if rate <= 0
                    else max(0.0, MIN_CELL_CLAIMED_MASS / rate)
                ),
            })
    return out


def _live_execution_mode() -> str:
    """The mode the production lane is actually compiled for."""

    try:
        from bayes_basket_target.production import (  # noqa: PLC0415
            PRODUCTION_EXECUTION_MODE,
        )
        return str(PRODUCTION_EXECUTION_MODE)
    except Exception:
        return "unknown"


def _read_open_orders() -> dict[str, Any]:
    """What the exchange says is resting, right now.

    The gateway is the only thing allowed to ask, and it is a local
    read-only call; a failure is reported rather than raised, because a
    board that cannot reach the gateway should say so, not vanish.

    A read can fail three ways and the board must not print them alike.
    UNREACHABLE is nothing answering.  REFUSED is the gateway answering
    with ``ok`` false and its own account of why.  INCOMPLETE is an
    enumeration that stopped paging -- absence from a truncated list is
    nothing, so its total is a floor and not a count of the account.

    Measured 2026-09-10: two consecutive renders carried
    ``{"available": false, "count": 0}`` and the card read "gateway did
    not answer: unknown" for a gateway that had answered -- this function
    kept ``bool(ok)`` and dropped the reply's ``error``, so the one thing
    that said what went wrong never left this frame.  The account held a
    live order the whole time.

    Only a complete, ok enumeration yields ``count``.  A payload carrying
    a zero next to a failed read is how a reader learns to believe an
    unread account is empty.
    """

    import datetime as _dt  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    port = os.environ.get("PM_CLOB_ORDER_SERVICE_PORT", "8765")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/lookup-open-orders",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # The board is a static render; this row is a reading taken once, at
    # render time.  Without the stamp a stale page asserts a live zero --
    # and a failed read gets the stamp too, so "when did we last fail to
    # ask" is answerable from the page.
    asked_at = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {
            "available": False,
            "reason": "unreachable",
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "asked_at": asked_at,
        }
    if not raw.get("ok"):
        return {
            "available": False,
            "reason": "refused",
            "error": str(
                raw.get("error") or raw.get("status") or "ok_false_without_error"
            )[:200],
            "asked_at": asked_at,
        }
    if raw.get("complete") is not True:
        return {
            "available": False,
            "reason": "incomplete",
            "error": (
                f"enumeration stopped after {raw.get('pages_checked')} pages;"
                f" {raw.get('total_open_orders')} seen so far is a floor"
            )[:200],
            "asked_at": asked_at,
        }
    orders = raw.get("open_orders") or []
    return {
        "available": True,
        "reason": "ok",
        "count": int(raw.get("total_open_orders") or 0),
        "asked_at": asked_at,
        "orders": [
            {
                "id": str(o.get("id") or ""),
                "side": str(o.get("side") or ""),
                "price": o.get("price"),
                "original_size": o.get("original_size"),
                "size_matched": o.get("size_matched"),
                "outcome": str(o.get("outcome") or ""),
                "created_at": o.get("created_at"),
            }
            for o in orders[:20]
        ],
    }


def _read_chain_probe() -> dict[str, Any]:
    """The recorded live-order round trip, if one has been run."""

    path = ROOT / "data" / "chain_probe_receipt.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


# Charter 75 reads the plan's direction straight off the skew sign, so a
# plan minted before the shape was recorded still says which way the day
# leaned -- with one caveat the label carries: evidence that was not fresh
# reads NEUTRAL whatever the distribution looked like.
_SKEW_BY_DIRECTION = {
    "UP": "RIGHT_SKEWED", "DOWN": "LEFT_SKEWED", "NEUTRAL": "SYMMETRIC",
}


def _window_seconds(created_at: Any, deadline_at: Any) -> float | None:
    """Seconds a basket had to reach the exchange, or None if unreadable.

    Reported rather than assumed: the deadline is a fixed offset today, but
    a panel that prints the constant instead of the row's own two timestamps
    would keep printing it after the rule changed.  Unparseable reads as
    unknown -- a missing window is not a generous one.
    """

    try:
        start = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(deadline_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return round((end - start).total_seconds(), 3)


#: Where registry copies older than the SSOT's retention window still live.
#: The target registry keeps only snapshots a reader still queries, so a
#: live station-day from a week ago has no row there any more; the archive's
#: registry lane keeps every listing it saw, keyed the same way.
ARCHIVE_DB = ROOT / "data" / "archive.db"


def _live_market_slugs(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    archive: Path | None = None,
) -> dict[str, str]:
    """The market each live station-day traded, as ``"STATION|DATE" -> slug``.

    For the link on each station line (user ruling, 2026-09-15: 增加站点的超级
    连接，点击即可跳转市场盘口).  Read from a registry, never rebuilt from city
    and date -- the slug is Polymarket's identifier, and a station-day listed
    under another name would get a link that quietly 404s.  The SSOT registry
    first, newest refresh winning a re-listing; the archive's registry lane
    for the days the SSOT has retired (measured 2026-09-15: 47 of 88 live
    station-days, no disagreement on the 41 both carry).  A station-day in
    neither gets no link rather than a guessed one.
    """

    scopes = sorted({
        (str(r.get("station_id") or ""), str(r.get("event_date") or ""))
        for r in rows
        if r.get("station_id") and r.get("event_date")
    })
    out: dict[str, str] = {}

    def lookup(db: sqlite3.Connection, table: str) -> None:
        for station, day in scopes:
            key = f"{station}|{day}"
            if key in out:
                continue
            found = db.execute(
                f"SELECT slug FROM {table}"
                " WHERE station_id=? AND event_date=? AND slug IS NOT NULL"
                " AND slug<>'' ORDER BY refreshed_at DESC LIMIT 1",
                (station, day),
            ).fetchone()
            if found:
                out[key] = str(found[0])

    try:
        lookup(conn, "basket_target_market_registry")
    except sqlite3.Error:
        pass
    path = ARCHIVE_DB if archive is None else archive
    if len(out) < len(scopes) and path.is_file():
        try:
            archived = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, timeout=30.0
            )
            try:
                lookup(archived, "reported_trajectory_market_registry")
            finally:
                archived.close()
        except sqlite3.Error:
            pass
    return out


def _live_basket_plan_state(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """What each live basket's own plan recorded about the day, by decision.

    Two readings, both read back rather than derived.  ``center_bucket`` is
    the rung the optimiser seated the ladder on (charter 75: the mean
    landing bucket, moved by the skew rule, which is why it is not simply
    the middle of the window).  ``skew_reading`` is the shape that placed it
    -- symmetric, right-skewed, left-skewed -- with the mean-minus-mode gap
    in bucket steps and the threshold it was judged against.

    Deriving either here would mean re-implementing the per-station grid and
    skew rules the plan already applied, and the ledger's seat row is not a
    stand-in: measured across the whole live order book on 2026-09-06, 15 of
    53 baskets carry a ledger seat decided at another moment, and 6 of those
    name a centre that is not even one of the basket's legs.

    ``skew_reading`` exists only on plans compiled from 2026-09-06.  Older
    plans fall back to the plan's own ``direction``, which is the same skew
    sign under charter 75 with one difference worth naming: a day whose
    evidence was not fresh reads NEUTRAL whatever its shape.  So the two are
    labelled apart -- the fallback says it is the lean the plan acted on,
    not the shape it read.
    """

    ids = sorted({
        str(row["decision_id"]) for row in rows if row.get("decision_id")
    })
    if not ids or not _table_exists(conn, "basket_p0_preselection_plan"):
        return {}
    # decision -> the plan it was compiled from, and the scope to look in.
    wanted: dict[tuple[str, str], dict[str, list[str]]] = {}
    try:
        query = conn.execute(
            "SELECT decision_id, station_id, event_date, details_json"
            " FROM auto_trade_decisions WHERE decision_id IN (%s)"
            % ",".join("?" * len(ids)),
            ids,
        )
    except sqlite3.Error:
        return {}
    for decision_id, station, day, details in query:
        try:
            plan_sha = str(
                (json.loads(details or "{}") or {}).get(
                    "production_plan_sha256"
                ) or ""
            )
        except (TypeError, ValueError):
            plan_sha = ""
        if not plan_sha:
            continue
        scope = wanted.setdefault((str(station), str(day)), {})
        scope.setdefault(plan_sha, []).append(str(decision_id))
    state: dict[str, dict[str, Any]] = {}
    for (station, day), by_plan in wanted.items():
        try:
            plans = conn.execute(
                "SELECT"
                " json_extract(plan_json,"
                "'$.compiled_plan.production_plan_sha256'),"
                " json_extract(plan_json,'$.compiled_plan.plan.center_bucket'),"
                " json_extract(plan_json,'$.compiled_plan.plan.direction'),"
                " json_extract(plan_json,"
                "'$.compiled_plan.plan.skew_reading.state'),"
                " json_extract(plan_json,"
                "'$.compiled_plan.plan.skew_reading.gap_buckets'),"
                " json_extract(plan_json,"
                "'$.compiled_plan.plan.skew_reading.threshold_buckets'),"
                " json_extract(plan_json,"
                "'$.compiled_plan.plan.skew_reading.threshold_sigma'),"
                " json_extract(plan_json,"
                "'$.compiled_plan.plan.skew_reading.sigma_buckets'),"
                " json_extract(plan_json,'$.compiled_plan.plan"
                ".basket_support_selection.seat_shape.state'),"
                " json_extract(plan_json,'$.compiled_plan.plan"
                ".basket_support_selection.seat_shape_rule'),"
                " json_extract(plan_json,'$.compiled_plan.plan"
                ".basket_support_selection.seat_shape.allowed_ordinals')"
                " FROM basket_p0_preselection_plan"
                " WHERE station_id=? AND event_date=?",
                (station, day),
            ).fetchall()
        except sqlite3.Error:
            continue
        for (plan_sha, center, direction, skew, gap, threshold,
             threshold_sigma, sigma, shape_state, shape_rule, shape_allowed) in plans:
            targets = by_plan.get(str(plan_sha or ""), ())
            if not targets:
                continue
            recorded = str(skew or "")
            reading = {
                "center": str(center or ""),
                # SYMMETRIC / RIGHT_SKEWED / LEFT_SKEWED, or the direction
                # the plan acted on when the shape was not written down.
                "skew": recorded or _SKEW_BY_DIRECTION.get(
                    str(direction or "").upper(), ""
                ),
                "skew_source": "reading" if recorded else (
                    "direction" if direction else ""
                ),
                "gap_buckets": gap,
                # The gate the sign was judged by, in bucket steps.  Since
                # 2026-09-11 it is 0.35 of the served sigma, so it differs
                # day to day; ``threshold_sigma`` says so and is None on
                # plans written under the old half-bucket gate.
                "threshold_buckets": threshold,
                "threshold_sigma": threshold_sigma,
                # Charter 122: the served width the shape was read at, in
                # the same bucket steps.  Written from 2026-09-08; older
                # plans carry no width and the chip says nothing about it.
                "sigma_buckets": sigma,
                # Charter 131 (2026-09-12): the shape the seat was confined
                # to and the windows it allowed, as the plan recorded them.
                # None on plans written under charters 79/122.
                "seat_shape_state": shape_state,
                "seat_shape_rule": shape_rule,
                "seat_shape_allowed": shape_allowed,
            }
            for decision_id in targets:
                state[decision_id] = reading
    return state


def _collect_live_orders(conn: sqlite3.Connection) -> dict[str, Any]:
    """The order book: how many, worth how much, and what came back.

    Two books, never mixed.  ``live_submit_authorized=1`` is the only
    thing that makes a row live -- the column exists on rows minted after
    the go-live migration widened the CHECK, and the 1406 rows that
    predate it are all 0.  The top-level counts are the live book, so the
    card's headline cannot quietly count paper intents; ``book`` is the
    whole intent table with the split named on every row, because an
    intent lane that is minting every cycle and never leaving the gateway
    is the state this system is actually in and it was invisible here.

    ROI and PnL come off the reconciliation table -- what the exchange
    said it filled -- and never off the paper ledger.  The settled bucket
    label comes from ``_live_settled_buckets``: the settler's own row where
    the paper book seated the day, and otherwise the truth lane scored
    through the settler's own two functions.  Nothing about the per-station
    grid is re-implemented here (°C and 2°F stations do not share one),
    because an audit that restates a rule invents its own findings -- but
    reading the paper table *alone* was its own error, and a quieter one:
    it made a live basket's verdict depend on whether a different book
    happened to seat the same station-day.  Money is entirely the live fill
    columns: cost is what was paid, payout is one unit per share on the
    bucket truth landed in.
    """

    out: dict[str, Any] = {
        "intent_count": 0, "submitted_count": 0, "accepted_count": 0,
        "notional_usdc": 0.0, "matched_notional_usdc": 0.0, "rows": [],
        "book": {}, "exchange": {}, "pnl": {}, "plan_state": {},
        "market_slugs": {},
    }
    if not _table_exists(conn, "basket_target_order_intents"):
        return out
    # Newest two hundred, PLUS every basket that actually bought something.
    # The cap alone was hiding exactly the days worth looking at: on
    # 2026-09-11 the newest two hundred were all disarmed intents dated
    # 09-10 to 09-13, while every filled leg on the book -- 107 of them,
    # 09-06 through 09-11 -- fell outside the window.  A listing that
    # cannot contain a fill cannot show whether a basket won, which is
    # what the reader opens it for.
    try:
        rows = conn.execute(
            "SELECT i.order_intent_id, i.decision_id, i.bucket, i.side,"
            " i.limit_price, i.requested_size, i.requested_notional_usdc,"
            " i.state, i.created_at, d.station_id, d.event_date,"
            " i.target_execution_hard_deadline_at"
            " FROM basket_target_order_intents i"
            " LEFT JOIN auto_trade_decisions d ON d.decision_id=i.decision_id"
            " WHERE i.live_submit_authorized=1 AND ("
            "   i.order_intent_id IN ("
            "     SELECT order_intent_id FROM basket_target_order_intents"
            "     WHERE live_submit_authorized=1"
            "     ORDER BY created_at DESC LIMIT 200)"
            "   OR i.decision_id IN ("
            "     SELECT decision_id FROM basket_target_order_intents"
            "     WHERE live_submit_authorized=1 AND state='filled')"
            " )"
            " ORDER BY i.created_at DESC"
        ).fetchall()
    except sqlite3.Error:
        return out
    # The count of intents, not the count of rows this listing kept.  It
    # used to be ``len(rows)``, so the tile read "200 笔" for as long as
    # there were at least two hundred -- a number that describes the LIMIT
    # above and nothing about the book.
    # Count AND notional off the whole book, for the same reason: a total
    # summed over the listed rows describes the window, and printing it
    # beside a whole-book count makes the tile two different populations.
    try:
        total, notional = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(requested_notional_usdc),0)"
            " FROM basket_target_order_intents WHERE live_submit_authorized=1"
        ).fetchone()
        out["intent_count"] = int(total)
        out["notional_usdc"] = float(notional or 0.0)
    except sqlite3.Error:
        out["intent_count"] = len(rows)
    out["listed_count"] = len(rows)
    # Why a leg ended the way it did, from its latest event: the venue's
    # refusal text, the gateway's, or the submission gate's (held, barred,
    # disarmed).  A state alone -- failed, cancelled -- was the whole story
    # the board told on 2026-09-05, and it was not enough to act on.
    latest_event: dict[str, tuple[str, str]] = {}
    if _table_exists(conn, "basket_target_order_events"):
        try:
            for intent_id, event_type, event_json in conn.execute(
                "SELECT e.order_intent_id, e.event_type, e.event_json"
                " FROM basket_target_order_events e"
                " JOIN basket_target_order_intents i"
                "   ON i.order_intent_id=e.order_intent_id"
                " WHERE i.live_submit_authorized=1"
                " ORDER BY e.observed_at ASC, e.event_id ASC"
            ):
                try:
                    reason = str(
                        (json.loads(str(event_json or "{}")) or {}).get("reason")
                        or ""
                    )
                except (TypeError, ValueError):
                    reason = ""
                latest_event[str(intent_id)] = (str(event_type), reason)
        except sqlite3.Error:
            latest_event = {}
    # Which station-days have settled, and on which bucket -- resolved the
    # same way the PnL below resolves it, so a date the board collapses is
    # a date whose money is already counted.  Both halves of this panel
    # call one resolver for exactly that reason: they used to share a query
    # instead, and a source that moved under one of them would have left
    # the other reading the old table and disagreeing about which baskets
    # had a verdict.
    settled_bucket = _live_settled_buckets(conn, {
        (str(r[9] or ""), str(r[10] or "")) for r in rows
    })
    for row in rows:
        station = str(row[9] or "")
        day = str(row[10] or "")
        event_type, reason = latest_event.get(str(row[0]), ("", ""))
        out["rows"].append({
            "order_intent_id": str(row[0]), "decision_id": str(row[1]),
            "bucket": str(row[2]), "side": str(row[3]),
            "limit_price": row[4], "requested_size": row[5],
            "requested_notional_usdc": row[6], "state": str(row[7]),
            "created_at": str(row[8]),
            # A leg is not a basket.  The decision is what the solver sized
            # as one joint action, so the panel has to be able to put the
            # legs of one basket back together -- read as five unrelated
            # single-bucket orders, this table says the system buys buckets
            # one at a time, which is the opposite of what it does.
            "station_id": station,
            "event_date": day,
            "execution_deadline_at": str(row[11] or ""),
            "execution_window_seconds": _window_seconds(row[8], row[11]),
            "event_type": event_type,
            "reason": reason,
            "settled_bucket": settled_bucket.get((station, day), ("", ""))[0],
            # Which book said so.  ``paper_ledger`` is the settler's own
            # row; ``truth_lane`` is a day the paper book never seated,
            # scored off the truth that stands and the day's own ladder.
            "settled_bucket_source": (
                settled_bucket.get((station, day), ("", ""))[1]
            ),
        })
    # What each basket's own plan recorded -- the centre rung and the day's
    # shape.  Keyed by decision, because the decision is the basket: a
    # station-day carries several and they share neither.
    out["plan_state"] = _live_basket_plan_state(conn, out["rows"])
    out["market_slugs"] = _live_market_slugs(conn, out["rows"])
    if _table_exists(conn, "basket_target_order_events"):
        try:
            for kind, count, matched in conn.execute(
                "SELECT e.event_type, COUNT(*),"
                " COALESCE(SUM(e.matched_notional_usdc),0)"
                " FROM basket_target_order_events e"
                " JOIN basket_target_order_intents i"
                "   ON i.order_intent_id=e.order_intent_id"
                " WHERE i.live_submit_authorized=1 GROUP BY e.event_type"
            ):
                kind_l = str(kind).lower()
                if "accept" in kind_l or "submit" in kind_l:
                    out["submitted_count"] += int(count)
                if "accept" in kind_l:
                    out["accepted_count"] += int(count)
                out["matched_notional_usdc"] += float(matched or 0.0)
        except sqlite3.Error:
            pass
    out["notional_usdc"] = round(out["notional_usdc"], 4)
    out["matched_notional_usdc"] = round(out["matched_notional_usdc"], 4)
    out["book"] = _collect_order_book(conn)
    out["exchange"] = _collect_order_exchange(conn)
    out["pnl"] = _collect_live_pnl(conn)
    return out


def _collect_order_book(conn: sqlite3.Connection) -> dict[str, Any]:
    """The live intent book, cut the three ways a reader asks for.

    Live only (user ruling, 2026-08-31): every query here is filtered to
    ``live_submit_authorized=1``.  The paper intents are a different book
    and counting them beside live ones in the same table is how the two
    get added up by eye.  Their count is carried separately, named as
    excluded, for the one sentence that explains an empty panel -- a
    reader who sees zero live intents still needs to know whether the
    lane is minting anything at all.
    """

    book: dict[str, Any] = {}
    joined = (
        " FROM basket_target_order_intents i"
        " LEFT JOIN auto_trade_decisions d ON d.decision_id=i.decision_id"
        " WHERE i.live_submit_authorized=1"
    )
    try:
        book["paper_excluded"] = int(conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents"
            " WHERE live_submit_authorized=0"
        ).fetchone()[0])
    except sqlite3.Error:
        book["paper_excluded"] = 0
    try:
        row = conn.execute(
            "SELECT COUNT(*),"
            " COALESCE(SUM(i.requested_notional_usdc),0),"
            " AVG(i.requested_notional_usdc), AVG(i.limit_price),"
            " COUNT(DISTINCT d.station_id), COUNT(DISTINCT i.decision_id),"
            " COUNT(DISTINCT i.cycle_id),"
            " MIN(d.event_date), MAX(d.event_date), MAX(i.created_at)"
            + joined
        ).fetchone()
    except sqlite3.Error:
        return book
    if not row or not row[0]:
        book["total"] = 0
        return book
    book.update({
        "total": int(row[0]),
        "notional_usdc": round(float(row[1] or 0.0), 2),
        "mean_notional_usdc": round(float(row[2] or 0.0), 3),
        "mean_limit_price": round(float(row[3] or 0.0), 4),
        "stations": int(row[4] or 0), "decisions": int(row[5] or 0),
        "cycles": int(row[6] or 0),
        "first_event_date": str(row[7] or ""),
        "last_event_date": str(row[8] or ""),
        "last_minted_at": str(row[9] or ""),
    })
    try:
        book["by_state"] = [
            {"state": str(state), "count": int(count),
             "notional_usdc": round(float(notional or 0.0), 2)}
            for state, count, notional in conn.execute(
                "SELECT i.state, COUNT(*),"
                " COALESCE(SUM(i.requested_notional_usdc),0)"
                " FROM basket_target_order_intents i"
                " WHERE i.live_submit_authorized=1"
                " GROUP BY i.state ORDER BY COUNT(*) DESC"
            )
        ]
    except sqlite3.Error:
        book["by_state"] = []
    try:
        book["by_day"] = [
            {"event_date": str(day), "count": int(count),
             "stations": int(stations or 0),
             "notional_usdc": round(float(notional or 0.0), 2)}
            for day, count, stations, notional in conn.execute(
                "SELECT d.event_date, COUNT(*),"
                " COUNT(DISTINCT d.station_id),"
                " COALESCE(SUM(i.requested_notional_usdc),0)"
                + joined +
                " AND d.event_date IS NOT NULL"
                " GROUP BY d.event_date ORDER BY d.event_date DESC LIMIT 14"
            )
        ]
    except sqlite3.Error:
        book["by_day"] = []
    try:
        book["by_station"] = [
            {"station_id": str(station), "count": int(count),
             "days": int(days or 0),
             "mean_limit_price": round(float(price or 0.0), 4),
             "notional_usdc": round(float(notional or 0.0), 2)}
            for station, count, days, price, notional in conn.execute(
                "SELECT d.station_id, COUNT(*),"
                " COUNT(DISTINCT d.event_date), AVG(i.limit_price),"
                " COALESCE(SUM(i.requested_notional_usdc),0)"
                + joined +
                " AND d.station_id IS NOT NULL"
                " GROUP BY d.station_id"
                " ORDER BY SUM(i.requested_notional_usdc) DESC LIMIT 12"
            )
        ]
    except sqlite3.Error:
        book["by_station"] = []
    return book


def _collect_order_exchange(conn: sqlite3.Connection) -> dict[str, Any]:
    """What came back from the gateway, on the live book only."""

    live_join = (
        " JOIN basket_target_order_intents i"
        "   ON i.order_intent_id=t.order_intent_id"
        " WHERE i.live_submit_authorized=1"
    )
    ex: dict[str, Any] = {
        "events": 0, "event_types": [], "reconciliations": 0,
        "transitions": 0, "filled_size": 0.0, "filled_notional_usdc": 0.0,
        "by_state": [], "blocking": [],
    }
    if _table_exists(conn, "basket_target_order_events"):
        try:
            ex["event_types"] = [
                {"event_type": str(kind), "count": int(count)}
                for kind, count in conn.execute(
                    "SELECT t.event_type, COUNT(*)"
                    " FROM basket_target_order_events t" + live_join +
                    " GROUP BY t.event_type ORDER BY COUNT(*) DESC"
                )
            ]
        except sqlite3.Error:
            ex["event_types"] = []
        ex["events"] = sum(item["count"] for item in ex["event_types"])
    if _table_exists(conn, "basket_target_order_reconciliations"):
        try:
            ex["by_state"] = [
                {"state": str(state), "count": int(count),
                 "filled_size": round(float(size or 0.0), 4),
                 "filled_notional_usdc": round(float(notional or 0.0), 4)}
                for state, count, size, notional in conn.execute(
                    "SELECT t.state, COUNT(*),"
                    " COALESCE(SUM(t.filled_size),0),"
                    " COALESCE(SUM(t.filled_notional_usdc),0)"
                    " FROM basket_target_order_reconciliations t" + live_join +
                    " GROUP BY t.state ORDER BY COUNT(*) DESC"
                )
            ]
            ex["blocking"] = [
                {"reason": str(reason), "count": int(count)}
                for reason, count in conn.execute(
                    # Only reconciliations that are still open.  The
                    # blocking_reason column keeps its last value after the
                    # question is settled, so counting every non-null one
                    # reported a resolved verdict as an outstanding blocker:
                    # order_never_reached_exchange is the answer, not the
                    # obstacle, and the board would have shown it as
                    # "对账阻断 x1" indefinitely over an order proven never
                    # to have been placed.
                    "SELECT t.blocking_reason, COUNT(*)"
                    " FROM basket_target_order_reconciliations t" + live_join +
                    "   AND t.blocking_reason IS NOT NULL"
                    "   AND t.state NOT IN ("
                    "     'resolved_filled','resolved_cancelled',"
                    "     'resolved_partial','resolved_not_placed')"
                    " GROUP BY t.blocking_reason ORDER BY COUNT(*) DESC LIMIT 8"
                )
            ]
        except sqlite3.Error:
            ex["by_state"] = []
        ex["reconciliations"] = sum(item["count"] for item in ex["by_state"])
        ex["filled_size"] = round(
            sum(item["filled_size"] for item in ex["by_state"]), 4)
        ex["filled_notional_usdc"] = round(
            sum(item["filled_notional_usdc"] for item in ex["by_state"]), 4)
    if _table_exists(conn, "basket_target_execution_transition_versions"):
        try:
            ex["transitions"] = int(conn.execute(
                "SELECT COUNT(*)"
                " FROM basket_target_execution_transition_versions t"
                + live_join
            ).fetchone()[0])
        except sqlite3.Error:
            pass
    return ex


def _collect_live_pnl(conn: sqlite3.Connection) -> dict[str, Any]:
    """ROI and PnL of the live book, off live fills and settled truth.

    One row per filled live intent: cost is the notional the exchange
    reported filling, payout is one USDC per share when truth landed in
    that intent's bucket and zero otherwise.  A filled leg whose
    station-day has no settled bucket yet is counted as pending, never
    scored as a loss -- an unsettled day that reads as a zero payout is
    how a live book would look worse than it is every afternoon.

    "Has no settled bucket yet" is ``_live_settled_buckets``, the same
    resolver the basket listing reads.  Sharing it is the point: while this
    function read the paper settlement table by itself, every live fill on
    a station-day the paper book never seated stayed pending forever, so
    the ROI tile was computed over a subset chosen by a different book's
    seating rule and called itself the live book's ROI.
    """

    pnl: dict[str, Any] = {
        "available": False, "reason": "还没有实盘成交",
        "filled_legs": 0, "won_legs": 0, "pending_legs": 0,
        "cost_usdc": 0.0, "payout_usdc": 0.0, "pnl_usdc": 0.0,
        "roi": None, "station_days": 0, "by_station": [],
    }
    needed = ("basket_target_order_reconciliations",
              "basket_target_order_intents", "auto_trade_decisions")
    if not all(_table_exists(conn, name) for name in needed):
        pnl["reason"] = "执行账本表还没建"
        return pnl
    # The live book's own loader, shared with the money board, the daily
    # table, the roster, the go-live gates and the automatic bar, so every
    # one of them counts the same legs.  Its settled buckets come from the
    # same resolver as the basket listing above: a filled leg counted
    # pending here while its basket upstairs shows a verdict is two numbers
    # for one fact, and the reader has no way to tell which one to believe.
    book = live_book.build(conn)
    fills = [
        (leg["station_id"], leg["event_date"], leg["bucket"],
         leg["filled_size"], leg["filled_notional_usdc"])
        for leg in book["legs"] if leg["filled_size"] > 0
    ]
    if not fills:
        pnl["reason"] = "还没有一笔实盘成交"
        return pnl
    settled = book["settled"]
    per_station: dict[str, dict[str, float]] = {}
    days: set[tuple[str, str]] = set()
    for station, day, bucket, size, notional in fills:
        key = (str(station), str(day))
        landed = settled.get(key)
        truth = landed[0] if landed else None
        cost = float(notional or 0.0)
        acc = per_station.setdefault(str(station), {
            "legs": 0.0, "won": 0.0, "pending": 0.0,
            "cost": 0.0, "payout": 0.0, "by_day": {},
        })
        acc["legs"] += 1
        if truth is None:
            acc["pending"] += 1
            pnl["pending_legs"] += 1
            continue
        days.add(key)
        payout = float(size or 0.0) if str(bucket) == truth else 0.0
        acc["cost"] += cost
        acc["payout"] += payout
        acc["won"] += 1 if payout > 0 else 0
        day_acc = acc["by_day"].setdefault(str(day), [0.0, 0.0])
        day_acc[0] += cost
        day_acc[1] += payout
        pnl["filled_legs"] += 1
        pnl["won_legs"] += 1 if payout > 0 else 0
        pnl["cost_usdc"] += cost
        pnl["payout_usdc"] += payout
    pnl["station_days"] = len(days)
    pnl["pnl_usdc"] = round(pnl["payout_usdc"] - pnl["cost_usdc"], 4)
    pnl["cost_usdc"] = round(pnl["cost_usdc"], 4)
    pnl["payout_usdc"] = round(pnl["payout_usdc"], 4)
    pnl["roi"] = (
        round(pnl["pnl_usdc"] / pnl["cost_usdc"], 6)
        if pnl["cost_usdc"] > 0 else None
    )
    # The trailing window the go-live table reads beside the paper book's:
    # the last five event dates this book settled, taken across the whole
    # live book so every station is cut at the same dates -- the same shape
    # as the paper column next to it, counted off live fills only.
    recent_dates = sorted({day for _station, day in days})[-5:]
    recent_floor = recent_dates[0] if recent_dates else ""
    pnl["recent_window_dates"] = recent_dates

    def _station_row(station: str, acc: dict[str, Any]) -> dict[str, Any]:
        recent = [v for day, v in acc["by_day"].items() if day >= recent_floor]
        recent_cost = sum(v[0] for v in recent)
        recent_payout = sum(v[1] for v in recent)
        return {
            "station_id": station, "legs": int(acc["legs"]),
            "won": int(acc["won"]), "pending": int(acc["pending"]),
            "settled_legs": int(acc["legs"] - acc["pending"]),
            "settled_days": len(acc["by_day"]),
            "cost_usdc": round(acc["cost"], 4),
            "pnl_usdc": round(acc["payout"] - acc["cost"], 4),
            "roi": (round((acc["payout"] - acc["cost"]) / acc["cost"], 6)
                    if acc["cost"] > 0 else None),
            "recent_days": len(recent),
            "recent_pnl_usdc": round(recent_payout - recent_cost, 4),
            "recent_roi": (
                round((recent_payout - recent_cost) / recent_cost, 6)
                if recent_cost > 0 else None
            ),
        }

    pnl["by_station"] = sorted((
        _station_row(station, acc) for station, acc in per_station.items()
    ), key=lambda item: item["cost_usdc"], reverse=True)
    pnl["available"] = pnl["filled_legs"] > 0
    if not pnl["available"]:
        pnl["reason"] = "成交腿都还没结算"
    return pnl


def _live_scope() -> dict[str, Any]:
    """The live membership, from the one place that owns it.

    Imported lazily: this module is also loaded by tooling that has no
    business starting anything, and the control server module is where the
    rule lives rather than a service that has to be running.
    """

    import sys as _sys  # noqa: PLC0415
    _scripts = str(ROOT / "scripts")
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
    from live_control_server import live_stations  # noqa: PLC0415

    return live_stations()


def collect_live_readiness(
    conn: sqlite3.Connection,
    band_stations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Everything the go-live page needs: gates per station, knobs, contracts.

    Five gates, all measured, none aesthetic, and since 2026-09-16 every one
    that reads a book reads the live one (user ruling: 过闸、未过项、不行、
    不行的维度 all on live data): the hit-to-claim ratio of the station's
    settled live baskets (claim honesty), their held centre against settled
    truth, the live settled station-days and ROI, and truth freshness.  The
    five-dimension read rides on the same rows (``failure``), off the same
    settlements.  No paper figure is on the row: the columns that sat beside
    the live ones for comparison left the page with the rest of the paper
    readings (user ruling, 2026-09-16).  ``band_stations`` only widens the
    roster to stations
    the model serves.  The three control files are read fresh at render so
    the page shows what production would read this cycle; the write path is
    scripts/live_control_server.py, never this page.
    """

    ratio_by_station = {
        str(row["station"]): row for row in (band_stations or [])
    }
    try:
        truth_latest = {
            str(sid): str(day) for sid, day in conn.execute(
                "SELECT station_id, MAX(local_date)"
                " FROM calibration_station_daily_high_truth"
                " GROUP BY station_id"
            )
        }
    except sqlite3.OperationalError:
        return {"available": False}

    import datetime as _dt
    fresh_floor = (
        _dt.date.today() - _dt.timedelta(days=2)
    ).isoformat()
    # Two per-station difficulty readings (2026-09-01 investigation).
    # ``climate_jump``: mean |Tmax(d) - Tmax(d-1)| from the truth table --
    # the station's weather temperament, slow-moving and exogenous to the
    # book.  It passed every hygiene check the investigation ran (feature
    # from pre-08-27 truth only, neutral in the early window p=0.56,
    # predictive in the late one p=0.023, stable across cut points), which
    # none of the ledger-derived screens did.  ``shape_gap``: the model's
    # own mean-to-mode split, pool scope over the last 14 days -- the
    # downstream shadow of the same difficulty (r=+0.32), kept beside it as
    # corroboration.  Both are instruments for the operator's manual bar
    # rulings, not gates: the hand-picked list is the only station screen
    # that has ever beaten random on this book.
    climate_jump: dict[str, float] = {}
    try:
        truth_rows = collections.defaultdict(dict)
        for sid, day, tmax in conn.execute(
            "SELECT station_id, local_date, official_tmax_c"
            " FROM calibration_station_daily_high_truth"
        ):
            truth_rows[str(sid)][str(day)] = float(tmax)
        for sid, days in truth_rows.items():
            ordered = sorted(days)
            jumps = [
                abs(days[ordered[i]] - days[ordered[i - 1]])
                for i in range(1, len(ordered))
            ]
            if len(jumps) >= 8:
                climate_jump[sid] = sum(jumps) / len(jumps)
    except sqlite3.OperationalError:
        pass
    shape_gap: dict[str, float] = {}
    try:
        gap_floor = (
            _dt.date.today() - _dt.timedelta(days=14)
        ).isoformat()
        # The last P0 of each station-day, chosen in SQL.
        #
        # Third of this shape in this function -- the timezone map and the
        # first-claim lookup were the other two -- and the only one that had
        # no index to fall back on at all.  Measured 2026-09-09 against the
        # live store: ``SCAN basket_probability_envelope``, 44,392 rows read
        # off a 38 GB file to return 720 station-days, 134.8s, with every
        # row's 8.6 KB lineage_json turned over to reach four small columns.
        # That was half of this section's 272s, and this section is 40% of
        # the render.
        #
        # Two things make it seekable.  ``state='COMMITTED'`` is the leading
        # column of basket_probability_envelope_publisher_scope_idx, and it
        # is the filter every other reader of this table already applies --
        # an audit column counting uncommitted envelopes was the defect, not
        # the feature.  And "latest" moves from created_at, which no index
        # carries, to decision_at_utc, which is the instant the stage
        # selector and the P0-M lane already mean by it.
        #
        # Verified equivalent before the switch, on the live store: 720
        # station-days both ways, all 720 rowids identical, none only-old,
        # none only-new.  2.36s, covering index, no table visits.
        latest = {
            (str(sid), str(day)): int(rowid)
            for sid, day, _decided, rowid in conn.execute(
                "SELECT station_id, event_date, MAX(decision_at_utc), rowid"
                " FROM basket_probability_envelope"
                " WHERE state='COMMITTED' AND event_date >= ? AND stage='P0'"
                " GROUP BY station_id, event_date",
                (gap_floor,),
            )
        }
        import re as _re
        gaps: dict[str, list[float]] = collections.defaultdict(list)
        rowids = list(latest.values())
        rid_station = {rowid: key[0] for key, rowid in latest.items()}
        for i in range(0, len(rowids), 500):
            part = rowids[i:i + 500]
            for rowid, pj in conn.execute(
                "SELECT rowid, probabilities_json"
                " FROM basket_probability_envelope WHERE rowid IN (%s)"
                % ",".join(map(str, part))
            ):
                try:
                    raw = {
                        b: float(v) for b, v in json.loads(pj or "{}").items()
                        if float(v) > 0
                    }
                except (TypeError, ValueError):
                    continue
                if len(raw) < 3:
                    continue
                ords = {}
                for b in raw:
                    m = _re.match(r"^\s*(-?\d+)", str(b))
                    if not m:
                        ords = None
                        break
                    ords[b] = int(m.group(1))
                if not ords:
                    continue
                total = sum(raw.values())
                mean_o = sum(ords[b] * v / total for b, v in raw.items())
                mode_o = ords[max(raw, key=lambda b: raw[b])]
                gaps[rid_station[rowid]].append(abs(mean_o - mode_o))
        for sid, values in gaps.items():
            if len(values) >= 3:
                shape_gap[sid] = sum(values) / len(values)
    except sqlite3.OperationalError:
        pass
    # The live book, per station.  The gates' money -- settled station-days
    # and ROI -- and the add / release candidates read it (user rulings,
    # 2026-09-16: the paper ledger is a rule comparator, and a reader
    # deciding whether a station may trade real money asked for what that
    # station's real money did).  Read once here and handed to
    # the return below, so the row and the money card are the same
    # collector's output.  A station with live fills is listed even when the
    # paper book never seated it: LTAC and NZWN had traded live and had no
    # row on this table at all.
    live_orders = _collect_live_orders(conn)
    live_by_station = {
        str(item["station_id"]): item
        for item in ((live_orders.get("pnl") or {}).get("by_station") or [])
    }
    # Claim honesty, centre bias and the five dimensions, off the live
    # settlements (user ruling, 2026-09-16).  The claim gate and the 声称
    # dimension are one number read against two thresholds, and so are the
    # centre gate and 中心: one book, so no longer two measurements.
    book = live_book.build(conn)
    quality = live_book.station_quality(
        book["settlements"], live_book.station_days(book)
    )
    quality_by_station = quality["stations"]
    stations = []
    for sid in sorted(set(ratio_by_station) | set(live_by_station)):
        live = live_by_station.get(sid)
        fx = quality_by_station.get(sid)
        ratio = fx["calibration_ratio"] if fx else None
        chain_count = int(live["settled_days"]) if live else 0
        roi = live["roi"] if live else None
        bias_c = fx["centre_residual_c"] if fx else None
        gates = {
            "claim_honest": (
                ratio is not None and 0.88 <= ratio <= 1.18
            ),
            "centre_bias": bias_c is not None and abs(bias_c) <= 0.8,
            "chains": chain_count >= 5,
            "roi": roi is not None and roi > -0.08,
            "truth_fresh": truth_latest.get(sid, "") >= fresh_floor,
        }
        stations.append({
            "climate_jump_c": round(climate_jump[sid], 2)
            if sid in climate_jump else None,
            "shape_gap_buckets": round(shape_gap[sid], 2)
            if sid in shape_gap else None,
            "station": sid,
            "station_label": STATION_LABELS.get(sid, ""),
            "score": sum(gates.values()),
            "gates": gates,
            "ratio": ratio,
            "bias_c": bias_c,
            # The live figures the gates and candidates read.
            "chains": chain_count,
            "roi": roi,
            "recent_roi": live["recent_roi"] if live else None,
            "recent_days": int(live["recent_days"]) if live else 0,
            # The five dimensions, same settlements.
            "failure": fx,
            "truth_latest": truth_latest.get(sid),
            "live": live,
        })
    stations.sort(key=lambda row: (-row["score"], -(row["roi"] or -9.0)))

    def _config(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    policy = _config(ROOT / "config/trading/portfolio_capital_policy_v1.json")
    # Charter 141: the three capital dials, as the resident will read them.
    capital_layer = _config(ROOT / "config/trading/capital_layer_v1.json")
    # The runtime contract the mode selects, which is the rule production
    # itself follows (weather_basket_yes_daemon.py, and the budget read in
    # production_worker.py).  This panel read the no-live file
    # unconditionally, so after the go-live migration minted the live twin
    # it reported live_submit_authorized=false about a system whose
    # contract said true -- the same mistake production_worker.py already
    # carries a comment about, and it went unnoticed there for the same
    # reason: the two files carry the same budget.  Naming the file that
    # was read is the part that makes it impossible to repeat.
    live_mode = _live_execution_mode() == "production_live"
    runtime_path = ROOT / "config/bayes_basket_target" / (
        "live_runtime_v1.json" if live_mode else "no_live_runtime_v1.json"
    )
    runtime = _config(runtime_path)
    intent = _config(ROOT / "config/trading/live_intent_v1.json")
    exclusion_doc = _config(
        ROOT / "config/trading/live_station_exclusions_v1.json"
    )
    exclusions_set = barred_station_set()
    exclusions = sorted(exclusions_set)
    auto_barred = sorted({
        str(row.get("station") or "").strip().upper()
        for row in (exclusion_doc.get("auto_excluded_stations") or [])
        if isinstance(row, dict) and row.get("station")
    })
    # Candidates, recomputed every render so the list tracks the data -- and
    # candidates only.  Fourteen automatic rules were walk-forward tested on
    # 2026-08-30 and the best was p=0.346 against excluding stations at
    # random, so nothing here may move a station by itself; the criteria earn
    # their keep by putting one in front of a person with its numbers.  The
    # money on each side of the bar moved to the money board with the live
    # book (``collect_live_money``), 2026-09-16.
    # The batch that would actually trade, derived the way the control server
    # derives it: in all_except_barred mode the stored list is empty and
    # reading it would report a live open of nothing.
    # Ask the control plane rather than re-deriving it.  This page had its own
    # copy of the membership rule for one afternoon and the two drifted the
    # moment a waiver appeared: the server had LIMC live, the board drew it as
    # probation, and a reader comparing them had no way to tell which one was
    # the system.  One rule, one owner, read here.
    scope = _live_scope()
    scope_mode = scope["scope"]
    minimum = int(scope.get("minimum_settlements") or 0)
    allowlist_set = set(scope["stations"])
    probation_set = set(scope["probation"])
    waived_set = set(scope.get("probation_waivers") or [])
    held_set = set(scope.get("probation_holds") or [])
    # Which of the two bar lists a station is on.  The switch behaves
    # differently on them -- releasing an automatic bar has to record an
    # operator ruling too, or the next sweep undoes the click -- so the row
    # has to be able to say which one it is looking at.
    operator_barred = sorted({
        str(row.get("station") or "").strip().upper()
        for row in (exclusion_doc.get("excluded_stations") or [])
        if isinstance(row, dict) and row.get("station")
    })
    for row in stations:
        row["excluded"] = row["station"] in exclusions
        barred = row["excluded"]
        roi_now, recent = row["roi"], row["recent_roi"]
        row["add_candidate"] = bool(
            not barred and roi_now is not None
            and roi_now <= -0.25 and row["chains"] >= 5
        )
        # Releasing is not the mirror of barring, and the criterion must not
        # be either.  Barring a station costs a skipped market; releasing one
        # puts money back on it, so release asks for more.  EPWA 2026-08-30 is
        # the case that forced this: it cleared an ROI-only rule on a 2.2-point
        # technicality (-22.8% against a -25% line) with a five-day window
        # reading +60.1% -- and underneath, every one of its six forward seats
        # lost 100%, the three wins were all P1 near-certainties bought after
        # the day was observed at claims of 0.93-0.99, and the model runs
        # 1.62C cold with a claim ratio of 0.46.  A recovery has to show in the
        # station's structure, not only in the last few settlements.
        # The row's own gates.  This read ``gates`` and ``sid``, the last
        # values the row-building loop above left behind, so every barred
        # station was judged on the alphabetically last station's centre and
        # claim gates and its five-day count.  The money is live since
        # 2026-09-16: at least three settled live days in the book's last
        # five, net positive over them.
        row["release_candidate"] = bool(
            barred and recent is not None and recent > 0.0
            and roi_now is not None and roi_now > -0.25
            and row["gates"]["claim_honest"] and row["gates"]["centre_bias"]
            and row["recent_days"] >= 3
        )
    return {
        "add_candidates": [
            r["station"] for r in stations if r["add_candidate"]
        ],
        "release_candidates": [
            r["station"] for r in stations if r["release_candidate"]
        ],
        "available": True,
        "stations": stations,
        # The five-dimension read's frame; each row carries its own
        # ``failure``.  Live settlements, so the halves split at the live
        # book's median settled day.
        "failure": {
            "available": bool(quality_by_station),
            "split_event_date": quality["split_event_date"],
            "thresholds": quality["thresholds"],
        },
        "kelly": capital_layer.get("kelly_fraction", 0.25),
        "exposure": policy.get("portfolio_risk_fraction"),
        "station_sizing": capital_layer.get("station_day_sizing_fraction", 0.065),
        "station_ceiling": capital_layer.get("station_day_ceiling_fraction", 0.10),
        "budget_usdc": runtime.get("target_remaining_budget_usdc"),
        # Charter 146: the tiebreak lane's per-trade stake and the four
        # lane switches, as the resident will read them.
        "tiebreak_stake": (
            (_load_strategy_lanes()["lanes"].get("tiebreak") or {}).get("stake_usdc")
        ),
        "strategy_lanes": {
            name: bool(lane.get("enabled") is True)
            for name, lane in _load_strategy_lanes()["lanes"].items()
        },
        "live_enabled": intent.get("live_enabled"),
        # What the system is, not what it was when this panel was written.
        # The banner used to state "三重合同仍锁 no_live" as a fixed string,
        # and the arming prompt promised "它不下单" -- both true only until
        # the go-live migration ran, after which the panel was reassuring the
        # operator about a system that had stopped being that system.
        "execution_mode": _live_execution_mode(),
        "submission_paused": (ROOT / "data" / "live_paused.flag").is_file(),
        # The live book, distinguished at the row level.  An intent minted
        # with live_submit_authorized=1 is a live order by identity -- the
        # widened CHECK is what the go-live migration existed to change --
        # while every pre-migration row carries 0.  Counted fresh at render
        # so the panel and the ledger cannot say different things.
        "live_orders": live_orders,
        # The round-trip that proved the chain, kept as the record it is.
        # Before it, every claim about whether this machine could reach the
        # exchange was inference: the region gate read the website's geoblock
        # and the exchange itself had not been asked since 2026-08-07.
        "chain_probe": _read_chain_probe(),
        # Asked of the exchange, not of our own records.  An order this
        # machine placed and then lost track of -- which happened on
        # 2026-08-31, for eighteen minutes -- is invisible to every table we
        # own and visible here.
        "open_orders": _read_open_orders(),
        "station_scope": scope_mode,
        "minimum_settlements_to_qualify": minimum,
        "station_probation_waivers": sorted(waived_set),
        # The third switch: a qualified station the operator is holding back.
        # Reported apart from probation itself because the two look identical
        # on the board and are not the same fact -- one is arithmetic, the
        # other is somebody's decision this morning.
        "station_probation_holds": sorted(held_set),
        "station_allowlist": sorted(allowlist_set),
        "station_probation": sorted(probation_set),
        # The bar, not just the pass.  A station absent from the allowlist
        # might be merely unselected; one on this list is refused, and the
        # station table below marks the difference so the two do not read
        # the same.
        "station_exclusions": exclusions,
        "auto_barred": auto_barred,
        "operator_barred": operator_barred,
        "contracts": {
            "execution_mode": runtime.get("execution_mode"),
            "live_submit_authorized": runtime.get("live_submit_authorized"),
            "calls_order_gateway": runtime.get("calls_order_gateway"),
            "places_orders": runtime.get("places_orders"),
            "policy_scope": policy.get("authority_scope"),
            "runtime_config": runtime_path.name,
            "minted_at_utc": runtime.get("minted_at_utc"),
        },
    }


def collect_center_width(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The two scalars that move the served distribution, per stage.

    ``delta_c`` slides the centre, ``width_lambda`` stretches or squeezes it.
    They are fitted per stage and must stay per stage: P0 sees a model blend,
    P1 sees that blend after the day's own observations have narrowed it, and
    one pair of numbers cannot be right for both.
    """

    if not _table_exists(conn, "calibration_center_width_table"):
        return []
    served = {
        str(stage): rest
        for stage, *rest in conn.execute(
            "SELECT stage, delta_c, width_lambda, fitted_at_utc,"
            " serving_day_count, evidence_json"
            " FROM calibration_center_width_table WHERE activated=1"
        )
    }
    journal: dict[str, list[Any]] = {}
    if _table_exists(conn, "calibration_center_width_fit_journal"):
        for stage, *rest in conn.execute(
            "SELECT stage, fitted_at_utc, outcome, delta_c, width_lambda,"
            " serving_day_count, serving_cell_count, forward_nll_gain"
            " FROM calibration_center_width_fit_journal"
            " ORDER BY fitted_at_utc DESC"
        ):
            journal.setdefault(str(stage), list(rest))
    rows = []
    for stage in ("P0", "P0-M", "P1"):
        active = served.get(stage)
        last = journal.get(stage)
        evidence: dict[str, Any] = {}
        if active:
            try:
                evidence = json.loads(active[4])
            except (ValueError, TypeError):
                evidence = {}
        outcome = str(last[1]) if last else ""
        if active:
            state, health = "已生效", "green"
        elif outcome == "NOT_WIRED":
            state, health = "未接线", "amber"
        elif outcome == "ACCUMULATING":
            state, health = "攒证据中", "amber"
        elif outcome == "REFUSED_FORWARD":
            state, health = "试算后更差，不采用", "green"
        elif outcome == "INCUMBENT_VETOED":
            # Charter 138: the lane retired its own pair because undoing it
            # beat serving it.  No active row here is a verdict, not a hole,
            # and identity is what the stage is deliberately serving.
            state, health = "现役被否决退役 · 按恒等服务", "green"
        else:
            state, health = "无记录", "amber"
        rows.append({
            "stage": stage,
            "meaning": STAGE_MEANING.get(stage, ""),
            "state": state,
            "health": health,
            # No activated row is not "no data": the runtime serves the
            # identity pair in that case, which is a served decision and
            # the one P1 is living under.  Printing "—" for it read as a
            # hole in the page (user report, 2026-09-12: "p1无数据") and
            # hid the fact that P1's untouched width is currently the
            # right one -- its only fitted row, 0.825 from 2026-08-16,
            # would narrow a distribution that measures 9% too narrow
            # already.
            "delta_c": active[0] if active else 0.0,
            "width_lambda": active[1] if active else 1.0,
            "served_identity": active is None,
            # Served scalars come from the active row; the evidence behind
            # them comes from the newest journal entry, because the active
            # row is only rewritten when the scalars change and would
            # otherwise report a fit age of however long they have held.
            "fitted_at_utc": str(last[0]) if last else (
                active[2] if active else None
            ),
            "fit_age_hours": _age_hours(
                str(last[0]) if last else (active[2] if active else None)
            ),
            "serving_days": (
                int(last[4]) if last
                else (int(active[3]) if active else 0)
            ),
            "serving_cells": (
                int(last[5]) if last
                else evidence.get("serving_cell_count")
            ),
            "forward_nll_gain": (
                float(last[6]) if last else evidence.get("forward_nll_gain")
            ),
            "min_forward_nll_gain": evidence.get("min_forward_nll_gain"),
            "last_outcome": outcome,
            **_refit_countdown(
                int(last[4]) if last else (int(active[3]) if active else 0)
            ),
            **_evidence_clock(conn, stage),
            **_taf_cell_reading(conn, stage),
        })
    return rows


TAF_CELL_LABELS = {
    "same": "TAF 与中心同档",
    "adjacent": "差 1 档",
    "far": "差 2 档及以上",
    "none": "TAF 不带 TX",
}


def _taf_cell_reading(conn: sqlite3.Connection, stage: str) -> dict[str, Any]:
    """Where each TAF width cell stands, for the stage that serves them.

    The census comes from the newest fit journal row -- the activated table
    row is only rewritten when the served numbers change, and every cell
    still accumulating changes nothing -- and what serves comes from the
    activated row.  A census written under a retired cell rule is not
    shown as this rule's progress: the direction cells counted different
    days.
    """

    try:
        from bayes_basket_target.center_width_store import (
            MIN_QUALIFYING_CELLS,
            MIN_QUALIFYING_DAYS,
            TAF_CELL_NAMES,
            TAF_CELL_RULE,
            TAF_CELL_STAGES,
        )
    except ImportError:
        return {}
    if stage not in TAF_CELL_STAGES:
        return {}
    census: dict[str, Any] = {}
    fitted_at = None
    try:
        row = conn.execute(
            "SELECT evidence_json, fitted_at_utc"
            " FROM calibration_center_width_fit_journal"
            " WHERE stage=? ORDER BY fitted_at_utc DESC LIMIT 1",
            (stage,),
        ).fetchone()
        if row and row[0]:
            census = json.loads(row[0]).get("taf_cells") or {}
            fitted_at = row[1]
    except (sqlite3.OperationalError, ValueError, TypeError, AttributeError):
        census = {}
    served: dict[str, Any] = {}
    try:
        row = conn.execute(
            "SELECT taf_cells_json FROM calibration_center_width_table"
            " WHERE activated=1 AND stage=?",
            (stage,),
        ).fetchone()
        if row and row[0]:
            served = json.loads(row[0])
    except (sqlite3.OperationalError, ValueError, TypeError):
        served = {}
    cells = []
    for name in TAF_CELL_NAMES:
        entry = census.get(name) if isinstance(census, dict) else None
        entry = entry if isinstance(entry, dict) and entry.get("rule") == TAF_CELL_RULE else {}
        cell = served.get(name) if isinstance(served, dict) else None
        cells.append({
            "name": name,
            "label": TAF_CELL_LABELS.get(name, name),
            "day_count": entry.get("day_count"),
            "cell_count": entry.get("cell_count"),
            "state": entry.get("state"),
            "forward_nll_gain": entry.get("forward_nll_gain_vs_pooled"),
            "candidate_taf_delta_c": entry.get("candidate_taf_delta_c"),
            "candidate_width_lambda": entry.get("candidate_width_lambda"),
            # (anchor shift, shift along the TAF, width) when served
            "served": (
                [float(value) for value in cell]
                if isinstance(cell, list) and len(cell) == 3 else None
            ),
        })
    return {
        "taf_cells": {
            "rule": TAF_CELL_RULE,
            "measured": any(cell["state"] for cell in cells),
            "fitted_at_utc": fitted_at,
            "min_days": MIN_QUALIFYING_DAYS,
            "min_cells": MIN_QUALIFYING_CELLS,
            "cells": cells,
        }
    }


def _refit_countdown(serving_days: int) -> dict[str, Any]:
    """How many more days before this stage may refit, said out loud.

    The card carried the two numbers -- "证据 2 天 / 需 8 天" -- in the same
    muted line as the cell count, and a reader who wants to know when a
    stale scalar can be corrected had to subtract them (user ruling,
    2026-09-12).  P0 is the case that made it matter: it serves a width
    fitted 2026-09-03, three days before the climate blend came out from
    under it, and the only thing standing between that and a clean refit
    is the day count.

    The date is an estimate and says so: a day counts only if it carried
    enough serving cells, so one calendar day is an upper bound on
    progress, and the date can only slip -- it slips all the way back to
    the start whenever the served version moves, which is what the
    evidence clock below the countdown is for.
    """

    try:
        from bayes_basket_target.center_width_store import MIN_QUALIFYING_DAYS
    except ImportError:
        return {"days_remaining": None, "earliest_refit_date": None}
    remaining = max(0, int(MIN_QUALIFYING_DAYS) - int(serving_days))
    return {
        "days_remaining": remaining,
        "earliest_refit_date": (
            (date.today() + timedelta(days=remaining)).isoformat()
            if remaining else None
        ),
    }


def _evidence_clock(conn: sqlite3.Connection, stage: str) -> dict[str, Any]:
    """Why this stage is still accumulating: where its floor sits and who set it.

    The fit's evidence floor is the latest of three bounds, and the AST
    fingerprint is the one that moves without anyone deciding anything: it
    hashes the serving functions, so a commit that only records a receipt
    or opens an inert replay door resets the floor exactly as a genuine
    arithmetic change does.  Measured 2026-08-27, three P0 generations
    landed in four hours and P1 has never once held a fingerprint for the
    eight days the layer needs.  Without this on the page the layer just
    reads "攒证据中" forever with nothing saying what keeps restarting it.
    """

    out: dict[str, Any] = {
        "min_qualifying_days": None,
        "ast_fingerprint": None,
        "ast_first_seen_utc": None,
        "ast_generations": None,
        "ast_days_held": None,
        "ast_longest_quiet_days": None,
        "watchdog_discrepancy": None,
        "served_version": None,
    }
    try:
        from bayes_basket_target import serving_arithmetic
        from bayes_basket_target.center_width_store import MIN_QUALIFYING_DAYS
    except ImportError:
        return out
    out["min_qualifying_days"] = MIN_QUALIFYING_DAYS
    try:
        alarm = serving_arithmetic.watchdog(conn, stage=stage)
        out["watchdog_discrepancy"] = bool(alarm.get("discrepancy"))
        out["served_version"] = alarm.get("served_version")
    except sqlite3.OperationalError:
        pass
    try:
        current = serving_arithmetic.fingerprint(stage)
        stamps = [
            (str(sha), str(seen))
            for sha, seen in conn.execute(
                "SELECT fingerprint_sha256, first_seen_utc"
                " FROM calibration_serving_arithmetic"
                " WHERE stage=? ORDER BY first_seen_utc",
                (stage,),
            )
        ]
    except sqlite3.OperationalError:
        return out
    if not stamps:
        return out
    out["ast_generations"] = len(stamps)
    seen_by_sha = {sha: seen for sha, seen in stamps}
    first_seen = seen_by_sha.get(str(current or ""))
    if first_seen:
        out["ast_fingerprint"] = str(current)[:10]
        out["ast_first_seen_utc"] = first_seen
        held = _age_hours(first_seen)
        out["ast_days_held"] = None if held is None else held / 24.0
    moments = [seen for _sha, seen in stamps]
    gaps = [
        (_age_hours(a) or 0.0) - (_age_hours(b) or 0.0)
        for a, b in zip(moments, moments[1:])
    ]
    newest_age = _age_hours(moments[-1])
    if newest_age is not None:
        gaps.append(newest_age)
    if gaps:
        out["ast_longest_quiet_days"] = max(gaps) / 24.0
    return out


# Thresholds for the six-layer scorecard.  Every one of them is anchored to
# something the system already means, not to a number from a textbook:
#
# * temperature errors are counted in RUNGS, never in degrees.  The ladder is
#   not one width: a Fahrenheit book steps two degrees F per rung (1.11C) and
#   a Celsius book one degree C, and 35 of the 46 stations are Celsius.  A
#   fixed degree tolerance would hold the two books to different standards,
#   and a rung is also the thing being traded -- picking the bucket is the
#   decision, so "off by one rung" is the error that costs money.  Half a
#   rung of bias is where the centre starts landing on the wrong rung
#   systematically; a full rung is where it usually does.
# * probability scores are judged against the same climatology baseline the
#   OOS gate uses, because a raw CRPS is not comparable across stations.
# * spread is judged against realised error: a calibrated ensemble states an
#   uncertainty that matches the error it actually makes (spread-skill ~ 1).
#   Too narrow is overconfidence, too wide is an ensemble saying nothing.
# * ECE is in probability units, where 5% is the usual "well calibrated" bar.
# * bootstrap width is relative, because it answers only "do we have enough
#   days to trust the number above", not "is the number good".
SCORECARD_THRESHOLDS = {
    "bias_rungs": (0.3, 0.8),        # |bias| in market rungs, either book
    "mae_rungs": (1.0, 1.5),         # one rung / one and a half rungs
    "crps_skill": (0.10, 0.0),       # share better than climatology, higher better
    "brier_skill": (0.10, 0.0),      # ditto
    "pit_deviation": (0.04, 0.08),   # max |empirical - uniform| over PIT bins
    "ece": (0.05, 0.10),             # expected calibration error
    "conditional_gap": (0.05, 0.10),  # worst well-populated reliability bin
    "residual_correlation": (0.5, 0.75),   # max off-diagonal
    "weight_distance": (0.10, 0.25),       # half-L1 gap, served vs production re-solve
    "spread_skill": (0.20, 0.45),    # |ratio - 1|, ensemble spread vs realised error
    "ci_relative": (0.15, 0.30),     # bootstrap half-width / value
}


# Where the truth lands relative to the distribution the seat is built on,
# split by how wide that distribution called itself (2026-09-05, user asked
# for the sigma-tier landing table on the page).  The centre/width section
# above says what the layer *does*; this says whether what it serves is
# where the day actually went, which is the question the seat rule and the
# positive-EV gate both read through the mean+-1 window.
# The cuts ABOVE production's last width-tier boundary are the page's own:
# the serving path has one tier up there, and splitting it is a reading
# choice about where flat days land, not a claim about the axis.
CENTER_LANDING_DISPLAY_CUTS = (2.5, 3.0)
CENTER_LANDING_FLAT_RUNGS = 2.5
_CENTER_LANDING_WORDS = ("尖", "中", "宽", "平", "最平")


def _rungs(value: float) -> str:
    """1.44 and 2.0, not 1.44 and 2 -- these are read side by side."""

    return f"{value:.1f}" if float(value).is_integer() else f"{value:g}"


def _center_landing_tiers() -> tuple[tuple[float, float, str, str], ...]:
    """The sigma tiers this page slices on, read off the serving path.

    The lower cuts are NOT the page's to choose.  They are
    ``WIDTH_TIER_BOUNDARIES_RUNGS`` -- the boundaries the width fit and the
    serving transform share, whose first value is also ``SIGMA_FLOOR_RUNGS``
    (1.44 rungs: below it the served claim's standardised error has sd
    0.95, above it 0.67, measured 2026-09-05).  They were copied here as
    literals when this table was added, and a copied boundary is how a page
    ends up slicing on an axis production has moved off: the boundaries are
    a research switch (``PM_CENTER_WIDTH_TIER_BOUNDARIES``), so they can
    change without anyone opening this file.  Read them instead, and fall
    back to the measured pair only if the package cannot be imported --
    with the fallback stated as one number in one place.
    """

    try:                                             # noqa: PLC0415
        from bayes_basket_target.center_width_calibration import (
            WIDTH_TIER_BOUNDARIES_RUNGS as served,
        )
        bounds = tuple(sorted(float(value) for value in served))
    except Exception:                                # pragma: no cover
        bounds = (1.44, 2.0)
    cuts = list(bounds) + [
        cut for cut in CENTER_LANDING_DISPLAY_CUTS if cut > bounds[-1]
    ]
    edges = [0.0] + cuts + [99.0]
    tiers = []
    for index, (low, high) in enumerate(zip(edges, edges[1:])):
        word = (
            _CENTER_LANDING_WORDS[index]
            if index < len(_CENTER_LANDING_WORDS)
            else f"第{index + 1}档"
        )
        if index == 0:
            label = f"{word} <{_rungs(high)}"
        elif high >= 99.0:
            label = f"{word} ≥{_rungs(low)}"
        else:
            label = f"{word} {_rungs(low)}-{_rungs(high)}"
        key = ("narrow", "mid", "wide", "flat", "flattest")[index] \
            if index < 5 else f"tier{index}"
        tiers.append((low, high, key, label))
    return tuple(tiers)


CENTER_LANDING_TIERS = _center_landing_tiers()
CENTER_LANDING_STAGE_MEANING = {
    "P0": "开仓：模型融合出的第一版概率",
    "P0-M": "确认：早上的 TAF 对 P0 的修正",
    "P1": "盯盘：当天实测进来后的滚动更新",
}


def _center_landing_bootstrap(
    by_day: dict[str, list[float]], draws: int = 600, seed: int = 11
) -> tuple[float | None, float | None]:
    """A 95% interval for the mean, resampling whole days.

    The day is the unit because one weather day is shared across stations:
    resampling cells would call a fleet-wide warm day forty-five
    independent observations and shrink the interval to nothing.
    """

    days = list(by_day)
    if not days:
        return None, None
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]
        values = [value for day in pick for value in by_day[day]]
        if values:
            means.append(sum(values) / len(values))
    if not means:
        return None, None
    means.sort()
    return (
        means[int(0.025 * (len(means) - 1))],
        means[int(0.975 * (len(means) - 1))],
    )


def _center_landing_seat(
    *,
    stage: str,
    keys: Sequence[str],
    probabilities: Mapping[str, float],
    excluded: Sequence[str] = (),
) -> dict[str, Any] | None:
    """The seat window charter 131 rules for one served cell.

    Read off the runtime's own functions rather than re-derived here (a
    panel that copied the seat rule would drift the day the rule moved):
    the centre is the mass-weighted mean bucket snapped to the nearest live
    rung (charter 54/75), the shape is fixed by the three-state reading at
    0.35 sigma -- symmetric ``c+-1``; skewed ``{c, tail, tail+1}`` with the
    mode bucket outside by ruling -- and a P1 day left with one live rung
    seats that rung alone.  The widest ruled shape whose rungs are all live
    is the window reported; production seats the two-rung subset only when
    the wider one fails a gate, which a page without prices cannot see.

    ``None`` when the runtime would seat nothing: no live rung, or every
    ruled shape reaches off the ladder or onto a dead rung.  Until
    2026-09-12 this panel hard-coded the mean bucket +-1 (charter 49/79,
    both retired), which on a skewed day is not the seat at all.
    """

    try:                                             # noqa: PLC0415
        from bayes_basket_target.delta_v2_runtime import (
            LADDER_CENTER_DEFINITION,
            _alternate_ladder_center,
            _skew_extension_sign,
            seat_shape_windows_v1,
        )
    except ImportError:
        return None
    ordinals = {str(key): index for index, key in enumerate(keys)}
    dead = {str(name) for name in excluded}
    admitted = [
        str(key) for key in keys
        if float(probabilities.get(key, 0.0) or 0.0) > 0.0
        and str(key) not in dead
    ]
    if not admitted:
        return None
    # The runtime's own order: heaviest live rung as the seed, the ruled
    # definition (mean, since charter 54) applied over it.
    centre = max(
        admitted,
        key=lambda bucket: (
            float(probabilities.get(bucket, 0.0) or 0.0), -ordinals[bucket]
        ),
    )
    if LADDER_CENTER_DEFINITION != "heaviest":
        centre = _alternate_ladder_center(
            LADDER_CENTER_DEFINITION,
            admitted=admitted,
            probabilities=probabilities,
            ordinals=ordinals,
        ) or centre
    sign = _skew_extension_sign(probabilities, ordinals)
    live = {ordinals[bucket] for bucket in admitted}
    _shapes, audit = seat_shape_windows_v1(
        centre_ordinal=ordinals[centre],
        sign=sign,
        stage=str(stage),
        probability_by_ordinal={
            ordinals[key]: float(probabilities.get(key, 0.0) or 0.0)
            for key in keys
        },
        live_ordinals=live,
    )
    by_ordinal = {index: key for key, index in ordinals.items()}
    for shape in audit.get("allowed_ordinals") or []:
        if all(int(ordinal) in live for ordinal in shape):
            return {
                "state": str(audit.get("state") or ""),
                "sign": int(sign),
                "centre": centre,
                "seat_keys": [by_ordinal[int(o)] for o in shape],
            }
    return None


def _center_landing_group(
    members: Sequence[Mapping[str, Any]], **head: Any
) -> dict[str, Any]:
    """One row of the landing table: bias, interval, and the window accounts.

    The seat, mode and retired-window columns average over the cells that
    have that window (a cell the runtime would not seat has no seat window
    but still lands somewhere), so ``seat_cells`` travels with them.
    """

    errors = [float(row["error"]) for row in members]
    count = len(errors)
    mean_error = sum(errors) / count
    spread = (
        math.sqrt(
            sum((value - mean_error) ** 2 for value in errors) / (count - 1)
        )
        if count > 1 else None
    )
    by_event: dict[str, list[float]] = {}
    for row in members:
        by_event.setdefault(str(row["event_date"]), []).append(
            float(row["error"])
        )
    low, high = _center_landing_bootstrap(by_event)

    def _account(prefix: str) -> dict[str, Any]:
        have = [row for row in members if row.get(f"{prefix}_claim") is not None]
        if not have:
            return {
                f"{prefix}_cells": 0,
                f"{prefix}_claim": None,
                f"{prefix}_cover": None,
                f"{prefix}_ratio": None,
            }
        claim = sum(float(row[f"{prefix}_claim"]) for row in have) / len(have)
        cover = sum(int(row[f"{prefix}_hit"]) for row in have) / len(have)
        return {
            f"{prefix}_cells": len(have),
            f"{prefix}_claim": claim,
            f"{prefix}_cover": cover,
            f"{prefix}_ratio": cover / claim if claim > 0.0 else None,
        }

    seated = [row for row in members if row.get("seat_claim") is not None]
    out: dict[str, Any] = dict(head)
    out.update({
        "cells": count,
        "bias_rungs": mean_error,
        "ci_low": low,
        "ci_high": high,
        "median_rungs": sorted(errors)[count // 2],
        "sd_rungs": spread,
        "warm_share": sum(1 for value in errors if value > 0.0) / count,
        "skewed_share": sum(
            1 for row in members if int(row.get("sign") or 0) != 0
        ) / count,
        "same_window_share": (
            sum(1 for row in seated if row.get("same_window")) / len(seated)
            if seated else None
        ),
        "significant": bool(
            low is not None and high is not None
            and (low > 0.0 or high < 0.0)
        ),
    })
    out.update(_account("seat"))
    out.update(_account("mode"))
    out.update(_account("legacy"))
    return out


def collect_center_landing(conn: sqlite3.Connection) -> dict[str, Any]:
    """Truth minus the served mean, in rungs, by how wide the day claimed to be.

    Read off the centre/width layer's own serving cells (one decision per
    station-day-stage-hourblock, anchored at the served-arithmetic
    boundary) and the official truth, so the population is the one that
    layer fits on and nothing here re-derives a rule.  Distances are in
    rungs of each station's own ladder (charter 53(b)), which is what makes
    a Celsius and a Fahrenheit market comparable in one column.

    The seat-window columns read the window charter 131 would seat on that
    cell (``_center_landing_seat``), and every row is also split by the
    day's three-state reading, because on a skewed day the seat and the
    mean +-1 window are different sets of rungs.
    """

    try:
        from bayes_basket_target.basket_kernel.support import (
            SKEW_STATE_LEFT,
            SKEW_STATE_RIGHT,
            SKEW_STATE_SYMMETRIC,
        )
        from bayes_basket_target.center_width_calibration import (
            sigma_floor_lift_v1,
        )
        from bayes_basket_target.center_width_store import (
            _evidence_floor,
            _truth,
            serving_cells_by_day,
        )
    except ImportError:
        return {}
    if not _table_exists(conn, "basket_probability_bridge_history"):
        return {}
    truth = _truth(conn)
    state_labels = (
        (SKEW_STATE_SYMMETRIC, "symmetric", "对称"),
        (SKEW_STATE_LEFT, "left", "左偏（冷尾）"),
        (SKEW_STATE_RIGHT, "right", "右偏（暖尾）"),
    )
    stages_out: list[dict[str, Any]] = []
    for stage in ("P0", "P0-M", "P1"):
        try:
            floor = _evidence_floor(conn, stage=stage, caller_bound=None)
            by_day = serving_cells_by_day(
                conn, truth, stage=stage, min_decision_at_utc=floor,
            )
        except Exception:  # noqa: BLE001 -- a panel must not blank the page
            continue
        landings: list[dict[str, Any]] = []
        for day in sorted(by_day):
            for cell in by_day[day]:
                probabilities = cell["probabilities"]
                keys = cell["ordered_keys"]
                centers = cell["key_centers_c"]
                actual = truth.get((cell["station_id"], cell["event_date"]))
                if actual is None:
                    continue
                try:
                    _lift, sigma_c, rung_c = sigma_floor_lift_v1(
                        probabilities,
                        ordered_keys=keys,
                        key_centers_c=centers,
                        floor_rungs=0.0,
                    )
                except (ValueError, KeyError):
                    continue
                if rung_c <= 0.0:
                    continue
                total = sum(probabilities.values())
                if total <= 0.0:
                    continue
                mean_c = sum(
                    centers[index] * probabilities[key]
                    for index, key in enumerate(keys)
                ) / total
                peak = max(
                    range(len(keys)),
                    key=lambda index: (probabilities[keys[index]], -index),
                )
                # The retired window (charter 49/79): the bucket nearest the
                # mean point and one rung either side.  Kept as a comparison
                # column so a reader can see what the ruled shape changed.
                legacy_centre = min(
                    range(len(keys)),
                    key=lambda index: (abs(centers[index] - mean_c), index),
                )

                def _cover(names: Sequence[str]) -> tuple[float, int]:
                    return (
                        sum(probabilities[name] for name in names) / total,
                        1 if cell["settled_bucket"] in names else 0,
                    )

                mode_names = keys[max(0, peak - 1):peak + 2]
                legacy_names = keys[
                    max(0, legacy_centre - 1):legacy_centre + 2
                ]
                seat = _center_landing_seat(
                    stage=stage,
                    keys=keys,
                    probabilities=probabilities,
                    excluded=cell.get("excluded_buckets") or (),
                )
                entry: dict[str, Any] = {
                    "sigma": sigma_c / rung_c,
                    "error": (float(actual) - mean_c) / rung_c,
                    "state": seat["state"] if seat else None,
                    "sign": seat["sign"] if seat else 0,
                    "seat_claim": None,
                    "seat_hit": None,
                    "same_window": None,
                    "station": cell["station_id"],
                    "event_date": cell["event_date"],
                }
                if seat is not None:
                    claim, hit = _cover(seat["seat_keys"])
                    entry["seat_claim"] = claim
                    entry["seat_hit"] = hit
                    entry["same_window"] = (
                        set(seat["seat_keys"]) == set(mode_names)
                    )
                entry["mode_claim"], entry["mode_hit"] = _cover(mode_names)
                entry["legacy_claim"], entry["legacy_hit"] = _cover(
                    legacy_names
                )
                landings.append(entry)
        if not landings:
            continue
        rows = []
        for lower, upper, key, label in CENTER_LANDING_TIERS:
            members = [
                row for row in landings if lower <= row["sigma"] < upper
            ]
            if not members:
                continue
            rows.append(_center_landing_group(members, tier=key, label=label))
        states = []
        for state, key, label in state_labels:
            members = [row for row in landings if row["state"] == state]
            if not members:
                continue
            states.append(_center_landing_group(
                members, state=key, label=label,
                share=len(members) / len(landings),
            ))
        flat = [
            row for row in landings
            if row["sigma"] >= CENTER_LANDING_FLAT_RUNGS
        ]
        by_station: dict[str, list[float]] = {}
        for row in flat:
            by_station.setdefault(row["station"], []).append(row["error"])
        flat_stations = sorted(
            (
                {
                    "station": station,
                    "cells": len(values),
                    "bias_rungs": sum(values) / len(values),
                    "warm_share": sum(
                        1 for value in values if value > 0.0
                    ) / len(values),
                    "days": len({
                        row["event_date"] for row in flat
                        if row["station"] == station
                    }),
                }
                for station, values in by_station.items()
                if len(values) >= 8
            ),
            key=lambda entry: -abs(entry["bias_rungs"]),
        )[:10]
        stages_out.append({
            "stage": stage,
            "meaning": CENTER_LANDING_STAGE_MEANING.get(stage, ""),
            "cells": len(landings),
            "days": len(by_day),
            "since": (floor or "")[:10],
            "unseated_cells": sum(
                1 for row in landings if row["seat_claim"] is None
            ),
            "rows": rows,
            "states": states,
            "flat_stations": flat_stations,
            "flat_cells": len(flat),
        })
    if not stages_out:
        return {}
    return {
        "schema": "center_landing_v2",
        "flat_threshold_rungs": CENTER_LANDING_FLAT_RUNGS,
        "seat_rule": _center_landing_rule(),
        "stages": stages_out,
    }


def _center_landing_rule() -> dict[str, Any]:
    """The rule constants the landing table reads but does not spell out.

    Named in the page so a reader sees which seat rule and which skew gate
    the columns were built under, and folded into the section cache's salt
    so a change to any of them cannot keep serving a cached table built
    under the previous one (the fingerprint only hashes collector source).
    """

    try:                                             # noqa: PLC0415
        from bayes_basket_target.basket_kernel.delta_v2 import (
            SEAT_SHAPE_RULE_VERSION,
        )
        from bayes_basket_target.delta_v2_runtime import (
            LADDER_CENTER_DEFINITION,
            SKEW_GAP_THRESHOLD_SIGMA,
        )
    except ImportError:
        return {}
    return {
        "seat_shape_rule": SEAT_SHAPE_RULE_VERSION,
        "skew_threshold_sigma": float(SKEW_GAP_THRESHOLD_SIGMA),
        "centre_definition": str(LADDER_CENTER_DEFINITION),
    }


def _center_landing_salt() -> str:
    helpers = _loaded_source(_center_landing_seat) + _loaded_source(
        _center_landing_group
    )
    return json.dumps(_center_landing_rule(), sort_keys=True) + helpers


def _grade(value: float | None, key: str, *, higher_better: bool = False) -> str:
    """Green / amber / red for one metric, or grey when it is not measurable."""

    if value is None:
        return "muted"
    good, warn = SCORECARD_THRESHOLDS[key]
    if higher_better:
        return "green" if value >= good else ("amber" if value >= warn else "red")
    return "green" if value <= good else ("amber" if value <= warn else "red")


def _worst(healths: list[str]) -> str:
    for level in ("red", "amber", "green"):
        if level in healths:
            return level
    return "muted"


def _bucket_order(centers: dict[str, float]) -> list[str]:
    return sorted(centers, key=lambda key: centers[key])


def _truth_bucket(centers: dict[str, float], order: list[str], truth: float) -> str:
    """Which market rung the settled high actually landed on.

    The end rungs are open ("20C or below"), so the nearest centre is the
    right answer there too -- clamping falls out of the same comparison.
    """

    return min(order, key=lambda key: abs(centers[key] - truth))


# Reading rules for the scorecard sample (2026-09-17 review).  Eight
# settled event days is the bar the width lane already holds its own fit to
# (``center_width_store.MIN_QUALIFYING_DAYS``), and it is also the fewest
# whole days a day-level bootstrap can resample into an interval.
SCORECARD_MIN_EVENT_DAYS = 8
SCORECARD_MAX_WINDOW_DAYS = 45
PIT_BIN_COUNT = 10


def _prediction_scores(
    probability: dict[str, float],
    centers: dict[str, float],
    truth: float,
) -> dict[str, float] | None:
    """Every per-day quantity the six layers are built from."""

    order = _bucket_order(centers)
    if not order:
        return None
    total = sum(probability.get(key, 0.0) for key in order)
    if total <= 0:
        return None
    probabilities = [probability.get(key, 0.0) / total for key in order]
    values = [centers[key] for key in order]
    actual_key = _truth_bucket(centers, order, truth)
    actual_index = order.index(actual_key)

    # Rung width read off the station's own ladder rather than from the unit
    # column: 1.0C on a Celsius book, 1.11C on a Fahrenheit one, and correct
    # by construction for any ladder either book grows later.
    steps = sorted(
        values[index + 1] - values[index] for index in range(len(values) - 1)
    )
    rung_c = steps[len(steps) // 2] if steps else 1.0
    if rung_c <= 0:
        return None

    mean = sum(p * v for p, v in zip(probabilities, values))
    variance = sum(p * (v - mean) ** 2 for p, v in zip(probabilities, values))
    error = mean - truth

    # CRPS over the bucket ladder, and the Brier score across all rungs.
    crps = 0.0
    cumulative = 0.0
    for index, p in enumerate(probabilities):
        cumulative += p
        crps += (cumulative - (1.0 if index >= actual_index else 0.0)) ** 2
    brier = sum(
        (p - (1.0 if index == actual_index else 0.0)) ** 2
        for index, p in enumerate(probabilities)
    )
    # Randomised PIT, taken in expectation so the page needs no random draw.
    # A discrete forecast puts the truth somewhere in [F(below), F(at)], and
    # the randomised PIT is uniform on that interval; spreading one unit of
    # mass across it is exactly the average over every draw.  Under a
    # calibrated forecast the ten bins are then equal by construction.
    #
    # Until 2026-09-17 this was the mid-PIT, ``F(below) + p/2``, described
    # as randomised.  The mid-PIT is not uniform when calibrated: every
    # truth lands mid-bucket, so a forecast with 30-40% on its peak bucket
    # humps in the middle on its own.  Measured on the page's own sample,
    # a perfectly calibrated forecast would have read 0.115 in the middle
    # and 0.082 at the ends, and the page's 5.7% deviation was 4.0% as a
    # randomised PIT -- the bars doubled the hump they were reporting.
    lower = sum(probabilities[:actual_index])
    upper = lower + probabilities[actual_index]
    pit_bins = [0.0] * PIT_BIN_COUNT
    if upper - lower <= 1e-12:
        pit_bins[min(PIT_BIN_COUNT - 1, max(0, int(lower * PIT_BIN_COUNT)))] = 1.0
    else:
        for index in range(PIT_BIN_COUNT):
            left = max(lower, index / PIT_BIN_COUNT)
            right = min(upper, (index + 1) / PIT_BIN_COUNT)
            if right > left:
                pit_bins[index] = (right - left) / (upper - lower)
    pit_total = sum(pit_bins)
    pit_bins = [value / pit_total for value in pit_bins]
    # The reliability diagram is built from EVERY rung, not just the settled
    # one: each (promised probability, did it happen) pair is one observation.
    # Scoring only the settled rung would take a biased sample of high
    # probabilities and report a base rate of 1 instead of 1/rungs.
    reliability_pairs = [
        (p, 1.0 if index == actual_index else 0.0)
        for index, p in enumerate(probabilities)
    ]
    return {
        # Rung-denominated, so a Celsius and a Fahrenheit book average
        # together honestly.  The degree figures ride along for display only.
        "error": error / rung_c,
        "abs_error": abs(error) / rung_c,
        "square_error": (error / rung_c) ** 2,
        "error_c": error,
        "abs_error_c": abs(error),
        "square_error_c": error * error,
        "rung_c": rung_c,
        "crps": crps,
        "brier": brier,
        "pit_bins": pit_bins,
        # Variance, not standard deviation: a calibrated forecast's mean
        # squared error equals its mean VARIANCE, so the width that answers
        # RMSE is the root of the averaged variance.  Averaging standard
        # deviations first reads narrower by Jensen -- 1.25 against 1.43 on
        # the 2026-09-17 sample -- and flattered a too-wide book.
        "variance_rungs": variance / (rung_c * rung_c),
        "variance_c": variance,
        "reliability_pairs": reliability_pairs,
    }


def _cluster_bootstrap_half_width(
    clusters: Mapping[str, tuple[float, float]],
    draws: int = 600,
    seed: int = 7,
) -> float | None:
    """Half-width of a 95% interval on a weighted mean, resampling whole days.

    ``clusters`` maps an event day to ``(sum of weight * value, sum of
    weight)``.  Every station's error on one day shares that day's weather,
    so rows are not independent draws: resampled one row at a time, the
    2026-09-17 sample reported +-0.004 rungs on its MAE where resampling
    event days gives +-0.056, fifteen times wider.
    """

    keys = sorted(key for key, (_value, weight) in clusters.items() if weight > 0)
    if len(keys) < SCORECARD_MIN_EVENT_DAYS:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        value_sum = weight_sum = 0.0
        for _day in keys:
            value, weight = clusters[keys[rng.randrange(len(keys))]]
            value_sum += value
            weight_sum += weight
        means.append(value_sum / weight_sum)
    means.sort()
    low = means[int(0.025 * draws)]
    high = means[int(0.975 * draws)]
    return (high - low) / 2.0


def _reliability_decomposition(
    pairs: list[tuple[float, float]] | list[tuple[float, float, float]],
    bins: int = 10,
) -> dict[str, float | list[dict[str, Any]]]:
    """Murphy's decomposition, on the probability given to the settled rung.

    Reliability is the gap between what was promised and what happened,
    resolution is how far the forecasts move away from the base rate, and the
    two only mean something together: a forecast that always says the base
    rate is perfectly reliable and completely useless.

    A pair may carry a third element, its weight; ``n`` in each bin is then
    the summed weight, so a population threshold reads in the same units the
    sample is weighted in.
    """

    if not pairs:
        return {"ece": None, "reliability": None, "resolution": None,
                "uncertainty": None, "bins": []}
    # Five-percent steps capped at 50% (user ruling, 2026-08-25): the band
    # cards in section six now render on the same grid, so the knob table
    # and this audit finally line up row for row.  Everything promised at
    # half or more pools into one open top bin -- claims that high are rare
    # enough that finer rows would be noise wearing labels.
    buckets: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    total = happened_total = 0.0
    for pair in pairs:
        promised, happened = float(pair[0]), float(pair[1])
        weight = float(pair[2]) if len(pair) > 2 else 1.0
        index = min(10, max(0, int(promised / 0.05)))
        slot = buckets[index]
        slot[0] += weight * promised
        slot[1] += weight * happened
        slot[2] += weight
        total += weight
        happened_total += weight * happened
    if total <= 0:
        return {"ece": None, "reliability": None, "resolution": None,
                "uncertainty": None, "bins": []}
    base_rate = happened_total / total
    ece = reliability = resolution = 0.0
    rows = []
    for index in sorted(buckets):
        promised_sum, happened_sum, weight = buckets[index]
        if weight <= 0:
            continue
        share = weight / total
        mean_promised = promised_sum / weight
        mean_happened = happened_sum / weight
        ece += share * abs(mean_promised - mean_happened)
        reliability += share * (mean_promised - mean_happened) ** 2
        resolution += share * (mean_happened - base_rate) ** 2
        rows.append({
            "bin": (
                "≥50%" if index >= 10
                else f"{index * 5}-{index * 5 + 5}%"
            ),
            "n": weight,
            "promised": mean_promised,
            "happened": mean_happened,
        })
    return {
        "ece": ece,
        "reliability": reliability,
        "resolution": resolution,
        "uncertainty": base_rate * (1.0 - base_rate),
        "bins": rows,
    }


def _scorecard_window(conn: sqlite3.Connection) -> dict[str, Any]:
    """Where the scored sample starts: P0's serving-arithmetic anchor.

    The scorecard read a calendar window over an append-only table, so a
    rule change never reached it: on 2026-09-17 the stable pool (charter
    134) had served for three days while 95% of the scored station-day-leads
    were decided under the contest it replaced, and the retired rule's too-
    wide book (width/error 1.53) painted the sharpness, PIT and calibration
    lamps for a book serving at 1.00.  The anchor is the same boundary the
    band audit and the fitting lanes already cut at.  The calendar window
    stays as an outer cap, and is the whole window only when no anchor can
    be read -- which the header then says.
    """

    try:
        from bayes_basket_target.center_width_store import (  # noqa: PLC0415
            served_arithmetic_anchor,
        )
    except ImportError:
        return {"anchor": None, "anchor_state": "unavailable"}
    try:
        anchor = served_arithmetic_anchor(conn, stage="P0")
    except Exception:  # noqa: BLE001 -- a broken anchor store is a reading, not a crash
        return {"anchor": None, "anchor_state": "error"}
    if not anchor:
        return {"anchor": None, "anchor_state": "missing"}
    return {"anchor": str(anchor), "anchor_state": "ok"}


def _parse_utc(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def collect_calibration_scorecard(conn: sqlite3.Connection) -> dict[str, Any]:
    """Six layers of calibration quality, each reduced to one verdict.

    The scores already existed in pieces across the page; what was missing
    was a judgement.  Every layer here answers "is this good" with a colour
    and a sentence, and keeps the number beside it as the evidence rather
    than the headline.

    Two rules on the sample, both from the 2026-09-17 review.  It starts at
    P0's serving-arithmetic anchor (``_scorecard_window``).  And each
    station x event day x lead counts once: the evaluation table writes a
    row per candidate view, cycle, season bin and re-evaluation, a median 59
    rows per station-day and up to 1,459, so an unweighted mean was a vote
    of whichever scopes the pool happened to mint most.  Weighted equally,
    the calibration lamp's worst bin moved from ">=50% under-claimed by 7.7
    points" to "25-30% under-claimed by 5.6", and the >=50% bin read 58.5%
    promised against 58.2% happened.
    """

    if not _table_exists(
        conn, "calibration_cross_native_ensemble_oos_prediction"
    ):
        return {"available": False}
    truth: dict[tuple[str, str], float] = {}
    for station, day, tmax in conn.execute(
        "SELECT station_id, local_date, official_tmax_c"
        " FROM calibration_station_daily_high_truth"
        " WHERE qc_status='ACCEPTED' ORDER BY revision_no"
    ):
        truth[(str(station), str(day))] = float(tmax)
    if not truth:
        return {"available": False}

    window = _scorecard_window(conn)
    anchor_at = _parse_utc(window["anchor"]) if window["anchor"] else None
    if window["anchor"] and anchor_at is None:
        window = {"anchor": None, "anchor_state": "unparseable"}
    window["max_days"] = SCORECARD_MAX_WINDOW_DAYS
    # The SQL bound is the anchor's whole second, which can only admit a few
    # rows too many; the exact comparison below drops them.
    anchor_prefix = (
        anchor_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        if anchor_at is not None else ""
    )

    scored: list[dict[str, Any]] = []
    stations: set[str] = set()
    latest = ""
    for (
        station, day, lead, probabilities_json, centers_json, decided
    ) in conn.execute(
        "SELECT station_id, event_date, lead_bucket, probabilities_json,"
        " bucket_centers_c_json, decision_at_utc"
        " FROM calibration_cross_native_ensemble_oos_prediction"
        " WHERE event_date>=date('now',?) AND decision_at_utc>=?"
        " ORDER BY decision_at_utc",
        (f"-{SCORECARD_MAX_WINDOW_DAYS} days", anchor_prefix),
    ):
        actual = truth.get((str(station), str(day)))
        if actual is None:
            continue
        # Calendar leads (d0/d1/d2) were retired for hour buckets; rows under
        # the old vocabulary stop in mid-August and would drag stale scopes
        # into a board that is meant to read as current.
        if not str(lead).startswith("h"):
            continue
        if anchor_at is not None:
            decided_at = _parse_utc(str(decided))
            if decided_at is None or decided_at < anchor_at:
                continue
        try:
            probability = json.loads(probabilities_json)
            centers = {
                str(key): float(value)
                for key, value in json.loads(centers_json).items()
            }
        except (ValueError, TypeError, AttributeError):
            continue
        scores = _prediction_scores(probability, centers, actual)
        if scores is None:
            continue
        # The no-skill reference, a flat forecast over the same ladder.  It
        # is not the gate's climatology, but measured on the 2026-09-17
        # sample it scores alike (mean CRPS 2.05 against 2.06 for a 60-day
        # kernel climatology built with the gate's own function), because a
        # summer window drifts off an autumn event as far as flat does.
        order = _bucket_order(centers)
        flat_scores = _prediction_scores(
            {key: 1.0 / len(order) for key in order}, centers, actual
        )
        if flat_scores is None:
            continue
        scores["flat_crps"] = flat_scores["crps"]
        scores["flat_brier"] = flat_scores["brier"]
        scores["station"] = str(station)
        scores["event_date"] = str(day)
        scores["lead"] = str(lead)
        scored.append(scores)
        stations.add(str(station))
        latest = max(latest, str(decided))

    if not scored:
        return {"available": False, "window": window}
    copies = Counter(
        (row["station"], row["event_date"], row["lead"]) for row in scored
    )
    for row in scored:
        row["weight"] = 1.0 / copies[
            (row["station"], row["event_date"], row["lead"])
        ]
    station_days = {(row["station"], row["event_date"]) for row in scored}
    event_days = {row["event_date"] for row in scored}
    accumulating = len(event_days) < SCORECARD_MIN_EVENT_DAYS

    def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
        weight = sum(row["weight"] for row in rows)
        if weight <= 0:
            return None
        return sum(row["weight"] * row[key] for row in rows) / weight

    def _skill(rows: list[dict[str, Any]], key: str) -> float | None:
        ours, theirs = _mean(rows, key), _mean(rows, f"flat_{key}")
        if ours is None or theirs is None or theirs <= 0:
            return None
        return 1.0 - ours / theirs

    def _width_ratio(rows: list[dict[str, Any]]) -> float | None:
        variance = _mean(rows, "variance_rungs")
        square = _mean(rows, "square_error")
        if variance is None or not square:
            return None
        return (variance ** 0.5) / (square ** 0.5)

    def _day_clusters(key: str) -> dict[str, tuple[float, float]]:
        clusters: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for row in scored:
            slot = clusters[row["event_date"]]
            slot[0] += row["weight"] * row[key]
            slot[1] += row["weight"]
        return {day: (slot[0], slot[1]) for day, slot in clusters.items()}

    # ---- layer 1: point forecast --------------------------------------
    bias = _mean(scored, "error")
    mae = _mean(scored, "abs_error")
    rmse = (_mean(scored, "square_error") or 0.0) ** 0.5
    bias_c = _mean(scored, "error_c")
    mae_c = _mean(scored, "abs_error_c")
    rmse_c = (_mean(scored, "square_error_c") or 0.0) ** 0.5
    point_health = _worst([
        _grade(abs(bias) if bias is not None else None, "bias_rungs"),
        _grade(mae, "mae_rungs"),
    ])

    # ---- layer 2: probability -----------------------------------------
    crps_skill = _skill(scored, "crps")
    brier_skill = _skill(scored, "brier")
    total_weight = sum(row["weight"] for row in scored)
    pit_bins = [
        sum(row["weight"] * row["pit_bins"][index] for row in scored)
        / total_weight
        for index in range(PIT_BIN_COUNT)
    ]
    pit_deviation = max(
        abs(value - 1.0 / PIT_BIN_COUNT) for value in pit_bins
    )
    probability_health = _worst([
        _grade(crps_skill, "crps_skill", higher_better=True),
        _grade(brier_skill, "brier_skill", higher_better=True),
        _grade(pit_deviation, "pit_deviation"),
    ])

    # ---- layer 3: calibration -----------------------------------------
    decomposition = _reliability_decomposition([
        (promised, happened, row["weight"])
        for row in scored for promised, happened in row["reliability_pairs"]
    ])
    ece = decomposition["ece"]
    # ECE alone hides the shape.  Four fifths of all (rung, day) pairs sit in
    # the 0-10% bin, where the gap is tiny, so a weighted average stays small
    # even when every confident forecast is off by ten points.  The worst
    # well-populated bin is what says whether a number can be acted on.
    # ``n`` is summed weight: 100 station-day-lead rungs, not 100 rows.
    populated = [
        row for row in decomposition["bins"] if float(row["n"]) >= 100
    ]
    conditional_gap = max(
        (abs(row["promised"] - row["happened"]) for row in populated),
        default=None,
    )
    conditional_signed = None
    if populated:
        worst = max(
            populated, key=lambda row: abs(row["promised"] - row["happened"])
        )
        conditional_signed = worst["happened"] - worst["promised"]
    calibration_health = _worst([
        _grade(ece, "ece"),
        _grade(conditional_gap, "conditional_gap"),
    ])

    # ---- layer 5: sharpness (read against realised error) --------------
    width_rungs = (_mean(scored, "variance_rungs") or 0.0) ** 0.5
    width_c = (_mean(scored, "variance_c") or 0.0) ** 0.5
    spread_skill = _width_ratio(scored)
    sharpness_health = _grade(
        abs(spread_skill - 1.0) if spread_skill is not None else None,
        "spread_skill",
    )

    # ---- layer 6: stability --------------------------------------------
    mae_ci = _cluster_bootstrap_half_width(_day_clusters("abs_error"))
    crps_ci = _cluster_bootstrap_half_width(_day_clusters("crps"))
    ci_relative = None
    if mae_ci is not None and mae:
        ci_relative = mae_ci / mae
    stability_health = _grade(ci_relative, "ci_relative")

    rows = []
    order = {name: index for index, (name, _label) in enumerate(LEAD_LABELS)}
    by_lead: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scored:
        by_lead[row["lead"]].append(row)
    for lead in sorted(by_lead, key=lambda name: order.get(name, 9)):
        group = by_lead[lead]
        group_bias = _mean(group, "error")
        group_mae = _mean(group, "abs_error")
        group_rmse = (_mean(group, "square_error") or 0.0) ** 0.5
        group_ratio = _width_ratio(group)
        group_crps_skill = _skill(group, "crps")
        rows.append({
            "lead_bucket": lead,
            "lead_label": dict(LEAD_LABELS).get(lead, lead),
            "station_days": len({
                (row["station"], row["event_date"]) for row in group
            }),
            "bias_rungs": group_bias,
            "mae_rungs": group_mae,
            "rmse_rungs": group_rmse,
            "bias_c": _mean(group, "error_c"),
            "mae_c": _mean(group, "abs_error_c"),
            "crps_skill": group_crps_skill,
            "brier_skill": _skill(group, "brier"),
            "spread_skill": group_ratio,
            "bias_health": _grade(
                abs(group_bias) if group_bias is not None else None,
                "bias_rungs",
            ),
            "mae_health": _grade(group_mae, "mae_rungs"),
            "crps_health": _grade(
                group_crps_skill, "crps_skill", higher_better=True
            ),
            "spread_health": _grade(
                abs(group_ratio - 1.0) if group_ratio is not None else None,
                "spread_skill",
            ),
        })

    fusion = _collect_fusion_layer(conn)
    layers = [
        {
            "key": "point",
            "name": "点预测",
            "question": "中心到底准不准",
            "health": point_health,
            "verdict": _point_verdict(bias, mae),
            "detail": (
                f"偏差 {bias:+.2f} 档（{bias_c:+.2f}°C） · "
                f"平均误差 {mae:.2f} 档（{mae_c:.2f}°C） · "
                f"均方根 {rmse:.2f} 档（{rmse_c:.2f}°C）"
            ) if bias is not None and mae is not None else "证据不足",
        },
        {
            "key": "probability",
            "name": "概率预测",
            "question": "概率说得准不准",
            "health": probability_health,
            "verdict": _probability_verdict(crps_skill, pit_deviation),
            "detail": (
                f"CRPS 比气候好 {crps_skill:.0%} · "
                f"Brier 好 {brier_skill:.0%} · "
                f"PIT 最大偏离 {pit_deviation:.1%}"
            ) if crps_skill is not None and brier_skill is not None
            else "证据不足",
        },
        {
            "key": "calibration",
            "name": "校准",
            "question": "说 70% 是不是真的 70%",
            "health": calibration_health,
            "verdict": _calibration_verdict(
                decomposition, conditional_gap, conditional_signed
            ),
            "detail": (
                f"整体 ECE {ece:.1%} · 最差分箱偏离 {conditional_gap:.1%} · "
                f"分辨力 {decomposition['resolution']:.4f}"
            ) if ece is not None and conditional_gap is not None
            else "证据不足",
        },
        {
            "key": "fusion",
            "name": "模型融合",
            "question": "几家模型的权重分对了吗",
            "health": fusion["health"],
            "verdict": fusion["verdict"],
            "detail": fusion["detail"],
        },
        {
            "key": "sharpness",
            "name": "分布锐度",
            "question": "分布宽窄配不配它的准度",
            "health": sharpness_health,
            "verdict": _sharpness_verdict(spread_skill),
            "detail": (
                f"宽度 {width_rungs:.2f} 档（{width_c:.2f}°C，方差均值开方） · "
                f"实际误差 {rmse:.2f} 档 · 宽度/误差 {spread_skill:.2f}"
            ) if spread_skill is not None else "证据不足",
        },
        {
            "key": "stability",
            "name": "稳定性",
            "question": "上面这些数字靠不靠得住",
            "health": stability_health,
            "verdict": _stability_verdict(
                ci_relative, len(station_days), len(event_days)
            ),
            "detail": (
                # mae_ci bootstraps `abs_error`, which is rung-denominated so
                # a Celsius and a Fahrenheit book average together honestly.
                # Labelling it °C read one rung as one degree, which is only
                # true on the 34 Celsius stations.
                f"{len(station_days):,} 个站日 · {len(event_days)} 个事件日"
                f" · 按事件日重采样，平均误差 95% 区间 ±{mae_ci:.3f} 档"
                f" · CRPS ±{crps_ci:.3f}"
            ) if mae_ci is not None and crps_ci is not None
            else f"{len(station_days):,} 个站日 · {len(event_days)} 个事件日",
        },
    ]
    if accumulating:
        # Every lamp but fusion reads this sample; fusion reads the served
        # seats and keeps its own colour.  The numbers stay in ``detail`` so
        # the reading is visible while it cannot yet be judged.
        for layer in layers:
            if layer["key"] == "fusion":
                continue
            layer["health"] = "muted"
            layer["verdict"] = (
                f"攒证据 {len(event_days)}/{SCORECARD_MIN_EVENT_DAYS} 个事件日："
                "服务算术锚之后的已结算样本还不够下结论，下面的数字只作参考"
            )
    return {
        "available": True,
        "overall": _worst([layer["health"] for layer in layers]),
        "layers": layers,
        "rows": rows,
        "pit_bins": pit_bins,
        "fusion_pairs": fusion["pairs"],
        "fusion_summary": fusion["summary"],
        "window": window,
        "accumulating": accumulating,
        "min_event_days": SCORECARD_MIN_EVENT_DAYS,
        "row_count": len(scored),
        "station_day_count": len(station_days),
        "station_day_lead_count": len(copies),
        "event_day_count": len(event_days),
        "station_count": len(stations),
        "latest_decision_at_utc": latest or None,
        "latest_age_hours": _age_hours(latest or None),
    }


def _point_verdict(bias: float | None, mae: float | None) -> str:
    """All figures in rungs, so Celsius and Fahrenheit books read alike."""

    if bias is None or mae is None:
        return "还没有足够的已结算日可以判断"
    side = "偏热" if bias > 0 else "偏冷"
    if abs(bias) <= 0.3 and mae <= 1.0:
        return f"中心站得住：系统性{side}不到 {abs(bias):.2f} 档，平均误差在一档内"
    if abs(bias) > 0.8:
        return f"中心系统性{side} {abs(bias):.2f} 档，接近整整一档，会持续押错桶"
    if mae > 1.5:
        return f"单日平均差 {mae:.2f} 档，超过一档半，选桶方向常常不对"
    return f"中心略{side} {abs(bias):.2f} 档，平均误差 {mae:.2f} 档，尚在容忍范围"


def _probability_verdict(skill: float | None, pit: float | None) -> str:
    """Worded on the same lines the lamp is graded on.

    The PIT sentence used to start at 8% while the lamp turned amber at 4%,
    so an amber lamp read "分布形状匀称，概率可以直接用" (2026-09-17).
    """

    if skill is None or pit is None:
        return "还没有足够的已结算日可以判断"
    if skill < 0:
        return f"概率比直接用气候还差 {abs(skill):.0%}，这一层目前是负价值"
    pit_good, pit_warn = SCORECARD_THRESHOLDS["pit_deviation"]
    skill_good, _skill_warn = SCORECARD_THRESHOLDS["crps_skill"]
    head = f"比气候基准好 {skill:.0%}"
    if pit > pit_warn:
        return f"{head}，但概率分布形状明显歪（PIT 偏离 {pit:.1%}），尾部不可信"
    if pit > pit_good:
        return f"{head}，但分布形状有点歪（PIT 偏离 {pit:.1%}），尾部别压满"
    if skill < skill_good:
        return f"{head}，优势很薄，接近没有信息"
    return f"{head}，且分布形状匀称，概率可以直接用"


def _calibration_verdict(
    decomposition: dict[str, Any],
    conditional_gap: float | None,
    conditional_signed: float | None,
) -> str:
    """Judged on the worst usable bin, not on the flattering average."""

    ece = decomposition["ece"]
    if ece is None or conditional_gap is None:
        return "还没有足够的已结算日可以判断"
    resolution = decomposition["resolution"] or 0.0
    reliability = decomposition["reliability"] or 0.0
    direction = (
        "说少了" if (conditional_signed or 0.0) > 0 else "说多了"
    )
    points = abs(conditional_signed or 0.0) * 100.0
    if conditional_gap > 0.10:
        return (
            f"能下注的那一段不可信：概率高的时候{direction} "
            f"{points:.0f} 个百分点，整体 ECE {ece:.1%} 是被低概率段拉平的"
        )
    if conditional_gap > 0.05:
        return (
            f"低概率段准，但概率一上来就{direction} {points:.0f} 个百分点"
            f"——真要下注的那一段有系统性偏差"
        )
    if resolution <= reliability:
        return f"偏差小（{ece:.1%}），但分辨力不足——概率总在基准率附近晃"
    return f"说 70% 就真是 70%（各段偏离都在 {conditional_gap:.1%} 内），可以直接用"


def _sharpness_verdict(ratio: float | None) -> str:
    if ratio is None:
        return "还没有足够的已结算日可以判断"
    if abs(ratio - 1.0) <= 0.20:
        return f"宽度配得上准度（宽度/误差 {ratio:.2f}），既不吹牛也不含糊"
    if ratio < 0.55:
        return f"分布太窄（只有实际误差的 {ratio:.2f} 倍）——过度自信，尾部会被打脸"
    if ratio > 1.45:
        return f"分布太宽（实际误差的 {ratio:.2f} 倍）——过于保守，等于没给信息"
    if ratio < 1.0:
        return f"分布偏窄（{ratio:.2f} 倍），可以用但别压满"
    return f"分布偏宽（{ratio:.2f} 倍），信息量打了折扣"


def _stability_verdict(
    relative: float | None, station_days: int, event_days: int
) -> str:
    """Resampled by event day, so it says so in days, not rows."""

    sample = f"{station_days:,} 个站日、{event_days} 个事件日"
    if relative is None:
        return f"只有 {sample}，还不足以按事件日重采样出区间"
    if relative <= 0.15:
        return f"{sample}，整天整天重采样，平均误差区间在数值的 {relative:.0%} 以内，结论稳"
    if relative <= 0.30:
        return f"{sample}，结论大方向可信，但小差距（几个百分点）别当真"
    return f"只有 {sample}，区间比数值本身还宽——上面每个判断都是暂定的"


def _fusion_synthetic_residuals(
    systems: Sequence[str],
    correlation: Mapping[str, Mapping[str, float]],
    sigmas: Sequence[float],
) -> dict[str, dict[str, float]] | None:
    """Residual series whose population covariance is exactly ``D R D``.

    Production weighs a seat from per-day train residuals.  The SSOT keeps
    what those residuals summarise -- the correlation matrix on the evidence
    row, and each system's variance through the inverse-variance shadow --
    but not the days.  Re-deriving the solver from those pieces would put a
    second copy of the rule on this page, and the copy that stood here until
    2026-09-17 had drifted three ways: it refused a lone active system where
    production allows one, ignored the family caps, and scaled by train RMSE
    where production scales by the residual sigma.  So the covariance is
    turned back into series that carry it exactly, and those are handed to
    the production function unchanged.

    Column ``m`` is ``sqrt(2) cos(2 pi (m+1) t / n)``: zero mean, mean square
    one, and orthogonal to its siblings while ``n > 2k``.  With ``S = L L^T``
    the rows ``x_t = L z_t`` have population covariance ``S``, the normaliser
    production divides by.  ``None`` when the stored correlation is not
    positive definite, which a matrix built from pairwise day sets can be.
    """

    from bayes_basket_target.native_ensemble_oos import (  # noqa: PLC0415
        MINIMUM_VARIANCE_MIN_DAYS,
    )

    size = len(systems)
    if size < 2 or len(sigmas) != size:
        return None
    try:
        covariance = [
            [
                float(correlation[left][right])
                * float(sigmas[row])
                * float(sigmas[column])
                for column, right in enumerate(systems)
            ]
            for row, left in enumerate(systems)
        ]
    except (KeyError, TypeError, ValueError):
        return None
    lower = [[0.0] * size for _ in range(size)]
    for row in range(size):
        for column in range(row + 1):
            value = covariance[row][column] - sum(
                lower[row][term] * lower[column][term]
                for term in range(column)
            )
            if row == column:
                if value <= 1e-12:
                    return None
                lower[row][row] = math.sqrt(value)
            else:
                lower[row][column] = value / lower[column][column]
    days = max(MINIMUM_VARIANCE_MIN_DAYS, 2 * size + 2)
    basis = [
        [
            math.sqrt(2.0) * math.cos(2.0 * math.pi * (term + 1) * day / days)
            for term in range(size)
        ]
        for day in range(days)
    ]
    return {
        system: {
            f"synthetic-{day:02d}": sum(
                lower[row][term] * basis[day][term]
                for term in range(row + 1)
            )
            for day in range(days)
        }
        for row, system in enumerate(systems)
    }


def _fusion_reconstruction(
    *,
    served: Mapping[str, float],
    correlation: Mapping[str, Mapping[str, float]],
    inverse_variance: Mapping[str, float] | None,
    member_scale: Mapping[str, float],
    member_train_n: Mapping[str, int | None],
    family_by_system: Mapping[str, str],
    family_caps: Mapping[str, float],
    kappa: float | None,
) -> dict[str, Any]:
    """A served seat's weights, re-solved by production's own functions.

    Every input is what the SSOT kept of what production read:

    * ``inverse_variance`` is the evaluation's shadow scheme, which is
      ``_minimum_variance_weights`` at shrinkage 1.0 over the same train
      series, so its shares are ``1/var`` of exactly the diagonal the served
      solve used.  Absent means that call returned nothing: the shared train
      window was under ``MINIMUM_VARIANCE_MIN_DAYS`` and production weighed
      the seat by inverse error.
    * ``member_scale`` and ``member_train_n`` are the member artifact's
      residual sigma and fit case count, which is what that fallback reads.
      The artifact persists the serving fit while the seat was weighed on
      the gate fit, a few days shorter, so a fallback seat re-solves only
      approximately.
    * ``kappa`` is parsed from the artifact's combination method, so a pool
      seat is shrunk by the value it was minted with.

    ``basis`` says which reconstruction this was: ``exact`` when the
    diagonal came from the shadow; ``capped`` when the shadow's shares had
    hit a family cap, which hides the variance ratio, so the member sigma
    stands in; ``fallback`` when the seat was weighed by inverse error; and
    ``non_psd`` when the stored correlation admits no covariance.  Only an
    ``exact`` distance checks production -- the others measure how much of
    it this page can reconstruct.
    """

    from bayes_basket_target.contracts import ContractError  # noqa: PLC0415
    from bayes_basket_target.native_ensemble_oos import (  # noqa: PLC0415
        SEAT_WEIGHT_RULE_INVERSE_RMSE,
        _seat_weights,
    )
    from bayes_basket_target.stable_pool import (  # noqa: PLC0415
        stable_pool_weights,
    )

    systems = sorted(served)
    families = {system: family_by_system[system] for system in systems}
    residuals: dict[str, dict[str, float]] = {system: {} for system in systems}
    basis = "fallback"
    if inverse_variance:
        shares = {
            system: float(inverse_variance.get(system) or 0.0)
            for system in systems
        }
        family_share: dict[str, float] = defaultdict(float)
        for system, share in shares.items():
            family_share[families[system]] += share
        capped = any(
            abs(share - float(family_caps.get(family, 0.75))) <= 1e-9
            for family, share in family_share.items()
        )
        if capped or min(shares.values()) <= 0.0:
            basis = "capped"
            sigmas = [float(member_scale[system]) for system in systems]
        else:
            basis = "exact"
            sigmas = [1.0 / math.sqrt(shares[system]) for system in systems]
        synthetic = _fusion_synthetic_residuals(systems, correlation, sigmas)
        if synthetic is None:
            return {"basis": "non_psd", "weights": None, "skill_rule": None}
        residuals = synthetic
    try:
        weights, skill_rule = _seat_weights(
            residuals_by_system=residuals,
            train_error_scale_by_system={
                system: float(member_scale[system]) for system in systems
            },
            family_by_system=families,
            family_caps=family_caps,
            train_n_by_system={
                system: member_train_n.get(system) for system in systems
            },
        )
        if kappa is not None:
            weights = stable_pool_weights(weights, families, kappa=kappa)
    except (ContractError, KeyError, ValueError, ZeroDivisionError):
        return {"basis": basis, "weights": None, "skill_rule": None}
    if skill_rule == SEAT_WEIGHT_RULE_INVERSE_RMSE:
        basis = "fallback"
    return {"basis": basis, "weights": dict(weights), "skill_rule": skill_rule}


def _fusion_rule_label(kappa: float | None, skill_rule: str | None) -> str:
    from bayes_basket_target.native_ensemble_oos import (  # noqa: PLC0415
        SEAT_WEIGHT_RULE_COVARIANCE,
    )

    if skill_rule is None:
        return "—"
    covariance = skill_rule == SEAT_WEIGHT_RULE_COVARIANCE
    if kappa is not None:
        return f"稳定池 κ={kappa:g} · {'协方差' if covariance else '逆误差'}"
    return "最小方差" if covariance else "逆误差兜底"


def _scorecard_salt(conn: sqlite3.Connection) -> str:
    """What the scorecard cache must also expire on.

    ``_section_fingerprint`` hashes only the collector it is handed.  The
    scorecard's arithmetic lives in the helpers it calls, the fusion layer
    defers to production's weighting functions, and the sample starts at
    P0's serving anchor, which moves when a version is declared.  A change
    to any of them changes what the lamps read.
    """

    from bayes_basket_target import (  # noqa: PLC0415
        native_ensemble_oos,
        stable_pool,
    )

    sources = "\n".join(
        _loaded_source(function)
        for function in (
            _bucket_order,
            _truth_bucket,
            _grade,
            _worst,
            _prediction_scores,
            _cluster_bootstrap_half_width,
            _reliability_decomposition,
            _scorecard_window,
            _parse_utc,
            _point_verdict,
            _probability_verdict,
            _calibration_verdict,
            _sharpness_verdict,
            _stability_verdict,
            _collect_fusion_layer,
            _fusion_reconstruction,
            _fusion_synthetic_residuals,
            _fusion_rule_label,
            native_ensemble_oos._seat_weights,
            native_ensemble_oos._minimum_variance_weights,
            native_ensemble_oos._bounded_family_weights,
            native_ensemble_oos._capped_family_weights,
            native_ensemble_oos._weight_maturity,
            stable_pool.stable_pool_weights,
            stable_pool.shrink_toward_baseline,
            stable_pool.family_balanced_weights,
        )
    )
    return "\n".join((
        json.dumps(_scorecard_window(conn), sort_keys=True),
        repr(sorted(SCORECARD_THRESHOLDS.items())),
        repr((SCORECARD_MIN_EVENT_DAYS, SCORECARD_MAX_WINDOW_DAYS,
              PIT_BIN_COUNT)),
        sources,
    ))


def _collect_fusion_layer(conn: sqlite3.Connection) -> dict[str, Any]:
    """Whether each served blend carries the weights production's rule gives.

    Until 2026-09-17 this compared the served weights against a minimum-
    variance optimum solved here from train RMSE, and three things had moved
    under it.  Charter 134 (2026-09-14) shrinks every pool seat halfway
    toward family-balanced weights, so a gap of about 20% was the design
    rather than a finding.  Production had weighed on the residual sigma
    since the bias correction, so a system with a large offset but a steady
    error -- GEFS at OEJN, RMSE 6.4 against sigma 0.8 -- was pointed the
    wrong way.  And the sample was the newest 400 evidence rows, of which 85
    were served.

    Now every served seat -- activation pointer, artifact, evidence, the
    path ``collect_serving_weights`` reads -- is re-solved by production's
    own functions from what the SSOT kept (``_fusion_reconstruction``).  The
    distance grades the lamp only where the diagonal is recovered exactly;
    measured at the rewrite, 686 of 826 served seats, 99th percentile 0.8%.
    The residual correlation grades it as before.
    """

    family_by_system = {
        str(system): str(family)
        for system, family in conn.execute(
            "SELECT ensemble_system_id, independence_family_id"
            " FROM calibration_native_ensemble_system"
        )
    }
    served_rows = conn.execute(
        "SELECT p.station_id, p.season_bin, p.lead_bucket, p.run_cycle,"
        " p.set_hash, ar.combination_method, se.content_sha256,"
        " se.selected_weights_json,"
        " se.cross_system_residual_correlation_json,"
        " se.run_cycle_by_system_json, se.family_weight_caps_json,"
        " se.train_end_date"
        " FROM (SELECT station_id, season_bin, lead_bucket, run_cycle,"
        "   artifact_id, available_system_set_hash_or_star AS set_hash,"
        "   MAX(generation)"
        "  FROM calibration_native_ensemble_activation"
        "  WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE'"
        "  AND season_bin IN"
        "   ('PEAK','DESCENT','TROUGH','ASCENT','ALLYEAR')"
        "  AND valid_until_utc > strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        "  GROUP BY station_id, season_bin, lead_bucket, run_cycle) p"
        " JOIN calibration_cross_native_ensemble_artifact ar"
        "  ON ar.cross_ensemble_artifact_id = p.artifact_id"
        " JOIN calibration_cross_native_ensemble_selection_evidence se"
        "  ON se.content_sha256 = ar.selection_evidence_sha256"
        " WHERE se.selected_system_count > 1"
    ).fetchall()

    rows: list[dict[str, Any]] = []
    unread = 0
    for (
        station, season, lead, cycle, set_hash, method, evidence_sha,
        weights_json, correlation_json, cycles_json, caps_json, train_end,
    ) in served_rows:
        try:
            served = {
                str(system): float(value)
                for system, value in json.loads(weights_json).items()
            }
            correlation = json.loads(correlation_json)
            cycles = json.loads(cycles_json) if cycles_json else {}
            caps = {
                str(family): float(value)
                for family, value in json.loads(caps_json).items()
            }
        except (AttributeError, TypeError, ValueError):
            unread += 1
            continue
        systems = sorted(served)
        if (
            len(systems) < 2
            or not isinstance(correlation, dict)
            or any(system not in family_by_system for system in systems)
        ):
            unread += 1
            continue
        off_diagonal = []
        for left in systems:
            for right in systems:
                if left == right:
                    continue
                try:
                    off_diagonal.append(float(correlation[left][right]))
                except (KeyError, TypeError, ValueError):
                    pass
        kappa = None
        for part in str(method or "").split("::")[1:]:
            if part.startswith("stable_pool_kappa="):
                try:
                    kappa = float(part.split("=", 1)[1])
                except ValueError:
                    pass
        evaluation = conn.execute(
            "SELECT json_extract(oos_metrics_json, '$.equal_weight_diagnostic"
            ".shadow_schemes.inverse_variance.weights')"
            " FROM calibration_cross_native_ensemble_oos_evaluation"
            " WHERE station_id=? AND season_bin=? AND lead_bucket=?"
            " AND run_cycle=? AND available_system_set_hash=?"
            " AND selection_evidence_sha256=?"
            " ORDER BY evaluated_at_utc DESC LIMIT 1",
            (station, season, lead, cycle, set_hash, evidence_sha),
        ).fetchone()
        member_scale: dict[str, float] = {}
        member_train_n: dict[str, int | None] = {}
        for system in systems:
            member = conn.execute(
                "SELECT spread_parameters_json, reliability_parameters_json"
                " FROM calibration_native_ensemble_system_artifact"
                " WHERE station_id=? AND lead_bucket=? AND run_cycle=?"
                " AND ensemble_system_id=? AND train_end_date<=?"
                " AND season_bin=?"
                " ORDER BY rowid DESC LIMIT 1",
                (
                    station, lead, str(cycles.get(system) or cycle), system,
                    train_end, season,
                ),
            ).fetchone()
            if member is None:
                break
            try:
                spread = json.loads(member[0])
                reliability = json.loads(member[1] or "{}")
                member_scale[system] = float(
                    spread.get("train_residual_sigma_c")
                    or spread["train_rmse_c"]
                )
            except (KeyError, TypeError, ValueError):
                break
            count = reliability.get("fit_case_count")
            member_train_n[system] = int(count) if count else None
        if evaluation is None or len(member_scale) != len(systems):
            unread += 1
            continue
        try:
            inverse_variance = (
                json.loads(evaluation[0]) if evaluation[0] else None
            )
        except (TypeError, ValueError):
            inverse_variance = None
        rebuilt = _fusion_reconstruction(
            served=served,
            correlation=correlation,
            inverse_variance=inverse_variance,
            member_scale=member_scale,
            member_train_n=member_train_n,
            family_by_system=family_by_system,
            family_caps=caps,
            kappa=kappa,
        )
        resolved = rebuilt["weights"]
        basis = rebuilt["basis"]
        if resolved is None and basis != "non_psd":
            basis = "unsolved"
        rows.append({
            "station_id": str(station),
            "season_bin": str(season),
            "lead_bucket": str(lead),
            "run_cycle": str(cycle),
            "rule": _fusion_rule_label(kappa, rebuilt["skill_rule"]),
            "basis": basis,
            "max_correlation": max(off_diagonal) if off_diagonal else None,
            "distance": (
                0.5 * sum(
                    abs(served[system] - float(resolved.get(system, 0.0)))
                    for system in systems
                )
                if resolved is not None else None
            ),
            "served": served,
            "resolved": resolved,
        })

    correlations = [
        row["max_correlation"] for row in rows
        if row["max_correlation"] is not None
    ]
    if not correlations:
        return {
            "health": "muted",
            "verdict": "还没有两家以上模型同时在座的在役组合",
            "detail": "证据不足",
            "pairs": [],
            "summary": {"served_count": len(rows), "unread_count": unread},
        }
    exact = [
        row for row in rows
        if row["basis"] == "exact" and row["distance"] is not None
    ]
    approximate = [row for row in rows if row not in exact]
    approximate_distances = sorted(
        row["distance"] for row in approximate if row["distance"] is not None
    )
    mean_correlation = sum(correlations) / len(correlations)
    mean_distance = (
        sum(row["distance"] for row in exact) / len(exact) if exact else None
    )
    correlation_health = _grade(mean_correlation, "residual_correlation")
    distance_health = _grade(mean_distance, "weight_distance")
    health = _worst([correlation_health, distance_health])
    if mean_distance is None:
        verdict = (
            f"模型之间残差相关 {mean_correlation:.2f}，"
            "但没有一份在役权重能从存档精确重解，自检做不了"
        )
    elif distance_health != "green":
        verdict = (
            f"在役权重和存档证据重解对不上（平均差 {mean_distance:.1%}）"
            "——先查数据再查解法"
        )
    elif correlation_health == "green":
        verdict = (
            f"在役权重与生产算术重解一致（平均差 {mean_distance:.1%}），"
            f"几家模型残差也互相独立（相关 {mean_correlation:.2f}）"
        )
    else:
        verdict = (
            f"在役权重与生产算术重解一致（平均差 {mean_distance:.1%}）；"
            f"灯色来自模型残差相关 {mean_correlation:.2f}，几家模型在犯相近的错"
        )
    return {
        "health": health,
        "verdict": verdict,
        "detail": (
            f"{len(rows)} 份在役权重 · 精确重解 {len(exact)} 份"
            f" · 平均最大相关 {mean_correlation:.2f}"
            + (f" · 离重解 {mean_distance:.1%}"
               if mean_distance is not None else "")
        ),
        "pairs": sorted(
            exact, key=lambda row: row["distance"], reverse=True
        )[:12],
        "summary": {
            "served_count": len(rows),
            "exact_count": len(exact),
            "unread_count": unread,
            "approximate_by_basis": dict(
                Counter(row["basis"] for row in approximate)
            ),
            "approximate_median_distance": (
                median(approximate_distances)
                if approximate_distances else None
            ),
            "approximate_max_distance": (
                approximate_distances[-1] if approximate_distances else None
            ),
        },
    }


def collect_gaps(
    conn: sqlite3.Connection,
    members: list[dict[str, Any]],
    stages: list[dict[str, Any]],
    tables: dict[str, Any],
) -> list[dict[str, Any]]:
    """Named holes, each with what it costs and whether it self-heals."""

    gaps = []
    for member in members:
        if member["health"] == "green":
            continue
        gaps.append({
            "severity": "high" if member["health"] == "red" else "medium",
            "what": f"{member['label']} 模型数据{member['note']}",
            "cost": (
                "该模型无法参与融合，少一路独立意见"
                + ("；此源无历史档案，丢一天永久丢一天"
                   if not member["backfillable"] else "")
            ),
            "self_heals": member["backfillable"],
        })
    # A family that arrives daily can still be near-useless to the fit if its
    # archive only goes back a few days -- the weight ramp prices it down and
    # the gate can refuse the combinations it appears in.
    deepest = max((m["training_days"] or 0) for m in members)
    for member in members:
        days = member["training_days"] or 0
        if deepest <= 0 or days >= deepest * 0.5:
            continue
        gaps.append({
            "severity": "medium",
            "what": (
                f"{member['label']} 的训练历史只有 {days} 天，"
                f"最深的同行有 {deepest} 天"
            ),
            "cost": (
                "样本浅的模型权重会被自动调低，参与融合的分量有限；"
                + ("可从公开档案回补" if member["backfillable"]
                   else "此源无历史档案，只能靠今后每天积累")
            ),
            "self_heals": True,
        })
    for stage in stages:
        if not stage.get("accumulating"):
            continue
        held = int(stage.get("active_cells") or 0)
        gaps.append({
            "severity": "low",
            "what": (
                f"{stage['stage']} 阶段校准仍在攒证据"
                f"（{stage['serving_days']}/{stage['min_days']} 天）"
            ),
            # Accumulating holds the incumbent rather than blanking it, so a
            # stage with live factors keeps applying them the whole time.
            "cost": (
                f"攒满前沿用上一版的 {held} 档修正系数，照常生效；"
                "攒满后自动重拟（保留、改值或撤回）"
                if held else
                "该阶段暂时不做分档修正，按原样服务，不影响正确性"
            ),
            "self_heals": True,
        })
    if tables.get("weight_lag_scopes"):
        gaps.append({
            "severity": "medium",
            "what": (
                f"{tables['weight_lag_scopes']} 个站×提前量的融合权重"
                "落后于偏差订正 3 天以上"
            ),
            "cost": "这些格子的权重仍是旧证据算的，融合比例可能不是当前最优",
            "self_heals": True,
        })
    stations = conn.execute(
        "SELECT COUNT(*) FROM calibration_station_registry"
    ).fetchone()[0]
    # From the capture table: the TAF member rows this used to count stopped
    # being written when that lane was retired (charter 133).
    taf = conn.execute(
        "SELECT COUNT(DISTINCT station_id)"
        " FROM calibration_forward_taf_capture WHERE tx_available=1"
    ).fetchone()[0]
    if taf < stations:
        gaps.append({
            "severity": "low",
            "what": f"{stations} 个站里只有 {taf} 个站的人工预报带温度组",
            "cost": "其余站的 P0-M 确认环节没有人工预报可用，是数据源本身的限制",
            "self_heals": False,
        })
    return gaps


LIVE_STATION_EXCLUSIONS_PATH = (
    ROOT / "config/trading/live_station_exclusions_v1.json"
)


def barred_station_set() -> set[str]:
    """Stations the live lane may not trade -- hand-ruled and automatic.

    One reader on purpose.  The live-readiness panel and the money board's
    bar sides both need this list, and two copies of the same file read is
    how two panels come to disagree about who is barred.
    """

    try:
        doc = json.loads(
            LIVE_STATION_EXCLUSIONS_PATH.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return set()
    return {
        str(row.get("station") or "").strip().upper()
        for key in ("excluded_stations", "auto_excluded_stations")
        for row in (doc.get(key) or [])
        if isinstance(row, dict) and row.get("station")
    }


# What a scope refusal means, for the one surface a person reads it on.
#
# The board printed these codes verbatim, and one of them is actively
# misleading: ``complete_basket_target_not_activated`` reads as "a feature
# is switched off" and means the opposite -- the target-inventory argmax
# chose the no-new-action baseline, which is an economic HOLD by a working
# engine.  On 2026-09-08 it was 11 of one batch's 15 refusals, and it cost
# an hour of misdiagnosis: the code was read as a disabled completion path
# while the engine was in fact declining to buy, on purpose, every time.
#
# Renamed nowhere.  The string is the charter's own spelling for clause 66
# (乙), clause 96 cites it as evidence, a doctrine test binds it to the
# kernel source, and 13,915 decision rows carry it -- so the identifier is
# constitutional and this is a reading fix, not a rename.  The raw codes
# stay untouched in ``blocked_scope_reasons`` for anything that greps.
#
# ``.get(name, name)`` on purpose: an unmapped code shows raw rather than
# being hidden or guessed at.
BLOCKED_SCOPE_REASON_LABELS = {
    "complete_basket_target_not_activated": "经济性持有（不新建仓最优）",
    "complete_basket_candidate_missing": "缺候选腿",
    "complete_basket_delta_not_positive": "增量不为正",
    "delta_v2_support_empty": "无可选支撑窗口",
    "exchange_minimum_lot_unaffordable": "交易所最小手买不起",
    "production_quote_expired": "报价过期",
    "p1_entry_window_not_open": "P1 入场窗未开",
    "preliminary_no_live_p1_pending": "P1 待定（初步）",
    "sizing_clamped_below_authorized_clip": "定量被砍到授权额以下",
    "bundle_rescore_refused": "重评拒绝",
    "support_floor_after_lift": "抬手后支持集地板拒",
    "no_decision_moment": "非决策时刻",
}


def _risk_metrics_from_days(
    rows: Sequence[Sequence[Any]], initial_capital_usdc: float
) -> dict[str, Any]:
    """The book as a portfolio: Sharpe against its target, and its cousins.

    ``rows`` are one per event day, oldest first: day, stake, pnl, claimed
    EV, stake-weighted odds, stake-weighted cost, stake-weighted hits, the
    settlement count and the hit count.  Either book can supply them -- the
    live book does, since the 2026-09-16 ruling made the paper ledger a rule
    comparator -- so the arithmetic below exists once.

    One return per event day -- the day's settled P&L over the equity the
    book opened that day with -- which is the series the equity curve is
    actually built from, so the chart and the ratios cannot disagree.
    Annualised at 365, not 252: this book trades every calendar day it has
    a market for, weekends included.

    The honest part is the error bar.  A Sharpe estimated on a handful of
    days is almost pure noise, and the classical standard error says so:
    ``sqrt((1 + S^2/2) / n)`` per period, which annualises by the same
    root-365 the ratio does.  On this book that error bar is several times
    the estimate, so the section leads with the interval and with how many
    days it would take to tell the number from zero -- otherwise a reader
    sees "1.54" beside a target of 2 and thinks the gap is the finding.
    """

    # Claimed EV comes along for the ride: a basket costing C pays 1 when
    # the truth lands inside, so a unit of stake buys 1/C of payout and
    # expects p/C - 1 back.  That is the same ``ev_per_usdc`` the coverage
    # section reports, summed in money instead of averaged per seat, so
    # the board has exactly one definition of EV and not a second one
    # wearing the same name.
    if len(rows) < 3:
        return {"available": False, "days": len(rows)}
    equity = initial_capital_usdc
    series: list[dict[str, Any]] = []
    returns: list[float] = []
    claimed_ev_total = 0.0
    claimed_equity = initial_capital_usdc
    odds_weighted = 0.0
    cost_weighted = 0.0
    hit_weighted = 0.0
    chain_count = 0
    hit_count = 0
    for day, staked, pnl, claimed_ev, odds_w, cost_w, hit_w, n, hits in rows:
        odds_weighted += float(odds_w or 0.0)
        cost_weighted += float(cost_w or 0.0)
        hit_weighted += float(hit_w or 0.0)
        chain_count += int(n or 0)
        hit_count += int(hits or 0)
        opening = equity
        value = float(pnl or 0.0)
        expected = float(claimed_ev or 0.0)
        returns.append(value / opening if opening > 0 else 0.0)
        equity = opening + value
        claimed_ev_total += expected
        claimed_equity += expected
        series.append({
            "event_date": str(day),
            "staked_usdc": round(float(staked or 0.0), 2),
            "pnl_usdc": round(value, 2),
            "claimed_ev_usdc": round(expected, 2),
            "opening_equity_usdc": round(opening, 2),
            "equity_usdc": round(equity, 2),
            "claimed_equity_usdc": round(claimed_equity, 2),
            "return": returns[-1],
        })
    count = len(returns)
    mean = sum(returns) / count
    variance = sum((r - mean) ** 2 for r in returns) / count
    sigma = math.sqrt(variance)
    # Downside deviation against a zero target: the losing days only, and
    # divided by the whole count -- the winning days are not evidence that
    # the downside is small, they are evidence that there is upside.
    downside = math.sqrt(
        sum(min(0.0, r) ** 2 for r in returns) / count
    )
    annual = 365.0
    root = math.sqrt(annual)
    sharpe = (mean / sigma * root) if sigma > 0 else None
    sortino = (mean / downside * root) if downside > 0 else None
    per_period = (mean / sigma) if sigma > 0 else 0.0
    sharpe_se = math.sqrt((1.0 + 0.5 * per_period ** 2) / count) * root
    # How long until the estimate could clear zero at 95%.  Solving
    # 1.96 * se(n) < |S| for n, with se scaling as 1/sqrt(n).
    days_needed = None
    if sharpe not in (None, 0.0):
        days_needed = int(math.ceil(
            (1.96 ** 2) * (1.0 + 0.5 * per_period ** 2) * annual
            / (sharpe ** 2)
        ))
    # The running peak starts at the capital the book opened with, not at
    # its first closing equity.  Seeded from the series instead, a book
    # that lost on day one had that loss excluded from its own drawdown:
    # this one opened at 1000, closed day one at 947.8, and reported -8.2%
    # where the trough is -9.3% below the money that was actually put in.
    # Value at Risk and its conditional cousin, from the empirical tail --
    # no distribution assumed, because fifteen points cannot support one.
    #
    # The honest part is the tail count.  Expected shortfall at 95% asks
    # for the mean of the worst 5% of days; on fifteen days that is the
    # worst 0.75 of a day, which rounds to exactly one observation.  Both
    # the 95% and 90% levels therefore report the same number and it is
    # not an estimate of a tail -- it is the worst day this book has had.
    # A tail needs its own sample: five observations in the tail wants
    # 100 days at 95%, 50 at 90%, 25 at 80%.  So each level is published
    # WITH the count standing behind it, and the section refuses to let a
    # one-observation number wear the same weight as the others.
    ordered = sorted(returns)

    def tail_at(level: float) -> dict[str, Any]:
        wanted = math.floor((1.0 - level) * count)
        size = max(1, wanted)
        tail = ordered[:size]
        return {
            "level": level,
            "tail_days": size,
            "tail_days_wanted": wanted,
            "var": tail[-1],
            "cvar": sum(tail) / size,
            "days_for_five_in_tail": int(math.ceil(5.0 / (1.0 - level))),
        }

    tails = [tail_at(level) for level in (0.95, 0.90, 0.80)]
    daily_ratios = [
        row["pnl_usdc"] / row["claimed_ev_usdc"]
        for row in series if row["claimed_ev_usdc"] > 0
    ]
    window = series[-7:]
    window_claim = sum(row["claimed_ev_usdc"] for row in window)
    trailing_7d = (
        sum(row["pnl_usdc"] for row in window) / window_claim
        if window_claim > 0 else None
    )
    losing = [r for r in returns if r < 0]
    peak = float(initial_capital_usdc)
    max_drawdown = 0.0
    trough_day = ""
    peak_day = ""
    running_peak_day = "开仓前"
    for row in series:
        if row["equity_usdc"] > peak:
            peak = row["equity_usdc"]
            running_peak_day = row["event_date"]
        drop = (row["equity_usdc"] - peak) / peak if peak > 0 else 0.0
        if drop < max_drawdown:
            max_drawdown = drop
            trough_day = row["event_date"]
            peak_day = running_peak_day
    # Least squares on cumulative P&L: the straight line a reader draws by
    # eye, made explicit so the eye is not doing the fitting.
    xs = list(range(1, count + 1))
    ys = [row["equity_usdc"] - initial_capital_usdc for row in series]
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    denominator = sum((x - mean_x) ** 2 for x in xs)
    slope = (
        sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
        if denominator else 0.0
    )
    intercept = mean_y - slope * mean_x
    residual = sum(
        (y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys)
    )
    total_ss = sum((y - mean_y) ** 2 for y in ys)
    total_return = (equity / initial_capital_usdc) - 1.0
    return {
        "available": True,
        "days": count,
        "first_day": series[0]["event_date"],
        "last_day": series[-1]["event_date"],
        "initial_equity_usdc": initial_capital_usdc,
        "final_equity_usdc": round(equity, 2),
        "total_return": total_return,
        "annualised_return_geometric": (
            (equity / initial_capital_usdc) ** (annual / count) - 1.0
            if equity > 0 else None
        ),
        "annualised_return_arithmetic": mean * annual,
        "annualised_volatility": sigma * root,
        "daily_mean_return": mean,
        "daily_volatility": sigma,
        "downside_deviation": downside,
        "sharpe": sharpe,
        "sharpe_target": 2.0,
        "sharpe_standard_error": sharpe_se,
        "sharpe_ci": (
            None if sharpe is None
            else [sharpe - 1.96 * sharpe_se, sharpe + 1.96 * sharpe_se]
        ),
        "days_to_separate_from_zero": days_needed,
        "sortino": sortino,
        # What the book said it would make, against what it made.  Not a
        # forecast score -- the claim is what sized every bet, so a ratio
        # far from 1.0 says the sizing input is wrong, not just the copy.
        "claimed_ev_usdc": claimed_ev_total,
        "total_staked_usdc": sum(row["staked_usdc"] for row in series),
        # (1 - cost) / cost: what a winning USDC pays back on top of itself.
        "odds_mean": (
            odds_weighted / sum(row["staked_usdc"] for row in series)
            if sum(row["staked_usdc"] for row in series) > 0 else None
        ),
        "cost_mean": (
            cost_weighted / sum(row["staked_usdc"] for row in series)
            if sum(row["staked_usdc"] for row in series) > 0 else None
        ),
        # The win rate those odds need just to break even, and what the
        # book actually hit -- the pair that says whether the price was
        # payable at all.
        "realised_win_rate_weighted": (
            hit_weighted / sum(row["staked_usdc"] for row in series)
            if sum(row["staked_usdc"] for row in series) > 0 else None
        ),
        "realised_win_rate": (hit_count / chain_count) if chain_count else None,
        "chain_count": chain_count,
        "breakeven_win_rate": (
            cost_weighted / sum(row["staked_usdc"] for row in series)
            if sum(row["staked_usdc"] for row in series) > 0 else None
        ),
        "claimed_ev_per_usdc": (
            claimed_ev_total / sum(row["staked_usdc"] for row in series)
            if sum(row["staked_usdc"] for row in series) > 0 else None
        ),
        "realised_pnl_usdc": equity - initial_capital_usdc,
        "ev_realisation_ratio": (
            (equity - initial_capital_usdc) / claimed_ev_total
            if claimed_ev_total > 0 else None
        ),
        "days_beating_claim": sum(
            1 for row in series
            if row["pnl_usdc"] >= row["claimed_ev_usdc"]
        ),
        # One ratio cannot answer this.  Money-weighted, a handful of big
        # losing days set the whole number: 0.055x aggregate against 0.343x
        # on the median day, a factor of six apart on the same fifteen days
        # (user challenge, 2026-08-30).  Both are published, plus the
        # trailing window, because they answer different questions -- what
        # happened to the money, what a typical day does, and where it has
        # been lately.  None of the three escapes the sample-size problem
        # the Sharpe tile states.
        "ev_realisation_daily_median": (
            statistics.median(daily_ratios) if daily_ratios else None
        ),
        "ev_realisation_daily_mean": (
            statistics.fmean(daily_ratios) if daily_ratios else None
        ),
        "ev_realisation_trailing_7d": trailing_7d,
        "ev_realisation_daily": daily_ratios,
        "tails": tails,
        "losing_days": len(losing),
        "mean_losing_day": (sum(losing) / len(losing)) if losing else None,
        "worst_day": ordered[0] if ordered else None,
        "worst_day_date": (
            next(
                (row["event_date"] for row in series
                 if abs(row["return"] - ordered[0]) < 1e-12),
                "",
            ) if ordered else ""
        ),
        "max_drawdown": max_drawdown,
        "max_drawdown_peak_day": peak_day,
        "max_drawdown_trough_day": trough_day,
        "trend_slope_usdc_per_day": slope,
        "trend_intercept_usdc": intercept,
        "trend_r_squared": (
            None if total_ss <= 0 else max(0.0, 1.0 - residual / total_ss)
        ),
        "series": series,
    }


# The three ways to average the same nine numbers.  Published together
# because they are three different questions and the book answers them
# differently enough that any one alone reads as the finding: on 2026-09-10
# the claim was 50.5% per chain, 63.5% per USDC and 49.8% per share.
#
# The share column is the one that closes.  Weighting each chain by the
# basket shares it bought makes cost-per-share equal total-money over
# total-shares and realised-EV-per-share equal total-PnL over total-shares,
# so their ratio IS the book's realised return -- the column cannot drift
# from the equity curve without one of them being wrong.  It is the only
# column the page draws (user ruling, 2026-09-13: 不要解释，用数据对比得出结论);
# the other two stay in the payload for the tests that pin them.


def _entry_economics_block(
    rows: list[tuple[Any, ...]],
) -> dict[str, Any] | None:
    """The nine numbers under all three weightings, for one set of chains.

    ``rows`` are settlement rows: stage, claim, hit, intended_hit, cost,
    stake, pnl, event_date.  A row with no recorded basket cost has no
    per-share anything, so it is dropped here and counted by the caller --
    counted rather than silently skipped, because a cost column that
    stopped being written would otherwise read as a smaller book.
    """

    # The same floor the rest of the board reads, from the module that
    # owns it.  A basket costing under a cent is a minimum-tick offer with
    # nothing bid against it -- the ledger itself calls those chains not
    # credible (``chain_credible = chain_cost >= DUST_COST_FLOOR``) and
    # every other section on the page drops them.  Keeping them here made
    # this section the only one counting 249 chains where the daily table
    # counts 239, and seven sub-cent misses moved the per-chain averages
    # more than their 0.25 USDC of stake could ever justify: cost 0.428 ->
    # 0.416, odds 1.34x -> 1.40x, and the plain per-chain odds mean from
    # 5.9x to 14.7x.
    priced = [
        row for row in rows
        if row[4] is not None and float(row[4]) >= DUST_COST_FLOOR
    ]
    if not priced:
        return None
    claim = [float(r[1]) for r in priced]
    hit = [float(r[2]) for r in priced]
    cost = [float(r[4]) for r in priced]
    stake = [float(r[5]) for r in priced]
    pnl = [float(r[6]) for r in priced]
    # The share unit is the basket share the ledger itself prices against:
    # stake / all-in cost, which is expected payout over claimed win
    # probability.  For a basket whose legs hold equal quantity it is the
    # contract count and a hit pays exactly 1.00 of it; for an uneven
    # basket it is a normalised unit and a hit pays a little less, which
    # the section states rather than rounding away.
    shares = [s / c for s, c in zip(stake, cost)]
    profit = [1.0 if p > 0 else 0.0 for p in pnl]
    per_share_pnl = [p / q for p, q in zip(pnl, shares)]
    claimed_edge = [q - c for q, c in zip(claim, cost)]

    def under(weights: list[float]) -> dict[str, Any] | None:
        total = sum(weights)
        if total <= 0:
            return None

        def w(values: list[float]) -> float:
            return sum(a * b for a, b in zip(weights, values)) / total

        def w_over(keep: list[bool], values: list[float]) -> float | None:
            picked = [
                (a, b) for a, b, k in zip(weights, values, keep) if k
            ]
            mass = sum(a for a, _ in picked)
            return (
                sum(a * b for a, b in picked) / mass if mass > 0 else None
            )

        cost_mean = w(cost)
        won = [p > 0 for p in pnl]
        lost = [p < 0 for p in pnl]
        return {
            "claimed_coverage": w(claim),
            "realised_coverage": w(hit),
            "profitable_rate": w(profit),
            # Payout is 1.00 a share, so the win rate that just pays for
            # the basket IS the price of the basket.  Same number as the
            # cost row below, deliberately: two labels, one quantity, and
            # the page says so instead of letting a reader wonder whether
            # they were computed apart and happen to agree.
            "breakeven_hit_rate": cost_mean,
            "cost_per_share": cost_mean,
            "net_odds": (
                (1.0 - cost_mean) / cost_mean if cost_mean > 0 else None
            ),
            "mean_win_per_share": w_over(won, per_share_pnl),
            "mean_loss_per_share": w_over(lost, per_share_pnl),
            "ev_per_share_claimed": w(claimed_edge),
            "ev_per_share_realised": w(per_share_pnl),
            "margin_over_breakeven": w(hit) - cost_mean,
            "realisation_ratio": (
                w(hit) / w(claim) if w(claim) > 0 else None
            ),
        }

    ones = [1.0] * len(priced)
    blocks = {
        "chain": under(ones),
        "stake": under(stake),
        "share": under(shares),
    }
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    naive_odds = [(1.0 - c) / c for c in cost]
    stake_total = sum(stake)
    hit_payout = sum(
        p + s for p, s, won in zip(pnl, stake, hit) if won > 0
    )
    hit_shares = sum(q for q, won in zip(shares, hit) if won > 0)
    return {
        "chains": len(priced),
        "weightings": blocks,
        # The per-chain money figures.  "平均盈利" in USDC is what a
        # winning chain returned, not a rate, so it has no weighted form
        # worth printing -- the weighted table carries the per-share
        # version of the same pair instead.
        "mean_win_usdc": (sum(wins) / len(wins)) if wins else None,
        "mean_loss_usdc": (sum(losses) / len(losses)) if losses else None,
        "win_count": len(wins),
        "loss_count": len(losses),
        "flat_count": sum(1 for p in pnl if p == 0),
        # A hit that did not pay, and a payer that did not hit.  Both are
        # zero today and the section prints the pair rather than asserting
        # "命中率就是盈利率": they come apart the moment a basket costs
        # more than the 1.00 it can win, and a reader who has been told
        # they are the same number would not look again.
        "hit_not_profitable": sum(
            1 for r, p in zip(priced, pnl) if float(r[2]) > 0 and p <= 0
        ),
        "profitable_not_hit": sum(
            1 for r, p in zip(priced, pnl) if float(r[2]) <= 0 and p > 0
        ),
        "intended_coverage": (
            sum(float(r[3]) for r in priced if r[3] is not None)
            / sum(1 for r in priced if r[3] is not None)
            if any(r[3] is not None for r in priced) else None
        ),
        # The average a reader would compute by hand, and why it is not on
        # a tile: (1-c)/c is convex, and 27 chains priced under five cents
        # drag the plain per-chain mean to 14.7x against a book whose
        # money actually bought 1.5x.  Published WITH its own maximum so
        # the number is visibly an artefact rather than a disagreement.
        "net_odds_chain_naive": (
            sum(naive_odds) / len(naive_odds) if naive_odds else None
        ),
        "net_odds_max": max(naive_odds) if naive_odds else None,
        "cheap_chains": sum(1 for c in cost if c < 0.05),
        "stake_usdc": stake_total,
        "shares": sum(shares),
        "pnl_usdc": sum(pnl),
        "roi": (sum(pnl) / stake_total) if stake_total > 0 else None,
        # What a hit actually pays for one of those shares.  Exactly 1.00
        # when the basket's legs hold equal quantity; a little under when
        # they do not, because the share is normalised on EXPECTED payout
        # and the winning leg may be the smaller one.  Printed so the
        # "每股" unit is not read as a contract it is not.
        "hit_pays_per_share": (
            hit_payout / hit_shares if hit_shares > 0 else None
        ),
    }


def _entry_economics_from_rows(
    rows: Sequence[Sequence[Any]],
) -> dict[str, Any]:
    """The nine numbers one entry is made of, off the settled book.

    ``rows`` are settlement rows -- stage, claim, hit, intended_hit, cost,
    stake, pnl, event_date -- from whichever book is being read.  Since the
    2026-09-16 ruling that is the live book (``live_book.settlement_rows``);
    the arithmetic is written once so the two books cannot drift apart.

    The board already carries most of these one at a time, scattered: the
    claim lives in the coverage section, the hit rate in the basket
    section, the odds and the break-even rate in the portfolio tiles.
    Read apart they answer nothing -- a 41% hit rate is a disaster against
    a 30-cent basket and a gift against a 50-cent one -- so this section
    puts the whole bet in one place: what the model said it covered, what
    it actually covered, what the coverage had to be to pay for itself,
    and what a share cost and returned.

    Every number here is per SETTLED CHAIN, one chain per station-day, and
    the coverage pair is the HELD basket both times: ``claimed_win_
    probability`` is the served probability summed over the buckets the
    chain ended up holding, and ``hit`` is whether the truth landed in one
    of those.  The chosen-basket hit rate is a different set and is
    published beside it, never in its place -- 2026-09-08 measured 60.7%
    chosen against 39.8% held, and the twenty-point gap is legs the
    ladder posted and the market never sold.
    """

    if not rows:
        # Distinguishable from a book that lost everything.
        return {"available": False, "reason": "还没有已结算的仓位",
                "chains": 0}
    overall = _entry_economics_block(rows)
    if overall is None:
        return {"available": False, "reason": "结算行里没有篮子成本",
                "chains": len(rows)}
    stages: list[dict[str, Any]] = []
    for stage in ("P0", "P0-M", "P1"):
        block = _entry_economics_block(
            [r for r in rows if str(r[0]) == stage]
        )
        if block is not None:
            stages.append({"stage": stage, **block})
    days = sorted(str(r[7]) for r in rows)
    return {
        "available": True,
        "first_day": days[0],
        "last_day": days[-1],
        "settled_rows": len(rows),
        "dust_cost_floor": DUST_COST_FLOOR,
        "cost_missing": sum(1 for r in rows if r[4] is None),
        "dust_excluded": sum(
            1 for r in rows
            if r[4] is not None and float(r[4]) < DUST_COST_FLOOR
        ),
        "dust_excluded_stake_usdc": sum(
            float(r[5]) for r in rows
            if r[4] is not None and float(r[4]) < DUST_COST_FLOOR
        ),
        # Published so the two totals on this page can be added back
        # together instead of read as a contradiction: the equity curve is
        # the whole account, every USDC that moved, and this section --
        # like the daily table and the coverage strip -- is the measurable
        # book.  The difference is exactly this.
        "dust_excluded_pnl_usdc": sum(
            float(r[6]) for r in rows
            if r[4] is not None and float(r[4]) < DUST_COST_FLOOR
        ),
        **overall,
        "by_stage": stages,
    }


def _live_runtime_config() -> tuple[Path, dict[str, Any]]:
    """The runtime contract the execution mode selects, and its path.

    The rule production itself follows (weather_basket_yes_daemon.py, and
    the budget read in production_worker.py): the live file in live mode,
    the no-live twin otherwise.  The go-live panel once read the no-live
    file unconditionally and reported live_submit_authorized=false about a
    system whose contract said true; naming the file read is what stops
    that repeating.
    """

    live_mode = _live_execution_mode() == "production_live"
    path = ROOT / "config/bayes_basket_target" / (
        "live_runtime_v1.json" if live_mode else "no_live_runtime_v1.json"
    )
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path, {}


def _live_bar_sides(
    rows: Sequence[Mapping[str, Any]],
    sides: Mapping[str, set[str]],
) -> dict[str, dict[str, Any]]:
    """The live money on each side of the station bar."""

    out: dict[str, dict[str, Any]] = {}
    for name, members in sides.items():
        picked = [r for r in rows if r["station_id"] in members]
        stake = sum(float(r["stake_usdc"]) for r in picked)
        pnl = sum(float(r["pnl_usdc"]) for r in picked)
        out[name] = {
            "stations": len({r["station_id"] for r in picked}),
            "settled_days": len({
                (r["station_id"], r["event_date"]) for r in picked
            }),
            "staked_usdc": stake,
            "pnl_usdc": pnl,
            "roi": (pnl / stake) if stake else None,
        }
    return out


def collect_live_money(conn: sqlite3.Connection) -> dict[str, Any]:
    """The money board, off the live book (user ruling, 2026-09-16).

    The paper ledger is a rule comparator from that day: it answers whether
    another rule would do better or worse, and it stopped being the account.
    So the board's 总账 / 对比 / 风险 / 曲线 read the live fills, through the
    same two pieces of arithmetic the paper book was read with.

    The capital base is the live budget knob, ``target_remaining_budget_usdc``
    in the runtime contract the execution mode selects (operator ruling, same
    day).  The account has no recorded balance history, so equity, return on
    capital, the annualised return and the percentage drawdown are all
    measured against the budget as it stands now -- a day traded under a
    larger budget reads as a larger return than it was, and the board says
    which number it used.
    """

    book = live_book.build(conn)
    rows = book["settlements"]
    runtime_path, runtime = _live_runtime_config()
    try:
        capital = float(runtime.get("target_remaining_budget_usdc"))
    except (TypeError, ValueError):
        capital = None
    pending_legs = sum(
        1 for leg in book["legs"]
        if leg["filled_size"] > 0
        and (leg["station_id"], leg["event_date"]) not in book["settled"]
    )
    economics = _entry_economics_from_rows([
        (r["stage"], r["claimed_win_probability"], r["hit"],
         r["intended_hit"], r["basket_all_in_cost"], r["stake_usdc"],
         r["pnl_usdc"], r["event_date"])
        for r in rows
    ])
    by_day: dict[str, list[float]] = {}
    for r in rows:
        acc = by_day.setdefault(r["event_date"], [0.0] * 8)
        stake = float(r["stake_usdc"])
        cost = r["basket_all_in_cost"]
        claim = r["claimed_win_probability"]
        acc[0] += stake
        acc[1] += float(r["pnl_usdc"])
        # The paper query's CASE WHEN cost > 0, row for row.
        if cost is not None and cost > 0 and claim is not None:
            acc[2] += stake * (claim / cost - 1.0)
        if cost is not None and cost > 0:
            acc[3] += stake * (1.0 - cost) / cost
        if cost is not None:
            acc[4] += stake * cost
        acc[5] += stake * int(r["hit"])
        acc[6] += 1
        acc[7] += int(r["hit"])
    days = [
        (day, acc[0], acc[1], acc[2], acc[3], acc[4], acc[5],
         int(acc[6]), int(acc[7]))
        for day, acc in sorted(by_day.items())
    ]
    if capital is None or capital <= 0:
        risk = {"available": False, "days": len(days),
                "reason": "预算旋钮读不出来，没有本金基数"}
    else:
        risk = _risk_metrics_from_days(days, capital)
    scope = _live_scope()
    return {
        "available": bool(rows),
        "reason": "" if rows else "还没有已结算的实盘成交",
        "capital_usdc": capital,
        "capital_source": runtime_path.name,
        "settled_rows": len(rows),
        "pending_legs": pending_legs,
        "economics": economics,
        "risk": risk,
        "bar_split": _live_bar_sides(rows, {
            "live": set(scope["stations"]),
            "probation": set(scope["probation"]),
            "barred": set(scope.get("barred") or barred_station_set()),
        }),
    }


def collect_live_days(conn: sqlite3.Connection) -> dict[str, Any]:
    """每日统计 and 逐站逐日明细, off the live book (user ruling, 2026-09-16).

    Both tables used to read the paper ledger's seats; the ruling that made
    the ledger a rule comparator moved them to the live fills.  One pass
    builds both, so a day's row and that day's station rows count the same
    legs.

    Two grains, each named where it is used.  A *station-day* is one station
    on one event day, whatever stages it traded; the basket verdicts are
    read at that grain -- 持仓命中 when a filled leg sits on the settled
    bucket, 篮子命中 when any posted leg does, filled or not (user ruling,
    2026-09-14).  A *笔* is one stage of one station-day that filled, the
    settlement row the money board reads; the claim, the hit rate against it
    and the odds are read at that grain, so this table and the board's
    nine numbers agree.
    """

    book = live_book.build(conn)
    legs = book["legs"]
    rows_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in book["settlements"]:
        rows_by_day[row["event_date"]].append(row)
    legs_by_sd: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for leg in legs:
        legs_by_sd[(leg["station_id"], leg["event_date"])].append(leg)
    slugs = _live_market_slugs(conn, [
        {"station_id": station, "event_date": day}
        for station, day in legs_by_sd
    ])

    # The station-day verdicts are the live book's (``live_book.station_days``),
    # the same records the go-live table counts by station.
    roster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in live_book.station_days(book):
        station, day = entry["station_id"], entry["event_date"]
        roster[day].append({
            "station_id": station,
            "event_date": day,
            "station_label": STATION_LABELS.get(station, ""),
            "market_slug": slugs.get(f"{station}|{day}", ""),
            **{k: v for k, v in entry.items()
               if k not in ("station_id", "event_date")},
        })

    daily: list[dict[str, Any]] = []
    for day in sorted(roster, reverse=True):
        entries = roster[day]
        rows = rows_by_day.get(day, [])
        by_stage = {stage: 0 for stage in live_book.STAGES}
        for entry in entries:
            for stage, summary in entry["stages"].items():
                by_stage[stage] = by_stage.get(stage, 0) + summary["filled_legs"]
        settled_sd = [e for e in entries if e["settled_bucket"] is not None]
        held_sd = [e for e in settled_sd if e["filled_legs"]]
        claimed = [r for r in rows if r["claimed_win_probability"] is not None]
        costed = [r for r in rows if r["basket_all_in_cost"] is not None]
        settled_stake = sum(r["stake_usdc"] for r in rows)
        pnl = sum(r["pnl_usdc"] for r in rows)
        hit_rate = (
            sum(r["hit"] for r in claimed) / len(claimed) if claimed else None
        )
        claim_mean = (
            sum(r["claimed_win_probability"] for r in claimed) / len(claimed)
            if claimed else None
        )
        # The day's price, stake-weighted, then inverted: the mean of each
        # row's own (1-c)/c is convex and rewards the cheapest basket out of
        # all proportion to its money (14.7x against 1.53x, 2026-09-10).
        cost_stake = sum(r["stake_usdc"] for r in costed)
        cost_mean = (
            sum(r["stake_usdc"] * r["basket_all_in_cost"] for r in costed)
            / cost_stake if cost_stake > 0 else None
        )
        daily.append({
            "event_date": day,
            "stations_posted": len(entries),
            "stations_filled": sum(1 for e in entries if e["filled_legs"]),
            "baskets": len({
                leg["decision_id"] for e in entries
                for leg in legs_by_sd[(e["station_id"], day)]
            }),
            "posted_legs": sum(e["posted_legs"] for e in entries),
            "filled_legs": sum(e["filled_legs"] for e in entries),
            "filled_leg_by_stage": by_stage,
            "staked_usdc": sum(e["staked_usdc"] for e in entries),
            "settled_station_days": len(settled_sd),
            "open_station_days": len(entries) - len(settled_sd),
            "basket_hit_station_days": sum(1 for e in settled_sd if e["basket_hit"]),
            "held_station_days": len(held_sd),
            "held_hit_station_days": sum(1 for e in held_sd if e["held_hit"]),
            "settled_rows": len(rows),
            "hit_rate": hit_rate,
            "claimed_win_probability_mean": claim_mean,
            "calibration_ratio": (
                hit_rate / claim_mean if hit_rate is not None and claim_mean
                else None
            ),
            "odds_mean": (
                (1.0 - cost_mean) / cost_mean if cost_mean else None
            ),
            "settled_staked_usdc": settled_stake,
            "pnl_usdc": pnl if rows else None,
            "roi": (pnl / settled_stake) if settled_stake > 0 else None,
        })
    return {
        "available": bool(legs),
        "daily": daily,
        "roster": {day: roster[day] for day in sorted(roster, reverse=True)},
    }


def collect_tiebreak_lane(conn: sqlite3.Connection) -> dict[str, Any]:
    """「秤砣线」-- the tiebreak lane's own statistics (charter 146).

    User ruling 2026-09-18: 前端增加可自行调整每笔预算和单独的统计.  Three
    reads, each from where the lane itself writes: the lane switches and
    the stake from the operator file; every first decision the lane scored
    from ``tiebreak_lane_evaluation`` (fired or not, with the reason a fired
    one did not send); and the trades from the live book, restricted to the
    lane's own decision rows (``tiebreak_decision_*``), scored against the
    settled bucket exactly as every other live leg is.

    The acceptance the charter names is printed as three conditions, not a
    verdict: realised minus ask with a day-clustered lower bound above zero,
    both halves the same sign, and at least thirty event days.
    """

    lanes = _load_strategy_lanes()
    tiebreak = lanes["lanes"].get("tiebreak") or {}
    out: dict[str, Any] = {
        "available": True,
        "settings": {
            "enabled": tiebreak.get("enabled") is True,
            "stake_usdc": tiebreak.get("stake_usdc"),
            "min_market_share_on_model_mode": tiebreak.get(
                "min_market_share_on_model_mode"
            ),
            "first_decision_fresh_seconds": tiebreak.get(
                "first_decision_fresh_seconds"
            ),
            "source": lanes.get("source"),
            "updated_at_utc": lanes.get("updated_at_utc"),
        },
        "lanes": {
            name: bool(lane.get("enabled") is True)
            for name, lane in lanes["lanes"].items()
        },
        "evaluations": {"available": False},
        "trades": {"available": False},
    }
    has_eval = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table'"
        " AND name='tiebreak_lane_evaluation'"
    ).fetchone() is not None
    if has_eval:
        total, fired, days, first_at, last_at = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(fired),0), COUNT(DISTINCT event_date),"
            " MIN(evaluated_at_utc), MAX(evaluated_at_utc)"
            " FROM tiebreak_lane_evaluation"
        ).fetchone()
        refusals = [
            {"refusal": str(r[0] or "sent"), "count": int(r[1]), "fired": int(r[2] or 0)}
            for r in conn.execute(
                "SELECT COALESCE(refusal,''), COUNT(*), SUM(fired)"
                " FROM tiebreak_lane_evaluation GROUP BY COALESCE(refusal,'')"
                " ORDER BY COUNT(*) DESC LIMIT 14"
            )
        ]
        fired_rows = [
            {
                "station_id": r[0], "event_date": r[1], "stage": r[2],
                "model_mode": r[3], "market_mode": r[4],
                "market_share_on_model_mode": r[5],
                "model_mode_share": r[6], "refusal": r[7],
                "order_intent_id": r[8], "evaluated_at_utc": r[9],
            }
            for r in conn.execute(
                "SELECT station_id, event_date, stage, model_mode, market_mode,"
                " market_share_on_model_mode, model_mode_share, refusal,"
                " order_intent_id, evaluated_at_utc"
                " FROM tiebreak_lane_evaluation WHERE fired=1"
                " ORDER BY evaluated_at_utc DESC LIMIT 40"
            )
        ]
        by_day = [
            {"event_date": r[0], "evaluated": int(r[1]), "fired": int(r[2] or 0),
             "sent": int(r[3] or 0)}
            for r in conn.execute(
                "SELECT event_date, COUNT(*), SUM(fired),"
                " SUM(CASE WHEN fired=1 AND refusal IS NULL THEN 1 ELSE 0 END)"
                " FROM tiebreak_lane_evaluation GROUP BY event_date"
                " ORDER BY event_date DESC LIMIT 21"
            )
        ]
        out["evaluations"] = {
            "available": True, "total": int(total or 0), "fired": int(fired or 0),
            "event_days": int(days or 0), "first_at_utc": first_at,
            "last_at_utc": last_at, "refusals": refusals,
            "fired_rows": fired_rows, "by_day": by_day,
        }
    book = live_book.build(conn)
    settled = book.get("settled") or {}
    legs = [
        leg for leg in (book.get("legs") or [])
        if str(leg.get("decision_id") or "").startswith("tiebreak_decision_")
    ]
    trades: list[dict[str, Any]] = []
    for leg in legs:
        key = (leg["station_id"], leg["event_date"])
        landed = settled.get(key)
        filled = float(leg.get("filled_size") or 0.0)
        notional = float(leg.get("filled_notional_usdc") or 0.0)
        hit = (landed[0] == leg["bucket"]) if (landed and filled > 0) else None
        payout = filled if hit else (0.0 if hit is False else None)
        trades.append({
            "station_id": leg["station_id"], "event_date": leg["event_date"],
            "stage": leg.get("stage"), "bucket": leg["bucket"],
            "limit_price": leg.get("limit_price"),
            "requested_size": leg.get("requested_size"),
            "requested_notional_usdc": leg.get("requested_notional_usdc"),
            "filled_size": filled, "filled_notional_usdc": notional,
            "ask_paid": (notional / filled) if filled > 0 else None,
            "claimed_probability": leg.get("claimed_probability"),
            "state": leg.get("state"), "created_at": leg.get("created_at"),
            "settled_bucket": landed[0] if landed else None,
            "hit": hit, "payout_usdc": payout,
            "pnl_usdc": (payout - notional) if payout is not None else None,
        })
    trades.sort(key=lambda t: (t["event_date"], t["station_id"]), reverse=True)
    filled_rows = [t for t in trades if t["filled_size"] > 0]
    settled_rows = [t for t in filled_rows if t["hit"] is not None]
    stake = sum(t["filled_notional_usdc"] for t in settled_rows)
    payout = sum(t["payout_usdc"] or 0.0 for t in settled_rows)
    shares = sum(t["filled_size"] for t in settled_rows)
    hits = sum(1 for t in settled_rows if t["hit"])
    avg_ask = (stake / shares) if shares > 0 else None
    hit_rate = (hits / len(settled_rows)) if settled_rows else None
    edge_rows = [
        ((1.0 if t["hit"] else 0.0) - (t["ask_paid"] or 0.0), t["event_date"])
        for t in settled_rows if t["ask_paid"] is not None
    ]
    edge = (sum(e for e, _d in edge_rows) / len(edge_rows)) if edge_rows else None
    ci = None
    event_days = sorted({t["event_date"] for t in settled_rows})
    if len(edge_rows) >= 5 and len(event_days) >= 3:
        import random as _random  # noqa: PLC0415

        rng = _random.Random(146)
        by_day_edges: dict[str, list[float]] = defaultdict(list)
        for e, d in edge_rows:
            by_day_edges[d].append(e)
        days_list = list(by_day_edges)
        draws: list[float] = []
        for _ in range(500):
            picked = [rng.choice(days_list) for _ in days_list]
            pool = [e for d in picked for e in by_day_edges[d]]
            draws.append(sum(pool) / len(pool))
        draws.sort()
        ci = [draws[int(0.05 * len(draws))], draws[int(0.95 * len(draws))]]
    halves = None
    if len(event_days) >= 2:
        cut = event_days[len(event_days) // 2]
        first = [e for e, d in edge_rows if d < cut]
        second = [e for e, d in edge_rows if d >= cut]
        halves = {
            "split_event_date": cut,
            "first": (sum(first) / len(first)) if first else None,
            "second": (sum(second) / len(second)) if second else None,
        }
    out["trades"] = {
        "available": True,
        "sent": len(trades), "filled": len(filled_rows),
        "settled": len(settled_rows), "hits": hits,
        "open": len(filled_rows) - len(settled_rows),
        "stake_usdc": stake, "payout_usdc": payout, "pnl_usdc": payout - stake,
        "roi": ((payout - stake) / stake) if stake > 0 else None,
        "avg_ask": avg_ask, "hit_rate": hit_rate,
        "realised_minus_ask": edge, "realised_minus_ask_ci90": ci,
        "halves": halves, "event_days": len(event_days),
        "acceptance": {
            "lower_bound_positive": bool(ci and ci[0] > 0),
            "halves_same_sign": bool(
                halves and halves["first"] is not None and halves["second"] is not None
                and (halves["first"] > 0) == (halves["second"] > 0)
            ),
            "event_days": len(event_days), "event_days_needed": 30,
        },
        "rows": trades[:60],
    }
    return out


def collect_convergence_claim(conn: sqlite3.Connection) -> dict[str, Any]:
    """Arm B (2026-09-06): the convergence claim lift, in shadow.

    Two halves.  The receipts are production's own: every basket decision
    row carries the enumeration's ``convergence_claim`` receipt -- how many
    windows the price gate judged were converging, how many the lift would
    have admitted -- read straight off ``auto_trade_decisions``.  The
    evidence is the measurement the arm rests on, recomputed on this book
    with the kernel's own state function and the same evidence loader
    production hands in: at sampled P0-M/P1 checkpoints of settled
    station-days, the max-mass three-rung window (a stand-in for the seat,
    not the seat rule) is classified converging or not and scored against
    truth.  Nothing here re-derives the rule; the rule is called.
    """

    from bayes_basket_target.basket_kernel import delta_v2 as K
    from bayes_basket_target.convergence_evidence import (
        best_asks,
        load_convergence_evidence,
        window_sum,
    )

    out: dict[str, Any] = {
        "mode": K.CONVERGENCE_CLAIM_MODE,
        "factor": K.CONVERGENCE_CLAIM_FACTOR,
        "horizon_hours": K.CONVERGENCE_CLAIM_HORIZON_HOURS,
        "stages": sorted(K.CONVERGENCE_CLAIM_STAGES),
        "min_agreement": K.CONVERGENCE_CLAIM_MIN_AGREEMENT,
        "min_claim_rise": K.CONVERGENCE_CLAIM_MIN_CLAIM_RISE,
        "min_price_fall": K.CONVERGENCE_CLAIM_MIN_PRICE_FALL,
        "receipts": [],
        "examples": [],
        "evidence": [],
        "evidence_days": 14,
    }

    # --- receipts: production's decisions over the last seven days ---
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    tally: dict[str, dict[str, int]] = {}
    examples: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT station_id, event_date, created_at, details_json"
            " FROM auto_trade_decisions"
            " WHERE strategy_lane='basket' AND created_at>=?"
            " ORDER BY created_at DESC LIMIT 6000",
            (since,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for station_id, event_date, created_at, details_json in rows:
        try:
            details = json.loads(details_json or "{}")
        except (TypeError, ValueError):
            continue
        receipt = details.get("convergence_claim")
        if not isinstance(receipt, Mapping) or not receipt:
            continue
        signature = details.get("decision_signature") or {}
        stage = str(signature.get("stage") or "?")
        slot = tally.setdefault(stage, {
            "decisions": 0, "with_evidence": 0, "candidates": 0,
            "would_admit": 0, "admitted_by_lift": 0,
        })
        slot["decisions"] += 1
        slot["with_evidence"] += int(bool(receipt.get("evidence_present")))
        slot["candidates"] += int(receipt.get("candidate_count") or 0)
        slot["would_admit"] += int(receipt.get("would_admit_count") or 0)
        slot["admitted_by_lift"] += int(receipt.get("admitted_by_lift_count") or 0)
        if len(examples) < 12:
            for row in receipt.get("candidates") or []:
                if len(examples) >= 12:
                    break
                examples.append({
                    "station_id": str(station_id), "event_date": str(event_date),
                    "stage": stage, "at": str(created_at)[:16],
                    "buckets": list(row.get("buckets") or []),
                    "claim": row.get("claim"), "price": row.get("all_in_price"),
                    "agreement": row.get("agreement"),
                    "claim_rise": row.get("claim_rise"),
                    "price_fall": row.get("price_fall"),
                    "lifted_edge": row.get("lifted_edge"),
                    "flips_gate": bool(row.get("flips_gate")),
                })
    out["receipts"] = [{"stage": stage, **slot} for stage, slot in sorted(tally.items())]
    out["examples"] = examples

    # --- evidence: settled station-days, sampled P0-M/P1 checkpoints ---
    try:
        truth: dict[tuple[str, str], float] = {}
        floor_day = (datetime.now(timezone.utc) - timedelta(days=out["evidence_days"])).date().isoformat()
        for st, ld, t in conn.execute(
            "SELECT station_id, local_date, official_tmax_c"
            " FROM calibration_station_daily_high_truth"
            " WHERE official_tmax_c IS NOT NULL AND local_date>=?"
            " ORDER BY revision_no",
            (floor_day,),
        ):
            truth[(str(st), str(ld))] = float(t)
    except sqlite3.Error:
        truth = {}
    cells: dict[str, dict[str, Any]] = {}

    def _cell(name: str) -> dict[str, Any]:
        return cells.setdefault(name, {"n": 0, "hits": 0, "claim": 0.0, "unit": 0.0, "stations": set()})

    for (st, ed), t_true in sorted(truth.items()):
        try:
            cps = conn.execute(
                "SELECT history_id, stage, decision_at_utc"
                " FROM basket_probability_bridge_history"
                " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                "   AND stage IN ('P0-M','P1') ORDER BY decision_at_utc",
                (st, ed),
            ).fetchall()
        except sqlite3.Error:
            break
        if not cps:
            continue
        last_slot = None
        for history_id, stage, decided in cps:
            slot = str(decided)[:13]  # one sample per station-day-hour
            hour = int(str(decided)[11:13] or 0)
            if slot == last_slot or hour % 4:
                continue
            last_slot = slot
            try:
                buckets = sorted(conn.execute(
                    "SELECT ordinal, bucket, lower_c, upper_c, probability"
                    " FROM basket_probability_bridge_bucket_probability WHERE history_id=?",
                    (history_id,),
                ).fetchall())
                book = conn.execute(
                    "SELECT buckets_json FROM basket_official_ws_snapshot"
                    " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                    "   AND observed_at>=? ORDER BY observed_at ASC LIMIT 1",
                    (st, ed, str(decided)),
                ).fetchone()
            except sqlite3.Error:
                break
            if len(buckets) < 3 or book is None:
                continue
            best, best_i = -1.0, 0
            for i in range(len(buckets) - 2):
                mass = sum(float(b[4]) for b in buckets[i:i + 3])
                if mass > best:
                    best, best_i = mass, i
            run = buckets[best_i:best_i + 3]
            names = [str(b[1]) for b in run]
            asks = best_asks(book[0])
            price_now = window_sum(asks, names)
            if price_now is None or best <= 0:
                continue
            evidence = load_convergence_evidence(
                conn, station_id=st, event_date=ed, as_of_utc=str(decided),
            )
            state = K.convergence_state_v1(
                stage=str(stage), claim_now=best,
                claim_prev=window_sum((evidence or {}).get("prior_probabilities"), names),
                price_now=price_now,
                price_prev=window_sum((evidence or {}).get("prior_asks"), names),
                agreement_now=price_now / best,
            )
            lo, hi = run[0][2], run[-1][3]
            hit = int((lo is None or t_true >= lo) and (hi is None or t_true < hi))
            if state["converging"]:
                name = "双向收敛（会被抬价）"
            elif state["reason"] == "claim_not_rising" and (state.get("price_fall") or 0) >= K.CONVERGENCE_CLAIM_MIN_PRICE_FALL:
                name = "模型没动、市场跌（不抬）"
            elif state["reason"] == "no_prior":
                name = "无先验"
            elif state["reason"] == "not_dear":
                name = "不贵（a<1.1）"
            else:
                name = "贵但未收敛"
            cell = _cell(f"{stage} / {name}")
            cell["n"] += 1
            cell["hits"] += hit
            cell["claim"] += best
            cell["unit"] += (1.0 / price_now - 1.0) if hit else -1.0
            cell["stations"].add(st)
    out["evidence"] = [
        {
            "cell": name, "n": c["n"], "stations": len(c["stations"]),
            "hit_pct": round(100.0 * c["hits"] / c["n"], 1) if c["n"] else None,
            "claimed_pct": round(100.0 * c["claim"] / c["n"], 1) if c["n"] else None,
            "realised_over_claimed": round(c["hits"] / c["claim"], 2) if c["claim"] else None,
            "unit_return_pct": round(100.0 * c["unit"] / c["n"], 1) if c["n"] else None,
        }
        for name, c in sorted(cells.items())
    ]
    return out


def collect_market_lean(conn: sqlite3.Connection) -> dict[str, Any]:
    """Charter 118 (一): the market-lean gate's own record.

    The gate reads one number off the book -- the mid-weighted mean bucket
    the quotes imply -- and compares it with the model's mean bucket; a
    window sitting on the far side of the model from the book's lean is
    refused.  It is a DIRECTION rule on window placement, not a price rule,
    and it never creates a buy.

    Everything here is production's own receipt (``details.market_lean`` on
    ``auto_trade_decisions``), not a re-derivation: the mode the decision
    ran under, the lean it saw, how many windows it refused, and -- while
    the gate sits in shadow -- how many it WOULD have refused.  Rows written
    before the receipt landed carry nothing, so the tally starts at the
    build that shipped it rather than pretending to reach back.
    """

    from bayes_basket_target import delta_v2_runtime as R

    out: dict[str, Any] = {
        "mode": R.MARKET_LEAN_GATE_MODE,
        "min_buckets": R.MARKET_LEAN_MIN_BUCKETS,
        "min_quoted_mass": R.MARKET_LEAN_MIN_QUOTED_MASS,
        "record_max": R.MARKET_LEAN_REFUSAL_RECORD_MAX,
        "receipts": [],
        "examples": [],
        "days": 7,
    }
    since = (
        datetime.now(timezone.utc) - timedelta(days=out["days"])
    ).isoformat(timespec="seconds")
    tally: dict[str, dict[str, int]] = {}
    examples: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT station_id, event_date, created_at, details_json"
            " FROM auto_trade_decisions"
            " WHERE strategy_lane='basket' AND created_at>=?"
            " ORDER BY created_at DESC LIMIT 6000",
            (since,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for station_id, event_date, created_at, details_json in rows:
        try:
            details = json.loads(details_json or "{}")
        except (TypeError, ValueError):
            continue
        receipt = details.get("market_lean")
        if not isinstance(receipt, Mapping) or not receipt:
            continue
        signature = details.get("decision_signature") or {}
        stage = str(signature.get("stage") or "?")
        slot = tally.setdefault(stage, {
            "decisions": 0, "with_lean": 0, "refused": 0, "would_refuse": 0,
        })
        slot["decisions"] += 1
        slot["with_lean"] += int(receipt.get("lean_ordinal") is not None)
        slot["refused"] += int(receipt.get("refused_window_count") or 0)
        slot["would_refuse"] += int(
            receipt.get("shadow_refused_window_count") or 0
        )
        for window in receipt.get("refused_windows") or []:
            if len(examples) >= 12:
                break
            examples.append({
                "station_id": str(station_id), "event_date": str(event_date),
                "stage": stage, "at": str(created_at)[:16],
                "buckets": list(window.get("buckets") or []),
                "window_mid_ordinal": window.get("window_mid_ordinal"),
                "model_mean_ordinal": window.get("model_mean_ordinal"),
                "lean": window.get("lean"),
                "enforced": bool(window.get("enforced")),
            })
    out["receipts"] = [
        {"stage": stage, **slot} for stage, slot in sorted(tally.items())
    ]
    out["examples"] = examples
    return out


def collect_effective_edge_lambda(conn: sqlite3.Connection) -> dict[str, Any]:
    """What fraction of a claim arrives, per stage and per agreement band.

    Charter 68 has the objective read ``lambda(a) * p``, where a = price /
    claim is how far the book agrees with a basket.  P0 and P0-M have carried
    a fitted table since 2026-09-01; P1 got one of its own on 2026-09-02, and
    the point of showing them side by side is that they are opposite shapes.
    P0's is an inverted U -- where the market disagrees most, five sixths of
    the claim never arrives -- and P1's is monotone at or above one, because
    P1's edge is the observation rather than the shape and the observation is
    right exactly where the market doubts it.

    Three columns per band and they answer different questions.  ``served`` is
    the constant the compile chain is running right now.  ``raw`` is what this
    book says that constant should be, recomputed here on the same rule the
    store's rolling fit uses, so a gap between the two IS the promotion
    backlog.  The early/late split is the only thing that decides whether a
    gap may be closed: a band whose two halves disagree is a band that has not
    earned a constant yet, and the 0.70-0.85 P1 band -- 0.54 early, 0.92 late
    -- is exactly that case.
    """

    from bayes_basket_target import effective_edge_store as _store
    from bayes_basket_target.basket_kernel.delta_v2 import (
        EFFECTIVE_EDGE_LAMBDA,
        EFFECTIVE_EDGE_P0M_LAMBDA,
        EFFECTIVE_EDGE_P0M_STAGES,
        EFFECTIVE_EDGE_P1_LAMBDA,
        EFFECTIVE_EDGE_P1_STAGES,
        EFFECTIVE_EDGE_STAGES,
    )
    from bayes_basket_target.delta_v2_runtime import EFFECTIVE_EDGE_MODE
    from bayes_basket_target.basket_kernel.delta_v2 import (
        CLAIM_EDGE_THERMOMETER_MODE,
        CLAIM_EDGE_THIN_BAND_LAMBDA,
        CLAIM_EDGE_THIN_BAND_MAX,
        EFFECTIVE_EDGE_BAND_BOUNDS,
    )

    def band_label(bound: float, lower: float) -> str:
        # Charter 96 moved the top boundary twice; a hardcoded "≥0.85" here
        # went on printing the retired partition after the split.  Derive it.
        return f"≥{lower:g}" if bound == float("inf") else f"<{bound:g}"

    def admits(lower: float, upper: float, lam: float) -> list[float] | None:
        """What this band can actually buy.

        Charter 96: the gate reduces to ``lambda(a) > a``, so a band admits
        exactly ``[its lower bound, its own lambda)`` -- its served value is
        its own cut point, and a band whose lambda falls under its floor buys
        nothing at all no matter how right the model is.  Showing the served
        number without this interval is what let two bands sit blind on the
        page for days looking like ordinary rows.
        """

        if lam <= lower:
            return None
        return [round(lower, 4), round(min(lam, upper), 4)]

    def measure(stages: tuple[str, ...], served, since: str | None,
                until: str | None) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in stages)
        clause = ""
        params: list[Any] = list(stages)
        if since:
            clause += " AND c.event_date >= ?"
            params.append(since)
        if until:
            clause += " AND c.event_date < ?"
            params.append(until)
        try:
            rows = conn.execute(
                # Every candidate window, which is the population the
                # served table is fitted on -- a panel that reads a
                # different sample than the fit is a panel that cannot be
                # used to check the fit.  ``win_probability`` here is
                # already the lifted claim; nothing lifts it again.
                #
                # P&L and stake come from the seats and only from the
                # seats: money follows the basket that was held, and a
                # window nobody bought has none.  The two halves of this
                # row answer two different questions on purpose, and the
                # money half is NULL for most rows.
                #
                # Joined per station-day-and-stage, not per station-day.  A
                # chain seats at P0 and is topped up at P0-M and P1, and a
                # station-day join handed the same settlement to each of
                # those seated candidates: 53 of them, carrying the chain's
                # whole P&L and stake once per stage.  That put 548 USDC of
                # stake and -373 of P&L on this panel against a ledger
                # holding 414 and -257, and moved the band returns with it
                # (measured 2026-09-08).  The settlement names the stage
                # that closed the chain; that seat, and only it, is where
                # the chain's money belongs -- the same one-book rule the
                # rest of the page reads.
                # Charter 140: the lane's own population, production's
                # evidence.  The money half joined the paper settlement of
                # the ledger's seat; production evidence has no paper seat,
                # so that half reads nothing rather than another book's P&L.
                "SELECT c.win_probability, c.all_in_price, c.hit,"
                " NULL, NULL, c.station_id, c.event_date"
                f" FROM {_store.CANDIDATE_SOURCE} c"
                f" WHERE c.stage IN ({placeholders}){clause}"
                "   AND c.hit IS NOT NULL",
                params,
            ).fetchall()
        except sqlite3.Error:
            return []
        tally = [
            {"seats": 0, "claim": 0.0, "hits": 0, "pnl": 0.0, "stake": 0.0,
             "station_days": set(), "event_days": set()}
            for _ in served
        ]
        for claim, cost, hit, pnl, stake, station, day in rows:
            if claim is None or cost is None or hit is None:
                continue
            claimed = float(claim or 0.0)
            price = float(cost or 0.0)
            if claimed <= 0.0 or price <= 0.0:
                continue
            index = _store._band_index(price / claimed, served)
            slot = tally[index]
            slot["seats"] += 1
            slot["station_days"].add((station, day))
            slot["event_days"].add(day)
            slot["claim"] += claimed
            slot["hits"] += int(hit or 0)
            slot["pnl"] += float(pnl or 0.0)
            slot["stake"] += float(stake or 0.0)
        out = []
        lower_bound = 0.0
        for (bound, lam), slot in zip(served, tally):
            raw = (slot["hits"] / slot["claim"]) if slot["claim"] > 0 else None
            out.append({
                "band": band_label(bound, lower_bound),
                "lower": round(lower_bound, 4),
                "upper": None if bound == float("inf") else round(bound, 4),
                "admits": admits(lower_bound, float(bound), float(lam)),
                "served": round(float(lam), 4),
                "seats": slot["seats"],
                "station_days": len(slot["station_days"]),
                "event_days": len(slot["event_days"]),
                "claim_mass": round(slot["claim"], 2),
                "hits": slot["hits"],
                "raw_lambda": None if raw is None else round(raw, 4),
                "return": (
                    None if slot["stake"] <= 0
                    else round(slot["pnl"] / slot["stake"], 4)
                ),
                # The store's four hurdles, all of them: a band the page
                # marked readable on seats and mass alone could still be one
                # the fit keeps at its incumbent for want of station-days or
                # of separate weather.
                "admissible": (
                    slot["seats"] >= _store.MIN_SEATS_PER_BAND
                    and len(slot["station_days"])
                    >= _store.MIN_STATION_DAYS_PER_BAND
                    and len(slot["event_days"])
                    >= _store.MIN_EVENT_DAYS_PER_BAND
                    and slot["claim"] >= _store.MIN_CLAIM_MASS
                ),
            })
            if bound != float("inf"):
                lower_bound = float(bound)
        return out

    def activated(scope: str) -> dict[str, Any] | None:
        # The window table: the one production serves.  A stage also carries
        # an activated bucket (research) table, and "newest fitted" picked
        # whichever of the two refit last (2026-09-14).
        from bayes_basket_target.effective_edge_store import (  # noqa: PLC0415
            _claim_scope_args,
            _claim_scope_clause,
        )

        try:
            row = conn.execute(
                "SELECT lambda_table_sha256, fitted_at_utc, seat_count,"
                " evidence_json FROM calibration_effective_edge_lambda"
                " WHERE activated=1 AND stage_scope=?"
                + _claim_scope_clause(conn, "window")
                + " ORDER BY fitted_at_utc DESC LIMIT 1",
                (scope, *_claim_scope_args(conn, "window")),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            evidence = json.loads(str(row[3]))
        except ValueError:
            evidence = []
        # Two shapes live in this column.  The store wrapped the band list
        # in {"bands": [...], "activation_guard": {...}} when the guard was
        # added; rows written before that are still the bare list, and P0's
        # activated row is one of them.  Iterating the wrapper yielded its
        # keys -- strings -- and the render died on
        # ``'str' object has no attribute 'get'`` after 516 seconds of work,
        # every render, producing nothing.  Read whichever shape is there.
        if isinstance(evidence, Mapping):
            evidence = evidence.get("bands") or []
        if not isinstance(evidence, list):
            evidence = []
        bands = [item for item in evidence if isinstance(item, Mapping)]
        return {
            "sha256": str(row[0])[:12],
            "fitted_at_utc": str(row[1]),
            "seat_count": int(row[2] or 0),
            "blind_bands": [
                item.get("band_upper_bound") for item in bands
                if item.get("accumulating") and not item.get("seats")
            ],
        }

    try:
        days = [
            str(row[0]) for row in conn.execute(
                f"SELECT DISTINCT event_date FROM {_store.CANDIDATE_SOURCE}"
                " WHERE hit IS NOT NULL ORDER BY event_date"
            )
        ]
    except sqlite3.Error:
        days = []
    midpoint = days[len(days) // 2] if days else None

    from bayes_basket_target import candidate_window_evidence as _evidence

    try:
        # Rowid order is insertion order on this append-only table, so the
        # first row is one seek rather than a scan of a created_at column
        # nothing indexes.
        first = conn.execute(
            f"SELECT created_at_utc FROM {_evidence.EVIDENCE_TABLE}"
            " ORDER BY rowid LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        first = None
    first_written_at_utc = str(first[0]) if first else None

    def evidence_status(stages: tuple[str, ...]) -> dict[str, Any] | None:
        """Why a band column is blank, in numbers the reader can check.

        Charter 140 moved the population from the paper table, which held
        history back to 2026-08-15, onto production's own record, which
        starts on the day it shipped.  Every column but the served one reads
        settled rows, so for the first days after the move the whole table
        is dashes -- the same page a broken query would print.  This says
        which it is: how many windows are written, how many have a label,
        and the earliest event day still waiting for one.
        """

        placeholders = ",".join("?" for _ in stages)
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(c.hit IS NOT NULL), 0),"
                " MIN(CASE WHEN c.hit IS NULL THEN c.event_date END),"
                " MAX(CASE WHEN c.hit IS NOT NULL THEN c.event_date END)"
                f" FROM {_store.CANDIDATE_SOURCE} c"
                f" WHERE c.stage IN ({placeholders})",
                stages,
            ).fetchone()
        except sqlite3.Error:
            return None
        return {
            "source": _evidence.EVIDENCE_SOURCE,
            "relation": _store.CANDIDATE_SOURCE,
            "first_written_at_utc": (
                first_written_at_utc
                if _evidence.EVIDENCE_SOURCE == "production" else None
            ),
            "windows": int(row[0] or 0),
            "labelled": int(row[1] or 0),
            "earliest_pending_event_date": row[2],
            "latest_labelled_event_date": row[3],
            "min_seats": _store.MIN_SEATS_PER_BAND,
            "min_station_days": _store.MIN_STATION_DAYS_PER_BAND,
            "min_event_days": _store.MIN_EVENT_DAYS_PER_BAND,
            "min_claim_mass": _store.MIN_CLAIM_MASS,
        }

    def serving(scope: str, bootstrap) -> tuple[tuple[float, float], ...]:
        """The table production is actually serving for this scope.

        Charter 68(c) (2026-09-05) made the rolling fit promote itself, so the
        kernel constant became the bootstrap and nothing more -- but this
        section went on reading the constant and calling the column "the
        constant the compile chain is running right now".  It was wrong for a
        day: on 2026-09-06 the page printed P0's top band as 0.82 while the
        store served 1.0112, and the split at 0.80 that opened a band did not
        appear at all.  Read what the dispatch reads, and fall back to the
        constant on exactly its own condition -- no activation yet.
        """

        active = _store.load_active_lambda(conn, stage_scope=scope)
        return tuple(active) if active else tuple(bootstrap)

    scopes = []
    for scope, stages, served, ceiling in (
        ("P0", tuple(sorted(EFFECTIVE_EDGE_STAGES)),
         serving("P0", EFFECTIVE_EDGE_LAMBDA), _store.LAMBDA_CEILING),
        ("P0-M", tuple(sorted(EFFECTIVE_EDGE_P0M_STAGES)),
         serving("P0-M", EFFECTIVE_EDGE_P0M_LAMBDA), _store.LAMBDA_CEILING),
        ("P1", tuple(sorted(EFFECTIVE_EDGE_P1_STAGES)),
         serving("P1", EFFECTIVE_EDGE_P1_LAMBDA), _store.P1_LAMBDA_CEILING),
    ):
        whole = measure(stages, served, None, None)
        early = measure(stages, served, None, midpoint)
        late = measure(stages, served, midpoint, None)
        for index, band in enumerate(whole):
            band["early_lambda"] = (
                early[index]["raw_lambda"] if index < len(early) else None
            )
            band["late_lambda"] = (
                late[index]["raw_lambda"] if index < len(late) else None
            )
            band["early_seats"] = (
                early[index]["seats"] if index < len(early) else 0
            )
            # A band may be promoted when this book says something different
            # from the constant, the evidence clears the store's own hurdle,
            # and both halves of the window agree.  Anything less is a number
            # waiting for its second half.
            capped = None if band["raw_lambda"] is None else min(
                float(ceiling), band["raw_lambda"]
            )
            band["fitted_capped"] = (
                None if capped is None else round(capped, 4)
            )
            stable = (
                band["early_lambda"] is not None
                and band["late_lambda"] is not None
                and band["early_seats"] >= _store.MIN_SEATS_PER_BAND
                and abs(band["early_lambda"] - band["late_lambda"]) <= 0.25
            )
            band["candidate"] = bool(
                capped is not None
                and band["admissible"]
                and abs(capped - band["served"]) > 0.02
            )
            band["stable"] = bool(stable)
        scopes.append({
            "scope": scope,
            "stages": list(stages),
            "ceiling": float(ceiling),
            "bands": whole,
            "evidence": evidence_status(stages),
            "store": activated(scope),
            # Shadow axis (2026-09-06): the same bands split by which way the
            # market moved the window in the hours before the seat.  Read
            # from the shadow journal the daily refit writes; served by nothing.
            "dq_shadow": _store.load_dq_shadow(conn, stage_scope=scope),
        })
    return {
        "mode": EFFECTIVE_EDGE_MODE,
        # Charter 96/97: the partition is a rule in the kernel, and the
        # claim-edge axis is a second one.  Both belong on the page, since a
        # reader cannot otherwise tell why a band with a healthy lambda still
        # buys nothing, or why a thin-edge window was bought at all.
        "band_bounds": [
            None if b == float("inf") else float(b)
            for b in EFFECTIVE_EDGE_BAND_BOUNDS
        ],
        "claim_edge_thermometer": CLAIM_EDGE_THERMOMETER_MODE,
        "claim_edge_thin_band_max": CLAIM_EDGE_THIN_BAND_MAX,
        "claim_edge_thin_band_lambda": CLAIM_EDGE_THIN_BAND_LAMBDA,
        "dq_shadow_mode": _store.DQ_SHADOW_MODE,
        "dq_shadow_horizon_hours": _store.DQ_SHADOW_HORIZON_HOURS,
        "dq_shadow_threshold": _store.DQ_SHADOW_THRESHOLD,
        "minimum_seats_per_band": _store.MIN_SEATS_PER_BAND,
        "split_at_event_date": midpoint,
        "scopes": scopes,
    }


def collect_beta_shadow(conn: sqlite3.Connection) -> dict[str, Any]:
    """Charter 128: what the beta lane serves, and what its gate would have
    done -- read off the ledger's own settled windows, not off a replay.

    Three readings, three sources, deliberately kept apart.  ``served`` is
    the store's active row per stage (the number the enumeration reads).
    ``replay`` re-judges every settled candidate window of the last 14 days
    with the beta the lane was serving at that window's decision instant
    (journal as-of, like lambda) against the verdict production recorded on
    the row, and prints what each of the four cells then realised -- that
    table is the promotion evidence charter 128 asks for.  ``research`` is
    the unanchored 28-day fit, which is what the slope looks like before the
    anchor is respected and is never served.  ``live`` is the shadow's own
    counters off the last two days of decisions, so a reader can see the
    lane is being written at all.
    """

    from bayes_basket_target import beta_edge_store as _b
    from bayes_basket_target.basket_kernel.delta_v2 import (
        BETA_EDGE_MODE,
        clears_economic_hurdle_v1 as _clears_economic_hurdle_v1,
    )

    out: dict[str, Any] = {
        "available": True, "mode": BETA_EDGE_MODE, "served": {},
        "research": {}, "replay": {"days": [], "cells": {}}, "live": {},
        # Charter 128 v2: the basis every number here is read on, and the
        # band rows beside each stage's whole row.
        "price_basis": _b.PRICE_BASIS, "lead_bands": {},
        "served_other_basis": 0,
    }
    # -- served rows, with the evidence that produced them
    try:
        for row in conn.execute(
            "SELECT stage_scope, beta, beta_lcb, beta_ucb, intercept,"
            " row_count, day_count, accumulating, as_of_event_date,"
            " activated_at_utc, evidence_json, lead_band, price_basis"
            " FROM calibration_beta_edge_table WHERE activated=1"
        ):
            if str(row[12] or "") != _b.PRICE_BASIS:
                # A row from the ask basis: still active in the table until
                # the next refit deactivates it, served by nobody.
                out["served_other_basis"] += 1
                continue
            try:
                ev = json.loads(row[10] or "{}")
            except ValueError:
                ev = {}
            reading = {
                "beta": row[1], "beta_lcb": row[2], "beta_ucb": row[3],
                "intercept": row[4], "rows": row[5], "days": row[6],
                "accumulating": bool(row[7]), "as_of": row[8],
                "activated_at": row[9], "anchor": ev.get("anchor_utc"),
                "excluded_before_anchor": ev.get("excluded_before_anchor"),
                "days_beta_positive": ev.get("days_beta_positive"),
                "layers": ev.get("layers") or {},
                "lead_band": str(row[11] or ""),
                # v3: the market's own slope; absent on rows fitted before it.
                "gamma": ev.get("gamma"), "gamma_lcb": ev.get("gamma_lcb"),
                "gamma_ucb": ev.get("gamma_ucb"),
                "market_centre": ev.get("market_centre"),
                "intercept_lcb": ev.get("intercept_lcb"),
                "intercept_ucb": ev.get("intercept_ucb"),
            }
            if reading["lead_band"] == _b.WHOLE_STAGE:
                out["served"][str(row[0])] = reading
            else:
                out["lead_bands"].setdefault(str(row[0]), {})[reading["lead_band"]] = reading
    except sqlite3.Error:
        out["served_error"] = "no_beta_table"
    # -- research: the unanchored slope, for the eye only
    for scope in _b.STAGE_SCOPES:
        try:
            fit = _b.fit_beta(conn, stage_scope=scope, anchored=False, draws=200)
        except Exception as exc:  # noqa: BLE001 -- a research line, never fatal
            out["research"][scope] = {"error": str(exc)[:120]}
            continue
        ev = fit["evidence"]
        out["research"][scope] = {
            "beta": ev.get("beta"), "beta_lcb": ev.get("beta_lcb"),
            "beta_ucb": ev.get("beta_ucb"), "intercept": ev.get("intercept"),
            "rows": fit["row_count"], "days": fit["day_count"],
            "days_beta_positive": ev.get("days_beta_positive"),
            "by_lead": (ev.get("layers") or {}).get("by_lead") or {},
            "gamma": ev.get("gamma"), "gamma_lcb": ev.get("gamma_lcb"),
            "gamma_ucb": ev.get("gamma_ucb"),
            "market_centre": ev.get("market_centre"),
        }
    # -- replay: every settled candidate window of the last 14 days,
    # re-judged with the beta served at its decision instant.
    since = (date.today() - timedelta(days=14)).isoformat()
    journal: dict[str, list[tuple[str, float]]] = {}
    try:
        for scope, at, lcb in conn.execute(
            "SELECT j.stage_scope, j.fitted_at_utc, t.beta_lcb"
            " FROM calibration_beta_edge_fit_journal j"
            " JOIN calibration_beta_edge_table t ON t.beta_sha256=j.beta_sha256"
            # The stage's whole row on this basis: a band row or an
            # ask-basis row is not what a window's decision read.
            " WHERE t.lead_band=? AND t.price_basis=?"
            " ORDER BY j.fitted_at_utc, j.fit_id",
            (_b.WHOLE_STAGE, _b.PRICE_BASIS),
        ):
            journal.setdefault(str(scope), []).append((str(at), float(lcb)))
    except sqlite3.Error:
        journal = {}

    def served_at(scope: str, at: str) -> float | None:
        best = None
        for fitted, lcb in journal.get(scope, ()):
            if fitted <= at:
                best = lcb
            else:
                break
        return best

    days: dict[str, dict[str, Any]] = {}
    cells = {k: {"n": 0, "hit_minus_price": 0.0, "price": 0.0}
             for k in ("both_admit", "beta_refuses", "beta_admits", "both_refuse")}
    try:
        rows = conn.execute(
            "SELECT stage, event_date, decision_at_utc, raw_win_probability,"
            " all_in_price, hit, admissible, market_mid"
            f" FROM {_b.CANDIDATE_SOURCE}"
            " WHERE settled_bucket IS NOT NULL AND event_date >= ?"
            "   AND raw_win_probability IS NOT NULL AND all_in_price > 0",
            (since,),
        ).fetchall()
    except sqlite3.Error:
        # A book without the v2 column (or without the table): nothing can
        # be re-judged on this basis, and nothing is judged on another.
        rows = []
    no_market_mid = 0
    # The acceptance sample (user ruling, 2026-09-17): the windows the model
    # claims at 0.45 or more while the market's mid sits under 0.30 -- the
    # shape the band table cannot see and this lane exists to catch.  Counted
    # on every settled window, judged where a beta was being served.
    from bayes_basket_target.candidate_window_evidence import (  # noqa: PLC0415
        ACCEPTANCE_CLAIM_FLOOR,
        ACCEPTANCE_MARKET_CEILING,
        in_acceptance_class,
    )
    acc: dict[str, Any] = {
        "windows": 0, "judged": 0, "hits": 0.0, "price": 0.0, "margin": 0.0,
        "current_admits": 0, "current_margin": 0.0,
        "beta_admits": 0, "beta_margin": 0.0, "days": set(),
    }
    for stage, day, at, raw, price, hit, adm, mid in rows:
        in_class = in_acceptance_class(raw, mid)
        if in_class:
            acc["windows"] += 1
            acc["days"].add(str(day))
            acc["hits"] += float(hit or 0)
            acc["price"] += float(price)
            acc["margin"] += float(hit or 0) - float(price)
            if bool(adm):
                acc["current_admits"] += 1
                acc["current_margin"] += float(hit or 0) - float(price)
        lcb = served_at(str(stage), str(at or ""))
        if lcb is None:
            continue
        try:
            mid_value = float(mid)
        except (TypeError, ValueError):
            mid_value = float("nan")
        if not (0.0 < mid_value < 1.0):
            no_market_mid += 1
            continue
        # v2 basis: the belief the served beta implies, judged against the
        # all-in ask under charter 130's hurdle -- the gate's own question.
        # At beta_lcb 0 the lane buys nothing, whatever the book's arithmetic.
        beta_adm = lcb > 1e-9 and _clears_economic_hurdle_v1(
            expected_payout=mid_value + lcb * (float(raw) - mid_value),
            cost=float(price),
        )
        cur_adm = bool(adm)
        key = ("both_admit" if beta_adm else "beta_refuses") if cur_adm else (
            "beta_admits" if beta_adm else "both_refuse")
        margin = float(hit or 0) - float(price)
        if in_class:
            acc["judged"] += 1
            if beta_adm:
                acc["beta_admits"] += 1
                acc["beta_margin"] += margin
        d = days.setdefault(str(day), {"date": str(day), "windows": 0, "current_admits": 0,
                                       "beta_admits": 0, "beta_refuses_of_admitted": 0,
                                       "beta_admits_of_refused": 0, "sum_margin_admitted": 0.0,
                                       "sum_margin_beta_admitted": 0.0})
        d["windows"] += 1
        if cur_adm:
            d["current_admits"] += 1
            d["sum_margin_admitted"] += margin
        if beta_adm:
            d["beta_admits"] += 1
            d["sum_margin_beta_admitted"] += margin
        if key == "beta_refuses":
            d["beta_refuses_of_admitted"] += 1
        if key == "beta_admits":
            d["beta_admits_of_refused"] += 1
        c = cells[key]
        c["n"] += 1
        c["hit_minus_price"] += margin
        c["price"] += float(price)
    for c in cells.values():
        c["mean_hit_minus_price"] = round(c["hit_minus_price"] / c["n"], 4) if c["n"] else None
        c["mean_price"] = round(c["price"] / c["n"], 4) if c["n"] else None
        del c["hit_minus_price"]
        del c["price"]
    out["replay"] = {
        "since": since, "windows": len(rows), "judged": sum(d["windows"] for d in days.values()),
        "no_market_mid": no_market_mid,
        "cells": cells,
        "days": [
            {**d,
             "mean_margin_admitted": round(d["sum_margin_admitted"] / d["current_admits"], 4) if d["current_admits"] else None,
             "mean_margin_beta_admitted": round(d["sum_margin_beta_admitted"] / d["beta_admits"], 4) if d["beta_admits"] else None}
            for d in sorted(days.values(), key=lambda x: x["date"])
        ],
    }
    for d in out["replay"]["days"]:
        d.pop("sum_margin_admitted", None)
        d.pop("sum_margin_beta_admitted", None)
    n_acc = acc["windows"]
    out["replay"]["acceptance"] = {
        "claim_floor": ACCEPTANCE_CLAIM_FLOOR,
        "market_ceiling": ACCEPTANCE_MARKET_CEILING,
        "windows": n_acc, "days": len(acc["days"]), "judged": acc["judged"],
        "hit_rate": (acc["hits"] / n_acc) if n_acc else None,
        "mean_price": round(acc["price"] / n_acc, 4) if n_acc else None,
        "mean_hit_minus_price": round(acc["margin"] / n_acc, 4) if n_acc else None,
        "current_admits": acc["current_admits"],
        "current_mean_hit_minus_price": (
            round(acc["current_margin"] / acc["current_admits"], 4)
            if acc["current_admits"] else None
        ),
        "beta_admits": acc["beta_admits"],
        "beta_mean_hit_minus_price": (
            round(acc["beta_margin"] / acc["beta_admits"], 4)
            if acc["beta_admits"] else None
        ),
    }
    # -- live: the shadow's own counters off recent decisions
    live_since = (date.today() - timedelta(days=2)).isoformat()
    live = {"since": live_since, "decisions": 0, "with_shadow": 0,
            "would_refuse": 0, "would_admit": 0, "served_seen": {}}
    try:
        facts = conn.execute(
            "SELECT feature_json FROM basket_policy_training_fact"
            " WHERE event_date >= ? ORDER BY decision_at_utc DESC LIMIT 400",
            (live_since,),
        ).fetchall()
    except sqlite3.Error:
        facts = []
    for (payload,) in facts:
        try:
            detail = json.loads(payload or "{}")
        except ValueError:
            continue
        live["decisions"] += 1
        sel = ((detail.get("plan") or {}).get("basket_support_selection")) or {}
        if "beta_edge_mode" not in sel:
            continue
        live["with_shadow"] += 1
        live["would_refuse"] += int(sel.get("beta_would_refuse_count") or 0)
        live["would_admit"] += int(sel.get("beta_would_admit_count") or 0)
        for scope, srv in (sel.get("beta_served") or {}).items():
            if isinstance(srv, dict):
                live["served_seen"][str(scope)] = srv.get("beta_lcb")
    out["live"] = live
    return out


def collect_lambda_axis_usage(conn: sqlite3.Connection) -> dict[str, Any]:
    """Which axis bought each seat, so charter 97 can be retired on evidence.

    Charter 97 serves the claim-edge thermometer as lambda's second axis and
    sets its own retirement condition: once the seats that axis opened clear
    charter 68's hurdle, retire it if their realisation falls under one.
    Nothing could evaluate that without knowing which axis admitted a window,
    which is why the gate now stamps ``effective_edge_lambda_source`` on every
    priced row.  This counts them.

    Deliberately cheap and deliberately honest about being young: the field
    was added 2026-09-06 and rows written before that carry none, so the
    reading starts empty and fills as cycles run.  An empty panel here means
    "not measured yet", never "the axis bought nothing".
    """

    since = (
        date.today() - timedelta(days=3)
    ).isoformat()
    seated: dict[str, int] = {}
    considered: dict[str, int] = {}
    scanned = 0
    try:
        rows = conn.execute(
            "SELECT details_json FROM auto_trade_decisions"
            " WHERE created_at >= ?"
            "   AND details_json LIKE '%effective_edge_lambda_source%'"
            " ORDER BY created_at DESC LIMIT 400",
            (since,),
        ).fetchall()
    except sqlite3.Error:
        return {"available": False, "since": since}

    def walk(node: Any) -> "Iterator[dict[str, Any]]":
        if isinstance(node, dict):
            if "basket_support_selection" in node:
                yield node["basket_support_selection"]
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    for (payload,) in rows:
        try:
            detail = json.loads(payload or "{}")
        except ValueError:
            continue
        scanned += 1
        for selection in walk(detail):
            seat = selection.get("seated")
            if isinstance(seat, dict) and seat.get("effective_edge_lambda_source"):
                key = str(seat["effective_edge_lambda_source"])
                seated[key] = seated.get(key, 0) + 1
            for row in selection.get("considered") or []:
                if isinstance(row, dict) and row.get(
                    "effective_edge_lambda_source"
                ):
                    key = str(row["effective_edge_lambda_source"])
                    considered[key] = considered.get(key, 0) + 1
    return {
        "available": True,
        "since": since,
        "scanned_decisions": scanned,
        "seated_by_source": seated,
        "considered_by_source": considered,
    }


def _wilson_interval(hits: int, n: int, z: float = 1.6449) -> tuple:
    """Two-sided Wilson interval on a rate; ``z`` is the 90% default."""

    if n <= 0:
        return (None, None)
    p = hits / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


_LEAD_DAY_END: dict[tuple, Any] = {}


def _lead_hours(day: str, zone: str, decided_at: str) -> float | None:
    """Hours from a decision to the moment the local day's high is final.

    The lead axis has to be physical, not "which cycle of the day this
    was": stations settle on their own midnight, so the same UTC hour is a
    different lead at every station.
    """

    key = (day, zone)
    if key not in _LEAD_DAY_END:
        try:
            year, month, dom = (int(part) for part in day.split("-"))
            _LEAD_DAY_END[key] = (
                datetime(year, month, dom, tzinfo=ZoneInfo(zone))
                + timedelta(days=1)
            ).astimezone(timezone.utc)
        except Exception:
            _LEAD_DAY_END[key] = None
    end = _LEAD_DAY_END[key]
    if end is None:
        return None
    try:
        at = datetime.fromisoformat(str(decided_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (end - at).total_seconds() / 3600.0


CLAIM_EDGE_STAGES = ("P0", "P0-M", "P1")


def _claim_edge_calibration(rows: list[tuple]) -> dict[str, Any]:
    """The claimed-edge thermometer: does a claim that calls itself a bargain
    deliver?  Asked where no fill model can reach it.

    Rows are ``(station, day, claim, price, won[, lead_hours[, stage]])``.
    The unit is the seated WINDOW at each decision and the outcome is
    whether the truth landed in it.  Nothing is conditioned on a fill,
    nothing is conditioned on a basket clearing the hurdle.

    This is the section's only reading since 2026-09-14.  The ledger version
    (``collect_edge_band_health``, posted legs split by whether the market
    sold them) was retired that day: charter 129 made the paper book's fills
    conditional on the outcome (p(fill|crossed) 0.4581 winning, 0.6650
    losing), so its filled side read the simulator's own assumption back;
    it held 99 settled filled legs with four of seven bands under its floor;
    and its headline pooled only the bands that cleared that floor, which on
    the day of retirement were exactly the three that excluded both fevered
    bands (x0.99 printed against x0.885 over every settled filled leg).

    The main reading per band is ``hit - price``, not ``hit / claim``.  The
    ratio collapses with edge under ANY model whose disagreement with the
    price is noise -- the band is cut on that disagreement, so a high band
    is where claim is far above price and a hit rate that follows the price
    divides out small.  Measured 2026-09-14 on P0: every band's hit - price
    sat between -2 and -7 points while claim - price ran from below zero to
    past thirty.  ``hit - price`` says that directly, and it is also what a
    share bought at that price earns.  The ratio stays beside it.

    Split by stage (user ruling 2026-09-02: the same claimed edge is not the
    same claim at P0 and at P1), with the pooled reading kept beside the
    split rather than instead of it.

    Intervals are block-bootstrapped over station-days: the same window is
    re-seated every cycle and the stages of one day settle against one
    high, so a row is not a draw.  Verdicts are taken on the unrounded
    quantiles and only then rounded for display.

    The regression beside it is charter 128's own form, (truth - price) =
    a + beta * (claim - price), so the panel's shape and the beta lane's
    slope are one reading rather than two vocabularies for it.
    """

    bands = ("<0", "0-2%", "2-5%", "5-10%", "10-20%", "20-30%", ">=30%")

    def band_of(edge: float) -> str:
        if edge < 0:
            return "<0"
        for top, name in (
            (0.02, "0-2%"), (0.05, "2-5%"), (0.10, "5-10%"),
            (0.20, "10-20%"), (0.30, "20-30%"),
        ):
            if edge < top:
                return name
        return ">=30%"

    # Every statistic here is a function of eight per-row sums, so the
    # bootstrap resamples per-day sums instead of re-walking rows: the same
    # day draws as a row-level walk, at a cost that no longer grows with
    # cycles per day.
    def sums(group: list[tuple]) -> list[float]:
        acc = [0.0] * 8
        for row in group:
            claim, price, won = float(row[2]), float(row[3]), float(row[4])
            x, y = claim - price, won - price
            acc[0] += 1.0
            acc[1] += claim
            acc[2] += won
            acc[3] += price
            acc[4] += x
            acc[5] += y
            acc[6] += x * x
            acc[7] += x * y
        return acc

    def ratio_of(s: Sequence[float]) -> float | None:
        return s[2] / s[1] if s[0] and s[1] > 0 else None

    def gap_of(s: Sequence[float]) -> float | None:
        return (s[2] - s[3]) / s[0] if s[0] else None

    def slope_of(s: Sequence[float]) -> float | None:
        n = s[0]
        if n < 20:
            return None
        sxx = s[6] - s[4] * s[4] / n
        if sxx <= 1e-12:
            return None
        return (s[7] - s[4] * s[5] / n) / sxx

    def hit_of(s: Sequence[float]) -> float | None:
        return s[2] / s[0] if s[0] else None

    stats = {
        "ratio": ratio_of, "gap": gap_of, "slope": slope_of, "hit": hit_of,
    }

    def interval(
        group: list[tuple], wanted: Sequence[str]
    ) -> dict[str, tuple]:
        empty = {name: (None, None) for name in wanted}
        by_day: dict[tuple, list] = {}
        for row in group:
            by_day.setdefault((row[0], row[1]), []).append(row)
        keys = list(by_day)
        if len(keys) < 10:
            return empty
        day_sums = [sums(by_day[key]) for key in keys]
        # Deterministic resampling: the page is rebuilt many times a day and
        # an interval that moves on its own would be read as the reading
        # moving, so the generator is seeded the same way every build.
        #
        # It used to be a hand-rolled LCG taken modulo the day count.  The
        # low bits of a power-of-two-modulus LCG cycle with period 2^k, so
        # with 128 station-days every "resample" drew each day exactly once
        # and the interval collapsed to the point estimate (P0-M 2-5%,
        # 2026-09-14: hit - price [-0.045, -0.045]); any day count with a
        # power-of-two factor was drawn narrower than it is.
        rng = random.Random(20260913)
        draws: dict[str, list[float]] = {name: [] for name in wanted}
        for _ in range(300):
            total = [0.0] * 8
            for _ in range(len(keys)):
                picked = day_sums[rng.randrange(len(keys))]
                for i in range(8):
                    total[i] += picked[i]
            for name in wanted:
                value = stats[name](total)
                if value is not None:
                    draws[name].append(value)
        out: dict[str, tuple] = {}
        for name in wanted:
            got = sorted(draws[name])
            out[name] = (
                (None, None) if len(got) < 60
                else (got[int(0.05 * len(got))], got[int(0.95 * len(got))])
            )
        return out

    def r3(value: float | None) -> float | None:
        return None if value is None else round(value, 3)

    def verdict(low: float | None, high: float | None, line: float) -> str | None:
        # Taken on the raw quantiles: 0.498987 is below 0.5 even though it
        # prints as 0.499, and a verdict read off the printed digit is a
        # verdict about the rounding.
        if low is None or high is None:
            return None
        if high < line:
            return "below"
        if low > line:
            return "above"
        return "unresolved"

    def summarise(group_rows: list[tuple]) -> dict[str, Any]:
        grouped: dict[str, list] = {}
        for row in group_rows:
            grouped.setdefault(band_of(row[2] - row[3]), []).append(row)
        band_rows = []
        for name in bands:
            group = grouped.get(name) or []
            if not group:
                continue
            s = sums(group)
            got = interval(group, ("ratio", "gap", "hit"))
            (r_lo, r_hi), (g_lo, g_hi) = got["ratio"], got["gap"]
            h_lo, h_hi = got["hit"]
            band_rows.append({
                "band": name,
                "n": len(group),
                "station_days": len({(row[0], row[1]) for row in group}),
                "share": round(len(group) / len(group_rows), 4),
                "claimed_mean": round(s[1] / s[0], 4),
                "price_mean": round(s[3] / s[0], 4),
                "hit_rate": round(s[2] / s[0], 4),
                # Drawn as the whisker on the page's truth dot, so the
                # interval is the hit rate's own and not the price's shifted.
                "hit_lo": r3(h_lo),
                "hit_hi": r3(h_hi),
                "realised_x": r3(ratio_of(s)),
                "realised_lo": r3(r_lo),
                "realised_hi": r3(r_hi),
                "realised_verdict": verdict(r_lo, r_hi, 1.0),
                "hit_minus_price": r3(gap_of(s)),
                "hit_minus_price_lo": r3(g_lo),
                "hit_minus_price_hi": r3(g_hi),
                "hit_minus_price_verdict": verdict(g_lo, g_hi, 0.0),
            })
        s = sums(group_rows)
        got = interval(group_rows, ("slope", "gap"))
        (b_lo, b_hi), (g_lo, g_hi) = got["slope"], got["gap"]
        return {
            "rows": len(group_rows),
            "station_days": len({(row[0], row[1]) for row in group_rows}),
            "bands": band_rows,
            "beta": r3(slope_of(s)),
            "beta_lo": r3(b_lo),
            "beta_hi": r3(b_hi),
            # Nearer "the price is the answer" (0) or "the disagreement
            # realises in full" (1), only when the whole interval says so.
            "beta_vs_half": verdict(b_lo, b_hi, 0.5),
            "pooled_x": r3(ratio_of(s)),
            "hit_minus_price": r3(gap_of(s)),
            "hit_minus_price_lo": r3(g_lo),
            "hit_minus_price_hi": r3(g_hi),
        }

    def slope(group: list[tuple]) -> float | None:
        return slope_of(sums(group))

    if not rows:
        return {"available": False}
    pooled = summarise(rows)
    out = pooled["bands"]
    beta = slope(rows)
    beta_lo, beta_hi = pooled["beta_lo"], pooled["beta_hi"]
    # A row that cannot say its stage stays in the pooled reading and sits
    # out the split, so the split is an exact partition of what it can see.
    by_stage = {
        stage: summarise(staged)
        for stage in CLAIM_EDGE_STAGES
        if (staged := [
            row for row in rows if len(row) > 6 and row[6] == stage
        ])
    }

    # The cut the user asked for, 2026-09-13: is there a subset whose beta
    # is clearly above zero?  Both axes are reported live so the question
    # keeps being answered as the sample grows; the verdict beside them is
    # the walk-forward, which is the only form of this question that has
    # ever survived in this repo.
    def sliced(key: Any, floor: int) -> list[dict[str, Any]]:
        groups: dict[Any, list] = {}
        for row in rows:
            mark = key(row)
            if mark is not None:
                groups.setdefault(mark, []).append(row)
        cut = []
        for mark, group in groups.items():
            if len(group) < floor:
                continue
            value = slope(group)
            if value is None:
                continue
            low, high = interval(group, ("slope",))["slope"]
            cut.append({
                "key": str(mark),
                "n": len(group),
                "station_days": len({(r[0], r[1]) for r in group}),
                "beta": round(value, 3),
                "beta_lo": r3(low),
                "beta_hi": r3(high),
                "above_zero": verdict(low, high, 0.0) == "above",
            })
        return cut

    lead_bins = ((0, 6), (6, 12), (12, 24), (24, 36), (36, 48), (48, 72))

    def lead_of(row: tuple) -> str | None:
        hours = row[5] if len(row) > 5 else None
        if hours is None:
            return None
        for low, high in lead_bins:
            if low <= hours < high:
                return f"{low}-{high}h"
        return None

    by_lead = sliced(lead_of, 100)
    by_lead.sort(key=lambda c: int(c["key"].split("-")[0]))
    by_station = sliced(lambda row: row[0], 150)
    by_station.sort(key=lambda c: -(c["beta_lo"] if c["beta_lo"] is not None
                                    else -9.0))
    return {
        "available": True,
        "rows": len(rows),
        "station_days": len({(row[0], row[1]) for row in rows}),
        "bands": out,
        "beta": None if beta is None else round(beta, 3),
        "beta_lo": beta_lo,
        "beta_hi": beta_hi,
        "beta_vs_half": pooled["beta_vs_half"],
        "pooled_x": pooled["pooled_x"],
        "hit_minus_price": pooled["hit_minus_price"],
        "hit_minus_price_lo": pooled["hit_minus_price_lo"],
        "hit_minus_price_hi": pooled["hit_minus_price_hi"],
        "stages": list(by_stage),
        "by_stage": by_stage,
        "by_lead": by_lead,
        "by_station": by_station,
        "stations_cut": len(by_station),
        "stations_above_zero": sum(
            1 for c in by_station if c["above_zero"]
        ),
        "leads_above_zero": sum(1 for c in by_lead if c["above_zero"]),
    }


def collect_lambda_axis_realisation(conn: sqlite3.Connection) -> dict[str, Any]:
    """Charter 97's retirement condition, read on the seat that settles.

    The clause retires its own axis once the seats it opened clear the
    hurdle and realise under one, and it pointed at the claimed-edge
    thermometer for that reading.  The thermometer cannot answer: the thin
    band is ``e <= 0.05``, which lands in its 0-2% and 2-5% rows, and after
    the 2026-09-12 re-derive those two hold 41 posted legs between them,
    under the section's ten-settled-leg floor and not closing.

    So the seat is read off the stamp the gate already writes.  Every
    priced row carries ``effective_edge_lambda_source``; the candidate
    window facts carry the seated window and the day's bucket intervals;
    the truth lane carries the high.

    The hard part is not the join, it is the unit, and the first version of
    this reader got it wrong.  It took each axis's own last decision of the
    day, which double-counts a station-day under both axes and hands the
    axis that did NOT serve at the close a stale mid-day snapshot.  That is
    not a detail: the axis changes inside the day on 83% of station-days,
    and coverage climbs through the day (0.76 in the first third to 0.91 in
    the last) while the claim stays near 0.91, so an axis measured earlier
    in the day is measured worse for reasons that have nothing to do with
    the axis.  The reversed reading it produced -- thin band 0.949 against
    the agreement band's 0.962 -- is the opposite of the paired one.

    Two readings replace it, and neither is a pool over cycles:

    * ``standing`` -- the seat that stood at the day's last decision,
      attributed to the axis that served it.  This is what settles, so this
      is what the clause's verdict is taken on.  One row per station-day.
    * ``paired`` -- over the station-days where BOTH axes served, each
      axis's own mean on that day.  Same days, same weather, same lead
      distribution: this is the only comparison between axes that is not
      confounded by when in the day each one happened to serve.

    The day floor and the "未分出" state are not in the clause either;
    they are the 2026-09-11 finding that the lambda guard compared ratios
    on one to four days with no interval, written into a reader.
    """

    minimum_station_days = 30
    window_days = 45
    since = (date.today() - timedelta(days=window_days)).isoformat()
    truth: dict[tuple, float] = {}
    try:
        for sid, day, high in conn.execute(
            "SELECT station_id, local_date, official_tmax_c"
            " FROM calibration_station_daily_high_truth"
            " WHERE qc_status='ACCEPTED' AND official_tmax_c IS NOT NULL"
            "   AND local_date >= ?",
            (since,),
        ):
            truth[(str(sid), str(day))] = float(high)
    except sqlite3.Error:
        return {"available": False, "since": since}

    def walk(node: Any) -> "Iterator[dict[str, Any]]":
        if isinstance(node, dict):
            if "basket_support_selection" in node:
                yield node["basket_support_selection"]
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    def landed(intervals: dict[str, Any], high: float) -> str | None:
        for label, span in intervals.items():
            try:
                low, top = span
            except (TypeError, ValueError):
                continue
            if (low is None or high >= float(low)) and (
                top is None or high < float(top)
            ):
                return str(label)
        return None

    # station-day -> every seated decision of that day, in time order.
    days: dict[tuple, list] = {}
    # Every seated window's (claim, all-in price, settled) -- the claimed
    # edge thermometer's own question asked where no fill model can reach
    # it.  See ``_claim_edge_calibration``.
    claim_edge: list[tuple] = []
    scanned = 0
    first_day = ""
    last_day = ""
    try:
        cursor = conn.execute(
            "SELECT station_id, event_date, decision_at_utc,"
            " settlement_timezone, feature_json, stage"
            " FROM basket_policy_training_fact"
            " WHERE event_date >= ?"
            "   AND feature_json LIKE '%win_probability%'",
            (since,),
        )
    except sqlite3.Error:
        return {"available": False, "since": since}
    for sid, day, decided_at, zone, payload, stage in cursor:
        high = truth.get((str(sid), str(day)))
        if high is None:
            continue
        try:
            fact = json.loads(payload or "{}")
        except ValueError:
            continue
        spans = fact.get("bucket_intervals_c_json")
        if not spans:
            continue
        try:
            intervals = json.loads(spans) if isinstance(spans, str) else spans
        except ValueError:
            continue
        settled_bucket = landed(intervals, high)
        if settled_bucket is None:
            continue
        scanned += 1
        first_day = str(day) if not first_day else min(first_day, str(day))
        last_day = max(last_day, str(day))
        for selection in walk(fact):
            seat = selection.get("seated")
            if not isinstance(seat, dict):
                continue
            source = seat.get("effective_edge_lambda_source")
            buckets = [str(b) for b in (seat.get("buckets") or [])]
            claim = seat.get("win_probability")
            if not buckets or claim is None:
                continue
            won = 1 if settled_bucket in buckets else 0
            # The axis tables answer for the stamp only -- it was added
            # 2026-09-06 and a row without it cannot say which axis bought
            # it.  The claimed-edge lane below has no such need and takes
            # the whole window.
            if source:
                days.setdefault((str(sid), str(day)), []).append((
                    str(decided_at or ""), str(source), float(claim), won,
                ))
            price = seat.get("all_in_price")
            if price is not None:
                claim_edge.append((
                    str(sid), str(day), float(claim), float(price), won,
                    _lead_hours(str(day), str(zone or ""), str(decided_at or "")),
                    str(stage or ""),
                ))
            # One fact is one decision; the first seated selection in it is
            # that decision's seat.
            break

    standing: dict[str, list] = {}
    day_means: dict[str, list] = {}
    mixed_days = 0
    for rows in days.values():
        rows.sort()
        if len({row[1] for row in rows}) > 1:
            mixed_days += 1
        _at, source, claim, won = rows[-1]
        standing.setdefault(source, []).append((claim, won))
        by_source: dict[str, list] = {}
        for _stamp, src, cl, win in rows:
            by_source.setdefault(src, []).append((cl, win))
        if len(by_source) > 1:
            for src, group in by_source.items():
                day_means.setdefault(src, []).append((
                    sum(c for c, _w in group) / len(group),
                    sum(w for _c, w in group) / len(group),
                ))

    def pooled(group: list) -> tuple:
        if not group:
            return (None, None, None)
        claimed = sum(c for c, _w in group) / len(group)
        rate = sum(w for _c, w in group) / len(group)
        ratio = rate / claimed if claimed else None
        return (claimed, rate, ratio)

    axes: list[dict[str, Any]] = []
    for source in sorted(set(standing) | set(day_means)):
        seats = standing.get(source, [])
        n = len(seats)
        claimed, rate, ratio = pooled(seats)
        low, high_bound = _wilson_interval(sum(w for _c, w in seats), n)
        band = (
            None if not claimed or low is None
            else (low / claimed, high_bound / claimed)
        )
        if n < minimum_station_days:
            verdict = "样本不足"
        elif band is None:
            verdict = "未分出"
        elif band[1] < 1.0:
            verdict = "跌破"
        elif band[0] >= 1.0:
            verdict = "达标"
        else:
            verdict = "未分出"
        pair = day_means.get(source, [])
        p_claim, p_rate, p_ratio = pooled(pair)
        axes.append({
            "source": source,
            "standing_days": n,
            "claimed_mean": None if claimed is None else round(claimed, 4),
            "hit_rate": None if rate is None else round(rate, 4),
            "realised_x": None if ratio is None else round(ratio, 3),
            "realised_lo": None if band is None else round(band[0], 3),
            "realised_hi": None if band is None else round(band[1], 3),
            "verdict": verdict,
            "paired_days": len(pair),
            "paired_claimed": None if p_claim is None else round(p_claim, 4),
            "paired_hit_rate": None if p_rate is None else round(p_rate, 4),
            "paired_x": None if p_ratio is None else round(p_ratio, 3),
        })
    axes.sort(key=lambda a: -a["standing_days"])
    return {
        "available": True,
        "since": since,
        "first_day": first_day,
        "last_day": last_day,
        "facts_scanned": scanned,
        "station_days": len(days),
        "mixed_axis_days": mixed_days,
        "minimum_station_days": minimum_station_days,
        "axes": axes,
        "claim_edge": _claim_edge_calibration(claim_edge),
    }


def collect_valuation_lane(conn: sqlite3.Connection) -> dict[str, Any]:
    """Charter 132's page: the pooled valuation as served, the walk-forward
    verdict each stage stands under, the lambda tables' shape vintage, and
    what the shadow recorded on recent decisions.

    Read-only and cheap: three small tables plus a bounded scan of the most
    recent decisions' plan headers.  The verdict's own evidence carries the
    four-arm readings, so the page repeats the numbers a promotion would be
    argued on rather than re-deriving them.
    """

    out: dict[str, Any] = {"available": False}
    try:
        from bayes_basket_target.effective_edge_store import (  # noqa: PLC0415
            lambda_vintage_status,
        )
        from bayes_basket_target.valuation_store import (  # noqa: PLC0415
            load_valuation_tables,
        )
    except ImportError:
        return out
    try:
        served = load_valuation_tables(conn)
    except sqlite3.Error:
        return out
    stages: list[dict[str, Any]] = []
    for scope in ("P0", "P0-M", "P1"):
        row = served.get(scope)
        verdict: dict[str, Any] | None = None
        try:
            got = conn.execute(
                "SELECT verdict, reason, first_test_day, last_test_day,"
                " computed_at_utc, evidence_json FROM calibration_valuation_verdict"
                " WHERE stage_scope=? AND activated=1"
                " ORDER BY computed_at_utc DESC LIMIT 1",
                (scope,),
            ).fetchone()
        except sqlite3.Error:
            got = None
        if got is not None:
            try:
                body = json.loads(got[5] or "{}")
            except ValueError:
                body = {}
            evidence = body.get("evidence") or {}
            information = evidence.get("information") or {}
            arms = (evidence.get("strategy") or {}).get("arms") or {}
            verdict = {
                "verdict": got[0], "reasons": [r for r in str(got[1]).split(";") if r],
                "first_test_day": got[2], "last_test_day": got[3],
                "computed_at_utc": got[4],
                "test_days": information.get("test_days"),
                "test_station_days": information.get("station_days"),
                "log_score": information.get("log_score"),
                "weather_minus_market": information.get("weather_minus_market_log"),
                "weather_minus_market_ci": information.get("weather_minus_market_log_ci"),
                "pool_minus_market": information.get("pool_minus_market_log"),
                "pool_minus_market_ci": information.get("pool_minus_market_log_ci"),
                "arms": {
                    arm: {
                        "roi": (arms.get(arm) or {}).get("roi_per_share"),
                        "roi_ci": (arms.get(arm) or {}).get("roi_ci"),
                        "station_days": (arms.get(arm) or {}).get("admitted_station_days"),
                        "windows": (arms.get(arm) or {}).get("admitted_windows"),
                        "brier": (arms.get(arm) or {}).get("window_brier"),
                    }
                    for arm in ("lambda", "weather", "market", "pool")
                },
            }
        stages.append({
            "stage": scope,
            "served": None if row is None else {
                key: row.get(key) for key in (
                    "beta", "beta_lcb", "beta_ucb", "accumulating",
                    "station_day_count", "day_count", "fit_cutoff_utc", "scores",
                )
            },
            "verdict": verdict,
        })
    shadow = {"decisions": 0, "states": {}, "would_admit": 0, "would_refuse": 0}
    try:
        # The plan header lives on the training fact, one per decision;
        # auto_trade_decisions carries it on a few rows only (2 of 121 on
        # 2026-09-17), so reading it there counted almost nothing.  The
        # table is append-only, so the newest rows are the highest rowids.
        rows = conn.execute(
            "SELECT feature_json FROM basket_policy_training_fact"
            " ORDER BY rowid DESC LIMIT 300"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for (payload,) in rows:
        try:
            detail = json.loads(payload or "{}")
        except ValueError:
            continue
        stack = [detail]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if "valuation_state" in node:
                    shadow["decisions"] += 1
                    state = str(node.get("valuation_state"))
                    shadow["states"][state] = shadow["states"].get(state, 0) + 1
                    shadow["would_admit"] += int(node.get("valuation_would_admit_count") or 0)
                    shadow["would_refuse"] += int(node.get("valuation_would_refuse_count") or 0)
                    continue
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    try:
        vintage = lambda_vintage_status(conn)
    except sqlite3.Error:
        vintage = {}
    # The live book's dollars, split along the chain by the same functions
    # the research report uses (scripts/edge_attribution.py), so the page and
    # the report cannot disagree about what a loss is made of.
    attribution: dict[str, Any] | None = None
    try:
        import edge_attribution  # noqa: PLC0415

        legs, counts = edge_attribution.load_legs(conn)
        attribution = {**edge_attribution.attribute(legs), "counts": counts}
    except (ImportError, sqlite3.Error, ValueError, KeyError, TypeError):
        attribution = None
    try:
        from bayes_basket_target.basket_kernel.valuation_v1 import (  # noqa: PLC0415
            VALUATION_MODE,
        )
    except ImportError:
        VALUATION_MODE = None  # noqa: N806
    out.update({
        "available": True, "stages": stages, "shadow": shadow,
        "lambda_vintage": vintage, "attribution": attribution,
        "mode": VALUATION_MODE,
    })
    return out


def collect_posterior_lane(conn: sqlite3.Connection) -> dict[str, Any]:
    """The L4 posterior lane (first-principles baseline, shadow): the served
    ``(alpha, gamma, w)`` rows per stage and lead band, the in-sample skill
    beside the forward skill read off the journal, the registered
    hypotheses, and what the shadow recorded on recent decisions.

    Read-only.  ``forward_skill`` re-reads each stage's population (the same
    ~10s the valuation lane's fit pays), which is why the section is cached.
    """

    out: dict[str, Any] = {"available": False}
    try:
        from bayes_basket_target.basket_kernel.posterior_v1 import (  # noqa: PLC0415
            POSTERIOR_MODE,
            POSTERIOR_RULE,
        )
        from bayes_basket_target.posterior_store import (  # noqa: PLC0415
            LEAD_BAND_LABELS,
            MIN_DAYS,
            MIN_STATION_DAYS,
            forward_skill,
            load_posterior_tables,
            realisation,
        )
    except ImportError:
        return out
    try:
        served = load_posterior_tables(conn)
    except sqlite3.Error:
        return out

    def _row(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            key: row.get(key) for key in (
                "lead_band", "alpha", "gamma", "w", "w_lcb", "w_ucb",
                "accumulating", "decision_count", "station_day_count",
                "day_count", "fit_cutoff_utc", "activated_at_utc", "skill", "ci",
            )
        }

    stages: list[dict[str, Any]] = []
    for scope in ("P0", "P0-M", "P1"):
        row = served.get(scope)
        try:
            forward = forward_skill(conn, stage_scope=scope)
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            forward = None
        stages.append({
            "stage": scope,
            "served": None if row is None else {
                **(_row(row) or {}),
                "lead_bands": {
                    label: _row((row.get("lead_bands") or {}).get(label))
                    for label in LEAD_BAND_LABELS
                },
            },
            "forward": forward,
        })
    shadow = {"decisions": 0, "states": {}, "would_admit": 0, "would_refuse": 0}
    try:
        # The plan header lives on the training fact, one per decision;
        # auto_trade_decisions carries it on a few rows only (2 of 121 on
        # 2026-09-17), so reading it there counted almost nothing.  The
        # table is append-only, so the newest rows are the highest rowids.
        rows = conn.execute(
            "SELECT feature_json FROM basket_policy_training_fact"
            " ORDER BY rowid DESC LIMIT 300"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for (payload,) in rows:
        try:
            detail = json.loads(payload or "{}")
        except ValueError:
            continue
        stack = [detail]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if "posterior_state" in node:
                    shadow["decisions"] += 1
                    state = str(node.get("posterior_state"))
                    shadow["states"][state] = shadow["states"].get(state, 0) + 1
                    shadow["would_admit"] += int(node.get("posterior_would_admit_count") or 0)
                    shadow["would_refuse"] += int(node.get("posterior_would_refuse_count") or 0)
                    continue
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    hypotheses: list[dict[str, Any]] = []
    try:
        for hid, status, min_days, expected in conn.execute(
            "SELECT hypothesis_id, status, min_event_days, expected_sign"
            " FROM calibration_posterior_hypothesis ORDER BY hypothesis_id"
        ):
            hypotheses.append({
                "id": hid, "status": status, "min_event_days": min_days,
                "expected_sign": expected,
            })
    except sqlite3.Error:
        pass
    # The money edge: every settled window production enumerated in the
    # last 14 days, re-judged with the L4 row active at its decision, against
    # the verdict production's lambda gate gave it.
    try:
        realised = realisation(
            conn, since_event_date=(date.today() - timedelta(days=14)).isoformat(),
        )
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        realised = None
    out.update({
        "available": True, "mode": POSTERIOR_MODE, "rule": POSTERIOR_RULE,
        "stages": stages, "shadow": shadow, "hypotheses": hypotheses,
        "min_station_days": MIN_STATION_DAYS, "min_days": MIN_DAYS,
        "realisation": realised,
    })
    return out


def charter_ev_identity(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The user's charter (2026-09-12), as arithmetic: edge = the market's
    error minus the model's error.

    Per bucket ``b`` of a ladder, with truth ``t_b`` (1 in the settled bucket,
    0 elsewhere), market mid ``m_b`` and served claim ``p_b``:

        e_q = m_b - t_b        the market's error
        e_p = p_b - t_b        the model's error

    Buying ``(p_b - m_b)`` shares of every bucket at the mid returns
    ``(p_b - m_b)(t_b - m_b) = e_q^2 - e_p e_q`` per bucket, so the strategy's
    expected profit per station-day is ``Var(e_q) - Cov(e_p, e_q)``: how much
    the market is wrong, less how much of that the model is wrong in the same
    direction.  ``k = Cov / Var(e_q)`` is the regression slope of the model's
    error on the market's: 1 means the model's error contains the whole of the
    market's error plus noise of its own; only below 1 is there anything to
    collect.  The model's own variance never enters -- the charter does not
    ask the model to be more accurate than the market, only to be wrong
    differently.

    ``rows`` carry ``p`` and ``m`` (vectors over the same ladder), ``label``,
    ``date`` and ``weight`` (each station-day's decisions sum to one, so the
    averages are per station-day).  The realised profit of the strategy is
    summed beside the identity rather than derived from it, so a reader --
    and the test -- sees the algebra hold on the data.

    ``EV`` is at the mid.  A row that carries ``half_spread`` (per bucket,
    on the vector's scale) also prices what the strategy pays to cross the
    book, ``sum |p_b - m_b| * half_spread_b``, which is the hurdle the
    charter sets for a live gate: the lower bound of EV above this cost.
    """

    var_market = var_model = cov = profit = spread = weight_total = 0.0
    spread_weight = 0.0
    by_day: dict[str, dict[str, float]] = {}
    station_days: set[tuple[str, str]] = set()
    for row in rows:
        p = row["p"]
        m = row["m"]
        label = str(row["label"])
        w = float(row.get("weight", 1.0) or 1.0)
        half = row.get("half_spread")
        vq = vp = c = pr = sc = 0.0
        for bucket in set(p) | set(m):
            t = 1.0 if str(bucket) == label else 0.0
            e_q = float(m.get(bucket, 0.0)) - t
            e_p = float(p.get(bucket, 0.0)) - t
            vq += e_q * e_q
            vp += e_p * e_p
            c += e_p * e_q
            pr += (e_p - e_q) * (-e_q)
            if half:
                sc += abs(e_p - e_q) * float(half.get(bucket, 0.0) or 0.0)
        var_market += w * vq
        var_model += w * vp
        cov += w * c
        profit += w * pr
        weight_total += w
        if half:
            spread += w * sc
            spread_weight += w
        day = by_day.setdefault(
            str(row["date"]),
            {"w": 0.0, "var_market": 0.0, "var_model": 0.0, "cov": 0.0},
        )
        day["w"] += w
        day["var_market"] += w * vq
        day["var_model"] += w * vp
        day["cov"] += w * c
        station_days.add((str(row["station"]), str(row["date"])))
    if weight_total <= 0.0:
        return {
            "decisions": 0, "station_days": 0, "days": 0,
            "var_market": None, "var_model": None, "cov": None,
            "ev": None, "k": None, "strategy_profit": None,
            "spread_cost": None, "days_ev_positive": None, "by_day": {},
        }
    var_q = var_market / weight_total
    return {
        "decisions": len(rows),
        "station_days": len(station_days),
        "days": len(by_day),
        "var_market": var_q,
        "var_model": var_model / weight_total,
        "cov": cov / weight_total,
        "ev": (var_market - cov) / weight_total,
        "k": (cov / var_market) if var_market > 0.0 else None,
        "strategy_profit": profit / weight_total,
        "spread_cost": (
            spread / spread_weight if spread_weight > 0.0 else None
        ),
        "days_ev_positive": sum(
            1 for day in by_day.values() if day["var_market"] > day["cov"]
        ),
        "by_day": by_day,
    }


def _charter_ev_reading(
    rows: Sequence[Mapping[str, Any]], *, draws: int, seed: int,
) -> dict[str, Any]:
    """The identity plus a by-day bootstrap on EV and k.

    Whole event days are resampled, as the valuation and beta lanes do: a
    fleet shares its weather, so station-days inside a day are not
    independent draws.  The interval is the 5th to 95th percentile.
    """

    reading = charter_ev_identity(rows)
    by_day = reading.pop("by_day")
    reading["ev_ci"] = None
    reading["k_ci"] = None
    keys = list(by_day)
    if len(keys) >= 2 and draws > 0:
        rng = random.Random(int(seed))
        evs: list[float] = []
        ks: list[float] = []
        for _ in range(int(draws)):
            picked = [by_day[key] for key in rng.choices(keys, k=len(keys))]
            w = sum(day["w"] for day in picked)
            vq = sum(day["var_market"] for day in picked)
            c = sum(day["cov"] for day in picked)
            if w > 0.0:
                evs.append((vq - c) / w)
            if vq > 0.0:
                ks.append(c / vq)

        def _band(values: list[float]) -> list[float] | None:
            if not values:
                return None
            ordered = sorted(values)
            lo = ordered[min(len(ordered) - 1, int(0.05 * len(ordered)))]
            hi = ordered[max(0, int(0.95 * len(ordered)) - 1)]
            return [lo, hi]

        reading["ev_ci"] = _band(evs)
        reading["k_ci"] = _band(ks)
    return reading


def collect_charter_ev(conn: sqlite3.Connection) -> dict[str, Any]:
    """Charter EV identity per stage: Var(e_q), Cov(e_p,e_q), Var - Cov, k.

    Reads the valuation lane's population -- one row per decision with the
    committed envelope's vector, the market mid vector rebuilt from the WS
    snapshot the decision priced against, and the settled bucket -- so this
    page and charter 132's fit describe the same sample.  Two readings per
    stage: the rolling 28-day research population, and the rows since the
    stage's serving-arithmetic anchor, which is the only sample that says
    anything about the vector served today.  Every row of the sample is a
    settled decision; nothing here is a fit and nothing here serves.
    """

    out: dict[str, Any] = {"available": False}
    try:
        from bayes_basket_target.center_width_store import (  # noqa: PLC0415
            served_arithmetic_anchor,
        )
        from bayes_basket_target.valuation_store import (  # noqa: PLC0415
            BOOTSTRAP_DRAWS,
            BOOTSTRAP_SEED,
            STAGE_SCOPES,
            WINDOW_DAYS,
            population,
        )
    except ImportError:
        return out
    scopes: list[dict[str, Any]] = []
    window: dict[str, Any] = {}
    for scope in ("P0", "P0-M", "P1"):
        try:
            rows, meta = population(conn, stage_scope=scope, anchored=False)
        except (sqlite3.Error, ValueError) as exc:
            scopes.append({"scope": scope, "error": f"{type(exc).__name__}:{exc}"})
            continue
        if meta.get("error"):
            scopes.append({"scope": scope, "error": str(meta["error"])})
            continue
        window = {
            "days": int(WINDOW_DAYS),
            "floor_day": meta.get("floor_day"),
            "as_of_event_date": meta.get("as_of_event_date"),
        }
        anchor: str | None = None
        anchor_state = "ok"
        try:
            anchor = served_arithmetic_anchor(
                conn, stage=STAGE_SCOPES[scope][0],
            )
        except Exception:  # noqa: BLE001 -- a broken anchor store is a reading, not a crash
            anchor_state = "error"
        since_anchor: list[Mapping[str, Any]] = []
        if anchor:
            anchor_at = datetime.fromisoformat(str(anchor).replace("Z", "+00:00"))
            since_anchor = [
                row for row in rows
                if datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00"))
                >= anchor_at
            ]
        # The first decision of each station-day, which is the reading the
        # 2026-09-12 measurement was taken on.  For P1 the two differ by
        # construction: later decisions carry the day's observations, so the
        # all-decisions row reads a better vector than the first one did.
        first_at: dict[tuple[str, str], str] = {}
        for row in rows:
            key = (str(row["station"]), str(row["date"]))
            if key not in first_at or str(row["at"]) < first_at[key]:
                first_at[key] = str(row["at"])
        first_rows = [
            dict(row, weight=1.0) for row in rows
            if first_at.get((str(row["station"]), str(row["date"])))
            == str(row["at"])
        ]
        scopes.append({
            "scope": scope,
            "research": _charter_ev_reading(
                rows, draws=BOOTSTRAP_DRAWS, seed=BOOTSTRAP_SEED,
            ),
            "first": _charter_ev_reading(
                first_rows, draws=BOOTSTRAP_DRAWS, seed=BOOTSTRAP_SEED,
            ),
            "anchor_utc": anchor,
            "anchor_state": anchor_state,
            "served": (
                _charter_ev_reading(
                    since_anchor, draws=BOOTSTRAP_DRAWS, seed=BOOTSTRAP_SEED,
                )
                if anchor else None
            ),
            "population": {
                key: meta.get(key) for key in (
                    "decisions_read", "no_label", "market_incomplete",
                    "label_outside_contract", "weather_vector_invalid",
                )
            },
        })
    out.update({
        "available": any("research" in scope for scope in scopes),
        "scopes": scopes,
        "window": window,
        "bootstrap_draws": int(BOOTSTRAP_DRAWS),
        "computed_at_utc": _now().isoformat().replace("+00:00", "Z"),
    })
    return out


def collect_window_divergence(conn: sqlite3.Connection) -> dict[str, Any]:
    """Station-days where the posterior and the held window part ways.

    The signal is the out-of-window posterior mass at the *last recompute
    where the held buckets were still alive* (mass on them above 5%) -- the
    last moment a top-up could still have acted.  A single daily maximum was
    tried first and rewarded the early morning's naturally broad posterior;
    the end-state is what the disagreement actually settled into.

    This is a disagreement detector, not a verdict.  Either side can be the
    wrong one, and both archetypes are real: RKPK 2026-08-21 held 29-32
    while the posterior (rightly) piled onto 33-34 and the day settled 34;
    KSEA 2026-08-17 held 78-79 while the model (wrongly) kept 76% at or
    above 80 through local midnight -- the hard bound only ever cuts from
    below, so a model stuck high is a wrongness no observation can remove.
    The settled column is what says who was right, station-day by
    station-day.

    The held window is what the live book filled (user ruling, 2026-09-16:
    the paper ledger is a rule comparator): every bucket a live leg bought
    that station-day, from the first fill's decision on.  The outcome is the
    day's settled live money.
    """

    book = live_book.build(conn)
    chains: dict[tuple[str, str], dict[str, Any]] = {}
    for leg in sorted(
        book["legs"], key=lambda leg: str(leg.get("decision_at_utc") or "")
    ):
        if float(leg["filled_size"] or 0.0) <= 0:
            continue
        key = (str(leg["station_id"]), str(leg["event_date"]))
        entry = chains.setdefault(key, {
            "held": set(),
            "opened": str(leg.get("decision_at_utc") or leg["created_at"]),
        })
        entry["held"].add(str(leg["bucket"]))
    money: dict[tuple[str, str], float] = defaultdict(float)
    for row in book["settlements"]:
        money[(str(row["station_id"]), str(row["event_date"]))] += float(
            row["pnl_usdc"]
        )
    settled = {
        key: {
            "hit": int(book["settled"][key][0] in chains[key]["held"]),
            "pnl": money.get(key, 0.0),
            "truth_bucket": str(book["settled"][key][0]),
        }
        for key in chains if key in book["settled"]
    }
    rows = []
    for (station, day), entry in chains.items():
        outcome = settled.get((station, day))
        if outcome is None:
            continue
        late = None
        # Stage precedence, not clock order: P0 recomputes all day but its
        # chain carries no observation, so at night its rows are forecasts
        # written after the answer was measured.  Once a P1 row exists for
        # the station-day, later P0/P0-M rows are not the end-state belief.
        p1_seen = False
        # Read p(B) from the bridge's own projection rather than rebuilding
        # each envelope body (2026-08-29).  The projection table is what
        # this module's docstring calls "a read-optimised projection" and
        # it is written from the envelope inside the same savepoint, so it
        # is the same numbers by construction -- checked value by value
        # over 600 rows sampled across the whole history, every
        # probability and ordinal identical, no row missing its
        # projection.  Rebuilding a body costs a JSON parse of the full
        # lineage plus a content hash, per decision, and this walk reads
        # about a hundred thousand of them.
        for row in conn.execute(
            "SELECT h.decision_at_utc, h.stage, h.history_id"
            " FROM basket_probability_bridge_history h"
            " WHERE h.station_id=? AND h.event_date=? AND h.state='COMMITTED'"
            " AND h.decision_at_utc>=? ORDER BY h.decision_at_utc",
            (station, day, entry["opened"]),
        ):
            if str(row[1]) == "P1":
                p1_seen = True
            elif p1_seen:
                continue
            projection = conn.execute(
                "SELECT bucket, ordinal, probability"
                " FROM basket_probability_bridge_bucket_probability"
                " WHERE history_id=?",
                (str(row[2]),),
            ).fetchall()
            if not projection:
                continue
            probabilities = {
                str(bucket): float(value)
                for bucket, _ordinal, value in projection
            }
            ordinals = {
                str(bucket): int(ordinal)
                for bucket, ordinal, _value in projection
            }
            inside = sum(
                value for bucket, value in probabilities.items()
                if bucket in entry["held"]
            )
            if inside <= 0.05:
                continue
            held_ordinals = [
                ordinals[bucket] for bucket in entry["held"]
                if bucket in ordinals
            ]
            if not held_ordinals:
                continue
            low, high = min(held_ordinals), max(held_ordinals)
            late = {
                "at": str(row[0]),
                "out": 1.0 - inside,
                "out_above": sum(
                    value for bucket, value in probabilities.items()
                    if ordinals.get(bucket, low) > high
                ),
                "out_below": sum(
                    value for bucket, value in probabilities.items()
                    if ordinals.get(bucket, high) < low
                ),
            }
        if late is None:
            continue
        rows.append({
            "station_id": station,
            "event_date": day,
            "held_buckets": sorted(entry["held"]),
            "late_out": late["out"],
            "out_above": late["out_above"],
            "out_below": late["out_below"],
            "measured_at_utc": late["at"],
            **outcome,
        })
    thresholds = []
    for threshold in (0.3, 0.4, 0.5):
        hot = [row for row in rows if row["late_out"] > threshold]
        cold = [row for row in rows if row["late_out"] <= threshold]
        if not hot or not cold:
            continue
        thresholds.append({
            "threshold": threshold,
            "hot_n": len(hot),
            "hot_hit_rate": sum(r["hit"] for r in hot) / len(hot),
            "hot_pnl": sum(r["pnl"] for r in hot),
            "cold_n": len(cold),
            "cold_hit_rate": sum(r["hit"] for r in cold) / len(cold),
            "cold_pnl": sum(r["pnl"] for r in cold),
        })
    flagged = sorted(
        (row for row in rows if row["late_out"] > 0.4),
        key=lambda row: (row["event_date"], row["late_out"]),
        reverse=True,
    )
    return {
        "evaluated_station_days": len(rows),
        "thresholds": thresholds,
        "flagged": flagged[:80],
    }


def build_payload(db: Path) -> dict[str, Any]:
    # The connection lives exactly one payload and is closed on every path.
    # It used to be leaked, one per page refresh, and a leaked read-only
    # connection is not harmless on a WAL database: any cursor left
    # unexhausted keeps a read transaction open, checkpointing can never
    # advance past the oldest reader snapshot, and the WAL grew to 417MB --
    # at which point every writer's transaction slowed enough that ordinary
    # lock contention compounded into 45-minute P1 outages (2026-08-24).
    conn = _connect(db)
    started = time.perf_counter()
    try:
        payload = _build_payload(conn, db)
    finally:
        conn.close()
    # The render's own cost, on the page.  Reported because the page is a
    # file on a thirty-minute timer, not a live query: a reader who
    # refreshes right after a change sees the PREVIOUS build until the next
    # one lands, and without these two numbers there is no way to tell that
    # from a broken section.  2026-09-09 is the case -- P1's band table
    # moved at 03:23Z, the page on screen was built at 03:07Z, and the
    # build that would have shown the move was twenty-four minutes in.
    elapsed = time.perf_counter() - started
    slowest = sorted(
        (
            {
                "section": name,
                "build_sec": float(entry["build_sec"]),
                "cached": bool(entry.get("cached")),
            }
            for name, entry in _SECTION_CACHE_LOG.items()
            if entry.get("build_sec") is not None
        ),
        key=lambda row: -row["build_sec"],
    )
    payload["render"] = {
        "generated_at_utc": payload.get("generated_at_utc"),
        "build_sec": round(elapsed, 1),
        # What was actually computed this round.  A cached section that was
        # reused contributes nothing here, which is the point: the sum is
        # what THIS build paid, not what a cold build would.
        "computed_sec": round(sum(r["build_sec"] for r in slowest), 1),
        "computed_sections": len(slowest),
        "reused_sections": sum(
            1 for e in _SECTION_CACHE_LOG.values() if e.get("reused")
        ),
        "slowest": slowest[:12],
    }
    return payload


REGIONAL_SWEEP_JSON = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "regional_hires_historical_oos.json"
)
# The four regional point systems and how each one's archive behaves.
# ``backfillable`` decides how a stale lane reads: the NOAA buckets and
# the RISH mirror keep history, so a stale lane there is a delay; the DWD
# directory keeps only the newest run of each cycle, so a stale lane
# there is runs permanently lost.
REGIONAL_LANES = {
    "JMA_MSM_POINT": {
        "label": "JMA MSM 5km", "region": "东亚",
        "transport": "RISH 京都镜像·字节区间", "backfillable": True},
    "NCEP_HRRR_POINT": {
        "label": "HRRR 3km", "region": "美国",
        "transport": "NOAA S3·idx 区间", "backfillable": True},
    "NCEP_NBM_POINT": {
        "label": "NBM 混合", "region": "美国",
        "transport": "NOAA S3·idx 区间", "backfillable": True},
    "DWD_ICON_EU_POINT": {
        "label": "ICON-EU 7km", "region": "欧洲",
        "transport": "DWD opendata·全域场", "backfillable": False},
}
_SWEEP_MODEL_TO_SYSTEM = {
    "jma_msm": "JMA_MSM_POINT",
    "ncep_hrrr_conus": "NCEP_HRRR_POINT",
    "ncep_nbm_conus": "NCEP_NBM_POINT",
    "dwd_icon_eu": "DWD_ICON_EU_POINT",
}


def _capture_rosters() -> dict[str, set[str]]:
    """Which stations each lane is RULED IN at -- read from the lanes.

    Not inferred from which rows exist.  A station whose lane started an
    hour ago, or whose turn the per-pass cap has not reached, has no rows
    and is not a reversal; reading the roster off the corpus would print
    "measured reversed" over stations that were admitted.  The rosters
    live in the archive modules for exactly this second reader.
    """

    from bayes_basket_target import noaa_conus_archive
    from bayes_basket_target import dwd_icon_eu_point_archive
    from bayes_basket_target import jma_msm_archive  # noqa: F401

    rosters = {
        "NCEP_HRRR_POINT": {
            s[0] for s in noaa_conus_archive.CAPTURE_STATIONS["HRRR"]
        },
        "NCEP_NBM_POINT": {
            s[0] for s in noaa_conus_archive.CAPTURE_STATIONS["NBM"]
        },
        "DWD_ICON_EU_POINT": {
            s[0] for s in dwd_icon_eu_point_archive.CAPTURE_STATIONS
        },
        "JMA_MSM_POINT": {
            s[0] for s in getattr(
                jma_msm_archive, "CAPTURE_STATIONS", ()
            )
        },
    }
    return rosters


def collect_regional_lanes(conn: sqlite3.Connection) -> dict[str, Any]:
    """The regional capture lanes: corpus growth, race state, verdicts.

    Three questions in one section, because they are one story read at
    three moments: what the 46-pair sweep measured (the reason a lane
    exists), what each lane has captured so far (the corpus the gates
    will re-ask), and where each system stands in the OOS race (what a
    win will buy -- envelope evidence, never a membership seat).
    """

    lanes = []
    rosters = _capture_rosters()
    captured_stations: dict[str, set[str]] = {}
    for system_id, meta in REGIONAL_LANES.items():
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT station_id),"
            " COUNT(DISTINCT station_id || '|' || target_local_date),"
            " MAX(archive_query_at_utc), MAX(native_run_init_utc)"
            " FROM calibration_native_ensemble_historical_member_run"
            " WHERE ensemble_system_id=?",
            (system_id,),
        ).fetchone()
        rows, stations, station_days, last_query, last_run = row
        captured_stations[system_id] = {
            str(r[0]) for r in conn.execute(
                "SELECT DISTINCT station_id"
                " FROM calibration_native_ensemble_historical_member_run"
                " WHERE ensemble_system_id=?",
                (system_id,),
            )
        }
        age = _age_hours(last_query)
        # A lane is judged by whether the hourly sweep is still landing
        # rows, on the clock of the product it captures (six-hourly runs,
        # a few hours of publication lag).  For the DWD lane a red is not
        # a delay but permanent loss, which the note says out loud.
        if rows == 0:
            health = "amber"
            note = "道已建，等第一批行落库"
        elif age is not None and age <= 26:
            health = "green"
            note = ""
        elif age is not None and age <= 50:
            health = "amber"
            note = "超过一天没有新行"
        else:
            health = "red"
            note = (
                "断采——DWD 不留历史，错过的 run 永久丢失"
                if not meta["backfillable"]
                else "断采——归档还在，可回填"
            )
        lanes.append({
            "system_id": system_id, **meta,
            "row_count": rows, "station_count": stations,
            # The roster's size beside the count that has rows: a lane
            # at 1/10 is either young or starved, and either way that is
            # the number to look at, not the row total.
            "roster_count": len(rosters.get(system_id, set())),
            "station_days": station_days,
            "last_capture_age_hours": age,
            "last_run_init": last_run,
            "health": health, "note": note,
        })

    # Race state, from the worker's own OOS ingress readiness -- the same
    # rows the admission gates read, not a reimplementation of them.
    readiness = []
    if STATUS_JSON.is_file():
        try:
            status = json.loads(STATUS_JSON.read_text())
            for entry in (
                status.get("production", {})
                .get("native_ensemble_oos_ingress", {})
                .get("readiness") or []
            ):
                if entry.get("ensemble_system_id") in REGIONAL_LANES:
                    readiness.append(entry)
        except (ValueError, OSError):
            pass

    # The sweep's verdicts, pinned: which pairs earned a lane and which
    # measured reversed.  Only the four systems that HAVE lanes -- the
    # rest of the 46 pairs stay in the evidence file.
    verdicts = []
    if REGIONAL_SWEEP_JSON.is_file():
        try:
            sweep = json.loads(REGIONAL_SWEEP_JSON.read_text())
            for candidate in sweep.get("candidates") or []:
                system_id = _SWEEP_MODEL_TO_SYSTEM.get(
                    str(candidate.get("model"))
                )
                if system_id is None:
                    continue
                station = str(candidate.get("station_id"))
                morning = candidate.get("morning_argmax") or {}
                verdicts.append({
                    "system_id": system_id,
                    "system_label": REGIONAL_LANES[system_id]["label"],
                    "station_id": station,
                    "passes_all_three": bool(
                        candidate.get("passes_all_three")
                    ),
                    "gates": candidate.get("gates") or {},
                    "holdout_days": candidate.get("holdout_days"),
                    "candidate_exact": (morning.get("candidate") or {})
                    .get("exact"),
                    "incumbent_exact": (morning.get("incumbent") or {})
                    .get("exact"),
                    "candidate_within_1": (morning.get("candidate") or {})
                    .get("within_1"),
                    # Two different questions, kept apart on purpose.
                    # ``in_roster`` is the ruling (is this pair captured
                    # at all); ``has_rows`` is progress (has its turn
                    # come yet).  Collapsing them prints "measured
                    # reversed" over stations that were admitted an hour
                    # ago and simply have no rows yet.
                    "in_roster": station in rosters.get(system_id, set()),
                    "has_rows": station in captured_stations[system_id],
                })
        except (ValueError, OSError):
            pass
    verdicts.sort(
        key=lambda v: (
            not v["in_roster"], not v["has_rows"],
            -(v["candidate_exact"] or 0.0),
        )
    )
    return {"lanes": lanes, "readiness": readiness, "verdicts": verdicts}


# ---------------------------------------------------------------------------
# Section cache
#
# A render rebuilds every section from scratch, and the hosted sweep starts
# one every thirty minutes; measured 2026-09-02 a full build took about
# fourteen minutes, most of it recomputing answers that had not moved since
# the previous build.  Sections whose inputs change on the order of hours
# keep their last value on disk and are recomputed only when it ages out.
#
# What may be cached is decided by two safety rules, not by cost:
#
#   * Nothing that feeds ``alerts`` / ``overall_health``.  The banner is the
#     one thing on this page a reader trusts to mean "now" -- production sat
#     BLOCKED_FAIL_CLOSED for eleven hours behind a healthy heartbeat on
#     2026-08-26 -- so members, chain, band stages and truth materialization
#     are rebuilt every render however long they take.
#   * Nothing about the live book.  Live readiness, open orders and the
#     whole selection payload are what the page is opened for mid-session.
#
# And every cached section carries the moment it was computed, which the
# footer prints, so a cache that quietly stops refreshing reads as stale
# rather than as current.
SECTION_CACHE_DIR = ROOT / "data" / "dashboard_section_cache"
# Bumped by hand when a helper a cached collector calls changes without the
# collector's own body changing -- the fingerprint below sees the collector
# and nothing it calls.  A bump discards every cached section.
SECTION_CACHE_EPOCH = 1
# Lifetimes, deliberately unequal.  Renders come round every thirty minutes;
# equal lifetimes would expire the whole set on one render and put the
# fourteen minutes back in a single go.
SECTION_CACHE_TTL_SEC = {
    "beta_shadow": 1800,
    "truth_landing": 150 * 60,
    "scorecard": 180 * 60,
    "h5_readiness": 195 * 60,
    "skill": 210 * 60,
    # Refits the fleet's bias under each half-life (~5 min); the verdict
    # moves with a settled day at most.
    "bias_half_life": 1440 * 60,
    "band_residual_audit": 240 * 60,
    "center_landing": 255 * 60,
    "convergence_claim": 270 * 60,
    # One pass over a week of decision rows, no joins: cheap, and the thing
    # it reports (a shadow gate's refusals) moves every cycle.
    "market_lean": 20 * 60,
    # 5,130 settled candidate-window facts, each one a JSON payload that
    # has to be parsed to reach the seated window: ~19s, and the reading
    # it produces moves on the scale of days, not cycles.
    "lambda_axis_realisation": 285 * 60,
    # Three small tables and the last 300 plan headers; the verdict moves
    # daily at most.
    "valuation_lane": 50 * 60,
    # Three small tables plus one population read per stage for the forward
    # skill score; the journal moves once a day.  Five minutes off the
    # valuation lane's so the two do not expire on the same render.
    "posterior_lane": 55 * 60,
    # 秤砣线 (charter 146): one small table and the live book the money
    # board already built; the lane moves every production cycle.
    "tiebreak_lane": 4 * 60,
    # Three populations of settled decisions with their books (~60s on the
    # 45GB store); every row is a settled station-day, so the reading moves
    # once a day when the truth lands.
    "charter_ev": 300 * 60,
    # The per-table growth walk counts rows over a day on ten tables, two
    # of which have no index on their time column.  The gauge above it is
    # read from status.json every build and is never cached; only the
    # composition is, and a composition that is half an hour old still
    # names the same tables.
    #
    # Measured 2026-09-17: the largest cached section of the page, by its
    # own build_sec -- 397s in the 12:14Z render, beside a production
    # cycle and a ledger rebuild; 77s read-only at 13:41Z with the disk
    # quiet.  All ten day-counts are scans (eight over covering indexes,
    # the registry and the settlement table over the table itself), and
    # the two maker_shadow tables alone are 36s of the quiet figure.  On a
    # 30-minute render clock a 45-minute TTL rebuilt the walk on nearly
    # every render, ~32 times a day.  The composition moves on the scale
    # of days -- the retention ruling reads it once a day at most -- so six
    # hours costs the reading nothing and leaves four walks a day.
    "ssot_capacity": 6 * 60 * 60,
}
# Set by ``--no-section-cache`` so a reader who needs every number computed
# against this instant can have it, at the price of the long build.
SECTION_CACHE_ENABLED = True
_SECTION_CACHE_LOG: dict[str, dict[str, Any]] = {}


def _loaded_source(obj: Any) -> str:
    """The source of ``obj`` as this process loaded it, never as the disk has it now.

    ``inspect.getsource`` re-reads the file whenever its mtime has moved and
    slices it at the line numbers the running code was compiled with.  A render
    takes forty minutes, and this file is edited by several sessions a day, so
    an edit landing mid-render handed the fingerprint a slice of the NEW file at
    the OLD offsets: a fragment that fails to tokenize and kills the render
    (2026-09-14: the 05:56Z run died on ``TokenError`` at 06:11Z, minutes after
    an edit to this file, and the page fell 91 minutes behind), or one that
    happens to parse and fingerprints the wrong function, which misses every
    cache and makes the render slower.

    A function defined in this module is sliced from the snapshot taken at
    import, whose line numbers are the running code's own.  Anything else goes
    through ``inspect`` and, if that fails for any reason, through its repr --
    a fingerprint is a cache key, and a render must never die computing one.
    """

    code = getattr(obj, "__code__", None)
    if (
        code is not None
        and _LOADED_SOURCE_LINES
        and getattr(obj, "__globals__", None) is globals()
    ):
        start = int(code.co_firstlineno) - 1
        if 0 <= start < len(_LOADED_SOURCE_LINES):
            try:
                return "".join(
                    inspect.getblock(_LOADED_SOURCE_LINES[start:])
                )
            except Exception:  # noqa: BLE001 - a cache key, never a failure
                pass
    try:
        return inspect.getsource(obj)
    except Exception:  # noqa: BLE001 - a cache key, never a failure
        return repr(obj)


def _section_fingerprint(collector: Any, salt: str = "") -> str:
    """What the cached value was computed from, as one hash.

    The collector's own source, plus whatever the caller declares it also
    reads.  Only the source is free to find; a module-level constant the
    collector merely names -- a band grid, a threshold -- is invisible here,
    so editing one would leave a stale file that still matches its
    fingerprint and keeps serving the retired shape until its TTL runs out.
    Callers pass those in as ``_salt``.
    """

    source = _loaded_source(collector)
    return hashlib.sha256(
        f"{SECTION_CACHE_EPOCH}\n{salt}\n{source}".encode("utf-8")
    ).hexdigest()


def _cached_section(
    name: str,
    collector: Any,
    *args: Any,
    _salt: str = "",
    **kwargs: Any,
) -> Any:
    """The collector's value, reused from disk while it is still young.

    A miss round-trips the fresh value through JSON before returning it, so
    a hit and a miss hand the rest of the build byte-identical structures --
    otherwise a tuple would reach the page as a tuple on the render that
    computed it and as a list on the five that reused it.
    """

    ttl = SECTION_CACHE_TTL_SEC.get(name)
    if not ttl or not SECTION_CACHE_ENABLED:
        # Still timed.  Only the nine sections with a TTL passed through
        # here, so the nine that were already the known-slow ones were the
        # only ones anybody could see the cost of -- and the render was 24
        # minutes on 2026-09-09 with no record of where the other minutes
        # went.  A section without a lifetime is not a section without a
        # price.
        started = time.perf_counter()
        value = collector(*args, **kwargs)
        _SECTION_CACHE_LOG[name] = {
            "build_sec": round(time.perf_counter() - started, 1),
            "reused": False,
            "cached": False,
        }
        return value
    fingerprint = _section_fingerprint(collector, _salt)
    path = SECTION_CACHE_DIR / f"{name}.json"
    now = _now()
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        stamp = str(blob["computed_at_utc"])
        age = (
            now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        ).total_seconds()
        if blob.get("fingerprint") == fingerprint and 0.0 <= age < ttl:
            _SECTION_CACHE_LOG[name] = {
                "computed_at_utc": stamp,
                "age_sec": round(age),
                "ttl_sec": ttl,
                "reused": True,
                "cached": True,
            }
            return blob["value"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    started = time.perf_counter()
    value = collector(*args, **kwargs)
    elapsed = time.perf_counter() - started
    stamp = now.isoformat().replace("+00:00", "Z")
    _SECTION_CACHE_LOG[name] = {
        "computed_at_utc": stamp,
        "age_sec": 0,
        "ttl_sec": ttl,
        "reused": False,
        "cached": True,
        "build_sec": round(elapsed, 1),
    }
    try:
        value = json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        # Not representable, so it cannot be cached either.  Serve it and
        # say so rather than pretending the section is cached.
        _SECTION_CACHE_LOG[name]["reused"] = False
        _SECTION_CACHE_LOG[name]["uncacheable"] = True
        return value
    try:
        SECTION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for stale in SECTION_CACHE_DIR.glob(f"{path.name}.*.tmp"):
            # A render killed mid-write leaves one of these; nothing reads
            # them, so they would accumulate silently forever.
            try:
                if stale.stat().st_mtime < time.time() - 6 * 3600:
                    stale.unlink()
            except OSError:
                pass
        # Unique per process: the hosted sweep and a hand-run render can
        # overlap (measured: two builds in flight at 04:10 on 2026-09-02),
        # and a shared temp name lets one replace the file the other is
        # still writing.  Distinct names make last-writer-wins the only
        # outcome, and both writers leave a complete file behind.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "schema": "dashboard_section_cache_v1",
                    "section": name,
                    "fingerprint": fingerprint,
                    "computed_at_utc": stamp,
                    "build_sec": round(elapsed, 1),
                    "value": value,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except (OSError, TypeError, ValueError):
        pass
    return value


def _build_payload(conn: sqlite3.Connection, db: Path) -> dict[str, Any]:
    _SECTION_CACHE_LOG.clear()
    cadence = _cached_section("run_cadence", collect_run_cadence, conn)
    members = _cached_section("members", collect_members, conn, cadence)
    tables = _cached_section(
        "correction_and_weights", collect_correction_and_weights, conn
    )
    stages = _cached_section("band_stages", collect_band_stages, conn)
    band_shadow = _cached_section(
        "band_shadow", collect_band_shadow, conn
    )
    chain = _cached_section("chain", collect_chain, conn)
    hosted_sweeps = _cached_section(
        "hosted_sweeps", collect_hosted_sweeps, conn
    )
    ssot_capacity = _cached_section(
        "ssot_capacity", collect_ssot_capacity, conn
    )
    delta_v2_authority = _cached_section(
        "delta_v2_authority", collect_delta_v2_authority, conn
    )
    gaps = _cached_section(
        "gaps", collect_gaps, conn, members, stages, tables
    )
    skill = _cached_section("skill", collect_model_skill, conn)
    truth = _cached_section(
        "truth_materialization", collect_truth_materialization, conn
    )
    scores = _cached_section("oos_scores", collect_oos_scores, conn)
    serving_weights = _cached_section(
        "serving_weights", collect_serving_weights, conn
    )
    center_width = _cached_section(
        "center_width", collect_center_width, conn
    )
    center_landing = _cached_section(
        "center_landing", collect_center_landing, conn,
        _salt=_center_landing_salt(),
    )
    scorecard = _cached_section(
        "scorecard", collect_calibration_scorecard, conn,
        _salt=_scorecard_salt(conn),
    )
    truth_landing = _cached_section(
        "truth_landing", collect_truth_landing, conn,
        # The band grid lives outside the collector, so it has to be
        # declared: changing it has to expire the file, not wait it out.
        _salt=repr(TRUTH_LANDING_PRICE_BANDS)
        + repr(TRUTH_LANDING_GAP_GROUPS) + repr(TRUTH_LANDING_UNLIKELY),
    )
    worker = {}
    if STATUS_JSON.is_file():
        try:
            status = json.loads(STATUS_JSON.read_text())
            worker = {
                "heartbeat_at": status.get("heartbeat_at"),
                "heartbeat_age_hours": _age_hours(
                    status.get("heartbeat_at")
                ),
                "pid": status.get("pid"),
                "live_submit_authorized": status.get(
                    "live_submit_authorized"
                ),
                **_production_health(status),
                **_taf_capture_health(status),
                **_nwp_rotation_health(status),
                **_official_native_family_coverage(status),
            }
        except (ValueError, OSError):
            worker = {}
    # The banner used to say "something below is red" and leave the reader
    # to scroll sixteen sections for the one red word.  Collect the
    # non-green rows from exactly the four sources the light is computed
    # from, so the banner can name them -- and derive the light from this
    # list, so the two can never disagree.
    alerts = (
        [
            {"health": m["health"], "label": f"模型 {m['label']}",
             "note": m.get("note") or ""}
            for m in members if m["health"] != "green"
        ]
        + [
            {"health": c["health"], "label": f"链路·{c['name']}",
             "note": c.get("note") or ""}
            for c in chain if c["health"] != "green"
        ]
        + [
            {"health": s["health"], "label": f"band 校准 {s['stage']}",
             "note": s.get("state") or ""}
            for s in stages if s["health"] != "green"
        ]
        + (
            [{"health": truth["health"], "label": "真值物化",
              "note": truth.get("note") or ""}]
            if truth["health"] != "green" else []
        )
        # The resident's own health.  It was absent from this list until
        # 2026-08-26, when production sat BLOCKED_FAIL_CLOSED for eleven
        # hours behind a healthy heartbeat: the page showed a live pid and
        # a fresh timestamp while nothing was being produced, and the
        # outage surfaced only because someone audited refusals by hand.
        # A heartbeat says the process is alive; it does not say the
        # pipeline is working.
        + (
            [{"health": worker["production_health"],
              "label": "生产周期",
              "note": worker.get("production_note") or ""}]
            if worker.get("production_health") not in (None, "green") else []
        )
        # Supply, beside production.  A stalled TAF lane does not stop the
        # cycle -- it makes the cycle refuse scopes for a reason that names
        # the probability stage, so it has to be reported where the refusal
        # is read rather than left to be inferred from it.
        + (
            [{"health": worker["taf_capture_health"],
              "label": "TAF 采集",
              "note": worker.get("taf_capture_note") or ""}]
            if worker.get("taf_capture_health") not in (None, "green") else []
        )
        # The resident's maintenance lanes.  A lane that is taking its turn
        # and failing takes everyone behind it down with it under an
        # exclusive write domain, and it had no symptom anywhere on this
        # page until 2026-09-10.  Only the red ones, and a merely starved
        # lane only once the generation has lived an hour: every lane looks
        # starved a minute after a restart, and saying so would train the
        # banner to be ignored on exactly the days it matters.
        + [
            {"health": "red", "label": f"扫描车道·{lane['name']}",
             "note": lane.get("note") or ""}
            for lane in (hosted_sweeps.get("lanes") or [])
            if lane.get("health") == "red"
            and (
                lane.get("stuck")
                or (lane.get("last_state") or "ok") != "ok"
                or (hosted_sweeps.get("generation_age_sec") or 0.0) >= 3600.0
            )
        ]
        # The execution authority's clock.  An end here is a fleet-wide
        # refusal on a date, with no earlier symptom anywhere downstream.
        + (
            [{"health": delta_v2_authority.get("health") or "amber",
              "label": "执行授权有效期",
              "note": delta_v2_authority.get("note") or ""}]
            if delta_v2_authority.get("health") != "green" else []
        )
        # The other supply lane, and the one whose stall has no other
        # symptom on this page: a stopped NWP rotation surfaces three layers
        # downstream as latest_stage_stale_fail_closed, which names a stage
        # selector and says nothing about a capture that never happened.
        + (
            [{"health": worker["nwp_rotation_health"],
              "label": "NWP 轮转",
              "note": worker.get("nwp_rotation_note") or ""}]
            if worker.get("nwp_rotation_health") not in (None, "green") else []
        )
        # A packet that shipped short of families.  Distinct from every row
        # above: the rotation advanced, the lane is alive, each family is
        # inside its own cadence, and the packets a decision reads still
        # carry two of four.  On 2026-09-10 that was true of all 37 of the
        # D+2 packets while this banner stayed silent.
        + (
            [{"health": worker["native_inbox_health"],
              "label": "包里的家族数",
              "note": "；".join(
                  f"{row['event_date']} 只有 {row['family_count']}"
                  f"/{row['expected_family_count']} 家"
                  + (f"（缺 {'、'.join(row['absent'])}）"
                     if row["absent"] else "")
                  for row in (worker.get("native_inbox_coverage") or [])
                  if row["event_date"]
                  in (worker.get("native_inbox_short_near_dates") or [])
              )}]
            if worker.get("native_inbox_health") not in (None, "green") else []
        )
    )
    overall = (
        "red" if any(a["health"] == "red" for a in alerts)
        else ("amber" if alerts else "green")
    )
    # 每日统计 and 逐站逐日明细 read the live book (user ruling, 2026-09-16).
    live_days = _cached_section("live_days", collect_live_days, conn)
    live_selection_payload = _cached_section(
        "live_selection", live_selection.build, conn
    )
    return {
        "schema": "calibration_dashboard_v2",
        "generated_at_utc": _now().isoformat().replace("+00:00", "Z"),
        "overall_health": overall,
        "alerts": alerts,
        "live_selection": live_selection_payload,
        "daily": live_days["daily"],
        "live_roster": live_days["roster"],
        "members": members,
        "tables": tables,
        "truth_landing": truth_landing,
        "band_residual_audit": (band_audit := _cached_section(
            "band_residual_audit", collect_band_residual_audit, conn,
        )),
        "live_readiness": _cached_section(
            "live_readiness", collect_live_readiness,
            conn, band_audit.get("stations") or [],
        ),
        "tiebreak_lane": _cached_section(
            "tiebreak_lane", collect_tiebreak_lane, conn
        ),
        "stages": stages,
        "band_shadow": band_shadow,
        "chain": chain,
        "hosted_sweeps": hosted_sweeps,
        "ssot_capacity": ssot_capacity,
        "delta_v2_authority": delta_v2_authority,
        "gaps": gaps,
        "skill": skill,
        "truth": truth,
        "scores": scores,
        "serving_weights": serving_weights,
        "center_width": center_width,
        "center_landing": center_landing,
        "scorecard": scorecard,
        "h5_readiness": _cached_section(
            "h5_readiness", collect_h5_readiness, conn
        ),
        "bias_half_life": _cached_section(
            "bias_half_life", collect_bias_half_life, conn
        ),
        "window_divergence": _cached_section(
            "window_divergence", collect_window_divergence, conn
        ),
        "effective_edge_lambda": _cached_section(
            "effective_edge_lambda", collect_effective_edge_lambda, conn
        ),
        "lambda_axis_usage": _cached_section(
            "lambda_axis_usage", collect_lambda_axis_usage, conn
        ),
        "lambda_axis_realisation": _cached_section(
            "lambda_axis_realisation", collect_lambda_axis_realisation, conn,
            # The thermometer's arithmetic lives in a helper the fingerprint
            # cannot see; without this a change to it keeps serving the
            # retired shape for the section's whole lifetime.
            _salt=_loaded_source(_claim_edge_calibration),
        ),
        "valuation_lane": _cached_section(
            "valuation_lane", collect_valuation_lane, conn
        ),
        "posterior_lane": _cached_section(
            "posterior_lane", collect_posterior_lane, conn
        ),
        "charter_ev": _cached_section(
            "charter_ev", collect_charter_ev, conn,
            # The arithmetic lives in two helpers the fingerprint cannot
            # see; without this a change to either keeps serving the old
            # reading for the section's whole lifetime.
            _salt=_loaded_source(charter_ev_identity)
            + _loaded_source(_charter_ev_reading),
        ),
        "beta_shadow": _cached_section(
            "beta_shadow", collect_beta_shadow, conn
        ),
        "convergence_claim": _cached_section(
            "convergence_claim", collect_convergence_claim, conn
        ),
        "market_lean": _cached_section(
            "market_lean", collect_market_lean, conn
        ),
        # The account, off the live book (user ruling, 2026-09-16).  The
        # paper book's own risk metrics and nine numbers left the page with
        # it: the ledger is a rule comparator, not the account.
        "live_money": _cached_section(
            "live_money", collect_live_money, conn
        ),
        "regional": _cached_section(
            "regional", collect_regional_lanes, conn
        ),
        "worker": worker,
        "section_cache": dict(_SECTION_CACHE_LOG),
    }


HTML_TEMPLATE = r"""<!doctype html><!--render-start:__RENDER_STARTED__--><!--payload-start:__PAYLOAD_STARTED__-->
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>气温篮子全链看板</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--soft:rgba(38,43,54,.6);
--text:#e6e9ef;--muted:#9aa4b8;--green:#3fb950;--amber:#d29922;--red:#f85149;
--accent:#58a6ff;--up:#7ee787;--down:#e3b341;--violet:#d2a8ff;
--th-claim:#3987e5;--th-price:#d95926;
--pill-green-bg:rgba(63,185,80,.14);--pill-green-fg:#7ee787;
--pill-amber-bg:rgba(210,153,34,.16);--pill-amber-fg:#e3b341;
--pill-red-bg:rgba(248,81,73,.15);--pill-red-fg:#ff7b72;
--pill-violet-bg:rgba(188,140,255,.16);
--why-bg:rgba(88,166,255,.07);--why-fg:#c9d4e5;--bar-bg:#232833;
--shadow:none;
/* Scrollbars, form controls and the ground the UA paints follow this, not
   the tokens above.  Omitting it left the board dark with light scrollbars
   wherever a host stylesheet had declared `color-scheme: light` -- which is
   what the Artifact shell does. */
color-scheme:dark}
/* The light palette is not the dark one inverted: on a white ground the
   same hues read washed out and the greens stop being distinguishable from
   the ambers, so each accent is darkened to its own readable weight. */
[data-theme="light"]{--bg:#f6f7f9;--card:#ffffff;--line:#e1e4ea;
--soft:rgba(140,149,165,.28);--text:#1c2029;--muted:#5b6472;--green:#1a7f37;
--amber:#9a6700;--red:#cf222e;--accent:#0969da;--up:#1a7f37;--down:#9a6700;
--violet:#8250df;
--th-claim:#2a78d6;--th-price:#eb6834;
--pill-green-bg:rgba(26,127,55,.11);--pill-green-fg:#116329;
--pill-amber-bg:rgba(154,103,0,.13);--pill-amber-fg:#7d4e00;
--pill-red-bg:rgba(207,34,46,.10);--pill-red-fg:#a40e26;
--pill-violet-bg:rgba(130,80,223,.12);
--why-bg:rgba(9,105,218,.06);--why-fg:#2b3648;--bar-bg:#e6e9ef;
--shadow:0 1px 2px rgba(27,31,36,.06);color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 72px}
h1{font-size:23px;margin:0 0 4px}
h2{font-size:16px;margin:22px 0 4px;display:flex;align-items:center;gap:8px}
.sub{color:var(--muted);font-size:13px;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);
border-radius:9px;padding:10px 12px;margin-bottom:8px;box-shadow:var(--shadow)}
/* The station cell's link to its own book.  Undecorated until hover so a
   table of forty-five stations does not read as forty-five links, and
   coloured from the accent token so it stays legible on both grounds
   rather than falling back to the browser's blue on a dark card. */
a.mkt{color:var(--accent);text-decoration:none}
a.mkt:hover,a.mkt:focus-visible{text-decoration:underline}
a.mkt:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.grid{display:grid;gap:8px}
.g4{grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
/* .g2 was written at two call sites and never defined here, so both fell
   back to bare `.grid` -- one column, every cell stacked, and the right
   two thirds of the card blank (user report, 2026-08-31).  Same auto-fit
   family as its siblings: the tracks that have no cell collapse and the
   ones that do share the whole row, so a three- or four-cell card fills
   the width instead of leaving a margin nothing lives in. */
.g2{grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none}
/* Status lamps (user ruling, 2026-09-11).  A lamp is the whole section
   when the section is only ever read for one binary -- is it moving.  Same
   dot as everywhere else on the page, so a colour means the same thing
   here as it does inside a table. */
.lamps{display:grid;gap:6px;
grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.lamp{display:flex;align-items:center;gap:8px;padding:7px 10px;
background:var(--card);border:1px solid var(--line);border-radius:7px}
.lamp b{font-weight:620;white-space:nowrap}
.lamp .rd{margin-left:auto;color:var(--muted);font-size:12px;
white-space:nowrap;font-variant-numeric:tabular-nums}
.lamp.bad{border-color:var(--red)}
/* The head deck (user ruling, 2026-09-11).  The lamps answer "is anything
   broken" and that is read on arrival, before a tab is chosen -- so they
   sit above the tab bar rather than inside two groups a click away.  A
   caption per row instead of a heading: this is the head, not a section. */
.lampdeck{margin-top:9px;padding-top:9px;border-top:1px solid var(--line)}
.lampdeck + .lampdeck{margin-top:9px}
.lampcap{display:block;color:var(--muted);font-size:11.5px;margin-bottom:5px}
/* Maintenance-lane health (user ruling, 2026-09-14: "sweep前端显式健康度").
   One row per hosted sweep.  The bar is the lane's OWN clock: each quarter is
   one of its intervals since it last completed, so a daily refit and a
   three-hourly ledger read on the same ruler.  Green quarter = inside its
   contract, amber = two turns missed, red = three or more. */
.swhead{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;
margin:2px 0 8px;font-size:12.5px}
.swrows{display:grid;gap:4px}
.swrow{display:grid;align-items:center;gap:4px 10px;font-size:12.5px;
grid-template-columns:minmax(150px,1.3fr) 76px minmax(150px,2fr) minmax(130px,1.1fr)}
.swname{display:flex;gap:7px;align-items:center;min-width:0}
.swname b{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.swdom{color:var(--muted);font-size:11.5px;white-space:nowrap}
.swbar{position:relative;height:10px;border-radius:5px;background:linear-gradient(
90deg,var(--pill-green-bg) 0 25%,var(--bar-bg) 25% 50%,
var(--pill-amber-bg) 50% 75%,var(--pill-red-bg) 75% 100%)}
.swbar::before{content:"";position:absolute;inset:0;border-radius:5px;
background:repeating-linear-gradient(90deg,transparent 0 calc(25% - 1px),
var(--line) calc(25% - 1px) 25%)}
.swm{position:absolute;top:50%;width:12px;height:12px;border-radius:50%;
transform:translate(-50%,-50%);border:2px solid var(--card)}
.swrun{position:absolute;left:0;bottom:-5px;height:3px;border-radius:2px;
background:var(--accent)}
.swstate{color:var(--muted);white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}
.swstate.hot{color:var(--text);font-weight:560}
.swaxis{position:relative;height:13px;font-size:10.5px;color:var(--muted)}
.swaxis em{position:absolute;top:0;transform:translateX(-50%);font-style:normal;
white-space:nowrap}
@media (max-width:760px){
.swrow{grid-template-columns:1fr auto}
.swrow .swdom{display:none}
.swrow .swbar{grid-column:1/-1;order:3}
.swrow .swaxisrow{display:none}
}
.green{background:var(--green)}.amber{background:var(--amber)}
.red{background:var(--red)}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;
border:1px solid var(--line);color:var(--muted)}
.pill.green{background:var(--pill-green-bg);color:var(--pill-green-fg);
border-color:transparent}
.pill.amber{background:var(--pill-amber-bg);color:var(--pill-amber-fg);
border-color:transparent}
.pill.red{background:var(--pill-red-bg);color:var(--pill-red-fg);
border-color:transparent}
/* The layer strip that opens every section (user ruling, 2026-09-13).
   Twenty sections is twenty different questions, and the two things a
   reader needs BEFORE the first number are which layer the section belongs
   to and whether what it describes is actually running.  A shadow lane
   that reads like a live one is the expensive failure here -- the page has
   carried both for weeks with nothing but the heading to tell them apart
   -- so the status is a word as well as a colour, and the words are the
   same four everywhere: 已上线 / 影子 / 已关闭 / 读数. */
.lane{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
margin:0 0 11px;padding:7px 11px;border-radius:8px;font-size:12.5px;
background:var(--card);border:1px solid var(--line);
border-left:3px solid var(--line)}
.lane[data-lane="live"]{border-left-color:var(--green)}
.lane[data-lane="shadow"]{border-left-color:var(--amber)}
.lane[data-lane="off"]{border-left-color:var(--red)}
.lane-l{font-weight:640;color:var(--text);white-space:nowrap}
.lane-r{color:var(--muted);flex:1 1 220px;min-width:200px}
.lane-s{white-space:nowrap;display:inline-flex;align-items:baseline;gap:6px}
.lane-s .muted{font-size:11.5px}
/* The same state on the jump chip, so the bar says it too. */
.jump button[data-lane="live"]::after,
.jump button[data-lane="shadow"]::after,
.jump button[data-lane="off"]::after{content:"";display:inline-block;
width:6px;height:6px;border-radius:50%;margin-left:5px;vertical-align:1px}
.jump button[data-lane="live"]::after{background:var(--green)}
.jump button[data-lane="shadow"]::after{background:var(--amber)}
.jump button[data-lane="off"]::after{background:var(--red)}
/* The live flag reads as a switch because it is one: a control with two
   states and no third.  The old button said "切换" next to a word saying
   OFF, which asks the reader to work out which of the two the button is
   about to do.  A switch shows the state and the action as the same
   object. */
.lc-sw{position:relative;width:34px;height:18px;padding:0;border-radius:99px;
border:1px solid var(--line);background:var(--line);cursor:pointer;
transition:background .13s ease;flex:none}
.lc-sw[aria-checked="true"]{background:var(--red);border-color:transparent}
.lc-sw::after{content:"";position:absolute;top:1px;left:1px;width:14px;
height:14px;border-radius:99px;background:#fff;
box-shadow:0 1px 2px rgba(0,0,0,.35);transition:transform .13s ease}
.lc-sw[aria-checked="true"]::after{transform:translateX(16px)}
.lc-sw:disabled{opacity:.4;cursor:not-allowed}
.lc-sw:focus-visible{outline:2px solid var(--text);outline-offset:2px}
/* The station gate, three switches per row.  A 34px slider each would be
   110px of chrome on a 45-row table, so these carry their state in the fill
   instead of a knob -- same contract as .lc-sw (the state and the action are
   one object), drawn small enough to live in a cell.  Off is an outline, on
   is the colour of what it means: red bars, green admits, amber waits. */
.lc-gate,.lc-bulk{font:inherit;font-size:11px;line-height:1;padding:3px 6px;margin-right:3px;
border-radius:99px;border:1px solid var(--line);background:transparent;
color:var(--muted);cursor:pointer;white-space:nowrap}
.lc-gate:hover:not(:disabled),.lc-bulk:hover:not(:disabled){border-color:var(--text);color:var(--text)}
.lc-bulk:disabled{opacity:.4;cursor:not-allowed}
.lc-gate[aria-checked="true"][data-gate="bar"]{background:var(--pill-red-bg);
border-color:var(--red);color:var(--red)}
.lc-gate[aria-checked="true"][data-gate="waive"]{background:var(--pill-green-bg);
border-color:var(--green);color:var(--green)}
.lc-gate[aria-checked="true"][data-gate="hold"]{background:var(--pill-amber-bg);
border-color:var(--amber);color:var(--amber)}
/* A bar the machine wrote, not the operator.  Dashed, because clicking it
   off is a ruling that also has to be recorded as one -- the switch does
   that, and the border is the warning that this row has an owner. */
.lc-gate[data-owner="auto"][aria-checked="true"]{border-style:dashed}
.lc-gate:disabled{opacity:.4;cursor:not-allowed}
.lc-gate:focus-visible{outline:2px solid var(--text);outline-offset:2px}
/* The station-day's outcome, on the cell that names the station: hit,
   miss, still open, never opened.  Four states that a reader otherwise has
   to reconstruct by reading three stage cells and a P&L column. */
td.st{border-left:3px solid transparent}
td.st-hit{background:var(--pill-green-bg);border-left-color:var(--green)}
td.st-miss{background:var(--pill-red-bg);border-left-color:var(--red)}
td.st-open{background:var(--pill-amber-bg);border-left-color:var(--amber)}
td.st-flat{background:var(--why-bg);border-left-color:var(--accent)}
/* The fold that hides the prose.  A pill rather than a triangle, because it
   sits under a heading where a bare disclosure arrow reads as an artifact. */
details.note>summary{color:var(--muted);font-size:12px;cursor:pointer;
list-style:none;display:inline-block;border:1px solid var(--line);
border-radius:99px;padding:1px 10px;margin:2px 0 6px;user-select:none}
details.note>summary::-webkit-details-marker{display:none}
details.note>summary:hover{color:var(--text)}
details.note[open]>summary{margin-bottom:8px}
/* The strip at the top.  Eight numbers, each one a question the rest of
   the page answers at length, coloured so a bad one is found without
   reading any of them. */
/* Four across, two rows.  Eight in a row does not fit the content width
   and lands as seven plus a widow. */
.kpi{display:grid;gap:7px;grid-template-columns:repeat(auto-fit,minmax(250px,1fr))}
.kpi .k{border:1px solid var(--line);border-radius:9px;padding:8px 10px;
background:var(--card);border-left-width:3px;border-left-style:solid}
.kpi .k.green{border-left-color:var(--green)}
.kpi .k.amber{border-left-color:var(--amber)}
.kpi .k.red{border-left-color:var(--red)}
.kpi .k.muted{border-left-color:var(--line)}
.kpi .k .lab{color:var(--muted);font-size:11.5px}
.kpi .k .val{font-size:20px;font-weight:650;letter-spacing:-.3px;
line-height:1.25;margin-top:1px}
.kpi .k .sub2{color:var(--muted);font-size:11.5px;margin-top:1px}
.kpi .k.red .val{color:var(--red)}
.kpi .k.amber .val{color:var(--amber)}
.big{font-size:19px;font-weight:650;letter-spacing:-.3px}
.muted{color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--muted);font-weight:500;font-size:12.5px;
padding:4px 8px;border-bottom:1px solid var(--line);user-select:none}
th:hover{color:var(--text)}
th[data-sorted="1"]{color:var(--accent)}
th[data-sort-dir="asc"]::after{content:" ↑"}
th[data-sort-dir="desc"]::after{content:" ↓"}
td{padding:5px 8px;border-bottom:1px solid var(--soft);
vertical-align:top}
tr:last-child td{border-bottom:none}
.scroll{max-height:520px;overflow:auto}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.bar{height:6px;border-radius:99px;background:var(--bar-bg);overflow:hidden;
margin-top:6px}
.bar>i{display:block;height:100%;background:var(--accent)}
.chain{display:flex;flex-direction:column;gap:0}
.link{display:flex;gap:10px;align-items:flex-start;padding:6px 0;
border-bottom:1px solid var(--soft)}
.link:last-child{border-bottom:none}
.link .body{flex:1;min-width:0}
.link .name{font-weight:600}
.why{background:var(--why-bg);border-left:2px solid var(--accent);
padding:9px 13px;border-radius:0 7px 7px 0;color:var(--why-fg);font-size:13.4px;
margin:9px 0 0}
.gap{display:flex;gap:9px;padding:6px 0;border-bottom:1px solid
var(--soft);align-items:flex-start}
.gap:last-child{border-bottom:none}
.factor{font-weight:650}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--muted)}
.th-legend{display:flex;gap:16px;flex-wrap:wrap;align-items:center;font-size:12.5px;
  margin:2px 0 6px}
.th-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:6px 18px}
.th-panel{min-width:0}
.th-head{display:flex;gap:8px;align-items:baseline;font-size:13px;margin:6px 0 0}
.th-chip{font-size:11.5px;padding:1px 8px;border-radius:10px;background:var(--bar-bg);
  color:var(--text)}
.th-row:hover .th-hit{fill:var(--line)!important;opacity:.5}
.th-row:hover .th-dot{r:6}
/* 真值落位's "who was right" table (user ruling, 2026-09-15): one row per
   disagreement group, three bars on one shared scale, the verdict on the
   right. */
.tl-rows{display:grid;gap:6px}
.tl-row{display:grid;grid-template-columns:minmax(150px,1.1fr) minmax(220px,2fr) auto;
gap:12px;align-items:center;padding:8px 12px;background:var(--card);
border:1px solid var(--line);border-radius:8px}
.tl-bar{display:grid;grid-template-columns:4.5em 1fr 3.6em;gap:8px;align-items:center;
font-size:12px;margin:2px 0}
.tl-track{height:9px;background:var(--bar-bg);border-radius:5px;overflow:hidden}
.tl-track span{display:block;height:100%;border-radius:5px}
.tl-bar .mono{text-align:right}
.tl-verdict{text-align:right;white-space:nowrap}
.tl-verdict-card{margin:6px 0 8px;font-size:13.4px;line-height:1.7}
/* 真值离均值多远 (2026-09-15): stage label, centre bar, coverage bars, verdict. */
.cl-row{grid-template-columns:minmax(150px,1fr) minmax(170px,1.2fr) minmax(220px,1.6fr) auto}
.cl-q{font-size:11.5px;color:var(--muted);margin-bottom:3px}
.cl-div{display:grid;grid-template-columns:auto 1fr auto;gap:6px;align-items:center;font-size:11.5px}
.cl-axis{position:relative;height:12px;background:var(--bar-bg);border-radius:6px}
.cl-axis i{position:absolute;top:0;bottom:0;border-radius:6px}
.cl-axis b{position:absolute;left:50%;top:-3px;bottom:-3px;width:2px;margin-left:-1px;background:var(--text)}
@media (max-width:640px){.tl-row{grid-template-columns:1fr}.tl-verdict{text-align:left}}
footer{margin-top:34px;color:var(--muted);font-size:12.5px;text-align:center}
/* The sticky head.  Sticky, because it is the only way back to the other
   two thirds of the page once a long section has been scrolled into.
   Two rows at two weights (user ruling, 2026-09-16: "信息太密集，没有主次").
   The first row is what a reader ACTS on -- home, the three areas, the
   live knobs, the theme -- drawn at full weight.  The second is the
   section index, which is looked up rather than scanned, so it is small
   borderless text under its area's caption.  Before this every one of
   thirty-odd chips, tabs and steppers wore the same border, and a bar of
   equal boxes has no first thing to read. */
.topnav{position:sticky;top:0;z-index:8;margin:0 0 16px;padding:8px 0 0;
background:var(--bg);border-bottom:1px solid var(--line)}
.navtop{display:flex;align-items:center;gap:8px 10px;flex-wrap:wrap;
padding:0 0 8px}
.nav-fill{flex:1;min-width:0}
.nav-div{flex:none;width:1px;height:20px;background:var(--line)}
/* 首页 (user ruling, 2026-09-16): back to the page head -- the total
   light, the LIVE switch and the lamp rows -- from anywhere on the page. */
.nav-home{cursor:pointer;display:inline-flex;align-items:center;gap:6px;
background:none;border:0;border-radius:8px;padding:5px 9px 5px 6px;
font-family:inherit;font-size:14px;font-weight:650;line-height:1.3;
color:var(--text);white-space:nowrap}
.nav-home svg{width:16px;height:16px;flex:none}
.nav-home:hover{background:var(--why-bg);color:var(--accent)}
.nav-home[data-here="1"]{color:var(--accent)}
/* Three areas (user ruling, 2026-08-29), now one segmented control rather
   than four bordered cards.  The hue stays -- a dot per area, the same
   hue the group titles below carry -- and the chosen area is the one
   segment lifted onto the card colour.  No section counts on the tabs: the
   row under them lists the sections themselves, and the count is in the
   hover.  "全部" is the last segment, with
   no dot, because it is not one of the areas; it is the way out of them. */
.tabs{display:inline-flex;gap:2px;flex-wrap:wrap;align-items:center;
margin:0;padding:3px;background:var(--bar-bg);border-radius:10px}
.tabs button{cursor:pointer;background:none;color:var(--muted);border:0;
border-radius:7px;padding:4px 11px 4px 9px;font-family:inherit;
font-size:13px;line-height:1.4;display:flex;align-items:center;gap:6px;
white-space:nowrap}
.tabs button::before{content:"";flex:none;width:7px;height:7px;
border-radius:50%;background:var(--tone,transparent)}
.tabs button.all{padding-left:11px}
.tabs button.all::before{display:none}
.tabs button:hover{color:var(--text)}
.tabs button[data-active="1"]{color:var(--text);background:var(--card);
font-weight:620;box-shadow:0 1px 2px rgba(0,0,0,.14)}
/* The group heading wears its area's colour on the same edge as its tab. */
.grouptitle{border-left:3px solid var(--tone,var(--line));padding-left:10px}
/* Per-day tabs inside a card.  The same pill as the group switcher, one
   size down and not sticky: this one picks which day a card shows, it is
   not a way around the page. */
.daytabs{display:flex;gap:6px;flex-wrap:wrap;align-items:center;
margin:0 0 10px}
.daytabs button{cursor:pointer;background:var(--bg);color:var(--muted);
border:1px solid var(--line);border-radius:99px;padding:4px 10px;
font-family:inherit;font-size:12.5px;line-height:1.35;
display:flex;align-items:center;gap:5px}
.daytabs button:hover{color:var(--text);border-color:var(--accent)}
.daytabs button[data-active="1"]{color:var(--text);border-color:var(--accent);
background:var(--why-bg)}
.daytabs .muted{font-size:11px}
/* 全部 stacks every day in one card, so the days need a seam.  Only
   there: with one pane showing, a rule keyed on the hidden pane before it
   draws a line under nothing. */
.card[data-dayall="1"] [data-daypane]+[data-daypane]{margin-top:14px;
padding-top:10px;border-top:1px solid var(--line)}
/* The money board (user ruling, 2026-09-13): one card per category, its
   hue on the title edge like the group titles, and the blocks inside it
   drop the top margin they carried as free-standing sections -- the tab
   strip already names them. */
.mb-cat{padding:12px 14px;margin-bottom:12px}
.mb-cat .grouptitle{margin:0 0 9px}
.mb-pane > h2:first-child,.mb-pane > h3:first-child{margin-top:2px!important}
.mb-pane > .lampdeck:first-child{margin-top:0;padding-top:0;border-top:0}
/* 纸面账本 as one data board (user ruling, 2026-09-13): a headline card,
   comparison cards whose bars share one zero-based scale, risk cells and
   the curves, each under a small caption. */
.pb-cap{color:var(--muted);font-size:11.5px;margin:12px 0 5px}
.ee-top{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.ee-verdict{font-size:14px;font-weight:650;padding:3px 12px}
.pb-kv{display:inline-flex;align-items:baseline;gap:6px;white-space:nowrap}
.pb-kv b{font-variant-numeric:tabular-nums}
.ee-cmps{display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(min(100%,430px),1fr))}
.ee-cmp{margin-bottom:0}
.ee-cmp:last-child:nth-child(odd){grid-column:1/-1}
.ee-h{display:flex;align-items:center;gap:8px;margin-bottom:7px}
.ee-h .pill{margin-left:auto}
.ee-row{display:grid;grid-template-columns:7.5em 1fr auto;gap:8px;
align-items:center;font-size:12.5px;margin:4px 0}
.ee-track{height:12px;background:var(--bar-bg);border-radius:6px;overflow:hidden}
.ee-fill{display:block;height:100%;border-radius:6px}
.ee-fill.ee-base{background:var(--muted);opacity:.45}
.ee-fill.muted{background:var(--muted)}
.ee-v{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.ee-d{margin-top:6px;font-size:12.5px}
.pb-kpis{display:grid;gap:6px;grid-template-columns:repeat(auto-fit,minmax(min(100%,150px),1fr))}
.pb-k{display:flex;flex-direction:column;gap:1px;padding:7px 10px;
background:var(--card);border:1px solid var(--line);border-radius:7px}
.pb-k b{font-size:17px;font-variant-numeric:tabular-nums}
.pb-s{font-size:11.5px}
.pb-legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--muted);margin:2px 0 4px}
.pb-legend i{display:inline-block;width:18px;margin-right:5px;vertical-align:middle}
.ee-foot{font-size:11.5px;margin-top:6px}
.grouptitle{display:flex;gap:10px;flex-wrap:wrap;align-items:baseline;
margin:18px 0 0;font-weight:650}
.grouptitle .muted{font-weight:400}
/* The five kinds of reading -- 实盘 above the tabs, then 模型, 边际, 选窗,
   定注 -- each open on a rule in their own colour and a banner title
   (user ruling, 2026-09-17: 五大分类之间加明显的分界线).  A section title
   and an area title used to differ by one thin edge, so where one kind of
   reading ended and the next began had to be read off the chips. */
.group,.toparea{margin-top:40px;padding-top:14px;
border-top:4px solid var(--tone,var(--line))}
.group > .grouptitle,.toparea > .grouptitle{margin:0 0 4px;padding:9px 12px;
border-left:0;border-radius:6px;font-size:17px;
background:color-mix(in srgb,var(--tone,var(--line)) 14%,transparent)}
/* A sub-heading inside an area: the kind of thing the sections under it
   are.  Its edge wears the lane colour so "live" and "shadow" read before
   any number does. */
.subgroup{margin:26px 0 10px;padding:8px 12px;border-radius:7px;
border:1px solid var(--line);border-left:4px solid var(--line);
background:color-mix(in srgb,var(--tone,var(--line)) 7%,var(--card))}
.subgroup[data-lane="live"]{border-left-color:var(--green)}
.subgroup[data-lane="shadow"]{border-left-color:var(--amber)}
.subgroup-t{font-size:15px;font-weight:650;display:flex;gap:8px;
align-items:baseline;flex-wrap:wrap}
.subgroup-n{font-size:12.5px;margin-top:3px}
/* The edge map. */
.emap{padding:14px;margin-bottom:6px}
.em-q{font-size:14px;margin-bottom:10px}
.em-formula{font-size:15px;padding:8px 12px;border-radius:7px;margin-bottom:10px;
background:color-mix(in srgb,var(--accent) 10%,var(--card));border:1px solid var(--line);
overflow-x:auto;white-space:nowrap}
.em-eq{display:grid;gap:8px;align-items:stretch;
grid-template-columns:minmax(0,1fr) auto minmax(0,1.6fr) auto minmax(0,1fr) auto minmax(0,.8fr)}
.em-op{align-self:center;font-size:22px;color:var(--muted);text-align:center}
.em-term{border:1px solid var(--line);border-radius:9px;padding:9px 10px;
display:flex;flex-direction:column;gap:4px;min-width:0}
.em-term.em-result{border-color:var(--accent);
background:color-mix(in srgb,var(--accent) 8%,transparent)}
.em-sym{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:16px;font-weight:650}
.em-tt{font-weight:620}
.em-list{display:flex;flex-direction:column;gap:6px;margin-top:6px}
.em-sub{font-size:11.5px;margin-top:4px}
@media (max-width:900px){.em-eq{grid-template-columns:1fr}.em-op{font-size:18px}}
.em-inputs{display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(min(100%,180px),1fr));margin-bottom:10px}
.em-band{border:1px dashed var(--line);border-radius:9px;padding:10px;margin-top:10px}
.em-band.em-live{border:1px solid var(--green);
background:color-mix(in srgb,var(--green) 6%,transparent)}
.em-band.em-shadow{border-color:var(--amber)}
.em-cap{font-size:12.5px;font-weight:620;margin-bottom:8px}
.em-flow{display:flex;flex-wrap:wrap;align-items:stretch;gap:6px}
.em-flow > .em-node{flex:1 1 170px}
.em-arrow{align-self:center;color:var(--muted);font-size:18px;padding:0 2px}
.em-ev{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}
.em-ev > .em-node{flex:0 1 300px}
.em-two{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr))}
.em-two > .em-band > .em-node{width:100%;margin-bottom:6px}
.em-inner{margin:2px 0 0 14px;padding-left:10px;border-left:2px solid var(--amber);
display:flex;flex-direction:column;gap:6px}
.em-inner > .muted{font-size:11.5px}
.em-node{cursor:pointer;display:flex;flex-direction:column;gap:3px;text-align:left;
font-family:inherit;color:var(--text);background:var(--card);
border:1px solid var(--line);border-left:4px solid var(--line);border-radius:7px;
padding:7px 10px;font-size:12.5px;line-height:1.35}
.em-node[data-lane="live"]{border-left-color:var(--green)}
.em-node[data-lane="shadow"]{border-left-color:var(--amber)}
.em-node:hover{border-color:var(--accent)}
.em-node.em-plain{cursor:default;background:transparent;border-left-width:1px}
.em-node.em-plain:hover{border-color:var(--line)}
.em-h{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.em-h .pill{margin-left:auto}
.em-w{color:var(--muted);font-size:12px}
.em-key{margin-top:12px;display:flex;flex-direction:column;gap:5px;font-size:12.5px}
/* The theme button rides at the end of the bar.  It is still a static
   element of the body, moved in once the bar exists, so a page whose
   render failed keeps it -- in the corner where it always was. */
#theme{cursor:pointer;background:none;color:var(--muted);
border:1px solid var(--line);border-radius:8px;padding:0;width:30px;
height:28px;font-family:inherit;font-size:14px;line-height:1;flex:none}
#theme:hover{color:var(--text);border-color:var(--accent)}
/* Charter 141's dials made the live strip wider than the bar's first row.
   The theme button keeps its place at the end of that row (after the fill),
   and the strip takes the next row from the bar's left edge -- the same edge
   首页 and the section index below start on -- whatever the width, rather
   than the strip wrapping and pushing the button off.  Right-aligned it
   floated with its left edge mid-bar, lined up with nothing (user,
   2026-09-17: "没有对齐"). */
.navtop > #theme{order:1}
.navtop > .navctl{order:2;margin-left:0}
body > #theme{position:fixed;top:14px;right:16px;z-index:9;width:auto;
padding:6px 12px;background:var(--card);box-shadow:var(--shadow)}
/* Second row of the sticky bar: one chip per section on screen, so a
   twenty-section page is one click deep instead of one scroll long.  It
   wraps rather than scrolling sideways -- a jump bar whose far half is
   off-screen is the problem it was added to solve.  The chips are text,
   not boxes, and each area gets its own line behind a caption that wears
   the area's hue on the same edge as the area title below, so the thirty
   chips under 全部 read as six short lists rather than one long one.  An
   area wraps as a unit: a two-section area shares a line with its
   neighbour instead of spending a line of the bar on itself.  The chip for
   the section under the bar is marked, which makes the same rows answer
   "where am I" as well as "where can I go". */
.jump{display:flex;flex-wrap:wrap;gap:1px 20px;align-items:flex-start;
padding:0 0 7px}
.jump-area{display:flex;align-items:flex-start;gap:4px;max-width:100%;
min-width:0}
.jump-g{flex:none;margin-top:3px;padding:0 2px 0 6px;font-size:11px;
font-weight:650;line-height:16px;color:var(--muted);white-space:nowrap;
border-left:3px solid var(--tone,var(--line))}
.jump-run{display:flex;flex-wrap:wrap;gap:0 1px;align-items:center;min-width:0}
.jump button{cursor:pointer;white-space:nowrap;flex:none;background:none;
color:var(--muted);border:0;border-radius:6px;padding:2px 7px;
font-family:inherit;font-size:12.5px;line-height:1.45}
.jump button:hover{color:var(--text);background:var(--bar-bg)}
.jump button[aria-current="true"]{color:var(--accent);background:var(--why-bg)}
/* The live knobs, top right of the bar (user ruling, 2026-09-11).  One
   card, one stepper per knob: the value sits between its own two buttons,
   so which button moves which number is never a matter of reading
   distances.  The status at the front says what a click would reach --
   the control server, and whether the book behind it is sending orders --
   because the same click means paper on one day and money on the next. */
.navctl{display:flex;align-items:center;flex-wrap:wrap;font-size:12px;
color:var(--text);background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:2px 3px;
/* Charter 141 added four dials: on a narrow bar the strip wraps its own
   knobs rather than overflowing. */
flex:0 1 auto;min-width:0}
.navctl[data-lc="live"]{border-color:var(--red)}
.navctl-k{display:inline-flex;align-items:center;gap:6px;padding:1px 7px;
white-space:nowrap;border-left:1px solid var(--line)}
.navctl-l{color:var(--muted)}
.stp{display:inline-flex;align-items:center;background:var(--bar-bg);
border-radius:7px;padding:1px}
.stp b{min-width:3.3em;padding:0 3px;text-align:center;font-size:13px;
font-weight:650;font-variant-numeric:tabular-nums;border-radius:5px}
.navctl .lc-w{cursor:pointer;background:none;color:var(--muted);border:0;
border-radius:6px;min-width:22px;height:22px;padding:0 5px;
font-family:inherit;font-size:14px;line-height:22px}
.navctl .lc-w.coarse{font-size:11px;font-variant-numeric:tabular-nums}
.navctl .lc-w:not(:disabled):hover{background:var(--card);color:var(--text);
box-shadow:0 1px 2px rgba(0,0,0,.14)}
.navctl .lc-w:disabled{opacity:.35;cursor:not-allowed}
.navctl-k[aria-busy="true"] b{opacity:.45}
.stp b.lc-flash{animation:lc-flash .9s ease-out}
@keyframes lc-flash{from{background:var(--pill-green-bg);
color:var(--pill-green-fg)}}
.navctl-s{display:inline-flex;align-items:center;gap:6px;padding:1px 9px 1px 6px;
font-size:12px;color:var(--muted);white-space:nowrap}
.navctl-s i{flex:none;width:8px;height:8px;border-radius:50%;
background:var(--muted)}
.navctl[data-lc="down"] .navctl-s{color:var(--amber)}
.navctl[data-lc="down"] .navctl-s i{background:none;
box-shadow:inset 0 0 0 1.5px var(--amber)}
.navctl[data-lc="armed"] .navctl-s i{background:var(--amber)}
.navctl[data-lc="live"] .navctl-s{color:var(--red);font-weight:620}
.navctl[data-lc="live"] .navctl-s i{background:var(--red);
box-shadow:0 0 0 3px var(--pill-red-bg)}
/* A phone gets the same two rows, but the section index scrolls sideways:
   wrapped at 375px it would be the whole screen, which is a bar that
   hides the page it navigates. */
@media(max-width:760px){
.navtop{gap:6px}
.nav-div,.nav-fill{display:none}
#theme{order:1;margin-left:auto}
.tabs{order:2;width:100%;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none}
.navctl{order:3;width:100%;justify-content:space-between}
.jump,.jump-run{flex-wrap:nowrap}
.jump-area{max-width:none;flex:none}
.jump{overflow-x:auto;scrollbar-width:none}
.jump::-webkit-scrollbar{display:none}
}
/* The same number under all three weightings, inside the tile that owns it.
   A row rather than a table: three values is not a table, and the table it
   replaced printed every figure on the section twice. */
.wts{display:flex;gap:9px;flex-wrap:wrap;align-items:baseline;margin-top:6px;
padding-top:6px;border-top:1px solid var(--line);font-size:11.5px}
.wts+.wts{border-top:none;padding-top:0;margin-top:3px}
.wts span{white-space:nowrap;font-variant-numeric:tabular-nums}
/* A band that can buy something, marked on the row rather than left to be
   inferred from one cell of nine. */
tr.buyable td:first-child{box-shadow:inset 3px 0 0 var(--green)}
tr.buyable td{background:rgba(63,185,80,.07)}
/* The starred chips.  Weight rather than hue: the bar already spends
   colour on the area tone, and a chip that differs only in hue is found no
   faster than one that does not.  They were filled pills until 2026-09-16;
   among borderless text, full-strength bold with a star is the contrast,
   and five filled pills were the loudest thing on a bar whose loudest
   thing should be the knobs. */
.jump button.star{color:var(--text);font-weight:620}
.jump button.star::before{content:"★";margin-right:3px;font-size:10px;
color:var(--accent);vertical-align:1px}
@media(max-width:820px){body > #theme{position:static;margin:14px 20px 0}}
</style>
<script>
// Before first paint, so a light-mode reader does not get a dark flash.
// The stored choice wins over the system one: someone who has clicked the
// button has said something the OS setting cannot override.
(function(){
  try{
    var saved = localStorage.getItem("board-theme");
    var dark = window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme =
      saved || (dark ? "dark" : "light");
  }catch(e){ document.documentElement.dataset.theme = "dark"; }
})();
</script>
</head><body>
<button id="theme" type="button"></button>
<div class="wrap" id="app"></div>
<script>
const D = __PAYLOAD__;
const h = (s) => String(s == null ? "" : s);
const hours = (v) => v == null ? "—" :
  (v < 1 ? Math.round(v * 60) + " 分钟前" :
   v < 48 ? v.toFixed(1) + " 小时前" : (v / 24).toFixed(1) + " 天前");
// Shares are whole lots: the exchange's minimum is five and the grid steps
// by one.  But sizing divides a notional by a price, so the stored REAL
// keeps the division's float residue -- 0.15×7.000000000000001 and
// 0.47×4.999999999999999 were both on the board, reading as precision the
// number does not have.  Print the integer when the value is one, to
// within that residue; a size that is genuinely fractional still shows its
// fraction rather than being silently rounded to a lot it is not.
const shares = (v) => {
  const n = Number(v);
  if (!isFinite(n)) return h(v);
  return Math.abs(n - Math.round(n)) < 1e-6
    ? String(Math.round(n)) : String(Number(n.toFixed(4)));
};
// Rungs read left to right, coldest first (user ruling, 2026-09-12).
// Nothing upstream promises ladder order.  The live legs arrive in the
// order their intents were minted -- newest first -- which is why one
// EPWA basket printed 19°C 20°C 18°C: three rungs of one ladder, drawn
// out of order, read as three unrelated buys.  The other ladders on this
// page happen to arrive sorted today; they are sorted here too, so
// left-to-right is a property of the page rather than of whichever writer
// last touched the table it came from.
//
// Every label this book mints is one of four shapes -- "18°C",
// "78-79°F", "17°C or below", "21°C or higher" -- and a few older lanes
// write "<=17°C" / ">=21°C".  A range sorts on its lower bound.  An open
// end sorts half a rung outside the degree it names, so the bottom bucket
// lands below its own number and the top one above it, which is where
// they sit on the ladder.  A label that parses to no number sorts last
// and keeps the order it came in: inventing a rung for it would be worse
// than admitting the page cannot place it.
const rungValue = (b) => {
  const s = String(b == null ? "" : b);
  const m = s.match(/-?\d+(?:\.\d+)?/);
  if (!m) return NaN;
  const v = Number(m[0]);
  if (!isFinite(v)) return NaN;
  if (/below|^\s*<=|^\s*\u2264/.test(s)) return v - 0.5;
  if (/higher|above|^\s*>=|^\s*\u2265/.test(s)) return v + 0.5;
  return v;
};
// Array.prototype.sort is stable, so ties and unplaceable labels keep the
// order they arrived in.
const byRung = (x, y) => {
  const a = rungValue(x), b = rungValue(y);
  if (!isFinite(a)) return isFinite(b) ? 1 : 0;
  if (!isFinite(b)) return -1;
  return a - b;
};
const rungSorted = (list, pick) => (list || []).slice()
  .sort((x, y) => byRung(pick ? pick(x) : x, pick ? pick(y) : y));
const OVERALL = {green:["运转正常","所有环节都在按预期更新"],
  amber:["有需要留意的地方","不影响出数，但有环节慢了或在攒数据"],
  red:["有环节断了","看下面三排灯，红的那盏就是断的那一环"]};

// The maintenance lanes, one row each (user ruling, 2026-09-14: "sweep前端
// 显式健康度").  The lamp in the machine row said "9 红 · 1 黄" and put the
// rest in a tooltip; on the morning that needed it, the ledger had gone nine
// hours without completing while a download capture held the SSOT queue and
// seven restarts killed whatever ran -- none of which a count can say.  So
// each lane shows where it is on its own clock, what it is doing now, and
// its place in its domain's queue.  Since the same day's second ruling it
// draws only the three least healthy lanes; the writer, the queue and the
// restart count stay in the payload and in the machine row's lamp.
const SWEEP_LABEL = {
  paper_trading_ledger:"纸面账本", settlement_truth:"结算真值",
  calibration_refit:"校准重拟", pr12_awc_observation:"观测身份（P1 的根）",
  settled_day_cycle_close:"已结算日收盘", maker_shadow_probe:"挂单影子探针",
  correction_review_auto_clear:"订正复核自动清除",
  forward_packet_retention:"前瞻包保留", auto_station_exclusions:"自动禁入站",
  capture_jma_msm_forward:"JMA MSM 捕获",
  capture_noaa_conus_forward:"NOAA HRRR/NBM 捕获",
  capture_dwd_icon_eu_forward:"DWD ICON-EU 捕获",
  capture_cma_grapes_forward:"CMA GRAPES 捕获",
  station_onboarding:"新站接入", market_registry_retention:"挂牌目录保留",
  ledger_alignment_guard:"账本对齐守卫", delta_v2_authority:"执行授权续期",
};
const SWEEP_DOMAIN = {ssot:"写 SSOT", archive:"写归档库", none:"不写库"};
function sweepDur(sec){
  if(sec == null || !isFinite(sec)) return "—";
  if(sec < 90) return Math.round(sec) + " 秒";
  if(sec < 5400) return Math.round(sec / 60) + " 分钟";
  return (sec / 3600).toFixed(1) + " 小时";
}
function sweepHealth(){
  const H = D.hosted_sweeps || {};
  const cap = `<span class="lampcap">维护车道健康度 · 最不健康的三条：离上次完成过了几个<b>自己的</b>回合`
    + `（刻度 1/2/3 个回合），右边是它此刻在干什么、在自己写域的队里排第几</span>`;
  if(!H.available){
    return `<div class="lampdeck">${cap}<div class="muted">读不出：${h(H.reason || "")}</div></div>`;
  }
  // Only the three least healthy (user ruling, 2026-09-14: 维护车道健康度：只显示
  // 不健康的前三项即可).  Sixteen rows put the four that were red among twelve
  // that were fine; red before amber, then whichever is more of its own turns
  // late, and the count of the rest on one line so a shorter list does not
  // read as "this is everything".
  const label = n => SWEEP_LABEL[n] || n;
  const TONE = {red: 0, amber: 1};
  const sick = (H.lanes || [])
    .filter(l => l.health in TONE)
    .sort((a, b) => TONE[a.health] - TONE[b.health]
      || (b.overdue_intervals || 0) - (a.overdue_intervals || 0));
  const lanes = sick.slice(0, 3);
  if(!lanes.length){
    return `<div class="lampdeck">${cap}<div class="swhead"><span class="pill green">全部准点</span></div></div>`;
  }
  const header = sick.length > lanes.length
    ? `<div class="swhead"><span class="muted">共 ${H.red_count || 0} 红 · ${H.amber_count || 0} 黄，其余 ${
        sick.length - lanes.length} 条未列</span></div>`
    : "";
  const axis = `<div class="swrow swaxisrow"><span></span><span></span>
    <div class="swaxis"><em style="left:25%">1 回合</em><em style="left:50%">2</em>
    <em style="left:75%">3</em><em style="left:100%">≥4</em></div><span></span></div>`;
  const rows = lanes.map(l => {
    const over = l.overdue_intervals;
    const x = over == null ? 0 : Math.min(4, Math.max(0, over)) / 4 * 100;
    let state, hot = false;
    if(l.running_now){
      state = `在跑 ${sweepDur(l.running_elapsed_sec)} / 超时 ${sweepDur(l.timeout_sec)}`;
      hot = true;
    } else if(l.stuck){
      state = "卡住：轮到就失败"; hot = true;
    } else if(l.last_state && l.last_state !== "ok"){
      state = `上次 ${l.last_state}`; hot = true;
    } else if(l.waiting){
      state = `排队 第 ${l.queue_rank}/${l.queue_size} 位`;
      hot = l.health !== "green";
    } else {
      state = "准点";
    }
    const tip = [
      `${l.name}`,
      `每 ${sweepDur(l.interval_sec)} 一轮，超时 ${sweepDur(l.timeout_sec)}`,
      `上次完成 ${sweepDur(l.since_completed_sec)}前`,
      `上次尝试 ${sweepDur(l.since_attempted_sec)}前`,
      over == null ? "" : `离上次完成 ${over.toFixed(2)} 个回合`,
      l.last_duration_sec == null ? "" : `上次跑了 ${sweepDur(l.last_duration_sec)}`
        + (l.lease_overrun_sec > 0 ? `（超写入租约 ${sweepDur(l.lease_overrun_sec)}）` : ""),
      SWEEP_DOMAIN[l.domain] || l.domain,
      l.download_capture ? "下载型：跑的时候不占写锁，也不占 SSOT 队列" : "",
      l.note || "",
    ].filter(Boolean).join(" · ");
    const run = l.running_now && l.running_timeout_share != null
      ? `<u class="swrun" style="width:${Math.min(100, 100 * l.running_timeout_share).toFixed(1)}%"></u>` : "";
    return `<div class="swrow" title="${h(tip)}">
      <span class="swname"><span class="dot ${l.health}"></span><b>${h(label(l.name))}</b></span>
      <span class="swdom">${h((SWEEP_DOMAIN[l.domain] || l.domain) + (l.download_capture ? "·下载" : ""))}</span>
      <div class="swbar"><b class="swm ${l.health}" style="left:${x.toFixed(1)}%"></b>${run}</div>
      <span class="swstate${hot ? " hot" : ""}">${h(state)}</span></div>`;
  }).join("");
  return `<div class="lampdeck">${cap}<div class="swrows">${axis}${rows}</div>${header}</div>`;
}

function head(){
  const [t, s] = OVERALL[D.overall_health] || OVERALL.amber;
  const w = D.worker || {};
  // Name the broken links on the banner itself.  The payload knows which
  // rows turned the light red; making the reader scroll for them is what
  // "black box" complaints are made of.  Red banner lists red rows, amber
  // banner lists amber rows -- the level that set the light.
  const named = (D.alerts || [])
    .filter(a => a.health === D.overall_health);
  const chips = named.slice(0, 6).map(a =>
    `<span class="pill ${a.health}" style="font-size:13px">${h(a.label)}
      <span style="opacity:.78">｜${h(a.note)}</span></span>`).join("");
  const more = named.length > 6
    ? `<span class="muted">…等 ${named.length} 处，其余见下面三排灯里标${
        D.overall_health === "red" ? "红" : "黄"}的那几盏</span>` : "";
  return `<h1>气温篮子全链看板</h1>
  <p class="sub">从模型到下单的一整条链。<b>页头就是整块看板</b>：总灯，
  然后三排状态灯——<b>机器</b>在不在转、<b>上游</b>的数据到没到、
  <b>选桶</b>挑得准不准。三个问题按人实际发问的顺序排，
  都在选标签页之前就该看见，所以放在标签栏上面而不是某个分组里面。
  紧接着是<b>资金看板</b>：这本账值多少，按纸面账本与实盘两类分页签；
  然后是站点实盘上线闸门，再往下是逐站账：每日统计、逐站逐日明细与三张跟进表；
  再往下按层级分四个<b>区域</b>——模型（概率层）、边际、选窗、定注，顺着链条从上游到下游，
  每区一个颜色，标签栏与区标题同色。</p>
  <div class="card"><div style="display:flex;gap:14px;align-items:center;
    flex-wrap:wrap">
    <span class="dot ${D.overall_health}" style="width:13px;height:13px"></span>
    <span class="big">${t}</span>
    <span class="muted">${s}</span>
    <span style="flex:1"></span>
    ${liveSwitch()}
  </div>
  ${(()=>{
    // One dashboard, four rows, in the order the questions get asked (user
    // ruling, 2026-09-11).  The strip of fourteen tiles and the three-state
    // cards used to live below the tab bar as their own sections; they are
    // readings about the same four things the lamps answer, so they are
    // lamps in the same rows rather than a second layout for the same job.
    // The fourth row, 钱, left for the money board on 2026-09-13 (user
    // ruling): what the book is worth is read beside the risk, the entry
    // economics and the live account, not beside the machine's lamps.
    let O;
    try { O = overviewLamps(); }
    catch (err) {
      O = {banner:"", machine:[lamp("red","总览出错",String(err))],
           upstream:[], selection:[]};
    }
    const rows=[
      ["机器 · 在不在转：常驻、周期、授权，然后是数据从头到尾的每一环"
        + "（上面停了，下面迟早跟着停）、库容量、维护车道、真值物化",
        () => O.machine.concat(healthLamps())],
      ["上游 · 喂进来的东西到没到：四家全球模型、三条区域采集道、区域赛道、"
        + "缺口检查，以及决策真正读到的那份包里有几家",
        () => upstreamLamps().concat(O.upstream)],
      ["选桶 · 挑得准不准：建仓率、声称兑现、各宽度篮子的选中命中",
        () => O.selection],
    ];
    const rendered = rows.map(([cap,build])=>
      lampRowSafe(()=>lampRow(cap,build()), cap.split(" ")[0]));
    // Directly under the machine row whose "维护车道" lamp it expands.
    const sweeps = (() => {
      try { return sweepHealth(); }
      catch (err) {
        return `<div class="lampdeck"><span class="lampcap">维护车道健康度</span>`
          + `<div class="muted">渲染出错：${h(String(err))}</div></div>`;
      }
    })();
    return O.banner + rendered[0] + sweeps + rendered.slice(1).join("");
  })()}
  ${renderStamp()}${chips ? `<div style="display:flex;gap:8px;flex-wrap:wrap;
    align-items:center;margin-top:9px">${chips}${more}</div>` : ""}</div>`;
}

// When this page was built, and how old that makes it.
//
// The page is a FILE on a thirty-minute timer, not a live query, and it
// takes minutes to build -- so a reader who changes something and refreshes
// gets the previous build back.  On 2026-09-09 that read as a bug: P1's
// band table moved at 03:23Z, the page on screen was built at 03:07Z, and
// the build that would have shown it was twenty-four minutes into its own
// run.  Nothing on the page said so; the only timestamp was in the footer,
// in UTC, seven thousand lines down.
//
// So: local time, an age in minutes, and the build's own cost -- amber once
// the age passes a full cycle, because past that the reader is looking at
// something a render should already have replaced.
// The two vintages the file was written with: when its template was read,
// and when the build behind its data began.  A hot update moves only the
// first (``reinflate``).
const PAGE_STAMP = "__RENDER_STARTED__";
const PAYLOAD_STAMP = "__PAYLOAD_STARTED__";
function renderStamp(){
  const R = D.render || {};
  const stamp = R.generated_at_utc || D.generated_at_utc;
  if(!stamp) return "";
  const at = Date.parse(stamp);
  if(!isFinite(at)) return "";
  const ageMin = Math.max(0, Math.round((Date.now() - at) / 60000));
  const local = new Date(at).toLocaleString(undefined, {
    month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
  // Thirty minutes is when the next render is DUE, not when this one is
  // late: launchd counts the interval from the previous START, and a build
  // that takes ten minutes pushes the next completion out to forty.  At a
  // flat 30 the banner said "the numbers may be behind" on every ordinary
  // afternoon, which is how a freshness light stops being read.  The bar
  // is the cycle plus what this build actually cost, floored at five
  // minutes so a fast build does not make the light hair-trigger.
  const cycleMin = 30;
  const buildMin = Math.max(5, Math.ceil((R.build_sec || 0) / 60));
  const staleAfter = cycleMin + buildMin;
  const stale = ageMin >= staleAfter;
  const build = R.build_sec == null ? null
    : (R.build_sec >= 90 ? (R.build_sec/60).toFixed(1) + " 分钟"
                         : Math.round(R.build_sec) + " 秒");
  const reused = R.reused_sections
    ? ` · 沿用 ${R.reused_sections} 区块` : "";
  // A hot update redraws the page around the same data; say when, so a
  // section that changed shape is not read as a section whose numbers moved.
  const drawn = Number(PAGE_STAMP), built = Number(PAYLOAD_STAMP);
  const hot = isFinite(drawn) && isFinite(built) && drawn - built > 120
    ? `<span class="muted" title="改前端只重灌模板、秒级生效；数据仍是左边那次整库渲染的，新增的数据字段要等下一轮整库渲染">模板热更新于 ${
        h(new Date(drawn * 1000).toLocaleString(undefined, {
          month: "2-digit", day: "2-digit", hour: "2-digit",
          minute: "2-digit", hour12: false}))}</span>` : "";
  return `<div style="display:flex;gap:10px;align-items:center;
    flex-wrap:wrap;margin-top:9px;padding-top:9px;
    border-top:1px solid var(--line)">
    <span class="dot ${stale ? "amber" : "green"}"></span>
    <b>本页渲染</b>
    <span class="${stale ? "pill amber" : "muted"}">${h(local)}
      · ${ageMin} 分钟前</span>${hot}
    <span class="muted">${stale
      ? `已超过一轮重建周期＋本轮构建耗时（${staleAfter} 分钟），页面上的数字可能落后于库里的`
      : `页面每 ${cycleMin} 分钟起一轮、本轮构建 ${buildMin} 分钟，刷新浏览器不会重算`}</span>
    <span style="flex:1"></span>
    <span class="muted">${build ? "构建耗时 " + h(build) : ""}${
      R.computed_sections ? ` · 现算 ${R.computed_sections} 区块` : ""
    }${reused}</span>
  </div>`;
}

// The top strip: one number per question the page spends a section
// answering.  Ordered the way a reader triages -- is the data coming in, is
// it being turned into positions, are the positions any good -- and
// coloured so a bad one is found without reading the others.
//
// Every number here is read from the same payload the sections below use.
// Restating a figure would let the strip and the section it summarises
// disagree, which is worse than not having the strip.
function overviewLamps(){
  // The 选桶 row reads the live book (user ruling, 2026-09-16: the paper
  // ledger is a rule comparator).  The banner that suppressed the strip
  // while a paper rebuild had emptied the tables went with it: no lamp
  // here reads a table a rebuild can empty.
  const S=D.live_selection||{};
  const pct0=(v)=>v==null?"—":(v*100).toFixed(0)+"%";
  const band=(v,good,ok)=>v==null?"muted"
    :(v>=good?"green":v>=ok?"amber":"red");
  // Buckets, because the strip used to be one queue of fourteen tiles in
  // the order they were added and a reader triaged it by reading all
  // fourteen (user ruling, 2026-09-11: classify them, and put them in the
  // head with the lamps).  Three readings, asked in the order anyone asks
  // them: is the machine turning, did the data arrive, did we pick well.
  // The fourth, what it is worth, is the money board.
  const G={machine:[],upstream:[],selection:[]};
  const push=(group,cls,lab,val,sub2,title)=>G[group].push(
    lamp(cls, lab, val, [sub2, title].filter(Boolean).join(" · ")));

  // 0. The resident itself, first: everything below this strip is read out
  // of a database the resident fills, so a reader who does not know the
  // pipeline stalled will misread every number after it as current.  A
  // heartbeat and a pid say the process is alive; the production light says
  // whether it is producing.  Those are different questions, and on
  // 2026-08-26 they had different answers for eleven hours.
  const W=D.worker||{};
  const beat=W.heartbeat_age_hours;
  push("machine", beat==null?"muted":beat<0.25?"green":beat<1?"amber":"red",
    "常驻心跳", beat==null?"—":hours(beat),
    W.pid?`pid ${W.pid}`:"进程未发布 pid",
    "常驻进程最后一次写状态的时刻。它只说进程活着，不说流水线在出货——看右边那张");
  push("machine", W.production_health||"muted",
    "生产周期", W.production_cycle_seconds==null?"—"
      :W.production_cycle_seconds.toFixed(0)+" 秒",
    h(W.production_note||"—"),
    `常驻自己的生产回执：${h(W.production_state||"未发布")}。`+
    `绿=正常产出，黄=单条车道阻断，红=多车道断/被写锁挡住/超一小时没完成一轮。`+
    `逐候选的日常拒绝不计入——那是目标函数在正常挑站日，见「没建仓是因为什么」`);
  push("machine", W.live_submit_authorized?"amber":"green",
    "下单授权", W.live_submit_authorized?"已授权":"未授权（纸面）",
    W.live_submit_authorized?"真实下单已开启":"全链纸面运行，网关不发单",
    "是否允许向交易网关发真实订单。纸面态是设计常态：绿灯代表没有真钱在跑");
  // Truth materialisation and the gap check are NOT pushed here: each
  // already has a lamp of its own in the machine or upstream row, and two
  // tiles for one reading is how the head ends up disagreeing with itself
  // when only one of them is updated.  "模型在进来" stays, because it is
  // not the four model lamps summarised -- it carries the lane's own idle
  // time, which per-family ages absorb by construction.

  // 1. Are the four model families still arriving.
  const fresh=(D.members||[]).filter(m=>m.health==="green").length;
  const total=(D.members||[]).length;
  // Which family is furthest from its last run.  The count alone says how
  // many are fine; the reader's next question is always how stale the
  // stalest one is, and that is one number.
  const laggard=(D.members||[]).slice().sort((a,b)=>
    (b.run_age_hours==null?1e9:b.run_age_hours)
    -(a.run_age_hours==null?1e9:a.run_age_hours))[0];
  // How long since the lane last landed ANY family.  Each family's own age
  // is measured against its own cadence and absorbs an idle lane -- on
  // 2026-09-09 nothing was captured for any family between 15:23Z and
  // 02:09Z, ten hours and forty-six minutes, and ECMWF still read healthy
  // at 8.6h afterwards because its next run had since arrived.  The
  // shortest capture age across the four is the one number that says the
  // lane itself stopped asking, and it cannot be absorbed by anything.
  const capAges=(D.members||[]).map(m=>m.latest_capture_age_hours)
    .filter(v=>v!=null);
  const idle=capAges.length?Math.min(...capAges):null;
  push("upstream", fresh>=total?"green":fresh>=total-1?"amber":"red",
    "模型在进来", `${fresh}/${total}`,
    (laggard?`最旧一家 ${h(laggard.label)} · ${
      hours(laggard.run_age_hours)}的 run`:"没有成员族")
      +(idle==null?"":` · 车道 ${hours(idle)}前最后落一家`),
    "页头「上游」灯排里的四盏模型灯：四家集合预报的到货情况。"
    +"「车道…前最后落一家」是采集器上一次成功落任何一家的时间 —— "
    +"每家自己的年龄按各自节奏算，会把「整条车道停了」这件事吸收掉，这个数不会");

  // 1b. What the packets a decision actually reads are carrying.  The card
  // above counts each family against its OWN cadence, which absorbs a
  // shortfall: on 2026-09-10 it read 3/4 with ECCC 32.6h late, while the
  // 2026-09-12 packets -- the 37 a D+2 decision reads -- carried exactly two
  // families, and nothing on this page said so.  Counted off the serving
  // inbox, so it is what P0 gets rather than what any lane reports.
  const COV=(W.native_inbox_coverage||[]);
  const near=COV[0];
  const held=(W.native_inbox_held_families||[]);
  push("upstream", W.native_inbox_health||"muted",
    "包里有几家",
    near?`${near.family_count}/${near.expected_family_count}`:"—",
    COV.length
      ? COV.map(c=>`${h(c.event_date.slice(5))} ${c.family_count}/${
          c.expected_family_count}`+(c.absent.length
            ? `（缺 ${c.absent.map(h).join("、")}）` : "")).join(" · ")
      : "服务清单读不到",
    "「服务清单」里每个事件日的包实际带了几家模型 —— 决策真正读到的那份。"
    +"上面那张按各家自己的节奏算到货，一份只有两家的包在那里看不出来。"
    +"最远那天（D+2）白天本来就会短（ECCC 的 12Z、ICON 的自有轮次都在傍晚才发），"
    +"所以只有 D+1 及更近的日子短了才点黄。"
    +(held.length
      ? "　上一轮采集有家没跟上：" + held.map(x=>
          `${h(x.label)}（${h(x.reason)}）`).join("；")
      : ""));

  // 4. How much of the live fleet actually filled on the newest event day
  // that has settled anything, against the stations allowed to trade.  The
  // newest day with orders is still opening -- tomorrow's P0 has filled two
  // stations by the evening before -- and would read as a collapse.
  const day0=(D.daily||[]).find(d=>d.settled_station_days>0)
    ||(D.daily||[])[0]||null;
  const liveStations=((D.live_readiness||{}).station_allowlist||[]).length;
  const opened=day0?day0.stations_filled:null;
  const rate=(day0&&liveStations)?opened/liveStations:null;
  push("selection", band(rate,0.5,0.25), "实盘建仓率",
    `${opened==null?"—":opened}/${liveStations||"—"}`,
    `${h((day0||{}).event_date||"")} · ${pct0(rate)} 的实盘站成交了`,
    "当天有实盘成交的站 ÷ 允许交易的站；逐站逐日明细是同一本实盘账");

  // 6. Does the model's stated probability survive contact with the truth.
  const P0=(S.stages||{}).P0||{};
  const cal=P0.calibration_ratio;
  push("selection", cal==null?"muted":(cal>=0.85&&cal<=1.15)?"green"
      :(cal>=0.6&&cal<=1.4)?"amber":"red",
    "说得准不准", cal==null?"—":cal.toFixed(2)+"×",
    `实盘 P0 已结算 ${P0.settled_count||0} 笔：买到的篮子命中率 ÷ 声称胜率，1.00× 是说得准`,
    "「band 表」与「覆盖 · 价格 · EV」：概率分档校准");

  // 6b. Does the basket the objective CHOSE cover the truth, at the widths
  // the charter actually seats -- and is that moving.  Grouped on the
  // chain-level union of intended legs (hedge rungs included), not the
  // posted or filled set: the market sells us part of a three-rung choice
  // and the narrower groupings file that day under one rung, emptying the
  // three-rung row of exactly its hardest cases.
  //
  // The trend is the later half of settlements against the earlier, split
  // where the counts are even rather than at the middle date.  A direction
  // is only printed when it clears two combined standard errors; under
  // that it is a coin landing differently and saying "worse" would invent
  // a signal the sample cannot carry.
  ((S.chosen_basket_hit||{}).sizes||[]).forEach(row=>{
    const hitv=row.hit_rate;
    const arrow=row.delta==null?""
      :!row.delta_is_readable?"→"
      :row.delta>0?"↑":"↓";
    const move=row.delta==null?`${row.settled_count} 站日 · 趋势样本不足`
      :!row.delta_is_readable
        ?`${row.settled_count} 站日 · 与噪音不可分（Δ${
          (row.delta*100>=0?"+":"")+(row.delta*100).toFixed(0)}，需 ±${
          (2*row.delta_stderr*100).toFixed(0)}）`
      :`${row.settled_count} 站日 · 后半 ${
        (row.delta*100>=0?"+":"")+(row.delta*100).toFixed(0)} 点`;
    // A width the book has only tried once or twice gets its row and no
    // verdict.  On 2026-09-12 the four-bucket row was one legacy chain
    // that missed, and a red 0% sat beside a three-bucket row built on
    // 36 -- the same shade the page uses for a width that is genuinely
    // failing.  The widths themselves are the runtime's (charter 131
    // seats 2-3; a chain's union reaches 4 when P0-M or P1 adds a rung),
    // so a seat rule that moves moves these rows with it.
    const thin = row.rate_is_readable===false;
    push("selection", hitv==null||thin?"muted":band(hitv,0.5,0.33),
      `${row.chosen_size} 桶挂出命中${row.seat_legal===false?"（加腿并集）":""} ${arrow}`,
      pct0(hitv), thin
        ? `${row.settled_count} 站日 · 不足 ${row.min_chains||8} 个，不判色`
        : move,
      `实盘挂到交易所的腿集合（跨阶段并集）盖住结算真值的比率。` +
      (row.seat_legal===false
        ? `宪章 131 的席位是 2–3 桶，这一行是 P0-M/P1 加腿后并集到 ${
            row.chosen_size} 桶的站日，不是开仓坐出来的宽度。` : "") +
      `买到的篮子只中 ${pct0(row.filled_hit_rate)}` +
      `，差额是市场没把会赢的那条腿卖给我们。声称 ${
      pct0(row.claimed_mean)}，实现/声称 ${
      row.realised_over_claimed==null?"—"
        :row.realised_over_claimed.toFixed(2)}×。见「篮子命中率」的脱靶分布`);
  });

  return {banner:"", ...G};
}


// A run stamp is the one timestamp on this page read as an identity rather
// than as a moment: 06Z says which cycle of the day the model ran, and two
// families are comparable only cycle against cycle.
const runLabel = (s) => {
  const m = String(s == null ? "" : s).match(/^\d{4}-(\d{2})-(\d{2})T(\d{2})/);
  return m ? `${m[1]}-${m[2]} ${m[3]}Z` : "—";
};
// One glyph per question (user ruling, 2026-09-11).  Seven sections --
// four about the machine, three about what feeds it -- were tables and
// card grids, and every one of them was opened to answer a single binary:
// is it moving.  They are lamps now.  The reading that was the point of
// each table rides on the lamp, whatever else it knew rides in the
// tooltip, and the long explanations live in the commit that removed
// them.  Where the payload states a health -- the members, the chain
// links, the regional lanes, truth materialisation -- the lamp shows that
// field rather than a rule re-derived here: two places deciding what red
// means is how they end up disagreeing.  Two payloads state none (the
// store's capacity and the sweep lanes), and for those the lamp carries
// the same threshold the deleted section drew its warning line on.
const attr = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
const lamp = (health, name, reading, tip) =>
  `<div class="lamp${health === "red" ? " bad" : ""}"${
    tip ? ` title="${attr(tip)}"` : ""}><span class="dot ${
    health || "amber"}"></span><b>${h(name)}</b><span class="rd">${
    reading == null ? "—" : reading}</span></div>`;
const lampRow = (caption, lamps) => lamps.length
  ? `<div class="lampdeck"><span class="lampcap">${caption}</span>
     <div class="lamps">${lamps.join("")}</div></div>` : "";
// The head is now the one element the page cannot afford to lose: the
// banner light, the production cycle and the render stamp are all in the
// same card as the lamps, and ``safe`` wraps the head as a whole.  So a
// lamp row that throws would take the banner down with it.  Each row
// catches its own instead, and says so as a lamp.
const lampRowSafe = (fn, caption) => {
  try { return fn(); }
  catch (err) {
    return lampRow(caption, [lamp("red", "灯板出错", String(err))]);
  }
};

function upstreamLamps(){
  const L = [];
  (D.members || []).forEach(m => {
    // Late by a whole cycle is late enough to say out loud; publication
    // times wander by hours below that.
    const late = m.overdue_hours != null && m.cycle_hours != null
      && m.overdue_hours > m.cycle_hours;
    L.push(lamp(m.health, m.label,
      `${runLabel(m.latest_run_utc)} · ${hours(m.latest_capture_age_hours)}落库`,
      `${m.stations} 站 · 存档 ${m.training_days} 天 · 被选进融合 ${m.seats_won} 次`
      + (late ? ` · 下一轮已晚 ${hours(m.overdue_hours).replace("前", "")}` : "")
      + (m.backfillable ? "" : " · 不留历史，当天没抓到就永久缺失")));
  });
  ((D.regional || {}).lanes || []).forEach(l => L.push(lamp(l.health, l.label,
    `${l.station_count || 0}/${l.roster_count || 0} 站 · ${
      hours(l.last_capture_age_hours)}`,
    `${l.region} · ${(l.row_count || 0).toLocaleString()} 行 · ${l.transport}`
    + (l.backfillable ? "" : " · 不留历史，错过永失"))));
  // The lane lamps say the corpus is arriving; this one says what the
  // corpus has won.  They are different questions and a green lane with an
  // empty scoreboard is a real state -- still accumulating, nothing wrong.
  const V = (D.regional || {}).verdicts || [];
  if(V.length){
    const passAll = V.filter(v => v.passes_all_three).length;
    const cut = V.filter(v => !v.in_roster).length;
    const waiting = V.filter(v => v.in_roster && !v.has_rows).length;
    const accruing = ((D.regional || {}).readiness || [])
      .filter(r => r.state === "ACCUMULATING_STRICT_OOS").length;
    L.push(lamp(passAll ? "green" : "amber", "区域赛道",
      `${passAll}/${V.length} 过三门`,
      `攒案例中的系统 ${accruing} 个 · 实测反向剔除 ${cut} 对`
      + ` · 在册等首行 ${waiting} 对`
      + " · 赢到的是扩展概率包络的证据席位，不是融合成员"));
  }
  const g = D.gaps || [];
  const high = g.filter(x => x.severity === "high").length;
  const med = g.filter(x => x.severity === "medium").length;
  const manual = g.filter(x => !x.self_heals).length;
  L.push(lamp(high ? "red" : (med ? "amber" : "green"), "缺口检查",
    g.length ? `${high} 要处理 · ${med} 留意 · ${g.length - high - med} 等样本`
      : "没有缺口",
    g.length
      ? g.slice(0, 8).map(x => x.what).join("；")
        + (manual ? ` · 其中 ${manual} 条不会自己好` : " · 都会自行恢复")
      : "每一项检查都通过了"));
  return L;
}

function healthLamps(){
  // The chain first, one lamp per link, in flow order: whichever one
  // stopped, everything under it stops next.
  const L = (D.chain || []).map(c => lamp(c.health, c.name, c.volume,
    `${c.plain} · ${c.note}`));
  const S = D.ssot_capacity || {};
  if(S.available){
    const GB = 1e9, days = S.days_to_ceiling;
    const tight = S.remaining_bytes < S.warn_remaining_bytes
      || (days != null && days <= S.warn_days_to_ceiling);
    L.push(lamp(tight ? "red" : "green", "SSOT 容量",
      `剩 ${(S.remaining_bytes / GB).toFixed(1)} GB · ${
        days == null ? "速率待测" : days.toFixed(0) + " 天"}`,
      `上限 ${(S.max_database_bytes / GB).toFixed(1)} GB · 已用 ${
        (100 * S.database_bytes / S.max_database_bytes).toFixed(1)}%`
      + (S.largest_table ? ` · 增得最快的表是 ${S.largest_table}` : "")
      + " · 撞顶＝每个写者 fail-close，整条链路当场停"));
  } else {
    L.push(lamp("amber", "SSOT 容量", "读不出", S.reason || ""));
  }
  const A = D.delta_v2_authority || {};
  if(A.available){
    const days = v => v == null ? "—" : (v >= 60 ? Math.round(v) : v.toFixed(1)) + " 天";
    L.push(lamp(A.health, "执行授权有效期",
      `成交概率 ${days(A.fill_days_left)} · 策略 ${days(A.policy_days_left)}`,
      (A.note ? A.note + " · " : "")
      + `成交概率是 ${A.fit_age_days == null ? "—" : A.fit_age_days.toFixed(0)} 天前拟合的（每周重拟合）`
      + " · 到期＝每个候选都解析不出成交概率或策略，全队当场停"));
  } else {
    L.push(lamp("amber", "执行授权有效期", "读不出", A.note || ""));
  }
  const H = D.hosted_sweeps || {};
  if(H.available){
    const bad = (H.lanes || []).filter(l => l.health !== "green");
    L.push(lamp(H.red_count ? "red" : (H.amber_count ? "amber" : "green"),
      "维护车道",
      (H.red_count || H.amber_count)
        ? `${H.red_count} 红 · ${H.amber_count} 黄` : "全部准点",
      bad.length
        // Starved and stuck are opposite fixes, so the tooltip says which:
        // stuck holds the exclusive write domain and blocks everyone behind
        // it, starved only needs a generation that lives long enough.
        ? bad.map(l => `${l.name} 落后 ${(l.overdue_intervals || 0).toFixed(1)}`
            + ` 个回合（${l.stuck ? "卡住，占着写域" : "没轮到"}）`).join("；")
        : "每条车道都在自己的间隔内跑过"));
  } else {
    L.push(lamp("amber", "维护车道", "读不出", H.reason || ""));
  }
  const T = D.truth || {};
  if((T.coverage || []).length){
    // Its own health, the same field the head strip reads.  The banner
    // already decides what an outstanding count means; a second rule here
    // is how the strip and the lamp end up disagreeing about the same day.
    L.push(lamp(T.health || "amber",
      "真值物化", `落后 ${T.outstanding} · 挡住 ${T.refused}`,
      `上次清扫 ${hours(T.sweep_age_hours)} · 当天还在结算 ${T.still_open} 个`
      + " · 「落后」不为零才是真的落后；「挡住」是证据不全或互相矛盾，"
      + "错过的日子没有任何东西会重试"));
  }
  return L;
}

function tables(){
  const t = D.tables;
  return `<h2>每站的偏差订正与融合权重：修正各模型的系统偏差，定下融合时各占多少</h2>
  <p class="sub">这两张表是"每个模型在每个站偏多少度"和"融合时各模型占多大比重"，
  每天自动重算、自动换代。</p>
  <div class="grid g3">
    <div class="card"><div class="muted">偏差订正表</div>
      <div class="big">${t.correction.stations} 站</div>
      <div class="muted">${t.correction.activations.toLocaleString()} 次激活 ·
        最近更新 ${hours(t.correction.age_hours)}</div></div>
    <div class="card"><div class="muted">融合权重表</div>
      <div class="big">${t.weights.stations} 站</div>
      <div class="muted">${t.weights.activations.toLocaleString()} 次激活 ·
        最近更新 ${hours(t.weights.age_hours)}</div></div>
    <div class="card"><div class="muted">权重落后订正的格子</div>
      <div class="big ${t.weight_lag_scopes ? "down" : ""}">
        ${t.weight_lag_scopes}</div>
      <div class="muted">落后 3 天以上的站×提前量组合；
        权重要过检验才换代，持续不过就会停在旧版本</div></div>
  </div>`;
}

function stages(){
  if(!D.stages.length) return "";
  const cards = D.stages.map(s => {
    const pct = s.accumulating && s.min_days ?
      Math.min(100, 100 * s.serving_days / s.min_days) : 100;
    // The rows are the TABLE's own axis, never the page's preference
    // (charter 105).  A stage fitted on distance from the peak has five
    // cells that are not probabilities at all, and reading them through
    // the claim grid put the [0,1) cell -- which every claim midpoint
    // falls into -- on all eleven rows: one factor, printed eleven times,
    // beside a header that said "4 档生效".  That was the page lying
    // about a table production was applying correctly.
    const axis = s.axis || "claim";
    const distance = axis === "centre_distance";
    // Accumulating HOLDS the incumbent, it does not blank it: the factors
    // on a held card are live on every decision, and the ratio beside each
    // one is the fit that set it, not today's (which has no report yet).
    const held = s.held_from || null;
    const heldAt = held ? h(held.fitted_at_utc).slice(5, 16).replace("T", " ") : "";
    const reasonFor = (basis, blocked) =>
      blocked === "interval_spans_one" || basis.includes("interval_spans_one")
        ? "统计上分不清是真偏还是巧合" :
      blocked === "sign_flips_between_halves" || basis.includes("sign_flips")
        ? "前后半段偏的方向相反，不稳定" :
      basis.includes("forward") ? "试算后反而变差，不采用" :
      basis.includes("no_sample") ? "这一档没有样本" : "证据未够格";
    const cell = (b, rowLabel, cellNote) => {
      const f = b.factor;
      const cls = f > 1.001 ? "up" : (f < 0.999 ? "down" : "flat");
      const arrow = f > 1.001 ? "调高" : (f < 0.999 ? "调低" : "不动");
      const measured = b.measured_ratio == null ? "—" :
        b.measured_ratio.toFixed(2) + "×";
      const basis = b.basis || "";
      const heldBasis = b.held_basis || "";
      const why = held && basis.includes("accumulating")
        ? (Math.abs(f - 1) > 1e-9
            ? `沿用 ${heldAt} 那版，照常生效；新证据攒满再重拟`
            : `沿用 ${heldAt} 那版：此档不修（${reasonFor(heldBasis, b.blocked_by)}）`)
        : Math.abs(f - 1) > 1e-9
        ? (basis === "gate_transfer"
            ? "本阶段样本还不够，但另一套证据独立测出同方向，已生效"
            : basis.includes("accumulating")
            ? "沿用上一版，照常生效；新证据攒满再重拟"
            : "证据充分，已生效")
        : (b.blocked_by === "interval_spans_one" ? "统计上还分不清是真偏还是巧合" :
           b.blocked_by === "sign_flips_between_halves" ? "前后半段偏的方向相反，不稳定" :
           basis.includes("accumulating") ? "样本还不够，先不动" :
           basis.includes("forward") ? "试算后反而变差，不采用" :
           basis.includes("no_sample") ? "这一档还没有样本" : "暂不修正");
      return `<tr>
        <td class="mono">${rowLabel}</td>
        <td>${measured}${cellNote}</td>
        <td class="factor ${cls}">${f.toFixed(3)} <span class="muted">${arrow}</span></td>
        <td class="muted">${why}</td></tr>`;
    };
    // Distance axis: one row per fitted cell, no display grid to map onto
    // -- the cells ARE the rungs.  The open top cell is every bucket four
    // or more steps out from the peak.
    const distanceRows = () => (s.bands || []).map(b => {
      const lo = Math.round(b.lower);
      const rowLabel = lo === 0 ? "峰值桶"
        : (b.upper >= 90 ? `离峰 ≥${lo} 格` : `离峰 ${lo} 格`);
      return cell(b, rowLabel, "");
    }).join("");
    // Five-percent display grid capped at 50% (user ruling, 2026-08-25),
    // the same rows the audit table below uses, so knob and audit line up.
    // A display bin belongs to the fitted cell holding its midpoint; the
    // fitted cells are wider than 5%, so consecutive rows sharing a cell
    // repeat its factor and say which cell owns them.
    const claimRows = () => {
      const GRID = [];
      for (let i = 0; i < 10; i++) GRID.push([i*5, i*5+5]);
      GRID.push([50, 100]);
      return GRID.map(([lo, hi]) => {
        const mid = (lo + hi) / 2 / 100;
        const b = (s.bands || []).find(x => mid >= x.lower && mid < x.upper);
        if (!b) return "";
        const cellLabel = `${(b.lower*100).toFixed(0)}–${
          Math.min(100, b.upper*100).toFixed(0)}%`;
        const rowLabel = hi >= 100 ? "≥50%" : `${lo}–${hi}%`;
        const cellNote = cellLabel === `${lo}–${hi}%` ||
          (hi >= 100 && b.lower >= 0.5)
          ? "" : ` <span class="muted">档 ${cellLabel}</span>`;
        return cell(b, rowLabel, cellNote);
      }).join("");
    };
    const bands = distance ? distanceRows() : claimRows();
    // Every outcome the journal's CHECK admits, by name: the two added with
    // the veto and the write-lock race used to fall through to "攒证据中".
    const OUTCOME = {ACTIVATED:"换新版本", UNCHANGED:"与上次相同",
      ACCUMULATING:"攒证据中", INCUMBENT_VETOED:"现役被否决·退役",
      FIT_NOT_ACTIVATED:"拟合成、未激活"};
    const journal = (s.journal || []).slice(0, 5).map(j =>
      `<tr><td class="mono">${h(j.at).slice(0,16).replace("T"," ")}</td>
       <td>${h(OUTCOME[j.outcome] || j.outcome)}</td>
       <td>${j.days} 天</td><td>${j.cells} 档生效</td>
       <td class="mono muted">${h(j.sha)}</td></tr>`).join("");
    // What the stage's recent decision rows applied.  Printed only when it
    // is not simply the table named above: P0 pins a table per artifact, so
    // an older artifact keeps serving an older table.
    const sv = s.served_versions || null;
    const svOthers = sv ? (sv.others || []) : [];
    const served = sv && (svOthers.length || sv.unreceipted_rows)
      ? `<div class="muted" style="margin-top:4px">近 ${sv.hours}h 决策行
        ${sv.active_rows}/${sv.rows} 用这一版${svOthers.length
          ? `；${svOthers.reduce((n, o) => n + o.rows, 0)} 行仍用旧 artifact 钉住的版本（${
            svOthers.slice(0, 3).map(o => `<span class="mono">${h(o.sha)}</span> ${
              o.axis === "centre_distance" ? "离峰距离" : "声称概率"}轴 ${o.rows} 行`)
            .join("、")}${svOthers.length > 3 ? ` 等 ${svOthers.length} 版` : ""}）` : ""}${sv.unreceipted_rows
          ? `；${sv.unreceipted_rows} 行无 band 回执` : ""}</div>` : "";
    return `<div class="card">
      <div style="display:flex;align-items:center;gap:8px">
        <span class="dot ${s.health}"></span><b>${h(s.stage)}</b>
        <span class="muted">${h(s.meaning)}</span>
        <span style="flex:1"></span>
        <span class="pill">${distance ? "按离峰距离分档" : "按声称概率分档"}</span>
        <span class="pill ${s.health}">${h(s.state)}</span>
      </div>
      <div class="muted" style="margin-top:8px">
        证据：${s.serving_days} 天 / ${s.station_days || 0} 个站日 ·
        当前版本 <span class="mono">${h(s.table_sha)}</span> ·
        上次拟合 ${hours(s.fit_age_hours)}</div>
      ${held ? `<div class="muted" style="margin-top:4px">系数与「实际兑现」沿用
        ${heldAt} 那次拟合（${held.days} 天 / ${held.station_days} 个站日）；
        上面的天数是服务算术换代后重新攒的</div>` : ""}
      ${served}
      ${s.accumulating ? `<div class="bar"><i style="width:${pct}%"></i></div>
        <div class="muted" style="margin-top:5px">${s.active_cells
          ? `攒够 ${s.min_days} 天自动重拟：这 ${s.active_cells} 档保留、改值或撤回；
            在那之前照常生效。不需要改代码`
          : `攒够 ${s.min_days} 天就会自动开始尝试修正，不需要改代码`}</div>` : ""}
      <table style="margin-top:12px"><thead><tr>
        <th>${distance ? "桶离峰距离" : "概率档"}</th>
        <th>实际兑现</th><th>修正系数</th><th>为什么</th>
      </tr></thead><tbody>${bands}</tbody></table>
      ${journal ? `<details style="margin-top:8px">
      <summary class="muted" style="cursor:pointer">最近几次自动拟合</summary>
      <table><thead><tr><th>时间</th><th>结果</th><th>样本</th>
      <th>生效档数</th><th>版本</th></tr></thead>
      <tbody>${journal}</tbody></table></details>` : ""}
    </div>`;
  }).join("");
  const axisNote = D.stages.map(s => `${s.stage}＝${
    (s.axis || "claim") === "centre_distance" ? "离峰距离" : "声称概率"}`)
    .join("、");
  return `<h2>band 表：用真值校准模型概率</h2>
  <p class="sub">系统会记录"凡是说过 X% 的地方，后来实际发生了多少次"。
  如果长期说 6% 只发生 4%，就把这一档的数字乘一个小于 1 的系数压回去。</p>
  <div class="why"><b>三张卡不一定按同一个东西分档</b>（第 105 条）：
  当前 ${h(axisNote)}。<b>声称概率</b>是按"这个桶报了几成"分；
  <b>离峰距离</b>是按"这个桶离当天分布的最高点差几格"分——
  这张表治的病是过度分散（峰上压得太薄、四格开外压得太厚），那是一句关于
  <b>位置</b>的话，而位置在乘完系数、重新归一之后不会变，声称概率会变。
  每张卡的行就是它自己那根轴，不再互相翻译。</div>
  <div class="why" style="margin-top:8px"><b>怎么看这张表：</b>「实际兑现」是实测兑现率除以声称概率——
  1.00× 表示说得准，0.69× 表示说大话（只兑现了 69%），1.23× 表示说小话。
  「修正系数」是实际乘上去的那个数，只有证据足够扎实的档才会不等于 1.000，
  而且取的是保守值——宁可少改，不可改过头。
  服务算术一换代，证据要从锚点重新攒；<b>攒满 10 天之前沿用上一版的系数、照常生效</b>，
  卡片会写明沿用自哪次拟合，「实际兑现」也是那次拟合的读数。</div>
  <div class="why" style="margin-top:8px"><b>这一节是一条闭环，分三步：</b><br>
  ① <b>记账</b>——三张卡各自记录本阶段"凡是说过 X% 的地方，后来兑现了多少"；<br>
  ② <b>拧旋钮</b>——证据过门的档拟出修正系数，乘进下一次服务的概率。
  这是全链<b>唯一改变数字</b>的一步；<br>
  ③ <b>体检</b>——下面「说了多少 vs 真发生了多少」量的是系数生效<b>之后</b>的
  剩余偏差，<b>分 stage、与上表同一取样车道、同一证据起点</b>
  （2026-08-25 起；此前它读的是训练端 OOS 预测，人群不同曾与上表互相矛盾）。
  它只诊断、不回写。<b>体检跟着上表那根轴走</b>（2026-09-09）：
  上表按离峰距离分档的 stage，体检也按离峰距离分档，三张卡都能逐行对照。
  在此之前体检恒按声称概率的 5% 尺分档，于是 P0 与 P1 这两个已换轴的 stage
  被摊在一张它们从没被评分过的网格上，看不出任何一档的系数有没有起作用。
  若某档残差持续变大，说明对应旋钮方向或粒度不对——回到 ②。</div>
  <div class="grid g3" style="margin-top:8px">${cards}</div>
  ${reliabilityAudit()}`;
}

// The shadow candidate for the knob section six turns.  It is rendered
// beside that section on purpose: the question it answers -- is one
// factor per band per stage too coarse -- is unreadable without the table
// it would replace sitting next to it.
// Charter-level knob, its shadow, and its audit are one reading, not three
// sections (user ruling, 2026-08-29): the shadow's whole question is "should
// the table above be split finer", and answering it two headings away made
// readers compare a pooled row with a split row by scrolling.
function bandTable(){
  // The edge thermometer and the lambda table used to render here (user
  // ruling, 2026-09-01: the band table audits claimed probability, the
  // thermometer audits claimed edge, and they are one reading).  They are
  // their own sections again as of 2026-09-11 -- not because that reading
  // was wrong, but because a section is also an entry in the jump bar, and
  // folded in here neither table had one: a reader who wanted the lambda
  // table had to know it was inside "band 表" and scroll.  The reading is
  // preserved by adjacency instead: all three sit together, in order, in
  // this group, and all three are starred in the bar.
  //
  // The convergence lift left for the same reason on 2026-09-13: it is a
  // claim-layer lane in SHADOW, and folded in behind a live section's
  // heading it inherited that section's lane strip and had no chip of its
  // own -- which is exactly the live/shadow confusion the strip exists to
  // end.
  return stages() + bandShadow();
}


function bandShadow(){
  const S = D.band_shadow || {};
  if(!(S.stages||[]).length) return "";
  const g = S.gates || {};
  const cards = S.stages.map(s => {
    // Three columns, not five: these cards sit three to a row, and a
    // five-column table there breaks its own header mid-word.  The funnel
    // reads better as one cell anyway -- 1091 → 0 → 0 is the story, and
    // splitting it across columns makes the reader reassemble it.
    const levels = (s.levels||[]).map(L => {
      const cls = L.promoted > 0 ? "up" : "flat";
      const blocked = (L.blocked||[]).map(b =>
        `${h(b.reason)} ${b.n}`).join("；");
      return `<tr>
        <td style="white-space:nowrap">${h(L.label)}</td>
        <td class="mono" style="white-space:nowrap">${L.tested} →
          ${L.eligible} → <span class="factor ${cls}">${L.promoted}</span></td>
        <td class="muted">${blocked || "—"}</td></tr>`;
    }).join("");
    // The shadow is keyed on the pooled table's axis: a distance rung
    // printed as a probability read "100–100%" (charter 105's bug, second
    // renderer).
    const shadowDistance = s.axis === "centre_distance";
    const rung = band => {
      const lo = Math.round(band[0]);
      return lo === 0 ? "峰值桶" : (band[1] >= 90 ? `离峰 ≥${lo} 格` : `离峰 ${lo} 格`);
    };
    const promoted = (s.promoted||[]).map(p => {
      const cls = p.factor > 1.001 ? "up" : (p.factor < 0.999 ? "down" : "flat");
      const arrow = p.factor > 1.001 ? "调高" : (p.factor < 0.999 ? "调低" : "不动");
      return `<tr>
        <td>${h(p.group_label)}${p.station ? " · " + h(p.station) : ""}</td>
        <td class="mono">${shadowDistance ? rung(p.band) : `${(p.band[0]*100).toFixed(0)}–${
          Math.min(100, p.band[1]*100).toFixed(0)}%`}</td>
        <td class="factor ${cls}">${p.factor.toFixed(3)}
          <span class="muted">${arrow}</span></td></tr>`;
    }).join("");
    const dist = (S.distance||[]).filter(d => d.stage === s.stage);
    const distRows = dist.map(d => {
      const label = d.level === "station" ? "站" : "站 × 桶位置";
      // Days REMAINING, not days total.  days_needed counts from the
      // start of accumulation, and reporting it raw reads as "37 more
      // days" when 28 of those 37 are already banked.
      const left = d.days_needed == null ? null
        : Math.max(0, Math.ceil(d.days_needed - d.days_now));
      const eta = d.reached > 0 ? "已达标"
        : (left == null ? "—" : `还需约 ${left} 天`);
      const pct = Math.min(100, 100 * d.best_mass / d.floor);
      return `<tr>
        <td style="white-space:nowrap">${label}<br>
          <span class="muted">${d.cells} 格</span></td>
        <td><div class="bar" style="margin:0"><i style="width:${pct}%"></i></div>
          <span class="muted">最肥格 ${d.best_mass.toFixed(2)} /
          门槛 ${d.floor.toFixed(1)}</span></td>
        <td>${eta}</td></tr>`;
    }).join("");
    const journal = (s.journal||[]).slice(0,5).map(j =>
      `<tr><td class="mono">${h(j.at).slice(0,16).replace("T"," ")}</td>
       <td>${j.days} 天</td><td>${j.promoted} 格继承非池因子</td>
       <td class="mono">${j.gain >= 0 ? "+" : ""}${j.gain.toFixed(2)}</td>
       <td class="mono muted">${h(j.sha)}</td></tr>`).join("");
    return `<div class="card">
      <div style="display:flex;align-items:center;gap:8px">
        <b>${h(s.stage)}</b><span class="muted">${h(s.meaning)}</span>
        <span style="flex:1"></span>
        <span class="pill">影子 · 不服务</span>
      </div>
      <div class="muted" style="margin-top:8px">
        证据：${s.serving_days} 天 / ${s.station_days} 个站日 ·
        版本 <span class="mono">${h(s.sha)}</span> ·
        上次拟合 ${hours(s.fit_age_hours)}</div>
      <table style="margin-top:12px"><thead><tr>
        <th>维度</th><th style="white-space:nowrap">测→过门→够格</th>
        <th>卡在哪一门</th></tr></thead><tbody>${levels}</tbody></table>
      ${promoted ? `<div class="muted" style="margin-top:10px">够格的格子
        （前向窗 ${s.forward_nll_gain >= 0 ? "+" : ""}${
        s.forward_nll_gain.toFixed(2)} NLL）：</div>
      <table><thead><tr><th>位置</th><th>${shadowDistance ? "离峰距离" : "概率档"}</th><th>系数</th>
      </tr></thead><tbody>${promoted}</tbody></table>` :
      `<div class="muted" style="margin-top:10px">本次没有任何格子够格，
        全部回退池权重。</div>`}
      ${distRows ? `<div class="muted" style="margin-top:10px">饿死的维度
        还差多远：</div>
      <table><thead><tr><th>维度</th><th>最肥格的证据量</th>
      <th>按当前速率</th></tr></thead><tbody>${distRows}</tbody></table>` : ""}
      ${journal ? `<details style="margin-top:8px">
      <summary class="muted" style="cursor:pointer">最近几次影子拟合</summary>
      <table><thead><tr><th>时间</th><th>样本</th><th>继承格数</th>
      <th>前向窗</th><th>版本</th></tr></thead>
      <tbody>${journal}</tbody></table></details>` : ""}
    </div>`;
  }).join("");
  return `<h3 style="margin-top:30px">影子 band 表：拆细了值不值得</h3>
  <p class="sub">上面那张表是<b>全舰队共用</b>的——四十五个站、一天里所有温度桶，
  只要落在同一档（按该 stage 那根轴：离峰距离或声称概率），就吃同一个系数。
  这一节在问：拆开来会不会更准？</p>
  <div class="why"><b>拆成两维试：</b>一维是<b>站</b>（成都和纽约分开记账），
  一维是<b>桶位置</b>（离当天最高点最近的那个桶，和远在偏冷／偏暖一侧的桶，分开记账）。
  拆完之后每一格都要自己重新过三道门；<b>过不了的格子，退回去用全舰队那个折扣</b>
  ——不是退回"不打折"，那样会把舰队层面已经测准的修正一起扔掉。</div>
  <div class="why" style="margin-top:8px"><b>这一节不改任何数字。</b>
  它是影子层：只记录"如果按这个更细的表来，会怎样"，服务端仍然走上一节那张池化表。
  真要启用，得改代码并过账本重放，不是这里点一下就生效。</div>
  <div class="why" style="margin-top:8px"><b>三道门为薄格重设过：</b>
  ①<b>样本门</b>——这一格至少要攒到 ${g.min_claimed_mass} 个"期望命中"、
  跨 ${g.min_days} 天、${g.min_rows} 行；攒不到就根本判不出该调高还是调低。
  ②<b>显著门</b>——按天重抽样，并做<b>多重比较校正</b>（FDR ${g.fdr_q}）：
  同时检验几千格时，不校正会凭空冒出上百个假阳性。
  ③<b>试算门</b>——把这个系数放进最近四成的日子里重算一遍，真的更准才采用，
  而且比的是"退回池权重"那个基准，不是"完全不修正"。</div>
  <div class="why" style="margin-top:8px"><b>怎么读「测→过门→够格」：</b>
  这是一条漏斗——先看这一维拆出<b>多少格</b>，再看<b>多少格攒够了样本</b>，
  最后看<b>多少格真的自己测出了系数</b>。最后那个数只算<b>这一维自己测出来</b>的；
  卡片下方「最近几次影子拟合」里的「继承格数」是另一回事——一个全舰队的桶位置
  系数会被两百多个细格继承，那<b>不代表</b>两百多个站各自测准了。</div>
  <div class="grid g3" style="margin-top:8px">${cards}</div>`;
}

// Step ③ of section 4: the post-correction residual, pooled and re-binned.
// Lives beside the knob it audits; reads the scorecard payload so the two
// sections cannot drift apart on data.
function reliabilityAudit(){
  // Same lane, same stages, same evidence anchors, same cells as the
  // knob table above -- the audit that lived here before read the training
  // lane's OOS holdout and disagreed with the knobs on the page for
  // population reasons, not calibration ones.
  const A = D.band_residual_audit || {};
  const sinceLabel = s => s.since
    ? `自 ${h(s.since).slice(5,16).replace("T"," ")}Z 起（服务算术锚，与上表拟合同一证据起点）`
    : "全历史";
  const stageBlocks = (A.stages||[]).map(s => {
    const rows = (s.rows||[])
      .filter(b => b.n >= 30)
      .map(b => {
        const gap = b.happened - b.promised;
        const tone = Math.abs(gap) <= 0.05 ? "green"
                   : (Math.abs(gap) <= 0.10 ? "amber" : "red");
        return `<tr>
          <td class="mono">${h(b.bin)}</td>
          <td class="mono muted">${b.n.toLocaleString()}</td>
          <td class="mono">${(b.promised*100).toFixed(1)}%</td>
          <td class="mono">${(b.happened*100).toFixed(1)}%</td>
          <td class="mono ${gap>0?"up":"down"}">
            <span class="dot ${tone}"></span>
            ${gap>0?"+":""}${(gap*100).toFixed(1)} 点</td>
        </tr>`;
      }).join("");
    const distance = s.axis === "centre_distance";
    const skipped = s.skipped_station_days || 0;
    // An empty stage says why rather than vanishing: right after the
    // arithmetic anchor moves every stage is thin, and an identity table
    // moves nothing at all.
    if(!rows) return `<div class="muted" style="margin-top:10px"><b>${h(s.stage)}</b> ·
      ${sinceLabel(s)} · ${(s.station_days || 0)
        ? `${s.station_days} 个站日被 band 改动过，但每档样本都不足 30，暂无可读残差`
        : `band 没有改动过任何站日（表为恒等或支持集里无可修的桶），没有残差可查`}</div>`;
    return `<div style="margin-top:10px">
      <div class="muted"><b>${h(s.stage)}</b> · ${sinceLabel(s)} · ${s.day_count} 天 ·
        ${s.station_days || 0} 个站日 · 每站日首个决策 · 与上表同轴（${
          distance ? "按离峰距离分档" : "按声称概率分档"}）${
        skipped ? ` · <span title="band 层没碰过的站日：早于该层上线，或支持集里没有可修的桶。它们不是「修完还剩多少」的证据。">另有 ${
          skipped} 个站日 band 未生效，不计入</span>` : ""}</div>
      <table style="margin-top:6px"><thead><tr>
        <th>${distance ? "桶离峰距离" : "声称档"}</th>
        <th>样本</th><th>平均声称</th><th>实际发生</th><th>差</th>
      </tr></thead><tbody>${rows}</tbody></table></div>`;
  }).join("");
  const stationRows=(A.stations||[]).map(r=>`<tr>
      <td class="mono">${h(r.station)}${r.station_label
        ?` <span class="muted">${h(r.station_label)}</span>`:""}</td>
      <td class="mono muted">${r.n}</td>
      <td class="mono">${(r.claimed*100).toFixed(0)}%</td>
      <td class="mono">${(r.realized*100).toFixed(0)}%</td>
      <td class="mono ${r.ratio>=1?"up":"down"}">${r.ratio.toFixed(2)}${
        r.significant?' <b title="按 2σ 显著偏离 1">✱</b>':""}</td>
    </tr>`).join("");
  const stationBlock=!stationRows?"":`<div style="margin-top:14px">
    <div class="muted"><b>每站 实现/声称（中心±1，三 stage 合并，全历史）</b> ·
    与上面各档不同，这张表<b>不随服务算术锚重置</b>（按锚点切，每站只剩几个站日），
    上线就绪页的「声称诚实」门读的就是这一列 ·
    band 是全舰队池化的，这张表是「band 要不要分站」的法庭：
    带 ✱ 的站在池化修正之后仍按 2σ 显著偏离 1——
    比值持续 &lt;1 是该站上游（偏差/权重层）没修干净的证据，
    先修上游；上游修好后仍显著，才轮到给 band 加站维度（向池化收缩）。</div>
    <div class="scroll" style="max-height:340px;margin-top:6px">
    <table><thead><tr>
      <th>站</th><th>站日</th><th>声称(中心±1)</th><th>实现</th><th>实现/声称</th>
    </tr></thead><tbody>${stationRows}</tbody></table></div></div>`;
  if(!stageBlocks && !stationBlock) return "";
  return `<div class="card" style="margin-top:12px">
  <div class="muted"><b>③ 体检：说了多少 vs 真发生了多少</b>（band 店同一车道 ·
  每站日首个决策 · 同 stage · <b>与上表拟合同一证据起点（服务算术锚）</b> ·
  <b>每张表按该 stage 自己那根轴分档</b>，和上表逐行对齐 ·
  唯一差别：上表读系数前的原始声称，本表读系数后的实际服务值 ·
  <b>只计 band 真的动过的站日</b>——没动过的行不是"修完还剩多少"的证据，
  混进来只会把每一格都往零拉（P1 峰值桶混着读 +3.6 点，只算生效日是 +4.4）·
  各档只诊断、不回写）。
  <b>为什么按锚点切</b>：不切时 P0 体检的 1,225 个站日里 1,025 个是按「声称概率」轴的
  旧表修的、只有 108 个是上表这一版修的，P0-M 的 651 个全是旧轴旧表——
  把旧表修剩的残差填进新轴的格子，读不出上表任何一档压没压够（2026-09-15 实测）。</div>
  ${stageBlocks}
  <div class="muted" style="margin-top:6px">
    「差」为正＝实际比声称更常发生（修完还保守）；为负＝修完仍吹头。
    与上表逐行对照：某档上表刚给了压缩系数、本表仍显著为负，说明系数还没压够；
    本表转正说明压过了头。行归哪一档由<b>系数生效前</b>的那份分布决定——
    production 当时就是照它挑的格子；档里的声称与命中读的是系数生效后的服务值。
    只显示样本 ≥30 的档。</div>
  ${stationBlock}</div>`;
}

function basketHit(){
  // The live book (user ruling, 2026-09-16: the paper ledger is a rule
  // comparator): settled live baskets, and the legs production posted.
  const S=D.live_selection||{};const bs=S.basket_hit_by_size||[];
  if(!bs.length) return "";
  const p=v=>v==null?"—":(v*100).toFixed(1)+"%";
  const rows=bs.map(b=>{
    const ratio=b.claimed_win_probability_mean>0
      ? b.hit_rate/b.claimed_win_probability_mean : null;
    const roi=b.staked_usdc>0 ? b.pnl_usdc/b.staked_usdc : null;
    return `<tr><td><b>${b.window_size} 桶</b></td>
      <td>${b.settled_count}</td>
      <td>${b.hit_count}</td>
      <td class="${b.hit_rate>=0.5?"up":""}">${p(b.hit_rate)}</td>
      <td>${p(b.claimed_win_probability_mean)}</td>
      <td class="${ratio==null?"":(ratio>=0.9&&ratio<=1.15?"up":"down")}">${
        ratio==null?"—":ratio.toFixed(2)+"×"}</td>
      <td class="mono">${b.all_in_cost_mean==null?"—":b.all_in_cost_mean.toFixed(3)}</td>
      <td class="mono ${roi==null?"":(roi>=0?"up":"down")}">${
        roi==null?"—":((roi>=0?"+":"")+(roi*100).toFixed(1)+"%")}</td></tr>`;
  }).join("");
  // What production posted, before the market thinned it: the station-day's
  // union of legs that reached the exchange.  The table above groups on the
  // width that actually filled, which files a thinned three-rung basket
  // under one rung -- these rows keep the hardest cases where the choice
  // put them.  The same numbers head the top strip.
  const chosen=((S.chosen_basket_hit||{}).sizes||[]);
  const chosenRows=chosen.map(row=>{
    const ratio=row.realised_over_claimed;
    return `<tr><td><b>${row.chosen_size} 桶</b>${row.seat_legal===false
      ?`<span class="muted" style="font-size:11px" title="宪章 131 的席位是 ${
        row.chosen_size-1} 桶封顶；这一行是 P0-M/P1 加腿后并集到的宽度"
        > 加腿并集</span>`:""}</td>
      <td>${row.settled_count}${row.rate_is_readable===false
        ?`<span class="muted" style="font-size:11px" title="不足 ${
          row.min_chains||8} 个已结算站日，这一行的比率不判色"> 样本薄</span>`:""}</td>
      <td class="${row.rate_is_readable===false?"muted"
        :(row.hit_rate>=0.5?"up":"")}">${p(row.hit_rate)}</td>
      <td>${p(row.filled_hit_rate)}</td>
      <td>${p(row.claimed_mean)}</td>
      <td class="${ratio==null?"":(ratio>=0.9&&ratio<=1.15?"up":"down")}">${
        ratio==null?"—":ratio.toFixed(2)+"×"}</td></tr>`;
  }).join("");
  const chosenBlock=chosen.length?`
  <h3 style="margin-top:18px">实盘挂出的篮子的真值命中率（跨阶段并集）</h3>
  <div class="why"><b>挂出≠买到：</b>「挂出命中」是真值落进生产挂到交易所的腿集合，
  「买到命中」是落进真正成交的腿。两列的差不是模型错，
  是市场没把会赢的那条腿按我们的价卖给我们。被实盘门拒掉、从没到过交易所的站日不计。</div>
  <table><thead><tr><th>挂出宽度</th><th>已结算站日</th><th>挂出命中</th>
    <th>买到命中</th><th>声称</th><th>实现/声称</th></tr></thead>
    <tbody>${chosenRows}</tbody></table>`:"";

  // Where the truth lands when the choice misses, in the station's own
  // bucket steps.  Distance is from the chosen set's EDGE -- the nearest
  // chosen bucket -- never from the centre bucket.  Sign is the
  // temperature axis: above means the day ran hotter than everything
  // chosen, below colder.
  const off=S.chosen_miss_offsets||null;
  let offBlock="";
  if(off&&off.misses){
    const most=Math.max(1,...(off.histogram||[]).map(b=>b.count));
    const bin=k=>((off.histogram||[]).find(b=>b.offset===k)||{}).count||0;
    const offRows=(off.histogram||[]).map(b=>`<tr>
      <td class="mono">${h(b.offset)}</td>
      <td class="mono">${b.count}</td>
      <td class="mono">${(100*b.count/off.misses).toFixed(0)}%</td>
      <td><span class="mono">${"█".repeat(
        Math.max(1,Math.round(12*b.count/most)))}</span></td></tr>`).join("");
    const sided=off.above+off.below;
    const hotOne=bin("+1"), coldOne=bin("-1");
    offBlock=`
  <h3 style="margin-top:18px">选桶脱靶分布（真值出选桶边界几格）</h3>
  <p class="sub">${off.settled} 个已结算站日：挂出盖住 ${off.hits}，脱靶 ${off.misses}${
    off.unparsed?`（另 ${off.unparsed} 条标签无法解析）`:""}${
    off.inside_gap?`（含 ${off.inside_gap} 条落在并集空隙里，无方向）`:""}。
    距离从选桶自己的边界起算，与中心桶无关：
    −1 = 真值停在最冷选桶再往下一格，+1 = 最热选桶再往上一格。</p>
  <div class="why"><b>贴边还是远脱：</b>出界 1 桶的有
  ${off.adjacent} 条（占有向脱靶的 ${sided?(
    100*off.adjacent/sided).toFixed(0):"—"}%）——真值就停在选桶隔壁，
  那一侧多放一格就盖住；出界 ≥2 桶的 ${off.far} 条（${sided?(
    100*off.far/sided).toFixed(0):"—"}%）——整个方向看歪了，加一格也救不回。
  方向上：出热边（真值在上）${off.above} 条 vs 出冷边 ${off.below} 条${
    off.above>off.below?"，选桶整体偏冷（低估了白天的抬升）"
    :off.below>off.above?"，选桶整体偏热":"，两侧均衡"}。
  算术上：热侧全体加一格可多盖 ${hotOne} 条（${off.hits}→${
    off.hits+hotOne}/${off.settled}），两侧各加一格到 ${
    off.hits+hotOne+coldOne}/${off.settled}——代价是每篮多一到两条腿的本金，
  且宪章上限四桶，这里只报算术不作推荐。</div>
  <table><thead><tr><th>出界（桶）</th><th>条数</th><th>占脱靶</th><th></th>
    </tr></thead><tbody>${offRows}</tbody></table>`;
  }

  // Section 14 was the maker lane's own account -- resting fill rates, the
  // shadow probe, the stage-by-stage saving-versus-taker table.  Charter 65
  // made taker the execution law, so it described a lane that stopped
  // running; it was removed on 2026-08-29 rather than left printing a dash.
  // The number is NOT reused: every other section keeps the number a reader
  // already knows it by, and a gap is cheaper than renumbering nine of them.
  return `<h2>篮子命中率：检验各宽度的篮子实际盖住真值的比例</h2>
  <p class="sub">实盘账：结算真值落在成交的桶里就算命中；按实际成交的桶数分组，一个站日的一个阶段算一笔。</p>
  <div class="why"><b>这张表回答一件事：</b>多买一个桶买到的命中率，
  值不值它多花的钱。命中率一定随桶数上升——问题是成本也上升，
  最右边的每元回报才是净结果。</div>
  <table><thead><tr><th>篮子宽度</th><th>已结算</th><th>命中</th><th>命中率</th>
    <th>声称</th><th>实现/声称</th><th>均成本</th><th>每元回报</th></tr></thead>
    <tbody>${rows}</tbody></table>
  ${chosenBlock}
  ${offBlock}`;
}

// Sits after the per-station summary because it is that table's follow-up
// question: the summary ranks stations by what they lost, this asks which
// of them lost for a reason.  Ranking by P&L alone cannot tell a broken
// station from an unlucky one on five settled chains; failing several
// dimensions that do not share a mechanism can.
// The five-dimension failure read, folded into the gate table rather than
// standing beside it (user ruling, 2026-09-09).  Both are per-station
// verdicts, and an operator flipping a switch needs the case for it on the
// same row -- two tables one under the other made them line up by eye.
//
// Since 2026-09-16 both read the live settlements (user ruling: 过闸、
// 未过项、不行、不行的维度 all on live data), so they are no longer two
// measurements: the claim gate and 声称 are one hit-to-claim ratio against
// two thresholds, and the centre gate and 中心 one held-centre residual.
// Until then this read the paper matrix, whose windows and populations
// differed from the gates' (CYYZ read 1.04 and 0.66 on the same day).
function stationFailureIndex(){
  const L=D.live_readiness||{};
  const M=L.failure;
  if(!M||!M.available) return null;
  const rows=(L.stations||[]).map(r=>r.failure).filter(Boolean);
  const by={};
  rows.forEach(s=>{by[s.station_id]=s;});
  return {by:by, thresholds:M.thresholds||{},
    worst:rows.filter(s=>s.flag_count>=4).map(s=>s.station_id),
    clean:rows.filter(s=>s.flag_count===0).length};
}

function refusalStats(){
  // Production's decisions and the live book (user ruling, 2026-09-16: the
  // paper ledger is a rule comparator).  One count per station-day at the
  // opening stage: only P0 opens a chain (charter 74), so a P0-M or P1
  // decision on a day P0 never opened is not a second refusal.  Drawn inside
  // 逐站逐日明细 since 2026-09-16 (user ruling), with each day's own list of
  // station-days that did not build beside that day's orders.
  const F=(D.live_selection||{}).refusal_stats||{};
  const st=(F.stages||[])[0];
  if(!st) return "";
  const all=(st.reasons||[]).slice().sort((a,b)=>b.count-a.count);
  const top=all.slice(0,4);
  const restCount=all.slice(4).reduce((a,r)=>a+r.count,0);
  const scope=st.scope_count||0, seated=st.seated_count||0;
  const rate=scope?seated/scope:0;
  const pctOf=n=>scope?(100*n/scope).toFixed(1)+"%":"—";
  return `<h3 style="margin-top:14px">没建仓是因为什么：实盘为什么没建上仓</h3>
  <p class="sub">${h(F.since_event_date||"")} 起，生产在开仓阶段 P0 做过决策的站日，一个站日算一次；
  <b>建仓＝当天有实盘成交</b>。没建仓的按先到先判归因：挂到交易所没成交 → 实盘门拒单（禁入/总闸/待观察/网关）
  → 出了交易计划没铸实盘意向 → P0 一直持有不动时，读最后一次 P0 计划自己记下的拒因。
  P0-M/P1 不开链（宪章 74），不另计。</p>
  <div class="card">
    <div class="muted">站日 ${scope} 个，实盘建仓 ${seated}</div>
    <div class="big ${rate>=0.3?"up":"down"}">${(100*rate).toFixed(1)}%</div>
    <div class="muted" style="margin-top:4px">没建仓的大头：${
      top.map(r=>`<b>${h(r.label)}</b> ${pctOf(r.count)}`).join(" · ")
    }${restCount?` · 其余 ${all.length-4} 项合计 ${pctOf(restCount)}`:""}</div>
  </div>`;
}

function truthLanding(){
  const T=D.truth_landing||{};
  if(!(T.by_price||[]).length) return "";
  const p=v=>v==null?"—":(v*100).toFixed(1)+"%";
  // Who was right when the two disagreed (user ruling, 2026-09-15: the
  // price x claim cross-tab was unreadable -- one simple table, with its
  // conclusion and a summary).  Each group is buckets where the model's
  // claim sat so many points above or below the market's entry ask; three
  // bars on one scale say what the model said, what the market charged and
  // how often it actually came in, and the pill names the nearer of the two.
  const G=T.disagreement||{};
  const groups=(G.groups||[]).filter(g=>g.buckets>0);
  const MIN_N=30;
  const judge=g=>{
    if(g.hit_rate==null||g.buckets<MIN_N) return "thin";
    const offModel=Math.abs(g.hit_rate-g.claim_mean);
    const offMarket=Math.abs(g.hit_rate-g.price_mean);
    return Math.abs(offModel-offMarket)<0.01?"tie"
      :(offMarket<offModel?"market":"model");
  };
  const WHO={market:["amber","市场更准"],model:["green","模型更准"],
    tie:["","一样准"],thin:["","样本少"]};
  const top=Math.max(0.01,...groups.flatMap(g=>[g.claim_mean,g.price_mean,g.hit_rate]
    .filter(v=>v!=null)));
  const bar=(label,v,color)=>`<div class="tl-bar"><span class="muted">${label}</span>
    <span class="tl-track"><span style="width:${(100*(v||0)/top).toFixed(1)}%;background:${color}"></span></span>
    <span class="mono">${p(v)}</span></div>`;
  const gapRows=groups.map(g=>{
    const [tone,word]=WHO[judge(g)];
    const cents=(g.hit_rate==null||g.price_mean==null)?null:100*(g.hit_rate-g.price_mean);
    return `<div class="tl-row${g.buckets<MIN_N?" muted":""}">
      <div><b>${h(g.label)}</b><div class="muted" style="font-size:11.5px">${
        g.buckets} 个桶 · 开出 ${g.hits} 个</div></div>
      <div>${bar("模型说",g.claim_mean,"var(--violet)")}${
        bar("市场价",g.price_mean,"var(--muted)")}${
        bar("实际开出",g.hit_rate,"var(--accent)")}</div>
      <div class="tl-verdict"><span class="pill ${tone}">${word}</span>
        <div class="mono ${cents==null?"":(cents>=0?"up":"down")}" style="font-size:12px"
          title="实际开出率 − 市场价：照市场价买一股，平均每股赚/亏多少（未计手续费）">${
          cents==null?"":`按市场价买 ${cents>=0?"+":""}${cents.toFixed(1)}¢/股`}</div></div>
    </div>`;
  }).join("");
  const byKey=Object.fromEntries(groups.map(g=>[g.key,g]));
  const disagree=groups.filter(g=>g.key!=="agree"&&judge(g)!=="thin");
  const tally=w=>disagree.filter(g=>judge(g)===w).length;
  const line=g=>g?`<b>${h(g.label)}</b>的 ${g.buckets} 个桶：模型说 ${p(g.claim_mean)}、
    市场价 ${p(g.price_mean)}，实际开出 <b>${p(g.hit_rate)}</b>`:"";
  const verdict=!disagree.length?"两边有分歧的桶还太少，判不出谁更准。"
    :`两边意见不一致的 ${disagree.length} 组里，<b>${tally("market")} 组市场更准</b>、${
      tally("tie")} 组一样准、${tally("model")} 组模型更准——${
      tally("model")===0?"分歧时该信市场"
        :tally("market")===0?"分歧时该信模型":"分歧时谁对要看分歧方向"}。`;
  const buyOver=["model_much","model_bit"].map(k=>byKey[k]).filter(g=>g&&g.hit_rate!=null&&g.buckets>=MIN_N);
  const overCents=buyOver.length
    ? 100*buyOver.reduce((a,g)=>a+(g.hit_rate-g.price_mean)*g.buckets,0)
      /buyOver.reduce((a,g)=>a+g.buckets,0) : null;
  const summary=overCents==null?""
    :overCents<0
      ?`<b>总结：</b>专挑「模型觉得比市场便宜」的桶去买，实际开出率低于买入价，平均每股
        <b class="down">${overCents.toFixed(1)}¢</b>——模型的分歧不是赚钱机会，市场价才是更好的概率。`
      :`<b>总结：</b>专挑「模型觉得比市场便宜」的桶去买，实际开出率高于买入价，平均每股
        <b class="up">+${overCents.toFixed(1)}¢</b>（未计手续费）——这部分分歧是模型的真信息。`;
  // The acceptance sample's baseline (user ruling, 2026-09-17): the buckets
  // the model claims at >=45% while the market asks under 30%.  The band
  // table reads the vector alone and cannot see them, so the lanes that
  // read the market -- beta and L4 -- are judged on them first.  This line
  // is the shape they have to beat; their own readings sit in their sections.
  const A=G.acceptance_baseline;
  const acceptCents=A&&A.hit_rate!=null&&A.price_mean!=null?100*(A.hit_rate-A.price_mean):null;
  const acceptLine=!A||!A.buckets||acceptCents==null?"":`<div class="card tl-verdict-card" style="margin-top:8px"><b>验收样本底数：</b>模型说 ≥${
    Math.round(100*A.claim_floor)}% 而市场价 &lt;${Math.round(100*A.market_ceiling)}% 的桶 <b>${A.buckets} 个，开出 ${A.hits} 个</b>（${p(A.hit_rate)}）；
    模型均说 ${p(A.claim_mean)}、市场均价 ${p(A.price_mean)}，按市场价买每股 <b class="${acceptCents<0?"down":"up"}">${
    acceptCents>=0?"+":""}${acceptCents.toFixed(1)}¢</b>。
    这类桶是 β 车道和 L4 的验收样本（2026-09-17 裁定）：band 表只读概率向量，按构造看不见它们；
    两条车道各自的「验收样本」行写着放行了几个、兑现了几成。</div>`;
  const gapTable=!groups.length?"":`<h3 style="margin-top:14px">模型和市场意见不一致时，谁更准</h3>
  <div class="card tl-verdict-card"><b>结论：</b>${verdict}<br>
    ${[line(byKey.model_much),line(byKey.market_much)].filter(Boolean).join("；<br>")}。
    ${summary?`<div style="margin-top:6px">${summary}</div>`:""}</div>
  ${acceptLine}
  <div class="tl-rows">${gapRows}</div>
  <p class="sub" style="margin-top:6px">市场价＝入场后 4 小时的中位卖一；「更准」＝实际开出率离谁更近（差不到 1 点算一样准）；
    不足 ${MIN_N} 个桶的组标灰不判。另有 ${G.unlikely_buckets||0} 个桶两边都低于 ${
    Math.round(100*(G.unlikely_below||0.05))}%（都说几乎不可能），实际开出 ${G.unlikely_hits||0} 个，未计入。</p>`;
  // The two rulers in one table (user ruling, 2026-09-17: 两张表融合在一起对比).
  // Rows are the shared 0.05 bands; each metric is a pair, market beside
  // model, as the model-skill table does (放在一起对比，不要左右).  The two
  // sides of a row are different buckets -- those the market priced in the
  // band and those the model claimed in it -- so the last column judges each
  // ruler against its own buckets, by the rule the table above uses.
  const bandKey=r=>r.band.join("|");
  const priceByBand=Object.fromEntries((T.by_price||[]).map(r=>[bandKey(r),r]));
  const claimByBand=Object.fromEntries((T.by_claim||[]).map(r=>[bandKey(r),r]));
  const bands=[...(T.by_price||[]),...(T.by_claim||[])].map(r=>r.band)
    .filter((b,i,all)=>all.findIndex(o=>o.join("|")===b.join("|"))===i)
    .sort((a,b)=>a[0]-b[0]);
  const bandLabel=b=>`${b[0].toFixed(2)}–${b[1]>=1?"1.00":b[1].toFixed(2)}`;
  const live=r=>r&&r.bucket_count>0?r:null;
  const ratioOf=r=>r&&r.value_mean>0&&r.hit_rate!=null?r.hit_rate/r.value_mean:null;
  const bandJudge=(mk,md)=>{
    if(!mk||!md||mk.hit_rate==null||md.hit_rate==null
      ||mk.bucket_count<MIN_N||md.bucket_count<MIN_N) return "thin";
    const offMarket=Math.abs(mk.hit_rate-mk.value_mean);
    const offModel=Math.abs(md.hit_rate-md.value_mean);
    return Math.abs(offModel-offMarket)<0.01?"tie"
      :(offMarket<offModel?"market":"model");
  };
  const EDGE='style="border-left:1px solid var(--line)"';
  const ratioCell=(r,edge)=>{
    const x=ratioOf(r);
    return `<td class="mono ${x==null?"":(x>=0.85&&x<=1.2?"up":"down")}"${edge?" "+EDGE:""}>${
      x==null?"—":x.toFixed(2)+"×"}</td>`;
  };
  const pair=(mk,md,f,cls="")=>`<td class="${cls}" ${EDGE}>${mk?f(mk):"—"}</td><td class="${cls}">${md?f(md):"—"}</td>`;
  const judged=[];
  const fusedRows=bands.map(b=>{
    const mk=live(priceByBand[b.join("|")]), md=live(claimByBand[b.join("|")]);
    if(!mk&&!md) return "";
    const who=bandJudge(mk,md);
    judged.push([who,bandLabel(b)]);
    const [tone,word]=WHO[who];
    return `<tr${who==="thin"?' class="muted"':""}><td class="mono">${bandLabel(b)}</td>
      ${pair(mk,md,r=>p(r.value_mean))}
      ${pair(mk,md,r=>`<b>${p(r.hit_rate)}</b>`)}
      ${ratioCell(mk,true)}${ratioCell(md,false)}
      ${pair(mk,md,r=>p(r.truth_share))}
      ${pair(mk,md,r=>`${r.bucket_count}<span class="muted">（${r.truth_count}）</span>`,"mono")}
      <td ${EDGE}><span class="pill ${tone}">${word}</span></td></tr>`;
  }).join("");
  const bandsWhere=w=>judged.filter(([who])=>who===w).map(([,label])=>label);
  const fusedVerdict=["market","model","tie"].map(w=>{
    const hit=bandsWhere(w);
    return hit.length?`<b>${WHO[w][1]} ${hit.length} 档</b>（${hit.join("、")}）`:`${WHO[w][1]} 0 档`;
  }).join("；");
  const sumOf=(rows,k)=>(rows||[]).reduce((a,r)=>a+(r[k]||0),0);
  const fusedTable=!fusedRows?"":`<h3 style="margin-top:14px">两把尺子按档对比：市场价格（入场后 4h 中位卖一）× 模型概率（P0 建仓时刻声称）</h3>
  <div class="card tl-verdict-card"><b>结论：</b>各档实际开出率离自己说的数更近的一方——${fusedVerdict}。</div>
  <div style="overflow-x:auto"><table><thead><tr>
    <th rowspan="2">档</th>
    <th colspan="2" ${EDGE}>说的概率</th><th colspan="2" ${EDGE}>实际开出</th>
    <th colspan="2" ${EDGE}>实现/声称</th><th colspan="2" ${EDGE}>占真值</th>
    <th colspan="2" ${EDGE}>桶数（其中真值）</th>
    <th rowspan="2" ${EDGE}>这一档谁说得准</th>
  </tr><tr>${`<th ${EDGE}>市场</th><th>模型</th>`.repeat(5)}</tr></thead>
  <tbody>${fusedRows}</tbody></table></div>
  <p class="sub" style="margin-top:6px">市场列＝入场后 4h 中位卖一落在这档的桶（至少两次卖一报价才计入，共 ${
    sumOf(T.by_price,"bucket_count")} 个桶、${sumOf(T.by_price,"truth_count")} 个真值）；
    模型列＝P0 建仓时刻声称落在这档的桶（共 ${sumOf(T.by_claim,"bucket_count")} 个桶、${
    sumOf(T.by_claim,"truth_count")} 个真值）。同一行左右<b>不是同一批桶</b>，各自对自己的桶打分；
    同一批桶上一对一比谁更准看上一张表。实现/声称在 0.85–1.20× 为绿；「谁说得准」口径同上表
    （差不到 1 点算一样准，任一侧不足 ${MIN_N} 个桶标灰不判）。</p>`;
  return `<h2>真值落位：按市场价格 × 模型概率分格，比谁更能预判真值</h2>
  <p class="sub">${T.since} 起 · 已闭合站日 ${T.closed_station_days}
    · 市场裁定出赢家 ${T.adjudicated_station_days}
    · 采集截断剔除 ${T.capture_truncated_days}。
    真值＝市场收敛裁定（末端买价 ≥0.80 的桶）；
    价格＝首个 P0 决策后 4 小时的中位卖一；概率＝同一时刻的服务后验。</p>
  <div class="why"><b>这两张表回答一件事：</b>真值到底住在哪个价位、哪个声称档，
  以及两把尺子对不齐时该信谁——第一张直接给答案，第二张把它读自的两把尺子按档并排
  （先结论后明细，2026-09-17 裁定；两把尺子合成一张对比，同日裁定）。</div>
  ${gapTable}
  ${fusedTable}`;
}

// The lambda tables, one per stage scope.  Placed beside the edge
// thermometer because they are the same question asked at the other end:
// the thermometer measures how much of a claimed EDGE arrives, this
// measures how much of a claimed PROBABILITY arrives, and the objective
// sizes on the second.

// Charter 128: the beta lane in shadow.  One slope per stage, and the
// table that decides its promotion: every settled window of the last
// fortnight re-judged by the beta the lane served at that instant, against
// the verdict production recorded, and what each of the four cells then
// realised.
function betaShadow(){
  const B=D.beta_shadow||{};
  if(!B.available) return `<h2>β 车道（影子）：测我们和市场的分歧能兑现几成</h2><div class="card">未采集。</div>`;
  const num=(v,d)=>v==null?"—":Number(v).toFixed(d==null?3:d);
  const h=s=>String(s==null?"":s).replace(/</g,"&lt;");
  const served=B.served||{}, res=B.research||{}, rep=B.replay||{}, live=B.live||{};
  const stages=["P0","P0-M","P1"];
  const bands=B.lead_bands||{};
  // v3 (2026-09-17): gamma, the market's own slope, beside the intercept.
  const gam=r=>r.gamma==null?'<span class="muted">—（三旋钮前的拟合）</span>':`${num(r.gamma)} <span class="muted">[${num(r.gamma_lcb)} … ${num(r.gamma_ucb)}] @ m₀ ${num(r.market_centre,2)}</span>`;
  const servedRow=(label,r,muted)=>`<tr class="${muted?"muted":""}"><td>${label}</td><td class="mono">${num(r.beta_lcb)}</td><td class="mono">${num(r.beta)}</td>
      <td class="mono">${num(r.beta_lcb)} … ${num(r.beta_ucb)}</td><td class="mono">${num(r.intercept)}</td><td class="mono">${gam(r)}</td>
      <td>${r.rows} / ${r.days}</td><td>${r.accumulating?"<b>累积中</b>":"够门槛"}</td>
      <td class="mono">${h((r.anchor||"").slice(0,16))}</td><td>${r.days_beta_positive==null?"—":r.days_beta_positive+"/"+r.days}</td></tr>`;
  const servedRows=stages.map(s=>{const r=served[s];
    const head=r?servedRow(s,r,false):`<tr><td>${s}</td><td colspan="9">无激活行（中价基底）</td></tr>`;
    // Charter 128 v2: the band rows under the stage; a band past the
    // hurdles serves in place of the whole row for decisions in its band.
    const bs=bands[s]||{};
    const bandRows=Object.keys(bs).map(k=>servedRow(`${s} · ${h(k)}${bs[k].accumulating?"":" ★"}`,bs[k],true)).join("");
    return head+bandRows;}).join("");
  const resRows=stages.map(s=>{const r=res[s]||{};if(r.error)return `<tr><td>${s}</td><td colspan="6">${h(r.error)}</td></tr>`;
    const lead=Object.entries(r.by_lead||{}).map(([k,v])=>`${k}: ${num(v.beta,2)} (n${v.rows})`).join("，");
    return `<tr><td>${s}</td><td class="mono">${num(r.beta)}</td><td class="mono">${num(r.beta_lcb)} … ${num(r.beta_ucb)}</td>
      <td class="mono">${num(r.intercept)}</td><td class="mono">${gam(r)}</td><td>${r.rows} / ${r.days}</td><td>${r.days_beta_positive==null?"—":r.days_beta_positive+"/"+r.days}</td><td>${h(lead)||"—"}</td></tr>`;}).join("");
  const cells=rep.cells||{};
  const cell=k=>{const c=cells[k]||{};return `<td class="mono">${c.n||0}</td><td class="mono">${c.mean_hit_minus_price==null?"—":(c.mean_hit_minus_price*100).toFixed(1)+"点"}</td>`;};
  // The acceptance sample (user ruling, 2026-09-17): windows the model
  // claims at >=45% while the market's mid sits under 30% -- the shape the
  // band table cannot see and this lane exists to catch.  Its baseline is
  // drawn in 真值落位; here is what the lane does with it.
  const A=rep.acceptance;
  const pt=v=>v==null?"—":((v>=0?"+":"")+(v*100).toFixed(1)+"点");
  const pc=v=>v==null?"—":(v*100).toFixed(0)+"%";
  const acceptance=!A?`<div class="muted" style="margin-top:8px"><b>验收样本</b>：本页数据还没带这一项，等下一轮整库渲染。</div>`
    :`<h3 style="margin-top:12px">验收样本：模型说 ≥${pc(A.claim_floor)} 而市场中价 &lt;${pc(A.market_ceiling)} 的窗</h3>
    ${!A.windows?`<div class="muted">最近 14 天还没有这类已结算的窗（历史上约 4% 的站日出现一次），累积中。</div>`
    :`<table><tr><th>窗 / 日</th><th>已判</th><th>命中率</th><th>均价</th><th>全部 均 hit−价</th><th>现闸放</th><th>现放 均 hit−价</th><th>β 放</th><th>β 放 均 hit−价</th></tr>
    <tr><td class="mono">${A.windows} / ${A.days}</td><td class="mono">${A.judged}</td><td class="mono">${pc(A.hit_rate)}</td><td class="mono">${num(A.mean_price,2)}</td><td class="mono">${pt(A.mean_hit_minus_price)}</td>
    <td class="mono">${A.current_admits}</td><td class="mono">${pt(A.current_mean_hit_minus_price)}</td><td class="mono">${A.beta_admits}</td><td class="mono">${pt(A.beta_mean_hit_minus_price)}</td></tr></table>`}
    <div class="muted">真值落位一节有这类桶的历史底数。β 放行的这类窗兑现为正，这条车道才算抓住了它该抓的东西；β 放 0 个就是它在这里什么也没说，现闸放了几个、亏了多少是它要拦的对象。</div>`;
  const dayRows=(rep.days||[]).map(d=>`<tr><td>${h(d.date)}</td><td class="mono">${d.windows}</td><td class="mono">${d.current_admits}</td>
      <td class="mono">${d.beta_admits}</td><td class="mono">${d.beta_refuses_of_admitted}</td><td class="mono">${d.beta_admits_of_refused}</td>
      <td class="mono">${d.mean_margin_admitted==null?"—":(d.mean_margin_admitted*100).toFixed(1)}</td><td class="mono">${d.mean_margin_beta_admitted==null?"—":(d.mean_margin_beta_admitted*100).toFixed(1)}</td></tr>`).join("");
  return `<h2>β 车道（影子）：测我们和市场的分歧能兑现几成</h2>
  <div class="card"><b>模式 ${h(B.mode)}</b>。宪章 128 v3（09-17 补上 γ）：每个枚举窗 <span class="mono">hit − m = a + β·(p − m) + γ·(m − m₀)</span>，p 原始声称、<b>m 盘口中价信念</b>（整梯归一化中价在窗内之和，与估值车道同一向量）、m₀ 拟合样本的中价均值；
    三个旋钮：a＝市场在 m₀ 处的整体偏差、<b>γ＝市场自己的斜率</b>（便宜桶兑现不足、贵桶兑现有余时为正）、β＝分歧兑现的份额。没有 γ 时市场自己的曲线会漏进 β（模型在便宜桶上分歧更大，corr(p−m, m) −0.42）：两旋钮 β −0.064、三旋钮 β +0.050、γ +0.21。
    服务信念 m + β_lcb·(p − m) 不变（不含 a、γ，宪章 132 记了为什么）；三旋钮的预测走联合抽样的分位数（β v2 读数）。闸拿信念和全含卖一比、按 5% 门槛判——点差不进回归，在闸上收。第二轴按距结算截止小时分档（<6h / 6–12h / 12–24h / 24–36h / ≥36h），档过门槛才服务（下表 ★），否则整阶段行服务。
    影子只记不闸；晋升要影子 ≥10 个事件日且下表"β 会拒"减"β 会放"的实现差大于按日噪声，且须裁决。
    最近两天决策 ${live.decisions||0} 条，其中带影子读数 ${live.with_shadow||0} 条；影子累计：现闸放行而 β 会拒 ${live.would_refuse||0} 窗、现闸拒而 β 会放 ${live.would_admit||0} 窗。
    ${B.served_other_basis?`<span class="muted">表里还有 ${B.served_other_basis} 行卖一基底的旧行，无人服务，下次拟合即退役。</span>`:""}</div>
  <div class="card">${threeKnobs()}</div>
  <div class="card"><h3>服务中（锚定、滚动 28 天、中价基底）</h3>
  <table><tr><th>阶段 · 档</th><th>服务 β_lcb</th><th>β 点估</th><th>90% 区间</th><th>截距 a(≈0 即市场在 m₀ 处校准)</th><th>γ 市场斜率 · 90%</th><th>行 / 日</th><th>状态</th><th>服务锚</th><th>逐日 β&gt;0</th></tr>${servedRows}</table>
  <div class="muted">锚以前的行不进样本；账本重导前写下的候选窗没有中价，计出不顶替；累积中＝行数或天数不够门槛（60 行 / 8 日），服务 β_lcb 0，信念＝市场信念。</div></div>
  <div class="card"><h3>研究读数（不锚定，28 天，不服务）</h3>
  <table><tr><th>阶段</th><th>β</th><th>90% 区间</th><th>截距 a</th><th>γ 市场斜率 · 90%</th><th>行 / 日</th><th>逐日 β&gt;0</th><th>按提前量</th></tr>${resRows}</table></div>
  <div class="card"><h3>重放：最近 14 天已结算枚举窗，按当时服务的 β 重判</h3>
  <table><tr><th></th><th>现闸放·β 放</th><th></th><th>现闸放·β 拒</th><th></th><th>现闸拒·β 放</th><th></th><th>现闸拒·β 拒</th><th></th></tr>
  <tr><td>窗数 / 均 hit−价</td>${cell("both_admit")}${cell("beta_refuses")}${cell("beta_admits")}${cell("both_refuse")}</tr></table>
  <table><tr><th>事件日</th><th>窗</th><th>现闸放</th><th>β 放</th><th>β 会拒(现放)</th><th>β 会放(现拒)</th><th>现放 均 hit−价(点)</th><th>β 放 均 hit−价(点)</th></tr>${dayRows||'<tr><td colspan="8">尚无可重判的窗（服务 β 的日志晚于这些窗的决策时刻）</td></tr>'}</table>
  <div class="muted">"β 会拒(现放)"那一格的均 hit−价 若持续低于"现闸放·β 放"，闸换 β 就是净少亏；两格打平则 β 只是少买。</div>
  ${acceptance}</div>`;
}

// The three knobs of a belief in (p, m) -- alpha the level, gamma the
// market's own slope, beta/w the share of the disagreement -- in one row
// per stage, the beta lane (probability space) beside L4 (logit space),
// with the bucket proxy's own fit under them (user ruling, 2026-09-17:
// 把三旋钮度数放在一排对比).  Same knob, two arithmetics: the columns compare
// sign and whether the interval crosses zero, never the magnitude.
function threeKnobs(){
  const B=D.beta_shadow||{}, L=D.posterior_lane||{};
  const X=(D.truth_landing||{}).three_knob_proxy||null;
  const h=s=>String(s==null?"":s).replace(/</g,"&lt;");
  const num=v=>v==null?"—":Number(v).toFixed(3);
  const cell=(pt,lo,hi,acc)=>acc?'<span class="muted">累积中</span>'
    :(pt==null?"—":`<b>${num(pt)}</b> <span class="muted">[${num(lo)} … ${num(hi)}]</span>`);
  const EDGE='style="border-left:1px solid var(--line)"';
  const lane=(b,l)=>{
    const lc=(l&&l.ci)||{};
    const bAcc=!!b&&!!b.accumulating, lAcc=!!l&&!!l.accumulating;
    const bc=(k,lo,hi)=>!b?"—":cell(b[k],b[lo],b[hi],bAcc);
    const lcell=(pt,c)=>!l?"—":cell(pt,(c||[])[0],(c||[])[1],lAcc);
    return `<td ${EDGE}>${bc("intercept","intercept_lcb","intercept_ucb")}</td><td>${lcell(l&&l.alpha,lc.alpha)}</td>
      <td ${EDGE}>${bc("gamma","gamma_lcb","gamma_ucb")}</td><td>${lcell(l&&l.gamma,lc.gamma)}</td>
      <td ${EDGE}>${bc("beta","beta_lcb","beta_ucb")}</td><td>${!l?"—":cell(l.w,l.w_lcb,l.w_ucb,lAcc)}</td>
      <td ${EDGE} class="mono">${b?`${b.rows} / ${b.days}`:"—"}</td><td class="mono">${l?`${l.decision_count} / ${l.day_count}`:"—"}</td>`;
  };
  const rows=["P0","P0-M","P1"].map(s=>{
    const b=(B.served||{})[s]||null;
    const l=((L.stages||[]).find(x=>x.stage===s)||{}).served||null;
    return `<tr><td>${s}</td>${lane(b,l)}</tr>`;
  }).join("");
  const proxy=!X||!X.rows||X.alpha==null?"":`<tr class="muted"><td>桶级代理 · 概率空间（真值落位口径，${h(X.since)} 起，非车道）</td>
      <td ${EDGE}>${cell(X.alpha,X.alpha_ci[0],X.alpha_ci[1])}</td><td>—</td>
      <td ${EDGE}>${cell(X.gamma,X.gamma_ci[0],X.gamma_ci[1])}</td><td>—</td>
      <td ${EDGE}>${cell(X.beta,X.beta_ci[0],X.beta_ci[1])}</td><td>—</td>
      <td ${EDGE} class="mono">${X.rows} / ${X.days}</td><td>—</td></tr>`;
  return `<h3>三旋钮一排对比：β 车道（概率空间）× L4（logit 空间）</h3>
  <div style="overflow-x:auto"><table><thead><tr><th rowspan="2">阶段</th>
    <th colspan="2" ${EDGE}>α 截距（市场在中心处的整体偏差）</th><th colspan="2" ${EDGE}>γ 市场斜率</th>
    <th colspan="2" ${EDGE}>β / w 分歧份额</th><th colspan="2" ${EDGE}>样本 行 / 日</th></tr>
  <tr>${`<th ${EDGE}>β 车道</th><th>L4</th>`.repeat(4)}</tr></thead>
  <tbody>${rows}${proxy}</tbody></table></div>
  <p class="sub" style="margin-top:6px">β 车道：hit − m = a + β·(p − m) + γ·(m − m₀)，概率空间；L4：logit b = logit m + w·(logit p − logit m) + α + γ·logit m，logit 空间。
    同一个旋钮两套算术，量纲不同——比符号、比区间是否跨零，不比大小；β 对应 w。三个 0 就是"市场对"。
    两条车道都在等生产证据结算（约 09-25 起有数）；末行是同一批桶级代理上用 β 车道的拟合器算的，有数但不是车道，量纲同 β 车道。</p>`;
}

function effectiveEdgeLambda(){
  const E=D.effective_edge_lambda||{};
  const scopes=E.scopes||[];
  if(!scopes.length)
    return `<h2>有效边际 λ 表：按历史兑现折算声称边际，供准入和定注使用</h2>
    <div class="card"><span class="dot green"></span> 无结算数据。</div>`;
  const num=(v,d)=>v==null?"—":v.toFixed(d==null?2:d);
  const pct=v=>v==null?"—":(v*100).toFixed(0)+"%";
  const bl=s=>String(s).replace(/</g,"&lt;");

  // ------------------------------------------------------------------
  // Charter 96, drawn instead of described.  The rule is one inequality --
  // the gate admits a window when lambda(a) > a -- and lambda is a
  // staircase, so a band buys exactly the sliver where its own step stands
  // above the diagonal.  Written out that took a paragraph and still left
  // the reader unable to say which bands were blind; drawn, a blind band is
  // one with no green under its step, and the promotion backlog is the
  // distance from the solid line up to the dotted one.  The three panels
  // share their scales on purpose: the claim being made is that P0 is an
  // inverted U and P1 is monotone, and that only reads on common axes.
  const XMAX=1.25, YMAX=1.30;
  const curve=sc=>{
    const W=340,H=204,PL=28,PR=8,PT=20,PB=44;
    const IW=W-PL-PR, IH=H-PT-PB, BASE=PT+IH;
    const X=a=>PL+IW*Math.max(0,Math.min(a,XMAX))/XMAX;
    const Y=l=>PT+IH*(1-Math.max(0,Math.min(l,YMAX))/YMAX);
    const bands=(sc.bands||[]).filter(b=>b.lower<XMAX);
    const hiSeats=Math.max(1,...bands.map(b=>b.seats||0));
    let seatBars="",grid="",stair="",rawline="",green="",ribbon="",hover="";
    let prevY=null;
    bands.forEach(b=>{
      const lo=b.lower, up=(b.upper==null?XMAX:Math.min(b.upper,XMAX));
      const xa=X(lo), xb=X(up), w=Math.max(1,xb-xa), y=Y(b.served);
      // Seat mass behind everything, so "this band is blind" can be read
      // together with "and it is where the book actually sat".
      const hgt=(b.seats||0)/hiSeats*(IH*0.36);
      if(hgt>0.5) seatBars+=`<rect x="${(xa+1).toFixed(1)}" y="${(BASE-hgt).toFixed(1)}"
        width="${Math.max(1,w-2).toFixed(1)}" height="${hgt.toFixed(1)}"
        fill="var(--bar-bg)" fill-opacity=".75"></rect>`;
      if(lo>0) grid+=`<line x1="${xa.toFixed(1)}" y1="${PT}" x2="${xa.toFixed(1)}"
        y2="${BASE}" stroke="var(--line)" stroke-width="1"></line>`;
      if(prevY!=null) stair+=`<line x1="${xa.toFixed(1)}" y1="${prevY.toFixed(1)}"
        x2="${xa.toFixed(1)}" y2="${y.toFixed(1)}" stroke="var(--accent)"
        stroke-width="1.4" stroke-opacity=".5"></line>`;
      stair+=`<line x1="${xa.toFixed(1)}" y1="${y.toFixed(1)}" x2="${xb.toFixed(1)}"
        y2="${y.toFixed(1)}" stroke="var(--accent)" stroke-width="2.4"></line>`;
      prevY=y;
      if(b.raw_lambda!=null){
        const yr=Y(b.raw_lambda);
        rawline+=`<line x1="${xa.toFixed(1)}" y1="${yr.toFixed(1)}" x2="${xb.toFixed(1)}"
          y2="${yr.toFixed(1)}" stroke="var(--violet)" stroke-width="1.5"
          stroke-dasharray="3 3"></line>`;
      }
      ribbon+=`<rect x="${xa.toFixed(1)}" y="${(BASE+8).toFixed(1)}"
        width="${w.toFixed(1)}" height="5" rx="2" fill="var(--line)"></rect>`;
      if(b.admits){
        const e=Math.min(b.admits[1],XMAX), xe=X(e);
        // The sliver: bounded below by the gate line, above by the step,
        // left by the band floor, right by the band's own lambda.
        green+=`<path d="M${xa.toFixed(1)},${Y(lo).toFixed(1)}
          L${xa.toFixed(1)},${y.toFixed(1)} L${xe.toFixed(1)},${y.toFixed(1)}
          L${xe.toFixed(1)},${Y(e).toFixed(1)} Z" fill="var(--green)"
          fill-opacity=".30" stroke="var(--green)" stroke-width="1.2"></path>`;
        ribbon+=`<rect x="${xa.toFixed(1)}" y="${(BASE+8).toFixed(1)}"
          width="${Math.max(2,xe-xa).toFixed(1)}" height="5" rx="2"
          fill="var(--green)"></rect>`;
      }
      hover+=`<rect x="${xa.toFixed(1)}" y="${PT}" width="${w.toFixed(1)}"
        height="${IH}" fill="transparent"><title>${sc.scope} ${bl(b.band)} 档 · ${b.seats} 座
服务 λ ${num(b.served)}　本账本 raw ${num(b.raw_lambda)}
${b.admits?"可买 ["+b.admits[0].toFixed(3)+", "+b.admits[1].toFixed(3)+")"
  :"空窗：λ "+num(b.served)+" ≤ 档下界 "+b.lower.toFixed(2)}</title></rect>`;
    });
    const yticks=[0.5,1.0].map(v=>`
      <line x1="${PL}" y1="${Y(v).toFixed(1)}" x2="${(W-PR).toFixed(1)}"
        y2="${Y(v).toFixed(1)}" stroke="var(--line)" stroke-width="1"
        stroke-dasharray="2 4"></line>
      <text x="${(PL-4).toFixed(1)}" y="${(Y(v)+3.5).toFixed(1)}" text-anchor="end"
        fill="var(--muted)" font-size="9.5">${v.toFixed(1)}</text>`).join("")
      +`<text x="2" y="${(PT-8).toFixed(1)}" fill="var(--muted)" font-size="9.5">λ</text>`;
    const xticks=[0.5,0.7,0.85,1.0].map(v=>`
      <text x="${X(v).toFixed(1)}" y="${(BASE+27).toFixed(1)}" text-anchor="middle"
        fill="var(--muted)" font-size="9.5">${v}</text>`).join("")
      +`<text x="${X(1.175).toFixed(1)}" y="${(BASE+27).toFixed(1)}"
        text-anchor="middle" fill="var(--muted)" font-size="9.5">≥1.1</text>
       <text x="${(W-PR).toFixed(1)}" y="${(BASE+39).toFixed(1)}" text-anchor="end"
        fill="var(--muted)" font-size="9.5">a = 篮子价 / 声称胜率 →</text>`;
    const diag=`<line x1="${X(0).toFixed(1)}" y1="${Y(0).toFixed(1)}"
      x2="${X(XMAX).toFixed(1)}" y2="${Y(XMAX).toFixed(1)}" stroke="var(--muted)"
      stroke-width="1.3" stroke-dasharray="5 4"></line>
      <text x="${(W-PR).toFixed(1)}" y="${(Y(XMAX)-5).toFixed(1)}"
        text-anchor="end" fill="var(--muted)" font-size="9.5">闸线 λ=a</text>`;
    const admitTxt=(sc.bands||[]).filter(b=>b.admits)
      .map(b=>`[${b.admits[0].toFixed(2)}, ${b.admits[1].toFixed(2)})`)
      .join(" ∪ ")||"整段空窗";
    const seatSum=(sc.bands||[]).reduce((a,b)=>a+(b.seats||0),0);
    const blindSeats=(sc.bands||[]).filter(b=>!b.admits)
      .reduce((a,b)=>a+(b.seats||0),0);
    return `<div class="card" style="margin-bottom:0">
      <div style="display:flex;align-items:baseline;gap:7px;flex-wrap:wrap">
        <b>${sc.scope}</b>
        <span class="muted" style="font-size:11.5px">阶段 ${sc.stages.join("/")}
          · 封顶 ${sc.ceiling.toFixed(2)} · ${seatSum} 座</span></div>
      <svg viewBox="0 0 ${W} ${H}" width="100%"
        style="height:auto;display:block;margin-top:2px" role="img"
        aria-label="${sc.scope} 的 λ 阶梯与闸线 λ=a，绿色是可买窗口">
        ${seatBars}${grid}${yticks}${green}${diag}${rawline}${stair}
        <line x1="${PL}" y1="${BASE}" x2="${(W-PR).toFixed(1)}" y2="${BASE}"
          stroke="var(--line)" stroke-width="1"></line>
        ${ribbon}${xticks}${hover}
      </svg>
      <div style="font-size:11.5px;margin-top:1px">可买
        <span class="mono up">${admitTxt}</span></div>
      <div class="muted" style="font-size:11.5px">空窗档坐过
        <b>${blindSeats}</b> 座（占 ${seatSum?pct(blindSeats/seatSum):"—"}）——
        这些席位往后一个也接不到。</div>
    </div>`;
  };
  const sw=(css,txt)=>`<span style="display:inline-flex;align-items:center;gap:5px">
    <i style="display:inline-block;width:17px;${css}"></i>${txt}</span>`;
  const legend=`<div class="muted" style="font-size:11.5px;display:flex;gap:6px 16px;
    flex-wrap:wrap;margin-top:7px">
    ${sw("height:0;border-top:2.4px solid var(--accent)","当前服务 λ（阶梯）")}
    ${sw("height:0;border-top:1.5px dashed var(--violet)","本账本 raw λ")}
    ${sw("height:0;border-top:1.3px dashed var(--muted)","闸线 λ=a")}
    ${sw("height:9px;border-radius:2px;background:var(--pill-green-bg);border:1px solid var(--green)","可买窗口＝阶梯高过闸线的那一段")}
    ${sw("height:9px;border-radius:2px;background:var(--bar-bg);border:1px solid var(--line)","席位分布（这档历史上坐过多少）")}
  </div>`;

  // The worked example is picked off the book rather than written down, so
  // it cannot go stale the way a hardcoded band label does: the blind band
  // with the most seats is the one a reader is most likely to be looking
  // for, and the widest raw-over-floor gap is the promotion backlog's own
  // best case.
  const all=[];
  scopes.forEach(sc=>(sc.bands||[]).forEach(b=>all.push({scope:sc.scope,b:b})));
  const blind=all.filter(x=>!x.b.admits&&x.b.seats>0)
    .sort((p,q)=>q.b.seats-p.b.seats)[0];
  const backlog=all.filter(x=>!x.b.admits&&x.b.raw_lambda!=null
      &&x.b.raw_lambda>x.b.lower)
    .sort((p,q)=>(q.b.raw_lambda-q.b.served)-(p.b.raw_lambda-p.b.served))[0];
  const plain=(()=>{
    if(!blind) return "";
    const b=blind.b;
    let s=`<b>白话版</b>：λ 读作「这一档的声称，历史上真到账几成」。
      买得下手的条件只有一句 —— <b>到账的比例，要高过要价的比例</b>，
      也就是这一档的 λ 得高过它自己的<b>起点</b>。看图：蓝阶梯掉到斜虚线以下的档，
      绿色就没了，模型再准也一张票发不出。眼下最典型的是
      <b>${blind.scope} 的 ${bl(b.band)} 档</b>：服务 λ <span class="mono">${num(b.served)}</span>
      看着不低，可这档从 <span class="mono">${b.lower.toFixed(2)}</span> 起步，
      ${num(b.served)} &lt; ${b.lower.toFixed(2)}，整档空窗 ——
      它历史上坐过 ${b.seats} 个席位。`;
    if(backlog){
      const q=backlog.b;
      s+=(backlog.scope===blind.scope&&q.band===b.band)
        ? `而同一档的 raw（紫点线）已经是 <span class="mono">${num(q.raw_lambda)}</span>，
           高过 ${q.lower.toFixed(2)} 了 —— <b>差的不是模型准不准，是这张表还没晋级</b>。`
        : `另一处：<b>${backlog.scope} 的 ${bl(q.band)} 档</b>服务
           <span class="mono">${num(q.served)}</span> 空窗，raw 却已到
           <span class="mono">${num(q.raw_lambda)}</span>（＞下界 ${q.lower.toFixed(2)}）——
           紫点线抬过了斜虚线、蓝实线没跟上的地方，就是待晋级的量。`;
    }
    return s;
  })();

  const bounds=(E.band_bounds||[]).map(b=>b==null?"∞":b).join(" / ");
  const chips=`<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:9px">
    <span class="pill ${E.mode==="on"?"green":"amber"}">开关 ${E.mode||"?"}</span>
    <span class="pill">入选门槛 ${E.minimum_seats_per_band} 座</span>
    <span class="pill">前后半以 ${E.split_at_event_date||"—"} 为界</span>
    <span class="pill">分档边界 <span class="mono">${bounds}</span></span>
  </div>`;

  // The second axis is a partition of one number, so it draws as one bar.
  // Widths are the real widths on e in [-0.05, 0.20]: the band that serves
  // 1.00 is a sliver, and a legend that made it look like a third of the
  // line would be arguing the opposite of what the clause says.
  // Only the interval goes inside the segment; the meaning goes underneath.
  // Labelling in place looked tidier at 1180px and clipped its own words at
  // 620 -- the thin band is a twentieth of the line, and no phrase fits in
  // a twentieth of anything.
  const seg=(flex,bg,fg,txt,tip)=>`<div title="${tip}" style="flex:${flex};
    background:${bg};color:${fg};display:flex;align-items:center;
    justify-content:center;padding:0 3px;white-space:nowrap;overflow:hidden">${txt}</div>`;
  const key=(bg,bd,txt)=>`<span style="display:inline-flex;align-items:center;gap:5px">
    <i style="width:9px;height:9px;border-radius:2px;background:${bg};
      border:1px solid ${bd};display:inline-block"></i>${txt}</span>`;
  const thermo=E.claim_edge_thermometer==="on"?`<div style="margin-top:12px">
    <div style="font-size:12.5px"><b>λ 有第二根轴</b>（宪章 97，2026-09-06 起服务）：按<b>绝对</b>声称边际
      <span class="mono">e = 抬后声称 − 全含价</span> 分段，<b>只准抬不准压</b> ——
      一致度轴已更信任的档不被它压低。</div>
    <div style="display:flex;height:26px;margin-top:5px;border:1px solid var(--line);
      border-radius:7px;overflow:hidden;font-size:11.5px;font-weight:600">
      ${seg(20,"var(--pill-red-bg)","var(--pill-red-fg)","e ≤ 0","声称还不如价格贵，闸不放行")}
      ${seg(20,"var(--pill-green-bg)","var(--pill-green-fg)",
        "0–"+E.claim_edge_thin_band_max,
        "体温计薄档轴：该档实测兑现 1.036，封顶 "+E.claim_edge_thin_band_lambda.toFixed(2))}
      ${seg(60,"var(--bar-bg)","var(--muted)","&gt; "+E.claim_edge_thin_band_max,
        "厚边际窗口仍由一致度轴定 λ")}
    </div>
    <div class="muted" style="font-size:11.5px;margin-top:4px;display:flex;
      gap:5px 15px;flex-wrap:wrap">
      ${key("var(--pill-red-bg)","var(--red)","不放行（声称还不如价格贵）")}
      ${key("var(--pill-green-bg)","var(--green)","λ 直接服务 <b>"+E.claim_edge_thin_band_lambda.toFixed(2)+"</b>（体温计薄档轴）")}
      ${key("var(--bar-bg)","var(--line)","回一致度轴（上面的阶梯）")}
    </div>
    <div class="muted" style="font-size:11.5px;margin-top:3px">
      封顶 ${E.claim_edge_thin_band_lambda.toFixed(2)} —— 信它，但不比它信自己更多。
      行上记 <span class="mono">effective_edge_lambda_source</span> 说明是哪根轴买的这一单。
      注意 <span class="mono">1−a</span> 是<b>相对</b>边际、是上面那根轴的补，
      <b>不是</b>这里的 e，两者不可混引。</div></div>`:"";

  // Charter 97's own instrument.  The clause retires itself when the seats
  // this axis opened clear the hurdle and realise under one; that can only
  // be judged if the seats are attributable, so the gate stamps the source
  // and this counts it.  Empty means not yet measured, never "bought none".
  const axisUsage=()=>{
    const U=D.lambda_axis_usage||{};
    if(!U.available) return "";
    const seated=U.seated_by_source||{};
    const considered=U.considered_by_source||{};
    const total=Object.values(seated).reduce((a,b)=>a+b,0);
    const label={agreement_band:"一致度轴（上面的阶梯）",
      claim_edge_thin_band:"体温计薄档轴"};
    const tone={agreement_band:"var(--accent)",
      claim_edge_thin_band:"var(--violet)"};
    if(!total) return `<p class="muted" style="margin-top:11px;font-size:12.5px">
      <b>λ 来源分布</b>（宪章 97 回收条件的仪表）：${U.since} 起扫到
      ${U.scanned_decisions} 份带来源字段的计划，<b>尚无入座记录</b>。
      来源字段 2026-09-06 才加，此前的计划不带；空＝还没量到，<b>不是</b>该轴没买到东西。</p>`;
    const items=Object.entries(seated).sort((a,b)=>b[1]-a[1]);
    const bar=items.map(([k,v])=>`<div title="${label[k]||k} 入座 ${v} 次"
      style="flex:${v};background:${tone[k]||"var(--muted)"};display:flex;
      align-items:center;justify-content:center;color:var(--bg);font-size:11.5px;
      font-weight:700">${(v/total*100).toFixed(0)}%</div>`).join("");
    const rows=items.map(([k,v])=>`<span style="display:inline-flex;align-items:center;gap:5px">
      <i style="width:9px;height:9px;border-radius:2px;display:inline-block;
        background:${tone[k]||"var(--muted)"}"></i>${label[k]||k}
      <b>${v}</b> 次入座${considered[k]?`（考虑 ${considered[k]} 次 · ${
        (v/considered[k]*100).toFixed(0)}% 落座）`:""}</span>`).join("");
    return `<div style="margin-top:12px">
      <div style="font-size:12.5px"><b>λ 来源分布</b>（宪章 97 回收条件的仪表，${
        U.since} 起 ${U.scanned_decisions} 份计划，${total} 次入座）</div>
      <div style="display:flex;height:22px;margin-top:5px;border-radius:6px;
        overflow:hidden;border:1px solid var(--line)">${bar}</div>
      <div class="muted" style="font-size:11.5px;margin-top:4px;display:flex;
        gap:6px 16px;flex-wrap:wrap">${rows}</div>
      <div class="muted" style="font-size:11.5px;margin-top:3px">
        体温计轴的席位攒够 ${E.minimum_seats_per_band} 座后若兑现跌破 1.0，
        按宪章 97 应收回该轴。</div>${axisRealisation()}</div>`;
  };

  // The clause's own trigger, read where the sample is.  It was written
  // against the thermometer's thin bands, and those two bands hold 41
  // posted legs in the re-derived book -- under this page's ten-leg floor
  // and not closing.  The stamped candidate windows answer the same
  // question on 177 station-days instead of 41 legs (user ruling,
  // 2026-09-12: 接).
  const axisRealisation=()=>{
    const R=D.lambda_axis_realisation||{};
    const axes=R.axes||[];
    if(!R.available||!axes.length) return "";
    const label={agreement_band:"一致度轴",
      claim_edge_thin_band:"体温计薄档轴"};
    const cls={"跌破":"down","达标":"up"};
    const mult=v=>v==null?"—":"×"+v.toFixed(3);
    const p1=v=>v==null?"—":(v*100).toFixed(1)+"%";
    const best=axes.reduce((a,b)=>
      (b.paired_x!=null&&(a==null||a.paired_x<b.paired_x))?b:a,null);
    const rows=axes.map(a=>`<tr>
      <td>${label[a.source]||a.source}</td>
      <td>${a.standing_days}</td>
      <td class="mono">${p1(a.claimed_mean)}</td>
      <td class="mono">${p1(a.hit_rate)}</td>
      <td class="mono ${cls[a.verdict]||""}"><b>${mult(a.realised_x)}</b></td>
      <td class="mono muted">${mult(a.realised_lo)} … ${mult(a.realised_hi)}</td>
      <td class="${cls[a.verdict]||"muted"}">${a.verdict}</td>
      <td class="mono">${p1(a.paired_claimed)}</td>
      <td class="mono">${p1(a.paired_hit_rate)}</td>
      <td class="mono ${best&&best.source===a.source?"up":""}"><b>${
        mult(a.paired_x)}</b></td></tr>`).join("");
    // The trigger firing on an axis that WINS the paired comparison is the
    // case the clause never imagined, and it is the case in front of us.
    const fired=axes.filter(a=>a.verdict==="跌破");
    const awkward=best&&fired.some(a=>a.source===best.source);
    return `<div style="margin-top:12px">
      <div style="font-size:12.5px"><b>宪章 97 回收判据实测</b>（${
      R.first_day||"—"} ～ ${R.last_day||"—"}，${R.station_days} 个站日，${
      R.facts_scanned} 份已结算候选窗事实）</div>
      <table style="margin-top:6px"><thead><tr><th rowspan="2">λ 来源</th>
      <th colspan="6" style="text-align:center">收盘在位的那一席（判据取这个）</th>
      <th colspan="3" style="text-align:center">同日配对（两轴都服务过的 ${
      (axes[0]||{}).paired_days||0} 个站日）</th></tr><tr>
      <th title="该轴在当天最后一次决策时在位的站日数；一个站日只算一次">站日</th>
      <th>声称</th><th>真值落窗</th><th>兑现比</th>
      <th title="命中率的 90% Wilson 区间 ÷ 声称均值">90% 区间</th>
      <th title="跌破＝区间上界 &lt; 1.00；达标＝下界 ≥ 1.00；未分出＝区间跨 1.00；样本不足＝站日 &lt; ${
      R.minimum_station_days}">判定</th>
      <th>声称</th><th>真值落窗</th><th>兑现比</th></tr></thead>
      <tbody>${rows}</tbody></table>
      <p class="note" style="margin-top:6px">口径＝带
      <span class="mono">effective_edge_lambda_source</span> 戳的<b>入座窗口</b>，
      真值按该事实自带的 <span class="mono">bucket_intervals_c_json</span> 映到桶。
      <b>单位是站日不是周期</b>：同一个窗每个周期都会重新入座，
      P0/P0-M/P1 同日又结算同一个最高温，按周期汇总会把区间除以一个当天没有的样本量。
      <b>轴在日内会换</b>——${R.station_days} 个站日里 ${R.mixed_axis_days} 个换过（${
      (R.mixed_axis_days/Math.max(1,R.station_days)*100).toFixed(0)}%），
      而覆盖率随当天推进从 0.76 升到 0.91、声称却一直停在 0.91 左右，
      所以「各轴各取自己当天最后一次决策」会把没撑到收盘的那根轴按盘中快照计分，
      量出来的是服务时点不是轴——右边的同日配对列才是可比的那一列。
      条文原本把判据挂在体温计薄档两格上，那两格全史只有 41 条腿、读不出来，
      判据因此改挂这条口径（用户裁定 2026-09-12）。
      站日门槛 ${R.minimum_station_days} 与「未分出」状态是条文没有、
      2026-09-11 λ 闸事故要求补上的两件。${awkward
      ?`<br><b>当前读数互相矛盾</b>：${label[best.source]||best.source}在位那一席
      判定「跌破」，但在同日配对下它恰是更好的一根（${mult(best.paired_x)} 对
      ${axes.filter(a=>a.source!==best.source).map(a=>mult(a.paired_x)).join("、")}）。
      按字面收回它，等于把这些窗口交还给一根在同样日子里更差的轴。
      条文的触发器需要改写成「显著差于在任替代轴」才可执行。`:""}</p></div>`;
  };

  const block=sc=>{
    const isP1=sc.scope==="P1";
    const rows=(sc.bands||[]).map(b=>{
      // A served value the book disagrees with is the promotion backlog;
      // whether it may be closed is the early/late column's answer, not
      // this one's.
      const gap=b.fitted_capped==null?null:b.fitted_capped-b.served;
      const mark=!b.candidate?["—",""]
        :b.stable?["可晋级","up"]
        :["证据不稳",""];
      // Which rows can buy anything is the question this table is opened
      // with, and it was answered only by reading the 可买区间 cell of every
      // row (user report, 2026-09-11: 看不懂).  Mark the row itself.
      const win=b.admits
        ? `<span class="mono">[${b.admits[0].toFixed(3)}, ${
            b.admits[1].toFixed(3)})</span> <span class="muted">宽 ${
            (b.admits[1]-b.admits[0]).toFixed(3)}</span>`
        : `<span class="down">空窗</span>`;
      return `<tr class="${b.admits?"buyable":""}">
        <td class="mono">${b.band}</td>
        <td class="mono">${num(b.served)}</td>
        <td>${win}</td>
        <td class="mono ${gap==null?"":(gap<0?"down":"up")}">${num(b.raw_lambda)}</td>
        <td class="mono">${num(b.fitted_capped)}</td>
        <td title="已结算窗口 ${b.seats} · 站日 ${b.station_days??"—"} · 事件日 ${b.event_days??"—"} · 声称质量 ${num(b.claim_mass)}；⚠＝未过拟合的四道门槛，服务值保持在任值">${b.seats}${b.admissible?"":" ⚠"}</td>
        <td class="mono">${num(b.early_lambda)} / ${num(b.late_lambda)}</td>
        <td class="mono">${pct(b.return)}</td>
        <td class="${mark[1]}">${mark[0]}</td></tr>`;
    }).join("");
    const st=sc.store;
    const note=isP1
      ? `P1 的形状是 P0 的镜像：市场越不同意，兑现越超过声称、回报越高，因为 P1 的边际是<b>观测</b>不是形状。
         服务值封顶 1.00 —— 豁免的意思是「信它」，不是「比它自己还信它」。`
      : `P0 / P0-M 是倒 U：市场最不同意的那一带，五分之四的声称从未到账。
         <b>能买的不是"λ 高的档"，是"λ 高过自己下界的档"</b> —— 上图绿色的那几段。`;
    // What "a" is, said in the table rather than in the chart's caption:
    // every other column is read through it and it was defined nowhere on
    // the row a reader is looking at.
    const aNote=`<div class="muted" style="font-size:11.5px;margin:6px 0 4px">
      <b>a ＝ 市场价 ÷ 模型声称</b>。a 小＝模型自认占大便宜，a &gt; 1＝市场要价比模型声称还高。
      闸的判据化简后是 <b>λ &gt; a</b>，所以<b>每一档只能买它最下面那一条缝
      [档下界, 本档 λ)</b>；λ 掉到本档下界以下，整档一个窗口都买不到。
      <b>绿边的行是能买的档。</b></div>`;
    return `<h3 style="margin-top:16px">${sc.scope} 档（阶段 ${sc.stages.join(" / ")}，封顶 ${sc.ceiling.toFixed(2)}）</h3>
      <div class="card" style="margin-bottom:6px">${note}</div>
      ${aNote}
      <table><thead><tr>
        <th title="a ＝ 市场价 ÷ 模型声称">一致度带 a=价/声称</th><th>当前服务 λ</th>
        <th title="闸的判据化简后是 λ(a)>a，所以这一档只放行 [档下界, 该档自己的 λ)">可买区间</th>
        <th title="已结算候选窗的 命中数 ÷ 声称质量，读生产证据（宪章 140），不是纸面账本">生产证据 raw λ</th>
        <th>封顶后</th><th>座位</th><th>前半 / 后半</th><th>该带回报</th><th>状态</th>
      </tr></thead><tbody>${rows}</tbody></table>
      ${evidenceNote(sc)}
      <div class="muted mono" style="margin-top:4px">${
        st?`store 已激活 ${st.sha256} · ${st.fitted_at_utc} · ${st.seat_count} 座`
          :"store 尚无该档的激活行（明日 refit 起逐日记录）"
      }</div>
      ${dqShadow(sc)}`;
  };
  // The shadow axis: same band, split by the market's own move on the
  // window over the hours before the seat.  A cell shows its fitted λ only
  // when it clears the store's admission rule; otherwise the seat count
  // alone, so a thin cell reads as thin rather than as a number.
  // Charter 140 (2026-09-16) moved the population onto production's record,
  // which starts the day it shipped.  Until a day settles every column but
  // the served one is dashes -- the page a broken query prints -- so the
  // table says which one it is.
  const evidenceNote=sc=>{
    const ev=sc.evidence;
    if(!ev) return "";
    const src=ev.source==="production"?"生产证据":"纸面账本（对比臂）";
    const since=ev.first_written_at_utc
      ?`自 ${String(ev.first_written_at_utc).slice(5,16).replace("T"," ")}Z 起`:"";
    const bar=`每档要 ≥${ev.min_seats} 窗、≥${ev.min_station_days} 个站日、≥${ev.min_event_days??8} 个事件日、声称质量 ≥${ev.min_claim_mass} 才算可读`;
    if(!ev.labelled) return `<div class="note" style="margin-top:6px"><b>空表不是故障：</b>
      λ 证据${since}改读${src}（宪章 140），${sc.scope} 已写 ${ev.windows} 个抽样候选窗、已结算 0 个${
      ev.earliest_pending_event_date?`；最早 ${ev.earliest_pending_event_date} 的站日结算后出第一批读数`:""}。
      ${bar}，此前 raw 各列为空、服务 λ 保持在任值。</div>`;
    return `<div class="muted" style="margin-top:4px">证据：${src}${since} · ${ev.windows} 个抽样候选窗，已结算 ${ev.labelled} 个${
      ev.latest_labelled_event_date?`（最新结算日 ${ev.latest_labelled_event_date}）`:""} · ${bar}</div>`;
  };
  const dqShadow=sc=>{
    const sh=sc.dq_shadow;
    if(!sh) return `<div class="muted" style="margin-top:6px">Δq 影子：尚无拟合行（下一轮 refit 起记录）。</div>`;
    const tiers=[["down","跌 ≤−"+E.dq_shadow_threshold],["steady","平"],["up","涨 ≥+"+E.dq_shadow_threshold],["no_prior","无先验"]];
    const rows=(sh.evidence||[]).map(b=>{
      const cells=tiers.map(([t])=>{
        const c=(b.tiers||{})[t]||{};
        if(!c.seats) return `<td class="mono muted">—</td>`;
        const lam=c.fitted_lambda;
        const cls=lam==null?"":(lam<b.served_lambda-0.1?"down":(lam>b.served_lambda+0.1?"up":""));
        return `<td class="mono ${cls}">${lam==null?`<span class="muted">(${c.seats}座 raw ${num(c.raw_lambda)})</span>`:`${num(lam)} <span class="muted">(${c.seats}座)</span>`}</td>`;
      }).join("");
      const bound=b.band_upper_bound==null?"≥0.85":"<"+b.band_upper_bound;
      return `<tr><td class="mono">${bound}</td><td class="mono">${num(b.served_lambda)}</td>${cells}</tr>`;
    }).join("");
    return `<div class="muted" style="margin-top:10px">Δq 影子轴（视野 ${E.dq_shadow_horizon_hours}h，同一带按席位前市场对该窗口的移动分格；只记账不服务；拟合 ${sh.fitted_at_utc} · ${sh.seat_count} 座）</div>
      <table><thead><tr><th>一致度带</th><th>服务 λ</th>${tiers.map(([,l])=>`<th>${l}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table>`;
  };

  return `<h2>有效边际 λ 表：按历史兑现折算声称边际，供准入和定注使用</h2>
    <div class="card">
      <div class="muted" style="font-size:12.5px;margin-bottom:9px">
        定注读的是 <span class="mono">λ(a)·p</span> 而不是 p（宪章 68），闸只放行
        <b><span class="mono">λ(a) &gt; a</span></b> 的窗口（宪章 96）—— λ 是<b>阶梯</b>，
        所以每一档只买得到 <span class="mono">[档下界, 该档自己的 λ)</span>，
        <b>下图绿色的就是它</b>；λ 掉到本档下界以下的档整段空窗。</div>
      <div class="grid g3">${scopes.map(curve).join("")}</div>
      ${legend}
      <div class="why" style="margin-top:10px">${plain}</div>
      ${chips}
      ${thermo}
      ${axisUsage()}
      <details class="note" style="margin-top:10px">
        <summary>展开条文原文 · 三列分别在答什么 · 2026-09-06 为什么拆档</summary>
        <div style="font-size:13.4px">
          <p style="margin:0 0 8px">a = 篮子价格 / 声称胜率，λ = 历史上这声称真正到账的比例。
          <b>服务列＝库里此刻激活的那张表</b>（宪章 68(c) 起机器自晋级，
          内核常量只在某 scope 从未激活过时兜底）；raw 列是生产证据（宪章 140）说它该是多少，
          两者的差就是待晋级的量，能不能晋级由前半/后半那一列决定 ——
          两半不一致的带还没挣到一个常量。</p>
          <p style="margin:0">宪章 96（2026-09-06）：闸判的是折后声称减价格为正，化简后就是
          <span class="mono">λ(a) &gt; a</span>。按这条在 <span class="mono">0.80</span> 与
          <span class="mono">0.95</span> 两处拆档，救回被平均掉的半档，
          并把顶档那把"用自己平均值切自己"的刀换成常量边界。</p>
        </div>
      </details>
    </div>
    ${scopes.map(block).join("")}`;
}


// Arm B (2026-09-06): the convergence claim lift, in shadow.  Left half is
// what production's enumeration journaled; right half is the measurement
// the arm rests on, recomputed on this book through the kernel's own state
// function.  Nothing is served by it until a person promotes the arm.
function convergenceClaim(){
  const C=D.convergence_claim||{};
  const num=(v,d)=>v==null?"—":Number(v).toFixed(d==null?2:d);
  const pct=v=>v==null?"—":Number(v).toFixed(0)+"%";
  const receipts=(C.receipts||[]).map(r=>`<tr>
      <td class="mono">${r.stage}</td><td>${r.decisions}</td><td>${r.with_evidence}</td>
      <td>${r.candidates}</td><td class="${r.would_admit?"up":""}">${r.would_admit}</td><td>${r.admitted_by_lift}</td></tr>`).join("");
  const examples=(C.examples||[]).map(e=>`<tr>
      <td class="mono">${e.station_id} ${e.event_date} ${e.stage}</td><td class="mono">${e.at}</td>
      <td class="mono">${rungSorted(e.buckets).join(" · ")}</td><td class="mono">${num(e.claim)}</td>
      <td class="mono">${num(e.price)}</td><td class="mono">${num(e.agreement)}</td>
      <td class="mono">${e.claim_rise==null?"—":(e.claim_rise>=0?"+":"")+num(e.claim_rise)} / ${e.price_fall==null?"—":"−"+num(e.price_fall)}</td>
      <td class="mono ${e.lifted_edge>0?"up":""}">${num(e.lifted_edge,3)}</td><td>${e.flips_gate?"会放行":"仍拒"}</td></tr>`).join("");
  const evidence=(C.evidence||[]).map(e=>`<tr>
      <td>${e.cell}</td><td>${e.n}</td><td>${e.stations}</td><td class="mono">${pct(e.hit_pct)}</td>
      <td class="mono">${pct(e.claimed_pct)}</td>
      <td class="mono ${e.realised_over_claimed>1.15?"up":(e.realised_over_claimed<0.9?"down":"")}">${num(e.realised_over_claimed)}</td>
      <td class="mono ${e.unit_return_pct>0?"up":"down"}">${e.unit_return_pct==null?"—":(e.unit_return_pct>=0?"+":"")+num(e.unit_return_pct,0)+"%"}</td></tr>`).join("");
  return `<h2 style="margin-top:30px">双向收敛系数（B 臂，影子）：模型上调、市场下压时放大声称</h2>
    <div class="card">
      模型把窗口调高 ≥${num(C.min_claim_rise)}、市场把同一窗口的卖一合计压低 ≥${num(C.min_price_fall)}（视野 ${C.horizon_hours}h），
      而市场仍比模型贵（a=价/声称 ≥${num(C.min_agreement)}）——这样的窗口实测兑现 1.3–1.4 倍声称，却被正 EV 闸挡住。
      本臂在闸门前把这种窗口的声称乘 <span class="mono">${num(C.factor)}</span>。当前模式 <span class="mono">${C.mode}</span>：
      影子只记账不放行；阶段限 ${(C.stages||[]).join("/")}，P0 开仓无先验不参与。
      与宪章 68 的 P1 封顶（信它不比它更信它）正面冲突，转正须点名裁定。
    </div>
    <h3 style="margin-top:12px">生产回执（近 7 天决策行）</h3>
    <table><thead><tr><th>阶段</th><th>决策</th><th>有先验</th><th>收敛候选窗</th><th>抬价会放行</th><th>已因抬价放行</th></tr></thead>
    <tbody>${receipts||'<tr><td colspan="6" class="muted">尚无带回执的决策行（常驻重启后开始记录）。</td></tr>'}</tbody></table>
    ${examples?`<h3 style="margin-top:12px">最近候选窗</h3>
    <table><thead><tr><th>站日/阶段</th><th>时刻</th><th>窗口</th><th>声称</th><th>价</th><th>a</th><th>Δ声称 / Δ价</th><th>抬后 edge</th><th>闸门</th></tr></thead><tbody>${examples}</tbody></table>`:""}
    <h3 style="margin-top:12px">证据格（近 ${C.evidence_days} 天已结算站日，P0-M/P1 每 4 小时抽样，最大质量三桶窗，研究口径）</h3>
    <table><thead><tr><th>阶段 / 格</th><th>n</th><th>站</th><th>命中</th><th>声称</th><th>实现÷声称</th><th>按卖一全买</th></tr></thead>
    <tbody>${evidence||'<tr><td colspan="7" class="muted">无样本。</td></tr>'}</tbody></table>`;
}

// Charter 118 (一): the market-lean gate, on the page (user ruling,
// 2026-09-13, "把市场方向闸显式出来").  It is the only gate on this system
// whose signal is read PURELY off the book, and until this build it had no
// section, no chip and no receipt anyone could reach -- the refused
// windows the ruling ordered written down went into an audit dict the plan
// does not persist.  Both halves are production's own: the mode each
// decision ran under and the windows it refused (or would have).
function marketLeanGate(){
  const M=D.market_lean||{};
  const num=(v,d)=>v==null?"—":Number(v).toFixed(d==null?2:d);
  const mode=String(M.mode||"?");
  const MODEWORD={on:"已上线（真拒窗）",shadow:"影子（只计数不拒窗）",off:"已关闭"};
  const receipts=(M.receipts||[]).map(r=>`<tr>
      <td>${h(r.stage)}</td>
      <td class="mono">${r.decisions}</td>
      <td class="mono">${r.with_lean}</td>
      <td class="mono ${r.refused?"down":""}">${r.refused}</td>
      <td class="mono ${r.would_refuse?"down":""}">${r.would_refuse}</td>
    </tr>`).join("");
  const examples=(M.examples||[]).map(e=>`<tr>
      <td>${h(e.station_id)} ${h(e.event_date)} <span class="muted">${
        h(e.stage)}</span></td>
      <td class="mono">${h(e.at)}</td>
      <td class="mono">${(e.buckets||[]).join(" / ")}</td>
      <td class="mono">${num(e.window_mid_ordinal,1)}</td>
      <td class="mono">${num(e.model_mean_ordinal,1)}</td>
      <td class="mono">${num(e.lean,1)}</td>
      <td>${e.enforced?'<span class="pill red">真拒了</span>'
        :'<span class="pill amber">影子会拒</span>'}</td>
    </tr>`).join("");
  return `<h2>市场方向闸：不许逆着盘口的倾向开窗</h2>
    <div class="card">
      <b>判据（<span class="mono">market_lean_ordinal</span>）：</b>
      把盘口每个桶的中价当权重，算出<b>市场隐含的均值桶</b>，与<b>模型均值桶</b>比。
      两者相差 ≥${num(M.min_buckets,1)} 档、且候选窗的中点坐在模型均值的<b>另一侧</b>
      ≥${num(M.min_buckets,1)} 档 —— 也就是这个窗<b>逆着盘口在下注</b> —— 就拒掉它。
      在任窗豁免（守住已持有的不算下注），报价桶太少或中价质量 &lt;
      ${num(M.min_quoted_mass,1)} 时不读（盘口空到说不出均值就什么都不说）。
      它只做减法：过了这道闸的窗，后面的腿地板、定价、边际闸照判。<br>
      <b>当前模式 <span class="pill ${mode==="on"?"green":(mode==="off"?"red":"amber")}">${
        h(MODEWORD[mode]||mode)}</span></b>
      <span class="muted">（<span class="mono">PM_MARKET_LEAN_GATE</span>）</span>
    </div>
    <div class="why"><b>它凭什么存在：</b>上线前在 481 个早晨站日上量过——
    盘口隐含均值比模型均值<b>暖</b>半档以上时，结算真的落在模型均值暖侧的比例
    <b>94%</b>（基准 56%）；冷侧镜像 <b>82%</b>（基准 44%），两边的逐日自助区间
    都离零 30 个百分点以上。这个信号正好活在共识桥的盲区里：桥要中心差 1.5°C
    才掺，而那些 94% 的早晨差的是 0.5–1.2°C，<b>没有任何一层在读它</b>。<br>
    <b>它为什么不在上线状态：</b>08-28 立时是影子；09-05 按账本重放转正 <span
    class="mono">on</span>（单独 +0.9 点、08-23 起 +12.4、组合里 +4.3）；
    但转正后<b>头四天拒掉的 60 个窗口，一个都没记下来</b>——每次拒绝只是计数器 +1
    然后 continue，事后只有 3 个能从账本重建，<b>n=3 定不了案</b>。
    宪章 118(一) 因此命令它在拒绝处写下四个字段；宪章 131(2)（09-12）则判
    <b>「市场的均值桶是正在收集的数据，还不是规则」</b>，把它退回影子：
    现在它每轮照算、照记、<b>不拒任何窗</b>。<br>
    <b>要它转正需要什么：</b>用下面这张自己的记录（不是账本重放）把「被它拒掉的窗
    后来落没落进真值」攒够样本——这正是 118(一) 写记录的目的。</div>
    <h3 style="margin-top:12px">生产回执（近 ${M.days} 天决策行）</h3>
    <table><thead><tr><th>阶段</th><th>决策</th><th>读到盘口倾向</th>
      <th>真拒窗</th><th>影子会拒</th></tr></thead>
    <tbody>${receipts||'<tr><td colspan="5" class="muted">尚无带回执的决策行——回执 2026-09-13 才随生产落盘，此前的行不带它（常驻重启后开始记录）。</td></tr>'}</tbody></table>
    ${examples?`<h3 style="margin-top:12px">最近被拒（或影子会拒）的窗</h3>
    <table><thead><tr><th>站日/阶段</th><th>时刻</th><th>窗口</th>
      <th>窗中点</th><th>模型均值</th><th>盘口均值</th><th>状态</th></tr></thead>
    <tbody>${examples}</tbody></table>
    <div class="muted" style="margin-top:6px">单次决策最多记
      ${M.record_max} 条（<span class="mono">MARKET_LEAN_REFUSAL_RECORD_MAX</span>），
      计数器在上限外仍精确，所以这张清单永远不该被读成全部。
      三个数都是<b>桶序号</b>：盘口均值在模型均值的哪一侧、窗中点又在哪一侧，
      一眼可对。</div>`:""}`;
}


// Rendered inside the band-table module, not as its own section (user
// ruling, 2026-09-01): both tables answer "when the model claims X, what
// actually happens", one keyed by claimed probability, one by claimed
// edge, and reading them two tabs apart hid that they are the same audit.
//
// Drawn, not tabled (user ruling 2026-09-14: "改为直观可视化，而不是一堆数字").
// Every band is one row on a 0-100% ruler carrying three marks: what the
// model claimed (blue ring), what the market charged (orange tick) and how
// often the truth actually landed (ink dot with its interval).  The whole
// finding of this section is where the dot sits -- on the tick, or on the
// ring -- and a ruler shows that without asking anyone to subtract.  The
// numbers stay one click away in the table view and on every row's hover.
// The ledger table that used to lead here was retired the same day (see
// ``_claim_edge_calibration``); every sentence below is computed.
const TH_BANDS=["<0","0-2%","2-5%","5-10%","10-20%","20-30%",">=30%"];
const TH_WORDS={"<0":"我们嫌贵","0-2%":"便宜 0–2 点","2-5%":"便宜 2–5 点",
  "5-10%":"便宜 5–10 点","10-20%":"便宜 10–20 点","20-30%":"便宜 20–30 点",
  ">=30%":"便宜 ≥30 点"};
const TH_THIN_DAYS=30;
const thPct=v=>v==null?"—":(v*100).toFixed(1)+"%";
const thPt=v=>v==null?"—":((v<0?"−":"+")+Math.abs(v*100).toFixed(1));

// One stage's ruler.  Seven fixed slots so the four panels line up row for
// row; a band a stage never saw keeps its slot, empty.
function thRuler(name,g){
  const W=430,L=96,R=16,T=6,RH=27,AX=24;
  const H=T+TH_BANDS.length*RH+AX;
  const x=v=>(L+Math.max(0,Math.min(1,v))*(W-L-R)).toFixed(1);
  const by={};(g.bands||[]).forEach(b=>by[b.band]=b);
  const grid=[0,.25,.5,.75,1].map(v=>`<line x1="${x(v)}" x2="${x(v)}" y1="${T}"
    y2="${H-AX+2}" style="stroke:var(--line);stroke-width:1"/><text x="${x(v)}"
    y="${H-8}" text-anchor="middle" style="fill:var(--muted);font-size:10.5px">${
    (v*100).toFixed(0)}%</text>`).join("");
  const rows=TH_BANDS.map((band,i)=>{
    const y=T+i*RH+RH/2, b=by[band];
    const label=`<text x="${L-10}" y="${(y+4).toFixed(1)}" text-anchor="end"
      style="fill:var(--${b?"text":"muted"});font-size:11.5px">${TH_WORDS[band]}</text>`;
    if(!b) return label;
    const thin=b.station_days<TH_THIN_DAYS;
    const whisker=b.hit_lo==null?"":`<line x1="${x(b.hit_lo)}" x2="${x(b.hit_hi)}"
      y1="${y}" y2="${y}" style="stroke:var(--text);stroke-width:2;opacity:.42;
      stroke-linecap:round"/>`;
    const tip=`${name} · ${TH_WORDS[band]}：模型说 ${thPct(b.claimed_mean)}，`
      +`市场全含价 ${thPct(b.price_mean)}，实际命中 ${thPct(b.hit_rate)}`
      +(b.hit_lo==null?"":`（90% 区间 ${thPct(b.hit_lo)}–${thPct(b.hit_hi)}）`)
      +`；按这个价买一股平均 ${thPt(b.hit_minus_price)} 点`
      +` · ${b.n} 窗 / ${b.station_days} 站日${thin?"（样本薄）":""}`;
    return `<g class="th-row"><title>${h(tip)}</title>
      <rect x="0" y="${(y-RH/2).toFixed(1)}" width="${W}" height="${RH}"
        class="th-hit" style="fill:transparent"/>
      ${label}<g style="opacity:${thin?.4:1}">${whisker}
      <line x1="${x(b.price_mean)}" x2="${x(b.price_mean)}" y1="${y-8}" y2="${y+8}"
        style="stroke:var(--th-price);stroke-width:3;stroke-linecap:round"/>
      <circle cx="${x(b.claimed_mean)}" cy="${y}" r="5.5"
        style="fill:var(--card);stroke:var(--th-claim);stroke-width:2.5"/>
      <circle class="th-dot" cx="${x(b.hit_rate)}" cy="${y}" r="4.5"
        style="fill:var(--text);stroke:var(--card);stroke-width:2"/></g></g>`;
  }).join("");
  const verdict=g.beta_vs_half==="below"?"实际贴着市场价"
    :(g.beta_vs_half==="above"?"实际贴着模型声称":"还分不出");
  return `<div class="th-panel"><div class="th-head"><b>${h(name)}</b>
    <span class="muted">${g.station_days} 站日</span>
    <span class="th-chip">${verdict}</span></div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" role="img"
      aria-label="${h(name)}：各档模型声称、市场价与实际命中">${grid}${rows}</svg></div>`;
}

// Mark keys for the legend, drawn with the same marks the rulers use.
function thKey(kind){
  const svg=body=>`<svg width="22" height="16" viewBox="0 0 22 16"
    style="vertical-align:-3px">${body}</svg>`;
  if(kind==="claim") return svg(`<circle cx="11" cy="8" r="5"
    style="fill:var(--card);stroke:var(--th-claim);stroke-width:2.5"/>`);
  if(kind==="price") return svg(`<line x1="11" x2="11" y1="1" y2="15"
    style="stroke:var(--th-price);stroke-width:3;stroke-linecap:round"/>`);
  return svg(`<line x1="2" x2="20" y1="8" y2="8" style="stroke:var(--text);
    stroke-width:2;opacity:.42;stroke-linecap:round"/><circle cx="11" cy="8"
    r="4.5" style="fill:var(--text);stroke:var(--card);stroke-width:2"/>`);
}

function edgeBandHealth(){
  const C=((D.lambda_axis_realisation||{}).claim_edge)||{};
  const head=`<h2>声称边际体温计：检验自认的便宜幅度实际兑现多少，为 λ 表提供证据</h2>`;
  if(!C.available||!(C.bands||[]).length)
    return head+`<div class="card"><span class="dot"></span> 还没有已结算的入座窗口。</div>`;
  const stages=(C.stages||[]).map(st=>[st,(C.by_stage||{})[st]])
    .filter(([,g])=>g&&(g.bands||[]).length);
  const panels=[...stages,["全部阶段",C]];
  const below=stages.filter(([,g])=>g.beta_vs_half==="below").map(([st])=>st);
  const above=stages.filter(([,g])=>g.beta_vs_half==="above").map(([st])=>st);
  const headline=stages.length&&below.length===stages.length
    ?`<b>${stages.length} 个阶段一个样：黑点（实际）贴着橙线（市场价），不跟着蓝圈（模型声称）走。</b>
      往下看，模型自认越便宜，蓝圈离橙线越远，黑点却留在橙线旁边——多出来的那段"便宜"没有兑现。`
    :`<b>${below.length?`${below.join("、")}：实际贴着市场价。`:""}${
      above.length?`${above.join("、")}：实际贴着模型声称。`:""}${
      stages.length-below.length-above.length>0?"其余阶段还分不出。":""}</b>`;
  const unstaged=C.rows-stages.reduce((a,[,g])=>a+(g.rows||0),0);
  return head+`<div class="card">
  <p style="margin:0 0 8px;font-size:14px;line-height:1.6">${headline}</p>
  <div class="th-legend">
    <span>${thKey("claim")} 模型声称</span>
    <span>${thKey("price")} 市场全含价（买入价）</span>
    <span>${thKey("hit")} 实际命中（横线＝90% 区间）</span>
    <span class="muted">淡色行＝不足 ${TH_THIN_DAYS} 个站日 · 悬停看数字</span></div>
  <div class="th-grid">${panels.map(([n,g])=>thRuler(n,g)).join("")}</div>
  <p class="note" style="margin-top:6px">每行是一类窗口：按「模型声称 − 市场价」分档，从「我们嫌贵」到「便宜 ≥30 点」。
  只看入座窗口和实际最高温，不经过成交模型、不看篮子过不过闸；分阶段画（用户裁定 2026-09-02），「全部阶段」放在旁边对照。${
  unstaged>0?` 另有 ${unstaged} 窗没记阶段，只进「全部阶段」。`:""}
  纸面账本那张「挂出去的腿 × 成没成交」的旧表 2026-09-14 下线：宪章 129 的成交概率按结局设定，读回的是模拟器自己的假设。
  「按 edge≥0.30 拒窗」的硬闸版已被账本否决，不要再提。</p>
  ${thTable(stages,C,unstaged)}</div>
  ${betaDial(C)}${betaSubsets(C)}`;
}

// The table view: every value the rulers draw, one click away.
function thTable(stages,C){
  const mult=v=>v==null?"—":"×"+v.toFixed(3);
  const row=b=>`<tr><td>${TH_WORDS[b.band]||h(b.band)}</td><td>${b.n}</td>
    <td>${b.station_days}</td><td class="mono">${thPct(b.claimed_mean)}</td>
    <td class="mono">${thPct(b.price_mean)}</td><td class="mono">${thPct(b.hit_rate)}</td>
    <td class="mono muted">${thPct(b.hit_lo)} … ${thPct(b.hit_hi)}</td>
    <td class="mono">${thPt(b.hit_minus_price)}</td>
    <td class="mono muted">${thPt(b.hit_minus_price_lo)} … ${thPt(b.hit_minus_price_hi)}</td>
    <td class="mono">${mult(b.realised_x)}</td></tr>`;
  const group=([name,g])=>`<tr><td colspan="10" style="padding-top:10px"><b>${h(name)}</b>
    <span class="muted"> · ${g.rows.toLocaleString()} 窗 / ${g.station_days} 站日</span></td></tr>${
    g.bands.map(row).join("")}`;
  return `<details style="margin-top:8px"><summary class="muted">看数字表</summary>
  <div class="scroll"><table><thead><tr><th>档</th><th>窗口</th><th>站日</th><th>模型声称</th>
  <th>全含价</th><th>实际命中</th><th>90% 区间</th><th>买一股（点）</th><th>90% 区间</th>
  <th>命中÷声称</th></tr></thead><tbody>${
  [...stages,["全部阶段",C]].map(group).join("")}</tbody></table></div></details>`;
}

// A forest: one row per group, a dot at the estimate and a bar across its
// interval, on a beta axis with its two meanings marked.  Shared by the
// per-stage reading, the lead cut, the station cut and the walk-forward.
const TH_BETA=[-0.5,1.5];
function thForest(rows,{labelW=70,width=430,rowH=22,marks=true}={}){
  const L=labelW,R=14,T=marks?26:6,AX=22;
  const H=T+rows.length*rowH+AX;
  const x=v=>(L+(Math.max(TH_BETA[0],Math.min(TH_BETA[1],v))-TH_BETA[0])
    /(TH_BETA[1]-TH_BETA[0])*(width-L-R)).toFixed(1);
  const ticks=[-0.5,0,0.5,1,1.5].map(v=>`<text x="${x(v)}" y="${H-6}"
    text-anchor="middle" style="fill:var(--muted);font-size:10.5px">${v}</text>`).join("");
  const ref=(v,label)=>`<line x1="${x(v)}" x2="${x(v)}" y1="${T-4}" y2="${H-AX+2}"
    style="stroke:var(--muted);stroke-width:1;opacity:.7"/>${marks?`<text x="${x(v)}"
    y="${T-10}" text-anchor="middle" style="fill:var(--muted);font-size:10.5px">${label}</text>`:""}`;
  const body=rows.map((r,i)=>{
    const y=T+i*rowH+rowH/2;
    const bar=r.lo==null?"":`<line x1="${x(r.lo)}" x2="${x(r.hi)}" y1="${y}" y2="${y}"
      style="stroke:var(--${r.emph?"th-claim":"text"});stroke-width:2.5;
      opacity:${r.emph?.8:.42};stroke-linecap:round"/>`;
    const dot=r.from==null?"":`<circle cx="${x(r.from)}" cy="${y}" r="4.5"
      style="fill:var(--card);stroke:var(--muted);stroke-width:2"/><line x1="${x(r.from)}"
      x2="${x(r.v)}" y1="${y}" y2="${y}" style="stroke:var(--muted);stroke-width:1.5"/>`;
    return `<g class="th-row"><title>${h(r.title||r.label)}</title>
      <rect x="0" y="${(y-rowH/2).toFixed(1)}" width="${width}" height="${rowH}"
        class="th-hit" style="fill:transparent"/>
      <text x="${L-8}" y="${(y+4).toFixed(1)}" text-anchor="end"
        style="fill:var(--text);font-size:11px">${h(r.label)}</text>
      ${bar}${dot}<circle class="th-dot" cx="${x(r.v)}" cy="${y}" r="4.5"
        style="fill:var(--${r.emph?"th-claim":"text"});stroke:var(--card);stroke-width:2"/></g>`;
  }).join("");
  return `<svg viewBox="0 0 ${width} ${H}" width="100%" role="img">${
    ref(0,"0 · 信价格")}${ref(1,"1 · 全额兑现")}${ticks}${body}</svg>`;
}

// beta per stage, drawn on the axis its two meanings sit on.
function betaDial(C){
  if(C.beta==null) return "";
  const b3=v=>v==null?"—":v.toFixed(3);
  const mk=(label,g)=>({label,v:g.beta,lo:g.beta_lo,hi:g.beta_hi,
    title:`${label}：β ${b3(g.beta)}（90% 区间 ${b3(g.beta_lo)} … ${b3(g.beta_hi)}）· ${
      g.station_days} 站日`});
  const rows=[...(C.stages||[]).map(st=>(C.by_stage||{})[st]?mk(st,C.by_stage[st]):null)
    .filter(r=>r&&r.v!=null),mk("全部阶段",C)];
  return `<div class="card" style="margin-top:12px">
  <div style="font-size:13px"><b>分歧兑现了几成：β</b>
  <span class="muted">（宪章 128：真值落窗 − 价格 = a + β ·（声称 − 价格））</span></div>
  <div style="display:flex;gap:18px;flex-wrap:wrap;align-items:center;margin-top:6px">
  <div style="flex:1;min-width:300px;max-width:520px">${thForest(rows)}</div>
  <div style="flex:1;min-width:230px;font-size:12.5px;line-height:1.7">
    <div><b>β = 1</b>：我们比市场价高 10 个点，真值就真的多落进去 10 个点。</div>
    <div><b>β = 0</b>：我们偏离市场多少都不影响结果，信价格就够。</div>
    ${betaReading(C)}</div></div></div>`;
}

// What the dial says, conditioned on the payload rather than typed in: the
// first version of this paragraph carried "x1.038" for the <0 band, and a
// day later that band read x1.209 with the sentence unchanged.
function betaReading(C){
  const neg=(C.bands||[]).find(b=>b.band==="<0");
  const lead=C.beta_vs_half==="below"?"<b>点都落在 0 这一侧：分歧基本是噪声。</b>"
    :(C.beta_vs_half==="above"?"<b>点落在 1 这一侧：分歧大半兑现。</b>"
    :"<b>点在 0 和 1 之间，还分不出。</b>");
  const mirror=(C.beta_vs_half==="below"&&neg&&neg.realised_verdict==="above")
    ?` 上面「我们嫌贵」那一行也是证据：黑点跑到了蓝圈右边——我们说贵的窗口，真值反而落得比我们说的还多。往上错、往下也错，这就是噪声的样子。`:"";
  return `<div style="margin-top:6px">${lead}${mirror}</div>`;
}

// "Is there a subset whose beta is clearly above zero?"  Both axes live, so
// the question keeps being answered; the verdict is the walk-forward,
// because a subset picked on the same rows it is judged on always wins.
// Drawn as forests: every station on one caterpillar, so "five of 45 clear
// zero" is seen next to the forty that do not.
function betaSubsets(C){
  const lead=C.by_lead||[], stn=(C.by_station||[]).slice();
  if(!lead.length&&!stn.length) return "";
  const b3=v=>v==null?"—":v.toFixed(3);
  const mk=c=>({label:c.key,v:c.beta,lo:c.beta_lo,hi:c.beta_hi,emph:c.above_zero,
    title:`${c.key}：β ${b3(c.beta)}（90% 区间 ${b3(c.beta_lo)} … ${b3(c.beta_hi)}）· ${
      c.n} 窗 / ${c.station_days} 站日${c.above_zero?" · 整段在 0 右边":""}`});
  stn.sort((a,b)=>b.beta-a.beta);
  const expect=(C.stations_cut||0)*0.05;
  // 2026-09-13's walk-forward, a fixed record rather than a live reading:
  // the hollow ring is beta on the days the subset was picked on, the dot
  // is beta on the days after.
  const walk=[
    {label:"不挑·全部",v:0.168,lo:0.051,hi:0.295,title:"不挑：检验窗 β 0.168 [0.051, 0.295]"},
    {label:"挑出的 4 站",v:-0.028,lo:-0.359,hi:0.278,
      title:"按站挑出 SAEZ/OEJN/MMMX/ZSQD，合起来在检验窗 β −0.028 [−0.359, 0.278]"},
    {label:"SAEZ",from:1.19,v:0.39,title:"SAEZ：挑的时候 1.19 → 往后 0.39"},
    {label:"ZSQD",from:1.00,v:-0.44,title:"ZSQD：挑的时候 1.00 → 往后 −0.44"},
    {label:"KLAX",from:0.86,v:-0.43,title:"KLAX：挑的时候 0.86 → 往后 −0.43"},
    {label:"OEJN",from:0.27,v:-0.44,title:"OEJN：挑的时候 0.27 → 往后 −0.44"},
  ];
  return `<div class="card" style="margin-top:12px">
  <div style="font-size:13px"><b>有没有 β 明显 &gt; 0 的子集</b>
  <span class="muted">（用户裁定 2026-09-13：按站和提前量再切一次，切完为止）</span></div>
  <div style="display:flex;gap:22px;flex-wrap:wrap;margin-top:6px">
  <div style="flex:1;min-width:300px;max-width:480px">
    <div class="muted" style="font-size:12px">按提前量（蓝＝整段在 0 右边）</div>
    ${thForest(lead.map(mk))}
    <div class="muted" style="font-size:12px;margin-top:10px">挑出来的，往后一检验就塌
      （2026-09-13 实测，切点 08-29；空心＝挑的时候，实心＝往后）</div>
    ${thForest(walk,{marks:false,labelW:78})}</div>
  <div style="flex:1;min-width:300px;max-width:480px">
    <div class="muted" style="font-size:12px">按站，全部 ${C.stations_cut} 个（从高到低）</div>
    ${thForest(stn.map(mk),{rowH:13,labelW:48})}</div></div>
  <p class="note" style="margin-top:6px"><b>看着像有，其实没有。</b>
  ${C.stations_cut} 个站里 ${C.stations_above_zero} 个整段在 0 右边，纯噪声下本来就会冒出约
  <b>${expect.toFixed(1)}</b> 个。能定论的只有前瞻检验：用前半段天数挑、在之后的天数上量——
  左下那张图里，挑的时候最亮眼的站往后全塌到 0 附近或以下，合起来还不如不挑。
  <b>结论：没有事前可识别的 β &gt; 0 子集，不挑就是最好的做法。</b></p></div>`;
}

// Charter 132: the valuation lane.  The promotion condition is printed on
// the page with the numbers a ruling would read, so "hold" is never a word
// without its reasons.
// The conclusion, said on the page in the words a ruling would use (user
// ruling, 2026-09-13: 前端增加显式).  Every sentence is conditioned on the
// numbers under it -- a stage whose lambda arm stops losing drops out of the
// headline, a verdict that turns admitted changes the "do not switch on"
// line -- so the banner cannot go on saying what the record no longer does.
function valuationConclusion(V){
  const stages=V.stages||[];
  if(!stages.length) return "";
  const pc=v=>v==null?"—":((v>=0?"+":"")+(v*100).toFixed(1)+"%");
  const n3=v=>v==null?"—":(+v).toFixed(3);
  const money=v=>v==null?"—":((v>=0?"+$":"−$")+Math.abs(v).toFixed(2));
  const verdictOf=st=>((st.verdict||{}).verdict)||"无裁决";
  const allHold=stages.every(st=>verdictOf(st)!=="admitted");
  // A stage whose walk-forward stood on fewer than ten test days is shown,
  // tagged, and kept out of the headline: one day's interval is an interval,
  // not a finding about the stage.
  const thin=st=>((st.verdict||{}).test_days||0)<10;
  const losing=stages.filter(st=>{
    if(thin(st)) return false;
    const a=(((st.verdict||{}).arms)||{}).lambda||{};
    return a.roi_ci&&a.roi_ci[1]!=null&&a.roi_ci[1]<0;
  });
  const behind=stages.filter(st=>{
    if(thin(st)) return false;
    const c=(st.verdict||{}).weather_minus_market_ci;
    return c&&c[1]!=null&&c[1]<0;
  });
  const betaZero=stages.filter(st=>(st.served||{}).beta_ucb!=null&&(st.served||{}).beta_ucb<=0.1);
  const heads=[];
  if(losing.length) heads.push(`现役 λ 策略在 <b>${losing.map(st=>st.stage).join("、")}</b> 显著亏钱`);
  if(behind.length) heads.push(`天气模型的预报在 <b>${behind.map(st=>st.stage).join("、")}</b> 比市场报价差`);
  if(allHold) heads.push("统一估值三阶段均未通过晋升");
  const tone=losing.length||behind.length?"red":(allHold?"amber":"green");
  const tiles=stages.map(st=>{
    const v=st.verdict||{}, a=((v.arms||{}).lambda)||{}, s=st.served||{};
    const wm=v.weather_minus_market, wc=v.weather_minus_market_ci||[];
    return `<div style="flex:1;min-width:200px;padding:10px 12px;border-radius:10px;
      border:1px solid var(--line);background:var(--bar-bg)">
      <div style="display:flex;align-items:center;gap:8px"><b>${h(st.stage)}</b>
        ${thin(st)?`<span class="pill amber" title="前瞻测试日不足 10 天，读数只作参考，不进结论句">样本薄 · ${v.test_days||0} 天</span>`:""}
        <span style="flex:1"></span><span class="pill ${verdictOf(st)==="admitted"?"green":"amber"}">${h(verdictOf(st))}</span></div>
      <div class="muted" style="font-size:11.5px;margin-top:8px">现役 λ 按股收益（前瞻）</div>
      <div style="font-size:24px;font-weight:800;color:var(${a.roi!=null&&a.roi<0?"--red":"--green"})">${pc(a.roi)}</div>
      <div class="muted" style="font-size:11.5px">90% [${pc((a.roi_ci||[])[0])}, ${pc((a.roi_ci||[])[1])}] · ${a.station_days==null?"—":a.station_days} 站日</div>
      <div class="muted" style="font-size:11.5px;margin-top:8px">天气 − 市场 对数得分</div>
      <div style="font-size:18px;font-weight:700;color:var(${wm!=null&&wm<0?"--red":"--text"})">${n3(wm)} nat</div>
      <div class="muted" style="font-size:11.5px">90% [${n3(wc[0])}, ${n3(wc[1])}] · 混合权重 β ${s.beta==null?"—":(+s.beta).toFixed(2)}</div>
    </div>`;
  }).join("");
  const A=V.attribution||null;
  const all=A&&A.by_stage&&A.by_stage.all;
  let waterfall="";
  if(all&&all.legs){
    const parts=[
      ["以为占到的便宜（声称 − 决策卖价）",all.claimed_edge],
      ["执行（成交价比决策卖价便宜）",all.execution],
      ["市场当时已说我们判断偏高",all.market_correction],
      ["结果相对市场参考的偏差",all.residual],
    ];
    const scale=Math.max(...parts.map(p=>Math.abs(p[1]||0)),Math.abs(all.total||0))||1;
    const bar=(label,v,strong)=>`<div style="display:grid;grid-template-columns:minmax(150px,230px) 1fr 80px;gap:8px;align-items:center;margin-top:4px">
      <div style="font-size:12px${strong?";font-weight:700":""}">${label}</div>
      <div style="position:relative;height:14px;background:var(--bar-bg);border-radius:4px">
        <div style="position:absolute;top:0;bottom:0;${v>=0?"left:50%":"right:50%"};width:${(Math.abs(v)/scale*50).toFixed(1)}%;
          background:var(${v>=0?"--green":"--red"});border-radius:3px;opacity:${strong?1:.8}"></div>
        <div style="position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--muted)"></div></div>
      <div class="mono" style="text-align:right;font-size:12px${strong?";font-weight:700":""}">${money(v)}</div></div>`;
    const sel=A.selection||{};
    const f=sel.filled, u=sel.unfilled;
    waterfall=`<div style="margin-top:12px">
      <div style="font-size:12.5px"><b>实盘每一美元去了哪</b>
        <span class="muted">（${all.legs} 条成交腿 · ${all.station_days} 站日 · 成本 $${(+all.cost).toFixed(2)} · 回报 ${pc(all.roi)}
        [${pc((all.roi_ci||[])[0])}, ${pc((all.roi_ci||[])[1])}]）</span></div>
      ${parts.map(p=>bar(p[0],p[1],false)).join("")}
      ${bar("合计",all.total,true)}
      ${f&&u?`<div style="font-size:12.5px;margin-top:8px"><b>成交本身在挑差的：</b>
        成交的 ${f.legs} 条腿比市场参考少落桶 <b style="color:var(--red)">${(Math.abs(f.outcome_minus_market)*100).toFixed(1)} 点</b>，
        没成交的 ${u.legs} 条多落 ${(u.outcome_minus_market*100).toFixed(1)} 点——
        逆选择 <b>${(sel.adverse_selection_points*100).toFixed(1)} 点</b>。挂单省下的钱被挑走的腿吃回去了。</div>`:""}
    </div>`;
  }
  const asks=[];
  if(losing.length) asks.push(`<li><b>是否停止新开仓</b>——现役策略在 ${losing.map(st=>st.stage).join("、")} 的前瞻收益上界都小于 0。这是你的裁决，页面只给证据。</li>`);
  if(allHold) asks.push(`<li><b>不要切 on</b>（当前 <span class="mono">PM_VALUATION_MODE=${h(V.mode||"shadow")}</span>）——裁决为 hold 的阶段在 on 下会整阶段停手。</li>`);
  if(behind.length) asks.push(`<li><b>不要再加新的乘子或子集筛选</b>——能改变结论的只有概率模型本身变好（β 升上去）；复查：<span class="mono">scripts/valuation_four_arm.py --stage all</span>。</li>`);
  return `<div class="card" style="border:2px solid var(--${tone});margin:6px 0 12px">
    <div style="font-size:12px;letter-spacing:.04em" class="muted">结论</div>
    <div style="font-size:18px;font-weight:800;margin-top:2px;line-height:1.45">${heads.join("；")||"暂无结论"}。</div>
    <div style="font-size:12.5px;margin-top:4px">大白话：市场报价本身就是一份天气预报，而且比我们准；花过路费跟它对赌没有依据。</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:12px">${tiles}</div>
    ${waterfall}
    ${asks.length?`<div style="margin-top:12px;padding:10px 12px;border-radius:8px;background:var(--pill-amber-bg)">
      <div style="font-size:12.5px"><b>需要你裁决</b></div><ol style="margin:6px 0 0 18px;padding:0;font-size:12.5px;line-height:1.6">${asks.join("")}</ol></div>`:""}
  </div>`;
}

function valuationLane(){
  const V=D.valuation_lane||{};
  if(!V.available) return "";
  const n3=v=>v==null?"—":(+v).toFixed(3);
  const n2=v=>v==null?"—":(+v).toFixed(2);
  const pc=v=>v==null?"—":((v>=0?"+":"")+(v*100).toFixed(1)+"%");
  const ci=(c,f)=>!c||c[0]==null?"":` <span class="muted">[${f(c[0])}, ${f(c[1])}]</span>`;
  const reasonText={
    "pool_log_gain_over_market_lcb<=0":"池化对市场的对数得分增益下界 ≤ 0",
    "pool_roi_lcb<=0":"池化按股收益下界 ≤ 0",
    "admitted_station_days<30":"池化入座站日 < 30",
    "test_days<10":"测试日 < 10",
    "test_station_days<100":"测试站日 < 100",
  };
  const cards=(V.stages||[]).map(st=>{
    const s=st.served||{}, v=st.verdict||{};
    const ok=v.verdict==="admitted";
    const arms=v.arms||{};
    const armRow=(k,label)=>{const a=arms[k]||{};return `<tr><td>${label}</td>
      <td>${a.windows==null?"—":a.windows}</td><td>${a.station_days==null?"—":a.station_days}</td>
      <td class="mono ${a.roi==null?"":(a.roi<0?"down":"up")}"><b>${pc(a.roi)}</b>${ci(a.roi_ci,pc)}</td>
      <td class="mono">${n3(a.brier)}</td></tr>`;};
    return `<div class="card">
      <div style="display:flex;align-items:center;gap:8px">
        <b>${h(st.stage)}</b><span style="flex:1"></span>
        <span class="pill ${ok?"green":"amber"}">${v.verdict?h(v.verdict):"无裁决"}</span></div>
      <div style="display:flex;gap:18px;margin-top:10px;flex-wrap:wrap">
        <div><div class="muted">池化权重 β（天气）</div>
          <div class="big">${n2(s.beta)}</div>
          <div class="muted">[${n2(s.beta_lcb)}, ${n2(s.beta_ucb)}] · ${s.station_day_count==null?"—":s.station_day_count} 站日 / ${s.day_count==null?"—":s.day_count} 天</div></div>
        <div><div class="muted">样本外对数得分：天气 − 市场</div>
          <div class="big ${v.weather_minus_market==null?"":(v.weather_minus_market<0?"down":"up")}">${n3(v.weather_minus_market)}</div>
          <div class="muted">${ci(v.weather_minus_market_ci,n3)||"—"} nat · 池化 − 市场 ${n3(v.pool_minus_market)}</div></div>
      </div>
      <table style="margin-top:10px"><thead><tr><th>臂（前瞻）</th><th>入座窗</th><th>站日</th><th>按股收益 · 90%</th><th>窗口 Brier</th></tr></thead>
      <tbody>${armRow("lambda","现役 λ")}${armRow("weather","天气单独")}${armRow("market","市场单独")}${armRow("pool","池化")}</tbody></table>
      ${(v.reasons||[]).length?`<div class="muted" style="margin-top:6px;font-size:11.5px">未晋升：${
        v.reasons.map(r=>reasonText[r]||h(r)).join("；")}。测试日 ${h(v.first_test_day||"—")} … ${h(v.last_test_day||"—")}</div>`:""}
    </div>`;
  }).join("");
  const sh=V.shadow||{};
  const states=Object.entries(sh.states||{}).map(([k,c])=>`${h(k)} ${c}`).join(" · ")||"重启后开始记录";
  const banner=valuationConclusion(V);
  const vint=Object.values(V.lambda_vintage||{});
  const stale=vint.filter(x=>x.verdict!=="current").length;
  return `<h2>统一估值车道：把市场盘口当第二份预报，与天气模型合成估值</h2>
  ${banner}
  <p class="sub">市场盘口是第二份预报：<span class="mono">p_value = (1−β)·市场 + β·天气</span>，
  β 在真值桶的对数得分上拟合；保守性放在动作上（抽样向量集上取收益 5% 分位）。
  <b>β = 0 表示天气模型在市场之外没有增量信息。</b>默认影子，只记录不改裁决；切 on 需该阶段裁决为 admitted。</p>
  <div class="grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px">${cards}</div>
  <p class="note" style="margin-top:10px">影子（最近 ${sh.decisions||0} 份计划头）：${states}；
  会多放行 ${sh.would_admit||0} 窗 · 会拒 ${sh.would_refuse||0} 窗。
  λ 表形状血统：${vint.length} 张现役表里 <b>${stale}</b> 张不是按当前席位形状拟的
  （服务侧暂不改，证据读者严格拒绝，见宪章 132）。</p>`;
}

// The edge area's map (user rulings, 2026-09-17: "把前端显式按照这个逻辑改一下",
// then "我说的是 l4 的逻辑").  Drawn as L4's own formula,
//   logit b = logit m + w·(logit p − logit m) + c(m),
// one box per term, each listing the sections that measure or serve that
// term; then the verdict the formula is judged on and the direction it
// falls without evidence.  Every section node jumps to its section, and its
// status is read from LANES so the map and the strips cannot disagree.
function edgeMap(){
  const st=(name)=>(LANES[name]||{}).status||"audit";
  const node=(name,label,what)=>{
    const s=st(name);
    return `<button type="button" class="em-node" data-lane="${s}" data-goto-name="${h(name)}">
      <span class="em-h"><b>${h(label)}</b><span class="pill ${LANE_TONE[s]||""}">${h(LANE_WORD[s]||s)}</span></span>
      <span class="em-w">${what}</span></button>`;
  };
  const term=(sym,title,meaning,body,cls)=>`<div class="em-term${cls?" "+cls:""}">
      <div class="em-sym">${sym}</div>
      <div class="em-tt">${title}</div>
      <div class="em-w">${meaning}</div>
      ${body?`<div class="em-list">${body}</div>`:""}</div>`;
  const op=(t)=>`<div class="em-op" aria-hidden="true">${t}</div>`;
  return `<div class="card emap">
    <div class="em-q">这一页只回答一个问题：<b>模型说 p，市场说 m，真值该信几成？</b>
      答案的形式就是 L4 的公式，页上每个区块都在量或在用其中一项。</div>
    <div class="em-formula mono">logit b = logit m + w · (logit p − logit m) + c(m)</div>
    <div class="em-eq">
      ${term("m","市场先验","盘口中价，本身就是一份预报；先信它",
        node("真值落位","真值落位","真值跟价格走，还是跟模型走"))}
      ${op("+")}
      ${term("w · (p − m)","分歧兑现几成","模型与市场的差里，历史上真兑现的份额；w = 0 就是全信市场",
        node("L4 后验层（影子）","L4 后验层","w：对数几率里拟合，按提前量分档")
        + `<div class="em-sub muted">它的两个特例（概率空间、没有 c(m)）</div>`
        + node("β 车道（影子）","β 车道","m + β(p − m)")
        + node("统一估值车道","统一估值车道","(1 − β)m + βp")
        + `<div class="em-sub muted">实盘此刻用来代替 w 的</div>`
        + node("有效边际 λ 表","有效边际 λ 表","不信市场，直接 λ(价格 ÷ 声称) × p")
        + node("声称边际体温计","体温计","λ 打折后实际兑现多少 → λ 的证据")
        + `<div class="em-sub muted">进来之前先改 p 的</div>`
        + node("双向收敛系数","双向收敛系数","模型往上、市场往下时把 p 抬高"))}
      ${op("+")}
      ${term("c(m)","市场自校正","市场自己系统性偏多少（γ、α）；不读模型",
        node("L4 后验层（影子）","L4 后验层","γ、α：只有 L4 在量"))}
      ${op("=")}
      ${term("b","信念","扣掉点差和手续费后，抽样下界过门槛才买","","em-result")}
    </div>
    <div class="em-two">
      <div class="em-band em-audit">
        <div class="em-cap">验收：这个公式赢没赢市场（到底有没有 edge）</div>
        ${node("宪章 EV 恒等式","宪章 EV 恒等式","量服务 p 对 m（p 已含中心/宽度与 band，不含 λ 与 L4）：k 低于 1 说明 p 有市场没有的信息")}
        ${node("L4 后验层（影子）","L4 前瞻技能分 S","log b − log m 的按日下界大于 0 才算赢")}
      </div>
      <div class="em-band em-shadow">
        <div class="em-cap">没证据时往哪倒</div>
        <div class="em-key">
          <div><b>L4、β、估值：</b>w = 0、c = 0 → b = m → 不买。</div>
          <div><b>实盘 λ 表：</b>沿用 09-01 手拟常数 → 继续买。</div>
          <div class="muted">例：09-16 ZUUU P0 22–24°C，模型 72%、市场 33%、卖价 40。λ 打六折成 43% 高于 40，买；L4 没证据，b = 33%，不买。真值 26°C，没中。</div>
        </div>
      </div>
    </div>
    <div class="em-key muted" style="margin-top:10px">点任一方框跳到对应区块。下面按「验收 → 实盘在用 → L4 与特例 → 进来前的修正」排列。</div>
  </div>`;
}

function posteriorLane(){
  const V=D.posterior_lane||{};
  if(!V.available) return "";
  const n3=v=>v==null?"—":(+v).toFixed(3);
  const n4=v=>v==null?"—":((v>=0?"+":"")+(+v).toFixed(4));
  const ci=(c,f)=>!c||c[0]==null?"":` <span class="muted">[${f(c[0])}, ${f(c[1])}]</span>`;
  const cards=(V.stages||[]).map(st=>{
    const s=st.served, f=st.forward||{}, sk=(s&&s.skill)||{}, c=(s&&s.ci)||{};
    const fwdOk=f.s_forward_ci&&f.s_forward_ci[0]!=null&&f.s_forward_ci[0]>0;
    const word=!s?"无拟合":(s.accumulating?"累积中，服务 b = m":(fwdOk?"前瞻技能分下界 > 0":"影子服务中，前瞻未过"));
    const tone=!s||s.accumulating?"amber":(fwdOk?"green":"amber");
    const bands=Object.values((s&&s.lead_bands)||{}).filter(Boolean).map(b=>`<tr><td>${h(b.lead_band)}</td>
      <td class="mono">${b.decision_count} / ${b.day_count}</td>
      <td class="mono">${b.accumulating?"<span class='muted'>累积中</span>":n3(b.w)+ci([b.w_lcb,b.w_ucb],n3)}</td>
      <td class="mono">${b.accumulating?"—":n3(b.gamma)}</td>
      <td class="mono">${b.accumulating?"—":n4((b.skill||{}).s_in_sample)}</td></tr>`).join("");
    return `<div class="card">
      <div style="display:flex;align-items:center;gap:8px"><b>${h(st.stage)}</b><span style="flex:1"></span>
        <span class="pill ${tone}">${word}</span></div>
      <div style="display:flex;gap:18px;margin-top:10px;flex-wrap:wrap">
        <div><div class="muted">分歧权重 w（logit）</div>
          <div class="big">${s&&!s.accumulating?n3(s.w):"—"}</div>
          <div class="muted">${s&&!s.accumulating?`[${n3(s.w_lcb)}, ${n3(s.w_ucb)}]`:""} · ${s?s.decision_count:"—"} 决策 / ${s?s.station_day_count:"—"} 站日 / ${s?s.day_count:"—"} 天</div></div>
        <div><div class="muted">市场自校正 γ · α</div>
          <div class="big">${s&&!s.accumulating?n3(s.gamma):"—"}</div>
          <div class="muted">${s&&!s.accumulating?`γ ${ci(c.gamma,n3)||""} · α ${n3(s.alpha)}${ci(c.alpha,n3)}`:""}</div></div>
        <div><div class="muted">技能分 S = log b − log m（样本内）</div>
          <div class="big ${sk.s_in_sample==null?"":(sk.s_in_sample<0?"down":"up")}">${n4(sk.s_in_sample)}</div>
          <div class="muted">${ci(sk.s_in_sample_ci,n4)||"—"} nat · IC ${n3(sk.ic_in_sample)}</div></div>
        <div><div class="muted">技能分 S（前瞻，按当时服务的拟合）</div>
          <div class="big ${f.s_forward==null?"":(f.s_forward<0?"down":"up")}">${n4(f.s_forward)}</div>
          <div class="muted">${ci(f.s_forward_ci,n4)||"—"} nat · IC ${n3(f.ic_forward)} · ${f.decisions||0} 决策 / ${f.days||0} 天${f.served_market?` · 其中 ${f.served_market} 次服务的是市场`:""}</div></div>
      </div>
      <table style="margin-top:10px"><thead><tr><th>提前量档</th><th>决策 / 天</th><th>w · 90%</th><th>γ</th><th>S 样本内</th></tr></thead>
      <tbody>${bands||"<tr><td colspan=5 class='muted'>无档行</td></tr>"}</tbody></table>
    </div>`;
  }).join("");
  const sh=V.shadow||{};
  const states=Object.entries(sh.states||{}).map(([k,c])=>`${h(k)} ${c}`).join(" · ")||"重启后开始记录";
  const hyps=(V.hypotheses||[]).map(x=>`${h(x.id)}（${h(x.status)}）`).join("、")||"未注册";
  return `<h2>L4 后验层（影子）：先信市场，再看分歧兑现几成</h2>
  <p class="sub">第一性原理基线 §4.4：<span class="mono">logit b = logit m + w·(logit p − logit m) + α + γ·logit m</span>，
  在全部站日 × 整梯全部桶上前瞻拟合，先验 w = γ = α = 0（市场对）。
  唯一验收量是相对市场的技能分 S 与 IC 的按日下界；<b>w ≈ 0 表示分歧不兑现，此时 b = m，闸门什么都不买</b>。
  默认影子，只记录不改裁决；切 on 需前瞻 S 下界 > 0（E15），且尚未实现。门槛 ${V.min_station_days} 站日 / ${V.min_days} 事件日。</p>
  <div class="grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:12px">${cards}</div>
  <p class="note" style="margin-top:10px">影子（最近 ${sh.decisions||0} 份计划头）：${states}；
  会多放行 ${sh.would_admit||0} 窗 · 会拒 ${sh.would_refuse||0} 窗。已注册假设（E11）：${hyps}。</p>
  ${posteriorRealisation(V.realisation)}`;
}

// L4's money edge (user ruling, 2026-09-17 "补"): the windows L4 would buy,
// after they settled.  Every settled window production enumerated in the
// last 14 days, re-judged with the L4 row that was active at its decision,
// crossed with the verdict the live lambda gate gave the same window.
function posteriorRealisation(R){
  const title=`<h3 style="margin-top:18px">兑现表：L4 会买的窗，结算后赚没赚</h3>`;
  if(!R) return `${title}<div class="card muted">未采集。</div>`;
  if(R.error) return `${title}<div class="card muted">不可读：${h(R.error)}。</div>`;
  const n3=v=>v==null?"—":(+v).toFixed(3);
  const pp=v=>v==null?"—":((v>=0?"+":"")+(v*100).toFixed(1)+" 点");
  const ci=c=>!c||c[0]==null?"":` <span class="muted">[${pp(c[0])}, ${pp(c[1])}]</span>`;
  const tone=x=>x.hit_minus_price==null?"":(x.ci&&x.ci[0]!=null&&x.ci[0]>0?"up":(x.ci&&x.ci[1]!=null&&x.ci[1]<0?"down":""));
  const row=(label,x,strong)=>{x=x||{};return `<tr${strong?' style="font-weight:620"':""}><td>${label}</td>
    <td class="mono">${x.n||0} / ${x.station_days||0} / ${x.days||0}</td>
    <td class="mono">${n3(x.price)}</td><td class="mono">${n3(x.hit)}</td>
    <td class="mono">${pp(x.claimed_edge)}</td>
    <td class="mono ${tone(x)}"><b>${pp(x.hit_minus_price)}</b>${ci(x.ci)}</td></tr>`;};
  const c=R.cells||{};
  // The acceptance sample (user ruling, 2026-09-17): the disagreement class
  // the band table cannot see; L4's edge is read here first.
  const A=R.acceptance;
  const pc=v=>v==null?"—":(v*100).toFixed(0)+"%";
  const acceptRows=!A?`<tr><td colspan="6" class="muted">验收样本：本页数据还没带这一项，等下一轮整库渲染。</td></tr>`
    :`<tr><td colspan="6" style="padding-top:10px"><b>验收样本</b>：模型说 ≥${pc(A.claim_floor)} 而市场中价 &lt;${pc(A.market_ceiling)} 的窗——已结算 ${A.windows}、已判 ${A.judged}${A.windows?"":"（累积中）"}</td></tr>
    ${row("　这类窗全部（不论谁买）",A.all)}${row("　其中 L4 会买",A.l4_admitted,true)}${row("　其中实盘 λ 会买",A.lambda_admitted)}`;
  const stages=Object.entries(R.by_stage||{}).map(([st,v])=>`${h(st)}：L4 会买 ${(v.l4_admitted||{}).n||0} 窗 ${pp((v.l4_admitted||{}).hit_minus_price)} · λ 会买 ${(v.lambda_admitted||{}).n||0} 窗 ${pp((v.lambda_admitted||{}).hit_minus_price)}`).join("；");
  const empty=!R.judged;
  return `${title}
  <p class="sub">最近 14 天（${h(R.since)} 起）生产枚举过、已结算的每个定价窗，用<b>决策当时生效的 L4 拟合</b>重判买不买，
  再和实盘 λ 闸对同一个窗的判决交叉。「L4 声称 edge」= L4 信念 − 全含价；「实际」= 命中 − 全含价，按事件日自助 90%。
  L4 放行那两行的实际值区间下界 &gt; 0 才算 L4 的 edge 兑现了。</p>
  <div class="card" style="overflow-x:auto"><table><thead><tr><th>哪些窗</th><th>窗 / 站日 / 天</th><th>平均价</th><th>命中率</th><th>L4 声称 edge</th><th>实际 命中 − 价 · 90%</th></tr></thead>
  <tbody>${row("L4 会买（合计）",R.l4_admitted,true)}${row("实盘 λ 会买（合计，对照）",R.lambda_admitted,true)}
  ${row("两边都买",c.both_admit)}${row("只有 L4 买",c.l4_only)}${row("只有 λ 买",c.lambda_only)}${row("两边都不买",c.both_refuse)}
  ${acceptRows}</tbody></table>
  <div class="muted" style="margin-top:6px;font-size:11.5px">${empty
    ?`还没有可判的窗：已结算窗 ${R.windows} 个，其中当时 L4 还在累积中 ${R.accumulating} 个（服务 b = m，不给判决）、当时还没有拟合 ${R.no_fit} 个、盘口或向量不全 ${R.no_vector} 个。L4 过门槛（约 09-25）后开始出数。`
    :`已判 ${R.judged} 窗；累积中未判 ${R.accumulating}、无拟合 ${R.no_fit}、盘口或向量不全 ${R.no_vector}。${stages}`}</div></div>`;
}

function charterEv(){
  const E=D.charter_ev||{};
  const title=`<h2>宪章 EV 恒等式：edge = 市场的错 − 模型的错，量出来是多少</h2>`;
  if(!E.available) return `${title}<div class="card">未采集${
    (E.scopes||[]).filter(s=>s.error).map(s=>`：${h(s.scope)} ${h(s.error)}`).join("")}。</div>`;
  const n3=v=>v==null?"—":(+v).toFixed(3);
  const n2=v=>v==null?"—":(+v).toFixed(2);
  const ci=(c,f)=>!c||c[0]==null?"":` <span class="muted">[${f(c[0])}, ${f(c[1])}]</span>`;
  // The verdict is read on the research population (the only one with
  // enough days); the since-anchor reading is what will replace it.  The
  // charter's live condition: EV's lower bound above the spread cost.
  const verdict=r=>{
    if(!r||r.ev==null) return {tone:"amber",word:"无样本"};
    const lcb=r.ev_ci?r.ev_ci[0]:null, kucb=r.k_ci?r.k_ci[1]:null;
    const hurdle=r.spread_cost==null?0:r.spread_cost;
    if(lcb!=null&&lcb>hurdle&&kucb!=null&&kucb<1) return {tone:"green",word:"有 edge（下界 > 点差）"};
    if(r.ev>hurdle) return {tone:"amber",word:"点估过点差，下界没过"};
    if(r.ev>0) return {tone:"amber",word:"中价为正，不够付点差"};
    return {tone:"red",word:"没有 edge"};
  };
  const row=(label,r,muted)=>{
    if(!r||!r.decisions) return `<tr class="${muted?"muted":""}"><td>${label}</td><td colspan="9">锚后尚无已结算的决策</td></tr>`;
    const evCls=r.ev==null?"":(r.ev<0?"down":"up");
    const kCls=r.k==null?"":(r.k<1?"up":"down");
    const net=(r.ev==null||r.spread_cost==null)?null:r.ev-r.spread_cost;
    return `<tr class="${muted?"muted":""}"><td style="white-space:nowrap">${label}</td>
      <td class="mono">${r.station_days} / ${r.days}</td>
      <td class="mono">${n3(r.var_market)}</td>
      <td class="mono">${n3(r.var_model)}</td>
      <td class="mono">${n3(r.cov)}</td>
      <td class="mono ${evCls}"><b>${r.ev==null?"—":((r.ev>=0?"+":"")+n3(r.ev))}</b>${ci(r.ev_ci,v=>(v>=0?"+":"")+n3(v))}</td>
      <td class="mono ${kCls}"><b>${n3(r.k)}</b>${ci(r.k_ci,n3)}</td>
      <td class="mono">${n3(r.spread_cost)}<br><span class="${net==null?"muted":(net<0?"down":"up")}">净 ${net==null?"—":((net>=0?"+":"")+n3(net))}</span></td>
      <td class="mono">${r.days_ev_positive==null?"—":r.days_ev_positive+" / "+r.days}</td></tr>`;
  };
  const cards=(E.scopes||[]).map(s=>{
    if(s.error) return `<div class="card"><b>${h(s.scope)}</b> <span class="muted">${h(s.error)}</span></div>`;
    const v=verdict(s.research);
    const anchor=s.anchor_utc?String(s.anchor_utc).slice(0,16).replace("T"," ")+"Z":"（未记录）";
    return `<div class="card">
      <div style="display:flex;align-items:center;gap:8px"><b>${h(s.scope)}</b><span style="flex:1"></span>
        <span class="pill ${v.tone}">${v.word}</span></div>
      <div style="overflow-x:auto;margin-top:8px"><table><thead><tr><th>样本</th><th>站日 / 天</th>
        <th>Var(e_q)<br><span class="muted">市场的错</span></th>
        <th>Var(e_p)<br><span class="muted">模型的错</span></th>
        <th>Cov(e_p,e_q)<br><span class="muted">同向的错</span></th>
        <th>EV = Var − Cov<br><span class="muted">每站日 · 中价 · 90%</span></th>
        <th>k = Cov / Var<br><span class="muted">90%</span></th>
        <th>点差成本<br><span class="muted">每站日 · EV − 点差</span></th>
        <th>EV &gt; 0 天数</th></tr></thead>
      <tbody>${row("滚动 28 天 · 逐决策站日等权",s.research,false)}${row("滚动 28 天 · 每站日首次决策",s.first,false)}${row("服务锚 "+anchor+" 起",s.served,true)}</tbody></table></div>
      <div class="muted" style="margin-top:6px;font-size:11.5px">裁决读第一行（与估值车道同一样本）；第二行是 09-12 首次测量的口径，P1 两行不同是因为后来的决策带了当天观测；
        锚后那行才说的是今天服务的向量，${s.served&&s.served.decisions?"两边都在时以锚后为准":"会随结算自动填满，够 8 个事件日前先读上两行"}。
        读到 ${s.population&&s.population.decisions_read!=null?s.population.decisions_read:"—"} 个决策，
        盘口不完整弃 ${s.population&&s.population.market_incomplete!=null?s.population.market_incomplete:"—"}。</div>
    </div>`;
  }).join("");
  const W=E.window||{};
  return `${title}
  <p class="sub">用户宪章（2026-09-12）：市场会错，模型也会错，<b>edge = 市场的错 − 模型的错</b>。
  这一节把它量出来。每个已结算的决策，取当时服务的概率向量 p、同一时刻盘口中价 m 和真值桶 t，
  整梯所有桶一起算，站日等权，${W.days||28} 天滚动（${h(W.floor_day||"—")} … ${h(W.as_of_event_date||"—")}）。
  <b>这是读数，不是拟合，也不服务任何东西。</b></p>
  <div class="why"><b>三个量是从同一个动作推出来的。</b>动作：每桶买 (p − m) 股、按中价成交，模型比市场高就买、低就卖。
  记市场的错 <span class="mono">e_q = m − t</span>、模型的错 <span class="mono">e_p = p − t</span>，
  一桶的利润 = (p − m)(t − m) = <span class="mono">e_q² − e_p·e_q</span>，全部桶、全部站日平均，就是
  <span class="mono">EV = Var(e_q) − Cov(e_p, e_q)</span>。<br>
  <b>Var(e_q)</b>：市场错的平方取平均——市场错了多少，<b>桌上有多少钱</b>；市场一点不错就是 0。<br>
  <b>Cov(e_p, e_q)</b>：模型的错 × 市场的错取平均——两个错<b>同向的程度</b>；一起错为正，模型对而市场错时小或为负。<br>
  <b>Var − Cov</b>：市场错的那部分里<b>模型看出来了多少</b>，也就是按分歧下注每站日的期望利润（中价、未扣点差）。<br>
  <b>k = Cov / Var</b>：拿模型的错对市场的错做回归的斜率。k = 1：模型的错 = 市场的错 + 自己的噪声，EV = 0；
  <b>k &lt; 1 才有 edge</b>；k &gt; 1：模型把市场的错同向放大。式子里没有 Var(e_p)：
  <b>不要求模型比市场准，只要求模型的错不完全包含市场的错。</b><br>
  <b>例子（真值落 32°C，看 31°C 这桶，t = 0）：</b>
  A·市场错模型对：市场 0.40、模型 0.20 → 卖 0.20 股、结算归零、赚 +0.08；公式 0.40² − 0.20×0.40 = +0.08。
  B·市场错、模型同向错得更多：市场 0.40、模型 0.55 → 买 0.15 股、亏 −0.06；公式 0.16 − 0.22 = −0.06。
  两种情况市场都错了 0.40，差别全在 Cov 那一项。</div>
  ${cards}
  <p class="note" style="margin-top:10px">怎么读：EV 的 90% 下界要高于真实点差才够实盘开闸（宪章：该数下界 &gt; 点差）；
  k 的区间不含 1 才能说模型有市场没有的信息。区间按事件日自助 ${E.bootstrap_draws||200} 次——全队同一天共享天气，站日不是独立样本。
  λ、5% 门槛、席位形状都不动 Cov，只能少买；能把这个数翻正的只有概率层接到市场没用足的信息。</p>`;
}

function windowDivergence(){
  const W=D.window_divergence||{};
  const rows=W.flagged||[];
  const ths=W.thresholds||[];
  if(!ths.length)
    return `<h2>分布与持仓分家：对比概率分布指向的桶与实际持有的桶</h2>
    <div class="card"><span class="dot green"></span> 没有可评估的站日。</div>`;
  const pct=v=>v==null?"—":(v*100).toFixed(0)+"%";
  const thRows=ths.map(t=>`<tr>
    <td class="mono">&gt;${pct(t.threshold)}</td>
    <td>${t.hot_n}</td><td>${pct(t.hot_hit_rate)}</td>
    <td class="${t.hot_pnl>=0?"up":"down"}">${t.hot_pnl>=0?"+":""}${t.hot_pnl.toFixed(1)}</td>
    <td>${t.cold_n}</td><td>${pct(t.cold_hit_rate)}</td>
    <td class="${t.cold_pnl>=0?"up":"down"}">${t.cold_pnl>=0?"+":""}${t.cold_pnl.toFixed(1)}</td></tr>`).join("");
  return `<h2>分布与持仓分家：对比概率分布指向的桶与实际持有的桶</h2>
  <p class="sub">每个已结算的<b>实盘</b>持仓站日（持仓＝实盘成交的桶，盈亏＝该站日实盘结算），取<b>持仓桶还活着的最后一次概率重算</b>
  （持仓桶质量仍高于 5%，即加仓还来得及的最后时刻），量它有多少后验质量落在持仓窗之外。
  共评估 ${W.evaluated_station_days||0} 个站日，其中末态窗外质量超过 40% 的
  ${rows.length} 个——逐站日明细不在此列出，只按阈值切分看总账。</p>
  <div class="why"><b>这是分歧探测器，不是判决书——两边谁错它都会亮。</b>
  两种原型都真实存在：RKPK 2026-08-21 持 29-32，后验（正确地）压向 33-34，实测 34，
  该加仓没加，是<b>选桶层</b>的均值锚拖不过右尾；KSEA 2026-08-17 持 78-79，
  模型到当地午夜还把 76% 压在 80 以上，实测 78-79——硬下界只能从下方砍桶，
  砍不掉模型头顶的幻觉，这次是<b>模型</b>错、持仓对（夜间无软证据层收敛，
  物理路径 v5 的 NIGHT scope 铺开后这类应减少）。
  「中/未中」与盈亏就是逐日的裁决记录。</div>
  <div class="card"><div class="muted">按阈值切分：分家越狠，命中越差</div>
  <table style="margin-top:8px"><thead><tr><th>末态窗外</th>
    <th>站日</th><th>命中</th><th>盈亏 u</th>
    <th>其余</th><th>命中</th><th>盈亏 u</th></tr></thead>
  <tbody>${thRows}</tbody></table></div>`;
}




// 纸面账本, one board (user ruling, 2026-09-13: 有些指标重复了，把纸面账本的
// 所有数据看板重新整理合并).  It was three tabs -- the 钱 lamps with the
// 待观察/禁入 totals, 组合风险指标, 入场的九个数 -- and one reading was printed
// in several of them: equity twice (1033 and 1032.71), the return on stake
// three times (回报率, 全舰队, 收益率), the claimed EV and how much of it came
// true twice (14.8%/U and 0.679x beside +5.0c and 68%), and CVaR at 95% and
// at 90% were both the worst day again.  Every quantity now has one place:
//
//   总账  equity, P&L, return on stake, return on capital, stake, chains,
//         settled days -- the whole account, read off the equity curve's
//         own series (risk_metrics)
//   对比  four comparisons that each end in their result, carrying the nine
//         numbers where they are compared.  ④ and ⑧ are one number by
//         definition, and ② and ③ are one while no hit loses money, so each
//         pair is printed once with both labels on it
//   风险  Sharpe, Sortino, drawdown, volatility, annualised return, the worst
//         day, and CVaR only at a level whose tail is more than that day
//   曲线  equity against its trend, and against what the claims added up to
//
// No prose (the same day's ruling on 入场的九个数: 不要给我解释).  The
// comparisons read entry_economics, which leaves out sub-cent baskets on the
// ledger's chain_credible rule; the foot says how many whenever there are any.
// Each verdict rests on an identity pinned in tests/test_entry_economics_section.py.
function moneyDataBoard(){
  // The live book since 2026-09-16 (user ruling: the paper ledger is a rule
  // comparator, not the account).  Same four blocks, same arithmetic, read
  // off live fills; the capital is the budget knob as it stands now.
  const M = D.live_money || {};
  const E = M.economics || {};
  const R = M.risk || {};
  const SP = M.bar_split || {};
  const S = (E.weightings || {}).share || {};
  const pct = (v,d=1)=>v==null?"—":(100*v).toFixed(d)+"%";
  const spct = (v,d=1)=>v==null?"—":((v>=0?"+":"")+(100*v).toFixed(d)+"%");
  const pts = (v,d=1)=>v==null?"—":((v>=0?"+":"")+(100*v).toFixed(d)+" 点");
  const cent = (v,d=1)=>v==null?"—":((v>=0?"+":"")+(100*v).toFixed(d)+"¢");
  const usd = (v,d=2)=>v==null?"—":((v>=0?"+":"")+v.toFixed(d)+" U");
  const num = (v,d=2)=>v==null?"—":v.toFixed(d);
  const tone = (v)=>v==null?"muted":(v>=0?"green":"red");
  const text = (v)=>v==null?"":(v>=0?"up":"down");
  const cap = (t)=>`<div class="pb-cap">${t}</div>`;

  // ---- 总账
  const whole = R.available;
  const pnl = whole ? R.realised_pnl_usdc : E.pnl_usdc;
  const stake = whole ? R.total_staked_usdc : E.stake_usdc;
  if(pnl==null) return `<div class="card"><span class="dot amber"></span>
    ${h(M.reason||E.reason||R.reason||"实盘还读不出来")}。</div>`;
  const roi = stake>0 ? pnl/stake : null;
  const head = `<div class="card ee-top">
    <span class="pill ${tone(pnl)} ee-verdict">${pnl>=0 ? "赚" : "亏"}</span>
    ${whole ? `<span class="pb-kv"><span class="muted">权益</span>
      <b class="big">${num(R.final_equity_usdc)}</b></span>` : ""}
    <span class="pb-kv"><span class="muted">盈亏</span>
      <b class="big ${text(pnl)}">${usd(pnl)}</b></span>
    <span class="pb-kv"><span class="muted">投入回报</span>
      <b class="${text(roi)}">${spct(roi)}</b></span>
    ${whole ? `<span class="pb-kv"><span class="muted">本金回报</span>
      <b class="${text(R.total_return)}">${spct(R.total_return,2)}</b></span>` : ""}
    <span class="pb-kv"><span class="muted">投入</span><b>${num(stake)} U</b></span>
    <span class="pb-kv"><span class="muted">已结算</span><b>${whole ? R.chain_count : E.chains} 笔</b></span>
    ${whole ? `<span class="pb-kv"><span class="muted">结算日</span><b>${R.days}</b></span>` : ""}
    <span class="muted">${h(whole ? R.first_day : E.first_day)} → ${
      h(whole ? R.last_day : E.last_day)}</span>
    <span class="muted" title="一笔＝一个站日的一个阶段里成交过的腿；本金＝预算旋钮当前值，账户没有历史余额记录，预算更大的日子会被读成更高的回报率">本金 ${
      M.capital_usdc==null ? "—" : num(M.capital_usdc,0)+" U"}（预算旋钮）${
      M.pending_legs ? ` · 待结算 ${M.pending_legs} 腿不计` : ""}</span>
  </div>`;

  // ---- 对比
  const bars = (rows) => {
    const top = Math.max(...rows.map(r => Math.abs(r[1] || 0))) || 1;
    return rows.map(([label, v, shown, fill]) => `<div class="ee-row">
      <span class="muted">${label}</span>
      <span class="ee-track"><span class="ee-fill ${fill}" style="width:${
        (100 * Math.abs(v || 0) / top).toFixed(1)}%"></span></span>
      <span class="ee-v">${shown}</span></div>`).join("");
  };
  const card = (title, verdict, verdictTone, rows, foot) => `<div class="card ee-cmp">
    <div class="ee-h"><b>${title}</b><span class="pill ${verdictTone}">${verdict}</span></div>
    ${bars(rows)}${foot ? `<div class="ee-d">${foot}</div>` : ""}</div>`;
  const cards = [];

  if(E.available){
    const gains = (E.win_count||0) * (E.mean_win_usdc||0);
    const losses = (E.loss_count||0) * Math.abs(E.mean_loss_usdc||0);
    cards.push(card("赚的钱 vs 亏的钱",
      gains>=losses ? "赢 ＞ 亏" : "亏 ＞ 赢", gains>=losses ? "green" : "red",
      [[`赢 ${E.win_count||0} 条`, gains, num(gains)+" U", "green"],
       [`亏 ${E.loss_count||0} 条`, losses, num(losses)+" U", "red"]],
      `⑥ 平均盈利 <b class="up">${usd(E.mean_win_usdc)}</b> · ⑦ 平均亏损 <b class="down">${
        usd(E.mean_loss_usdc)}</b>${E.flat_count ? ` · 平 ${E.flat_count} 条` : ""}`));

    const margin = S.margin_over_breakeven;
    const sameHit = S.profitable_rate!=null && S.realised_coverage!=null
      && Math.abs(S.profitable_rate - S.realised_coverage) < 5e-4;
    cards.push(card("实际命中 vs 保本线",
      margin==null ? "—" : `${margin>=0 ? "盖过" : "低于"} ${pts(margin)}`, tone(margin),
      [["② 实际覆盖率", S.realised_coverage, pct(S.realised_coverage), tone(margin)],
       ["④ 保本命中率", S.breakeven_hit_rate, pct(S.breakeven_hit_rate), "ee-base"]],
      `⑧ 每股成本 ＝ ④ · ③ 盈利率 ${sameHit ? "＝ ②" : `<b>${pct(S.profitable_rate)}</b>`} · ⑤ 净赔率 <b>${
        num(S.net_odds)}x</b>`));

    const said = S.ev_per_share_claimed, got = S.ev_per_share_realised;
    const kept = said>0 && got!=null ? got/said : null;
    const daily = whole ? ` · 兑现比 中位日 <b>${num(R.ev_realisation_daily_median)}×</b> · 近 7 日 <b>${
      num(R.ev_realisation_trailing_7d)}×</b> · 跑赢声称 <b>${R.days_beating_claim}/${R.days}</b> 天` : "";
    cards.push(card("⑨ 每股期望：声称 vs 实际",
      said==null||got==null ? "—" : said<=0 ? "声称不为正" : got>=said ? "实际 ≥ 声称" : `兑现 ${pct(kept,0)}`,
      got==null ? "muted" : got<0 ? "red" : (kept!=null && kept<1 ? "amber" : "green"),
      [["声称", said, cent(said), "ee-base"],
       ["实际", got, cent(got), tone(got)]],
      `① 声称覆盖率 <b>${pct(S.claimed_coverage)}</b> · 声称累计 <b>${
        usd(said!=null && E.shares!=null ? said*E.shares : null)}</b>${daily}`));
  }

  // The three sides of the bar.  全舰队 is not a row: it is the whole book,
  // which the headline already is.
  if(SP.barred){
    const sides = [["live","上线"],["probation","待观察"],["barred","禁入"]]
      .map(([key,label]) => [label, SP[key] || {}]);
    const live = (SP.live||{}).roi, barred = (SP.barred||{}).roi;
    cards.push(card("上线 / 待观察 / 禁入",
      live==null||barred==null ? "—" : live>=barred ? "上线 ＞ 禁入" : "禁入 ≥ 上线",
      live==null||barred==null ? "muted" : live>=barred ? "green" : "red",
      sides.map(([label, g]) => [`${label} ${g.stations||0} 站`, g.roi,
        `${spct(g.roi)} <span class="muted">${usd(g.pnl_usdc)}</span>`, tone(g.roi)]),
      sides.map(([label, g]) => `${label} <b>${g.settled_days||0}</b> 站日 / <b>${
        num(g.staked_usdc||0)}</b> U`).join(" · ")));
  }

  const stages = (E.by_stage||[]).filter(s => s.chains);
  if(stages.length){
    const up = stages.filter(s => (s.pnl_usdc||0) >= 0).map(s => s.stage);
    const down = stages.filter(s => (s.pnl_usdc||0) < 0);
    cards.push(card("分阶段",
      !down.length ? "各阶段都赚" : !up.length ? "各阶段都亏" : `赚在 ${up.map(h).join("、")}`,
      !down.length ? "green" : !up.length ? "red" : "amber",
      stages.map(s => [`${h(s.stage)} · ${s.chains} 笔`, s.roi,
        `${spct(s.roi)} <span class="muted">${usd(s.pnl_usdc,1)}</span>`, tone(s.roi)]), ""));
  }

  // ---- 风险
  let risk = "";
  if(whole){
    const CI = R.sharpe_ci || [];
    const decided = CI.length===2 && (CI[0]>0 || CI[1]<0);
    const kpi = (label, value, sub, cls) => `<div class="pb-k"><span class="muted">${label}</span>
      <b class="${cls||""}">${value}</b><span class="muted pb-s">${sub}</span></div>`;
    // A tail that is only the worst day is the worst day; print it once.
    const tail = (R.tails||[]).filter(t => t.tail_days > 1)
      .sort((a, b) => b.tail_days - a.tail_days)[0];
    risk = cap("风险") + `<div class="pb-kpis">
      ${kpi("夏普", num(R.sharpe), `[${num(CI[0])}, ${num(CI[1])}] · ${
        decided ? "与 0 可分" : `与 0 不可分 · 需 ${R.days_to_separate_from_zero ?? "—"} 天`}`,
        decided ? text(R.sharpe) : "muted")}
      ${kpi("Sortino", num(R.sortino), `下行波动 ${pct(R.downside_deviation,2)}/天`,
        decided ? text(R.sortino) : "muted")}
      ${kpi("最大回撤", spct(R.max_drawdown), `${h(R.max_drawdown_peak_day||"—")} → ${
        h(R.max_drawdown_trough_day||"—")}`, R.max_drawdown<=-0.15 ? "down" : "")}
      ${kpi("年化收益", spct(R.annualised_return_geometric), `几何 · 算术 ${
        spct(R.annualised_return_arithmetic)}`, text(R.annualised_return_geometric))}
      ${kpi("年化波动", pct(R.annualised_volatility), `日波动 ${pct(R.daily_volatility,2)}`, "")}
      ${kpi("最差一天", spct(R.worst_day,2), `${h(R.worst_day_date||"—")} · ${
        usd(R.worst_day*R.final_equity_usdc,1)} · 亏损日 ${R.losing_days}/${R.days} 平均 ${
        spct(R.mean_losing_day,2)}`, "down")}
      ${tail ? kpi(`CVaR ${(100*tail.level).toFixed(0)}%`, spct(tail.cvar,2),
        `尾部 ${tail.tail_days} 天${tail.tail_days<5 ? ` · <span class="pill amber">样本不足</span>` : ""}`,
        "down") : ""}
    </div>`;
  }

  // ---- 曲线: two panels, because the claim can run far enough past the
  // equity to flatten it on a shared axis.
  let curves = "";
  const series = whole ? (R.series || []) : [];
  if(series.length >= 2){
    const W = 900, PADX = 46, PADY = 16;
    const X = i => PADX + (W-PADX-10) * (i/(series.length-1));
    const eq = series.map(r => r.equity_usdc);
    const panel = (H, lines, extra, label) => {
      const all = [R.initial_equity_usdc].concat(...lines.map(l => l.vals));
      const lo = Math.min(...all), hi = Math.max(...all), span = (hi-lo)||1;
      const Y = v => PADY + (H-2*PADY) * (1 - (v-lo)/span);
      const path = vals => vals.map((v,i)=>`${i?"L":"M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
      const ticks = [hi, lo].map(v => `<text x="6" y="${(Y(v)+4).toFixed(1)}"
        fill="var(--muted)" font-size="11">${v.toFixed(0)}</text>`).join("");
      return `<div class="scroll"><svg viewBox="0 0 ${W} ${H}" width="100%"
        style="min-width:520px;height:${H}px;display:block" role="img" aria-label="${label}">
        ${extra(Y, H)}${ticks}
        <line x1="${PADX}" y1="${Y(R.initial_equity_usdc).toFixed(1)}" x2="${W-10}"
          y2="${Y(R.initial_equity_usdc).toFixed(1)}" stroke="var(--muted)"
          stroke-width="1" stroke-dasharray="3 3"></line>
        ${lines.map(l => `<path d="${path(l.vals)}" fill="none" stroke="${l.stroke}"
          stroke-width="${l.width}"${l.dash ? ` stroke-dasharray="${l.dash}"` : ""}></path>`).join("")}
      </svg></div>`;
    };
    const trough = series.findIndex(r => r.event_date === R.max_drawdown_trough_day);
    const peak = series.findIndex(r => r.event_date === R.max_drawdown_peak_day);
    const band = (Y, H) => {
      const x0 = peak >= 0 ? X(peak) : X(0);
      return trough >= 0 && X(trough) > x0
        ? `<rect x="${x0.toFixed(1)}" y="${PADY}" width="${(X(trough)-x0).toFixed(1)}"
            height="${H-2*PADY}" fill="var(--pill-red-bg)"></rect>` : "";
    };
    const trend = series.map((_, i) => R.trend_intercept_usdc
      + R.trend_slope_usdc_per_day*(i+1) + R.initial_equity_usdc);
    const legend = (items) => `<div class="pb-legend">${items.map(([color, dash, word]) =>
      `<span><i style="border-top:2px ${dash ? "dashed" : "solid"} ${color}"></i>${word}</span>`).join("")}</div>`;
    curves = cap("曲线") + `<div class="card">
      ${legend([["var(--up)", false, "实际权益"],
        ["var(--accent)", true, `趋势 ${usd(R.trend_slope_usdc_per_day)}/天 · R² ${num(R.trend_r_squared)}`],
        ["var(--muted)", true, `本金 ${num(R.initial_equity_usdc,0)}（预算旋钮）`],
        ["var(--pill-red-fg)", false, "回撤区间"]])}
      ${panel(190, [
        {vals: trend, stroke: "var(--accent)", width: 1.5, dash: "6 4"},
        {vals: eq, stroke: "var(--up)", width: 2}], band, "权益曲线与趋势线")}
      ${legend([["var(--up)", false, "实际权益"],
        ["var(--violet)", true, "声称 EV 累计到的权益"]])}
      ${panel(140, [
        {vals: series.map(r => r.claimed_equity_usdc), stroke: "var(--violet)", width: 1.5, dash: "2 3"},
        {vals: eq, stroke: "var(--up)", width: 2}], () => "", "声称权益与实际权益")}
    </div>`;
  }

  const excluded = [
    E.dust_excluded ? `对比不含 ${E.dust_excluded} 笔整篮成本 &lt; ${
      (100*(E.dust_cost_floor||0.01)).toFixed(0)}¢ 的链（${usd(E.dust_excluded_pnl_usdc)}）` : "",
    E.cost_missing ? `对比不含 ${E.cost_missing} 行缺成本` : "",
  ].filter(Boolean).join(" · ");

  return head
    + (cards.length ? cap("对比 · 按股 · 整本账") + `<div class="ee-cmps">${cards.join("")}</div>` : "")
    + risk + curves
    + (excluded ? `<div class="muted ee-foot">${excluded}</div>` : "");
}


// The one table that lives above the tabs, with the health light and the
// strip (user ruling, 2026-08-21): one row per event day, and it answers
// "what happened yesterday" without choosing a group first.
//
// The live book since 2026-09-16 (user ruling: 每日统计、逐站逐日明细也改为
// 实盘数据; the paper ledger is a rule comparator).  Two grains, each named
// in its header: a station-day for the basket verdicts, a 笔 (one stage of
// one station-day that filled) for the claim, the hit rate against it and
// the odds -- the grain the money board's nine numbers read, so the two
// agree.
function daily(){
  const rows=D.daily||[];
  if(!rows.length) return "";
  // A hot update can draw this template around a payload built before the
  // table moved to the live book; its rows are the paper table's shape.
  if(rows[0].stations_posted===undefined)
    return `<h2>每日统计：按天汇总实盘下单、成交与结算</h2>
    <div class="card muted">这一节已改读实盘，当前数据来自改动之前的整库渲染，下一轮整库渲染后显示。</div>`;
  const STAGES=["P0","P0-M","P1"];
  const pct=v=>v==null?"—":(v*100).toFixed(0)+"%";
  const usd=v=>v==null?"—":v.toFixed(2);
  const signed=v=>v==null?"—":((v>=0?"+":"")+v.toFixed(2));
  // Newest five open, the rest folded (user ruling, 2026-09-11).
  const OPEN_DAYS=5;
  const rowHtml=r=>{
    const byStage=r.filled_leg_by_stage||{};
    const stageTxt=STAGES.map(st=>{
      const n=byStage[st]||0;
      return n?`<span title="${st}：${n} 条成交腿">${st} <b>${n}</b></span>`
        :`<span class="muted">${st} —</span>`;
    }).join(`<span class="muted"> · </span>`);
    const cal=r.calibration_ratio;
    const calCls=cal==null?"":(cal>=0.9&&cal<=1.15?"up":"down");
    const frac=(a,b)=>b?`${a}/${b} <span class="muted">${pct(a/b)}</span>`:"—";
    return `<tr>
      <td class="mono">${h(r.event_date)}</td>
      <td class="mono">${r.stations_posted}<span class="muted"> / ${r.stations_filled}</span></td>
      <td class="mono">${r.baskets}</td>
      <td class="mono">${r.filled_legs}<span class="muted">/${r.posted_legs}</span>
        <div class="muted" style="font-size:11px;margin-top:2px">${stageTxt}</div></td>
      <td class="mono">${usd(r.staked_usdc)}</td>
      <td class="mono">${r.settled_station_days}${r.open_station_days
        ?`<span class="muted"> · 未结 ${r.open_station_days}</span>`:""}</td>
      <td class="mono">${frac(r.basket_hit_station_days, r.settled_station_days)}</td>
      <td class="mono">${frac(r.held_hit_station_days, r.held_station_days)}</td>
      <td class="mono">${r.settled_rows||"—"}</td>
      <td>${pct(r.hit_rate)}</td>
      <td>${pct(r.claimed_win_probability_mean)}</td>
      <td class="factor ${calCls}">${cal==null?"—":cal.toFixed(2)+"×"}</td>
      <td>${r.odds_mean==null?"—":r.odds_mean.toFixed(2)+"x"}</td>
      <td class="mono">${r.settled_rows?usd(r.settled_staked_usdc):"—"}</td>
      <td class="mono ${r.pnl_usdc==null?"":(r.pnl_usdc>=0?"up":"down")}">${signed(r.pnl_usdc)}</td>
      <td class="mono ${r.roi==null?"":(r.roi>=0?"up":"down")}">${r.roi==null?"—":((r.roi>=0?"+":"")+(100*r.roi).toFixed(1)+"%")}</td>
    </tr>`;
  };
  const recent=rows.slice(0,OPEN_DAYS).map(rowHtml).join("");
  const older=rows.slice(OPEN_DAYS);
  const head=`<thead><tr>
    <th>事件日</th>
    <th title="当天有实盘意向的站 / 其中真有腿成交的站">站数<br><span class="muted">下单/成交</span></th>
    <th title="决策数：每个决策是一个篮子">篮子</th>
    <th title="成交腿 / 挂出的腿，下面按阶段拆成交腿">腿<br><span class="muted">成交/挂出 · 分阶段</span></th>
    <th title="当天所有成交腿的实付，不等结算">投入</th>
    <th title="有真值的站日（不论成交没有）· 还没真值的站日">已结算<br><span class="muted">站日</span></th>
    <th title="真值落在挂出的桶里，成交与否都算（2026-09-14 裁决）">篮子命中<br><span class="muted">站日</span></th>
    <th title="真值落在成交的桶里，只数有成交的站日">持仓命中<br><span class="muted">站日</span></th>
    <th title="一笔＝一个站日一个阶段里成交过的腿，资金看板的九个数按这个口径">已结算<br><span class="muted">笔</span></th>
    <th title="按笔：持有的桶里含真值的比例">命中率<br><span class="muted">笔</span></th>
    <th title="按笔：下单决策绑定的概率包里，持有各桶概率之和">声称<br><span class="muted">笔</span></th>
    <th title="命中率 ÷ 声称，1.00× 是说得准">实现/声称</th>
    <th title="(1−成本)/成本，成本按投入加权后再取倒数">赔率</th>
    <th title="已结算笔的实付">结算投入</th><th>盈亏</th><th>ROI</th>
  </tr></thead>`;
  return `<h2>每日统计：按天汇总实盘下单、成交与结算</h2>
  <p class="sub">全部读<b>实盘成交</b>（<span class="mono">live_submit_authorized=1</span>）：
  钱是交易所报回来的成交名义，结算桶与资金看板同一个解析器。纸面账本从 2026-09-16 起只当规则对比器，不在这里。</p>
  <div class="why"><b>两种口径，列头写明：</b><b>站日</b>＝一个站一个事件日，不论交易了几个阶段——
  「篮子命中」看真值是否落在挂出的桶里（成交没有都算），「持仓命中」看是否落在成交的桶里；
  <b>笔</b>＝一个站日里一个阶段成交过的腿，「命中率 / 声称 / 实现/声称 / 赔率 / 盈亏」按笔算，和资金看板的九个数同一口径。
  「投入」是当天全部成交的实付、不等结算；「盈亏」只属于已结算的笔，所以未结算日这两列不能相除，ROI 用的是「结算投入」。</div>
  <div class="card"><table>${head}<tbody>${recent}</tbody></table>${older.length ? `<details style="margin-top:10px">
      <summary class="muted" style="cursor:pointer">更早的 ${older.length} 天
        （${h(older[older.length-1].event_date)} 到 ${h(older[0].event_date)}）</summary>
      <table style="margin-top:8px">${head}
        <tbody>${older.map(rowHtml).join("")}</tbody></table></details>` : ""}</div>`;
}

function coverage(){
  // Production's seats for the live orders, and what those orders settled
  // at (user ruling, 2026-09-16: the paper ledger is a rule comparator).
  const c=(D.live_selection||{}).coverage||{};
  if(!c.decision_count) return "";
  const p=v=>v==null?"—":(v*100).toFixed(1)+"%";
  const n=(v,d=3)=>v==null?"—":Number(v).toFixed(d);
  const sd=c.sides||{};const tot=c.decision_count||1;
  const seg=(k,col)=>`<i style="display:block;height:9px;width:${100*(sd[k]||0)/tot}%;background:${col}"></i>`;
  return `<h2>覆盖 · 价格 · EV：检验买入价与窗口覆盖率撑不撑得起事前期望</h2>
  <p class="sub">只算挂到交易所的实盘决策：前三格读生产那一笔决策入座时记下的席位，
  第四格是这些单实际的结算。三组数回答同一个问题的三面：
  座位罩住了多少概率、为此付了多少、这笔交换值不值。</p>
  <div class="card"><div class="muted">相对市场的位置（<b>席位中心桶</b>对市场中心桶）
    <span title="生产那一笔决策记下的席位中心桶（宪章 54）与市场中心桶，按包络桶序号比较，偏一格就算偏。
不是模型未取整的质量中心：那是另一个数，两者在部分决策上不一致。">ⓘ</span></div>
    <div style="display:flex;height:9px;border-radius:99px;overflow:hidden;margin-top:7px">
      ${seg("above","#bc8cff")}${seg("aligned","#3fb950")}${seg("below","#d29922")}</div>
    <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
      <span class="pill" style="background:var(--pill-violet-bg);color:var(--violet)">偏上 ${sd.above||0}</span>
      <span class="pill green">一致 ${sd.aligned||0}</span>
      <span class="pill amber">偏下 ${sd.below||0}</span>
      <span style="flex:1"></span>
      <span class="muted">${c.decision_count} 笔实盘决策 · ${c.day_count} 天</span></div></div>
  <div class="grid g4">
    <div class="card"><div class="muted">选桶概率和</div>
      <div class="big">${p(c.model_coverage_mean)}</div>
      <div class="muted">校准后概率在座位内的合计 · 平均 ${n(c.window_size_mean,1)} 桶</div></div>
    <div class="card"><div class="muted">选桶卖价和</div>
      <div class="big">${n(c.seat_ask_total_mean,3)}</div>
      <div class="muted">同样这几个桶的全含费卖价合计，即整篮成本</div></div>
    <div class="card"><div class="muted">每 1U 的期望回报</div>
      <div class="big ${(c.ev_per_usdc_mean||0)>0?"up":"down"}">${(c.ev_per_usdc_mean||0)>0?"+":""}${n(c.ev_per_usdc_mean,2)}U</div>
      <div class="muted">概率和÷卖价和 − 1，把不同价位的座位放到同一把尺子上</div></div>
    <div class="card"><div class="muted">已结算：期望 vs 实得</div>
      <div class="big">${n(c.expected_usdc,2)} → <span class="${(c.realized_usdc||0)>=0?"up":"down"}">${n(c.realized_usdc,2)}</span></div>
      <div class="muted">实盘已结算 ${c.settled_count} 笔 · 投入 ${n(c.staked_usdc,2)}U · 兑现比 ${c.realization_ratio==null?"—":n(c.realization_ratio,2)+"×"}</div></div>
  </div>
  <div class="why"><b>三个数都只关于这个篮子：</b>概率和是我们认为它值多少，
  卖价和是买下它要付多少，EV/U 是两者的比值减一——把 0.07 的座位和 0.7 的座位
  放到同一把尺子上比较。兑现比（实得÷期望）是最终裁判：长期低于 1，
  说明期望里有校准高估的水分。</div>`;
}


// A station's whole-window account, one row per station.  These averages
// used to ride along on every row of the daily roster, where a station with
// no book that day still showed an ask total and an EV and read as that
// day's price.  Here the window is stated once in the header and a row is a
// station, so the number cannot be mistaken for a date's.
// Station geography, [latitude, coastal], and the climate bands the station
// table groups by.  Top level since 逐站汇总（跨日） merged into the go-live
// station table (user ruling, 2026-09-16).
const GEO={CYYZ:[43.679,0],EDDM:[48.348,0],EFHK:[60.327,1],EGLC:[51.505,0],
  EHAM:[52.315,1],EPWA:[52.163,0],FACT:[-33.965,1],KATL:[33.63,0],
  KAUS:[30.183,0],KBKF:[39.713,0],KDAL:[32.838,0],KHOU:[29.646,1],
  KLAX:[33.938,1],KLGA:[40.779,1],KMIA:[25.788,1],KORD:[41.96,0],
  KSEA:[47.445,1],KSFO:[37.62,1],LEMD:[40.466,0],LFPB:[48.967,0],
  LIMC:[45.631,0],LLBG:[32.011,1],LTAC:[40.128,0],LTFM:[41.262,1],
  MMMX:[19.436,0],MPMG:[8.967,1],NZWN:[-41.331,1],OEJN:[21.685,1],
  OPKC:[24.902,1],RCSS:[25.069,1],RJTT:[35.553,1],RKPK:[35.179,1],
  RKSI:[37.469,1],RPLL:[14.507,1],SAEZ:[-34.822,1],SBGR:[-23.432,0],
  UUWW:[55.592,0],VILK:[26.761,0],WMKK:[2.747,1],WSSS:[1.368,1],
  ZBAA:[40.082,0],ZGGG:[23.392,0],ZGSZ:[22.639,1],ZHHH:[30.783,0],
  ZSPD:[31.146,1],ZSQD:[36.362,1],ZUCK:[29.718,0],ZUUU:[30.576,0]};
// Band edges at the tropic (23.4), 35 and 55 degrees.  The tropic edge is
// deliberately the line itself, not a rounded 23.5: Sao Paulo (-23.432)
// and Guangzhou (23.392) both sit within 5 km of it, and 23.5 would put
// Sao Paulo -- a 750 m subtropical-highland station -- in the tropics
// band while the line itself does not.
const bandOf=lat=>lat>=55?"北半球·高纬":lat>=35?"北半球·温带"
  :lat>=23.4?"北半球·副热带":lat>=-23.4?"热带"
  :lat>=-35?"南半球·副热带":"南半球·温带";
const BAND_ORDER=["北半球·高纬","北半球·温带","北半球·副热带","热带",
  "南半球·副热带","南半球·温带"];
const fmtLat=lat=>Math.abs(lat).toFixed(1)+"°"+(lat>=0?"N":"S");



// 逐站逐日明细 off the live book (user ruling, 2026-09-16).  One row per
// station that minted a live intent that day, one cell per stage: every
// bucket the stage posted, cold to warm, struck through where nothing
// filled; what the held legs claimed and cost; and the per-leg fill under a
// fold.  Stations with no live intent are not listed -- there is no live
// account of them to show -- and the day line counts them.
function roster(){
  const R=D.live_roster||{};
  // Why each station-day without a live position did not build, by day.
  const NOT_BUILT={};
  (((D.live_selection||{}).refusal_stats||{}).station_days||[]).forEach(x=>{
    (NOT_BUILT[x.event_date]||(NOT_BUILT[x.event_date]=[])).push(x);
  });
  const dates=[...new Set([...Object.keys(R), ...Object.keys(NOT_BUILT)])]
    .sort().reverse();
  if(!dates.length) return "";
  const short=x=>String(x).replace("°C","").replace("°F","");
  const usd2=v=>v==null?"—":v.toFixed(2);
  const pctS=v=>v==null?"—":(v*100).toFixed(0)+"%";
  const STATE={
    hit:["st-hit","持仓命中"],basket:["st-open","篮子命中·没买到"],
    miss:["st-miss","未命中"],open:["st-open","未结算"],flat:["st-flat","未成交"],
  };
  const stateOf=(e)=>{
    if(e.settled_bucket==null) return e.filled_legs?"open":"flat";
    if(e.held_hit) return "hit";
    if(e.basket_hit) return "basket";
    return e.filled_legs?"miss":"flat";
  };
  const cell=(s, truth)=>{
    if(!s) return `<td class="muted">—</td>`;
    const ladder=rungSorted(s.buckets, b=>b.bucket);
    const chips=ladder.map(b=>{
      const got=b.filled_size>0;
      const isTruth=truth!=null&&String(b.bucket)===String(truth);
      return `<span class="mono ${got?(truth==null?"":(isTruth?"up":"down")):"muted"}"
        style="${got?"":"text-decoration:line-through"}"
        title="${h(b.bucket)} · 挂 ${shares(b.posted_size)} 份 · 成交 ${shares(b.filled_size)} 份 · 声称 ${pctS(b.claimed_probability)}${isTruth?" · 结算桶":""}">${h(short(b.bucket))}${isTruth?"✓":""}</span>`;
    }).join("+");
    const claim=s.claimed_win_probability, cost=s.basket_all_in_cost;
    const odds=cost?((1-cost)/cost):null;
    const money=s.filled_legs
      ? `<div class="muted">${pctS(claim)} @ ${cost==null?"—":cost.toFixed(3)} · ${odds==null?"—":odds.toFixed(2)+"x"}</div>
         <div class="muted" style="font-size:11px">实投 ${usd2(s.stake_usdc)} · ${s.filled_legs}/${s.posted_legs} 腿成交${s.baskets>1?` · ${s.baskets} 篮`:""}</div>`
      : `<div class="muted" style="font-size:11px">${s.posted_legs} 腿挂出、一腿都没成交${s.baskets>1?` · ${s.baskets} 篮`:""}</div>`;
    const legs=ladder.map(b=>`<tr${b.filled_size>0?"":' class="muted"'}>
      <td>${h(b.bucket)}</td><td>${shares(b.posted_size)}</td>
      <td>${b.filled_size>0?shares(b.filled_size):"<b>0</b>"}</td>
      <td>${b.filled_size>0?(b.filled_notional_usdc/b.filled_size).toFixed(4):"—"}</td>
      <td>${b.filled_size>0?usd2(b.filled_notional_usdc):"—"}</td>
      <td>${pctS(b.claimed_probability)}</td></tr>`).join("");
    return `<td><div class="mono" style="font-size:11.5px">${chips}</div>${money}
      <details style="font-size:11px"><summary class="muted">逐腿</summary>
      <table class="mono" style="font-size:11px"><thead><tr>
        <th>桶</th><th>挂出份</th><th>成交份</th><th>均价</th><th>资金</th><th>声称</th>
      </tr></thead><tbody>${legs}</tbody></table></details></td>`;
  };
  const row=(e)=>{
    const state=stateOf(e);
    const src=e.settled_source==="truth_lane"
      ?`<div class="muted" style="font-size:11px" title="结算桶取自真值车道，按当日自己的桶阶梯落桶">真值车道</div>`:"";
    const pnl=e.pnl_usdc==null
      ?`<span class="muted">—</span>`
      :`<span class="${e.pnl_usdc>=0?"up":"down"}">${e.pnl_usdc>=0?"+":""}${e.pnl_usdc.toFixed(2)}</span>`;
    return `<tr>
      <td class="st ${STATE[state][0]}" title="${STATE[state][1]}">
        <div class="mono">${e.market_slug
          ? `<a class="mkt" target="_blank" rel="noopener noreferrer"
              href="https://polymarket.com/event/${encodeURIComponent(e.market_slug)}"
              title="打开 ${h(e.station_id)} ${h(e.event_date)} 的盘口">${h(e.station_id)} ↗</a>`
          : h(e.station_id)}</div>
        <div class="muted">${h(e.station_label)}</div>
        <div class="muted" style="font-size:11px">${STATE[state][1]}</div></td>
      ${cell(e.stages.P0, e.settled_bucket)}
      ${cell(e.stages["P0-M"], e.settled_bucket)}
      ${cell(e.stages.P1, e.settled_bucket)}
      <td class="mono">${e.settled_bucket==null?`<span class="muted">未结算</span>`:h(e.settled_bucket)}${src}</td>
      <td class="mono">${pnl}<div class="muted" style="font-size:11px">投入 ${usd2(e.staked_usdc)}</div></td></tr>`;
  };
  const head=`<tr><th>站点</th><th>P0 建仓</th><th>P0-M 确认</th><th>P1 盯盘</th>
    <th>结算桶</th><th>实得</th></tr>`;
  const notBuiltLine=(day)=>{
    const items=NOT_BUILT[day]||[];
    if(!items.length) return "";
    const byReason={};
    items.forEach(x=>{(byReason[x.label]||(byReason[x.label]=[])).push(x.station_id);});
    const parts=Object.entries(byReason).sort((a,b)=>b[1].length-a[1].length)
      .map(([label,ids])=>`<div><b>${h(label)}</b> ${ids.length}：<span class="mono">${
        ids.sort().map(h).join(" ")}</span></div>`).join("");
    return `<details class="note" style="margin-top:8px"><summary>没建仓 ${items.length} 个站日 · 按原因</summary>
      <div class="muted" style="font-size:12px">${parts}</div></details>`;
  };
  const blocks=dates.map(day=>{
    const rows=R[day]||[];
    const filled=rows.filter(e=>e.filled_legs).length;
    const staked=rows.reduce((a,e)=>a+(e.staked_usdc||0),0);
    const settled=rows.filter(e=>e.pnl_usdc!=null);
    const pnl=settled.reduce((a,e)=>a+e.pnl_usdc,0);
    const money=settled.length
      ?` · <span class="${pnl>=0?"up":"down"}">${pnl>=0?"+":""}${pnl.toFixed(2)} U</span>`:"";
    return {day, count:rows.length, filled,
      pane:`<div data-daypane="${h(day)}" hidden>
      <div class="card">
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:baseline">
          <b class="mono">${h(day)}</b>
          <span class="muted">${rows.length} 站下单 · ${filled} 站成交 · 实投 ${usd2(staked)} U${money}</span></div>
        ${rows.length?`<div class="scroll" style="margin-top:10px"><table>
          <thead>${head}</thead><tbody>${rows.map(row).join("")}</tbody></table></div>`:""}
        ${notBuiltLine(day)}
      </div></div>`};
  });
  // Nothing open until a date is clicked (user ruling, 2026-09-11).
  const dayNav=`<nav class="daytabs">${blocks.map(b=>
    `<button type="button" data-day="${h(b.day)}"
      title="${b.count} 站下单 · ${b.filled} 站成交">${h(b.day)}
      <span class="muted">成交 ${b.filled}</span></button>`).join("")
    }<button type="button" data-day="all">全部 <span class="muted">${
      blocks.length} 天</span></button></nav>`;
  return `<h2>逐站逐日明细：逐个站日核对实盘挂了哪些桶、买到了哪些，没建仓的为什么</h2>
  <p class="sub">全部读<b>实盘成交</b>与生产决策。每个事件日一组，表里列当天铸出过实盘意向的站，
  表下「没建仓」列出当天生产在 P0 决策过、却没有实盘成交的站日和原因。
  每格上行是这一阶段挂出的桶（冷到暖，划线＝一份没成交，✓＝结算桶），下方是持有各桶的声称 @ 成本 · 赔率、实投与成交腿数，
  展开「逐腿」是每个桶挂出份数 / 成交份数 / 均价 / 资金 / 声称。
  <b>默认一天都不展开</b>——点日期展开，「全部」是通栏视图。</p>
  <div class="why"><b>站点格的底色＝这个站日最后怎么了：</b>
  <span class="pill green">持仓命中</span>成交的桶里有真值 ·
  <span class="pill amber">篮子命中·没买到</span>挂出的桶里有真值、但那一桶没成交 ·
  <span class="pill red">未命中</span>挂出的桶里没有真值 ·
  <span class="pill amber">未结算</span>买了但真值还没到 ·
  <span class="pill">未成交</span>挂了一腿都没成交。
  <b>声称</b>＝下单那个决策绑定的概率包里该桶的概率；份数不等的持仓按概率加权份数算成本，保证「投入×(声称÷成本−1)」就是这些腿声称能赚的钱。</div>
  ${refusalStats()}
  <div data-daytabs="roster">${dayNav}${blocks.map(b=>b.pane).join("")}</div>`;
}


const pct1 = (v) => v == null ? "—" : (v * 100).toFixed(1) + "%";
const deg2 = (v) => v == null ? "—" : v.toFixed(2) + " °C";

function dispersion(){
  const S = D.skill || {}; const rows = S.rows || [];
  if(!rows.length) return "";
  const body = rows.map(r => `<tr>
    <td><b>${h(r.label)}</b></td>
    <td>${h(r.lead_label)}</td>
    <td class="mono">${r.runs.toLocaleString()}</td>
    <td class="mono">${r.members_per_run == null ? "—" :
      r.members_per_run.toFixed(0)}</td>
    <td class="mono">${deg2(r.member_sigma_c)}</td>
    <td class="mono">${r.paired.toLocaleString()}</td>
    <td class="mono">${r.spread_factor == null ? "—" :
      r.spread_factor.toFixed(2) + "×"}</td>
    <td class="mono">${deg2(r.served_width_c)}</td>
    <td class="mono">${deg2(r.corrected_rmse_c)}</td>
    <td class="mono ${r.width_ratio == null ? "" :
      (r.width_ratio >= 0.85 && r.width_ratio <= 1.15 ? "up" : "down")}">${
      r.width_ratio == null ? "—" : r.width_ratio.toFixed(2) + "×"}</td>
  </tr>`).join("");
  const bare = [...new Set(rows.filter(r => !r.paired).map(r => r.label))];
  return `<h2>四大模型的离散度：衡量模型间分歧，作为服务分布宽度的依据</h2>
  <p class="sub">离散度是一个集合预报里几十个成员之间的分歧，也是模型对自己唯一的
  不确定性表态：成员挤在一起就是"我很有把握"，散开就是"这天不好说"。
  统计窗口 ${h(S.summary.window_start)} 至 ${h(S.summary.window_end)}
  （${S.summary.window_days} 天）。</p>
  <div class="why"><b>怎么看：</b>「成员离散度」是模型原始的成员标准差；系统不直接
  用它，而是乘一个「服务放大」系数再拿去出概率——因为集合预报普遍过于自信，
  成员分歧一向小于实际会犯的错。<b>最右边的「宽度校准比」才是结论：</b>
  它是校正后的实际误差除以服务宽度，1.00× 表示放大得刚好，
  大于 1 表示放大之后仍然偏窄（还是过于自信），小于 1 表示放过了头。</div>
  <table style="margin-top:12px"><thead><tr>
    <th>模型</th><th>提前量</th><th>run 数</th><th>成员/run</th>
    <th>成员离散度 σ</th><th>可比 run</th><th>服务放大</th><th>服务宽度</th>
    <th>校正后 RMSE</th><th>宽度校准比</th>
  </tr></thead><tbody>${body}</tbody></table>
  <div class="muted" style="margin-top:6px">左边三列按全部 run 统计；
  「服务放大」之后的四列只能在有校正表可用的那部分 run 上测，
  所以另立「可比 run」一列说明它们的分母。${bare.length ?
    `<b>${bare.join("、")}</b> 在本窗口内一个可比 run 都没有：
     它现存的校正表是按旧的 d0/d1/d2 口径、且挂在聚合 cycle 上拟合的，
     和它今天每一次 native 采集所带的 run cycle 对不上，因此右侧四列为空。`
    : ""}</div>`;
}

function hitRate(){
  const S = D.skill || {};
  const rows = S.rows || []; const totals = S.totals || [];
  if(!rows.length) return "";
  const cards = totals.map(t => {
    const gain = t.paired ?
      t.paired_calibrated_rates[1] - t.paired_raw_rates[1] : null;
    return `<div class="card">
      <div style="display:flex;align-items:center;gap:8px">
        <b>${h(t.label)}</b><span style="flex:1"></span>
        <span class="muted">${t.scored.toLocaleString()} 个已结算 run</span>
      </div>
      <div class="big" style="margin-top:6px">${pct1(t.raw_rates[1])}</div>
      <div class="muted">原始预测落在真值 ±1 桶内</div>
      <div style="margin-top:9px;font-size:13.5px">
        精准命中 ${pct1(t.raw_rates[0])} · ±2 桶 ${pct1(t.raw_rates[2])}</div>
      ${t.paired ? `<div class="why" style="margin-top:10px">
        同样 ${t.paired.toLocaleString()} 个 run 上，校正后 ±1 桶
        <b class="up">${pct1(t.paired_calibrated_rates[1])}</b>
        （校正前 ${pct1(t.paired_raw_rates[1])}，
        ${gain >= 0 ? "+" : ""}${(gain * 100).toFixed(1)} 个百分点）</div>`
      : `<div class="why" style="margin-top:10px">
        这个模型在本窗口内没有自己的校正表，无法给出校正后数字</div>`}
    </div>`;
  }).join("");
  // One table, compared metric by metric (user rulings, 2026-09-17: 放在一起
  // 对比，不要左右).  Each metric is a group of three: 原始 over every settled
  // run, then 校正前 and 校正后 on the same runs -- the ones that had a
  // calibration table fitted before the event day -- side by side.  A row
  // with no such run shows those cells empty.
  const EDGE = 'style="border-left:1px solid var(--line)"';
  const rate = (v, edge="", cls="") => `<td class="${cls}"${edge?" "+edge:""}>${v == null ? "—" : pct1(v)}</td>`;
  const after = (before, v) => `<td>${v == null ? '<span class="muted">—</span>'
    : `<b class="${before == null ? "" : (v > before ? "up" : (v < before ? "down" : ""))}">${pct1(v)}</b>`}</td>`;
  const metric = (r, k, rawCls="") => {
    const before = r.paired ? r.paired_raw_rates[k] : null;
    const cal = r.paired ? r.paired_calibrated_rates[k] : null;
    return rate(r.raw_rates[k], EDGE, rawCls)
      + `<td class="${before == null ? "muted" : ""}">${before == null ? "—" : pct1(before)}</td>`
      + after(before, cal);
  };
  const body = rows.map(r => `<tr>
    <td><b>${h(r.label)}</b></td>
    <td>${h(r.lead_label)}</td>
    <td class="mono" ${EDGE}>${r.scored.toLocaleString()}</td>
    <td class="mono ${r.paired ? "" : "muted"}"${r.paired ? "" :
      ` title="这一格的 ${r.uncorrected.toLocaleString()} 个 run 都没有事件日之前就已拟合好的校正表，不参与校正前后对比"`}>${
      (r.paired || 0).toLocaleString()}</td>
    ${metric(r, 0, r.raw_rates[0] >= 0.25 ? "up" : "")}
    ${metric(r, 1)}
    ${metric(r, 2)}
    <td class="mono" ${EDGE}>${deg2(r.mae_c)}</td>
    <td class="mono">${r.paired ? `<b>${deg2(r.paired_mae_c)}</b>` : '<span class="muted">—</span>'}</td>
    <td class="mono ${r.signed_bias_c == null ? "" :
      (Math.abs(r.signed_bias_c) <= 0.5 ? "up" : "down")}" ${EDGE}>${
      r.signed_bias_c == null ? "—" :
      (r.signed_bias_c >= 0 ? "+" : "") + r.signed_bias_c.toFixed(2)}</td>
  </tr>`).join("");
  const group = (label, span) => `<th colspan="${span}" class="muted" style="text-align:center;font-weight:normal;border-left:1px solid var(--line)">${label}</th>`;
  const trio = `<th ${EDGE} title="原始：全部已结算 run">原始</th>
    <th title="校正前：有事件日之前拟合好校正表的那批 run，校正之前">校正前</th>
    <th title="校正后：同一批 run，套上事件日之前拟合好的校正表之后">校正后</th>`;
  return `<h2>四大模型的命中率：检验偏差订正有没有让预报更准</h2>
  <p class="sub">把一个模型的成员均值当成它的答案，看它落在市场的哪个桶里，
  再和当天结算真值所在的桶比。桶按各站自己的市场刻度数——美国站 2°F 一档，
  其余 1°C 一档——所以"差一个桶"在各站是同一件事。</p>
  <div class="grid g4" style="margin-top:12px">${cards}</div>
  <div class="why" style="margin-top:12px"><b>为什么原始命中率这么低：</b>
  全球集合预报没有见过某一个跑道旁的温度计。它给的是格点上的温度，
  站点的地形、下垫面、仪器位置带来的系统性偏差要靠每站的偏差订正表补回来，
  这正是下表「校正前 → 校正后」两列在量的东西。</div>
  <div class="why" style="margin-top:12px"><b>读表前两点必须说明：</b>一是用的校正表必须是<b>事件日之前</b>就已经
  拟合好的那一版，所以它没见过被打分的这一天；二是「校正前」「校正后」跑在<b>同一批 run</b>（可比 run）上，
  只有约三成 run 在当时已经有可用的校正表。「原始」是全部已结算 run，拿它去比校正后，
  比的是覆盖面而不是校正本身——比校正效果看紧挨着的校正前和校正后。</div>
  <table style="margin-top:12px"><thead><tr>
    <th rowspan="2">模型</th><th rowspan="2">提前量</th>
    ${group("run 数", 2)}${group("精准命中", 3)}${group("±1 桶内", 3)}${group("±2 桶内", 3)}
    ${group("平均绝对误差", 2)}
    <th rowspan="2" ${EDGE} title="预报减真值，全部已结算 run">平均偏差</th>
  </tr><tr>
    <th ${EDGE} title="全部已结算 run">已结算</th><th title="有事件日之前拟合好校正表的 run，校正前后都在这批上算">可比</th>
    ${trio}${trio}${trio}
    <th ${EDGE} title="全部已结算 run，校正前">原始</th><th title="可比 run，校正后">校正后</th>
  </tr></thead><tbody>${body}</tbody></table>
  <div class="muted" style="margin-top:6px">「平均偏差」是预报减真值：
  负数表示这个模型在这一提前量上系统性报冷。「校正后」绿色＝比紧挨着的校正前高、红色＝低。
  「可比」为 0 的格子没有事件日之前就已拟合好的校正表，不参与校正前后对比（悬停看 run 数）。</div>`;
}


function h5Readiness(){
  // The single-system bias half-life in one place, conclusions only (user
  // rulings, 2026-09-17: 和换代进度融合，突出结论; 增加 h15 的对比; 测试 H3;
  // 文案前加单系统校准).  Two answers: has the fleet turned over to the
  // 5-day tables, and which half-life corrects closest to truth when the
  // half-life is the only thing changed (``collect_bias_half_life``).  An arm
  // differs from the serving one only when its day-resampled interval
  // clears zero -- a station-day vote alone reads weather shared across the
  // fleet as independent evidence.
  const H = D.h5_readiness || {}; const days = H.days || [];
  const B = D.bias_half_life || {};
  if(!days.length && !B.available) return "";
  const latest = days.length ? days[days.length-1] : null;
  const ready = H.scoreable_days || [];
  const done = latest && latest.share >= 0.9;
  const rollPill = !latest ? "" : done
    ? `<span class="pill green">已换代 ${(100*latest.share).toFixed(0)}%</span>`
    : `<span class="pill amber">换代中 ${(100*latest.share).toFixed(0)}%</span>`;
  const rollLine = !latest ? "" : done
    ? `<b>舰队已全部换到 H5：</b>最近事件日 ${h(latest.event_date)} 的
       ${latest.decisions.toLocaleString()} 笔已标定决策中 ${(100*latest.share).toFixed(0)}% 由 5 天半衰期订正表服务。`
    : `<b>还在换代：</b>最近事件日 ${h(latest.event_date)} 只有 ${(100*latest.share).toFixed(0)}% 的决策由 H5 订正表服务。`;

  const hl = v => "H" + Number(v).toFixed(0);
  // Rounded first, so a value that rounds to zero reads 0.000, not -0.000.
  const signed = (v, d=3) => {
    const r = Number(Number(v).toFixed(d));
    return r === 0 ? (0).toFixed(d) : (r > 0 ? "+" : "") + r.toFixed(d);
  };
  let accPill = "", accLine = "", table = "";
  if(B.available){
    const serving = B.serving_half_life_days;
    const mae = {}; (B.arms||[]).forEach(a => { mae[a.half_life_days] = a.mae_c; });
    const best = (B.arms||[]).reduce((x, a) => (!x || a.mae_c < x.mae_c) ? a : x, null);
    const verdict = c => c.ci_low_c > 0 ? "worse" : (c.ci_high_c < 0 ? "better" : "tie");
    const beaten = (B.comparisons||[]).filter(c => verdict(c) === "better");
    const phrase = c => {
      const v = verdict(c);
      return `${hl(c.half_life_days)} ${v === "worse" ? "显著更差" : v === "better" ? "显著更准" : "没有显著差别"}（误差${
        c.mean_diff_c >= 0 ? "多" : "少"} ${Math.abs(c.mean_diff_c).toFixed(3)}°C，95% 区间 ${signed(c.ci_low_c)} 至 ${signed(c.ci_high_c)}）`;
    };
    if(beaten.length){
      accPill = `<span class="pill amber">${beaten.map(c => hl(c.half_life_days)).join("、")} 比 ${hl(serving)} 更准</span>`;
      accLine = `<b>现役 ${hl(serving)} 不是最准的：</b>${(B.comparisons||[]).map(phrase).join("；")}。`;
    } else if(best && best.half_life_days === serving){
      accPill = `<span class="pill green">${hl(serving)} 最准</span>`;
      accLine = `<b>现役 ${hl(serving)} 在三者里订正后误差最小：</b>${(B.comparisons||[]).map(phrase).join("；")}。`;
    } else {
      accPill = `<span class="pill green">${hl(serving)} 没有被超过</span>`;
      accLine = `<b>没有哪个半衰期显著好过现役 ${hl(serving)}：</b>${(B.comparisons||[]).map(phrase).join("；")}。`;
    }
    const cmp = {}; (B.comparisons||[]).forEach(c => { cmp[c.half_life_days] = c; });
    const rows = (B.arms||[]).map(a => {
      const c = cmp[a.half_life_days];
      const isServing = a.half_life_days === serving;
      const decided = c ? c.arm_closer + c.serving_closer : 0;
      return `<tr${isServing ? ' style="font-weight:600"' : ""}>
        <td>${hl(a.half_life_days)}（${Number(a.half_life_days).toFixed(0)} 天${isServing ? "，现役" : ""}）</td>
        <td class="mono ${best && a.half_life_days === best.half_life_days ? "up" : ""}">${a.mae_c.toFixed(3)}°C</td>
        <td class="mono ${!c ? "muted" : verdict(c) === "worse" ? "down" : verdict(c) === "better" ? "up" : ""}">${
          !c ? "—" : `${signed(c.mean_diff_c)}°C <span class="muted">[${signed(c.ci_low_c)}, ${signed(c.ci_high_c)}]</span>`}</td>
        <td class="mono">${!c ? "—" : `${c.arm_closer}:${c.serving_closer}${decided ? ` <span class="muted">${(100*c.arm_closer/decided).toFixed(0)}%</span>` : ""}`}</td>
      </tr>`;
    }).join("");
    table = `<table style="margin-top:8px;font-size:13.5px"><thead><tr>
        <th>半衰期</th><th>订正后平均绝对误差</th><th>比 ${hl(serving)} 多出的误差 <span class="muted">[95% 区间]</span></th>
        <th>离真值更近的站日 <span class="muted">它:${hl(serving)}</span></th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  return `<h2 id="h5">单系统校准 · 偏差半衰期 H5：换代进度与订正准度 ${rollPill} ${accPill}</h2>
  <p class="sub">单系统校准里每个模型自己的中心偏差，按最近真值加权拟合；半衰期 2026-08-27 从 15 天改为 5 天（H5）。</p>
  <div class="card" style="font-size:14.5px;line-height:1.7">
    ${B.available ? `<div>${accPill} ${accLine}</div>${table}` : ""}
    ${latest ? `<div style="margin-top:6px">${rollPill} ${rollLine}</div>` : ""}
  </div>
  ${B.available ? `<div class="muted" style="margin-top:6px">准度＝最近 ${h(B.window_from)} 至 ${h(B.window_to)} 的
    ${B.station_days.toLocaleString()} 个已结算站日，每天只用之前的数据按各半衰期重拟合（生产的样本加载、拟合、订正函数），
    只换半衰期；区间按整天重抽样。每天算一次，测于 ${h(String(B.measured_at_utc||"").slice(0,16).replace("T"," "))}Z。</div>` : ""}`;
}


function oosScores(){
  const S = D.scores || {}; const rows = S.rows || [];
  if(!rows.length) return "";
  const n3 = (v) => v == null ? "—" : v.toFixed(3);
  const body = rows.map(r => `<tr>
    <td>${h(r.lead_label)}</td>
    <td class="mono">${r.scopes}</td>
    <td class="${r.pass_rate == null ? "" :
      (r.pass_rate >= 0.75 ? "up" : "down")}">${pct1(r.pass_rate)}</td>
    <td class="mono">${n3(r.crps)}</td>
    <td class="mono muted">${n3(r.crps_base)}</td>
    <td class="mono ${r.crps_skill == null ? "" :
      (r.crps_skill > 0 ? "up" : "down")}">${pct1(r.crps_skill)}</td>
    <td class="mono">${n3(r.brier)}</td>
    <td class="mono muted">${n3(r.brier_base)}</td>
    <td class="mono ${r.brier_skill == null ? "" :
      (r.brier_skill > 0 ? "up" : "down")}">${pct1(r.brier_skill)}</td>
    <td class="mono">${n3(r.nll)}</td>
    <td class="mono muted">${n3(r.nll_base)}</td>
  </tr>`).join("");
  return `<h2>OOS 打分：用样本外真值给概率预报打分</h2>
  <p class="sub">这是门自己用的分数，全部在样本外的 holdout 上算。
  近 7 天共 ${S.scope_count.toLocaleString()} 个站×提前量×cycle 被评过，
  最近一次 ${hours(S.latest_age_hours)}。</p>
  <div class="why"><b>三个分数各管一件事：</b>CRPS 管整条分布离真值多远（越小越好，
  单位是度）；Brier 管每个桶的概率说得准不准；NLL 是门做取舍时真正比的那个数。
  <b>光看绝对值没有意义</b>——它随站点、季节、桶宽变。所以每个分数旁边都摆着
  同样日子上的气候基准，右边的「胜出」＝比气候好了百分之多少，
  这才是可以跨站比较的一个数。</div>
  <table style="margin-top:12px"><thead><tr>
    <th>提前量</th><th>评过的格子</th><th>通过率</th>
    <th>CRPS</th><th>气候</th><th>胜出</th>
    <th>Brier</th><th>气候</th><th>胜出</th>
    <th>NLL</th><th>气候</th>
  </tr></thead><tbody>${body}</tbody></table>
  <div class="muted" style="margin-top:6px">窗口口径
    ${h((rows[0] || {}).profile || "")}：5 个训练日 + 3 天共享 holdout。
    没通过的格子不是坏事，是门在挡——它们不会被拿去服务。</div>`;
}

function centerWidth(){
  const rows = D.center_width || [];
  if(!rows.length) return "";
  // The wait was two numbers in a muted line the reader had to subtract
  // (user ruling, 2026-09-12: 加显著的显式倒计时).  It is the card's most
  // actionable fact whenever a served scalar is stale -- P0 serves a width
  // fitted three days before the climate blend came out from under it, and
  // the day count is the only thing between that and a clean refit.
  const countdown = r => {
    if(r.days_remaining == null || !r.min_qualifying_days) return "";
    const done = r.serving_days || 0, need = r.min_qualifying_days;
    const ready = r.days_remaining === 0;
    const pct = Math.max(2, Math.min(100, done / need * 100));
    return `<div style="margin-top:12px;padding:9px 11px;border-radius:8px;
      background:var(${ready ? "--pill-green-bg" : "--pill-amber-bg"})">
      <div style="display:flex;align-items:baseline;gap:9px;flex-wrap:wrap">
        <span style="font-size:21px;font-weight:800;color:var(${
          ready ? "--green" : "--amber"})">${ready
          ? "可以重拟了" : "还需 " + r.days_remaining + " 天"}</span>
        <span class="muted" style="font-size:12px">${ready
          ? "证据已够，下一轮拟合就会试"
          : "才够重拟一次" + (r.earliest_refit_date
            ? " · 最早 " + h(r.earliest_refit_date) : "")}</span>
      </div>
      <div style="height:7px;border-radius:4px;background:var(--bar-bg);
        margin-top:7px;overflow:hidden"><div style="height:100%;width:${
        pct.toFixed(1)}%;background:var(${
        ready ? "--green" : "--amber"})"></div></div>
      <div class="muted" style="font-size:11.5px;margin-top:5px">
        ${done}/${need} 天${ready ? "" :
        "。日期按每天攒一天算，是<b>最早</b>——格子不够的日子不算，" +
        "而服务版本一动，进度整条清零重来（见下方证据时钟）。"}</div></div>`;
  };
  // Charter 57's cells (user ruling, 2026-09-13: 看板加 TAF 格).  The two
  // numbers above serve every P0-M day; a cell replaces them on the days
  // the TAF sits that far from the model centre, once it clears the same
  // hurdles, with its shift turned toward the forecaster.
  const tafCells = r => {
    const t = r.taf_cells;
    if(!t) return "";
    const stateText = {ACCUMULATING: "攒证据中", ADMITTED: "前瞻胜出",
      REFUSED_FORWARD: "前瞻没赢"};
    const lines = (t.cells || []).map(c => {
      const served = c.served;
      const pill = served ? `<span class="pill green">服务中</span>`
        : c.state ? `<span class="pill ${c.state === "REFUSED_FORWARD"
            ? "green" : "amber"}">${h(stateText[c.state] || c.state)}</span>`
        : `<span class="pill amber">未读</span>`;
      const count = (v, need, unit) => v == null ? "—" :
        `<b>${v.toLocaleString()}</b>/${need}${unit}`;
      const detail = served
        ? `δ ${served[0] >= 0 ? "+" : ""}${served[0].toFixed(2)} ` +
          `± ${served[1].toFixed(2)}（朝 TAF） · λ ${served[2].toFixed(3)}`
        : c.forward_nll_gain == null ? ""
        : `候选 δ ±${Math.abs(c.candidate_taf_delta_c).toFixed(2)} · ` +
          `λ ${c.candidate_width_lambda.toFixed(3)} · 收益 ` +
          c.forward_nll_gain.toFixed(4);
      return `<tr style="white-space:nowrap"><td>${h(c.label)}</td>
        <td style="text-align:right">${count(c.day_count, t.min_days, " 天")}</td>
        <td style="text-align:right">${count(c.cell_count, t.min_cells, " 格")}</td>
        <td>${pill}</td></tr>${detail ? `<tr><td colspan="4" class="muted"
        style="font-size:11px;padding-top:0">${detail}</td></tr>` : ""}`;
    }).join("");
    return `<div style="margin-top:12px;border-top:1px solid
      rgba(128,128,128,.25);padding-top:8px">
      <b>TAF 格</b> <span class="muted" style="font-size:11.5px">按 TAF 最高温离
      模型中心几档分；某格准入后，那类日子改用该格自己的一对数，位移朝 TAF 那一侧。
      没准入就照用上面两个数。</span>
      ${t.measured ? `<div style="overflow-x:auto"><table
        style="margin-top:6px;font-size:12px"><thead><tr><th>格</th><th>天</th>
        <th>格子</th><th>状态</th></tr></thead><tbody>${lines}</tbody></table></div>
      <div class="muted" style="font-size:11.5px">每格要 ${t.min_days} 天且
        ${t.min_cells} 个格子、并在前瞻上赢过上面那对数才准入；上面两个数攒够之前，
        格只计数不评分。</div>`
      : `<div class="muted" style="font-size:11.5px;margin-top:4px">
        距离档规则还没跑过一次拟合，下一轮拟合后出第一份计数。</div>`}
    </div>`;
  };
  const cards = rows.map(r => `<div class="card">
    <div style="display:flex;align-items:center;gap:8px">
      <span class="dot ${r.health}"></span><b>${h(r.stage)}</b>
      <span style="flex:1"></span>
      <span class="pill ${r.health}">${h(r.state)}</span>
    </div>
    <div class="muted" style="margin-top:6px">${h(r.meaning)}</div>
    <div style="display:flex;gap:22px;margin-top:12px">
      <div><div class="muted">中心 δ</div>
        <div class="big ${r.delta_c == null ? "" :
          (Math.abs(r.delta_c) < 1e-9 ? "flat" :
           (r.delta_c > 0 ? "up" : "down"))}">${
          r.delta_c == null ? "—" :
          (r.delta_c > 0 ? "+" : "") + r.delta_c.toFixed(2)}</div>
        <div class="muted">°C，整条分布平移</div></div>
      <div><div class="muted">宽度 λ</div>
        <div class="big ${r.width_lambda == null ? "" :
          (Math.abs(r.width_lambda - 1) < 1e-9 ? "flat" :
           (r.width_lambda < 1 ? "up" : "down"))}">${
          r.width_lambda == null ? "—" : r.width_lambda.toFixed(3)}</div>
        <div class="muted">×，小于 1 是收窄</div></div>
    </div>
    ${r.served_identity ? `<div class="muted" style="margin-top:8px;
      font-size:11.5px"><span class="pill amber">恒等 · 无拟合行</span>
      这两个数不是缺数据，是这一层<b>此刻真正在服务的值</b>：没有生效行时
      运行时原样放行分布（δ=0、λ=1），分布一点没被动过。</div>` : ""}
    ${countdown(r)}
    ${tafCells(r)}
    <div class="muted" style="margin-top:10px">
      证据 ${r.serving_days} 天${r.min_qualifying_days
        ? " / 需 " + r.min_qualifying_days + " 天" : ""}${r.serving_cells ?
        " · " + r.serving_cells.toLocaleString() + " 个格子" : ""} ·
      上次拟合 ${hours(r.fit_age_hours)}</div>
    ${r.forward_nll_gain == null ? "" : `<div class="muted">
      前瞻 NLL 收益 ${r.forward_nll_gain.toFixed(4)}${
      r.min_forward_nll_gain == null ? "" :
      "，门槛 " + r.min_forward_nll_gain}</div>`}
    ${r.ast_days_held == null ? "" : `<div class="muted"
      style="margin-top:8px;border-top:1px solid rgba(128,128,128,.25);
             padding-top:8px">
      <b>证据时钟</b>：边界取<b>服务版本</b>
      <span class="mono">${h(r.served_version || "—")}</span>
      ${r.watchdog_discrepancy
        ? `<span class="pill amber" title="服务函数的 AST 在该版本开始服务之后变过：
若那次改动确实动了分布，请升 posterior_version；若只是记账/回放门，无需处理">
           看门狗：算术变了但版本没升</span>`
        : `<span class="pill green">看门狗：版本与算术同步</span>`}
      <br>代码指纹 <span class="mono">${h(r.ast_fingerprint || "")}</span>
      已保持 ${r.ast_days_held.toFixed(2)} 天
      ${r.ast_generations ? "· 累计 " + r.ast_generations + " 代" : ""}
      ${r.ast_longest_quiet_days == null ? "" :
        "· 历史最长安静窗 " + r.ast_longest_quiet_days.toFixed(1) + " 天"}
      <br>2026-08-27 起指纹只报警不清零：它认代码不认分布，
      只补记账的提交曾与真改算术一样清空八天证据。</div>`}
  </div>`).join("");
  return `<h2>融合分布层 · 中心/宽度两标量：平移分布中心、缩放分布宽度</h2>
  <p class="sub">最后一层修正只有两个数：把整条分布左右平移多少（中心 δ），
  再横向拉宽或收窄多少倍（宽度 λ）。δ=0、λ=1 就是原样不动。</p>
  <div class="why"><b>为什么必须按阶段分开拟合：</b>P0 面对的是一堆模型融出来的
  分布，P1 面对的是当天实测已经把它收窄之后的分布——同一对数字不可能对两者都成立。
  一个阶段还没接线或还在攒证据，是按原样服务，不是出错。
  <b>只有前瞻试算确实变好（NLL 收益超过门槛）才会换上去</b>，试算变差就退回原样。<br>
  <b>"攒证据中"为什么可能一直攒不满（2026-08-27 实测）：</b>证据窗从"服务算术上次
  改变"起算，而那个时刻由被服务函数的 AST 指纹判定——<b>指纹只看代码变没变，
  不看分布变没变</b>：一条只补记账字段、或只开一扇回放门的提交，与真正改算术一样
  把时钟清零。当天 P0 四小时内换了三代指纹，P1 自 08-23 起<b>从未</b>持有过一代
  满 8 天。每张卡片底部的"证据时钟"就是这件事的读数：它不满 8 天，这一层就永远
  按原样服务。</div>
  <div class="grid g3" style="margin-top:12px">${cards}</div>`;
}

// Does the served distribution cover the day?  Scored against the truth,
// per stage.  Rewritten 2026-09-15 (user ruling: 真值落在离服务均值多远：这个也是
// -- the same ask as the truth-landing cross-tab: simple, readable, with its
// conclusion and a summary).  Two questions per stage, each a pair of bars
// on one scale and a one-word verdict: which side of the served mean the
// truth lands on, and whether the seat window covers as often as the
// distribution claims it will.  The per-width and three-state splits are
// the same accounts cut finer, folded underneath.
function centreLanding(){
  const L = D.center_landing || {};
  const stages = (L.stages || []).filter(st => st.cells);
  if(!stages.length) return "";
  const MIN_DAYS = 8;
  const CENTRE = 0.2, HONEST = 0.05;
  // One stage's accounts, pooled over its width tiers -- they partition its
  // cells, and each carries its own seat/mode denominators.
  const pooled = (rows) => {
    const w = (key, n) => {
      let num = 0, den = 0;
      rows.forEach(r => { const k = r[n] || 0; if(r[key] != null && k){ num += r[key] * k; den += k; } });
      return den ? num / den : null;
    };
    return {
      cells: rows.reduce((a, r) => a + (r.cells || 0), 0),
      bias: w("bias_rungs", "cells"),
      claim: w("seat_claim", "seat_cells"),
      cover: w("seat_cover", "seat_cells"),
      mode: w("mode_cover", "mode_cells"),
    };
  };
  const P = stages.map(st => ({st, ...pooled(st.rows || [])}));
  const pc = (v) => v == null ? "—" : (100 * v).toFixed(0) + "%";
  const rungs = (v) => v == null ? "—" : Math.abs(v).toFixed(2) + " 桶";
  const side = (v) => v == null ? "—" : Math.abs(v) < CENTRE ? "基本居中"
    : (v < 0 ? "真值偏冷" : "真值偏暖");
  const honesty = (x) => (x.claim == null || x.cover == null) ? ["", "—"]
    : x.cover < x.claim * (1 - HONEST) ? ["amber", "声称偏高"]
    : x.cover > x.claim * (1 + HONEST) ? ["green", "声称偏保守"] : ["green", "声称诚实"];
  const cbar = (label, v, color) => `<div class="tl-bar"><span class="muted">${label}</span>
    <span class="tl-track"><span style="width:${(100 * (v || 0)).toFixed(1)}%;background:${color}"></span></span>
    <span class="mono">${pc(v)}</span></div>`;
  // A diverging bar: the middle is the served mean, one side colder, one
  // side warmer, clamped at a bucket and a half.
  const dbar = (v) => {
    const x = Math.max(-1.5, Math.min(1.5, v || 0)) / 1.5 * 50;
    return `<div class="cl-div"><span class="muted">冷</span><span class="cl-axis">
      <i style="left:${x < 0 ? 50 + x : 50}%;width:${Math.abs(x)}%;background:${
        Math.abs(v || 0) < CENTRE ? "var(--muted)" : (v < 0 ? "var(--accent)" : "var(--red)")}"></i>
      <b></b></span><span class="muted">暖</span></div>`;
  };
  const rows = P.map(x => {
    const [tone, word] = honesty(x);
    const thin = x.st.days < MIN_DAYS;
    return `<div class="tl-row cl-row">
      <div><b>${h(x.st.stage)}</b><div class="muted" style="font-size:11.5px">${h(x.st.meaning)}</div>
        <div class="muted" style="font-size:11.5px">${x.cells.toLocaleString()} 个格子 · ${x.st.days} 天${
          thin ? ` <span class="pill amber" style="font-size:10px">样本少</span>` : ""}</div></div>
      <div><div class="cl-q">中心：真值落在均值哪边</div>${dbar(x.bias)}
        <div class="mono" style="font-size:12px;text-align:center">${side(x.bias)} ${
          x.bias == null || Math.abs(x.bias) < CENTRE ? "" : rungs(x.bias)}</div></div>
      <div><div class="cl-q">覆盖：席位窗盖住真值</div>
        ${cbar("模型说", x.claim, "var(--violet)")}${cbar("实际", x.cover, "var(--accent)")}</div>
      <div class="tl-verdict"><span class="pill ${tone}">${word}</span>
        <div class="mono" style="font-size:12px">${x.claim == null || x.cover == null ? ""
          : `差 ${((x.cover - x.claim) * 100).toFixed(0)} 点`}</div></div>
    </div>`;
  }).join("");

  const list = (f) => P.map(x => `${h(x.st.stage)} ${f(x)}`).join("、");
  const colder = P.filter(x => x.bias != null && x.bias <= -CENTRE);
  const warmer = P.filter(x => x.bias != null && x.bias >= CENTRE);
  const centreLine = colder.length === P.length
    ? `三个阶段的真值<b>都比服务均值偏冷</b>（${list(x => "−" + rungs(x.bias))}）——模型整体偏暖。`
    : warmer.length === P.length
      ? `三个阶段的真值<b>都比服务均值偏暖</b>（${list(x => "+" + rungs(x.bias))}）——模型整体偏冷。`
      : `中心：${list(x => side(x.bias) + (Math.abs(x.bias || 0) < CENTRE ? "" : " " + rungs(x.bias)))}。`;
  const high = P.filter(x => honesty(x)[1] === "声称偏高");
  const coverLine = high.length === P.length
    ? `席位窗<b>实际盖住的都比模型说的少</b>（${list(x => pc(x.cover) + " 对 " + pc(x.claim))}）。`
    : `覆盖：${list(x => honesty(x)[1] + " " + pc(x.cover) + " 对 " + pc(x.claim))}。`;
  const modeGain = P.filter(x => x.mode != null && x.cover != null);
  const modeLine = modeGain.length && modeGain.every(x => x.mode > x.cover)
    ? `换成以<b>概率最高的桶</b>为心的三桶，每个阶段都能多盖住 ${
        modeGain.map(x => h(x.st.stage) + " +" + ((x.mode - x.cover) * 100).toFixed(0) + " 点").join("、")
      }（但最高桶市场定价也最贵，覆盖多不等于赚钱）。` : "";
  const days = Math.min(...P.map(x => x.st.days));
  const gap = P.filter(x => x.claim != null && x.cover != null);
  const avgGap = gap.length
    ? 100 * gap.reduce((a, x) => a + (x.cover - x.claim) * x.cells, 0) / gap.reduce((a, x) => a + x.cells, 0) : null;
  const summary = avgGap == null ? ""
    : `<b>总结：</b>${avgGap < -100 * HONEST * 0.8
        ? `服务分布把真值放得偏${colder.length >= warmer.length ? "暖" : "冷"}，所以席位窗的覆盖声称平均虚高 <b class="down">${Math.abs(avgGap).toFixed(0)} 点</b>——按声称定价的篮子会系统性买贵。`
        : `席位窗的覆盖声称和实际平均只差 ${avgGap.toFixed(0)} 点，服务分布基本盖得住真值。`}${
        days < MIN_DAYS ? `<span class="muted">（目前只有 ${days} 个事件日，${h(P[0].st.since)} 起，数字还会动。）</span>` : ""}`;

  // The finer cuts, folded: same accounts per width tier and per day shape.
  const detailRows = (list2) => list2.map(r => `<tr><td>${h(r.label)}</td>
    <td class="mono">${(r.cells || 0).toLocaleString()}</td>
    <td class="mono">${r.bias_rungs == null ? "—" : (r.bias_rungs > 0 ? "+" : "") + r.bias_rungs.toFixed(2)}</td>
    <td class="mono">${pc(r.seat_claim)}</td><td class="mono">${pc(r.seat_cover)}</td>
    <td class="mono muted">${pc(r.mode_cover)}</td></tr>`).join("");
  const details = `<details class="note" style="margin-top:8px"><summary>按宽度和当天形状拆开看</summary>
    ${stages.map(st => `<div style="margin-top:8px"><b>${h(st.stage)}</b>
      <div class="scroll"><table style="margin-top:4px"><thead><tr><th>分组</th><th>格子</th>
        <th>偏差（桶，负＝偏冷）</th><th>模型说盖住</th><th>实际盖住</th><th>最高桶为心</th></tr></thead>
      <tbody>${detailRows((st.rows || []).concat(st.states || []))}</tbody></table></div></div>`).join("")}
    <div class="muted" style="font-size:11.5px;margin-top:6px">单位是桶：摄氏市场一桶 1°C、华氏 2°F。
      宽度按服务分布自称的宽度切；形状是宪章 131 的三态读数（${
      h(String((L.seat_rule || {}).skew_threshold_sigma ?? 0.35))}σ）。席位窗＝裁定形状里最宽的窗；
      无席位的格子只进偏差列。</div></details>`;

  return `<h2>真值落在离服务均值多远：检验服务分布盖不盖得住真值</h2>
  <div class="card tl-verdict-card"><b>结论：</b>${centreLine}<br>${coverLine}${
    modeLine ? "<br>" + modeLine : ""}
    ${summary ? `<div style="margin-top:6px">${summary}</div>` : ""}</div>
  <div class="tl-rows">${rows}</div>
  <p class="sub" style="margin-top:6px">中心条：中间是服务分布的均值，蓝往冷、红往暖，差不到 ${CENTRE} 桶算居中。
    覆盖：「模型说」＝席位窗里各桶概率之和，「实际」＝真值落进席位窗的比例，差 ${Math.round(100 * HONEST)}% 以内算诚实。</p>
  ${details}`;
}


// The live flag belongs where the reader's eye already is (user ruling,
// 2026-08-29): it is the one control on this page that changes what the
// system may do, and it was fifteen sections down inside a table of
// station gates.  The banner carries the switch, and since 2026-08-31 the
// readiness section sits directly beneath it in the same head area, so the
// control and its gates are read together.
// What arming does *right now*, not in general.  The three downstream gates
// disagree with each other often enough that "this starts live trading" is
// sometimes false and sometimes a serious understatement.  With no dialog on
// the way up, the hover text is where that sentence lives.
function lcArmEffect(L){
  const C = L.contracts || {};
  const compiled = L.execution_mode === "production_live";
  if(compiled && C.live_submit_authorized === true){
    // No second step any more.  Until 2026-09-02 arming left
    // data/live_paused.flag in place and this string had to tell the
    // operator to go delete a file -- a switch labelled ARMED over a
    // gateway that refused every order.  The switch now lifts the flag on
    // the way up and places it on the way down, so both branches of this
    // sentence became the same sentence.
    return "举旗即刻开始用真实资金下单 —— 开关同时撤掉网关暂停旗";
  }
  return compiled
    ? "举旗：代码已 live，但合同未授权发单，现在铸不出 live 行"
    : "举旗：代码仍编译为 no-live，它不下单，但 go-live 迁移以它为前提";
}

function liveSwitch(){
  const L = D.live_readiness || {};
  if(!L.available) return "";
  const on = !!L.live_enabled;
  return `<span class="pill ${on?"red":""}" id="lc-head"
    style="font-size:13px;display:inline-flex;gap:7px;align-items:center">
    <b>LIVE</b>
    <button type="button" id="lc-live-btn" class="lc-sw" role="switch"
      aria-checked="${on}" aria-label="上线意向旗" disabled onclick="lcLive()"
      title="${h(on ? "点击落旗，即刻停止发单" : lcArmEffect(L))}"></button>
    <span id="lc-live" class="mono">${on?"ARMED":"OFF"}</span>
    <span class="muted" id="lc-head-note" style="font-size:11.5px"
      title="控制服务未连接时开关不可用">控制服务未连接</span>
  </span>`;
}


function liveOrderCard(L){
  // Live rows are live by identity: live_submit_authorized=1 exists only on
  // rows minted after the go-live migration widened the CHECK.  Every number
  // here is counted off those rows at render, so the card and the ledger
  // cannot disagree.
  const O = L.live_orders || {};
  const X = O.exchange || {};
  const P = O.pnl || {};
  // Two different flags, and they disagree today: the production lane is
  // compiled for live, while the runtime contract still refuses to
  // authorise a live submit.  A pill that reads only the first says
  // "实盘发单中" over a book that cannot mint a live row -- the exact
  // reassurance about a system that has stopped being that system which
  // this panel already got wrong once.
  const C = L.contracts || {};
  const compiled = L.execution_mode === "production_live";
  const authorized = C.live_submit_authorized === true;
  const liveMode = compiled && authorized;
  const state = !compiled ? "代码编译为 no-live"
    : !authorized ? "合同未授权发单"
    : L.submission_paused ? "暂停旗在位" : "实盘发单中";
  const u = (v,d=2)=>v==null?"—":v.toFixed(d)+" U";
  const signed = (v,d=2)=>v==null?"—":((v>=0?"+":"")+v.toFixed(d)+" U");
  const pct = (v,d=1)=>v==null?"—":((v>=0?"+":"")+(100*v).toFixed(d)+"%");
  const tile=(lab,val,sub2,klass)=>`<div class="card">
    <div class="muted">${lab}</div>
    <div class="big ${klass||""}">${val}</div>
    <div class="muted" style="font-size:11.5px">${sub2}</div></div>`;
  // Grouped by decision, because the decision is the basket.  The solver
  // sizes the whole support set as one joint action and the selector
  // refuses any proper subset of it -- then the exchange, which has no
  // basket primitive, takes one order per bucket.  A flat leg table shows
  // the exchange's shape and hides ours: read row by row it says the
  // system picks temperature buckets one at a time, when in fact 31°C and
  // 32°C on 2026-08-31 were two legs of a single WMKK basket that stood
  // or fell together.  So legs sit under their basket, and a basket that
  // is one leg wide says so rather than being indistinguishable from a
  // leg that lost its siblings.
  const groups = [];
  const byDecision = new Map();
  for (const r of (O.rows||[])){
    let g = byDecision.get(r.decision_id);
    if (!g){
      g = {decision_id:r.decision_id, station_id:r.station_id||"",
            event_date:r.event_date||"", window:r.execution_window_seconds,
            legs:[], notional:0, latest:r.created_at};
      byDecision.set(r.decision_id, g);
      groups.push(g);
    }
    g.legs.push(r);
    g.notional += (r.requested_notional_usdc||0);
    if (String(r.created_at) > String(g.latest)) g.latest = r.created_at;
  }
  // Legs of one basket carry one deadline; if they ever disagree the
  // narrower one is the one that governs, so show that rather than the
  // first row's.
  for (const g of groups){
    const w = g.legs.map(l=>l.execution_window_seconds).filter(v=>v!=null);
    g.window = w.length ? Math.min(...w) : null;
    // And the basket's own ladder, coldest rung first.  Everything else
    // this loop's readers ask of ``legs`` -- the deadline, the fills, the
    // centre -- is order-free, so the one order that survives is the one
    // the chips are drawn in.
    g.legs = rungSorted(g.legs, l=>l.bucket);
  }
  // One line per basket, one <details> per event date, settled dates
  // closed by default (user ruling, 2026-09-05: the leg table grew with
  // every order and took the viewport).  A leg is a chip: bucket, price x
  // shares, its end state, and -- when the state alone says nothing --
  // the reason its latest event carried.  A settled date shows the bucket
  // the day landed in and each basket's money.
  const short = (r)=>{
    const v = String(r||"");
    if(!v) return "";
    if(v.startsWith("invalid amount")) return "低于$1";
    if(v==="clob_collateral_evidence_unavailable") return "网关抵押读数缺失";
    if(v==="live_station_held") return "站被暂扣";
    if(v==="live_station_barred") return "站被禁入";
    if(v==="live_station_probation") return "站在考察期";
    if(v==="live_intent_disarmed") return "实盘旗未举";
    if(v==="live_submit_not_authorized") return "合同未授权";
    return v.length>28 ? v.slice(0,26)+"…" : v;
  };
  // What the basket's own plan recorded, keyed by the decision that is the
  // basket.  Nothing here is derived: a basket whose plan is no longer in
  // the store gets no ring and no state, because an unmarked basket beats a
  // guessed one.
  const SEATS = O.plan_state || {};
  // The market this station-day traded, straight from the registry the
  // collector read (user ruling, 2026-09-15: 点击即可跳转市场盘口).  No slug,
  // no link: a guessed URL is worse than plain text.
  const SLUGS = O.market_slugs || {};
  const stationLink = (station, day)=>{
    const slug = SLUGS[station + "|" + day];
    return slug
      ? `<a class="mkt" style="color:inherit" target="_blank" rel="noopener noreferrer"
          href="https://polymarket.com/event/${encodeURIComponent(slug)}"
          title="打开 ${h(station)} ${h(day)} 的盘口">${h(station)} ↗</a>`
      : h(station);
  };
  const planOf = (g)=>SEATS[g.decision_id] || null;
  const centerOf = (g)=>String((planOf(g)||{}).center||"");
  // The day's shape, in the words charter 75 places the window with.  Left
  // is the cold tail, right the warm one -- named by where the tail runs,
  // not by which way the page happens to draw the ladder.
  const SKEW_LABEL = {
    SYMMETRIC: ["对称日", "均值与众数相差不到 0.35 σ —— 宪章 131 把席位定为均值桶±1"],
    RIGHT_SKEWED: ["右偏 · 暖尾", "均值在众数之上超过 0.35 σ：席位为 {均值桶, 尾侧一格(, 尾侧两格)}，众数桶在窗外"],
    LEFT_SKEWED: ["左偏 · 冷尾", "均值在众数之下超过 0.35 σ：席位为 {均值桶, 尾侧一格(, 尾侧两格)}，众数桶在窗外"],
  };
  // Charter 131: the windows the day was allowed, as the plan recorded them
  // (ordinal sets, widest first).
  const allowedText = (plan)=>{
    let a = plan.seat_shape_allowed;
    if(typeof a === "string"){ try { a = JSON.parse(a); } catch(e) { a = null; } }
    if(!Array.isArray(a) || !a.length) return "";
    return a.map(w => Array.isArray(w) ? `${w[0]}–${w[w.length-1]}` : String(w)).join(" / ") + " 格序数";
  };
  const skewChip = (g)=>{
    const plan = planOf(g);
    if(!plan || !plan.skew) return "";
    const label = SKEW_LABEL[String(plan.skew)];
    if(!label) return "";
    const gap = plan.gap_buckets;
    const read = plan.skew_source === "reading";
    const detail = read
      ? `均值−众数 = ${gap==null?"—":(gap>0?"+":"")+Number(gap).toFixed(2)} 格`
        + `（判定阈 ±${plan.threshold_buckets==null?"0.50":Number(plan.threshold_buckets).toFixed(2)} 格${plan.threshold_sigma==null?"":" = "+Number(plan.threshold_sigma).toFixed(2)+" σ"}）`
        + (plan.sigma_buckets==null ? "" : ` · 服务宽度 σ ${Number(plan.sigma_buckets).toFixed(2)} 格`)
        + (plan.seat_shape_rule==null ? "" :
            ` · 第 131 条席位形状：${allowedText(plan) || "无可坐窗"}`)
        + " · 取自该决策自己的计划所记形态读数"
      : "取自该计划记下的方向（宪章 75 下方向就是偏态符号）。"
        + "证据不新鲜的那天一律记 NEUTRAL，所以这一路读数可能把当天读成对称——"
        + "形态读数是 2026-09-06 起才写进计划的。";
    const tone = String(plan.skew)==="SYMMETRIC" ? "" : "down";
    return `<span class="pill ${tone}" style="font-size:10px"
      title="${h(label[1])} · ${h(detail)}">${h(label[0])}${read?"":"*"}</span>`;
  };
  const legChip = (r, truth, center)=>{
    const st = String(r.state||"");
    const hit = truth && String(r.bucket)===truth;
    const cls = st==="filled" ? (truth ? (hit?"up":"down") : "") : "muted";
    const mark = st==="filled" ? (truth ? (hit?"✓中":"✗") : "●")
      : st==="failed" ? "✗" : st==="cancelled" ? "—" : "…";
    const why = (st==="failed"||st==="cancelled") ? short(r.reason) : "";
    // The centre is marked on the frame, not on the text: the text colour
    // already carries the leg's own outcome (filled, hit, refused), and
    // painting the centre green there would make one colour answer two
    // different questions.
    const mid = center && String(r.bucket)===String(center);
    const ring = mid
      ? "border-color:var(--up);box-shadow:inset 0 0 0 1px var(--up)"
      : "border-color:var(--line,#ddd)";
    return `<span class="mono ${cls}" style="display:inline-block;padding:1px 6px;margin:1px 3px 1px 0;border:1px solid;${ring};border-radius:5px;font-size:11.5px;white-space:nowrap"
      title="${h(r.bucket)}${mid?" · 均值中心桶":""} · ${h(r.order_intent_id)} · ${h(st)}${r.reason?" · "+h(r.reason):""} · ${h(r.created_at)}">${h(r.bucket)} ${r.limit_price}×${shares(r.requested_size)} <b>${mark}</b>${why?" <span class='muted'>"+h(why)+"</span>":""}</span>`;
  };
  const byDate = new Map();
  for (const g of groups){
    const key = g.event_date || "?";
    if(!byDate.has(key)) byDate.set(key, []);
    byDate.get(key).push(g);
  }
  const truthOf = (g)=> (g.legs.find(l=>l.settled_bucket)||{}).settled_bucket || "";
  // Where that label came from.  ``paper_ledger`` is the settler's own row;
  // ``truth_lane`` is a day the paper book never seated, scored off the
  // truth that stands and the day's own ladder.  The two are different
  // claims and the page says which, because until 2026-09-12 the second
  // kind did not exist here at all -- those days read 未结算 for as long as
  // the paper book declined to seat them.
  const truthSrc = (g)=> (g.legs.find(l=>l.settled_bucket)||{}).settled_bucket_source || "";
  const dates = [...byDate.keys()].sort().reverse();
  // A date folds once its money is counted, or once it is behind us: a
  // past day the paper settler never seated (a held station's refused
  // basket) has no settlement row and would otherwise stay open forever.
  const today = new Date().toISOString().slice(0,10);
  // A basket every leg of which the station bar refused never reached the
  // exchange: no fill, no cost, no payout.  Six of the ten lines on the
  // book this morning were these, and they crowded out the four that
  // bought something (user ruling, 2026-09-06: do not list barred
  // stations).  Dropped from the listing, counted on the date line -- a
  // silently shorter list reads as "this is everything".
  const barred = (g)=> g.legs.length > 0
    && g.legs.every(l=>String(l.reason||"")==="live_station_barred");
  // One line per station-day (user ruling, 2026-09-14: 把同一站点的合并在一起).
  // A basket that never reached the exchange is decided again next cycle
  // and mints a fresh one, so the disarmed morning of 2026-09-12 printed
  // ZBAA four times and ZGSZ three, each with a verdict of its own.  The
  // station now wears one verdict over all its baskets, and its baskets sit
  // under it.  Baskets that bought nothing and repeat one another's legs
  // fold into one line with a count; a basket that filled anything is never
  // folded, because its fills are money.
  const foldKey = (g)=> g.legs.map(l=>[l.bucket, l.state, l.reason||""].join("|")).join(",")
    + "#" + String((planOf(g)||{}).skew||"");
  const clock = (t)=> { const m = String(t||"").match(/T(\d{2}:\d{2})/); return m ? m[1]+"Z" : ""; };
  const rows = dates.map(day=>{
    const all = byDate.get(day).slice().sort((a,b)=>
      String(a.station_id).localeCompare(String(b.station_id))
      || String(a.latest).localeCompare(String(b.latest)));
    const hidden = all.filter(barred);
    const gs = all.filter(g=>!barred(g));
    const daySettled = gs.length>0 && gs.every(g=>truthOf(g));
    const folded = daySettled || (day < today);
    const stations = [];
    const byStation = new Map();
    for (const g of gs){
      const key = g.station_id || "?";
      if(!byStation.has(key)){ byStation.set(key, []); stations.push(key); }
      byStation.get(key).push(g);
    }
    let legsN=0, filledN=0, cost=0, payout=0, hitN=0, settledN=0;
    const stationLines = stations.map(station=>{
      const baskets = byStation.get(station);
      const truth = baskets.map(truthOf).find(Boolean) || "";
      let sLegs=0, sFilled=0, sCost=0, sPay=0;
      for (const g of baskets){
        const gFilled = g.legs.filter(l=>l.state==="filled");
        sLegs += g.legs.length; sFilled += gFilled.length;
        sCost += gFilled.reduce((a,l)=>a+(l.requested_notional_usdc||0),0);
        if(truth) sPay += gFilled.reduce((a,l)=>a+(String(l.bucket)===truth?(l.requested_size||0):0),0);
      }
      legsN += sLegs; filledN += sFilled; cost += sCost; payout += sPay;
      const covered = !!truth && baskets.some(g=>g.legs.some(l=>String(l.bucket)===truth));
      const src = baskets.map(truthSrc).find(Boolean)==="truth_lane"
        ? "　真值取自真值车道（这个站日纸面账本没坐过，没有它的结算行），按当日自己的桶阶梯落桶"
        : "";
      // The verdict is the basket's (user ruling, 2026-09-14: 命中不管成不成交，
      // 都要显示是否篮子命中真值): 命中 when the settled bucket is one of the
      // buckets this station-day's baskets asked for, 未命中 when it is not,
      // filled or not.  What the fills did is a second word beside it --
      // 持仓命中, 持仓未中, 未成交 -- because the two part company exactly where
      // money is lost: a window that held the truth and a rung that did not
      // fill.  Until that morning one word did both jobs and called every
      // disarmed basket 未命中 (2026-09-13: six stations, nothing filled, five
      // windows holding the truth).  Words beside colours (user ruling,
      // 2026-09-11): a colour says these lines differ, not what any of them is.
      const verdict = !truth
        ? ["amber", "未结算", "这个站日的真值还没到"]
        : covered
          ? ["green", "命中", "篮子挂出的桶里有结算桶 " + truth + src]
          : ["red", "未命中", "篮子挂出的桶里没有结算桶 " + truth + src];
      if(truth){ settledN += 1; if(covered) hitN += 1; }
      const fill = !truth ? ""
        : `<span class="pill ${!sFilled ? "" : sPay>0 ? "green" : "red"}" style="font-size:10px"
            title="${!sFilled ? "一条腿都没成交" : sPay>0 ? "成交的腿里有结算桶" : "成交的腿都不在结算桶"}">${
            !sFilled ? "未成交" : sPay>0 ? "持仓命中" : "持仓未中"}</span>`;
      const money = !truth
        ? `<span class="muted mono" style="font-size:11px">投入 ${sCost.toFixed(2)} U</span>`
        : (sFilled
            ? `<span class="mono ${sPay-sCost>=0?"up":"down"}" style="font-size:11px">${(sPay-sCost>=0?"+":"")+(sPay-sCost).toFixed(2)} U</span>
               <span class="muted mono" style="font-size:11px">投入 ${sCost.toFixed(2)} · 赔付 ${sPay.toFixed(2)}</span>`
            : "")
          + `<span class="muted mono" style="font-size:11px">落在 ${h(truth)}</span>`;

      const lines = [];
      const foldAt = new Map();
      for (const g of baskets){
        const key = g.legs.some(l=>l.state==="filled") ? null : foldKey(g);
        if(key!=null && foldAt.has(key)){ lines[foldAt.get(key)].push(g); continue; }
        if(key!=null) foldAt.set(key, lines.length);
        lines.push([g]);
      }
      const legsOf = (items)=>{
        const g = items[items.length-1];
        // A basket whose centre it does not itself hold has no green rung,
        // and an unmarked line is indistinguishable from a line whose plan
        // is missing.  Two of the fifty-three baskets on the book are this:
        // gap fills and top-ups that bought a shoulder without the centre.
        // Say which rung is missing rather than leaving the reader to guess
        // whether the mark is broken.
        const mid = centerOf(g);
        const offCenter = mid && !g.legs.some(l=>String(l.bucket)===mid)
          ? `<span class="muted mono" style="font-size:11px"
              title="中心桶取自该决策自己的计划">中心 ${h(mid)} 不在本篮</span>`
          : "";
        const count = items.length > 1
          ? `<span class="pill" style="font-size:10px" title="${h(items.length + " 篮同一窗口、都没成交，显示最新一篮的价："
              + items.map(x=>clock(x.latest)+" "+x.decision_id).join("；"))}">×${items.length} 篮</span>`
          : "";
        // Several different baskets under one station each say whether
        // they held the truth, since the station's word is their union.
        const own = truth && lines.length > 1
          ? `<span class="mono ${g.legs.some(l=>String(l.bucket)===truth)?"up":"down"}" style="font-size:11px">${
              g.legs.some(l=>String(l.bucket)===truth) ? "含" : "不含"}真值</span>`
          : "";
        return `${skewChip(g)}<span>${g.legs.map(l=>legChip(l, truth, mid)).join("")}</span>${offCenter}${count}${own}`;
      };
      const head = `<span class="pill red" style="font-size:10px">LIVE</span>
        <b class="mono pill ${verdict[0]}" title="${h(verdict[2])} · ${
          h(baskets.map(g=>g.decision_id).join("、"))}">${stationLink(station, day)} ${verdict[1]}</b>${fill}
        <span class="muted" style="font-size:11px">${baskets.length>1 ? baskets.length+" 篮 · " : ""}${
          sLegs} 腿 · ${sFilled} 成交</span>`;
      const row = `display:flex;flex-wrap:wrap;align-items:center;gap:6px`;
      if(lines.length === 1) return `<div style="${row};padding:5px 0;border-top:1px dashed var(--line,#eee)">
        ${head}${legsOf(lines[0])}${money}</div>`;
      return `<div style="padding:5px 0;border-top:1px dashed var(--line,#eee)">
        <div style="${row}">${head}${money}</div>
        ${lines.map(items=>`<div style="${row};margin:3px 0 0 18px">
          <span class="muted mono" style="font-size:11px">${h(clock(items[items.length-1].latest))}</span>
          ${legsOf(items)}</div>`).join("")}</div>`;
    }).join("");
    const pnl = daySettled ? payout - cost : null;
    const barredNote = hidden.length
      ? ` · <span class="muted" title="${h(hidden.map(g=>g.station_id).join("、"))}"
          >禁入 ${hidden.length} 篮未列</span>`
      : "";
    const head = `${h(day)} · ${stations.length} 站 · ${gs.length} 篮${
        settledN ? ` · 篮子命中 ${hitN}/${settledN} 站` : ""} · ${legsN} 腿 · ${filledN} 成交 · 投入 ${cost.toFixed(2)} U` + (pnl==null
        ? ` · <span class="muted">未结算</span>`
        : ` · <span class="${pnl>=0?"up":"down"}">${(pnl>=0?"+":"")+pnl.toFixed(2)} U</span> · 已结算`)
      + barredNote;
    // A date whose every basket was barred still gets a line: the day had
    // intents, and a missing date answers a different question than an
    // empty one.
    const body = gs.length ? stationLines
      : `<div class="muted" style="font-size:12px;padding:5px 0">
         这一天铸出的 ${hidden.length} 篮全部被站级禁入挡在交易所之外，未列出。</div>`;
    return `<details ${folded?"":"open"} style="margin-top:6px">
      <summary class="mono" style="cursor:pointer;font-size:12.5px">${head}</summary>
      <div style="margin-left:4px">${body}</div></details>`;
  }).join("");
  const moreGroups = "";
  // Everything above is counted off our own tables.  This one row is not:
  // it is what the exchange says is resting on the account right now.  On
  // 2026-08-31 an order this machine placed and then lost track of rested
  // for eighteen minutes; it appeared in no table here, and only asking the
  // account found it.  So the account gets its own row, and a row that
  // cannot be read says so rather than rendering a reassuring zero.
  const O2 = L.open_orders || {};
  const openRows = (O2.orders||[]).map(o=>`<tr>
    <td><span class="pill red" style="font-size:10px">挂单中</span></td>
    <td class="mono">${o.outcome||"—"}</td><td>${o.side||""}</td>
    <td class="mono">${o.price??"—"}</td>
    <td class="mono">${(o.size_matched==null?"?":shares(o.size_matched))
      +"/"+(o.original_size==null?"?":shares(o.original_size))}</td>
    <td class="mono" title="${o.id}">${String(o.id||"").slice(0,14)}…</td>
    <td class="mono">${o.created_at??""}</td></tr>`).join("");
  // Three ways the read fails, and they are three different sentences.
  // "Nothing answered" and "the gateway answered and said no" send an
  // operator to different places; printing both as 网关无响应 sent them to
  // the wrong one twice on 2026-09-10, while a live order rested.
  const O2WHY = {
    unreachable: "网关无响应",
    refused: "网关答了，但这次查询失败",
    incomplete: "清单没枚举完，不是账户的全部",
  }[O2.reason] || "读取失败";
  const openCard = !O2.available
    ? `<div class="note" style="margin-top:9px">⚠️ <b>账户挂单读不到</b>
       <span class="muted">（${h(O2WHY)}：<span class="mono">${h(O2.error||"unknown")}</span>
       ，问于 <span class="mono">${h(O2.asked_at||"?")}</span>）——
       这一行问的是交易所，读不到就不是零。</span></div>`
    : O2.count === 0
      ? `<div class="muted" style="font-size:12px;margin-top:9px">✅ 交易所侧
         <b>账户零挂单</b>（问于 <span class="mono">${h(O2.asked_at||"?")}</span>）</div>`
      : `<div style="margin-top:9px"><b class="mono" style="color:var(--red)">
         交易所侧：账户 ${O2.count} 笔挂单</b>
         <span class="muted mono" style="font-size:11px">问于 ${h(O2.asked_at||"?")}</span>
         <span class="muted" style="font-size:12px">—— 逐笔核对；若某笔不在上面的意向表里，
         它就是本机丢了记录的孤儿单，撤掉它。</span>
         <div style="overflow-x:auto"><table><thead><tr><th></th><th>桶</th><th>侧</th>
         <th>价</th><th>成交/总</th><th>order id</th><th>挂单时刻</th></tr></thead>
         <tbody>${openRows}</tbody></table></div></div>`;
  // Counts here, money in the board above (2026-09-16): the PnL and ROI
  // tiles were the second print of what the data board's 总账 already says.
  return `<h3 style="margin-top:22px;font-size:15px">订单数量
    <span class="pill ${liveMode&&!L.submission_paused?"red":""}"
      style="font-size:11px">${state}</span></h3>
  <div class="card">
    <div class="grid g3">
      ${tile("实盘意向", (O.intent_count||0)+" 笔",
        "计划名义 "+u(O.notional_usdc)+(O.listed_count&&O.listed_count<O.intent_count
          ? " · 下面按篮列出最近 "+O.listed_count+" 笔（新的 200 笔，外加每个成交过的篮子）"
          : ""), O.intent_count?"":"muted")}
      ${tile("走到交易所", (X.events||0)+" 回执",
        "已提交 "+(O.submitted_count||0)+" · 受理 "+(O.accepted_count||0)+
        " · 对账 "+(X.reconciliations||0)+" 行", (X.events||0)?"":"muted")}
      ${tile("已成交", (P.filled_legs||0)+"／"+((P.filled_legs||0)+(P.pending_legs||0))+" 腿",
        "成交名义 "+u(X.filled_notional_usdc||0)+" · "+(P.station_days||0)+" 个结算站日"
          +((P.pending_legs||0)?" · "+P.pending_legs+" 腿待结算":""),
        (P.filled_legs||0)?"":"muted")}
    </div>
    ${openCard}
    <details class="note"><summary>实盘盈亏是怎么算的 · 声称从哪来 · 本金是什么</summary>
      <div class="muted">钱<b>全部来自实盘成交列</b>：成本＝对账表报回来的成交名义
      <span class="mono">filled_notional_usdc</span>，赔付＝真值落在该腿桶里时每股 1 USDC、
      否则 0。结算桶标签有<b>两个来源，按先后</b>：纸面账本坐过的站日，直接用生产结算器写下的那一行；
      <b>它没坐过的站日</b>，用真值车道当日成立的那版真值配上该站日自己的桶阶梯，
      调<b>生产结算器本人的函数</b>落桶——各站档位规则一个字都不在这里重写。
      借来的判定和自己推的判定在篮子行上分开标注。<br>
      成交了但所在站日还没结算的腿计入<b>待结算</b>，<b>不当亏损算</b>。<br>
      <b>声称</b>＝下单那个决策绑定的概率包（按内容哈希精确取回）里该桶的概率；
      一笔＝一个站日一个阶段里成交过的腿，腿份数不等时份数单位取概率加权份数，
      保证「投入×(声称÷成本−1)」正好等于这些腿声称能赚的钱。<br>
      <b>本金</b>＝预算旋钮当前值（${h(String((D.live_money||{}).capital_usdc ?? "—"))} U，读自
      <span class="mono">${h((D.live_money||{}).capital_source||"—")}</span>）：账户没有历史余额记录，
      预算更大时下的单会被读成更高的回报率。<br>
      当前两个闸的读数：代码执行模式 <span class="mono">${h(L.execution_mode||"—")}</span>、
      合同 <span class="mono">${h(C.execution_mode||"—")}</span>
      （<span class="mono">live_submit_authorized=${C.live_submit_authorized===true?"true":"false"}</span>、
      <span class="mono">calls_order_gateway=${C.calls_order_gateway===true?"true":"false"}</span>），
      读自 <span class="mono">${h(C.runtime_config||"—")}</span>。
      <b>两个都为真才可能铸出实盘行</b>，都是每次渲染现读。</div>
    </details>
    ${(X.blocking||[]).length?`<div class="muted" style="margin-top:8px;font-size:11.5px">
      对账阻断：${(X.blocking||[]).map(b=>h(b.reason)+" ×"+b.count).join(" · ")}</div>`:""}
  </div>
  <h3 style="margin-top:22px;font-size:15px">站明细 · 逐日逐站的实盘篮子</h3>
  <div class="card">
    ${rows?`<div>${rows}</div>${moreGroups}`
      :`<div class="muted">尚无实盘意向。第一条出现时此表自动列出。</div>`}
  </div>`;
}


// The live intent book, and only it (user ruling, 2026-08-31): paper
// intents are a different book and were being read as this one's rows.
// With no live intent yet the panel says so in one line rather than
// rendering three empty tables -- but it does not disappear, because a
// missing panel and an empty one answer different questions.
function orderBookCard(B){
  const paper = B.paper_excluded||0;
  const foot = `<details class="note"><summary>这里只数实盘 · 纸面意向为什么不算在内</summary>
      <div class="muted">本板块每一个数都过
      <span class="mono">live_submit_authorized=1</span> 这一道筛——
      那一位由 runtime 合同给，只有它为 1 的意向才会真的交到交易所。<br>
      账本里另有 <b>${paper}</b> 笔 <span class="mono">live_submit_authorized=0</span> 的纸面意向，
      <b>不计入上面任何一个数</b>：它们跑的是同一套选桶与定规模，但走到交易所边界就停，
      和实盘是两本账，并排放进同一张表只会被眼睛加到一起。
      纸面账本自 2026-09-16 起只做规则对照，不在本页出成绩。</div>
    </details>`;
  if(!B.total) return `<div class="card" style="margin-top:12px">
    <div class="muted">实盘意向簿</div>
    <div style="margin:6px 0">还没有实盘意向。<span class="muted">第一笔铸出后，
      这里按<b>状态 / 事件日 / 站</b>三个维度分列笔数、名义与均价。</span></div>
    ${foot}</div>`;
  const u = (v,d=2)=>v==null?"—":v.toFixed(d)+" U";
  const share = (n)=>B.total?((100*n/B.total).toFixed(1)+"%"):"—";
  const tile=(lab,val,sub2,klass)=>`<div class="card">
    <div class="muted">${lab}</div>
    <div class="big ${klass||""}">${val}</div>
    <div class="muted" style="font-size:11.5px">${sub2}</div></div>`;
  const stateRows=(B.by_state||[]).map(r=>`<tr>
    <td class="mono">${h(r.state)}</td>
    <td class="mono">${r.count}</td>
    <td class="mono">${share(r.count)}</td>
    <td class="mono">${(r.notional_usdc||0).toFixed(2)}</td></tr>`).join("");
  const dayRows=(B.by_day||[]).map(r=>`<tr>
    <td class="mono">${h(r.event_date)}</td>
    <td class="mono">${r.count}</td>
    <td class="mono">${(r.notional_usdc||0).toFixed(2)}</td>
    <td class="mono">${r.stations}</td></tr>`).join("");
  const stationRows=(B.by_station||[]).map(r=>`<tr>
    <td class="mono">${h(r.station_id)}</td>
    <td class="mono">${r.count}</td>
    <td class="mono">${(r.notional_usdc||0).toFixed(2)}</td>
    <td class="mono">${r.days}</td>
    <td class="mono">${(r.mean_limit_price||0).toFixed(3)}</td></tr>`).join("");
  return `<div class="card" style="margin-top:12px">
    <div class="muted">实盘意向簿 · 只数 <span class="mono">live_submit_authorized=1</span> 的行</div>
    <div class="grid g4" style="margin-top:8px">
      ${tile("实盘意向", B.total+" 笔", "均单 "+u(B.mean_notional_usdc,3))}
      ${tile("计划名义合计", u(B.notional_usdc), "全为 BUY/YES")}
      ${tile("平均限价", (B.mean_limit_price||0).toFixed(4), "按笔简单平均")}
      ${tile("覆盖", B.stations+" 站",
        B.decisions+" 个决策 · "+B.cycles+" 个周期<br>"+
        h(B.first_event_date)+" → "+h(B.last_event_date))}
    </div>
    <div class="grid g3" style="margin-top:12px">
      <div><div class="muted" style="margin-bottom:4px">按状态</div>
        <div class="scroll"><table><thead><tr><th>状态</th><th>笔</th>
          <th>占比</th><th>名义u</th></tr></thead>
          <tbody>${stateRows}</tbody></table></div></div>
      <div><div class="muted" style="margin-bottom:4px">按事件日（近 14 个有单的日子）</div>
        <div class="scroll" style="max-height:300px"><table><thead><tr><th>事件日</th>
          <th>笔</th><th>名义u</th><th>站</th></tr></thead>
          <tbody>${dayRows}</tbody></table></div></div>
      <div><div class="muted" style="margin-bottom:4px">按站（名义前 12）</div>
        <div class="scroll" style="max-height:300px"><table><thead><tr><th>站</th>
          <th>笔</th><th>名义u</th><th>日</th><th>均价</th></tr></thead>
          <tbody>${stationRows}</tbody></table></div></div>
    </div>
    ${foot}
  </div>`;
}


// The money board (user ruling, 2026-09-13: 把「组合风险指标」、入场的九个数、
// 钱 · 这本账值多少、实盘订单、实盘意向簿融合在一个大容器内，做成一个数据看板，
// 分类展示).  Five readings of what the book is worth sat in three places:
// the 钱 lamp row in the head, two sections under it, and two cards inside
// 站点实盘上线闸门 -- where the live P&L was read under a paragraph about the
// arming flag.  They are one container now, in two categories: the paper
// book every rule is measured on, and the live account that actually
// spends.  Each category shows one block at a time and remembers which
// across reloads; each block keeps its own guard, so one that throws does
// not blank the other four.  The paper category's three blocks became one
// board the same evening (paperBoard), because they printed the same
// readings; a category with one block draws no tab strip.
//
// One category since 2026-09-16 (user ruling: 纸面账本的数据同样改为实盘数据，
// 把「实盘订单 · 数量 / ROI / PnL」融合在一起，上面是数据看板，下方是站明细;
// 账本从今天起只当规则对比器).  The paper category left with the ruling; its
// four blocks now read the live book and sit on top of the live order card,
// whose own PnL and ROI tiles went because the board above already prints
// them once.
const MONEY_BOARD = [
  {id:"live", label:"实盘", tone:"#ff7b72",
   note:"只数 live_submit_authorized=1 的行：真的交到交易所的单。纸面账本只当规则对比器，不在这里",
   panes:[
     ["orders", "实盘 · 数据看板与站明细",
      () => safe(() => moneyDataBoard(), "实盘数据看板") + liveMoney((L) => liveOrderCard(L)),
      "sec-live-orders"],
     ["book", "实盘意向簿",
      () => liveMoney((L) => orderBookCard((L.live_orders||{}).book||{})),
      "sec-live-book"],
     // Charter 146: the tiebreak lane's own page -- its switch and stake,
     // every first decision it scored, and its trades on their own.
     ["tiebreak", "秤砣线 · 宪章 146",
      () => tiebreakLane(),
      "sec-tiebreak"]]},
];

// Both live blocks read the live-readiness payload, which is absent when the
// collector could not build it.  Absent is said, not drawn as empty tiles.
function liveMoney(render){
  const L = D.live_readiness || {};
  if(!L.available) return `<div class="muted">实盘读数这一轮没有生成
    （live_readiness 不可用），实盘订单与意向簿无从读起。</div>`;
  return render(L);
}

// 「秤砣线」(charter 146): the lane's switch and stake, the first decisions it
// scored, and its trades -- on their own page, never mixed into the basket
// book's numbers (user ruling 2026-09-18: 单独的统计).
function tiebreakLane(){
  const T=D.tiebreak_lane||{};
  if(!T.available) return `<div class="muted">秤砣线读数这一轮没有生成。</div>`;
  const S=T.settings||{}, E=T.evaluations||{}, R=T.trades||{}, A=R.acceptance||{};
  const usd=v=>v==null?"—":((v<0?"−":"")+"$"+Math.abs(+v).toFixed(2));
  const pc=v=>v==null?"—":(Math.round(+v*1000)/10)+"%";
  const sp=v=>v==null?"—":((v>=0?"+":"−")+Math.abs(Math.round(+v*1000)/10)+"点");
  const n3=v=>v==null?"—":(+v).toFixed(3);
  const lanes=T.lanes||{};
  const laneWord=(k,zh)=>`<span class="pill ${lanes[k]?"green":"amber"}">${zh}：${lanes[k]?"运行":"暂停"}</span>`;
  const tile=(label,val,sub)=>`<div class="card"><div class="muted">${label}</div>
    <div class="mono" style="font-size:1.35em">${val}</div>${sub?`<div class="muted">${sub}</div>`:""}</div>`;
  const cond=(ok,text)=>`<span class="pill ${ok?"green":"amber"}">${text}</span>`;
  const settings=`<div class="card"><div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
      <b>秤砣线</b>
      <span class="pill ${S.enabled?"green":"red"}">${S.enabled?"运行中":"已关闭"}</span>
      <span>每笔 <b class="mono">${usd(S.stake_usdc)}</b>（顶部「秤砣每笔」旋钮可调）</span>
      <span class="muted">市场给模型桶 ≥ ${n3(S.min_market_share_on_model_mode)}，首决策 ${S.first_decision_fresh_seconds==null?"—":Math.round(S.first_decision_fresh_seconds/60)} 分钟内</span>
      <span style="flex:1"></span>
      ${laneWord("basket","篮子买单")} ${laneWord("certain_win_recycle","0.998 回收")} ${laneWord("mis_bought_leg_exit","144 卖回")}
    </div>
    <div class="muted" style="margin-top:6px">规则：每个阶段的首个决策，模型众数桶与市场众数桶相邻、市场归一化中价给模型桶不低于门槛，就按卖一梯走到预算所及的最深一档 FOK 买模型桶，一个站日只买一次。读数依据：2,255 首决策 / 33 日里唯一模型胜市场的格（n=42，+17.8% 于 20U/笔，日聚类 90% [−7.3%, +41.8%]）——假设，不是结论。</div></div>`;
  const evalTiles=E.available?`<div class="grid g3" style="margin-top:8px">
    ${tile("评估过的阶段首决策",E.total,`${E.event_days} 个事件日 · 最近 ${h((E.last_at_utc||"").slice(0,16).replace("T"," "))}Z`)}
    ${tile("触发（四条件成立）",E.fired,`${E.total?pc(E.fired/E.total):"—"} 的首决策`)}
    ${tile("发单 / 成交 / 已结算",`${R.sent==null?"—":R.sent} / ${R.filled==null?"—":R.filled} / ${R.settled==null?"—":R.settled}`,`在途 ${R.open==null?"—":R.open}`)}
  </div>`:`<div class="muted" style="margin-top:8px">评估表尚未建立（常驻重启后第一轮才写）。</div>`;
  const money=R.available?`<div class="grid g3" style="margin-top:8px">
    ${tile("投入 → 赔付",`${usd(R.stake_usdc)} → ${usd(R.payout_usdc)}`,`已结算 ${R.settled} 笔`)}
    ${tile("盈亏 / ROI",`${usd(R.pnl_usdc)} / ${pc(R.roi)}`,`命中 ${R.hits==null?"—":R.hits}${R.settled?` / ${R.settled}`:""}`)}
    ${tile("实现 − 付出的卖一",`${sp(R.realised_minus_ask)}`,`命中率 ${pc(R.hit_rate)} 对 均价 ${n3(R.avg_ask)}${R.realised_minus_ask_ci90?` · 90% [${sp(R.realised_minus_ask_ci90[0])}, ${sp(R.realised_minus_ask_ci90[1])}]`:" · 样本不足 5 笔或 3 日，无区间"}`)}
  </div>
  <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
    ${cond(A.lower_bound_positive,"① 实现−卖一 日聚类下界 > 0")}
    ${cond(A.halves_same_sign,"② 前后半段同号"+(R.halves&&R.halves.first!=null?`（${sp(R.halves.first)} / ${sp(R.halves.second)}）`:""))}
    ${cond((A.event_days||0)>=(A.event_days_needed||30),`③ 事件日 ${A.event_days||0} / ${A.event_days_needed||30}`)}
    <span class="muted">三条同时满足才算过；不过则关掉本线、恢复其余车道（同一文件）。首次复核 09-25。</span>
  </div>`:"";
  const refusals=(E.refusals||[]).map(r=>`<tr><td class="mono">${h(r.refusal)}</td><td class="mono">${r.count}</td><td class="mono">${r.fired}</td></tr>`).join("");
  const refTable=E.available&&refusals?`<details style="margin-top:8px"><summary>为什么没买（评估结果分布，${E.total} 条）</summary>
    <table><thead><tr><th>结果 / 未发单原因</th><th>条数</th><th>其中触发</th></tr></thead><tbody>${refusals}</tbody></table>
    <div class="muted">rule_not_met:* 是四条件不成立；first_decision_stale 是上线时已过时的首决策；station_* / account_* / ask_book_* / market_* 是触发后被各闸挡下；空白（sent）是已发单。</div></details>`:"";
  const rows=(R.rows||[]).map(t=>`<tr>
    <td>${h(t.station_id)}</td><td class="mono">${h(t.event_date)}</td><td>${h(t.stage||"")}</td><td>${h(t.bucket)}</td>
    <td class="mono">${n3(t.limit_price)}</td><td class="mono">${t.requested_size==null?"—":(+t.requested_size).toFixed(2)}</td>
    <td class="mono">${(+t.filled_size).toFixed(2)}</td><td class="mono">${usd(t.filled_notional_usdc)}</td>
    <td>${h(t.state||"")}</td><td>${t.settled_bucket?h(t.settled_bucket):"<span class='muted'>未结算</span>"}</td>
    <td>${t.hit==null?"—":(t.hit?"<span class='pill green'>中</span>":"<span class='pill red'>未中</span>")}</td>
    <td class="mono">${t.pnl_usdc==null?"—":usd(t.pnl_usdc)}</td></tr>`).join("");
  const tradeTable=`<details open style="margin-top:8px"><summary>逐笔（${(R.rows||[]).length} 笔，新的在上）</summary>
    ${rows?`<table><thead><tr><th>站</th><th>事件日</th><th>阶段</th><th>桶</th><th>限价</th><th>下单股</th><th>成交股</th><th>成交额</th><th>状态</th><th>结算桶</th><th>命中</th><th>盈亏</th></tr></thead><tbody>${rows}</tbody></table>`:`<div class="muted">还没有一笔。</div>`}</details>`;
  const firedRows=(E.fired_rows||[]).map(r=>`<tr><td>${h(r.station_id)}</td><td class="mono">${h(r.event_date)}</td><td>${h(r.stage)}</td>
    <td>${h(r.model_mode)} ← ${h(r.market_mode)}</td><td class="mono">${n3(r.market_share_on_model_mode)}</td><td class="mono">${n3(r.model_mode_share)}</td>
    <td class="mono">${r.refusal?h(r.refusal):"<span class='pill green'>已发单</span>"}</td><td class="mono">${h((r.evaluated_at_utc||"").slice(5,16).replace("T"," "))}</td></tr>`).join("");
  const firedTable=E.available?`<details style="margin-top:8px"><summary>触发过的首决策（${(E.fired_rows||[]).length} 条，含被闸挡下的）</summary>
    ${firedRows?`<table><thead><tr><th>站</th><th>事件日</th><th>阶段</th><th>模型桶 ← 市场桶</th><th>市场给模型桶</th><th>模型自报</th><th>结果</th><th>评估于</th></tr></thead><tbody>${firedRows}</tbody></table>`:`<div class="muted">还没有触发过。</div>`}</details>`:"";
  return settings+evalTiles+money+refTable+tradeTable+firedTable;
}

function moneyBoard(){
  const cats = MONEY_BOARD.map(c => {
    const nav = c.panes.map(([key, label], i) =>
      `<button type="button" data-day="${key}"${i ? "" : ` data-active="1"`}>${
        h(label)}</button>`).join("");
    const panes = c.panes.map(([key, label, fn, id], i) =>
      `<div class="mb-pane" data-daypane="${key}" id="${id}"${i ? " hidden" : ""}>${
        safe(fn, label)}</div>`).join("");
    return `<div class="card mb-cat" data-daytabs="money-${c.id}" data-remember="1"
      style="--tone:${c.tone}">
      <div class="grouptitle">${h(c.label)}<span class="muted">${h(c.note)}</span></div>
      ${c.panes.length > 1
        ? `<div class="daytabs">${nav}<button type="button" data-day="all">全部</button></div>`
        : ""}
      ${panes}</div>`;
  }).join("");
  return `<h2>资金看板：实盘的盈亏和风险</h2>
  <p class="sub">全部读<b>实盘成交</b>。第一页上面是数据看板（总账、对比、风险、曲线，每个数只出现一次），
  下面是订单数量与站明细；第二页是实盘意向簿。纸面账本从 2026-09-16 起只当规则对比器，不在这里。</p>
  ${cats}`;
}


// The three-state comparison is a portfolio reading, not a control: it
// says in money what the bars cost or saved, on the same book the strip at
// the top of the page counts.  It moved out of the live control section
// and onto the main board (user ruling, 2026-08-31) -- it answers "how is
// the account doing", not "may this book go live".  The prose that
// justifies the split folds: it is read once, and it was pushing the four
// numbers it explains below the fold.
// The three live knobs (user ruling, 2026-09-11: top right of the nav, not
// in the page).  They are a CONTROL, not a reading -- the one thing on this
// page that changes what production does next -- and a control belongs
// where it is always reachable rather than five thousand pixels down inside
// the section that explains it.  Same ids as before, because the control
// server's own script paints into them and enables the buttons; only the
// layout moved.
// The knobs, their names, and how each one reads back out of the control
// server's answer.  One table, because the stepper that draws a knob, the
// paint that fills it and the click that moves it all have to agree on
// which field is which -- the cap's "unset means the default" rule used to
// be written out twice.
const LC_KNOBS = {
  // Charter 141 (2026-09-17): four capital dials.  Kelly is the share of full
  // Kelly a basket stakes; the other three are shares of the real account,
  // shown as percentages.  ``decimals`` is how finely a step may land.
  kelly:  {id:"lc-kelly", name:"凯利", path:"/api/kelly-fraction",
    field:"kelly_fraction", spec:s=>s.kelly, decimals:2,
    read:s=>s.kelly.kelly_fraction,
    fmt:v=>Number(v).toFixed(2)},
  exposure: {id:"lc-exposure", name:"总敞口", path:"/api/exposure",
    field:"portfolio_risk_fraction", spec:s=>s.exposure, decimals:2, pct:true,
    read:s=>s.exposure.portfolio_risk_fraction,
    fmt:v=>`${Math.round(Number(v)*100)}%`},
  sizing: {id:"lc-sizing", name:"定注", path:"/api/station-sizing",
    field:"station_day_sizing_fraction", spec:s=>s.station_sizing, decimals:3, pct:true,
    read:s=>s.station_sizing.station_day_sizing_fraction,
    fmt:v=>`${Math.round(Number(v)*1000)/10}%`},
  ceiling: {id:"lc-ceiling", name:"上限", path:"/api/station-ceiling",
    field:"station_day_ceiling_fraction", spec:s=>s.station_ceiling, decimals:3, pct:true,
    read:s=>s.station_ceiling.station_day_ceiling_fraction,
    fmt:v=>`${Math.round(Number(v)*1000)/10}%`},
  // Whole multiples of 5 (operator ruling, 2026-09-16; the server refuses
  // anything else).  A value still off the grid -- 114.29 from before the
  // ruling -- is shown as it is, and its first step lands on the grid.
  budget: {id:"lc-budget", name:"预算", path:"/api/budget", unit:" USDC",
    field:"target_remaining_budget_usdc", spec:s=>s.budget, grid:5,
    read:s=>s.budget.target_remaining_budget_usdc,
    fmt:v=>{const r=Math.round(Number(v)*100)/100;
      return Number.isInteger(r) ? String(r) : r.toFixed(2);}},
  // Charter 146: the tiebreak lane's per-trade stake (秤砣线每笔预算), whole
  // dollars on the board; the server accepts cents and bounds it.  Absent
  // from an older control server's answer: read as unset, never thrown on.
  tiebreak: {id:"lc-tiebreak", name:"秤砣每笔", path:"/api/tiebreak-stake", unit:" USDC",
    field:"stake_usdc", spec:s=>s.tiebreak_stake||{bounds:[1,100],step:1}, grid:1,
    read:s=>(s.tiebreak_stake||{}).stake_usdc,
    fmt:v=>{const r=Math.round(Number(v)*100)/100;
      return Number.isInteger(r) ? String(r) : r.toFixed(2);}},
};
// Where one click takes a knob.  On a grid, a step goes to the grid line
// that far away in its direction, so 114.29 +5 is 115 and −5 is 110, and
// +50 is 160 rather than an off-grid 164.29.
function lcNext(K, cur, delta){
  if(!K.grid){
    const scale = Math.pow(10, K.decimals == null ? 2 : K.decimals);
    return Math.round((cur + delta) * scale) / scale;
  }
  const g = K.grid, units = delta / g;
  const base = delta > 0 ? Math.floor(cur / g + 1e-9) : Math.ceil(cur / g - 1e-9);
  return Math.round((base + units) * g * 100) / 100;
}
function liveControlStrip(){
  const L = D.live_readiness || {};
  if(!L.available) return "";
  const b=(knob,delta,text,coarse)=>`<button type="button" disabled
    class="lc-w${coarse?" coarse":""}" data-knob="${knob}" data-delta="${delta}"
    aria-label="${LC_KNOBS[knob].name} ${delta>0?"+":"−"}${Math.abs(delta)}"
    onclick="lcAdjust('${knob}',${delta})">${text}</button>`;
  const knob=(key,title,buttons)=>`<span class="navctl-k" data-knob="${key}"
    title="${title}"><span class="navctl-l">${LC_KNOBS[key].name}</span>
    <span class="stp">${buttons}</span></span>`;
  return `<div class="navctl" id="lc-strip" data-lc="down" role="group"
    aria-label="实盘旋钮">
    <span class="navctl-s" id="lc-status"
      title="控制服务未连接——launchd 常驻 com.weather.live_control；拉起：launchctl kickstart -k gui/$(id -u)/com.weather.live_control"
      ><i></i><span id="lc-status-t">控制服务未连接</span></span>
    ${knob("kelly", "凯利系数 kelly_fraction（每个篮子押完整凯利的几分之几；宪章 141）",
      `${b("kelly",-0.05,"−")}<b id="lc-kelly">${
        L.kelly==null?"—":LC_KNOBS.kelly.fmt(L.kelly)}</b>${b("kelly",0.05,"+")}`)}
    ${knob("exposure", "总敞口上限 portfolio_risk_fraction（持仓市值+在挂买单 占 真实余额 的上限；宪章 141）",
      `${b("exposure",-0.05,"−")}<b id="lc-exposure">${
        L.exposure==null?"—":LC_KNOBS.exposure.fmt(L.exposure)}</b>${b("exposure",0.05,"+")}`)}
    ${knob("sizing", "单站日定注 station_day_sizing_fraction（凯利押注、各类候选方案最多按真实余额的这个比例定大小，整条链共用；宪章 141）",
      `${b("sizing",-0.005,"−")}<b id="lc-sizing">${
        L.station_sizing==null?"—":LC_KNOBS.sizing.fmt(L.station_sizing)}</b>${b("sizing",0.005,"+")}`)}
    ${knob("ceiling", "单站日上限 station_day_ceiling_fraction（抬到最小一手、补腿、下单最多花真实余额的这个比例，整条链共用；不能低于定注；宪章 141）",
      `${b("ceiling",-0.01,"−")}<b id="lc-ceiling">${
        L.station_ceiling==null?"—":LC_KNOBS.ceiling.fmt(L.station_ceiling)}</b>${b("ceiling",0.01,"+")}`)}
    ${knob("budget", "预算 target_remaining_budget_usdc（USDC，按 5 的整数倍）",
      `${b("budget",-50,"−50",true)}${b("budget",-5,"−")}<b id="lc-budget">${
        L.budget_usdc==null?"—":LC_KNOBS.budget.fmt(L.budget_usdc)}</b>${
        b("budget",5,"+")}${b("budget",50,"+50",true)}`)}
    ${knob("tiebreak", "秤砣线每笔预算 stake_usdc（USDC；每个站日至多一笔 FOK 买单花这么多；宪章 146）",
      `${b("tiebreak",-5,"−5",true)}${b("tiebreak",-1,"−")}<b id="lc-tiebreak">${
        L.tiebreak_stake==null?"—":LC_KNOBS.tiebreak.fmt(L.tiebreak_stake)}</b>${
        b("tiebreak",1,"+")}${b("tiebreak",5,"+5",true)}`)}
    <span class="navctl-k" id="lc-capital" title="宪章 141：按最新真实余额换算的每站日 定注 / 上限（美元）"><span class="stp"><b id="lc-capital-v">—</b></span></span>
  </div>`;
}

function liveReadiness(){
  const L = D.live_readiness || {};
  if(!L.available) return "";
  const allow = new Set(L.station_allowlist||[]);
  const barred = new Set(L.station_exclusions||[]);
  const waived = new Set(L.station_probation_waivers||[]);
  const heldSet = new Set(L.station_probation_holds||[]);
  // Probation of either kind gets the hourglass: a station kept out because
  // its settlement count has not got there yet reads the same to a trader as
  // one the operator is holding, and the row's switches say which it is.
  const waiting = new Set(L.station_probation||[]);
  const autoBar = new Set(L.auto_barred||[]);
  const GATE = {claim_honest:"声称诚实", centre_bias:"中心偏差",
    chains:"实盘站日", roi:"实盘ROI", truth_fresh:"真值新鲜"};
  // Three states, not two: on the allowlist, merely unselected, or barred.
  // An unselected station can still be added; a barred one is refused by
  // the control server, so it must not read as simply "not chosen".
  //
  // And each of the three is now a switch rather than a reading.  The bar
  // was editable only by hand in JSON until 2026-09-02 -- which is why it
  // moved four times in one day through a text editor and why one of those
  // days is recorded in the file as a same-day round trip -- while probation
  // had no control at all: an operator who wanted a station recorded but not
  // traded had the bar as the only tool, and the bar is a verdict.
  // The five-dimension read shares this row (user ruling, 2026-09-09).
  // Every gated station gets a line whether or not it is flagged: the old
  // separate table listed only stations with at least one flag, which meant
  // a clean station's evidence simply was not on the page.
  const FX = stationFailureIndex();
  const FLAG = {realization:"实现", claim:"声称", centre:"中心",
    basket_hit:"篮子命中", persistence:"持续"};
  const fnum = (v,d,suffix="") => v==null ? "—"
    : ((v>=0&&suffix==="C")?"+":"") + Number(v).toFixed(d) + suffix;
  const EDGE = 'style="border-left:1px solid var(--line)"';
  // The live book per station: what the exchange filled, settled on the
  // same resolver as the money card.  The gates, the five dimensions and
  // their seven columns read the same book (user rulings, 2026-09-16).
  // Pending legs stay out of the ratio.
  const LP = (L.live_orders||{}).pnl || {};
  // 逐站汇总（跨日） lives in this table since 2026-09-16 (user ruling): every
  // live intent a station minted -- refused by the send gate, posted and
  // unfilled, or filled -- its basket completeness, the seat production
  // compiled, and where the station sits on the globe.
  const SEL = D.live_selection || {};
  const SS = {};
  (SEL.station_stats||[]).forEach(x=>{ SS[x.station_id]=x; });
  const pc0 = v => v==null ? "—" : (100*v).toFixed(0)+"%";
  const n2 = (v,d=2) => v==null ? "—" : Number(v).toFixed(d);
  // Hit rates per station at 每日统计's grains, counted off the same
  // station-day verdicts (live_book.station_days): 篮子命中 over every settled
  // station-day, 持仓命中 over the ones that filled, and the 笔 rate beside
  // its claim, the 90% interval in the hover.
  const hfrac = (a,b) => b ? `${a}/${b} <span class="muted">${pc0(a/b)}</span>` : "—";
  // Sort key for a hit cell: the rate, and on a tie the larger sample sorts
  // higher (the nudge stays below the gap between any two distinct rates on
  // these denominators).
  const hsort = (a,b) => `data-sort="${b ? a/b + b*1e-9 : ""}"`;
  const HT = Object.values(SS).reduce((a,x)=>{
    a.sd+=x.settled_station_days||0; a.bh+=x.basket_hit_station_days||0;
    a.hd+=x.held_station_days||0; a.hh+=x.held_hit_station_days||0;
    a.k+=x.hit_count||0; a.n+=x.claimed_count||0;
    a.c+=(x.claimed_win_probability_mean||0)*(x.claimed_count||0);
    return a;
  }, {sd:0,bh:0,hd:0,hh:0,k:0,n:0,c:0});
  // Fleet basket completeness: how many buckets each live basket meant to
  // buy against how many it bought, e.g. posted 3 and bought 2.  The three
  // headline numbers only; the per-(posted, bought) table under them came
  // off the card (user ruling, 2026-09-17).
  const BC = SEL.basket_completeness || {};
  const completenessCard = BC.baskets ? `<div class="card" style="margin-top:12px">
    <div class="muted">篮子完整度（实盘，挂到交易所的篮子；一个站日的一个阶段算一篮）</div>
    <div style="display:flex;gap:18px;flex-wrap:wrap;align-items:baseline;margin-top:6px">
      <div><span class="big">${pc0(BC.complete_share)}</span>
        <span class="muted">完整篮 ${BC.complete}/${BC.baskets}（挂出的桶全部买到）</span></div>
      <div><span class="big">${pc0(BC.mean)}</span>
        <span class="muted">平均完整度（买到桶数÷挂出桶数）</span></div>
      <div><span class="big down">${BC.unfilled}</span>
        <span class="muted">篮一桶都没买到</span></div></div></div>` : "";
  // Group subtotals by climate band and coast, off the same station rows:
  // plain sums for money, hit and claim weighted by settled rows.
  const groups = new Map();
  Object.values(SS).forEach(x=>{
    const g = GEO[x.station_id]; if(!g) return;
    const k = bandOf(g[0])+"·"+(g[1]?"沿海":"内陆");
    const a = groups.get(k) || {n:0,intents:0,filled:0,set:0,wHit:0,wClaim:0,
      wSet:0,stake:0,real:0,baskets:0,complete:0,sd:0,bh:0,hd:0,hh:0};
    a.n++; a.intents+=x.intent_leg_total||0; a.filled+=x.filled_leg_total||0;
    a.set+=x.settled_count||0; a.stake+=x.staked_usdc||0; a.real+=x.realized_usdc||0;
    const c = x.completeness||{}; a.baskets+=c.baskets||0; a.complete+=c.complete||0;
    a.sd+=x.settled_station_days||0; a.bh+=x.basket_hit_station_days||0;
    a.hd+=x.held_station_days||0; a.hh+=x.held_hit_station_days||0;
    // Weighted by the rate's own denominator -- the 笔 whose claim is known
    // -- so the group's rate is its hits over its 笔, not a mean of means.
    if(x.hit_rate!=null && x.claimed_count){
      a.wHit+=x.hit_count;
      a.wClaim+=(x.claimed_win_probability_mean||0)*x.claimed_count;
      a.wSet+=x.claimed_count;
    }
    groups.set(k, a);
  });
  const groupRows = BAND_ORDER.flatMap(bd=>["沿海","内陆"].map(cl=>{
    const a = groups.get(bd+"·"+cl); if(!a) return "";
    const hit = a.wSet ? a.wHit/a.wSet : null;
    const claim = a.wSet ? a.wClaim/a.wSet : null;
    const cal = (hit!=null && claim) ? hit/claim : null;
    const roi = a.stake>0 ? a.real/a.stake : null;
    return `<tr><td>${h(bd)}</td><td>${cl}</td><td>${a.n}</td>
      <td class="mono">${a.intents}</td><td class="mono">${a.filled}</td>
      <td class="mono">${a.baskets?`${a.complete}/${a.baskets}`:"—"}</td>
      <td class="mono" ${hsort(a.bh, a.sd)}>${hfrac(a.bh, a.sd)}</td><td class="mono" ${hsort(a.hh, a.hd)}>${hfrac(a.hh, a.hd)}</td>
      <td class="mono">${a.set}</td><td>${pc0(hit)}</td><td>${pc0(claim)}</td>
      <td class="${cal==null?"":(cal>=1?"up":"down")}">${cal==null?"—":n2(cal)+"×"}</td>
      <td class="mono">${n2(a.stake)}</td>
      <td class="mono ${a.real>=0?"up":"down"}">${(a.real>=0?"+":"")+n2(a.real)}</td>
      <td class="${roi==null?"":(roi>=0?"up":"down")}">${roi==null?"—":(roi>=0?"+":"")+(100*roi).toFixed(1)+"%"}</td></tr>`;
  })).join("");
  const groupBlock = groupRows ? `<details class="note" style="margin-top:12px">
    <summary>按气候带 × 海陆分组小计（实盘）</summary>
    <div class="scroll"><table><thead><tr>
      <th>气候带</th><th>海陆</th><th>站数</th><th>意向腿</th><th>成交腿</th><th>完整篮</th>
      <th>篮子命中<br><span class="muted">站日</span></th><th>持仓命中<br><span class="muted">站日</span></th>
      <th>已结算笔</th><th>命中率<br><span class="muted">笔</span></th><th>声称</th><th>命中/声称</th>
      <th>投入</th><th>实得</th><th>ROI</th></tr></thead><tbody>${groupRows}</tbody></table></div>
    <div class="muted" style="margin-top:6px">禁入站也计入小计。南北半球季节相位相反，命中率别直接横比。</div>
    </details>` : "";
  const lpct = v => v==null ? "—" : (v>=0?"+":"")+(100*v).toFixed(1)+"%";
  const lsign = v => v==null ? "" : (v>=0?"up":"down");
  // One list of columns, grouped by what they measure (user ruling,
  // 2026-09-17: 同类的放在一起).  The header and every row are drawn from it,
  // so a column cannot move in one without the other -- the cells used to be
  // six hand-counted runs that had to line up with a separate header by eye.
  // Each ``td`` returns [attributes, content]; the first column of a group
  // gets the group's rule.
  const MISS = ['class="mono muted"', "—"];
  const fx = sid => FX ? FX.by[sid] : null;
  const COLUMN_GROUPS = [
    ["站点与开关", [
      {th:['', "站"], td:r=>['class="mono" style="white-space:nowrap"',
        `<span data-lc-state="${h(r.station)}">${allow.has(r.station)?"✔ ":(barred.has(r.station)?"⛔ ":(waiting.has(r.station)?"⏳ ":""))}</span>${h(r.station)}${r.station_label?` <span class="muted">${h(r.station_label)}</span>`:""}`]},
      {th:['title="三个开关都是人工裁定，点击即开启关闭，改动立刻写盘并记审计。禁入=不下单（记账照旧）；豁免=证据不够也上线；待观察=够格也先不下单。三者互斥。"', "闸门（人工）"],
        td:r=>['style="white-space:nowrap"', lcGateButtons(r.station, barred.has(r.station),
          waived.has(r.station), heldSet.has(r.station), autoBar.has(r.station))]},
    ]],
    ["判定", [
      {th:['', "过闸"], td:r=>[`class="mono ${r.score>=5?"up":(r.score>=4?"":"down")}"`, `${r.score}/5`]},
      {th:['', "未过项"], td:r=>['class="muted"',
        `${r.add_candidate?"☆建议加入 ":""}${r.release_candidate?"★建议放出 ":""}${Object.entries(r.gates).filter(([k,v])=>!v).map(([k])=>GATE[k]).join("、")||"✓"}`]},
      {fx:true, th:['title="实盘：五个维度里不行的项数，每个维度指认不同的一层"', "不行"],
        td:r=>{ const s=fx(r.station); return !s ? MISS
          : [`class="mono ${s.flag_count>=4?"down":""}"`, `<b>${s.flag_count}</b>/5`]; }},
      {fx:true, th:['title="实盘：实现、声称、中心、篮子命中四项见右边各列的说明；「持续」＝以全队已结算站日的中位日切成前后两段，两段的站日盈亏都为负"', "不行的维度"],
        td:r=>{ const s=fx(r.station); if(!s) return ['class="muted"', "—"];
          const bad = Object.keys(FLAG).filter(k=>(s.flags||{})[k]);
          return ['', bad.map(k=>`<span class="pill amber">${FLAG[k]}</span>`).join(" ")
            || '<span class="muted">✓</span>']; }},
    ]],
    ["下单与成交", [
      {th:['title="实盘订单：该站铸出的全部实盘意向腿，含被实盘门拒掉的"', "意向腿"],
        td:r=>{ const x=SS[r.station]; return x ? ['class="mono"', x.intent_leg_total] : MISS; }},
      {th:['title="实盘订单：被实盘门（禁入/总闸/待观察/网关）拒掉、从没到过交易所的腿"', "被拒"],
        td:r=>{ const x=SS[r.station]; return x ? [`class="mono ${x.refused_leg_total?"down":""}"`, x.refused_leg_total] : MISS; }},
      {th:['title="实盘订单：到过交易所的腿"', "到所"],
        td:r=>{ const x=SS[r.station]; return x ? ['class="mono"', x.reached_leg_total] : MISS; }},
      {th:['title="实盘订单：成交过的腿"', "成交腿"],
        td:r=>{ const x=SS[r.station]; return x ? ['class="mono"', x.filled_leg_total] : MISS; }},
      {th:['title="实盘订单：全部意向的请求名义金额(U)"', "意向名义"],
        td:r=>{ const x=SS[r.station]; return x ? ['class="mono"', n2(x.intent_notional_usdc,1)] : MISS; }},
      {th:['title="实盘订单：成交金额(U)"', "成交额"],
        td:r=>{ const x=SS[r.station]; return x ? ['class="mono"', n2(x.filled_notional_usdc,1)] : MISS; }},
      {th:['title="篮子完整：挂出的桶全部买到的篮数/挂到交易所的篮数（一个站日的一个阶段算一篮）"', "完整篮"],
        td:r=>{ const x=SS[r.station]; if(!x) return MISS; const c=x.completeness||{};
          return ['class="mono"', c.baskets?`${c.complete}/${c.baskets}`:"—"]; }},
      {th:['title="篮子完整：每篮 买到桶数÷挂出桶数 的平均，如挂 3 买 2 记 67%"', "完整度"],
        td:r=>{ const x=SS[r.station]; if(!x) return MISS; const c=x.completeness||{};
          return [`class="mono ${c.mean==null?"":(c.mean>=0.9?"up":(c.mean<0.6?"down":""))}"`, pc0(c.mean)]; }},
    ]],
    ["命中（结算后）", [
      {th:['title="实盘：该站真实成交腿里已结算的站日数"', "实盘站日"],
        td:r=>r.live ? ['class="mono"', r.live.settled_days] : MISS},
      {fx:true, th:['title="实盘：已结算的实盘篮子数（站日×阶段，每个阶段各持一个篮子）"', "实盘结算笔"],
        td:r=>{ const s=fx(r.station); if(!s) return MISS;
          // Thin stations are not good stations, they are unjudged ones, so
          // the count is muted rather than hidden and says so in place.
          const thin = s.settled_count < 6;
          return [`class="mono${thin?" muted":""}"`, `${s.settled_count}${thin?" 薄":""}`]; }},
      {th:['title="命中·站日：真值落在生产挑中的桶里的站日/已结算站日；挑中的桶成交与否、是否被实盘门拒掉都算（与每日统计同一判定）"', '篮子命中<br><span class="muted">站日</span>'],
        td:r=>{ const x=SS[r.station]; return x ? [`class="mono" ${hsort(x.basket_hit_station_days, x.settled_station_days)}`,
          hfrac(x.basket_hit_station_days, x.settled_station_days)] : MISS; }},
      {fx:true, th:[`title="实盘：真值落在当天挂出的桶里（各阶段合并，成交与否都算）的站日 ÷ 有成交的已结算站日——即只算有成交站日的「篮子命中·站日」；＜${FX?(100*(FX.thresholds.basket_hit_floor||0)).toFixed(0):"—"}% 算「篮子命中」不行"`, '篮子命中<br><span class="muted">有成交站日</span>'],
        td:r=>{ const s=fx(r.station); if(!s) return MISS;
          return [`class="mono" ${hsort(s.basket_hit_days, s.basket_hit_rate==null?0:s.settled_days)}`,
            s.basket_hit_rate==null?"—":hfrac(s.basket_hit_days, s.settled_days)]; }},
      {th:['title="命中·站日：真值落在成交的桶里的站日/有成交的已结算站日"', '持仓命中<br><span class="muted">站日</span>'],
        td:r=>{ const x=SS[r.station]; return x ? [`class="mono" ${hsort(x.held_hit_station_days, x.held_station_days)}`,
          hfrac(x.held_hit_station_days, x.held_station_days)] : MISS; }},
      {th:['title="实盘：命中腿/已结算成交腿，+N待＝已成交但还没结算的腿"', "实盘腿"],
        td:r=>{ const x=r.live; return x ? ['class="mono"',
          `${x.won}/${x.settled_legs}${x.pending?`<span class="muted"> +${x.pending}待</span>`:""}`] : MISS; }},
      {th:['title="命中·笔：一个站日一个阶段成交过的腿算一笔；持有的桶含真值的笔/声称已知的已结算笔。悬停看 90% 区间；声称落在区间外才着色（红＝声称高于区间上沿）"', '命中率<br><span class="muted">笔</span>'],
        td:r=>{ const x=SS[r.station]; if(!x) return MISS;
          const ci = x.hit_rate_interval_90;
          const claim = x.claimed_win_probability_mean;
          // Coloured only when the claim sits outside the interval: inside
          // it, a station's handful of 笔 cannot tell its claim from luck.
          const off = (ci && claim!=null) ? (claim>ci[1]?"down":(claim<ci[0]?"up":"")) : "";
          return [`class="mono ${off}" ${hsort(x.hit_count, x.claimed_count)}${ci?` title="90% 区间 ${pc0(ci[0])}–${pc0(ci[1])}，声称 ${pc0(claim)}"`:""}`,
            hfrac(x.hit_count, x.claimed_count)]; }},
    ]],
    ["模型声称（下单时）", [
      {th:['title="生产入座时记下的整篮声称概率，按决策平均"', "选桶概率和"],
        td:r=>{ const x=SS[r.station]; return x ? ['class="mono"', pc0(x.model_coverage_mean)] : MISS; }},
      {th:['title="命中·笔：下单决策绑定的概率包里持有各桶概率之和，按笔平均"', '声称<br><span class="muted">笔</span>'],
        td:r=>{ const x=SS[r.station]; return x ? ['class="mono"', pc0(x.claimed_win_probability_mean)] : MISS; }},
      {fx:true, th:[`title="实盘：命中率÷平均声称。五闸「声称诚实」要求 0.88–1.18；＜${FX?FX.thresholds.claim_floor:"—"} 算「声称」不行"`, "命中/声称"],
        td:r=>{ const s=fx(r.station); return s ? ['class="mono"', fnum(s.calibration_ratio,2)] : MISS; }},
      {th:['title="生产那一笔决策的席位中心桶相对市场中心桶：偏上/一致/偏下的决策数"', '位置<br><span class="muted">上/平/下</span>'],
        td:r=>{ const x=SS[r.station]; if(!x) return MISS; const sd=x.sides||{};
          return ['class="mono muted"', `${(sd.above||0)}/${(sd.aligned||0)}/${(sd.below||0)}`]; }},
      {fx:true, th:[`title="实盘：成交篮子所依据的模型中心−结算真值，按站日平均。五闸「中心偏差」要求 |x|≤0.8°C；＞${FX?FX.thresholds.centre_limit_c:"—"}°C 算「中心」不行"`, "中心偏差"],
        td:r=>{ const s=fx(r.station); return s ? ['class="mono"', fnum(s.centre_residual_c,2,"C")] : MISS; }},
    ]],
    ["价格与盈亏", [
      {th:['title="生产入座时记下的整篮全含费卖价，按决策平均"', "卖价和"],
        td:r=>{ const x=SS[r.station]; return x ? ['class="mono"', n2(x.seat_ask_total_mean,3)] : MISS; }},
      {th:['title="按实际成交成本算的赔率（按投入加权的均价再求赔率）"', "赔率"],
        td:r=>{ const x=SS[r.station]; return x ? ['class="mono"', x.odds_mean==null?"—":n2(x.odds_mean,2)+"x"] : MISS; }},
      {th:['title="已结算实盘单的声称期望÷投入"', "EV/U"],
        td:r=>{ const x=SS[r.station]; if(!x) return MISS; const ev=x.ev_per_usdc;
          return [`class="mono ${ev==null?"":(ev>0?"up":"down")}"`, ev==null?"—":(ev>0?"+":"")+n2(ev,2)]; }},
      {th:['title="实盘：已结算成交腿的实付金额(U)"', "实盘投入"],
        td:r=>{ const x=r.live; return x ? ['class="mono"', x.settled_legs>0?x.cost_usdc.toFixed(1):"—"] : MISS; }},
      {th:['title="实盘：已结算成交腿的盈亏(U)＝命中份数×1U−实付"', "实盘盈亏"],
        td:r=>{ const x=r.live; if(!x) return MISS; const settled=x.settled_legs>0;
          return [`class="mono ${settled?lsign(x.pnl_usdc):""}"`, settled?(x.pnl_usdc>=0?"+":"")+x.pnl_usdc.toFixed(1):"—"]; }},
      {fx:true, th:['title="实盘：实得盈亏÷声称期望；＜0 算「实现」不行"', "实现/期望"],
        td:r=>{ const s=fx(r.station); return s ? ['class="mono"', fnum(s.realization_ratio,2)] : MISS; }},
      {th:['title="实盘：盈亏÷投入，待结算腿不计入"', "实盘ROI"],
        td:r=>r.live ? [`class="mono ${lsign(r.live.roi)}"`, `<b>${lpct(r.live.roi)}</b>`] : MISS},
      {th:[`title="实盘：实盘账本最近 5 个结算日的 ROI${(LP.recent_window_dates||[]).length?"（"+LP.recent_window_dates[0]+" 起）":""}"`, "实盘近5日"],
        td:r=>r.live ? [`class="mono ${lsign(r.live.recent_roi)}"`, lpct(r.live.recent_roi)] : MISS},
    ]],
    ["站点属性", [
      {th:['title="该站真值的日际跳变均值(°C)：天气性格，>1.76 的站在真边际枯竭期实测 −43%（留存组 +23%，p=0.023）。外生、慢变、先于账本。"', "跳变/日"],
        td:r=>[`class="mono ${r.climate_jump_c==null?"":(r.climate_jump_c>1.76?"down":"up")}"`, r.climate_jump_c==null?"—":r.climate_jump_c.toFixed(2)]},
      {th:['title="近14天池子里模型分布的均值−众数距离(桶)：跳变在模型层的影子(r=+0.32)，>0.93 为高。"', "形状gap"],
        td:r=>[`class="mono ${r.shape_gap_buckets==null?"":(r.shape_gap_buckets>0.93?"down":"")}"`, r.shape_gap_buckets==null?"—":r.shape_gap_buckets.toFixed(2)]},
      {th:['title="站点纬度分带：回归线以内热带，23.4°–35° 副热带，35°–55° 温带，55° 以上高纬"', "气候带"],
        td:r=>{ const g=GEO[r.station]; const bd=g?bandOf(g[0]):null;
          return ['', `${bd?h(bd.replace("半球·","")):"—"} <span class="muted">${g?fmtLat(g[0]):""}</span>`]; }},
      {th:['title="离开阔海面约 30 公里内算沿海，湖和内河不算"', "海陆"],
        td:r=>{ const g=GEO[r.station]; return ['', g?(g[1]?"沿海":"内陆"):"—"]; }},
    ]],
  ].map(([name, cols]) => [name, cols.filter(c => !c.fx || FX)]);
  // The group's rule goes on its first column; a cell that already carries
  // a style takes the rule inside it rather than as a second attribute.
  const cell = (tag, [attrs, content], edge) => {
    let a = String(attrs||"");
    if(edge) a = a.includes('style="') ? a.replace('style="', 'style="border-left:1px solid var(--line);') : `${a} ${EDGE}`;
    return `<${tag}${a?" "+a:""}>${content}</${tag}>`;
  };
  const groupHead = COLUMN_GROUPS.map(([name, cols], gi) =>
    `<th colspan="${cols.length}" style="text-align:center;font-weight:normal${gi?";border-left:1px solid var(--line)":""}" class="muted">${name}</th>`).join("");
  const columnHead = COLUMN_GROUPS.flatMap(([_, cols], gi) =>
    cols.map((c, ci) => cell("th", c.th, gi>0 && ci===0))).join("");
  const rows = (L.stations||[]).map(r=>`<tr class="${barred.has(r.station)?"muted":""}">${
    COLUMN_GROUPS.flatMap(([_, cols], gi) =>
      cols.map((c, ci) => cell("td", c.td(r), gi>0 && ci===0))).join("")}</tr>`).join("");
  const CT = L.contracts || {};
  const authorised = CT.live_submit_authorized === true;
  return `<h2>站点实盘上线闸门</h2>
  <p class="sub">下面的开关是<b>上线意向旗</b>。能不能真发单由<b>两个闸一起决定</b>：
  代码编译的执行模式 <span class="mono">${h(L.execution_mode||"—")}</span>，
  和 runtime 合同 <span class="mono">${h(CT.execution_mode||"—")}</span>
  （<span class="mono">live_submit_authorized=${authorised?"true":"false"}</span>）。
  <b>${authorised?"两个闸都已放行——旗亮即会用真实资金发单。"
    :"合同未授权：旗亮也铸不出 live 契约，意向照造但不出网关。"}</b>
  合同读自 <span class="mono">${h(CT.runtime_config||"—")}</span>${
    CT.minted_at_utc?`（铸于 ${h(CT.minted_at_utc)}）`:""}——
  <b>按执行模式选文件，和生产同一条规则</b>。这里曾经不论模式都读 no-live 那份，
  于是在 go-live 迁移之后，页面对着一个合同说 true 的系统报 false。</p>
  <p class="sub"><b>网关暂停旗归这个开关管（操作者裁定 2026-09-02）。</b>
  举旗时自动撤掉 <span class="mono">data/live_paused.flag</span>，落旗时先把它
  放回去再落旗——<b>没有第二个要记得的文件</b>。在此之前举旗只写意向旗，
  开关写着 ARMED 而网关逐笔拒单，页面只能提示"再去删一个文件"。
  这个文件仍然是随时可用的急停：手动 <span class="mono">touch</span> 它，
  所有车道立刻停。当前
  ${L.submission_paused
    ? `<b style="color:var(--red)">暂停旗在位</b>——它是被手动放上去的（或某次举旗撤旗失败），网关不发单`
    : "暂停旗不在位"}。
  实盘订单与意向簿在上面的<a href="#sec-money">资金看板</a>「实盘」一类里。
  下面的首批站点闸门表同时是<b>逐站汇总（跨日）</b>：每站的实盘订单（含被实盘门拒掉的）、篮子完整度、
  生产入座时的席位读数和地理分组都在同一行，全部读实盘与生产记录，没有纸面账本的列。</p>
  ${completenessCard}
  <div class="card" style="margin-top:12px">
    <div class="muted" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">首批站点闸门
      <button type="button" class="lc-bulk" data-bulk="waive_all" disabled
        onclick="lcBulk(this)"
        title="点击＝全部站一起开：放出所有禁入站（记 released_stations，自动层从此不碰）、撤掉所有待观察、给全部站写豁免——下一轮生产起全部站按实盘下单。一次写盘、一条审计。">✅ 一键全部开（豁免）</button>
      <button type="button" class="lc-bulk" data-bulk="bar_all" disabled
        onclick="lcBulk(this)"
        title="点击＝全部站一起禁入：每个站写一条操作者禁入裁定，同时撤掉所有豁免与待观察——下一轮生产起不再给任何站下单（账本照常记账）。一次写盘、一条审计。">⛔ 一键全部禁入</button></div>
    <div class="muted" style="margin-top:6px;font-size:11.5px">
      全队命中（含禁入站）：篮子命中 ${hfrac(HT.bh, HT.sd)} 站日 · 持仓命中 ${hfrac(HT.hh, HT.hd)} 站日 ·
      命中率 ${hfrac(HT.k, HT.n)} 笔，声称 ${HT.n?pc0(HT.c/HT.n):"—"}。
      篮子命中算挑中的桶，持仓命中只算买到的桶，两者之差在执行；命中率对声称看的是模型。</div>
    ${FX?`<div class="muted" style="margin-top:4px;font-size:11.5px">
      五维诊断：${FX.worst.length?h(FX.worst.join("、"))+" 至少四项不行":"没有站四项以上不行"}，
      另有 ${FX.clean} 站一项都不占。</div>`:""}
    ${groupBlock}
    <div class="scroll" style="max-height:420px;margin-top:6px">
    <table style="white-space:nowrap"><thead><tr>${groupHead}</tr><tr>${columnHead}</tr></thead><tbody>${rows}</tbody></table></div>
  </div>`;
}

// Every request is numbered and every write is counted while it is out, so
// an answer that is not the newest -- or that lands while a click is still
// in flight -- can tell, and does not paint over a state that has moved.
let lcSeq = 0, lcBusy = 0, lcSynced = 0, lcRetry = null;
function lcApi(path, body){
  lcSeq += 1;
  if(body) lcBusy += 1;
  return fetch("http://127.0.0.1:8788"+path, body?{
    method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)}:{}).catch(()=>{
      // A refused connection surfaced as "TypeError: Failed to fetch", which
      // names neither the server nor how to bring it back.
      const err = new Error("控制服务未连接（127.0.0.1:8788）——"
        + "launchctl kickstart -k gui/$(id -u)/com.weather.live_control");
      // Marked, so a caller can tell "nobody is listening" (every control
      // goes back to disabled) from "the server refused this one".
      err.lcDown = true;
      throw err;
    }).finally(()=>{ if(body) lcBusy -= 1; }).then(r=>r.json()).then(j=>{
      // A refused or half-applied request answers with {error}.  Returning
      // it as if it were state let lcRender die on a TypeError whose alert
      // named a missing property instead of the server's reason -- which
      // matters most for the one case that can now half-apply: intent
      // armed, gateway pause flag not lifted.
      if(j && j.error) throw new Error(j.error);
      return j;
    });
}
function lcRender(s){
  if(!s) return;
  const live=document.getElementById("lc-live");
  lcPaintKnobs(s);
  lcPaintStatus(s);
  if(live) live.textContent = s.live.enabled?"ARMED":"OFF";
  // The switch is repainted from the server's answer, never from the click:
  // an optimistic flip would show ARMED over a refused arm, which is the one
  // lie this control must never tell.
  const sw=document.getElementById("lc-live-btn");
  if(sw){
    sw.setAttribute("aria-checked", String(!!s.live.enabled));
    // The hover text carries what a dialog used to say, so it has to follow
    // the flag: left alone it would keep offering to arm a lane that is
    // already armed.
    sw.title = s.live.enabled
      ? "点击落旗：立刻落下网关暂停旗并停止发单"
      : lcArmEffect(D.live_readiness || {});
  }
  // The banner switch carries the same state; paint it red only when the
  // flag is actually up, so a glance at the top of the page answers "may
  // this book go live".
  const head=document.getElementById("lc-head");
  if(head) head.className = "pill" + (s.live.enabled?" red":"");
  const headNote=document.getElementById("lc-head-note");
  if(headNote){
    const L = D.live_readiness || {};
    const C = L.contracts || {};
    const compiled = L.execution_mode === "production_live";
    const authorized = C.live_submit_authorized === true;
    // The pause flag is read from the control server's answer, not from
    // L: the page snapshot was taken at render time and the switch moves
    // the flag on this very click, so L would keep reporting the state the
    // click just left.  Since the switch owns the flag, an armed-and-paused
    // reading no longer means "now go delete a file" -- it means the two
    // disagree, which only a hand `touch` or a failed lift can cause, and
    // the note says so instead of describing a step that no longer exists.
    const paused = s.live.submission_paused === true;
    headNote.textContent = !s.live.enabled ? "控制服务已连接"
      : compiled && authorized && !paused
        ? "实盘中 · 网关正在下单"
        : compiled && authorized
          ? "已举旗但暂停旗仍在位 —— 有人手动放了它，网关不发单"
          : compiled
            ? "旗已举起 · 代码 live，合同未授权发单"
            : "旗已举起 · 代码仍编译为 no-live";
  }
  if(sw) sw.disabled = false;
  // The station gate repaints from the server's answer like the rest: an
  // optimistic flip would show a bar the server refused, which is the one
  // lie a control that stops orders must never tell.
  lcPaintGates(s);
  window.__lcState = s;
  lcSynced = Date.now();
}
// The three steppers, from the server's answer.  A button whose step would
// leave the server's own bounds is drawn disabled rather than left to be
// refused by an alert, and every button's hover names the exact move --
// "预算 114.29 → 64.29" -- because a bare minus that takes fifty dollars off
// is not something a reader should have to learn by clicking it.
function lcPaintKnobs(s){
  const prev = window.__lcState;
  // Charter 141: the station-day's two numbers off the latest real account
  // read, shown beside the dials rather than as one (they are code, not
  // an operator setting).
  const cap = (s && s.capital_layer) || {};
  const capEl = document.getElementById("lc-capital-v");
  if(capEl){
    const eq = cap.account && Number(cap.account.equity_usdc);
    capEl.textContent = (eq && isFinite(eq))
      ? `$${(eq*cap.sizing_fraction).toFixed(1)}/${(eq*cap.ceiling_fraction).toFixed(1)}`
      : "—";
    const capBox = document.getElementById("lc-capital");
    if(capBox && eq && isFinite(eq)){
      capBox.title = `每站日 定注 $${(eq*cap.sizing_fraction).toFixed(2)} / 上限 $${(eq*cap.ceiling_fraction).toFixed(2)}`
        + `（真实余额 $${eq.toFixed(2)}，读于 ${cap.account.observed_at_utc||"—"}；宪章 141）`;
    }
  }
  Object.entries(LC_KNOBS).forEach(([key, K])=>{
    const cur = K.read(s);
    const spec = K.spec(s) || {};
    const [lo, hi] = spec.bounds || [-Infinity, Infinity];
    const unit = K.unit || "";
    const el = document.getElementById(K.id);
    if(el){
      el.textContent = K.fmt(cur);
      // A value the answer moved blinks once, so a click that landed is
      // seen landing -- and one that did not is seen not to.
      if(prev && K.read(prev) !== cur){
        el.classList.remove("lc-flash");
        void el.offsetWidth;
        el.classList.add("lc-flash");
      }
    }
    const box = document.querySelector(`.navctl-k[data-knob="${key}"]`);
    if(box){
      if(!box.dataset.base) box.dataset.base = box.title;
      const unset = false;
      box.title = `${box.dataset.base} · 当前 ${K.fmt(cur)}${unit}${
        unset ? "（未设置，按默认值）" : ""}${isFinite(lo)
        ? ` · 范围 ${K.fmt(lo)}–${K.fmt(hi)}` : ""} · 每次改动写 data/live_control_audit.jsonl`;
      box.removeAttribute("aria-busy");
    }
    document.querySelectorAll(`.lc-w[data-knob="${key}"]`).forEach(btn=>{
      const delta = Number(btn.dataset.delta);
      const next = lcNext(K, cur, delta);
      const out = next < lo - 1e-9 || next > hi + 1e-9;
      btn.disabled = out;
      btn.title = out
        ? `${K.name} ${delta < 0 ? "再减会低于下限" : "再加会超过上限"} ${
            K.fmt(delta < 0 ? lo : hi)}${unit}`
        : `${K.name} ${K.fmt(cur)} → ${K.fmt(next)}${unit}`;
    });
  });
}
// One word in front of the knobs for what a click on them reaches.  It is
// read from the same answer as the LIVE switch, so the bar that stays on
// screen says what the switch at the page head says: the same click moves
// paper on one day and money on the next.
function lcPaintStatus(s){
  const strip = document.getElementById("lc-strip");
  const status = document.getElementById("lc-status");
  const text = document.getElementById("lc-status-t");
  const mode = !s ? "down"
    : s.live.submitting ? "live" : s.live.enabled ? "armed" : "idle";
  const WORD = {down:"控制服务未连接", live:"实盘发单中",
    armed:"已举旗 · 未发单", idle:"未举旗"};
  const NOTE = {
    live:"网关正在用真实资金下单，旋钮改的是下一轮真钱的注码",
    armed:"旗已举起但网关没有发单，原因见页首 LIVE 开关旁",
    idle:"没有举旗，旋钮改的是生产下一轮的注码，不发单",
  };
  if(strip) strip.dataset.lc = mode;
  if(text) text.textContent = WORD[mode];
  if(status) status.title = mode === "down"
    ? "控制服务未连接——launchd 常驻 com.weather.live_control；拉起：launchctl kickstart -k gui/$(id -u)/com.weather.live_control"
    : `控制服务已连接（127.0.0.1:8788）· ${NOTE[mode]} · LIVE 开关在页首`;
}
// Nobody is listening.  Every control goes back to the disabled state it
// was drawn in -- one that looks clickable while the server is down would
// let an operator believe a click did something -- and the page keeps
// asking, so launchd bringing the server back needs no reload.
function lcDown(){
  window.__lcState = null;
  lcPaintStatus(null);
  document.querySelectorAll(".lc-w, #lc-live-btn, .lc-gate, .lc-bulk")
    .forEach(b=>{ b.disabled = true; });
  document.querySelectorAll(".navctl-k[aria-busy]")
    .forEach(k=>k.removeAttribute("aria-busy"));
  const headNote = document.getElementById("lc-head-note");
  if(headNote) headNote.textContent = "控制服务未连接";
  if(!lcRetry) lcRetry = setTimeout(()=>{ lcRetry = null; lcInit(); }, 10000);
}
function lcAdjust(knob, delta){
  const s = window.__lcState, K = LC_KNOBS[knob];
  if(!s || !K) return;
  const next = lcNext(K, K.read(s), delta);
  // Held down for the round trip, like the LIVE switch.  A second click
  // before the answer computed its step from the state the first had not
  // yet replaced, so it sent the same value again: two clicks, one step,
  // and nothing on screen said so.
  const box = document.querySelector(`.navctl-k[data-knob="${knob}"]`);
  if(box) box.setAttribute("aria-busy", "true");
  document.querySelectorAll(`.lc-w[data-knob="${knob}"]`)
    .forEach(b=>{ b.disabled = true; });
  lcApi(K.path, {value: next}).then(lcRender).catch(e=>{
    if(e && e.lcDown) lcDown();
    else if(window.__lcState) lcPaintKnobs(window.__lcState);
    alert("失败: "+e);
  });
}
function lcLive(){
  const s = window.__lcState; if(!s) return;
  const next = !s.live.enabled;
  // One click, both directions, no dialog -- operator ruling 2026-09-01,
  // reaffirmed after I shipped a confirmation on the arming half.  What the
  // switch is about to do is written beside it in the banner: the state
  // word, the note naming which downstream gates are open, and the readiness
  // section directly beneath.  A modal would repeat what the reader is
  // already looking at, and it would sit in the way of the one action that
  // must never be slowed -- the same click, a moment later, is the stop.
  const btn = document.getElementById("lc-live-btn");
  // Held down for the round trip: the switch is one control over one piece
  // of state, and a second click while the first is in flight sends the
  // opposite instruction against a state that has already moved.
  if(btn) btn.disabled = true;
  // The server still refuses an arm that presents no token; the switch is
  // the confirmation now, so the switch carries it.
  lcApi("/api/live", next
      ? {enabled:true, confirm:"LIVE", by:"dashboard"}
      : {enabled:false, by:"dashboard"})
    .then(lcRender)
    .catch(e=>{
      if(e && e.lcDown) lcDown(); else if(btn) btn.disabled=false;
      alert("失败: "+e);
    });
}
// The station gate: three switches per row, one click each.
//
// They are drawn disabled and only the control server's answer enables them
// -- same contract as every other control on this page.  A gate that looks
// clickable while nothing is listening would let an operator believe a
// station was barred because the pill turned red.
const LC_GATES = {
  bar:   {path:"/api/station-bar",     key:"barred", label:"⛔禁入"},
  waive: {path:"/api/station-waiver",  key:"waived", label:"✅豁免"},
  hold:  {path:"/api/station-hold",    key:"held",   label:"⏳待观察"},
};
function lcGateTip(kind, on, isAuto){
  if(kind==="bar") return on
    ? (isAuto?"自动层禁的。点击＝人工放出，并记一条操作者裁定，让 sweep 不再碰它"
            :"点击＝人工放出（记 released_stations，自动层从此不碰）")
    : "点击＝人工禁入：不再给这个站下单，账本照常记账";
  if(kind==="waive") return on
    ? "点击＝撤销豁免，回到按结算笔数判"
    : "点击＝豁免：结算笔数不够也上线";
  return on ? "点击＝解除按住，回到按结算笔数判"
            : "点击＝按住：够格也先不下单，不占禁入席位";
}
function lcGateButtons(station, isBarred, isWaived, isHeld, isAuto){
  const state = {bar:isBarred, waive:isWaived, hold:isHeld};
  return Object.keys(LC_GATES).map(kind=>{
    const on = !!state[kind];
    return `<button type="button" class="lc-gate" data-station="${station}"
      data-gate="${kind}" data-owner="${kind==="bar"&&isAuto?"auto":"operator"}"
      role="switch" aria-checked="${on}" disabled
      title="${lcGateTip(kind,on,isAuto)}"
      onclick="lcGate(this)">${LC_GATES[kind].label}</button>`;
  }).join("");
}
function lcGate(btn){
  const kind = btn.dataset.gate, station = btn.dataset.station;
  const spec = LC_GATES[kind]; if(!spec) return;
  const on = btn.getAttribute("aria-checked")==="true";
  // Every switch is held down for the round trip, not only the one clicked:
  // the server clears the other two on the station it moves, so more than
  // one of them may be about to change and none may show a state that has
  // not come back yet.
  document.querySelectorAll(".lc-gate, .lc-bulk").forEach(b=>{b.disabled=true;});
  const body = {station: station, by: "dashboard"};
  body[spec.key] = !on;
  lcApi(spec.path, body).then(lcRender).catch(e=>{
    // Repaint from the last state the server confirmed, then say why.  A
    // refusal here is usually the server enforcing the exclusivity the row
    // shows (no waiver or hold on a barred station), and leaving the row
    // disabled would look like the page had broken instead.
    if(e && e.lcDown) lcDown(); else lcPaintGates(window.__lcState);
    alert("失败: "+e);
  });
}
// The two bulk switches (user, 2026-09-16): every station open on a waiver,
// or every station barred, in one write and one audit line.  Same contract
// as the per-row switches -- drawn disabled, enabled only by the server's
// answer, every switch on the table held down for the round trip, and the
// table repainted from what comes back rather than from the click.  No
// dialog, as with the LIVE switch: the hover names exactly what it does.
function lcBulk(btn){
  const action = btn.dataset.bulk;
  if(action!=="waive_all" && action!=="bar_all") return;
  document.querySelectorAll(".lc-gate, .lc-bulk").forEach(b=>{b.disabled=true;});
  lcApi("/api/station-bulk", {action: action, by: "dashboard"})
    .then(lcRender).catch(e=>{
      if(e && e.lcDown) lcDown(); else lcPaintGates(window.__lcState);
      alert("失败: "+e);
    });
}
function lcPaintGates(s){
  const live = s && s.live; if(!live) return;
  document.querySelectorAll(".lc-bulk").forEach(b=>{ b.disabled = false; });
  const barred = new Set(live.station_exclusions||[]);
  const waived = new Set(live.station_probation_waivers||[]);
  const held   = new Set(live.station_probation_holds||[]);
  const allow  = new Set(live.station_allowlist||[]);
  const auto   = new Set((live.station_bar_sources||{}).auto||[]);
  document.querySelectorAll(".lc-gate").forEach(b=>{
    const station = b.dataset.station, kind = b.dataset.gate;
    const on = kind==="bar" ? barred.has(station)
             : kind==="waive" ? waived.has(station) : held.has(station);
    const isAuto = kind==="bar" && auto.has(station);
    b.setAttribute("aria-checked", String(on));
    b.dataset.owner = isAuto ? "auto" : "operator";
    b.title = lcGateTip(kind, on, isAuto);
    // The server refuses a waiver or a hold on a barred station rather than
    // accepting one that would be inert, so those two must not look
    // available while the bar is up.
    b.disabled = kind!=="bar" && barred.has(station);
  });
  const waiting = new Set(live.station_probation||[]);
  document.querySelectorAll("[data-lc-state]").forEach(el=>{
    const station = el.dataset.lcState;
    el.textContent = barred.has(station) ? "⛔ "
      : allow.has(station) ? "✔ " : (waiting.has(station) ? "⏳ " : "");
    const row = el.closest("tr");
    if(row) row.className = barred.has(station) ? "muted" : "";
  });
}
function lcInit(){
  if(!document.getElementById("lc-status")) return;
  // Keep asking until the server answers.  One attempt at load meant a page
  // opened while the server was down stayed disabled until a manual reload,
  // even after launchd had it back -- and the switch that cannot be clicked
  // is also the one that stops orders.  Only a failed connection retries: a
  // render error is thrown past this handler rather than hidden behind a
  // silent retry loop.
  lcApi("/api/state").then(lcRender, lcDown);
}
// Read again when the tab comes back into view, at most once a minute --
// not on a timer.  Every answer walks the SSOT for the station scope (about
// half a second), and a board left open all day would ask thousands of
// times for numbers that move a few times a day.  What it catches is the
// case a timer was wanted for: a knob or the flag moved from another tab
// or by hand while this one sat in the background.
document.addEventListener("visibilitychange", ()=>{
  if(document.visibilityState !== "visible") return;
  if(!document.getElementById("lc-status") || lcRetry) return;
  if(Date.now() - lcSynced < 60000) return;
  const request = lcApi("/api/state");
  const at = lcSeq;
  request.then(s=>{ if(lcSeq === at && !lcBusy) lcRender(s); },
    e=>{ if(lcSeq === at && e && e.lcDown) lcDown(); });
});

// Click a header to sort by that column.  Numbers, percentages, prices and
// odds all read as numbers; anything else falls back to text, and an em dash
// always sinks so empty cells never crowd the top of either direction.
function makeSortable(table){
  // Some tables carry meaning in their row order and cannot be sorted at
  // all: the live order book groups legs under a basket header row, so any
  // column sort interleaves headers with legs from other baskets and the
  // grouping silently becomes a lie.  Those tables opt out here rather
  // than growing a group-aware comparator, and they opt out before the
  // headers are made to look clickable -- a pointer cursor on a header
  // that does nothing is its own small deception.
  if(table.dataset.grouped==="1") return;
  // Direct children only.  A stage cell now carries its own per-leg table,
  // so a descendant selector reaches into it: the outer table's header
  // collected the inner table's `th`, and sorting collected the inner
  // table's rows and re-appended them into the outer `tbody`.  The page
  // showed bucket names under the station column and rung counts under the
  // stage columns -- the inner table's rows, wearing the outer table's
  // header.
  //
  // A header can span rows -- a group title over its columns -- so a
  // header's column is read off the grid its rows lay out, not off its
  // place in a flat list of every th: that list counted a group title as a
  // column and sorted each header on a neighbour's cells.  Only a header
  // that ends on the last header row and covers one column sorts.
  const headRows=[...table.querySelectorAll(":scope > thead > tr")];
  const taken=headRows.map(()=>[]);
  const heads=[];
  headRows.forEach((tr,ri)=>{
    let col=0;
    [...tr.children].forEach(th=>{
      while(taken[ri][col]) col++;
      const cs=Math.max(1,th.colSpan||1), rs=Math.max(1,th.rowSpan||1);
      for(let dr=0;dr<rs&&ri+dr<headRows.length;dr++)
        for(let dc=0;dc<cs;dc++) taken[ri+dr][col+dc]=true;
      if(cs===1&&ri+rs>=headRows.length) heads.push({th,index:col});
      col+=cs;
    });
  });
  heads.forEach(({th,index})=>{
    th.style.cursor="pointer";
    // Appended, not assigned: assigning wiped every header's own hover
    // text, so no column on a sortable table could say what it counts.
    th.title=th.title ? th.title+"（点击排序）" : "点击排序";
    th.addEventListener("click",()=>{
      const body=table.querySelector(":scope > tbody");
      if(!body) return;
      const rows=[...body.querySelectorAll(":scope > tr")];
      const asc=th.dataset.sortDir!=="asc";
      heads.forEach(({th:o})=>{o.dataset.sortDir="";o.dataset.sorted="";});
      th.dataset.sortDir=asc?"asc":"desc";
      th.dataset.sorted="1";
      const value=(row)=>{
        const cell=row.cells[index];
        if(!cell) return {n:null,s:""};
        // A cell that shows two numbers says which one it sorts on.  "8/9
        // 89%" read as its first number sorted the hit columns by hit count,
        // so 1/1 sat under 8/9; the cell carries the rate instead, and an
        // empty value sinks like a dash.
        if(cell.dataset.sort!==undefined){
          const n=cell.dataset.sort===""?NaN:parseFloat(cell.dataset.sort);
          return Number.isFinite(n)?{n:n,s:""}:{n:null,s:""};
        }
        // Sort on what the cell shows, not on what it hides.  ``textContent``
        // is recursive, so a folded-away per-leg table contributes its share
        // counts and rung prices to the sort key -- and the first number in
        // the cell is then whichever one the DOM happens to reach first.
        const shown=cell.cloneNode(true);
        shown.querySelectorAll("details,table").forEach(n=>n.remove());
        const text=shown.textContent.trim();
        if(!text||text==="—") return {n:null,s:""};
        const m=text.replace(/[,\s]/g,"").match(/-?\d+(\.\d+)?/);
        return {n:m?parseFloat(m[0]):null,s:text};
      };
      rows.sort((a,b)=>{
        const x=value(a),y=value(b);
        if(x.n===null&&y.n===null) return x.s.localeCompare(y.s)*(asc?1:-1);
        if(x.n===null) return 1;   // blanks sink either way
        if(y.n===null) return -1;
        return (x.n-y.n)*(asc?1:-1);
      });
      rows.forEach(r=>body.appendChild(r));
    });
  });
}

// P0-M is the one stage with no output at all, and the page used to say
// nothing about why.  It is not broken: the training lane runs every cycle
// for every station and refuses to fit on evidence it does not have.  Show
// the distance, per station, so the wait is a number instead of a mystery.

// One section throwing used to blank the whole page: the assembly is a
// single expression, so a ReferenceError anywhere in it means nothing is
// ever assigned to #app and the reader gets a white screen with no clue.
// A section that cannot render says so, in place, and the other twelve
// still render.
function safe(fn, name){
  try { return fn(); }
  catch (err) {
    return `<h2>${name}</h2><div class="card"><span class="dot red"></span>
      这一节渲染失败：<span class="mono">${String(err)}</span></div>`;
  }
}

// Six layers, six lights.  The colour and the sentence are the answer; the
// numbers underneath are only there for someone who wants to check it.
const VERDICT_WORD = {green:"良好", amber:"注意", red:"有问题", muted:"证据不足"};

function scorecard(){
  const S = D.scorecard || {};
  if(!S.available) return `<h2>模型校准总检：检查概率模型六层是否各自达标</h2>
    <p class="sub">还没有已结算的预测可以评分。</p>`;
  const cards = (S.layers||[]).map(L => `<div class="card">
    <div style="display:flex;align-items:center;gap:8px">
      <span class="dot ${L.health}"></span><b>${h(L.name)}</b>
      <span style="flex:1"></span>
      <span class="pill ${L.health}">${VERDICT_WORD[L.health]||""}</span>
    </div>
    <div class="muted" style="margin-top:2px">${h(L.question)}</div>
    <div style="margin-top:10px;font-size:14.5px;line-height:1.5">
      ${h(L.verdict)}</div>
    <div class="muted" style="margin-top:8px">${h(L.detail)}</div>
  </div>`).join("");

  // While the sample accumulates, no cell is graded: a lamp on four event
  // days says more than the evidence does, in the table as on the cards.
  const tone = health => S.accumulating ? "muted" : health;
  const leadRows = (S.rows||[]).map(r => `<tr>
    <td>${h(r.lead_label)}</td>
    <td class="mono muted">${r.station_days == null ? "—"
      : r.station_days.toLocaleString()}</td>
    <td class="mono"><span class="dot ${tone(r.bias_health)}"></span>
      ${r.bias_rungs == null ? "—" :
        (r.bias_rungs>0?"+":"")+r.bias_rungs.toFixed(2)} 档</td>
    <td class="mono"><span class="dot ${tone(r.mae_health)}"></span>
      ${r.mae_rungs == null ? "—" : r.mae_rungs.toFixed(2)} 档</td>
    <td class="mono"><span class="dot ${tone(r.crps_health)}"></span>
      ${pct1(r.crps_skill)}</td>
    <td class="mono muted">${pct1(r.brier_skill)}</td>
    <td class="mono"><span class="dot ${tone(r.spread_health)}"></span>
      ${r.spread_skill == null ? "—" : r.spread_skill.toFixed(2)}×</td>
  </tr>`).join("");

  const pit = (S.pit_bins||[]);
  const pitMax = Math.max(0.1, ...pit);
  const pitBars = pit.map((v,i) => `<div style="flex:1;text-align:center">
    <div style="height:52px;display:flex;align-items:flex-end">
      <div style="width:100%;height:${(v/pitMax*100).toFixed(0)}%;
        background:var(--${S.accumulating ? "muted" : (Math.abs(v-0.1)<=0.04?"green":
          (Math.abs(v-0.1)<=0.08?"amber":"red"))});border-radius:3px 3px 0 0">
      </div></div>
    <div class="muted" style="font-size:10px">${(i*10)}</div>
  </div>`).join("");

  const fusionSummary = S.fusion_summary || {};
  const fusion = (S.fusion_pairs||[]).filter(p => p.distance != null)
    .slice(0,8).map(p => {
      const names = Object.keys(p.served||{}).sort();
      const tone = p.distance <= 0.10 ? "green"
                 : (p.distance <= 0.25 ? "amber" : "red");
      return `<tr>
        <td class="mono">${h(p.station_id)}</td>
        <td class="mono muted">${h(p.lead_bucket)}</td>
        <td class="mono muted">${h(p.run_cycle)} · ${h(p.season_bin)}</td>
        <td class="muted" style="font-size:12px">${h(p.rule)}</td>
        <td class="mono">${p.max_correlation == null ? "—"
          : p.max_correlation.toFixed(2)}</td>
        <td class="mono"><span class="dot ${tone}"></span>
          ${(p.distance*100).toFixed(1)}%</td>
        <td class="muted" style="font-size:12px">${names.map(n =>
          `${h(n.split("_")[0])} ${(p.served[n]*100).toFixed(0)}→${
           (((p.resolved||{})[n]||0)*100).toFixed(0)}%`).join(" · ")}</td>
      </tr>`;
    }).join("");
  const fusionBasisWords = {capped: "族上限咬住", fallback: "逆误差兜底",
    non_psd: "相关阵非正定", unsolved: "重解失败"};
  const fusionApprox = Object.entries(fusionSummary.approximate_by_basis||{})
    .map(([basis, count]) => `${fusionBasisWords[basis]||basis} ${count}`)
    .join("、");
  const fusionPct = v => v == null ? "—" : (v*100).toFixed(1) + "%";

  const W = S.window || {};
  const count = v => v == null ? "—" : v.toLocaleString();
  const windowText = W.anchor
    ? `P0 服务算术锚 ${h(String(W.anchor).slice(5,16).replace("T"," "))}Z 起、最多回看 ${W.max_days ?? 45} 天`
    : `近 ${W.max_days ?? 45} 天（没读到服务算术锚，样本没有按规则换代截断）`;
  return `<h2>模型校准总检：检查概率模型六层是否各自达标</h2>
  <p class="sub">${windowText}：${count(S.station_day_count)} 个已结算站日，
  ${count(S.event_day_count)} 个事件日、${count(S.station_count)} 个站，全部在样本外。
  同一站×事件日×提前量不论被多少个候选视图、cycle 重复打分，都只按一份计。总体
  <span class="pill ${S.overall}">${VERDICT_WORD[S.overall]||""}</span>，
  最近一次预测 ${hours(S.latest_age_hours)}。</p>
  ${S.accumulating ? `<div class="why"><b>攒证据 ${count(S.event_day_count)}/${count(S.min_event_days)} 个事件日：</b>
  锚点之前的预测出自已退役的规则，不再计入。除「模型融合」外，五盏灯暂不评级，
  数字照常显示；够 ${count(S.min_event_days)} 个事件日后自动恢复评级。</div>` : ""}
  <div class="why"><b>怎么看：</b>先看六个灯，绿＝这一层能用，黄＝能用但别压满，
  红＝这一层的数字现在不能直接拿来下注。每个灯下面那句话就是结论，再下面的数字
  只是给你复核用的。<br>
  <b>为什么用「档」不用「度」：</b>盘口有摄氏和华氏两种，华氏每档 2°F（1.11°C）、
  摄氏每档 1°C，用度会把两种盘口按不同标准打分。而且真正要押的就是档位，
  所以「差几档」才是会亏钱的那个误差。<br>
  <b>红黄绿的线划在哪：</b>偏差 0.3/0.8 档、平均误差 1.0/1.5 档（半档起就会系统性押错）；
  概率分要赢气候基准 10% 以上，PIT 最大偏离 4%/8%；ECE 与最差分箱偏离 5%/10%；
  宽度/误差偏离 1.0 超过 0.20/0.45；按事件日整天重采样的区间超过数值本身 15%/30%
  就说明天数还不够。</div>
  <div class="grid g3" style="margin-top:12px">${cards}</div>

  <h2 style="margin-top:26px">按提前量拆开</h2>
  <p class="sub">越往后越难，这张表看的是哪一档提前量开始撑不住。</p>
  <table><thead><tr>
    <th>提前量</th><th>站日</th><th>偏差</th><th>平均误差</th>
    <th>CRPS 胜气候</th><th>Brier 胜气候</th><th>宽度/误差</th>
  </tr></thead><tbody>${leadRows}</tbody></table>

  <p class="sub" style="margin-top:26px">「说了多少 vs 真发生了多少」残差表
  已并入「band 表」，与产生它的修正系数放在同一条闭环里；本节的校准灯仍由同一份
  数据打分。</p>

  <h2 style="margin-top:26px">PIT：分布形状正不正</h2>
  <p class="sub">如果分布形状是对的，真值落在分布各个位置的次数应该一样多，
  下面十根柱子应该一样高（每根 10%）。中间鼓＝分布太宽，两头翘＝分布太窄。
  这里用的是随机化 PIT：真值所在那一档在分布里占一段区间，真值按这段区间均匀摊开，
  所以完全校准时十根柱子严格一样高。</p>
  <div style="display:flex;gap:4px;align-items:flex-end;margin-top:10px">
    ${pitBars}</div>
  <div class="muted" style="margin-top:4px">横轴：真值在预测分布中的位置（百分位）</div>

  ${fusion ? `<h2 style="margin-top:26px">融合权重 vs 生产重解</h2>
  <p class="sub"><b>现役权重由稳定池规则给出（宪章 134，2026-09-14 上线）。</b>
  先按训练残差的收缩协方差求最小方差权重，共享训练日不足 12 天时落回逆误差兜底；
  再把结果向「按族均分」收缩一半（κ=0.5）。八月的竞赛件在过期前仍按最小方差服务。
  这张表是一致性自检，只读在役指针指向的那份证据。右边是用存档的残差相关性、
  影子逆方差权重（还原各家方差）和生产同一组函数当场重解的权重，距离应当接近零。
  距离若系统性变大，说明上线的解和存档的证据对不上了，先查数据再查解法。</p>
  <table><thead><tr>
    <th>站</th><th>提前量</th><th>cycle · 季节</th><th>规则</th>
    <th>最大相关</th><th>离重解</th><th>现在 → 重解</th>
  </tr></thead><tbody>${fusion}</tbody></table>
  <div class="muted" style="margin-top:6px">
    共 ${fusionSummary.served_count ?? "—"} 份在役权重，
    只列能精确重解的 ${fusionSummary.exact_count ?? "—"} 份里偏离最大的几个。
    ${fusionApprox ? `另有 ${fusionApprox} 份只能近似重解，不计入灯色
    （中位差 ${fusionPct(fusionSummary.approximate_median_distance)}、
    最大 ${fusionPct(fusionSummary.approximate_max_distance)}）：
    兜底分支生产读的是闸门那次拟合的误差与天数，存档只留了服务拟合那一份。` : ""}
    相关高但距离小＝模型在重复犯错，权重照规则分，不代表算错。</div>` : ""}`;
}

function servingWeights(){
  const W = D.serving_weights || {}; const rows = W.rows || [];
  if(!rows.length) return "";
  const summary = W.summary || [];
  const pct = w => w == null ? "—" : (w * 100).toFixed(0) + "%";
  const cards = summary.map(s => `<div class="card">
    <div class="muted">${h(s.label)}</div>
    <div class="big">${s.seats} <span class="muted"
      style="font-size:14px">/ ${W.scope_count} 个格子在场</span></div>
    <div class="muted">上场时权重中位数 ${pct(s.median_weight)} ·
      参赛 ${s.candidacies} 次，落选 ${s.eliminated} 次</div></div>`).join("");
  const leadLabels = {};
  rows.forEach(r => { leadLabels[r.lead_bucket] = r.lead_label; });
  const leadOrder = Object.keys((summary[0] || {}).by_lead || {})
    .filter(bucket => bucket in leadLabels);
  const matrixHead = leadOrder.map(bucket =>
    `<th>${h(leadLabels[bucket])}</th>`).join("");
  const matrixBody = summary.map(s => `<tr><td><b>${h(s.label)}</b></td>${
    leadOrder.map(bucket => {
      const cell = (s.by_lead || {})[bucket];
      if(!cell || !cell.seats) return `<td class="muted">—</td>`;
      return `<td class="mono">${pct(cell.median_weight)}
        <span class="muted">×${cell.seats}</span></td>`;
    }).join("")}</tr>`).join("");
  const modelCols = summary.map(s => s.label);
  const detailBody = rows.map(r => {
    const stale = W.stale_cut && r.holdout_end < W.stale_cut;
    const cells = modelCols.map(label => {
      if(label in r.weights)
        return `<td class="mono"><b>${pct(r.weights[label])}</b></td>`;
      if(r.candidates.includes(label))
        return `<td class="muted">落选</td>`;
      return `<td class="muted">—</td>`;
    }).join("");
    return `<tr><td><b>${h(r.station)}</b></td>
      <td>${h(r.lead_label)}</td><td class="mono">${h(r.run_cycle)}</td>
      ${cells}
      <td class="mono">${r.candidate_count}→${r.selected_count}</td>
      <td class="mono ${stale ? "down" : ""}">${h(r.holdout_end)}</td></tr>`;
  }).join("");
  return `<h2>四大模型的权重：决定各模型在服务概率里占多少</h2>
  <p class="sub">这里读的是服务路径本身——每个「站×提前量×cycle」格子当前激活的
  组合指针，连到它绑定的选拔证据，把里面的权重原样摊开。不是权重表的行数统计，
  就是 P0 此刻在用的那份配比。</p>
  <div class="why"><b>怎么看：</b>每个格子的席位都是在完全相同的检验窗上赛出来的：
  参赛的模型一起在最近几天的共同 holdout 上打分，CRPS 最低的子集当选，权重随之而来。
  「落选」＝参赛了但没打赢（权重 0），「—」＝这个格子没凑齐它的参赛条件。
  权重每天随新证据重赛，所以同一格子的配比会换代；
  「窗至」是该格子当前配比所依据的检验窗最后一天，
  日期旧说明这个格子还在服旧一轮的赛果——换代受快照新鲜度限速，逐格推进，属正常。</div>
  <div class="grid g4" style="margin-top:12px">${cards}</div>
  <table style="margin-top:12px"><thead><tr>
    <th>模型</th>${matrixHead}
  </tr></thead><tbody>${matrixBody}</tbody></table>
  <div class="muted" style="margin-top:6px">格子里是「上场时权重中位数 × 席位数」，
  按提前量分列。${W.stale_count ? `目前 <b>${W.stale_count}</b> 个格子仍在服
    ${h(W.stale_cut)} 之前的赛果（最新窗至 ${h(W.newest_holdout_end)}），
    重绑进行中。` : `全部格子的检验窗都在 ${h(W.newest_holdout_end)} 附近，无积压。`}</div>
  <details style="margin-top:10px">
    <summary class="muted" style="cursor:pointer">
      逐格明细：${W.scope_count} 个格子的完整权重（可排序）</summary>
    <table style="margin-top:8px"><thead><tr>
      <th>站</th><th>提前量</th><th>cycle</th>
      ${modelCols.map(label => `<th>${h(label)}</th>`).join("")}
      <th>候选→当选</th><th>窗至</th>
    </tr></thead><tbody>${detailBody}</tbody></table>
  </details>`;
}



// Sections of one kind stand together, and only one kind is on screen at a
// time (user ruling, 2026-08-21).  Twenty sections in a single column is a
// page nobody scrolls to the end of, and the order they were written in
// interleaved the three questions being asked of them: the model's skill,
// the market's account of it, and whether the data feeding both arrived.
// Reading any one of those meant scrolling past the other two.
//
// The section numbers follow this grouping rather than the old order.  A
// number that no longer matches its neighbours is a worse handle than a
// renumbered one, because it only reads as an accident.
const GROUPS = [
  // Grouped by what the reader is asking, not by the order the sections
  // were built in (user ruling, 2026-08-29).  The other two questions -- is
  // the data arriving, is the machine running -- left the tab bar on
  // 2026-09-11: they are answered by the head's lamps, and an answer read
  // on arrival does not belong behind a tab.  Each group carries a colour
  // so the tab bar and the group titles agree without matching text.
  //
  // One area per layer, in the order of the chain (user rulings,
  // 2026-09-16: "固化减仓不应该放在形状里", then "band表应该放在模型里,
  // 再次按照层级归类整理").  The layer is where the rule actually runs, not
  // the word the section happens to use: band 表 is called a claim table
  // but it is applied inside the probability serving chain, after
  // 中心/宽度, fitted on served probability against settled truth with no
  // market in it -- so it is the model's last step, not a claim discount.
  // Everything that prices our probability against the market -- the EV
  // identity, truth landing on the price × probability grid, λ, β, the
  // valuation lane, the thermometer that is λ's evidence, the convergence
  // lift -- is one layer, 边际层 (the E of the P/M/E/S/Z/X layering).
  //   模型 概率层 → 边际 边际层 → 选窗 选窗层 → 定注 定注层
  // The book, the live switch and the refusal count stay above the tabs:
  // they are read on arrival, whatever layer is being studied.
  //
  // Inside an area the summary comes first and the detail follows it (user
  // rulings, 2026-09-17: 总分逻辑; 四大模型的权重、离散度要前移).  The model
  // area opens on 模型校准总检, then walks the serving chain from upstream
  // down: the four models' own inputs (correction, weights, spread, hit
  // rate), the two steps that shape the served distribution (centre/width,
  // band table), the checks of that served distribution (OOS, where truth
  // lands against its mean), and last the half-life roll-out.
  {id:"model", label:"模型", short:"模型", tone:"#d2a8ff",
   note:"概率层：先看总检，再沿链条从上游到下游——四大模型（订正、权重、离散度、命中率）→ 服务分布（中心/宽度、band 表）→ 检验（OOS、真值离均值）→ 换代（H5 半衰期）",
   sections:[[scorecard,"模型校准总检"],
     // the four models, as they come in
     [tables,"偏差订正与权重"],[servingWeights,"四大模型权重"],
     [dispersion,"模型离散度"],[hitRate,"模型命中率"],
     // the served distribution
     [centerWidth,"中心/宽度"],[bandTable,"band 表"],
     // checks of what was served
     [oosScores,"OOS 打分"],[centreLanding,"真值离均值多远"],
     // roll-out
     [h5Readiness,"H5 半衰期"]]},
  // The edge area answers one question -- the model says p, the market says
  // m, how much of the gap should be believed -- and its eight sections are
  // four different kinds of thing (user ruling, 2026-09-17: "把前端显式按照
  // 这个逻辑改一下").  Filed under explicit sub-headings, in reading order:
  // the referees that read truth and say whether there is an edge at all;
  // the one table production buys on, with its evidence; the three
  // candidates that answer the same question in shadow (L4 contains the
  // other two); and the extra lift applied to p before any of them.  The
  // map at the head of the area draws the same relations and jumps to each.
  {id:"edge", label:"边际", short:"边际", tone:"#79c0ff",
   note:"边际层：按 L4 的公式 logit b = logit m + w·(logit p − logit m) + c(m) 读——① 验收（有没有 edge）→ ② 实盘此刻用什么代替 w → ③ L4 与它的特例（影子）→ ④ 进来前先改 p（影子）",
   intro: edgeMap,
   sections:[
     {sub:"① 验收：这个公式赢没赢市场（到底有没有 edge）", status:"audit",
      note:"读真值，不参与决策。EV 恒等式量的是概率层服务出来的 p（已含 band，不含 λ、L4 等读价格的层）有没有市场没有的信息；L4 自己赢没赢看 L4 小节的前瞻技能分"},
     [charterEv,"宪章 EV 恒等式"],
     [truthLanding,"真值落位"],
     {sub:"② 实盘此刻用来代替 w 的：λ 表", status:"live",
      note:"不从市场出发，按「价格 ÷ 声称」分档直接给 p 打折；没证据时沿用常数继续买。体温计是它的证据，不是规则"},
     [effectiveEdgeLambda,"有效边际 λ 表"],
     [edgeBandHealth,"声称边际体温计"],
     {sub:"③ L4 与它的两个特例（影子，只记录不买）", status:"shadow",
      note:"L4 量 w 与 c(m)；β 车道和估值车道是 L4 取概率空间、去掉 c(m) 的特例。三条都从 b = m 出发，没证据就不买"},
     [posteriorLane,"L4 后验层（影子）"],
     [betaShadow,"β 车道（影子）"],
     [valuationLane,"统一估值车道"],
     {sub:"④ 进来前先改 p（影子）", status:"shadow",
      note:"模型往上、市场往下时把 p 抬高，再交给上面；只记账"},
     [convergenceClaim,"双向收敛系数"]]},
  // 覆盖 · 价格 · EV is the whole seat in one line (what it covered, cost
  // and was worth), 篮子命中率 splits it by width, and the market-lean gate
  // -- a rule in shadow -- follows the readings of what actually seated.
  {id:"select", label:"选窗", short:"选窗", tone:"#e3b341",
   note:"选窗层：挑哪几个桶、挑得值不值——覆盖与 EV（总）、篮子命中率、市场方向闸（影子）",
   sections:[[coverage,"覆盖 · 价格 · EV"],[basketHit,"篮子命中率"],
     [marketLeanGate,"市场方向闸"]]},
  {id:"sizing", label:"定注", short:"定注", tone:"#56d4dd",
   note:"定注层：最后真正拿着什么——分布与持仓分家",
   sections:[[windowDivergence,"分布与持仓分家"]]},
];
const tabs = `<nav class="tabs" id="tabs" aria-label="区域">${
  GROUPS.map(g => `<button type="button" data-tab="${g.id}"
    style="--tone:${g.tone}" title="${g.label}（${
    g.sections.filter(Array.isArray).length} 个区块）· ${
    g.note}">${g.short}</button>`).join("")
}<button type="button" class="all" data-tab="all"
  title="所有区域叠在一页">全部</button></nav>`;

// Every section gets its own element and id so the jump bar has somewhere
// to land.  The chip says the name the section is filed under here -- the
// name its lane strip, its star and its error guard all key on -- and its
// hover carries the section's own <h2> in full.  Until 2026-09-16 the chip
// was the heading cut at its first colon, which ran the model area to two
// lines ("四大模型的" three times over) and named only the first of the two
// headings under 订正准度与政权偏移.
// The homepage board is one section, not two (user ruling, 2026-08-31):
// the tile strip and the three-state comparison of what the bars cost read
// the same account, and splitting them behind another heading and another
// jump chip made a reader navigate between halves of one question.  Each
// part still renders under its own guard.
//
// The portfolio ratios left this board on 2026-09-09 (user ruling) for the
// head status bar: "is the machine turning" and "what is the book worth"
// are the two things read on arrival, and the ratios sat five thousand
// pixels below the bar that answers the first.
// Three chips are starred (user ruling, 2026-09-11).  The bar gives every
// section the same chip, which is right when the sections are peers and
// wrong here: these three are how the objective is read -- what a claimed
// probability is worth, what a claimed edge is worth, and how much of a
// claim the sizing may believe -- and in a row of twenty equal chips they
// were found by reading every label.  Keyed on the registry's name rather
// than the heading, so a heading may be reworded without silently losing
// its star.
// Which layer each section belongs to, what it does there, and -- the
// part that was nowhere on this page until 2026-09-13 -- whether the thing
// it describes is running or only being watched (user ruling: "把前端各个
// 板块前面增加分别是哪一层，分别有什么作用…并凸显是否上线还是影子").
//
// Four states, and the distinction between the last two is not cosmetic:
//   live   the rule is in production and refuses / cuts / lifts for real
//   shadow the lane computes its state and journals it, and changes nothing
//   off    wired, switchable, currently not in the path
//   audit  not a rule at all -- a measurement of what already happened
//
// Keyed on the registry name rather than the heading, like STARRED above,
// so a heading may be reworded without silently losing its lane.  A name
// with no entry renders no strip, which is the honest default: a lane
// nobody has classified must not be given a green light by omission.
const LANES = {
  // --- model: where the probability itself comes from ---
  "偏差订正与权重": {layer:"概率层", status:"live",
    role:"每站每提前量订正多少、四个模型各占多少 → 中心从这里来"},
  "四大模型权重": {layer:"概率层", status:"live",
    role:"服务路径此刻在用的那份配比，不是权重表的行数统计"},
  "模型离散度": {layer:"概率层 · 读数", status:"audit",
    role:"成员之间分歧多大 → 服务宽度的原料"},
  "模型命中率": {layer:"概率层 · 读数", status:"audit",
    role:"订正到底有没有让点预报更准"},
  "OOS 打分": {layer:"概率层 · 读数", status:"audit",
    role:"样本外 CRPS / Brier / NLL 够不够格"},
  "中心/宽度": {layer:"概率层", status:"live",
    role:"中心平移多少、宽度收窄多少 → 服务分布的两个旋钮"},
  "真值离均值多远": {layer:"概率层 · 读数", status:"audit",
    role:"真值实际落在离服务均值几档 → 中心诚不诚实"},
  "模型校准总检": {layer:"概率层 · 读数", status:"audit",
    role:"六层各自及格没有，一屏看完"},
  "H5 半衰期": {layer:"概率层 · 读数", status:"audit",
    role:"单系统校准的偏差半衰期：舰队换完没有、3/5/15 天哪个订正最准"},
  "band 表": {layer:"概率层", status:"live",
    role:"单系统校准与融合之后，服务概率按离中心距离兑现多少 → 按档修正尾形残差（市场不进）"},
  // --- edge: what our probability is worth against the market ---
  "宪章 EV 恒等式": {layer:"边际层 · ① 验收", status:"audit",
    role:"edge = 市场的错 − 模型的错 = Var(e_q) − Cov(e_p,e_q)，量的是服务 p 对 m（p 已含中心/宽度与 band，不含 λ 与 L4；概率空间、整梯）；k 低于 1 才有 edge"},
  "真值落位": {layer:"边际层 · ① 验收", status:"audit",
    role:"真值落在市场价格 × 模型概率的哪一格 → 跟价格走说明该信 m"},
  "有效边际 λ 表": {layer:"边际层 · ② 实盘代替 w", status:"live",
    role:"按「价格 ÷ 声称」分档给 p 打折 → 闸和定注读的都是 λ(a)·声称；没证据时沿用常数继续买"},
  "声称边际体温计": {layer:"边际层 · ② λ 的证据", status:"audit",
    role:"自认便宜多少时真兑现多少 → λ 的证据，不是 λ 本身"},
  "L4 后验层（影子）": {layer:"边际层 · ③ L4", status:"shadow",
    role:"logit b = logit m + w·(logit p − logit m) + c(m)：量 w 与市场自校正 c(m)；没证据时 b = m 不买"},
  "β 车道（影子）": {layer:"边际层 · ③ L4 的特例", status:"shadow",
    role:"m + β(p − m)：L4 取概率空间、去掉 c(m)"},
  // PM_VALUATION_MODE defaults to shadow (valuation_v1) and nothing sets it.
  "统一估值车道": {layer:"边际层 · ③ L4 的特例", status:"shadow",
    role:"(1 − β)m + βp：L4 取概率空间、去掉 c(m)"},
  "双向收敛系数": {layer:"边际层 · ④ 进来前先改 p", status:"shadow",
    role:"模型往上、市场往下、还没走到一起 → 把 p 抬三成再交给上面（只记账）"},
  // --- select: which buckets to seat, and whether seating them paid ---
  "市场方向闸": {layer:"选窗层", status:"shadow",
    role:"只看市场往哪边歪 → 不许逆着它开窗（09-05 上线、09-12 退回影子）"},
  "覆盖 · 价格 · EV": {layer:"选窗层 · 读数", status:"audit",
    role:"实盘决策入座时的覆盖、卖价与事前 EV，对这些单的实际结算"},
  "篮子命中率": {layer:"选窗层 · 读数", status:"audit",
    role:"实盘几桶的篮子实际命中多少，挂出的和买到的各算一遍"},
  // --- sizing: from a seat to a stake, and what is actually held ---
  "分布与持仓分家": {layer:"定注层 · 读数", status:"audit",
    role:"分布说的桶和真正持有的桶差多少"},
  // --- the book's own account ---
  "资金看板": {layer:"实盘 · 读数", status:"audit",
    role:"实盘赚没赚：总账、对比、风险、曲线，订单数量与站明细，意向簿"},
  "站点实盘上线闸门": {layer:"执行层 · 运维", status:"audit",
    role:"开关能不能翻、翻了之后哪几道门还拦着"},
  "每日统计": {layer:"实盘 · 读数", status:"audit",
    role:"实盘每天下了几站、成交多少、结算赚没赚"},
  "逐站逐日明细": {layer:"实盘 · 读数", status:"audit",
    role:"每个站日实盘挂了哪些桶、买到了哪些"},
};
const LANE_WORD = {live:"已上线", shadow:"影子", off:"已关闭", audit:"读数"};
const LANE_TONE = {live:"green", shadow:"amber", off:"red", audit:""};
const LANE_NOTE = {
  live:"生产正在按它执行",
  shadow:"只记账，不改任何决策",
  off:"已接线但当前不在路径上",
  audit:"不是规则，是对已发生的事的测量",
};
const laneStrip = (name) => {
  const L = LANES[name];
  if(!L) return "";
  return `<div class="lane" data-lane="${L.status}">
    <span class="lane-l">${h(L.layer)}</span>
    <span class="lane-r">${h(L.role)}</span>
    <span class="lane-s"><span class="pill ${LANE_TONE[L.status] || ""}">${
      h(LANE_WORD[L.status] || L.status)}</span><span class="muted">${
      h(L.note || LANE_NOTE[L.status] || "")}</span></span></div>`;
};
const STARRED = new Set(["band 表", "声称边际体温计", "有效边际 λ 表", "统一估值车道", "宪章 EV 恒等式"]);
// The strip rides INSIDE the section, above its <h2>, and only when the
// section rendered something: an empty section is guarded out of the page
// on purpose, and a lane strip would put it back as a chip pointing at
// nothing.  The jump bar still reads the heading, which sits below.
const sec = (fn, name, id) => {
  const body = safe(fn, name);
  const lane = body && body.trim() ? LANES[name] : null;
  return `<section class="sec" id="${id}" data-name="${name}"${
    lane ? ` data-lane="${lane.status}"` : ""}${
    STARRED.has(name) ? ` data-star="1"` : ""}>${
    lane ? laneStrip(name) : ""}${body}</section>`;
};

// A sub-heading inside an area: not a section, so it carries no jump chip
// and no guard -- it names the kind of thing the sections under it are.
const subHtml = (item) => `<div class="subgroup" data-lane="${
  item.status || ""}"><div class="subgroup-t">${h(item.sub)}${
  item.status ? ` <span class="pill ${LANE_TONE[item.status] || ""}">${
  h(LANE_WORD[item.status] || item.status)}</span>` : ""}</div>${
  item.note ? `<div class="subgroup-n muted">${h(item.note)}</div>` : ""}</div>`;
const groupHtml = GROUPS.map(g => `<section class="group" data-group="${g.id}"
  style="--tone:${g.tone}">
  <div class="grouptitle">${g.label}<span class="muted">${g.note}</span></div>
  ${g.intro ? safe(g.intro, `${g.label}逻辑图`) : ""}
  ${g.sections.map((item, i) => Array.isArray(item)
    ? sec(item[0], item[1], `sec-${g.id}-${i}`)
    : subHtml(item)).join("")}</section>`).join("");

// The bar is the first thing on the page (user ruling, 2026-09-09).  It was
// already sticky, but sitting under the banner and the live-readiness
// section meant it only became a navigation after the reader had scrolled
// past the two longest blocks on the page -- which is the scroll it exists
// to save.  First element, stuck at zero, visible from the first pixel.
// Live readiness is no longer one of the sections below the bar: it is
// rendered in the head area, directly under the banner that carries the
// LIVE switch (user ruling, 2026-08-31).  The switch and the gates that
// decide whether flipping it does anything are one question, and they were
// split by the tab bar and the overview strip.  It keeps its own .sec
// wrapper, so it still gets a jump chip -- the first one.
// The day-level account, in the order a reader walks it: the strip, the
// per-day table, the per-station-day detail, and then the three tables
// that answer questions the detail raises -- how each station does across
// days, which books were abnormal, and why a station did not seat (user
// ruling, 2026-08-29).  These three used to sit in the market group, two
// scrolls away from the rows they explain.
// The homepage is a dashboard: the health strip, the portfolio tiles and
// their charts, and the day table they summarise (user ruling,
// 2026-08-30).  The four per-station-day tables that used to sit here are
// a different kind of reading -- you go to them with a question, you do
// not scan them -- so they move into their own area together, keeping the
// order the reader asked for: detail first, then the three tables that
// answer what the detail raises.
// 2026-09-16 (user rulings): 逐站汇总（跨日） merged into the go-live
// station table, 没建仓是因为什么 moved inside 逐站逐日明细, and the
// ledger ↔ production guard left the page with the paper readings.
const TOP = [[daily, "每日统计"], [roster, "逐站逐日明细"]];
// The area above the tabs is the fifth kind of reading and wears a title
// and a colour like the four below it; the jump bar paints its chips in the
// same colour.
const TOP_AREA = {label:"实盘", tone:"#7ee787",
  note:"打开页面先看：资金、站点上线闸门、每日统计、逐站逐日明细"};

// Which sections on this page were reused from the previous build, and how
// old each one is.  A cached section that quietly stops refreshing is the
// failure this whole mechanism can cause, so its age is printed rather than
// left for someone to infer from a number that stopped moving.
function sectionCacheNote(){
  const c=D.section_cache||{};
  const NAME={truth_landing:"真值落点",window_divergence:"窗口分歧",
    scorecard:"校准计分卡",h5_readiness:"H5 就绪",skill:"模型技巧",
    bias_half_life:"半衰期对比",band_residual_audit:"band 残差审计",
    center_landing:"真值离均值多远"};
  const reused=Object.entries(c).filter(([,v])=>v&&v.reused);
  if(!reused.length){
    const built=Object.keys(c).length;
    return built?`本轮全部区块现算 · `:"";
  }
  const worst=Math.max(...reused.map(([,v])=>Number(v.age_sec)||0));
  const list=reused.map(([k,v])=>
    `${NAME[k]||k} ${Math.round((Number(v.age_sec)||0)/60)} 分钟前`).join("、");
  return `<span title="慢变区块按各自节奏重算，其余每轮现算">沿用上一轮的区块：`
    + `${h(list)}（最旧 ${Math.round(worst/60)} 分钟）</span> · `;
}

document.getElementById("app").innerHTML =
  `<div class="topnav" id="topnav">
    <div class="navtop">
      <button type="button" class="nav-home" id="nav-home"
        title="回到页首：总灯、LIVE 开关与三排状态灯"><svg viewBox="0 0 16 16"
        fill="none" stroke="currentColor" stroke-width="1.6"
        stroke-linejoin="round" aria-hidden="true"><path
        d="M2.5 7.2 8 2.6l5.5 4.6V13a.9.9 0 0 1-.9.9H9.8V10H6.2v3.9H3.4a.9.9 0 0 1-.9-.9z"/></svg>首页</button>
      <span class="nav-div"></span>
      ${tabs}<span class="nav-fill"></span>${safe(liveControlStrip, "实盘控制")}
      <span id="nav-theme"></span></div>
    <nav class="jump" id="jump" aria-label="区块"></nav></div>` +
  safe(head, "页头") +
  `<section class="toparea" style="--tone:${TOP_AREA.tone}">
    <div class="grouptitle">${TOP_AREA.label}<span class="muted">${TOP_AREA.note}</span></div>` +
  sec(moneyBoard, "资金看板", "sec-money") +
  sec(liveReadiness, "站点实盘上线闸门", "sec-live") +
  TOP.map(([fn, name], i) => sec(fn, name, `sec-top-${i}`)).join("") +
  `</section>` +
  groupHtml +
  `<footer>数据生成于 ${h(D.generated_at_utc)} · 只读页面，不写任何数据 ·
    ${sectionCacheNote()}
    <a href="#" id="footer-top">返回顶部</a></footer>`;

// Which group is showing survives a reload, because the page is reloaded
// far more often than the question being asked of it changes.  "全部" is
// the old single-column page, one click away, for anyone reading it top to
// bottom or searching it with the browser's own find.
(function(){
  const nav = document.getElementById("tabs");
  if(!nav) return;
  let current = GROUPS[0].id;
  try{
    const saved = localStorage.getItem("board-tab");
    if(saved) current = saved;
  }catch(e){}
  const known = new Set(GROUPS.map(g=>g.id).concat(["all"]));
  if(!known.has(current)) current = GROUPS[0].id;
  const jump = document.getElementById("jump");
  const bar = document.getElementById("topnav");
  const esc = (t) => String(t).replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;").replace(/</g, "&lt;");
  const home = document.getElementById("nav-home");
  const chip = (node) => {
    if(!node.firstElementChild) return "";
    const head2 = node.querySelector("h2");
    const full = head2
      ? head2.textContent.trim().replace(/\s+/g, " ")
      : (node.dataset.name || "");
    if(!full) return "";
    const short2 = (node.dataset.name || "").split(/[（(]/)[0].trim()
      || full.split(/[：（(·]/)[0].trim() || full;
    // The lane dot rides the chip too (user ruling, 2026-09-13): the bar is
    // where a reader picks a section, so "is this one live or shadow" has
    // to be answerable before the click, not after it.
    const lane = node.dataset.lane;
    const laneWord = {live:"已上线", shadow:"影子", off:"已关闭"}[lane];
    return `<button type="button" data-goto="${node.id}" title="${
      esc(full + (laneWord ? " · " + laneWord : ""))}"${
      lane ? ` data-lane="${esc(lane)}"` : ""}${
      node.dataset.star ? ` class="star"` : ""}>${esc(short2)}</button>`;
  };
  // One run of chips per area, behind the area's caption, wrapping as a unit.
  const run = (label, tone, nodes) => {
    const chips = nodes.map(chip).filter(Boolean);
    if(!chips.length) return "";
    return `<span class="jump-area"><span class="jump-g"${
      tone ? ` style="--tone:${esc(tone)}"` : ""}>${esc(label)}</span>
      <span class="jump-run">${chips.join("")}</span></span>`;
  };
  const paintJump = () => {
    if(!jump) return;
    // "实盘", not "账本": every section above the tabs reads the live book
    // since 2026-09-16, and "账本" read as the paper ledger.
    const runs = [run(TOP_AREA.label, TOP_AREA.tone,
        [...document.querySelectorAll(".toparea > .sec")])]
      .concat([...document.querySelectorAll(".group:not([hidden])")].map(g => {
        const G = GROUPS.find(x => x.id === g.dataset.group) || {};
        return run(G.short || G.label || "", G.tone || "",
          [...g.querySelectorAll(".sec")]);
      }));
    jump.innerHTML = runs.filter(Boolean).join("");
    spy();
  };
  // Which section is under the bar: the last one whose top has scrolled up
  // past it.  An area's title counts as a boundary -- once it is under the
  // bar the reader has left the section above it, even before the area's
  // first section arrives, which is exactly where a tab click lands.  Above
  // the first section the reader is at the page head, and the home button
  // is the one lit.
  let spyQueued = false;
  function spy(){
    spyQueued = false;
    if(!jump) return;
    const line = (bar ? bar.getBoundingClientRect().bottom : 0) + 24;
    let here = null, area = null, passed = false;
    jump.querySelectorAll("button[data-goto]").forEach(b => {
      const node = document.getElementById(b.dataset.goto);
      if(!node || !node.offsetParent) return;
      const group = node.closest(".group");
      if(group && group !== area){
        area = group;
        if(group.getBoundingClientRect().top <= line){ here = null; passed = true; }
      }
      if(node.getBoundingClientRect().top <= line){ here = b; passed = true; }
    });
    jump.querySelectorAll('[aria-current="true"]').forEach(b => {
      if(b !== here) b.removeAttribute("aria-current");
    });
    if(here) here.setAttribute("aria-current", "true");
    if(home) home.dataset.here = passed ? "" : "1";
  }
  window.addEventListener("scroll", () => {
    if(spyQueued) return;
    spyQueued = true;
    requestAnimationFrame(spy);
  }, {passive: true});
  if(home) home.addEventListener("click", () => window.scrollTo(0, 0));
  // Land the heading below the sticky bar instead of underneath it.
  const goTo = (node) => {
    if(!node) return;
    const offset = (bar ? bar.offsetHeight : 0) + 10;
    const y = node.getBoundingClientRect().top + window.scrollY - offset;
    window.scrollTo(0, Math.max(0, y));
  };
  const paint = () => {
    document.querySelectorAll(".group").forEach(sec => {
      sec.hidden = current !== "all" && sec.dataset.group !== current;
    });
    nav.querySelectorAll("button").forEach(b => {
      b.dataset.active = b.dataset.tab === current ? "1" : "";
    });
    paintJump();
  };
  if(jump) jump.addEventListener("click", (ev) => {
    const button = ev.target.closest("button[data-goto]");
    if(!button) return;
    goTo(document.getElementById(button.dataset.goto));
  });
  // The edge map's nodes land on their sections.
  document.addEventListener("click", (ev) => {
    const node = ev.target.closest("[data-goto-name]");
    if(!node) return;
    const name = node.dataset.gotoName;
    const found = [...document.querySelectorAll(".group:not([hidden]) .sec")]
      .find(s => s.dataset.name === name);
    goTo(found);
  });
  nav.addEventListener("click", (ev) => {
    const button = ev.target.closest("button[data-tab]");
    if(!button) return;
    current = button.dataset.tab;
    try{ localStorage.setItem("board-tab", current); }catch(e){}
    paint();
    // Switching groups from the top of the page used to change something
    // two thousand pixels below the fold and look like it did nothing.
    if(current !== "all") goTo(document.querySelector(
      `.group[data-group="${current}"]`));
  });
  paint();
})();

// Per-day tab strips inside a card.  Scoped to the card that carries the
// strip, so a second one on the page does not paint the first one's panes.
// A card marked data-remember keeps its choice across reloads, the way the
// group bar does -- the money board's categories are one of those.
document.querySelectorAll("[data-daytabs]").forEach(card => {
  const nav = card.querySelector(".daytabs");
  if(!nav) return;
  const key = "pane-" + card.dataset.daytabs;
  const show = (day) => {
    nav.querySelectorAll("button[data-day]").forEach(b => {
      b.dataset.active = b.dataset.day === day ? "1" : "";
    });
    card.querySelectorAll("[data-daypane]").forEach(pane => {
      pane.hidden = day !== "all" && pane.dataset.daypane !== day;
    });
    card.dataset.dayall = day === "all" ? "1" : "";
  };
  nav.addEventListener("click", (ev) => {
    const button = ev.target.closest("button[data-day]");
    if(!button) return;
    show(button.dataset.day);
    if(card.dataset.remember){
      try{ localStorage.setItem(key, button.dataset.day); }catch(e){}
    }
  });
  if(card.dataset.remember){
    try{
      const saved = localStorage.getItem(key);
      if(saved && nav.querySelector(`button[data-day="${CSS.escape(saved)}"]`)) show(saved);
    }catch(e){}
  }
});
// A link to something behind an unchosen tab opens that tab first, so the
// browser's own jump lands on it instead of on a hidden element.
document.addEventListener("click", (ev) => {
  const link = ev.target.closest('a[href^="#"]');
  if(!link) return;
  const target = document.getElementById(link.getAttribute("href").slice(1));
  const pane = target && target.closest("[data-daypane]");
  if(!pane || !pane.hidden) return;
  const card = pane.closest("[data-daytabs]");
  const button = card && card.querySelector(
    `.daytabs button[data-day="${CSS.escape(pane.dataset.daypane)}"]`);
  if(button) button.click();
});

// Back to the top from the end of the page.  The floating "↑ 顶部" button
// left on 2026-09-16: 首页 in the sticky bar does the same from anywhere,
// and a second way to one place was one more thing on screen.
(function(){
  const link = document.getElementById("footer-top");
  if(link) link.addEventListener("click", (ev) => {
    ev.preventDefault();
    window.scrollTo(0, 0);
  });
})();

document.querySelectorAll("table").forEach(makeSortable);
lcInit();

// Prose folds away by default (user ruling, 2026-08-18).  Forty-six
// explanation blocks is most of the page's height, and every one of them is
// something a reader needs once and then never again -- while the numbers
// they explain are what the page is opened for.  Done here rather than in
// each section because the rule is "all of it", and forty-six hand-written
// wrappers is forty-six chances to miss one.
//
// Consecutive blocks share a fold: three separate "说明" toggles in a row
// is the same clutter in a smaller font.
document.querySelectorAll(".why,.sub").forEach(node=>{
  // Only fold prose.  A block holding a table or a card is not an
  // explanation that ran long -- it is a mis-closed tag that swallowed the
  // section, and folding it hides the numbers instead of the words.  Section
  // six closed a `div.why` with `</p>`, so its whole daily table lived
  // inside the explanation and disappeared behind the toggle.  The tag is
  // fixed; this makes the next one cost a stray blue box rather than a
  // section.
  if(node.querySelector("table,.card")) return;
  const prev=node.previousElementSibling;
  if(prev&&prev.classList.contains("note")){prev.appendChild(node);return;}
  const box=document.createElement("details");
  box.className="note";
  box.innerHTML="<summary>说明</summary>";
  node.parentNode.insertBefore(box,node);
  box.appendChild(node);
});

// The button lives in the static body rather than in a rendered section, so
// it still works on a page where every section failed to draw -- which is
// exactly when a reader is most likely to be squinting at it.
(function(){
  const button = document.getElementById("theme");
  // Into the bar once the bar exists; a page whose render failed has no
  // slot, and the button stays in its corner.
  const slot = document.getElementById("nav-theme");
  if(slot) slot.replaceWith(button);
  const paint = () => {
    const dark = document.documentElement.dataset.theme !== "light";
    const docked = button.parentElement !== document.body;
    button.textContent = dark ? (docked ? "☀" : "☀ 亮色")
                              : (docked ? "☾" : "☾ 暗色");
    button.title = dark ? "切换到亮色" : "切换到暗色";
    button.setAttribute("aria-label", button.title);
  };
  button.addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme !== "light";
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try{ localStorage.setItem("board-theme", dark ? "light" : "dark"); }
    catch(e){}
    paint();
  });
  paint();
})();

// Hot update (user ruling, 2026-09-16: 前端改为热更新).  Every save of the
// template redraws the page file in a second (``reinflate``) and writes its
// version beside it as a script, because a page opened from file:// may not
// fetch but may still load a script.  The open page asks every ten seconds;
// a newer version reloads it where the reader was.  Not while a control
// request is in flight -- a reload would drop the answer the switch is
// waiting for -- and not on a copy served from anywhere but this machine.
(function(){
  const KEY = "board-hot-scroll";
  try{
    const saved = JSON.parse(sessionStorage.getItem(KEY) || "null");
    if(saved && saved.path === location.pathname){
      sessionStorage.removeItem(KEY);
      window.addEventListener("load", () => window.scrollTo(0, saved.y));
      setTimeout(() => window.scrollTo(0, saved.y), 0);
    }
  }catch(e){}
  const local = location.protocol === "file:"
    || location.hostname === "127.0.0.1" || location.hostname === "localhost";
  if(!local || !/\.html?$/.test(location.pathname)) return;
  const file = location.pathname.split("/").pop().replace(/\.html?$/, "_version.js");
  let asking = false;
  // Hidden tabs ask too: a tab reloaded in the background is already new
  // when the reader comes back to it, and coming back asks at once.
  const ask = () => {
    if(asking) return;
    asking = true;
    const probe = document.createElement("script");
    const done = () => { asking = false; probe.remove(); };
    probe.onerror = done;
    probe.onload = () => {
      done();
      const seen = Number(window.__BOARD_VERSION__);
      if(!(seen > Number(PAGE_STAMP) + 0.0005)) return;
      if(typeof lcBusy !== "undefined" && lcBusy > 0) return;
      try{
        sessionStorage.setItem(KEY, JSON.stringify(
          {path: location.pathname, y: window.scrollY}));
      }catch(e){}
      location.reload();
    };
    probe.src = file + "?t=" + Date.now();
    document.head.appendChild(probe);
  };
  setInterval(ask, 10000);
  document.addEventListener("visibilitychange", () => {
    if(!document.hidden) ask();
  });
})();
</script></body></html>"""


#: The build's own start time, written into the first line of the document.
#: The guard below compares these rather than file mtimes; see it for why.
RENDER_STAMP_RE = re.compile(r"<!--render-start:([0-9.]+)-->")
PAYLOAD_STAMP_RE = re.compile(r"<!--payload-start:([0-9.]+)-->")


def render_html(
    payload: dict[str, Any],
    started_at: float | None = None,
    payload_started_at: float | None = None,
) -> str:
    """The page: this template around ``payload``, with both vintages stamped.

    ``started_at`` is the page's code vintage -- when the template it was
    drawn with was read -- and ``payload_started_at`` the data's, when the
    build that produced the payload began.  A full render has one moment for
    both; a hot update (``reinflate``) draws a new template around the last
    payload, so the two part company and the page says so.
    """

    page = time.time() if started_at is None else started_at
    data = page if payload_started_at is None else payload_started_at
    return HTML_TEMPLATE.replace(
        "__PAYLOAD__", json.dumps(payload, ensure_ascii=False)
    ).replace(
        "__RENDER_STARTED__", f"{page:.3f}"
    ).replace(
        "__PAYLOAD_STARTED__", f"{data:.3f}"
    )


def _head_stamp(target: Path, pattern: re.Pattern[str]) -> float | None:
    """A stamp from the first line of a rendered page, if the page carries it.

    Only the first line is read: the stamps sit before anything else in the
    document precisely so this check never touches the megabytes behind it.
    """

    try:
        with target.open("r", encoding="utf-8") as handle:
            head = handle.read(300)
    except OSError:
        return None
    match = pattern.search(head)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _render_started_at(target: Path) -> float | None:
    """When the template on this page was read."""

    return _head_stamp(target, RENDER_STAMP_RE)


def _payload_started_at(target: Path) -> float | None:
    """When the build behind this page's data began.

    A page written before the data stamp existed was a full render, whose
    template and data share one start, so its render stamp stands in.
    """

    stamp = _head_stamp(target, PAYLOAD_STAMP_RE)
    return _render_started_at(target) if stamp is None else stamp


def _newer_payload_landed(target: Path, payload_started_at: float) -> bool:
    """Whether the page on disk carries data from a build newer than this one.

    A full render takes twenty minutes and two run on different clocks, so
    the guard's job is that an older build never puts older numbers back on
    the page.  It compared code vintages until 2026-09-16, because a render
    used to draw its page with whatever template it had imported when it
    started -- a sweep that began before an edit put pre-edit HTML back
    (2026-08-26), and comparing write times instead let the older process
    win whenever it finished last (2026-09-09).  Since the hot update every
    page is drawn with the template on disk at the moment it is written, so
    the code can no longer regress and only the data vintage is left to
    guard.  Files with no stamp at all fall back to the mtime test, which is
    right for them: an unstamped file is older than any of this.
    """

    try:
        existing_mtime = target.stat().st_mtime
    except OSError:
        return False
    landed = _payload_started_at(target)
    if landed is None:
        return existing_mtime > payload_started_at
    # The page's stamp is written to the millisecond, the sidecar keeps the
    # full float: read at one precision, or a build's own page reads as
    # newer than its own payload whenever the rounding went up -- 698.590
    # against 698.5898612 on 2026-09-17, which refused every template save
    # until the next full render.
    return round(landed, 3) > round(payload_started_at, 3)


def _payload_sidecar(target: Path) -> Path:
    """The last full build's payload, beside the page it was drawn into."""

    return target.with_name(target.stem + "_payload.json")


def _version_sidecar(target: Path) -> Path:
    """The page's version, as a script the open page can poll from file://."""

    return target.with_name(target.stem + "_version.js")


def _atomic_write(path: Path, text: str) -> None:
    """Write beside, then rename: a browser reloading mid-write never reads
    half a page."""

    scratch = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    scratch.write_text(text, encoding="utf-8")
    os.replace(scratch, path)


def _save_payload(
    target: Path, payload: dict[str, Any], started_at: float
) -> bool:
    """Keep this build's payload for hot updates, unless a newer one is kept.

    The build start is serialised first so the check reads a few bytes, not
    the whole payload.
    """

    sidecar = _payload_sidecar(target)
    try:
        with sidecar.open("r", encoding="utf-8") as handle:
            head = handle.read(80)
        match = re.search(r'"build_started_at":\s*([0-9.]+)', head)
        if match and float(match.group(1)) > started_at:
            return False
    except OSError:
        pass
    _atomic_write(sidecar, json.dumps(
        {"build_started_at": started_at, "payload": payload},
        ensure_ascii=False,
    ))
    return True


def _inline_script_error(html: str) -> str | None:
    """Why the page's inline script would not run, or None.

    A hot update fires on every save of this file, including a save in the
    middle of an edit.  A template whose script does not parse draws a blank
    page, so it never reaches the page on disk; the previous page stays up
    and the next save tries again.  Without node there is nothing to check
    with, and the page is written as before.
    """

    node = shutil.which("node")
    if node is None:
        return None
    bodies = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    with tempfile.TemporaryDirectory() as scratch:
        for index, body in enumerate(bodies):
            script = Path(scratch) / f"inline_{index}.js"
            script.write_text(body, encoding="utf-8")
            done = subprocess.run(
                [node, "--check", str(script)],
                capture_output=True, text=True, timeout=60,
            )
            if done.returncode != 0:
                lines = [line for line in done.stderr.splitlines() if line.strip()]
                return f"inline script #{index}: " + " | ".join(lines[-3:])
    return None


def _payload_in_page(target: Path) -> tuple[dict[str, Any], float] | None:
    """The payload a rendered page was drawn with, and its data vintage.

    Decoded with the JSON parser from where the template put it, rather than
    scanned for a closing brace: section labels contain braces, and a scan
    that stops at the first one cuts the payload short.
    """

    try:
        html = target.read_text(encoding="utf-8")
    except OSError:
        return None
    marker = HTML_TEMPLATE.split("__PAYLOAD__", 1)[0].rsplit("\n", 1)[-1]
    start = html.find(marker)
    stamp = _payload_started_at(target)
    if start < 0 or stamp is None:
        return None
    try:
        payload, _end = json.JSONDecoder().raw_decode(html, start + len(marker))
    except ValueError:
        return None
    return (payload, stamp) if isinstance(payload, dict) else None


def _renderer_on_disk() -> Any:
    """``render_html`` as this file reads NOW, not as this process imported it.

    A full render imports the template when it starts and finishes twenty
    minutes later; any edit in between is on disk and not in memory.  When
    the source is unchanged this process's own function is it.
    """

    source = Path(__file__)
    try:
        current = source.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return render_html
    if current == _LOADED_SOURCE_LINES:
        return render_html
    spec = importlib.util.spec_from_file_location(
        "_calibration_dashboard_on_disk", source
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.render_html


def reinflate(target: Path, renderer: Any | None = None) -> str:
    """Hot update: the template on disk around the last full build's payload.

    User ruling, 2026-09-16: 前端改为热更新.  The page is data and template
    in one file, and the data takes twenty minutes to build, so a change to
    a section's HTML used to wait for the next full render to be seen.  The
    template alone draws in a second.  A launchd watch on this file runs
    this on every save, and every full render ends with it too, so the page
    on disk always carries the newest template around the newest payload.

    A section that needs a payload field the last build did not have draws
    its empty state until the next full render supplies it.
    """

    sidecar = _payload_sidecar(target)
    try:
        blob = json.loads(sidecar.read_text(encoding="utf-8"))
        payload = blob["payload"]
        payload_started = float(blob["build_started_at"])
    except (OSError, ValueError, KeyError, TypeError):
        # No saved payload yet -- the page was drawn before builds kept one.
        # The page carries its own, verbatim, so read it back out.
        recovered = _payload_in_page(target)
        if recovered is None:
            return f"{target}: skipped, no saved payload at {sidecar.name}"
        payload, payload_started = recovered
    if _newer_payload_landed(target, payload_started):
        return f"{target}: skipped, the page carries newer data than {sidecar.name}"
    now = time.time()
    render = renderer or _renderer_on_disk()
    html = render(payload, started_at=now, payload_started_at=payload_started)
    error = _inline_script_error(html)
    if error is not None:
        return f"{target}: skipped, the template does not parse -- {error}"
    _atomic_write(target, html)
    _atomic_write(
        _version_sidecar(target),
        f'window.__BOARD_VERSION__ = "{now:.3f}";\n',
    )
    return str(target)


def serve(db: Path, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api"):
                body = json.dumps(
                    build_payload(db), ensure_ascii=False
                ).encode()
                content_type = "application/json; charset=utf-8"
            else:
                body = render_html(build_payload(db)).encode()
                content_type = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"气温篮子全链看板: http://127.0.0.1:{port}/  (Ctrl-C 停止)")
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--html-out", type=Path)
    parser.add_argument(
        "--reinflate",
        type=Path,
        help=(
            "hot update: redraw this page with the template on disk around"
            " the payload its last full render saved, in seconds"
        ),
    )
    parser.add_argument(
        "--no-section-cache",
        action="store_true",
        help=(
            "recompute every section against this instant instead of"
            " reusing the slow-moving ones from data/dashboard_section_cache"
        ),
    )
    args = parser.parse_args()
    global SECTION_CACHE_ENABLED
    SECTION_CACHE_ENABLED = not args.no_section_cache
    if args.reinflate:
        print(reinflate(args.reinflate, renderer=render_html))
        return 0
    database = args.db.expanduser().resolve(strict=True)
    if args.json:
        print(json.dumps(
            build_payload(database), ensure_ascii=False, sort_keys=True
        ))
        return 0
    if args.html_out:
        # Build the payload, keep it, then draw the page with the template
        # as it is on disk NOW (user ruling 2026-09-16, hot update).  A build
        # takes twenty minutes and an edit landing inside them used to be
        # thrown away or to throw this build away (see
        # ``_newer_payload_landed``); drawing at the end with the current
        # template means neither happens, and the payload stays behind for
        # the watch to redraw on the next save.
        started_at = time.time()
        payload = build_payload(database)
        target = args.html_out
        if not _save_payload(target, payload, started_at):
            print(
                f"{target}: skipped, a newer build's payload landed while "
                "this one was building"
            )
            return 0
        print(reinflate(target))
        return 0
    serve(database, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
