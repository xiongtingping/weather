"""One station's two live days, read for the board on demand.

User, 2026-10-05: 「前端增加zuuu单站点每日的概率分布（含模型run及权重、概率的分布
（温度竖）、概率的均值桶、众数桶，对比市场的均值桶、众数桶，以及p0、p0m、p1三阶段
的概率变化）以及温度桶概率变化的趋势。只显示第二日和今日。包括taf、metar、辐射偏
移、云雨等信息。这个区域增加刷新和渲染的频率，增加自动刷新和一键点击刷新」，并
「以及均值、方差、离散度等」.

A reader: everything here is what production already stored -- the committed
envelopes and their lineage receipts (model runs, weights, the radiation
shift, the TAF and METAR receipts), the official CLOB WS snapshots, the
archive's reports, the TAF captures and the hourly precipitation versions.
Nothing is fitted, compiled or re-judged.  The one production function
called is ``market_reference_v1`` -- the market vector rule itself, applied
to the stored books, so the market's mode here is the one charter 249 trades.

Served by ``scripts/live_control_server.py`` at ``GET /api/station-day``:
one station, two event days, a couple of seconds of short read-only reads,
so the board can poll it every minute instead of waiting for the page render.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
ARCHIVE_DB = ROOT / "data" / "archive.db"
TAF_INBOX_PATH = ROOT / "data" / "bayes_basket_target_taf_revision_inbox.json"
# The fleet's one list of cities, and their display names (user 2026-10-10
# 「增加所有站，按照区域分区，比如欧洲、美国、亚洲等」).
CITY_CONTRACT_PATH = ROOT / "config" / "trading" / "awc_truth_ingress_v1.json"
CITY_NAMES_PATH = ROOT / "config" / "ui" / "station_city_names_v1.json"
REGIONS = ("中国", "亚洲", "欧洲", "美国", "美洲", "非洲·大洋洲")
#: The member packets production compiled each P0 from, one file per packet,
#: named by the first 16 hex of the snapshot sha the envelope's lineage keeps.
FORWARD_PACKET_ARCHIVE = ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "forward_packet_archive"
SCHEMA = "station_day_board_v1"
#: Charter 266's direction signal, as the resident's executor last wrote it
#: for the whole fleet (user 2026-10-10 「在前端"站点看板"的"辐射偏移（宪章
#: 222）"增加各个站点的方向信息」) -- read, never computed here.
DIRECTION_BOARD_PATH = ROOT / "data" / "runtime" / "direction_signal_board_v1.json"
_DIRECTION_CACHE: dict[str, Any] = {}
#: The direction card's 「最新」 badge (user 2026-10-13 「增加一个数据是否最新的
#: 徽标」, after 「为什么更新是3h以前」 over a card that was simply between
#: runs).  Hours after its init by which each momentum component's run is
#: normally in hand -- measured at ZUUU over the 10-07..10-12 runs: AIFS ENS
#: 6.0-7.1, IFS ENS 7.2-8.0, the HRES relay 6.0-6.5 (relay outages to 19),
#: AIFS single 5.5-5.9 -- a run not in hand past its hour reads late.  The
#: snapshot is rewritten every ten minutes; past 25 it reads stalled.
#: Display only: nothing downstream reads either number.
DIRECTION_RUN_DUE_HOURS = {"aifs_ens": 7.5, "ifs_ens": 8.5, "hres": 7.0, "aifs_single": 6.5}
DIRECTION_SNAPSHOT_STALE_SEC = 1500.0
#: Charter 267's listing-time forecast F, as the resident last wrote it every
#: ten minutes (user 2026-10-13 「增加目前已接入的"确定性预报模型"的数据」, on
#: the P0 card) -- read, never computed here.
LISTING_F_PATH = ROOT / "data" / "runtime" / "listing_forecast_f_v1.json"
_LISTING_F_CACHE: dict[str, Any] = {}
STAGES = ("P0", "P0-M", "P1")
#: One market point per this many minutes on the trend chart (plus the newest).
MARKET_TREND_STEP_MIN = 15
#: The newest P1 steps kept on the trend (a replayed day has hundreds).
P1_TREND_MAX = 160
#: Taker fee rate the calculator offers by default (strategy_lanes_v1's
#: tail-short ``taker_fee_rate``; the venue's fee is rate x p x (1-p) a share).
DEFAULT_TAKER_FEE_RATE = 0.05


def _utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any, default: Any) -> Any:
    if value in (None, "", "NONE"):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


# ---------------------------------------------------------------------------
# The ladder and the shape of a vector on it.


def ladder_axis(ordinals: Mapping[str, Any], intervals: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each rung with its °C bounds and representative temperature, lowest first.

    The representative temperatures are production's own
    (``center_width_calibration.bucket_centers_from_intervals`` at
    ``center_width_store.EDGE_WIDTH_C``, the axis the P0-M and P1 stage
    anchors weight): a finite rung at its midpoint, an open edge 1°C inside
    its bound.  User 2026-10-09「统一」: the board had put the edges half a rung
    inside, so its 分布均值 read 21.67 beside the served centre 21.66 on the
    same P0-M vector.  An axis production could not place has no centres.
    """

    from .center_width_calibration import bucket_centers_from_intervals  # noqa: PLC0415
    from .center_width_store import EDGE_WIDTH_C  # noqa: PLC0415

    order = sorted(ordinals, key=lambda b: int(ordinals[b]))
    bounds = {}
    for b in order:
        lo, hi = (list(intervals.get(b) or [None, None]) + [None, None])[:2]
        bounds[b] = (_float(lo), _float(hi))
    try:
        centres = bucket_centers_from_intervals(
            {b: list(v) for b, v in bounds.items()}, ordered_keys=order, edge_width_c=EDGE_WIDTH_C)
    except (TypeError, ValueError):
        centres = [None] * len(order)
    return [{"bucket": str(b), "ordinal": int(ordinals[b]), "lo_c": bounds[b][0], "hi_c": bounds[b][1],
             "centre_c": None if c is None else round(float(c), 4)} for b, c in zip(order, centres)]


def bucket_of(axis: Sequence[Mapping[str, Any]], celsius: float | None) -> str | None:
    if celsius is None:
        return None
    for row in axis:
        lo, hi = row["lo_c"], row["hi_c"]
        if (lo is None or celsius >= lo) and (hi is None or celsius < hi):
            return row["bucket"]
    return None


def skew_floor(mode: str | None, axis: Sequence[Mapping[str, Any]], running_high_c: float | None) -> bool:
    """A 「下界日」 (user 2026-10-16 「把众数压在已测下界上的站日单列为「下界日」，标签写
    「下界·待涨」，不计入左偏/右偏判对率」): the vector's mode is the rung holding the
    running observed high, so no rung below the mode can still settle and any
    lean the skew reading names is right by construction.  The one rule the
    board's card, the fleet tally and the settled skew reading all call."""

    if not mode or running_high_c is None:
        return False
    return bucket_of(axis, float(running_high_c)) == str(mode)


def _lineage_running_high(lineage: Mapping[str, Any], stage: str) -> float | None:
    """The running observed high a P0-M or P1 envelope was decided on, from
    its own lineage; None for P0 (no observation stage)."""

    key = {"P0-M": "observed_running_high_c", "P1": "reported_high_c"}.get(stage)
    return _float(lineage.get(key)) if key else None


def vector_stats(vector: Mapping[str, float], axis: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Mean, variance, spread and the two summary rungs of a ladder vector.

    ``mode`` is the largest rung, the lower on a tie (the rule charter 249's
    market mode uses); ``mean_bucket`` is production's centre bucket
    (``mean_center_bucket_v1``, charter 75): the rung nearest the
    probability-weighted mean ordinal, ties to the heavier rung.
    ``entropy_bits`` and the 80 % central interval are the dispersion beside
    the standard deviation: the first counts rungs in play regardless of
    their order, the second is the span a reader would quote.
    """

    rows = [(r, float(vector.get(r["bucket"], 0.0) or 0.0)) for r in axis]
    mass = sum(p for _r, p in rows)
    if not (mass > 0.0):
        return None
    rows = [(r, p / mass) for r, p in rows]
    known = [(r["centre_c"], p) for r, p in rows if r["centre_c"] is not None]
    mean = sum(c * p for c, p in known) / (sum(p for _c, p in known) or 1.0)
    var = sum(p * (c - mean) ** 2 for c, p in known) / (sum(p for _c, p in known) or 1.0)
    mode_index = max(range(len(rows)), key=lambda i: (rows[i][1], -i))
    entropy = -sum(p * math.log2(p) for _r, p in rows if p > 0.0)
    cum = 0.0
    lo80 = hi80 = None
    for r, p in rows:
        cum += p
        if lo80 is None and cum >= 0.10:
            lo80 = r["bucket"]
        if hi80 is None and cum >= 0.90:
            hi80 = r["bucket"]
    mode_ord = rows[mode_index][0]["ordinal"]
    near_mode = sum(p for r, p in rows if abs(r["ordinal"] - mode_ord) <= 1)
    third = sum(p * (c - mean) ** 3 for c, p in known)
    sd = math.sqrt(var)
    from .basket_kernel.support import mean_center_bucket_v1, mean_mode_skew_reading_v1  # noqa: PLC0415

    # The skew is production's own reading (charters 55/75/131), by
    # probability rather than by rung identity (user 2026-10-11 「不要按照均值
    # 桶和众数桶为一个桶统计，而是按照概率来区分」): the probability-weighted
    # mean position minus the modal rung, read as a lean only past 0.35 sigma.
    skew = mean_mode_skew_reading_v1({r["bucket"]: p for r, p in rows},
                                     {r["bucket"]: r["ordinal"] for r, _p in rows})
    return {
        "mean_c": round(mean, 3), "var_c2": round(var, 4), "sd_c": round(sd, 3),
        "skew": None if sd <= 1e-9 else round(third / sd ** 3, 3),
        "mode": rows[mode_index][0]["bucket"], "mode_p": round(rows[mode_index][1], 4),
        # Production's centre bucket (user 2026-10-11 「均值桶也按照生产口径来计
        # 算」), not the rung the °C mean falls in: they part on 0.7 % of
        # vectors (a mean on a rung edge, an open edge's centre on F ladders).
        "mean_bucket": mean_center_bucket_v1({r["bucket"]: p for r, p in rows},
                                             {r["bucket"]: r["ordinal"] for r, _p in rows}),
        "skew_sign": int(skew["sign"]) if skew["readable"] else None,
        "skew_gap_buckets": skew["gap_buckets"],
        "skew_threshold_buckets": skew["threshold_buckets"],
        "entropy_bits": round(entropy, 3),
        "effective_rungs": round(2.0 ** entropy, 2),
        "interval80": [lo80, hi80],
        "mass_mode_pm1": round(near_mode, 4),
    }


def _norm(vector: Mapping[str, Any]) -> dict[str, float]:
    clean = {str(k): float(v) for k, v in vector.items() if _float(v) is not None}
    total = sum(clean.values())
    return {k: v / total for k, v in clean.items()} if total > 0 else clean


# ---------------------------------------------------------------------------
# Reads.


def station_timezone(conn: sqlite3.Connection, station: str) -> str | None:
    row = conn.execute(
        "SELECT settlement_timezone FROM calibration_station_market_timezone_binding"
        " WHERE station_id=? LIMIT 1", (station,),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _region(station: str, tz_name: str) -> str:
    """The picker's group, from the settlement timezone: China apart from the
    rest of Asia, the US (ICAO K) apart from the rest of the Americas."""

    if tz_name == "Asia/Shanghai":
        return "中国"
    if tz_name.startswith("America/"):
        return "美国" if station.startswith("K") else "美洲"
    return {"Asia": "亚洲", "Europe": "欧洲"}.get(tz_name.split("/")[0], "非洲·大洋洲")


def fleet(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every station the settlement-source contract lists, grouped for the
    picker and ordered east to west inside a group; [] when unreadable."""

    try:
        cities = (json.loads(CITY_CONTRACT_PATH.read_text(encoding="utf-8"))
                  .get("settlement_source_contract") or {}).get("cities") or {}
    except (OSError, ValueError):
        return []
    try:
        names = json.loads(CITY_NAMES_PATH.read_text(encoding="utf-8")).get("names") or {}
    except (OSError, ValueError):
        names = {}
    now = now or datetime.now(timezone.utc)
    out = []
    for slug, entry in cities.items():
        station = str((entry or {}).get("station_id") or "") if isinstance(entry, Mapping) else ""
        tz_name = station_timezone(conn, station) if station else None
        if not tz_name:
            continue
        out.append({"station_id": station, "city_slug": slug,
                    "name": names.get(slug) or slug.replace("-", " ").title(),
                    "region": _region(station, tz_name),
                    "utc_offset_h": now.astimezone(ZoneInfo(tz_name)).utcoffset().total_seconds() / 3600})
    return sorted(out, key=lambda r: (REGIONS.index(r["region"]), -r["utc_offset_h"], r["station_id"]))


def _envelope_head(conn, station: str, day: str, stage: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT checkpoint_id, decision_at_utc, fresh_until_utc, probabilities_json,"
        " bucket_ordinals_json, lineage_json, posterior_version"
        " FROM basket_probability_envelope WHERE station_id=? AND event_date=?"
        " AND +stage=? AND state='COMMITTED' ORDER BY decision_at_utc DESC LIMIT 1",
        (station, day, stage),
    ).fetchone()
    if row is None:
        return None
    return {
        "checkpoint_id": row[0], "decided_at_utc": row[1], "fresh_until_utc": row[2],
        "vector": _norm(_json(row[3], {})), "ordinals": _json(row[4], {}),
        "lineage": _json(row[5], {}), "version": row[6],
    }


def _models(lineage: Mapping[str, Any], axis) -> list[dict[str, Any]]:
    weights = _json(lineage.get("system_weights_json"), {})
    families = _json(lineage.get("system_family_ids_json"), {})
    cycles = _json(lineage.get("run_cycle_by_system_json"), {})
    inits = _json(lineage.get("run_init_utc_by_system_json"), {})
    leads = _json(lineage.get("hour_lead_bucket_by_system_json"), {})
    arrivals = _json(lineage.get("archive_query_at_utc_by_system_json"), {})
    centres = _json(lineage.get("native_calibrated_system_centers_c_json"), {})
    vectors = _json(lineage.get("native_system_probabilities_json"), {})
    observed = _json(lineage.get("observed_ensemble_system_ids_json"), [])
    systems = sorted(set(weights) | set(observed) | set(vectors),
                     key=lambda s: -(float(weights.get(s) or 0.0)))
    out = []
    for system in systems:
        vec = _norm(vectors.get(system) or {})
        out.append({
            "system_id": system, "family": families.get(system),
            "weight": _float(weights.get(system)),
            "run_cycle": cycles.get(system), "run_init_utc": inits.get(system),
            "lead_bucket": leads.get(system), "arrived_at_utc": arrivals.get(system),
            "calibrated_centre_c": _float(centres.get(system)),
            "seated": system in weights,
            "vector": {k: round(v, 5) for k, v in vec.items()},
            "stats": vector_stats(vec, axis) if vec else None,
        })
    return out


def _previous_p0(conn, station: str, day: str, before_utc: str) -> dict[str, Any] | None:
    """The P0 decided just before ``before_utc`` for the same day."""

    row = conn.execute(
        "SELECT decision_at_utc, lineage_json FROM basket_probability_envelope"
        " WHERE station_id=? AND event_date=? AND +stage='P0' AND state='COMMITTED'"
        " AND decision_at_utc<? ORDER BY decision_at_utc DESC LIMIT 1",
        (station, day, before_utc),
    ).fetchone()
    if row is None:
        return None
    return {"decided_at_utc": row[0], "lineage": _json(row[1], {})}


def _model_history(conn, station: str, day: str, axis, n: int = 5) -> list[dict[str, Any]]:
    """Each model's calibrated distribution over the day's last ``n`` P0
    decisions, newest first (user 2026-10-16「模型 run 与权重……保留模型校准后5次的
    变化」): per system the calibrated mean, mode bucket and its probability,
    and the weight it sat at; ``mode_move_pt`` is that bucket's probability
    change, in points, against the decision before (user 「概率的变化，比如↓−0.1点
    或者 ↑+0.1点」) -- one extra decision is read so the oldest has one too."""

    rows = conn.execute(
        "SELECT decision_at_utc, json_extract(lineage_json,'$.system_weights_json'),"
        " json_extract(lineage_json,'$.native_system_probabilities_json')"
        " FROM basket_probability_envelope WHERE station_id=? AND event_date=?"
        " AND +stage='P0' AND state='COMMITTED' ORDER BY decision_at_utc DESC LIMIT ?",
        (station, day, n + 1),
    ).fetchall()
    decoded = [(str(at), _json(w, {}), {k: _norm(v or {}) for k, v in _json(v, {}).items()})
               for at, w, v in rows]
    out = []
    for i, (at, weights, vectors) in enumerate(decoded[:n]):
        before = decoded[i + 1][2] if i + 1 < len(decoded) else {}
        systems = {}
        for system, vec in vectors.items():
            stats = vector_stats(vec, axis) if vec else None
            if stats is None:
                continue
            prior = before.get(system)
            systems[system] = {
                "mean_c": stats["mean_c"], "mode": stats["mode"], "mode_p": stats["mode_p"],
                "weight": _float(weights.get(system)),
                "mode_move_pt": None if not prior else round(100 * (stats["mode_p"] - prior.get(stats["mode"], 0.0)), 2),
            }
        out.append({"at_utc": at, "systems": systems})
    return out


def _with_previous(models: list[dict[str, Any]], previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each model beside what the previous P0 had for it (user 2026-10-05
    「run的更新变化、权重的变化、校准中心、均值等变化在表里标注」); a model the
    previous P0 did not carry is marked new, one it carried and this one does
    not is listed as dropped."""

    before = {m["system_id"]: m for m in previous}
    out = []
    for m in models:
        p = before.get(m["system_id"])
        out.append({**m, "prev": None if p is None else {
            "run_init_utc": p["run_init_utc"], "run_cycle": p["run_cycle"],
            "weight": p["weight"], "calibrated_centre_c": p["calibrated_centre_c"],
            "seated": p["seated"],
            "mean_c": (p.get("stats") or {}).get("mean_c"),
            "sd_c": (p.get("stats") or {}).get("sd_c"),
            "mode": (p.get("stats") or {}).get("mode"),
        }})
    here = {m["system_id"] for m in models}
    for p in previous:
        if p["system_id"] not in here and p.get("seated"):
            out.append({**p, "dropped": True, "weight": 0.0, "seated": False,
                        "prev": {"weight": p["weight"], "run_init_utc": p["run_init_utc"]}})
    return out


def _raw_by_system(station: str, day: str, snapshot_sha: Any, axis) -> dict[str, dict[str, Any]]:
    """Each system's members before calibration (user 2026-10-05「增加显示模型
    校准前后的均值、众数桶和概率」): the very packet this P0 was compiled from,
    found by the snapshot sha its lineage keeps; the raw member highs, their
    mean and spread, and the share of members on each rung of the day's
    ladder.  A packet past retention is simply absent."""

    import gzip  # noqa: PLC0415

    sha = str(snapshot_sha or "")
    if len(sha) < 16:
        return {}
    path = FORWARD_PACKET_ARCHIVE / day / f"{station}-{sha[:16]}.json.gz"
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            packet = json.load(handle)
    except (OSError, ValueError):
        return {}
    packet = packet.get("packet", packet) if isinstance(packet, Mapping) else {}
    if str(packet.get("snapshot_content_sha256") or "") != sha:
        return {}
    out = {}
    for system in packet.get("systems") or []:
        values = [float(m["raw_tmax_c"]) for m in system.get("members") or []
                  if _float(m.get("raw_tmax_c")) is not None]
        if not values:
            continue
        counts: dict[str, float] = {}
        for value in values:
            bucket = bucket_of(axis, value)
            if bucket is not None:
                counts[bucket] = counts.get(bucket, 0.0) + 1.0
        n = len(values)
        mean = sum(values) / n
        vector = {b: c / n for b, c in counts.items()}
        stats = vector_stats(vector, axis) if vector else None
        out[str(system.get("ensemble_system_id"))] = {
            "members": n, "mean_c": round(mean, 3),
            "sd_c": round(math.sqrt(sum((v - mean) ** 2 for v in values) / n), 3),
            "min_c": round(min(values), 2), "max_c": round(max(values), 2),
            "mode": None if stats is None else stats["mode"],
            "mode_p": None if stats is None else stats["mode_p"],
            "vector": {k: round(v, 4) for k, v in vector.items()},
            "run_init_utc": system.get("run_init_utc"),
        }
    return out


def _p0_brief(head: Mapping[str, Any], axis, previous: Mapping[str, Any] | None = None,
              *, station: str = "", day: str = "", conn=None) -> dict[str, Any]:
    L = head["lineage"]
    rad = _json(L.get("synoptic_cooling_json"), {})
    width = _json(L.get("center_width_json"), {})
    bridge = _json(L.get("consensus_bridge_json"), {})
    taf_w = _json(L.get("taf_envelope_witness_json"), {})
    ceiling = _json(L.get("far_peak_ceiling_json"), {})
    models = _models(L, axis)
    raw = _raw_by_system(station, day, L.get("native_forward_snapshot_sha256"), axis)
    for m in models:
        m["raw"] = raw.get(m["system_id"])
    if previous is not None:
        models = _with_previous(models, _models(previous["lineage"], axis))
    return {
        "models": models,
        "previous_decided_at_utc": None if previous is None else previous["decided_at_utc"],
        "model_history": _model_history(conn, station, day, axis) if conn is not None else [],
        "run_cycle": L.get("run_cycle"), "lead_bucket": L.get("lead_bucket"),
        "season_bin": L.get("season_bin"),
        "model_center_c": _float(L.get("model_center_c")),
        "model_envelope": [L.get("model_envelope_lower_bucket"), L.get("model_envelope_upper_bucket")],
        "radiation": {
            "state": rad.get("state"), "method": rad.get("method_tag"),
            "side": rad.get("radiation_side"),
            "radiation_sum_mj": _float(rad.get("radiation_sum")),
            "clearness_index": _float(rad.get("clearness_index")),
            "clearness_mean": _float(rad.get("history_clearness_mean")),
            "clearness_sd": _float(rad.get("history_clearness_sd")),
            "standardised": _float(rad.get("standardised")),
            "shift_c": _float(rad.get("shift_c")),
            "sigma_warm": _float(rad.get("shift_sigma_per_unit_warm")),
            "sigma_cold": _float(rad.get("shift_sigma_per_unit_cold")),
        } if rad else None,
        "width": {
            "lambda": _float(width.get("width_lambda")),
            "sigma_c": _float(width.get("distribution_sigma_c")),
            "mean_c": _float(width.get("distribution_mean_c")),
            "seated": width.get("seated_system_count"),
            "hours_to_settlement": _float(width.get("hours_to_settlement")),
            "tail_weight": _float(width.get("width_slice_tail_weight")),
        } if width else None,
        "bridge": {"lambda": _float(bridge.get("lambda")),
                   "consensus_center_c": _float(bridge.get("consensus_center_c")),
                   "center_gap_c": _float(bridge.get("center_gap_c"))} if bridge else None,
        "taf_witness": {
            "forecast_revision_id": taf_w.get("forecast_revision_id"),
            "capture_content_sha256": taf_w.get("capture_content_sha256"),
            "issued_at_utc": taf_w.get("issued_at_utc"), "tx_c": _float(taf_w.get("tx_c")),
            "cloud_cover_fraction": _float(taf_w.get("cloud_cover_fraction")),
            "precipitation_probability": _float(taf_w.get("precipitation_probability")),
            "wind_speed_mps": _float(taf_w.get("wind_speed_mps")),
        } if taf_w else None,
        "far_peak_ceiling": ceiling or None,
    }


def _excluded(text: Any) -> list[str]:
    return [b for b in str(text or "").split(",") if b and b != "NONE"]


def _p0m_brief(head: Mapping[str, Any]) -> dict[str, Any]:
    L = head["lineage"]
    receipt = _json(L.get("p0m_taf_likelihood_json"), {})
    return {
        "model_center_c": _float(L.get("model_center_c")),
        "taf_state": receipt.get("state") or L.get("taf_revision_state"),
        "tx_c": _float(L.get("taf_observed_tx_c")),
        "root_center_c": _float(receipt.get("root_center_c")),
        "x_c": _float(receipt.get("x_c")), "b": _float(receipt.get("b")),
        "tau": _float(receipt.get("tau")), "tempering": receipt.get("tempering"),
        "likelihood_by_bucket": receipt.get("likelihood_by_bucket") or {},
        "observed_running_high_c": _float(L.get("observed_running_high_c")),
        "excluded": _excluded(L.get("observed_hard_excluded_buckets")),
    }


def _p1_brief(head: Mapping[str, Any]) -> dict[str, Any]:
    L = head["lineage"]
    receipt = _json(L.get("p1_observation_likelihood_json"), {})
    return {
        "model_center_c": _float(L.get("model_center_c")),
        "state": receipt.get("state"), "update": receipt.get("update"),
        "running_high_c": _float(L.get("metar_trajectory_running_high_c")),
        "rise_probability": _float(receipt.get("rise_probability")),
        "alpha": _float(receipt.get("alpha")), "lambda": _float(receipt.get("lambda")),
        "features": receipt.get("features") or {},
        "likelihood_by_bucket": receipt.get("likelihood_by_bucket") or {},
        "excluded": _excluded(L.get("hard_excluded_buckets")),
        "root_stage": L.get("prediction_root_stage"),
    }


def _p1_root_center(conn, head: Mapping[str, Any], station: str, day: str) -> float | None:
    """Read the exact committed prediction root, never the latest stage head."""
    lineage = head["lineage"]
    checkpoint = lineage.get("prediction_root_checkpoint_id")
    digest = lineage.get("prediction_root_content_sha256")
    stage = lineage.get("prediction_root_stage")
    if not checkpoint or not digest or stage not in {"P0", "P0-M"}:
        return None
    row = conn.execute(
        "SELECT lineage_json, decision_at_utc FROM basket_probability_envelope"
        " WHERE checkpoint_id=? AND content_sha256=? AND station_id=? AND event_date=?"
        " AND stage=? AND state='COMMITTED' LIMIT 1",
        (checkpoint, digest, station, day, stage),
    ).fetchone()
    if row is None:
        return None
    root_time, head_time = _utc(row[1]), _utc(head["decided_at_utc"])
    # Millisecond and microsecond strings are not lexically comparable:
    # .844Z is earlier than .844006Z even though it sorts after it.
    if root_time is None or head_time is None or root_time > head_time:
        return None
    return _float(_json(row[0], {}).get("model_center_c"))


def _timeline(conn, station: str, day: str, axis) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT stage, decision_at_utc, probabilities_json, posterior_version,"
        " json_extract(lineage_json,'$.model_center_c')"
        " FROM basket_probability_envelope WHERE station_id=? AND event_date=?"
        " AND state='COMMITTED' ORDER BY decision_at_utc",
        (station, day),
    ).fetchall()
    # A P1 decision second replays the day's prefix: only the last of a
    # second was served (the station Bayes card's rule).
    keep: dict[tuple[str, str], tuple] = {}
    for row in rows:
        keep[(str(row[0]), str(row[1])[:19])] = row
    p1 = [k for k in keep if k[0] == "P1"]
    drop = set(sorted(p1, key=lambda k: k[1])[:-P1_TREND_MAX]) if len(p1) > P1_TREND_MAX else set()
    out = []
    for key, (stage, at, probs, version, center) in keep.items():
        if key in drop:
            continue
        vec = _norm(_json(probs, {}))
        stats = vector_stats(vec, axis)
        if stats is None:
            continue
        out.append({"stage": stage, "at_utc": str(at), "version": version,
                    "center_c": _float(center),
                    "vector": {k: round(v, 5) for k, v in vec.items()}, "stats": stats})
    out.sort(key=lambda e: (e["at_utc"], STAGES.index(e["stage"]) if e["stage"] in STAGES else 9))
    return out


def _market(conn, station: str, day: str, axis) -> dict[str, Any]:
    from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: PLC0415

    stamps = conn.execute(
        "SELECT rowid, observed_at FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED' ORDER BY observed_at",
        (station, day),
    ).fetchall()
    if not stamps:
        return {"available": False, "reason": "no_ws_snapshot"}
    picked: list[int] = []
    last_slot = None
    for rowid, at in stamps:
        when = _utc(at)
        if when is None:
            continue
        slot = int(when.timestamp() // (MARKET_TREND_STEP_MIN * 60))
        if slot != last_slot:
            picked.append(int(rowid))
            last_slot = slot
    newest_rowid = int(stamps[-1][0])
    if newest_rowid not in picked:
        picked.append(newest_rowid)
    trend = []
    latest = None
    for start in range(0, len(picked), 200):
        chunk = picked[start:start + 200]
        for rowid, at, buckets_json, source in conn.execute(
            "SELECT rowid, observed_at, buckets_json, quote_source FROM basket_official_ws_snapshot"
            f" WHERE rowid IN ({','.join('?' * len(chunk))}) ORDER BY observed_at", chunk,
        ):
            buckets = _json(buckets_json, [])
            ref = market_reference_v1(buckets)
            if rowid == newest_rowid:
                latest = (at, buckets, ref, source)
            if ref.get("state") != "complete":
                continue
            vec = {str(k): float(v) for k, v in ref["vector"].items()}
            stats = vector_stats(vec, axis)
            if stats:
                trend.append({"at_utc": str(at), "vector": {k: round(v, 5) for k, v in vec.items()},
                              "stats": stats})
    # The book shown and priced is the WS publisher's own when it has this
    # day -- the committed snapshot is the resident's once-a-cycle copy.
    try:
        from .p1_tail_lane import _ws_manifest_root, live_ws_snapshot  # noqa: PLC0415

        live = live_ws_snapshot(
            conn, _ws_manifest_root(None), station_id=station, event_date=day,
            now_utc=_iso(datetime.now(timezone.utc)), include_listing_days=True,
        )
    except Exception:  # noqa: BLE001 -- no live book falls back to the snapshot
        live = None
    if live is not None:
        live_ref = market_reference_v1(live["buckets"])
        latest = (live["observed_at"], live["buckets"], live_ref, "WS_PUBLISHER_LIVE")
        if live_ref.get("state") == "complete":
            vec = {str(k): float(v) for k, v in live_ref["vector"].items()}
            stats = vector_stats(vec, axis)
            if stats and (not trend or trend[-1]["at_utc"] < str(live["observed_at"])):
                trend.append({"at_utc": str(live["observed_at"]),
                              "vector": {k: round(v, 5) for k, v in vec.items()}, "stats": stats})
    if latest is None:
        return {"available": False, "reason": "newest_snapshot_unreadable", "trend": trend}
    at, buckets, ref, source = latest
    book = []
    for row in buckets:
        asks = sorted(
            ((float(l["price"]), float(l["shares"])) for l in (row.get("ask_levels") or [])
             if _float(l.get("price")) and _float(l.get("shares"))),
            key=lambda x: x[0])
        bids = sorted(
            ((float(l["price"]), float(l["shares"])) for l in (row.get("bid_levels") or [])
             if _float(l.get("price")) and _float(l.get("shares"))),
            key=lambda x: -x[0])
        book.append({
            "bucket": str(row.get("bucket")), "ordinal": row.get("ordinal"),
            "best_ask": asks[0][0] if asks and row.get("ask_available", True) else None,
            "best_bid": bids[0][0] if bids else (_float(row.get("bid_price")) or None),
            "ask_levels": [[p, s] for p, s in asks],
            "bid_levels": [[p, s] for p, s in bids],
            "tick": _float(row.get("price_tick_size")),
            "min_order_size": _float(row.get("min_order_size")),
            "book_observed_at": row.get("book_observed_at"),
        })
    vec = {str(k): float(v) for k, v in (ref.get("vector") or {}).items()}
    return {
        "available": True, "observed_at_utc": str(at), "quote_source": source,
        "reference_state": ref.get("state"), "reference_reason": ref.get("reason"),
        "mid_sum": ref.get("mid_sum"), "max_spread": ref.get("max_spread"),
        "vector": {k: round(v, 5) for k, v in vec.items()},
        "stats": vector_stats(vec, axis) if vec else None,
        "book": book, "trend": trend,
    }


def _local_window(day: str, zone: ZoneInfo) -> tuple[str, str]:
    d = datetime.fromisoformat(day).date()
    start = datetime.combine(d, dtime(0), zone)
    return _iso(start), _iso(start + timedelta(days=1))


def _metar(station: str, day: str, zone: ZoneInfo) -> list[dict[str, Any]]:
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import report_explain as rex  # noqa: PLC0415
        archive = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=5)
    except (ImportError, sqlite3.Error):
        return []
    lo, hi = _local_window(day, zone)
    try:
        rows = archive.execute(
            "SELECT valid_at, raw_text, reported_temperature_c, report_type, correction_suspected"
            " FROM reported_trajectory_observations WHERE station_id=? AND valid_at>=? AND valid_at<?"
            " AND raw_text IS NOT NULL AND raw_text<>'' ORDER BY valid_at",
            # Compared on the bare second: stored stamps carry milliseconds
            # ("...16:00:00.000Z" sorts before "...16:00:00Z"), which dropped
            # the local-midnight report from its own day.
            (station, lo[:19], hi[:19]),
        ).fetchall()
    finally:
        archive.close()
    out = []
    seen = set()
    running = None
    for at, raw, temp, kind, cor in rows:
        key = (str(at), str(raw))
        if key in seen:
            continue
        seen.add(key)
        parsed = rex.explain_metar(str(raw))
        t = _float(temp) if temp is not None else _float(parsed.get("temp_c"))
        if t is not None:
            running = t if running is None else max(running, t)
        when = _utc(at)
        tokens = str(raw).split()
        body = tokens[:tokens.index("RMK")] if "RMK" in tokens else tokens
        vis = 10000.0 if "CAVOK" in body else next(
            (float(t) for t in body[2:8] if len(t) == 4 and t.isdigit()), None)
        # Observed rain (user 2026-10-09「云雨 · 逐小时降水预报：增加实时的数据显示」):
        # US ASOS reports the last hour's amount in its remarks as Pnnnn
        # (hundredths of an inch; P0000 a trace); recent rain since the last
        # report as RE groups.  Nothing is inferred where neither is reported.
        remarks = tokens[tokens.index("RMK") + 1:] if "RMK" in tokens else []
        p_group = next((t for t in remarks if re.fullmatch(r"P\d{4}", t)), None)
        # Taiwan's CAA writes the amount in millimetres: "RA AMT 2.6MM".
        amount = re.search(r"\bAMT (\d+(?:\.\d+)?)MM\b", " ".join(remarks))
        recent = [t for t in body if re.fullmatch(r"RE(?:SH|TS|FZ)?(?:RA|DZ|SN|GR|GS|PL|UP)+", t)]
        out.append({
            "valid_at_utc": str(at),
            "local": when.astimezone(zone).strftime("%H:%M") if when else "",
            "raw": str(raw), "kind": parsed.get("kind") or kind,
            "clouds": [t for t in body if t[:3] in ("FEW", "SCT", "BKN", "OVC", "VV/")
                       or t in ("CAVOK", "NSC", "NCD", "SKC", "CLR")],
            "visibility_m": vis,
            "correction_suspected": bool(cor),
            "temp_c": t, "dew_c": _float(parsed.get("dew_c")),
            "cloud_cover": _float(parsed.get("cover")),
            "wind_mps": _float(parsed.get("wind_mps")),
            "precip": bool(parsed.get("precip")),
            "weathers": parsed.get("weathers") or [],
            "recent_precip": recent,
            "precip_1h_mm": (round(int(p_group[1:]) * 0.254, 2) if p_group is not None
                             else float(amount.group(1)) if amount else None),
            "precip_1h_trace": p_group == "P0000",
            "running_high_c": running,
            # The table's groups, each raw code with a few words (user
            # 2026-10-10「metar改为表格形式，拆分报文及对应简要说明和趋势」).
            "groups": rex.metar_groups(str(raw)),
        })
    return out


# ---------------------------------------------------------------------------
# 逐小时云与辐射：实况对预报 (user 2026-10-07「实况观测的云和辐射对比预测的差异
# 在哪里，标注出来」).  The forecast is Open-Meteo's hourly model output -- an
# aggregator republisher, read here for display only, nothing downstream
# reads it -- in two versions: the newest run and the one issued a day
# before.  The airport does not measure radiation, so the "observed" side is
# inferred from the report's cloud and the sun's height: clear-sky GHI
# (Haurwitz) x (1 - 0.75 N^3.4) (Kasten-Czeplak), N the reported cover.  A
# METAR reports the low layers only (CAVOK/NSC: nothing below 1,500 m), so
# it is compared with the forecast's LOW cloud, and the inferred radiation
# is an upper bound under high cloud.

FORECAST_CACHE_SECONDS = 1800.0
CLOSED_DAY_CACHE_SECONDS = 6 * 3600.0
_FORECAST_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
OPEN_METEO_PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
# The forecast call shares the egress address's daily allowance with the
# relay lanes (2026-10-11: a research pull spent the day at 09:41Z and this
# panel read 「HTTP Error 429: Too Many Requests」 until midnight).  It is
# charged to its own lane of the shared ledger (open_meteo_quota), so the
# board's spend is counted and bounded, and a spent day is answered from the
# ledger without a request.  The board never marks the day spent for the
# production lanes; a daily refusal it meets itself is held here, in process,
# until the next UTC midnight or one cache period, whichever is sooner.
QUOTA_LANE = "station_day_board"
_DAY_REFUSED_UNTIL: list[str] = []
# Measured sunshine (user 2026-10-09「实况日照数据源是什么？感觉有点高了」): the
# METAR-inferred figure read 483-807 W/m2 at ZUUU 10-09 10-13h against a
# satellite 83-254 -- the report sees only low cloud, and haze and the
# clean-air clear-sky model add more.  The geostationary products below
# (Open-Meteo's satellite archive, a republisher: board only) are measured
# irradiance; the product follows the longitude the way the 2026-10-07 source
# check picked it per station.  West of 60W none is real (the default there
# is forecast-filled); those stations read NOAA's own GOES DSR instead
# (goes_surface_radiation, user ruling「全部都用卫星实测日照」).
OPEN_METEO_SATELLITE = "https://satellite-api.open-meteo.com/v1/archive"
SATELLITE_CACHE_SECONDS = 900.0
_SATELLITE_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
SATELLITE_PRODUCTS = {
    # East of 90E the column reads JAXA's own copy of the product it used to
    # read here republished (user 2026-10-15 「接入：http://hokusai.eorc.jaxa.jp/」,
    # jaxa_ptree_radiation): same quantity, official source, about 45 minutes
    # sooner.
    "jaxa_ptree_himawari": "JAXA Himawari（P-Tree 官方）",
    "eumetsat_lsa_saf_iodc": "EUMETSAT 印度洋（IODC）",
    "dwd_sis_europe_africa_v4": "DWD SIS 欧洲/非洲",
    "eumetsat_lsa_saf_msg": "EUMETSAT MSG",
    "noaa_goes_dsr": "NOAA GOES-19/18 DSR",
}


#: How each source reaches the board and how late an hour lands.
SATELLITE_VIA = {
    "jaxa_ptree_himawari": "JAXA P-Tree 官方，整点后约 15 分钟",
    "noaa_goes_dsr": "NOAA 官方，扫描后约 25 分钟",
}
SATELLITE_VIA_DEFAULT = "经 Open-Meteo 转发，约晚 1 小时"


def _satellite_product(lon: float) -> str | None:
    if lon >= 90.0 or lon <= -150.0:
        return "jaxa_ptree_himawari"
    if lon >= 45.0:
        return "eumetsat_lsa_saf_iodc"
    if lon >= -20.0:
        return "dwd_sis_europe_africa_v4"
    if lon >= -60.0:
        return "eumetsat_lsa_saf_msg"
    return None
CLOUD_DIFF = 0.375          # three oktas
RADIATION_DIFF_FRAC = 0.25
RADIATION_DIFF_MIN_WM2 = 100.0
TEMP_DIFF_C = 1.0
HAZE_VISIBILITY_M = 8000.0
# Morning sunshine -> the day's maximum (user 2026-10-13 「前端显式：上午日照偏差 →
# 最高温相对预报的偏差，显示度数」): the two-slope line fitted per last measured
# hour, read here and applied to the satellite against the day-ahead forecast
# -- the fit's own basis, not the latest forecast the headline compares.
# Display only.  Since 2026-10-17 (user 「改用 W/m² 的绝对差重新拟合这条线」) the
# line reads the mean W/m² difference over the hours 05..H
# (docs/research/sun_tmax_shift_abs_20261017/fit.py): a percentage of a dim
# forecast blew up (ZUUU that morning, +200 W/m² over 52 read +106%) and had
# to refuse every morning forecast under 60 W/m² an hour.  Out of sample it
# is as good as the percentage line where that one applied, and beats no
# reading on the mornings it refused.
SUN_TMAX_SHIFT_PATH = ROOT / "config" / "ui" / "sun_tmax_shift_v2.json"


def _sun_tmax_shift(rows: Sequence[Mapping[str, Any]], measured: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not measured:
        return None
    try:
        fit = json.loads(SUN_TMAX_SHIFT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "unfitted"}
    table = fit.get("by_last_hour") or {}
    last = max(int(r["hour"]) for r in measured)
    hours = sorted(int(h) for h in table)
    tmax_d1 = [r["forecast"].get("temp_c_d1") for r in rows if r["forecast"].get("temp_c_d1") is not None]
    tmax = round(max(tmax_d1), 1) if tmax_d1 else None
    window = [r for r in measured if int(r["hour"]) >= 5]
    if not hours or last < hours[0] or not window:
        return {"state": "too_early", "from_hour": hours[0] if hours else None, "forecast_tmax_d1_c": tmax}
    line = table[str(min(last, hours[-1]))]
    diff = sum(float(r["sat_sw_wm2"]) - float(r["forecast"].get("sw_d1") or 0.0) for r in window) / len(window)
    clip = float(fit.get("x_clip_wm2") or 300.0)
    x = max(-clip, min(clip, diff))
    side, ci = ("dark", "dark_ci95") if x < 0 else ("bright", "bright_ci95")
    shift = lambda b: float(line["intercept_c"]) + b / 100.0 * x
    lo, hi = sorted((shift(line[ci][0]), shift(line[ci][1])))
    return {"state": "ok", "last_hour": last, "wm2_d1": round(diff, 1), "shift_c": round(shift(line[side + "_c_per_100wm2"]), 2),
            "range_c": [round(lo, 2), round(hi, 2)], "forecast_tmax_d1_c": tmax,
            "resid_sd_c": line.get("resid_sd_c"), "r2": line.get("r2"), "n": line.get("n"),
            "window": fit.get("window"), "fitted_at_utc": fit.get("fitted_at_utc")}


def _station_lat_lon(conn, station: str) -> tuple[float, float] | None:
    row = conn.execute(
        "SELECT latitude, longitude FROM calibration_station_forecast_physics_version"
        " WHERE station_id=? AND latitude IS NOT NULL ORDER BY captured_at_utc DESC LIMIT 1",
        (station,),
    ).fetchone()
    return None if row is None else (float(row[0]), float(row[1]))


def _open_meteo_hourly(lat: float, lon: float, tz: str, day: str) -> dict[str, Any]:
    import ssl  # noqa: PLC0415
    import time  # noqa: PLC0415
    from urllib.parse import urlencode  # noqa: PLC0415
    from urllib.request import Request, urlopen  # noqa: PLC0415

    from urllib.error import HTTPError  # noqa: PLC0415

    from . import open_meteo_quota as quota  # noqa: PLC0415

    key = (f"{lat:.3f},{lon:.3f}", day)
    hit = _FORECAST_CACHE.get(key)
    # A closed local day (yesterday's column) no longer changes: kept six hours.
    ttl = CLOSED_DAY_CACHE_SECONDS if day < datetime.now(ZoneInfo(tz)).date().isoformat() else FORECAST_CACHE_SECONDS
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    now_iso = datetime.now(timezone.utc).isoformat()
    if _DAY_REFUSED_UNTIL and now_iso < _DAY_REFUSED_UNTIL[0]:
        return {"available": False, "reason": "Open-Meteo 当日额度已满，00:00Z 恢复"}
    try:
        quota.charge(QUOTA_LANE, 1)
    except quota.QuotaRefused as exc:
        spent_day = str(exc) == "open_meteo_day_exhausted"
        return {"available": False, "reason": "Open-Meteo 当日额度已满，00:00Z 恢复" if spent_day
                else f"看板当日 Open-Meteo 份额已用完（{str(exc).rsplit(':', 1)[-1]}）"}
    # Ten variables, one call's weight: dew point, wind and rain for the cards
    # beside the sun (user 2026-10-15「增加露点、风、雨等信息卡片」) take the place
    # of the day-before total cloud nothing read.
    fields = ["cloud_cover", "cloud_cover_low", "shortwave_radiation", "temperature_2m"]
    extra = ["dew_point_2m", "wind_speed_10m", "precipitation"]
    query = urlencode({
        "latitude": lat, "longitude": lon, "timezone": tz, "start_date": day, "end_date": day,
        "hourly": ",".join(fields + extra + [f + "_previous_day1" for f in fields if f != "cloud_cover"]),
        "wind_speed_unit": "ms",
    })
    try:
        import certifi  # noqa: PLC0415

        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    try:
        with urlopen(Request(f"{OPEN_METEO_PREVIOUS_RUNS}?{query}",
                             headers={"User-Agent": "weather-board-station-day"}),
                     timeout=20, context=context) as resp:  # noqa: S310 -- fixed https host
            data = json.loads(resp.read().decode("utf-8"))
        out = {"available": "hourly" in data, "hourly": data.get("hourly") or {},
               "fetched_at_utc": _iso(datetime.now(timezone.utc))}
    except HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 -- the status line still says something
            body = ""
        if quota.classify(body, exc.code) == "day":
            now = datetime.now(timezone.utc)
            midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            # Asked again after one cache period at most: if the aggregator's
            # day does not turn at 00:00Z the panel is dark for half an hour,
            # not a day.
            _DAY_REFUSED_UNTIL[:] = [min(midnight, now + timedelta(seconds=FORECAST_CACHE_SECONDS)).isoformat()]
            out = {"available": False, "reason": "Open-Meteo 当日额度已满，00:00Z 恢复"}
        else:
            out = {"available": False, "reason": f"HTTPError {exc.code}: {(body or str(exc))[:120]}"}
    except Exception as exc:  # noqa: BLE001 -- the timeline says why it is empty
        out = {"available": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if out.get("available"):
        _FORECAST_CACHE[key] = (time.monotonic(), out)
    return out


def _open_meteo_satellite(lat: float, lon: float, tz: str, day: str, model: str) -> dict[str, Any]:
    """Hourly measured shortwave (W/m2, the mean of the hour ending at the
    stamp, local time) from the named satellite product; cached briefly."""

    import ssl  # noqa: PLC0415
    import time  # noqa: PLC0415
    from urllib.parse import urlencode  # noqa: PLC0415
    from urllib.request import Request, urlopen  # noqa: PLC0415

    key = (f"{lat:.3f},{lon:.3f}", day, model)
    hit = _SATELLITE_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < SATELLITE_CACHE_SECONDS:
        return hit[1]
    query = urlencode({"latitude": lat, "longitude": lon, "timezone": tz, "start_date": day,
                       "end_date": day, "hourly": "shortwave_radiation", "models": model})
    try:
        import certifi  # noqa: PLC0415

        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    try:
        with urlopen(Request(f"{OPEN_METEO_SATELLITE}?{query}", headers={"User-Agent": "weather-board-station-day"}),
                     timeout=20, context=context) as resp:  # noqa: S310 -- fixed https host
            data = json.loads(resp.read().decode("utf-8"))
        values = (data.get("hourly") or {}).get("shortwave_radiation") or []
        out = {"available": any(v is not None for v in values), "values": [_float(v) for v in values],
               "fetched_at_utc": _iso(datetime.now(timezone.utc))}
    except Exception as exc:  # noqa: BLE001 -- the panel falls back and says why
        out = {"available": False, "values": [], "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if out.get("available"):
        _SATELLITE_CACHE[key] = (time.monotonic(), out)
    return out


def _satellite_ahead_of_utc(lat: float, lon: float, day: str, zone: ZoneInfo, model: str) -> dict[str, Any]:
    """A local day that has begun while UTC is still on the day before.

    The archive's range ends at UTC today, so asking for the local date
    answers 400 (2026-10-13: every East Asian station from local midnight
    to 08/09 local, the morning sun included).  Every hour of that local
    day so far lies on UTC today: read that UTC day and fold it back onto
    local hours.  A zone off by a fraction of an hour has no whole UTC hour
    to fold onto; it says when the local date opens instead.
    """

    start = datetime.combine(datetime.fromisoformat(day).date(), dtime(0), zone)
    offset = start.utcoffset() or timedelta(0)
    opens = int(offset.total_seconds() // 3600)
    if offset.total_seconds() % 3600:
        return {"available": False, "values": [None] * 24,
                "reason": f"卫星档案按 UTC 日期开放，当地 {opens:02d}:{int(offset.total_seconds() % 3600 // 60):02d} 后可取"}
    utc_day = start.astimezone(timezone.utc).date().isoformat()
    raw = _open_meteo_satellite(lat, lon, "GMT", utc_day, model)
    got = raw.get("values") or []
    values: list[float | None] = [None] * 24
    for h in range(24):
        index = 24 - opens + h  # local hour h ends at UTC (h - offset) on utc_day
        if 0 <= index < len(got):
            values[h] = got[index]
    out = dict(raw, values=values, available=any(v is not None for v in values))
    if not out["available"] and not out.get("reason"):
        out["reason"] = f"卫星档案按 UTC 日期开放，当地 {opens:02d}:00 后按当地日期取"
    return out


def _jaxa_hours(station: str, lat: float, lon: float, day: str, zone: ZoneInfo) -> dict[str, Any]:
    """The local day's hourly JAXA Himawari SWR, as far as the background
    reader has it: hour h (the mean of the hour ending at h:00, the forecast
    column's stamp) reads the UTC hour file whose hour holds h-0:30 -- the
    same hour in a whole-hour zone; night hours are zero; a published hour not
    read yet is absent and arrives on a later refresh."""

    from . import jaxa_ptree_radiation as jaxa  # noqa: PLC0415

    d = datetime.fromisoformat(day).date()
    now_utc = datetime.now(timezone.utc)
    values: list[float | None] = [None] * 24
    wanted: dict[int, datetime] = {}
    for h in range(24):
        middle = (datetime.combine(d, dtime(h), zone) - timedelta(minutes=30)).astimezone(timezone.utc)
        start = jaxa.hour_start(middle)
        if start + jaxa.PUBLISH_AFTER > now_utc:
            continue  # the hour's file is not public yet
        if _clear_sky_ghi(lat, lon, middle) <= 0.0:
            values[h] = 0.0
            continue
        wanted[h] = start
    try:
        got = jaxa.measured(station, list(wanted.values()))
    except Exception as exc:  # noqa: BLE001 -- the column says why
        return {"available": False, "values": values, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
    for h, start in wanted.items():
        values[h] = got.get(start.strftime("%Y-%m-%dT%H:00Z"))
    # Cloud seen from above (user 2026-10-15 「接云」): the scan at the hour's
    # middle, h-0:30, day-time hours that are public only.
    clouds: list[dict[str, Any] | None] = [None] * 24
    scans: dict[int, datetime] = {}
    for h in wanted:
        middle = (datetime.combine(d, dtime(h), zone) - timedelta(minutes=30)).astimezone(timezone.utc)
        scan = middle.replace(minute=middle.minute - middle.minute % 10, second=0, microsecond=0)
        if scan + jaxa.CLP_PUBLISH_AFTER <= now_utc:
            scans[h] = scan
    try:
        seen = jaxa.clouds(station, list(scans.values()))
    except Exception:  # noqa: BLE001 -- the column stays empty, the sunshine stands
        seen = {}
    for h, scan in scans.items():
        reading = seen.get(scan.strftime("%Y-%m-%dT%H:%MZ"))
        if reading:
            clouds[h] = {**reading, "scan_utc": scan.strftime("%Y-%m-%dT%H:%MZ"),
                         "type_name": jaxa.CLP_TYPES.get(reading.get("type"))}
    reader = jaxa.status()
    # The newest 10-minute scan, today only (user 2026-10-15 「加L2」): 「此刻」.
    today = d == now_utc.astimezone(zone).date()
    return {"available": any(v for v in values), "values": values, "fetched_at_utc": reader.get("last_read_at_utc"),
            "pending_hours": sum(1 for h in wanted if values[h] is None), "reason": reader.get("last_error"),
            "latest": jaxa.latest(station) if today else None, "clouds": clouds}


def _goes_hours(station: str, lat: float, lon: float, day: str, zone: ZoneInfo) -> dict[str, Any]:
    """The local day's hourly GOES DSR, as far as the background reader has
    it: hour h is the scan nearest h-1:30 (the middle of the hour the forecast
    column averages); night hours are zero; a published scan not read yet is
    absent and arrives on a later refresh."""

    from . import goes_surface_radiation as goes  # noqa: PLC0415

    d = datetime.fromisoformat(day).date()
    now_utc = datetime.now(timezone.utc)
    values: list[float | None] = [None] * 24
    wanted: dict[int, datetime] = {}
    for h in range(24):
        middle = (datetime.combine(d, dtime(h), zone) - timedelta(minutes=30)).astimezone(timezone.utc)
        if middle + timedelta(minutes=25) > now_utc:
            continue  # the scan is not public yet
        if _clear_sky_ghi(lat, lon, middle) <= 0.0:
            values[h] = 0.0
            continue
        wanted[h] = middle
    try:
        got = goes.measured(station, lat, lon, sorted(wanted.values(), reverse=True))
    except Exception as exc:  # noqa: BLE001 -- the column says why
        return {"available": False, "values": values, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
    for h, instant in wanted.items():
        values[h] = got.get(instant.isoformat())
    return {"available": any(v for v in values), "values": values,
            "fetched_at_utc": _iso(now_utc), "pending_hours": sum(1 for h in wanted if values[h] is None)}


def _clear_sky_ghi(lat: float, lon: float, when_utc: datetime) -> float:
    """Haurwitz clear-sky global horizontal irradiance, W/m^2."""

    n = when_utc.timetuple().tm_yday
    hour = when_utc.hour + when_utc.minute / 60.0
    gamma = 2 * math.pi / 365 * (n - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    eot = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                    - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    solar_minutes = hour * 60 + eot + 4 * lon
    ha = math.radians(solar_minutes / 4 - 180)
    phi = math.radians(lat)
    cosz = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    return 0.0 if cosz <= 0.01 else 1098.0 * cosz * math.exp(-0.057 / cosz)


def _hourly_vs_forecast(conn, station: str, day: str, zone: ZoneInfo,
                        metar: list[dict[str, Any]]) -> dict[str, Any]:
    where = _station_lat_lon(conn, station)
    if where is None:
        return {"available": False, "reason": "no_station_coordinates"}
    lat, lon = where
    fc = _open_meteo_hourly(lat, lon, str(zone.key), day)
    product = _satellite_product(lon)
    started = datetime.fromisoformat(day).date() <= datetime.now(timezone.utc).astimezone(zone).date()
    # A day not begun has nothing measured yet (the archive answers 400).
    if product == "jaxa_ptree_himawari" and started:
        sat = _jaxa_hours(station, lat, lon, day, zone)
    elif product and started and day > datetime.now(timezone.utc).date().isoformat():
        sat = _satellite_ahead_of_utc(lat, lon, day, zone, product)
    elif product and started:
        sat = _open_meteo_satellite(lat, lon, str(zone.key), day, product)
    elif started:
        product, sat = "noaa_goes_dsr", _goes_hours(station, lat, lon, day, zone)
    else:
        product = product or "noaa_goes_dsr"
        sat = {"available": False, "reason": "day_not_started"}
    sat_values = sat.get("values") or []
    hourly = fc.get("hourly") or {}
    times = hourly.get("time") or []
    by_hour: dict[int, dict[str, Any]] = {}
    for m in metar:
        when = _utc(m["valid_at_utc"])
        if when is None:
            continue
        local = when.astimezone(zone)
        if local.minute <= 10:             # the routine report at the hour
            by_hour[local.hour] = m
    now_local = datetime.now(timezone.utc).astimezone(zone)
    d = datetime.fromisoformat(day).date()
    rows = []
    for h in range(24):
        start = datetime.combine(d, dtime(h), zone)
        def val(name: str, prev: bool = False) -> float | None:
            arr = hourly.get(name + ("_previous_day1" if prev else "")) or []
            return _float(arr[h]) if h < len(arr) else None
        fc_row = {
            "cloud": val("cloud_cover"), "cloud_low": val("cloud_cover_low"),
            "sw": val("shortwave_radiation"), "temp_c": val("temperature_2m"),
            "cloud_low_d1": val("cloud_cover_low", True),
            "dew_c": val("dew_point_2m"), "wind_mps": val("wind_speed_10m"), "precip_mm": val("precipitation"),
            "sw_d1": val("shortwave_radiation", True), "temp_c_d1": val("temperature_2m", True),
        } if times else {}
        obs = by_hour.get(h)
        sat_sw = sat_values[h] if h < len(sat_values) else None
        sat_cloud = (sat.get("clouds") or [None] * 24)[h]
        row: dict[str, Any] = {"hour": h, "past": start <= now_local, "forecast": fc_row, "obs": None,
                               "sat_sw_wm2": None if sat_sw is None else round(sat_sw, 0),
                               "sat_cloud": sat_cloud, "diffs": []}
        if obs is not None:
            prev = by_hour.get(h - 1)
            covers = [x["cloud_cover"] for x in (prev, obs) if x and x.get("cloud_cover") is not None]
            cover = sum(covers) / len(covers) if covers else None
            # Open-Meteo's radiation at hh:00 is the mean over the hour before.
            clear = _clear_sky_ghi(lat, lon, (start - timedelta(minutes=30)).astimezone(timezone.utc))
            inferred = None if cover is None else clear * (1 - 0.75 * cover ** 3.4)
            row["obs"] = {
                "temp_c": obs.get("temp_c"), "cloud_cover": obs.get("cloud_cover"),
                "clouds": obs.get("clouds") or [], "weathers": obs.get("weathers") or [],
                "visibility_m": obs.get("visibility_m"), "precip": obs.get("precip"),
                "dew_c": obs.get("dew_c"), "wind_mps": obs.get("wind_mps"),
                "clear_sky_wm2": round(clear, 0), "inferred_sw_wm2": None if inferred is None else round(inferred, 0),
            }
            diffs = row["diffs"]
            low = fc_row.get("cloud_low")
            if low is not None and obs.get("cloud_cover") is not None:
                gap = obs["cloud_cover"] - low / 100.0
                if gap >= CLOUD_DIFF:
                    diffs.append({"kind": "cloud", "dir": "more", "text": f"比预报多云（低云实况 {obs['cloud_cover']:.0%} 对预报 {low:.0f}%）"})
                elif gap <= -CLOUD_DIFF:
                    diffs.append({"kind": "cloud", "dir": "less", "text": f"比预报晴（低云实况 {obs['cloud_cover']:.0%} 对预报 {low:.0f}%）"})
            sw = fc_row.get("sw")
            # Measured where a satellite covers the station; the inferred
            # figure judges only where none does (it reads high under haze
            # and mid/high cloud the report cannot see).
            seen, basis = (sat_sw, "卫星实测") if product else (inferred, "报文推算")
            if seen is not None and sw is not None and max(seen, sw) >= RADIATION_DIFF_MIN_WM2:
                gap = seen - sw
                if abs(gap) >= RADIATION_DIFF_MIN_WM2 and abs(gap) >= RADIATION_DIFF_FRAC * max(sw, 1.0):
                    diffs.append({"kind": "sw", "dir": "more" if gap > 0 else "less",
                                  "text": f"日照{'强于' if gap > 0 else '弱于'}预报（{basis} {seen:.0f} 对预报 {sw:.0f} W/m²）"})
            t_fc = fc_row.get("temp_c")
            if t_fc is not None and obs.get("temp_c") is not None:
                gap = obs["temp_c"] - t_fc
                if abs(gap) >= TEMP_DIFF_C:
                    diffs.append({"kind": "temp", "dir": "more" if gap > 0 else "less",
                                  "text": f"比预报{'暖' if gap > 0 else '冷'} {abs(gap):.2f}°C"})
            vis = obs.get("visibility_m")
            if vis is not None and vis < HAZE_VISIBILITY_M:
                diffs.append({"kind": "haze", "dir": "less", "text": f"能见度 {vis:.0f} m（霾/雾，削弱日照）"})
            if obs.get("precip"):
                diffs.append({"kind": "rain", "dir": "less", "text": "有降水"})
        rows.append(row)
    sums = {k: (round(sum(r["forecast"].get(k) or 0 for r in rows) * 0.0036, 2) if times else None)
            for k in ("sw", "sw_d1")}
    past = [r for r in rows if r["obs"] and r["obs"]["inferred_sw_wm2"] is not None]
    measured = [r for r in rows if r["past"] and r["sat_sw_wm2"] is not None]
    return {
        "available": bool(times), "reason": fc.get("reason"),
        "source": "Open-Meteo 逐小时（aggregator_republisher，仅作参考，不进任何层）",
        "fetched_at_utc": fc.get("fetched_at_utc"), "rows": rows,
        "forecast_sw_mj": sums["sw"], "forecast_sw_d1_mj": sums["sw_d1"],
        "satellite": {"product": product, "label": SATELLITE_PRODUCTS.get(product or "", ""),
                      "via": SATELLITE_VIA.get(product or "", SATELLITE_VIA_DEFAULT),
                      "latest": sat.get("latest"),
                      "available": bool(sat.get("available")), "reason": sat.get("reason"),
                      "fetched_at_utc": sat.get("fetched_at_utc"),
                      "last_hour": max((r["hour"] for r in measured), default=None)},
        "sat_sw_mj_so_far": round(sum(r["sat_sw_wm2"] for r in measured) * 0.0036, 2) if measured else None,
        "sat_forecast_sw_mj_so_far": round(sum(r["forecast"].get("sw") or 0 for r in measured) * 0.0036, 2) if measured and times else None,
        "inferred_sw_mj_so_far": round(sum(r["obs"]["inferred_sw_wm2"] for r in past) * 0.0036, 2) if past else None,
        "forecast_sw_mj_so_far": round(sum(r["forecast"].get("sw") or 0 for r in past) * 0.0036, 2) if past and times else None,
        "sun_tmax_shift": _sun_tmax_shift(rows, measured) if times else None,
    }


def _taf(conn, station: str, day: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT issued_at_utc, provider_available_at_utc, revision_kind, revision_sequence,"
        " valid_from_utc, valid_until_utc, tx_c, tx_available, cloud_cover_fraction,"
        " precipitation_probability, wind_speed_mps FROM calibration_forward_taf_capture"
        " WHERE station_id=? AND event_date=? ORDER BY provider_available_at_utc, revision_sequence",
        (station, day),
    ).fetchall()
    captures = [{
        "issued_at_utc": r[0], "available_at_utc": r[1], "kind": r[2], "sequence": r[3],
        "valid_from_utc": r[4], "valid_until_utc": r[5], "tx_c": _float(r[6]),
        "tx_available": bool(r[7]), "cloud_cover_fraction": _float(r[8]),
        "precipitation_probability": _float(r[9]), "wind_speed_mps": _float(r[10]),
    } for r in rows]
    return {"captures": captures}


def _taf_inbox() -> dict[str, Any]:
    """A bounded read shared by the raw-text and exact cloud-height displays."""
    try:
        with TAF_INBOX_PATH.open("rb") as handle:
            blob = handle.read(8 * 1024 * 1024 + 1)
        if len(blob) > 8 * 1024 * 1024:
            return {}
        inbox = json.loads(blob)
    except (OSError, ValueError):
        return {}
    if not isinstance(inbox, dict) or not isinstance(inbox.get("captures"), list) or len(inbox["captures"]) > 2048:
        return {}
    return inbox


def _taf_cloud_layers(raw: str) -> list[dict[str, Any]]:
    """Display cloud bases from report tokens, without deriving new cloud fractions."""
    layers = []
    seen = set()
    for token in raw.split():
        match = re.fullmatch(r"(FEW|SCT|BKN|OVC|VV)(\d{3}|///)(CB|TCU)?", token)
        if match is None or token in seen:
            continue
        seen.add(token)
        cover, height, convective = match.groups()
        feet = None if height == "///" else int(height) * 100
        metres = None if feet is None else feet * 0.3048
        band = "vertical_visibility" if cover == "VV" else "unknown" if metres is None else (
            "below_2km" if metres < 2000 else "2_to_6km" if metres < 6000 else "at_or_above_6km")
        layers.append({"code": token, "cover": cover, "height_ft_agl": feet,
                       "height_m_agl": None if metres is None else round(metres, 1),
                       "base_band": band, "convective": convective})
    return layers


def _taf_cloud_evidence(conn, station: str, day: str, witness: Mapping[str, Any], inbox: Mapping[str, Any]) -> dict[str, Any]:
    """Bind report-height facts to the capture the P0 actually witnessed."""
    absent = {"state": "unavailable", "layers": []}
    revision, digest = witness.get("forecast_revision_id"), witness.get("capture_content_sha256")
    if not revision or not digest:
        return absent
    row = conn.execute(
        "SELECT source_object_sha256 FROM calibration_forward_taf_capture"
        " WHERE station_id=? AND event_date=? AND revision_id=? AND content_sha256=?"
        " AND source_class='official_forward_native_receipt' LIMIT 1",
        (station, day, revision, digest),
    ).fetchone()
    if row is None:
        return absent
    for capture in inbox.get("captures") or []:
        if not isinstance(capture, Mapping):
            continue
        reference = capture.get("forecast_revision") or {}
        if not isinstance(reference, Mapping) or (
            reference.get("station_id"), reference.get("event_date"), reference.get("revision_id"),
            reference.get("source_object_sha256")) != (station, day, revision, row[0]):
            continue
        raw = capture.get("raw_taf")
        if not isinstance(raw, str) or len(raw) > 16384 or hashlib.sha256(raw.encode("utf-8")).hexdigest() != row[0]:
            return absent
        return {"state": "matched", "source_object_sha256": row[0], "layers": _taf_cloud_layers(raw)}
    return absent


def _taf_groups(raw: str, issued_at_utc: str | None) -> dict[str, Any] | None:
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import report_explain as rex  # noqa: PLC0415
    except ImportError:
        return None
    return rex.taf_groups(raw, issued_at_utc)


def _taf_raw(station: str, inbox: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    inbox = _taf_inbox() if inbox is None else inbox
    out: dict[str, dict[str, Any]] = {}
    for capture in inbox.get("captures") or []:
        revision = capture.get("forecast_revision") or {}
        if str(revision.get("station_id") or "") != station:
            continue
        raw = str(capture.get("raw_taf") or "").strip()
        if not raw:
            continue
        issued = revision.get("issued_at_utc") or capture.get("issued_at_utc")
        out[str(capture.get("raw_taf_sha256") or raw)] = {
            "issued_at_utc": issued, "raw": raw,
            # Base forecast and change groups, as the METAR table (「TAF 同理」).
            "groups": _taf_groups(raw, issued),
        }
    return sorted(out.values(), key=lambda r: str(r.get("issued_at_utc") or ""))[-3:][::-1]


def _precip(conn, station: str, day: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT source_id, captured_at_utc, capture_mode, hourly_precip_mm_json, precip_sum_mm,"
        " wet_hours, first_wet_hour, peak_hour FROM calibration_station_forecast_precip_hourly_version"
        " WHERE station_id=? AND local_date=? ORDER BY captured_at_utc DESC LIMIT 1",
        (station, day),
    ).fetchone()
    if row is None:
        return None
    return {"source_id": row[0], "captured_at_utc": row[1], "capture_mode": row[2],
            "hourly_mm": _json(row[3], None), "sum_mm": _float(row[4]),
            "wet_hours": row[5], "first_wet_hour": row[6], "peak_hour": row[7],
            "lineage": "aggregator_republisher（参考，不进任何层）"}


def _precip_sat(station: str, day: str, zone: ZoneInfo) -> dict[str, Any] | None:
    """The day's measured rain from space, JAXA GSMaP_NOW (user 2026-10-15
    「云雨数据呢」 → 「可以」): local hour h (the forecast column's stamp, the
    hour ending at h:00) reads the half-hour file that starts at h-1:00 local
    -- exact in whole- and half-hour zones; hours whose file is not public yet
    are absent.  None off the 60N..60S band."""

    from . import jaxa_gsmap as gsmap  # noqa: PLC0415

    where = _COORDINATES_FOR_BOARD(station)
    if where is None or gsmap.cell(*where) is None:
        return None
    d = datetime.fromisoformat(day).date()
    now_utc = datetime.now(timezone.utc)
    starts: dict[int, datetime] = {}
    # Keyed like the forecast's hourly series: stamp HH is the hour ending HH:00.
    for h in range(24):
        start = (datetime.combine(d, dtime(0), zone) + timedelta(hours=h - 1)).astimezone(timezone.utc)
        if start + gsmap.PUBLISH_AFTER <= now_utc:
            starts[h] = start
    try:
        got = gsmap.rain(station, list(starts.values()))
    except Exception as exc:  # noqa: BLE001 -- the card says why
        return {"available": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
    hours = {}
    for h, start in starts.items():
        reading = got.get(gsmap.slot_key(start))
        if reading:
            hours[f"{h:02d}"] = reading
    reader = gsmap.status()
    return {"available": bool(hours), "source": gsmap.LABEL, "hours": hours,
            "pending_hours": len(starts) - len(hours), "fetched_at_utc": reader.get("last_read_at_utc"),
            "reason": reader.get("last_error"),
            "sum_mm": round(sum((v.get("mm") or 0.0) for v in hours.values()), 1) if hours else None}


def _COORDINATES_FOR_BOARD(station: str) -> tuple[float, float] | None:
    from .station_registry import _COORDINATES  # noqa: PLC0415

    got = _COORDINATES.get(station.upper())
    return None if got is None else (float(got[0]), float(got[1]))


def _day(conn, station: str, day: str, role: str, zone: ZoneInfo, *, taf_inbox: Mapping[str, Any] | None = None) -> dict[str, Any]:
    heads = {stage: _envelope_head(conn, station, day, stage) for stage in STAGES}
    newest = max((h for h in heads.values() if h), key=lambda h: str(h["decided_at_utc"]), default=None)
    if newest is None:
        return {"role": role, "event_date": day, "available": False}
    axis = ladder_axis(newest["ordinals"],
                       _json(newest["lineage"].get("bucket_intervals_c_json"), {}))
    metar = _metar(station, day, zone)
    running_high = max((m["temp_c"] for m in metar if m["temp_c"] is not None), default=None)
    stages = {}
    for stage, head in heads.items():
        if head is None:
            continue
        stats = vector_stats(head["vector"], axis)
        if stats is not None:
            stats["skew_floor"] = skew_floor(stats["mode"], axis, running_high)
        entry = {
            "decided_at_utc": head["decided_at_utc"], "fresh_until_utc": head["fresh_until_utc"],
            "version": head["version"], "checkpoint_id": head["checkpoint_id"],
            "vector": {k: round(v, 5) for k, v in head["vector"].items()},
            "stats": stats,
        }
        if stage == "P0":
            entry.update(_p0_brief(head, axis, _previous_p0(
                conn, station, day, str(head["decided_at_utc"])), station=station, day=day, conn=conn))
            if entry.get("taf_witness"):
                entry["taf_witness"]["cloud_evidence"] = _taf_cloud_evidence(
                    conn, station, day, entry["taf_witness"], taf_inbox or {})
            if entry.get("radiation"):
                entry["radiation"].update(_radiation_capture(
                    conn, station, day, str(head["decided_at_utc"]), entry["radiation"].get("radiation_sum_mj")))
        elif stage == "P0-M":
            entry.update(_p0m_brief(head))
        else:
            entry.update(_p1_brief(head))
            entry["root_center_c"] = _p1_root_center(conn, head, station, day)
        stages[stage] = entry
    head_stage = day_word(stages)
    market = _market(conn, station, day, axis)
    if isinstance(market.get("stats"), dict):
        market["stats"]["skew_floor"] = skew_floor(market["stats"]["mode"], axis, running_high)
    precip = _precip(conn, station, day)
    precip_sat = _precip_sat(station, day, zone)
    sky = _hourly_vs_forecast(conn, station, day, zone, metar)
    return {
        "role": role, "event_date": day, "available": True, "axis": axis,
        "head_stage": head_stage, "stages": stages,
        "timeline": _timeline(conn, station, day, axis),
        "market": market,
        "metar": metar,
        "running_high_c": running_high,
        "taf": _taf(conn, station, day),
        "precip": precip,
        "precip_sat": precip_sat,
        "precip_5min": _precip_5min(station, day, zone),
        "hourly_sky": sky,
        "direction": _direction(station, day),
        "deterministic": _deterministic(conn, station, day),
        "d0": _d0(station, day) if role == "today" else None,
        "member_bayes": _member_bayes(station, day) if role in ("today", "ahead") else None,
    }


# The D0 observation model (user 2026-10-15 「不要结案，作为只读」 → 「后台计算，
# 前端只读，保留历史数据」): computed every ten minutes by d0_readings.py
# (launchd com.weather.d0_readings); the board shows the stored reading and
# the day's history.  Walk-forward it is behind both the market (~0.2 C) and
# the served P1 (~0.1 C); it enters no layer.
def _d0(station: str, day: str) -> dict[str, Any] | None:
    from . import d0_readings  # noqa: PLC0415

    return d0_readings.stored(station, day)


def _member_bayes(station: str, day: str) -> dict[str, Any] | None:
    from . import member_bayes_readings  # noqa: PLC0415

    return member_bayes_readings.stored(station, day)


def day_word(stages: Mapping[str, Any]) -> str | None:
    """The day's word: the most informed stage present (charter 260's
    precedence, ``stage_authority.BELIEF_STAGE_PRECEDENCE``: P1 > P0-M > P0).

    It was the newest stage decided, but P0-M (and P0) keep committing after
    P1 exists, so the word flipped back to a superseded stage -- over
    2026-09-01..10-10 the day's last P0-M came after its first P1 on 1,582 of
    1,687 station-days, and those late P0-M vectors kept a dead upside (on
    their right-skewed days the truth sat on the mode 85 % of the time
    against 48 % claimed).
    """

    from .stage_authority import BELIEF_STAGE_PRECEDENCE  # noqa: PLC0415

    return next((stage for stage in BELIEF_STAGE_PRECEDENCE if stage in stages), None)


def _direction_board() -> dict[str, Any] | None:
    """The fleet's direction snapshot, re-read only when the file changed."""

    try:
        stamp = DIRECTION_BOARD_PATH.stat().st_mtime_ns
    except OSError:
        return None
    if _DIRECTION_CACHE.get("stamp") != stamp:
        try:
            doc = json.loads(DIRECTION_BOARD_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        _DIRECTION_CACHE.update(stamp=stamp, doc=doc)
    return _DIRECTION_CACHE.get("doc")


def _direction(station: str, day: str) -> dict[str, Any] | None:
    """One station-day's direction reading from the snapshot, with the
    instant it was computed; None when the snapshot has none."""

    doc = _direction_board()
    if not doc:
        return None
    entry = ((doc.get("stations") or {}).get(station) or {}).get(day)
    if not entry:
        return None
    return {**entry, "computed_at_utc": doc.get("computed_at_utc"),
            "momentum_quiet_below": doc.get("momentum_quiet_below"),
            "run_due_hours": dict(DIRECTION_RUN_DUE_HOURS),
            "snapshot_stale_sec": DIRECTION_SNAPSHOT_STALE_SEC}


#: The source charter 222's radiation sum is read from
#: (scripts/capture_station_forecast_physics.py).
RADIATION_SOURCE_ID = "OPEN_METEO_MODEL_FORECAST"


def _radiation_capture(conn, station: str, day: str, decided_at: str, used_mj: float | None) -> dict[str, Any]:
    """When the radiation sum P0 read was captured, and the newest capture
    since (user 2026-10-13 「把两处的辐射总量…突出，并显示数据的陈旧度」).

    P0 reads the station-day's current physics row at compile time, so the
    capture it used is the newest version at or before its decision;
    ``first_seen_utc`` is when that value first arrived (a re-capture that
    returns the same number does not make the forecast newer).  A value that
    matches no version is reported, never guessed."""

    rows = [(str(at), float(v)) for at, v in conn.execute(
        "SELECT captured_at_utc, shortwave_radiation_sum FROM calibration_station_forecast_physics_version"
        " WHERE station_id=? AND local_date=? AND source_id=? AND shortwave_radiation_sum IS NOT NULL"
        " ORDER BY captured_at_utc",
        (station, day, RADIATION_SOURCE_ID),
    )]
    decided = _utc(decided_at)
    before = [r for r in rows if decided is not None and (_utc(r[0]) or decided) <= decided]
    out: dict[str, Any] = {"capture_state": "no_capture_record"}
    if before and used_mj is not None and abs(before[-1][1] - float(used_mj)) < 0.005:
        first = len(before) - 1
        while first > 0 and abs(before[first - 1][1] - before[-1][1]) < 0.005:
            first -= 1
        out = {"capture_state": "matched", "captured_at_utc": before[-1][0], "first_seen_utc": before[first][0]}
    elif before:
        out = {"capture_state": "value_not_in_record"}
    if rows and (not before or rows[-1][0] != before[-1][0]):
        out.update(latest_mj=round(rows[-1][1], 2), latest_captured_at_utc=rows[-1][0])
    return out


def _listing_f() -> dict[str, Any] | None:
    """The listing-time F file, re-read only when it changed."""

    try:
        stamp = LISTING_F_PATH.stat().st_mtime_ns
    except OSError:
        return None
    if _LISTING_F_CACHE.get("stamp") != stamp:
        try:
            doc = json.loads(LISTING_F_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        _LISTING_F_CACHE.update(stamp=stamp, doc=doc)
    return _LISTING_F_CACHE.get("doc")


def _deterministic(conn, station: str, day: str) -> dict[str, Any]:
    """Every connected deterministic model's newest run for the station-day
    and the run before it (first capture of each run, as charter 201's
    readers take it), plus the listing-time F built from six of them.  The
    runs are the stored ``calibration_regional_point_run`` rows; F is the
    resident's file.  Display only."""

    from .deterministic_forecast_family import F_SYSTEM_IDS, FAMILY, SYSTEM_IDS  # noqa: PLC0415

    product_of = {SYSTEM_IDS[product]: product for product in FAMILY}
    runs: dict[str, list[tuple[str, str, float]]] = {}
    marks = ",".join("?" * len(product_of))
    for system, init, knowable, value in conn.execute(
        "SELECT ensemble_system_id, run_init_utc, MIN(knowable_at_utc), forecast_tmax_c"
        " FROM calibration_regional_point_run"
        f" WHERE station_id=? AND target_local_date=? AND ensemble_system_id IN ({marks})"
        " AND knowable_at_utc IS NOT NULL AND forecast_tmax_c IS NOT NULL"
        " GROUP BY ensemble_system_id, run_init_utc",
        (station, day, *product_of),
    ):
        runs.setdefault(str(system), []).append((str(init), str(knowable), float(value)))
    order = {product: index for index, product in enumerate(FAMILY)}
    systems = []
    for system, held in runs.items():
        held.sort()
        newest, before = held[-1], (held[-2] if len(held) > 1 else None)
        product = product_of[system]
        systems.append({
            "product": product, "system_id": system, "in_f": system in F_SYSTEM_IDS,
            "relay": bool(FAMILY[product].get("relay")),
            "run_init_utc": newest[0], "knowable_at_utc": newest[1], "tmax_c": round(newest[2], 2),
            "prev_run_init_utc": before[0] if before else None,
            "prev_tmax_c": round(before[2], 2) if before else None,
        })
    systems.sort(key=lambda row: (not row["in_f"], order[row["product"]]))
    doc = _listing_f() or {}
    entry = ((doc.get("station_days") or {}).get(f"{station}|{day}")) or {}
    f = None
    if entry:
        f = {"f_c": entry.get("f_c"), "raw_median_c": entry.get("raw_median_c"), "n_systems": entry.get("n_systems"),
             "state": entry.get("state"), "computed_at_utc": doc.get("computed_at_utc"),
             "f_v3_c": (entry.get("v3") or {}).get("f_c"),
             # Charter 267 补记七: the keep-F pick, written unverified (display only).
             "pick": entry.get("pick")}
    return {"systems": systems, "f": f}


def _precip_5min(station: str, day: str, zone: ZoneInfo) -> dict[str, Any] | None:
    """MADIS five-minute rain for US ASOS stations (board only); None elsewhere."""

    if not station.upper().startswith("K"):
        return None
    from . import madis_precip_board  # noqa: PLC0415

    try:
        return madis_precip_board.station_day(station, day, zone)
    except Exception as exc:  # noqa: BLE001 -- the card says why
        return {"available": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}


DAY_ROLES = (("yesterday", -1), ("today", 0), ("ahead", 1), ("ahead2", 2))


def _event_days(conn, station: str, now: datetime) -> tuple[str, list[tuple[str, str]]] | None:
    tz_name = station_timezone(conn, station)
    if not tz_name:
        return None
    zone = ZoneInfo(tz_name)
    today = now.astimezone(zone).date()
    # User 2026-10-16「同时展示过去一天、今天、d+1、d+2的日期」: four local days
    # side by side; today stays after its peak (the peak reading rides on it).
    days = [(role, (today + timedelta(days=offset)).isoformat()) for role, offset in DAY_ROLES]
    return tz_name, days


def _model_peak(conn, station: str, day: str, zone: ZoneInfo) -> dict[str, Any] | None:
    """When the models put the day's warmest hour: the top of the day's earliest
    P0 temperature curve (the earliest still spans the whole local day; later
    ones carry only the hours after their decision)."""

    from bayes_basket_target.materialization import rehydrate_lineage_documents  # noqa: PLC0415

    row = conn.execute(
        "SELECT json_extract(lineage_json,'$.conditional_future_temperature_curve_json')"
        " FROM basket_probability_envelope WHERE station_id=? AND event_date=?"
        " AND +stage='P0' AND state='COMMITTED' ORDER BY decision_at_utc LIMIT 1",
        (station, day),
    ).fetchone()
    if row is None or not row[0]:
        return None
    try:
        body = rehydrate_lineage_documents(conn, {"curve": row[0]})["curve"]
    except Exception:  # noqa: BLE001 -- a missing body means no peak, not a crash
        return None
    lo, hi = _local_window(day, zone)
    points = [
        (when, float(p["temperature_c"]))
        for p in (_json(body, {}).get("points") or [])
        if (when := _utc(p.get("valid_time_utc"))) is not None
        and lo <= _iso(when) < hi and _float(p.get("temperature_c")) is not None
    ]
    if not points:
        return None
    when, temp = max(points, key=lambda x: x[1])
    return {"peak_at_utc": _iso(when), "peak_local": when.astimezone(zone).strftime("%H:%M"),
            "curve_peak_c": round(temp, 2), "curve_step_hours": 3}


def past_peak(conn, station: str, day: str, zone: ZoneInfo, now: datetime) -> dict[str, Any]:
    """User 2026-10-05「过了当日最高峰就不显示当日了」: past the models' warmest
    hour, and the newest report since then is already below the day's high
    (the reading has turned down)."""

    peak = _model_peak(conn, station, day, zone)
    metar = _metar(station, day, zone)
    high = max((m["temp_c"] for m in metar if m["temp_c"] is not None), default=None)
    latest = next((m for m in reversed(metar) if m["temp_c"] is not None), None)
    result = {"past": False, "peak": peak, "running_high_c": high,
              "latest_temp_c": None if latest is None else latest["temp_c"],
              "latest_local": None if latest is None else latest["local"]}
    if peak is None or latest is None or high is None:
        return result
    peak_at = _utc(peak["peak_at_utc"])
    latest_at = _utc(latest["valid_at_utc"])
    result["past"] = bool(
        now >= peak_at and latest_at is not None and latest_at >= peak_at
        and latest["temp_c"] < high
    )
    return result


def _token(conn, station: str, days: Sequence[tuple[str, str]], zone: ZoneInfo) -> str:
    """What the panel redraws on: the newest committed envelope of each day,
    and the newest report.

    User 2026-10-05 「改为概率有更新才刷新」: the board polls this (one index
    seek a day) and rebuilds only when a new P0/P0-M/P1 has been committed;
    the book is re-read with it, not on its own.  User 2026-10-10
    「METAR/SPECI……没有显示最新的metar」: a report lands between envelopes (the
    13:00 METAR sat unseen behind an unchanged token), so the day's newest
    METAR/SPECI (one index seek a day on the archive) and the TAF inbox's
    mtime are in it too.
    """

    parts = []
    for _role, day in days:
        row = conn.execute(
            "SELECT MAX(decision_at_utc), COUNT(*) FROM basket_probability_envelope"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'",
            (station, day),
        ).fetchone()
        parts.append(f"{day}:{row[0]}:{row[1]}")
    try:
        archive = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=5)
        try:
            for _role, day in days:
                lo, hi = _local_window(day, zone)
                row = archive.execute(
                    "SELECT MAX(valid_at), COUNT(*) FROM reported_trajectory_observations"
                    " WHERE station_id=? AND valid_at>=? AND valid_at<?"
                    " AND raw_text IS NOT NULL AND raw_text<>''",
                    (station, lo[:19], hi[:19]),
                ).fetchone()
                parts.append(f"obs{day}:{row[0]}:{row[1]}")
        finally:
            archive.close()
    except sqlite3.Error:
        parts.append("obs:unreadable")
    try:
        parts.append(f"taf:{TAF_INBOX_PATH.stat().st_mtime_ns}")
    except OSError:
        parts.append("taf:absent")
    # The direction snapshot (charter 266) is rewritten every ten minutes.
    try:
        parts.append(f"dir:{DIRECTION_BOARD_PATH.stat().st_mtime_ns}")
    except OSError:
        parts.append("dir:absent")
    return "|".join(parts)


def probability_token(station: str = "ZUUU", *, db: Path = DEFAULT_DB,
                      now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        resolved = _event_days(conn, station, now)
        token = None if resolved is None else _token(conn, station, resolved[1], ZoneInfo(resolved[0]))
    finally:
        conn.close()
    return {"schema": SCHEMA, "station_id": station, "token": token,
            "checked_at_utc": _iso(datetime.now(timezone.utc))}


def build(station: str = "ZUUU", *, db: Path = DEFAULT_DB, now: datetime | None = None) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    now = now or started
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("PRAGMA query_only=1")
        resolved = _event_days(conn, station, now)
        if resolved is None:
            return {"schema": SCHEMA, "station_id": station, "available": False,
                    "reason": "no_timezone_binding", "built_at_utc": _iso(started)}
        tz_name, event_days = resolved
        zone = ZoneInfo(tz_name)
        token = _token(conn, station, event_days, zone)
        stations = fleet(conn, now)
        taf_inbox = _taf_inbox()
        days = [_day(conn, station, day, role, zone, taf_inbox=taf_inbox) for role, day in event_days]
        today = now.astimezone(zone).date().isoformat()
        peak_today = past_peak(conn, station, today, zone, now)
        for day in days:
            if day["event_date"] == today:
                day["peak"] = peak_today
    finally:
        conn.close()
    finished = datetime.now(timezone.utc)
    return {
        "schema": SCHEMA, "station_id": station, "available": True, "timezone": tz_name,
        "token": token,
        "local_now": now.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
        "built_at_utc": _iso(finished),
        "build_sec": round((finished - started).total_seconds(), 2),
        "taf_raw": _taf_raw(station, taf_inbox),
        "taker_fee_rate": DEFAULT_TAKER_FEE_RATE,
        "fleet": stations,
        "days": days,
    }


def skew_history() -> dict[str, Any] | None:
    """The settled skew reading the board-readings job keeps
    (``board_readings.skew_truth_side``: which side the truth lands on skewed
    station-days, and the mode / centre-bucket hit rates, model against the
    market) -- read, never computed here.  None until the job has written it."""

    from .board_readings import load_reading  # noqa: PLC0415

    stored = load_reading("skew_truth_side")
    if not stored or not isinstance(stored.get("value"), dict):
        return None
    return {**stored["value"], "computed_at_utc": stored.get("computed_at_utc")}


def mode_agreement_history() -> dict[str, Any] | None:
    """The settled reading of where our mode and the market's agree
    (``board_readings.mode_agreement``: the truth's hit rate in the bucket of
    the first, second and last agreement, by stage, lead and day) -- read,
    never computed here; the per-station-day records stay in the file."""

    from .board_readings import load_reading  # noqa: PLC0415

    stored = load_reading("mode_agreement")
    if not stored or not isinstance(stored.get("value"), dict):
        return None
    value = {k: v for k, v in stored["value"].items() if k != "days"}
    return {**value, "computed_at_utc": stored.get("computed_at_utc")}


FLEET_SHAPE_SCHEMA = "station_day_fleet_shape_v1"


def shape_word(sign: Any) -> str:
    """Production's skew sign as a word: symmetric / left / right."""

    if sign is None:
        return "none"
    return "symmetric" if sign == 0 else "left" if sign < 0 else "right"


def _committed_market_stats(conn, station: str, day: str, axis) -> dict[str, Any] | None:
    """The newest committed book (the resident's once-a-cycle copy) as a
    ladder vector's statistics; None without a complete one."""

    from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: PLC0415

    row = conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED' ORDER BY observed_at DESC LIMIT 1",
        (station, day),
    ).fetchone()
    if row is None:
        return None
    ref = market_reference_v1(_json(row[0], []))
    if ref.get("state") != "complete":
        return None
    return vector_stats({str(k): float(v) for k, v in ref["vector"].items()}, axis)


def fleet_shape(*, db: Path = DEFAULT_DB, now: datetime | None = None) -> dict[str, Any]:
    """Every listed station's local today and tomorrow, counted by
    production's skew reading (user 2026-10-10 「48站点每日的均值桶和众数桶的
    位置的统计：对称、左偏、右偏分别有多少」; by probability since 2026-10-11).

    Per event date: the day's word (the head stage, as the panel's card reads
    it), each stage, and the market's newest committed book.  Read-only; the
    newest committed envelope of each stage, one index seek apiece.
    """

    started = datetime.now(timezone.utc)
    now = now or started
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    by_day: dict[str, dict[str, Any]] = {}
    try:
        conn.execute("PRAGMA query_only=1")
        stations = fleet(conn, now)
        for entry in stations:
            station = entry["station_id"]
            tz_name = station_timezone(conn, station)
            if not tz_name:
                continue
            today = now.astimezone(ZoneInfo(tz_name)).date()
            for offset in (0, 1):
                day = (today + timedelta(days=offset)).isoformat()
                heads = {stage: _envelope_head(conn, station, day, stage) for stage in STAGES}
                seen = {s: h for s, h in heads.items() if h}
                if not seen:
                    continue
                newest = max(seen.values(), key=lambda h: str(h["decided_at_utc"]))
                axis = ladder_axis(newest["ordinals"],
                                   _json(newest["lineage"].get("bucket_intervals_c_json"), {}))
                signs, gaps, floors = {}, {}, {}
                # The newest observation stage's running high (P1 over P0-M).
                high = next((h for h in (_lineage_running_high(seen[s]["lineage"], s)
                                         for s in ("P1", "P0-M") if s in seen) if h is not None), None)
                for stage, head in seen.items():
                    stats = vector_stats(head["vector"], axis) or {}
                    signs[stage], gaps[stage] = stats.get("skew_sign"), stats.get("skew_gap_buckets")
                    floors[stage] = skew_floor(stats.get("mode"), axis, high)
                market = _committed_market_stats(conn, station, day, axis)
                if market is not None:
                    signs["MKT"], gaps["MKT"] = market.get("skew_sign"), market.get("skew_gap_buckets")
                    floors["MKT"] = skew_floor(market.get("mode"), axis, high)
                head_stage = day_word(seen)
                book = by_day.setdefault(day, {"event_date": day, "rows": []})
                book["rows"].append({"station_id": station, "name": entry.get("name"),
                                     "head_stage": head_stage, "signs": signs, "gaps": gaps, "floors": floors})
    finally:
        conn.close()
    days = []
    for day in sorted(by_day):
        rows = by_day[day]["rows"]
        counts: dict[str, dict[str, int]] = {}
        for key in ("HEAD", *STAGES, "MKT"):
            tally = {"symmetric": 0, "left": 0, "right": 0, "floor": 0, "none": 0}
            for r in rows:
                k = r["head_stage"] if key == "HEAD" else key
                if key != "HEAD" and key not in r["signs"]:
                    continue
                # A floor day is counted apart, not as a lean (user 2026-10-16).
                tally["floor" if (r.get("floors") or {}).get(k) else shape_word(r["signs"].get(k))] += 1
            counts[key] = tally
        days.append({"event_date": day, "station_days": len(rows), "counts": counts, "rows": rows})
    finished = datetime.now(timezone.utc)
    return {"schema": FLEET_SHAPE_SCHEMA, "built_at_utc": _iso(finished),
            "build_sec": round((finished - started).total_seconds(), 2),
            "stations": len(stations), "days": days}


if __name__ == "__main__":  # pragma: no cover - manual probe
    print(json.dumps(build(sys.argv[1] if len(sys.argv) > 1 else "ZUUU"), ensure_ascii=False)[:4000])
