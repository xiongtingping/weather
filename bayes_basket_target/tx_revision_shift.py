"""Charter 282: P1 moved by the latest TAF TX revision, in a narrow window only.

User ruling 2026-10-26 「按方案上线（默认影子、每日拟合过 240 门才服务），升版重启」 on
``docs/research/taf_tx_revision_20261026`` (GATED.md): when the event day's TX
changed between consecutive bulletins, a P1 decided within WINDOW_HOURS of that
bulletin's issue and between LOCAL_HOURS local is moved by

    delta = kappa * (TX now - TX before)    (degrees C; TAF TX is C everywhere)

and every other P1 is left as compiled.  The window was read off the same
history the rule is scored on (gated decisions -0.018 [-0.050, +0.012], not
significant), so the layer serves only while its own walk-forward passes
charter 240 (``scripts/fit_tx_revision_shift.py``); otherwise the receipt
records the move it would have made and the vector is untouched (SHADOW).

The move is a translation of the bucket vector read as uniform within each
cell (an open bucket cut at the median width), applied after the joint
likelihood and the hard bound and before charter 224's floor; a bucket the
reports ruled out stays at zero.  A bulletin counts only once it was received
at the decision (``received_at_utc``), so the window effectively opens on
arrival.  Inputs ride on the P1 packet (``tx_revision_shift``) so a replay
rebuilds the envelope exactly.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

RULE_VERSION = "tx_revision_shift_v1"
LINEAGE_KEY = "tx_revision_shift_json"
FIT_SCHEMA = "bayes_basket_target_tx_revision_shift_fit_v1"
ROOT = Path(__file__).resolve().parents[1]
FIT_ARTIFACT_PATH = Path(
    os.environ.get("PM_TX_REVISION_SHIFT_FIT")
    or ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "tx_revision_shift_fit.json"
)
#: ``serve`` serves an artifact whose walk-forward passes; ``shadow`` never moves the vector.
MODE = os.environ.get("PM_TX_REVISION_SHIFT_MODE", "serve").strip().lower()
WINDOW_HOURS = 3.0
#: Local hours [start, end) of the event day in which the move applies.
LOCAL_HOURS = (10, 16)
#: A bulletin issued after this local hour of the event day is not read as the day's.
ISSUED_BY_LOCAL_HOUR = 15
VERDICT_MIN_EVENT_DAYS = 20

BULLETINS_SQL = (
    "SELECT issued_at_utc, received_at_utc, tx_c, revision_sequence, revision_id "
    "FROM calibration_forward_taf_capture "
    "WHERE station_id=? AND event_date=? AND tx_c IS NOT NULL AND received_at_utc<=? "
    "ORDER BY issued_at_utc, revision_sequence"
)

_FIT_CACHE: dict[str, Any] = {}


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(timezone.utc)


def _original(revision_id: str) -> str:
    return str(revision_id or "").split("~", 1)[0]


def load_bulletins(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, as_of_utc: str
) -> list[dict[str, Any]] | None:
    """The event day's TX bulletins received by ``as_of_utc``; none without the TAF lane.

    None when the read fails: P1 then compiles without the layer (no bundle,
    no receipt) rather than not at all -- the move is optional, P1 is not.
    """

    try:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='calibration_forward_taf_capture'"
        ).fetchone() is None:
            return []
        return [
            {"issued_at_utc": str(r[0]), "received_at_utc": str(r[1]), "tx_c": float(r[2]),
             "revision_sequence": int(r[3] or 0), "revision_id": str(r[4] or "")}
            for r in conn.execute(BULLETINS_SQL, (station_id, event_date, as_of_utc))
        ]
    except sqlite3.Error:
        return None


def latest_revision(
    bulletins: Sequence[Mapping[str, Any]],
    *,
    decision_at_utc: str,
    event_date: str,
    settlement_timezone: str,
) -> dict[str, Any] | None:
    """The last TX change knowable at the decision, or None."""

    t = _parse(decision_at_utc)
    zone = ZoneInfo(settlement_timezone)
    y, m, d = (int(x) for x in event_date.split("-"))
    issued_by = datetime(y, m, d, tzinfo=zone) + timedelta(hours=ISSUED_BY_LOCAL_HOUR)
    known = [
        b for b in bulletins
        if _parse(b["issued_at_utc"]) <= t and _parse(b["received_at_utc"]) <= t
        and _parse(b["issued_at_utc"]) <= issued_by
    ]
    ids = [str(b["revision_id"]) for b in known]
    # A restated bulletin is replaced by its newest restatement (「以最新的为准」).
    known = [
        b for b in known
        if not any(other > str(b["revision_id"]) and _original(other) == _original(b["revision_id"]) for other in ids)
    ]
    by_issue: dict[str, Mapping[str, Any]] = {}
    for b in sorted(known, key=lambda b: (b["issued_at_utc"], b["revision_sequence"])):
        by_issue[b["issued_at_utc"]] = b
    seq = [by_issue[k] for k in sorted(by_issue)]
    last = None
    for before, now in zip(seq, seq[1:]):
        if float(now["tx_c"]) != float(before["tx_c"]):
            last = (before, now)
    if last is None:
        return None
    before, now = last
    return {
        "issued_at_utc": now["issued_at_utc"], "received_at_utc": now["received_at_utc"],
        "revision_id": now["revision_id"], "tx_c": float(now["tx_c"]),
        "previous_tx_c": float(before["tx_c"]), "step_c": float(now["tx_c"]) - float(before["tx_c"]),
        "hours_since_issue": round((t - _parse(now["issued_at_utc"])).total_seconds() / 3600.0, 6),
    }


def local_hour(decision_at_utc: str, *, event_date: str, settlement_timezone: str) -> int | None:
    """The decision's local hour on the event day, None on any other day."""

    local = _parse(decision_at_utc).astimezone(ZoneInfo(settlement_timezone))
    return local.hour if local.date().isoformat() == event_date else None


def in_window(revision: Mapping[str, Any] | None, hour: int | None) -> bool:
    return (
        revision is not None and hour is not None and LOCAL_HOURS[0] <= hour < LOCAL_HOURS[1]
        and 0.0 <= float(revision["hours_since_issue"]) < WINDOW_HOURS
    )


def load_fit_artifact(path: Path | None = None) -> dict[str, Any] | None:
    """The daily fit, re-read when its file changes; None when absent or unreadable."""

    target = Path(path or FIT_ARTIFACT_PATH)
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        return None
    cached = _FIT_CACHE.get(str(target))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        raw = target.read_bytes()
        payload = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != FIT_SCHEMA:
        return None
    payload["content_sha256"] = hashlib.sha256(raw).hexdigest()
    _FIT_CACHE[str(target)] = (stamp, payload)
    return payload


def fit_summary(fit: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What a P1 packet carries of the fit: kappa, the verdict and the artifact's identity."""

    if not fit:
        return None
    return {
        "kappa": float(fit.get("kappa") or 0.0),
        "verdict": str((fit.get("walk_forward") or {}).get("verdict") or "INSUFFICIENT"),
        "fitted_at_utc": str(fit.get("fitted_at_utc") or ""),
        "content_sha256": str(fit.get("content_sha256") or ""),
    }


def step_bundle(
    bulletins: Sequence[Mapping[str, Any]] | None,
    *,
    decision_at_utc: str,
    event_date: str,
    settlement_timezone: str,
    fit: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The inputs one P1 step reads, carried on its packet."""

    return {
        "rule": RULE_VERSION,
        "revision": latest_revision(
            bulletins or (), decision_at_utc=decision_at_utc, event_date=event_date,
            settlement_timezone=settlement_timezone,
        ),
        "local_hour": local_hour(decision_at_utc, event_date=event_date, settlement_timezone=settlement_timezone),
        "fit": fit,
    }


def geometry(ordered: Sequence[str], intervals: Mapping[str, Sequence[float | None]]) -> dict[str, list[float]]:
    """Source cells (open bucket cut at the median width) and target bounds."""

    widths = sorted(float(hi) - float(lo) for lo, hi in (intervals[k] for k in ordered) if lo is not None and hi is not None)
    w = widths[len(widths) // 2] if widths else 1.0
    slo, shi, tlo, thi = [], [], [], []
    for key in ordered:
        lo, hi = intervals[key]
        slo.append(float(lo) if lo is not None else float(hi) - w)
        shi.append(float(hi) if hi is not None else float(lo) + w)
        tlo.append(float(lo) if lo is not None else -math.inf)
        thi.append(float(hi) if hi is not None else math.inf)
    return {"slo": slo, "shi": shi, "tlo": tlo, "thi": thi}


def shift_vector(vector: Sequence[float], geom: Mapping[str, Sequence[float]], delta: float) -> list[float]:
    """The vector translated by ``delta`` C; a zero bucket stays zero.  No floor."""

    n = len(vector)
    out = [0.0] * n
    for i, p in enumerate(vector):
        if p <= 0.0:
            continue
        lo, hi = geom["slo"][i] + delta, geom["shi"][i] + delta
        for j in range(n):
            overlap = min(hi, geom["thi"][j]) - max(lo, geom["tlo"][j])
            if overlap > 0.0:
                out[j] += p * overlap / (hi - lo)
    out = [0.0 if vector[j] <= 0.0 else v for j, v in enumerate(out)]
    total = sum(out)
    if total <= 0.0:
        base = sum(vector)
        return [v / base for v in vector]
    return [v / total for v in out]


def apply(
    probabilities: Mapping[str, float],
    *,
    bucket_ordinals: Mapping[str, int],
    intervals_json: str | None,
    bundle: Mapping[str, Any],
    mode: str | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """The P1 vector after the layer and its receipt."""

    mode = (mode or MODE).strip().lower()
    revision, hour, fit = bundle.get("revision"), bundle.get("local_hour"), bundle.get("fit")
    receipt: dict[str, Any] = {
        "rule": RULE_VERSION, "revision": revision, "local_hour": hour, "fit": fit,
        "window_hours": WINDOW_HOURS, "local_hours": list(LOCAL_HOURS),
    }
    if not in_window(revision, hour):
        return dict(probabilities), {**receipt, "state": "IDLE",
                                     "reason": "NO_REVISION" if revision is None else "OUTSIDE_WINDOW"}
    if not fit:
        return dict(probabilities), {**receipt, "state": "IDLE", "reason": "NO_FIT"}
    try:
        intervals = json.loads(intervals_json or "")
        ordered = sorted(probabilities, key=lambda k: (bucket_ordinals[k], k))
        geom = geometry(ordered, intervals)
    except (KeyError, TypeError, ValueError):
        return dict(probabilities), {**receipt, "state": "IDLE", "reason": "NO_GEOMETRY"}
    delta = float(fit["kappa"]) * float(revision["step_c"])
    moved = dict(zip(ordered, shift_vector([float(probabilities[k]) for k in ordered], geom, delta)))
    receipt = {**receipt, "delta_c": round(delta, 6), "pre_shift_probabilities": dict(probabilities),
               "shifted_probabilities": moved}
    if fit.get("verdict") != "PASS":
        return dict(probabilities), {**receipt, "state": "SHADOW", "reason": "GATE_NOT_PASSED"}
    if mode != "serve":
        return dict(probabilities), {**receipt, "state": "SHADOW", "reason": "MODE_SHADOW"}
    return moved, {**receipt, "state": "SERVED"}


__all__ = [
    "FIT_ARTIFACT_PATH", "FIT_SCHEMA", "LINEAGE_KEY", "RULE_VERSION", "apply", "fit_summary",
    "geometry", "in_window", "latest_revision", "load_bulletins", "load_fit_artifact",
    "local_hour", "shift_vector", "step_bundle",
]
