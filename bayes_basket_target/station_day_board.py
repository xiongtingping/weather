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
#: The member packets production compiled each P0 from, one file per packet,
#: named by the first 16 hex of the snapshot sha the envelope's lineage keeps.
FORWARD_PACKET_ARCHIVE = ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "forward_packet_archive"
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
              *, station: str = "", day: str = "") -> dict[str, Any]:
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
            "running_high_c": running,
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
_FORECAST_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
OPEN_METEO_PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
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
    "jma_jaxa_himawari": "JAXA Himawari",
    "eumetsat_lsa_saf_iodc": "EUMETSAT 印度洋（IODC）",
    "dwd_sis_europe_africa_v4": "DWD SIS 欧洲/非洲",
    "eumetsat_lsa_saf_msg": "EUMETSAT MSG",
    "noaa_goes_dsr": "NOAA GOES-19/18 DSR",
}


def _satellite_product(lon: float) -> str | None:
    if lon >= 90.0 or lon <= -150.0:
        return "jma_jaxa_himawari"
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

    key = (f"{lat:.3f},{lon:.3f}", day)
    hit = _FORECAST_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < FORECAST_CACHE_SECONDS:
        return hit[1]
    fields = ["cloud_cover", "cloud_cover_low", "shortwave_radiation", "temperature_2m"]
    query = urlencode({
        "latitude": lat, "longitude": lon, "timezone": tz, "start_date": day, "end_date": day,
        "hourly": ",".join(fields + [f + "_previous_day1" for f in fields]),
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
    if product and started:
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
            "cloud_d1": val("cloud_cover", True), "cloud_low_d1": val("cloud_cover_low", True),
            "sw_d1": val("shortwave_radiation", True), "temp_c_d1": val("temperature_2m", True),
        } if times else {}
        obs = by_hour.get(h)
        sat_sw = sat_values[h] if h < len(sat_values) else None
        row: dict[str, Any] = {"hour": h, "past": start <= now_local, "forecast": fc_row, "obs": None,
                               "sat_sw_wm2": None if sat_sw is None else round(sat_sw, 0), "diffs": []}
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
                      "available": bool(sat.get("available")), "reason": sat.get("reason"),
                      "fetched_at_utc": sat.get("fetched_at_utc"),
                      "last_hour": max((r["hour"] for r in measured), default=None)},
        "sat_sw_mj_so_far": round(sum(r["sat_sw_wm2"] for r in measured) * 0.0036, 2) if measured else None,
        "sat_forecast_sw_mj_so_far": round(sum(r["forecast"].get("sw") or 0 for r in measured) * 0.0036, 2) if measured and times else None,
        "inferred_sw_mj_so_far": round(sum(r["obs"]["inferred_sw_wm2"] for r in past) * 0.0036, 2) if past else None,
        "forecast_sw_mj_so_far": round(sum(r["forecast"].get("sw") or 0 for r in past) * 0.0036, 2) if past and times else None,
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


def _day(conn, station: str, day: str, role: str, zone: ZoneInfo, *, taf_inbox: Mapping[str, Any] | None = None) -> dict[str, Any]:
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
            entry.update(_p0_brief(head, axis, _previous_p0(
                conn, station, day, str(head["decided_at_utc"])), station=station, day=day))
            if entry.get("taf_witness"):
                entry["taf_witness"]["cloud_evidence"] = _taf_cloud_evidence(
                    conn, station, day, entry["taf_witness"], taf_inbox or {})
        elif stage == "P0-M":
            entry.update(_p0m_brief(head))
        else:
            entry.update(_p1_brief(head))
            entry["root_center_c"] = _p1_root_center(conn, head, station, day)
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
        "hourly_sky": _hourly_vs_forecast(conn, station, day, zone, metar),
    }


def _event_days(conn, station: str, now: datetime) -> tuple[str, list[tuple[str, str]]] | None:
    tz_name = station_timezone(conn, station)
    if not tz_name:
        return None
    zone = ZoneInfo(tz_name)
    today = now.astimezone(zone).date()
    days = [(role, (today + timedelta(days=offset)).isoformat())
            for role, offset in (("today", 0), ("ahead", 1))]
    if past_peak(conn, station, days[0][1], zone, now)["past"]:
        days = days[1:]
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
        # Today, once dropped for being past its peak: why, in one line.
        "today_past_peak": None if any(d["role"] == "today" for d in days) else {
            "event_date": today, **peak_today},
        "local_now": now.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
        "built_at_utc": _iso(finished),
        "build_sec": round((finished - started).total_seconds(), 2),
        "taf_raw": _taf_raw(station, taf_inbox),
        "taker_fee_rate": DEFAULT_TAKER_FEE_RATE,
        "days": days,
    }


if __name__ == "__main__":  # pragma: no cover - manual probe
    print(json.dumps(build(sys.argv[1] if len(sys.argv) > 1 else "ZUUU"), ensure_ascii=False)[:4000])
