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
import math
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
SCHEMA = "station_day_board_v1"
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
    """Each rung with its °C bounds and midpoint, lowest first.

    An open edge takes its neighbour's width (a C rung is 1°C, an F rung
    2°F = 1.111°C), so its midpoint sits one half-rung inside the bound.
    """

    order = sorted(ordinals, key=lambda b: int(ordinals[b]))
    widths = []
    for b in order:
        lo, hi = (list(intervals.get(b) or [None, None]) + [None, None])[:2]
        if lo is not None and hi is not None:
            widths.append(float(hi) - float(lo))
    rung = min(widths) if widths else 1.0
    out = []
    for b in order:
        lo, hi = (list(intervals.get(b) or [None, None]) + [None, None])[:2]
        lo = _float(lo)
        hi = _float(hi)
        if lo is not None and hi is not None:
            centre = (lo + hi) / 2.0
        elif hi is not None:
            centre = hi - rung / 2.0
        elif lo is not None:
            centre = lo + rung / 2.0
        else:
            centre = None
        out.append({"bucket": str(b), "ordinal": int(ordinals[b]), "lo_c": lo, "hi_c": hi,
                    "centre_c": None if centre is None else round(centre, 4)})
    return out


def bucket_of(axis: Sequence[Mapping[str, Any]], celsius: float | None) -> str | None:
    if celsius is None:
        return None
    for row in axis:
        lo, hi = row["lo_c"], row["hi_c"]
        if (lo is None or celsius >= lo) and (hi is None or celsius < hi):
            return row["bucket"]
    return None


def vector_stats(vector: Mapping[str, float], axis: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Mean, variance, spread and the two summary rungs of a ladder vector.

    ``mode`` is the largest rung, the lower on a tie (the rule charter 249's
    market mode uses); ``mean_bucket`` is the rung the mean falls in.
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
    return {
        "mean_c": round(mean, 3), "var_c2": round(var, 4), "sd_c": round(sd, 3),
        "skew": None if sd <= 1e-9 else round(third / sd ** 3, 3),
        "mode": rows[mode_index][0]["bucket"], "mode_p": round(rows[mode_index][1], 4),
        "mean_bucket": bucket_of(axis, mean),
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


def _p0_brief(head: Mapping[str, Any], axis) -> dict[str, Any]:
    L = head["lineage"]
    rad = _json(L.get("synoptic_cooling_json"), {})
    width = _json(L.get("center_width_json"), {})
    bridge = _json(L.get("consensus_bridge_json"), {})
    taf_w = _json(L.get("taf_envelope_witness_json"), {})
    ceiling = _json(L.get("far_peak_ceiling_json"), {})
    return {
        "models": _models(L, axis),
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
        "state": receipt.get("state"), "update": receipt.get("update"),
        "running_high_c": _float(L.get("metar_trajectory_running_high_c")),
        "rise_probability": _float(receipt.get("rise_probability")),
        "alpha": _float(receipt.get("alpha")), "lambda": _float(receipt.get("lambda")),
        "features": receipt.get("features") or {},
        "likelihood_by_bucket": receipt.get("likelihood_by_bucket") or {},
        "excluded": _excluded(L.get("hard_excluded_buckets")),
        "root_stage": L.get("prediction_root_stage"),
    }


def _timeline(conn, station: str, day: str, axis) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT stage, decision_at_utc, probabilities_json, posterior_version"
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
    for key, (stage, at, probs, version) in keep.items():
        if key in drop:
            continue
        vec = _norm(_json(probs, {}))
        stats = vector_stats(vec, axis)
        if stats is None:
            continue
        out.append({"stage": stage, "at_utc": str(at), "version": version,
                    "vector": {k: round(v, 5) for k, v in vec.items()}, "stats": stats})
    out.sort(key=lambda e: (e["at_utc"], STAGES.index(e["stage"]) if e["stage"] in STAGES else 9))
    return out


def _weight_history(conn, station: str, day: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT decision_at_utc, json_extract(lineage_json,'$.system_weights_json'),"
        " json_extract(lineage_json,'$.run_init_utc_by_system_json')"
        " FROM basket_probability_envelope WHERE station_id=? AND event_date=?"
        " AND +stage='P0' AND state='COMMITTED' ORDER BY decision_at_utc",
        (station, day),
    ).fetchall()
    return [{"at_utc": str(at), "weights": _json(w, {}), "run_init_utc": _json(r, {})}
            for at, w, r in rows]


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
            "ask_levels": [[p, s] for p, s in asks[:12]],
            "bid_levels": [[p, s] for p, s in bids[:6]],
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
            (station, lo, hi),
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
        out.append({
            "valid_at_utc": str(at),
            "local": when.astimezone(zone).strftime("%H:%M") if when else "",
            "raw": str(raw), "kind": parsed.get("kind") or kind,
            "correction_suspected": bool(cor),
            "temp_c": t, "dew_c": _float(parsed.get("dew_c")),
            "cloud_cover": _float(parsed.get("cover")),
            "wind_mps": _float(parsed.get("wind_mps")),
            "precip": bool(parsed.get("precip")),
            "weathers": parsed.get("weathers") or [],
            "running_high_c": running,
        })
    return out


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


def _taf_raw(station: str) -> list[dict[str, Any]]:
    try:
        inbox = json.loads(TAF_INBOX_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: dict[str, dict[str, Any]] = {}
    for capture in inbox.get("captures") or []:
        revision = capture.get("forecast_revision") or {}
        if str(revision.get("station_id") or "") != station:
            continue
        raw = str(capture.get("raw_taf") or "").strip()
        if not raw:
            continue
        out[str(capture.get("raw_taf_sha256") or raw)] = {
            "issued_at_utc": revision.get("issued_at_utc") or capture.get("issued_at_utc"),
            "raw": raw,
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


def _day(conn, station: str, day: str, role: str, zone: ZoneInfo) -> dict[str, Any]:
    heads = {stage: _envelope_head(conn, station, day, stage) for stage in STAGES}
    newest = max((h for h in heads.values() if h), key=lambda h: str(h["decided_at_utc"]), default=None)
    if newest is None:
        return {"role": role, "event_date": day, "available": False}
    axis = ladder_axis(newest["ordinals"],
                       _json(newest["lineage"].get("bucket_intervals_c_json"), {}))
    stages = {}
    for stage, head in heads.items():
        if head is None:
            continue
        entry = {
            "decided_at_utc": head["decided_at_utc"], "fresh_until_utc": head["fresh_until_utc"],
            "version": head["version"], "checkpoint_id": head["checkpoint_id"],
            "vector": {k: round(v, 5) for k, v in head["vector"].items()},
            "stats": vector_stats(head["vector"], axis),
        }
        if stage == "P0":
            entry.update(_p0_brief(head, axis))
        elif stage == "P0-M":
            entry.update(_p0m_brief(head))
        else:
            entry.update(_p1_brief(head))
        stages[stage] = entry
    # The day's word: the newest stage decided (P1 > P0-M > P0 on a tie).
    head_stage = max(stages, key=lambda s: (str(stages[s]["decided_at_utc"])[:19], STAGES.index(s)))
    metar = _metar(station, day, zone)
    return {
        "role": role, "event_date": day, "available": True, "axis": axis,
        "head_stage": head_stage, "stages": stages,
        "timeline": _timeline(conn, station, day, axis),
        "weight_history": _weight_history(conn, station, day),
        "market": _market(conn, station, day, axis),
        "metar": metar,
        "running_high_c": max((m["temp_c"] for m in metar if m["temp_c"] is not None), default=None),
        "taf": _taf(conn, station, day),
        "precip": _precip(conn, station, day),
    }


def _event_days(conn, station: str, now: datetime) -> tuple[str, list[tuple[str, str]]] | None:
    tz_name = station_timezone(conn, station)
    if not tz_name:
        return None
    today = now.astimezone(ZoneInfo(tz_name)).date()
    return tz_name, [(role, (today + timedelta(days=offset)).isoformat())
                     for role, offset in (("today", 0), ("ahead", 1))]


def _token(conn, station: str, days: Sequence[tuple[str, str]]) -> str:
    """What the panel redraws on: the newest committed envelope of each day.

    User 2026-10-05 「改为概率有更新才刷新」: the board polls this (one index
    seek a day) and rebuilds only when a new P0/P0-M/P1 has been committed;
    the book is re-read with it, not on its own.
    """

    parts = []
    for _role, day in days:
        row = conn.execute(
            "SELECT MAX(decision_at_utc), COUNT(*) FROM basket_probability_envelope"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'",
            (station, day),
        ).fetchone()
        parts.append(f"{day}:{row[0]}:{row[1]}")
    return "|".join(parts)


def probability_token(station: str = "ZUUU", *, db: Path = DEFAULT_DB,
                      now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        resolved = _event_days(conn, station, now)
        token = None if resolved is None else _token(conn, station, resolved[1])
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
        token = _token(conn, station, event_days)
        days = [_day(conn, station, day, role, zone) for role, day in event_days]
    finally:
        conn.close()
    finished = datetime.now(timezone.utc)
    return {
        "schema": SCHEMA, "station_id": station, "available": True, "timezone": tz_name,
        "token": token,
        "local_now": now.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
        "built_at_utc": _iso(finished),
        "build_sec": round((finished - started).total_seconds(), 2),
        "taf_raw": _taf_raw(station),
        "taker_fee_rate": DEFAULT_TAKER_FEE_RATE,
        "days": days,
    }


if __name__ == "__main__":  # pragma: no cover - manual probe
    print(json.dumps(build(sys.argv[1] if len(sys.argv) > 1 else "ZUUU"), ensure_ascii=False)[:4000])
