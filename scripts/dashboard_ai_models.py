"""Board section: the AI weather models -- AIFS ENS seated in P0, the
deterministic feature family (AIFS single point among it) in shadow, and the
NOAA AIGEFS research backfill.

Three products with three different standings, read from where each one
lives, so the page says which is which:

* **ECMWF AIFS ENS** (charter 204, 2026-09-23): a formal P0 member.  Its
  standing is read off the SSOT -- the system row, its calibration
  artifacts and activations (the qualification), the historical member rows
  (the backfill), the forward inbox (the run in hand), and the P0 envelopes
  committed over the last day (whether the served pool seats it and at what
  weight).
* **The deterministic feature family** (charter 201): AIFS single point,
  HRRR, NBM, ICON-EU, MSM, CMA GRAPES -- a centre-correction candidate per
  station, gated on its own out-of-sample record.  Read off the gate
  artifact and the P0 receipts; nothing of it serves while the mode is
  ``shadow``.
* **NOAA AIGEFS** (research only): every run since June into a research
  database on the cold volume; no production reader.

Read-only; every SSOT query seeks an index.  Missing files or an absent cold
volume are reported as such, never raised.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AIFS_ENS_SYSTEM_ID = "ECMWF_AIFS_ENS_GLOBAL"
AIFS_SINGLE_SYSTEM_ID = "ECMWF_AIFS_SINGLE_POINT"
AIGEFS_SYSTEM_ID = "NOAA_AIGEFS_025"
FORWARD_INBOX_PATH = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "official_native_ensemble_forward_inbox.json"
)
AIGEFS_RESEARCH_DIR = ROOT / "data" / "aigefs_ens_research"
AIGEFS_RESEARCH_DB = AIGEFS_RESEARCH_DIR / "aigefs_ens_allruns.db"
SERVED_WINDOW_HOURS = 24.0
MAX_SERVED_ROWS = 4000

SERVED_SQL = (
    "SELECT b.station_id, b.event_date, b.decision_at_utc, b.created_at_utc,"
    " e.posterior_version, e.lineage_json"
    " FROM basket_probability_bridge_history b"
    " JOIN basket_probability_envelope e ON e.envelope_id = b.checkpoint_id"
    " WHERE b.stage='P0' AND b.decision_at_utc>=? AND b.state='COMMITTED'"
    " ORDER BY b.decision_at_utc DESC LIMIT ?"
)


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _age_hours(value: Any, now: datetime) -> float | None:
    moment = _parse_utc(value)
    return None if moment is None else round((now - moment).total_seconds() / 3600.0, 2)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _loads(text: Any) -> Any:
    try:
        return json.loads(text) if text else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- AIFS ENS


def aifs_ens_standing(conn: sqlite3.Connection) -> dict[str, Any]:
    """Registry row, qualification and backfill of the seated product."""

    out: dict[str, Any] = {"system_id": AIFS_ENS_SYSTEM_ID}
    if _has_table(conn, "calibration_native_ensemble_system"):
        row = conn.execute(
            "SELECT canonical_name, independence_family_id, expected_member_count,"
            " valid_from_utc FROM calibration_native_ensemble_system"
            " WHERE ensemble_system_id=?", (AIFS_ENS_SYSTEM_ID,),
        ).fetchone()
        out["registered"] = row is not None
        if row:
            out["canonical_name"], out["family"] = row[0], row[1]
            out["expected_member_count"], out["valid_from_utc"] = row[2], row[3]
    if _has_table(conn, "calibration_native_ensemble_system_artifact"):
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT station_id),"
            " COUNT(DISTINCT lead_bucket||'/'||run_cycle)"
            " FROM calibration_native_ensemble_system_artifact"
            " WHERE ensemble_system_id=?", (AIFS_ENS_SYSTEM_ID,),
        ).fetchone()
        out["artifacts"] = {"count": row[0], "stations": row[1], "cells": row[2]}
    if _has_table(conn, "calibration_native_ensemble_activation"):
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT station_id), MAX(activated_at_utc)"
            " FROM calibration_native_ensemble_activation"
            " WHERE ensemble_system_id_or_star=?", (AIFS_ENS_SYSTEM_ID,),
        ).fetchone()
        out["activations"] = {
            "count": row[0], "stations": row[1], "latest_at_utc": row[2],
        }
    if _has_table(conn, "calibration_native_ensemble_historical_member_run"):
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT station_id),"
            " MIN(target_local_date), MAX(target_local_date)"
            " FROM calibration_native_ensemble_historical_member_run"
            " WHERE ensemble_system_id=?", (AIFS_ENS_SYSTEM_ID,),
        ).fetchone()
        out["history"] = {
            "rows": row[0], "stations": row[1],
            "first_date": row[2], "last_date": row[3],
        }
    return out


def aifs_ens_forward(inbox_path: Path, *, now: datetime) -> dict[str, Any]:
    """The AIFS run each forward packet carries, off the official inbox."""

    try:
        inbox = json.loads(Path(inbox_path).read_text())
    except (OSError, ValueError):
        return {"available": False}
    packets = inbox.get("packets") if isinstance(inbox, dict) else None
    if not isinstance(packets, list):
        return {"available": False}
    stations: dict[str, dict[str, Any]] = {}
    latest_init: str | None = None
    with_aifs = 0
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        inits = packet.get("run_init_utc_by_system") or {}
        init = inits.get(AIFS_ENS_SYSTEM_ID) if isinstance(inits, dict) else None
        if not init:
            continue
        with_aifs += 1
        station = str(packet.get("station_id") or "")
        entry = {
            "event_date": packet.get("event_date"),
            "run_init_utc": init,
            "run_cycle": (packet.get("run_cycle_by_system") or {}).get(AIFS_ENS_SYSTEM_ID),
            "lead_bucket": (packet.get("lead_bucket_by_system") or {}).get(AIFS_ENS_SYSTEM_ID),
            "received_at_utc": packet.get("received_at_utc"),
        }
        if station not in stations or str(init) > str(stations[station]["run_init_utc"]):
            stations[station] = entry
        if latest_init is None or str(init) > latest_init:
            latest_init = str(init)
    return {
        "available": True,
        "packets": len(packets),
        "packets_with_aifs": with_aifs,
        "stations_with_aifs": len(stations),
        "latest_run_init_utc": latest_init,
        "latest_run_age_hours": _age_hours(latest_init, now),
        "stations": stations,
    }


def aifs_ens_served(conn: sqlite3.Connection, *, now: datetime) -> dict[str, Any]:
    """Whether the P0 envelopes committed over the last day seat AIFS, and
    at what weight; the newest per station is the one the page lists."""

    from bayes_basket_target.materialization import (  # noqa: PLC0415
        rehydrate_lineage_documents,
    )

    floor = (now - timedelta(hours=SERVED_WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
    pools: Counter[tuple[str, ...]] = Counter()
    versions: Counter[str] = Counter()
    latest: dict[str, dict[str, Any]] = {}
    packets = with_aifs = with_aigefs = 0
    weights_seen: list[float] = []
    aigefs_weights: list[float] = []
    shadow: Counter[str] = Counter()
    shadow_abs: list[float] = []
    for station, day, decided, created, version, lineage_json in conn.execute(
        SERVED_SQL, (floor, MAX_SERVED_ROWS)
    ):
        try:
            lineage = rehydrate_lineage_documents(conn, json.loads(lineage_json))
        except Exception:  # noqa: BLE001 -- one unreadable row is not the section
            continue
        weights = _loads(lineage.get("system_weights_json")) or {}
        systems = tuple(sorted(str(k) for k in weights))
        packets += 1
        pools[systems] += 1
        versions[str(version)] += 1
        aifs_w = weights.get(AIFS_ENS_SYSTEM_ID)
        if aifs_w is not None:
            with_aifs += 1
            weights_seen.append(float(aifs_w))
        # AIGEFS has been a P0 member since charter 212 (2026-09-24): read off
        # the same served rows, the same way.
        aigefs_w = weights.get(AIGEFS_SYSTEM_ID)
        if aigefs_w is not None:
            with_aigefs += 1
            aigefs_weights.append(float(aigefs_w))
        feature = _loads(lineage.get("deterministic_feature_json"))
        if isinstance(feature, dict):
            shadow[str(feature.get("state") or "?")] += 1
            try:
                shadow_abs.append(abs(
                    float(feature.get("shadow_centre_c")) - float(feature.get("centre_c"))
                ))
            except (TypeError, ValueError):
                pass
        scope = f"{station}|{day}"
        if scope not in latest or str(decided) > latest[scope]["decision_at_utc"]:
            latest[scope] = {
                "station_id": str(station), "event_date": str(day),
                "decision_at_utc": str(decided), "version": str(version),
                "systems": list(systems),
                "aifs_weight": None if aifs_w is None else round(float(aifs_w), 3),
                "aigefs_weight": None if aigefs_w is None else round(float(aigefs_w), 3),
                "weights": {k: round(float(v), 3) for k, v in weights.items()},
            }
    weights_seen.sort()
    by_day: dict[str, dict[str, int]] = {}
    for entry in latest.values():
        slot = by_day.setdefault(
            entry["event_date"], {"scopes": 0, "with_aifs": 0, "with_aigefs": 0}
        )
        slot["scopes"] += 1
        slot["with_aifs"] += entry["aifs_weight"] is not None
        slot["with_aigefs"] += entry["aigefs_weight"] is not None
    return {
        "window_hours": SERVED_WINDOW_HOURS,
        "p0_packets": packets,
        "with_aifs": with_aifs,
        # The newest P0 per station-day: AIFS seats where its run for that
        # day is in hand (D+2 waits for the 18Z run), so this is per scope.
        "scopes_seated": sum(1 for v in latest.values() if v["aifs_weight"] is not None),
        "scopes_seen": len(latest),
        "by_event_date": dict(sorted(by_day.items())),
        "aifs_weight_median": (
            round(weights_seen[len(weights_seen) // 2], 3) if weights_seen else None
        ),
        "aifs_weight_max": round(weights_seen[-1], 3) if weights_seen else None,
        "with_aigefs": with_aigefs,
        "aigefs_weight_median": (
            round(sorted(aigefs_weights)[len(aigefs_weights) // 2], 3)
            if aigefs_weights else None
        ),
        "versions": dict(versions),
        "pools": [
            {"systems": list(systems), "count": count}
            for systems, count in pools.most_common()
        ],
        "scopes": sorted(latest.values(), key=lambda v: (v["event_date"], v["station_id"])),
        "deterministic_shadow": {
            "states": dict(shadow),
            "mean_abs_shift_c": (
                round(sum(shadow_abs) / len(shadow_abs), 3) if shadow_abs else None
            ),
            "max_abs_shift_c": round(max(shadow_abs), 3) if shadow_abs else None,
        },
    }


# ------------------------------------------------- deterministic feature family


def deterministic_family(gate_path: Path | None = None) -> dict[str, Any]:
    """The charter 201 gate: per station, the candidate and whether it passed."""

    from bayes_basket_target import deterministic_feature_correction as DFC  # noqa: PLC0415

    path = Path(gate_path) if gate_path is not None else DFC.GATE_ARTIFACT_PATH
    out: dict[str, Any] = {
        "mode": DFC.MODE, "rule": DFC.RULE_VERSION, "cap_c": DFC.CAP_C,
        "gate_min_event_days": DFC.GATE_MIN_EVENT_DAYS,
        "aifs_single_system_id": AIFS_SINGLE_SYSTEM_ID,
    }
    try:
        gate = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        out["gate_available"] = False
        return out
    stations = gate.get("stations") if isinstance(gate.get("stations"), dict) else {}
    systems: Counter[str] = Counter()
    selected = 0
    rows = []
    for station in sorted(stations):
        entry = stations[station] or {}
        candidate = entry.get("candidate_system")
        chosen = entry.get("selected_system")
        selected += bool(chosen)
        sources = entry.get("sources") if isinstance(entry.get("sources"), dict) else {}
        for system in sources:
            systems[str(system)] += 1
        source = sources.get(candidate) if candidate else None
        source = source if isinstance(source, dict) else {}
        rows.append({
            "station_id": station,
            "candidate_system": candidate,
            "selected_system": chosen,
            "event_days": source.get("event_days"),
            "d_rps": source.get("d_rps"),
            "verdict": source.get("verdict"),
            "aifs_single": (
                (sources.get(AIFS_SINGLE_SYSTEM_ID) or {}).get("d_rps")
                if isinstance(sources.get(AIFS_SINGLE_SYSTEM_ID), dict) else None
            ),
        })
    out.update({
        "gate_available": True,
        "fitted_at_utc": gate.get("fitted_at_utc"),
        "window": gate.get("window"),
        "stations": len(stations),
        "selected": selected,
        "systems": dict(systems),
        "aifs_single_stations": systems.get(AIFS_SINGLE_SYSTEM_ID, 0),
        "by_station": rows,
    })
    return out


# ---------------------------------------------------------------- AIGEFS


def aigefs_research(
    db_path: Path = AIGEFS_RESEARCH_DB, research_dir: Path = AIGEFS_RESEARCH_DIR,
    *, now: datetime,
) -> dict[str, Any]:
    """The research backfill's own tables; absent volume reported, not raised."""

    out: dict[str, Any] = {
        # A P0 member since charter 212 (2026-09-24); this block is its
        # research backfill, and its seating is read off the served P0s
        # (``aifs_ens_served``'s with_aigefs) like AIFS's.
        "system_id": AIGEFS_SYSTEM_ID, "in_production": True,
        "db_path": str(db_path),
    }
    plans = sorted(Path(research_dir).glob("plan_*.json")) if Path(research_dir).is_dir() else []
    if plans:
        stem = plans[-1].stem.split("_")
        if len(stem) >= 3:
            out["plan"] = {"from": stem[1], "to": stem[2]}
    log = Path(research_dir) / "backfill.log"
    try:
        out["log_updated_at_utc"] = _utc_iso(
            datetime.fromtimestamp(log.stat().st_mtime, tz=timezone.utc)
        )
        out["log_age_hours"] = _age_hours(out["log_updated_at_utc"], now)
    except OSError:
        pass
    if not Path(db_path).exists():
        out["available"] = False
        return out
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        out.update({"available": False, "error": f"{type(exc).__name__}: {exc}"[:120]})
        return out
    try:
        if _has_table(conn, "research_aigefs_runs"):
            row = conn.execute(
                "SELECT COUNT(*), MIN(run_init_utc), MAX(run_init_utc),"
                " MAX(finished_at_utc), SUM(rows), SUM(station_days)"
                " FROM research_aigefs_runs"
            ).fetchone()
            out["runs"] = {
                "count": row[0], "first_run_init_utc": row[1], "last_run_init_utc": row[2],
                "last_finished_at_utc": row[3], "rows": row[4], "station_days": row[5],
            }
        if _has_table(conn, "research_aigefs_missing_runs"):
            out["missing_runs"] = conn.execute(
                "SELECT COUNT(*) FROM research_aigefs_missing_runs"
            ).fetchone()[0]
        if _has_table(conn, "calibration_native_ensemble_historical_member_run"):
            row = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT station_id),"
                " MIN(target_local_date), MAX(target_local_date)"
                " FROM calibration_native_ensemble_historical_member_run"
            ).fetchone()
            out["member_rows"] = {
                "rows": row[0], "stations": row[1],
                "first_date": row[2], "last_date": row[3],
            }
        out["available"] = True
    except sqlite3.Error as exc:
        out.update({"available": False, "error": f"{type(exc).__name__}: {exc}"[:120]})
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------- section


def collect_ai_models(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    inbox_path: Path = FORWARD_INBOX_PATH,
    gate_path: Path | None = None,
    research_db: Path = AIGEFS_RESEARCH_DB,
    research_dir: Path = AIGEFS_RESEARCH_DIR,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    served = aifs_ens_served(conn, now=now)
    return {
        "available": True,
        "generated_at_utc": _utc_iso(now),
        "aifs_ens": {
            "charter": 204,
            "standing": aifs_ens_standing(conn),
            "forward": aifs_ens_forward(inbox_path, now=now),
            "served": served,
        },
        "deterministic_family": {
            "charter": 201,
            **deterministic_family(gate_path),
            "served": served.get("deterministic_shadow"),
        },
        "aigefs": aigefs_research(research_db, research_dir, now=now),
    }


if __name__ == "__main__":  # pragma: no cover - a hand-run reading
    import sys

    db = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "bayes_basket_target_ssot.db"
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        print(json.dumps(collect_ai_models(connection), ensure_ascii=False, indent=1)[:6000])
    finally:
        connection.close()
