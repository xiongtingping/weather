#!/usr/bin/env python3
"""Charter 207: reconcile our settled buckets against what the market paid.

A reader.  It asks Gamma for the fleet's highest-temperature events over the
last ``--days`` event dates, reads the standing truth labels from the SSOT
through a read-only connection held for a couple of seconds, and writes

* ``data/settlement_truth_reconciliation_status.json`` -- this pass: counts
  by verdict, the disagreements, the window; what the board reads; and
* ``data/settlement_truth_reconciliation_history.jsonl`` -- one line per
  disagreement the first time it is seen (and again if its verdict or either
  bucket changes), so a disagreement that later heals still has a record.

Without ``--repair`` it never writes the SSOT, a truth, a label, an order
or a registry row, so it runs outside the resident's writer rotation
(single-writer rule): a pass that holds nothing cannot starve anything.  That
is the three-hourly pass.

``--repair`` is the daily pass (charter 225 (八), user 2026-09-25: 「每天把新
结算的市场和真值对一遍，不一致就按结算源自动修补」).  Every disagreement is
re-settled through ``settlement_source_truth`` from the market's own
settlement evidence, captured outside any lock (``settlement_source_capture``):
Wunderground's page, or -- NOAA's series being origin-locked to weather.gov --
the market's official resolution, exact for a one-degree bucket.  Each repair
is one short BEGIN IMMEDIATE.  Evidence that cannot be fetched or does not
determine a value leaves the day as it is and is listed.  Bounded: ``--max-repairs`` a pass, a free-disk floor,
the process memory budget.  The status is read again after the repairs, and
each repair outcome enters ``settlement_source_repair_history.jsonl`` once.

    python3 scripts/settlement_truth_reconciliation.py            # write status
    python3 scripts/settlement_truth_reconciliation.py --days 40  # wider window
    python3 scripts/settlement_truth_reconciliation.py --dry-run  # print only
    python3 scripts/settlement_truth_reconciliation.py --repair   # daily repair
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.candidate_window_evidence import standing_labels  # noqa: E402
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.market_registry import settlement_timezone_for_scope  # noqa: E402
from bayes_basket_target.resource_bounds import assert_process_memory_budget  # noqa: E402
from bayes_basket_target.settlement_reconciliation import (  # noqa: E402
    DISAGREEMENTS,
    SCHEMA,
    reconcile,
    summarize,
)
from bayes_basket_target.settlement_source_capture import (  # noqa: E402
    capture,
    capture_resolution,
    load_case,
)
from bayes_basket_target.settlement_source_truth import (  # noqa: E402
    WU_FAMILY,
    materialize_settlement_source_truth,
)
from reported_trajectory_market_registry import (  # noqa: E402
    fetch_gamma_events_window,
    load_config,
    settlement_contract,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STATUS_JSON = ROOT / "data" / "settlement_truth_reconciliation_status.json"
HISTORY_JSONL = ROOT / "data" / "settlement_truth_reconciliation_history.jsonl"
REPAIR_STATUS_JSON = ROOT / "data" / "settlement_source_repair_status.json"
REPAIR_HISTORY_JSONL = ROOT / "data" / "settlement_source_repair_history.jsonl"
CAPTURES = ROOT / "data" / "settlement_source_captures"
MAX_REPAIRS = 20
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
# Gamma's tag listing returns at most four 100-row pages a call; three event
# dates of ~51 cities fit inside that with room to spare.
GAMMA_DAYS_PER_CALL = 3
MAX_DAYS = 45


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _market_of(event: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """The repair's view of one resolved market, from the event itself."""

    contract = settlement_contract(str(event.get("description") or ""))
    if contract["station_id"] != row["station_id"]:
        raise ContractError("settlement_source_market_station_mismatch")
    return {
        "slug": str(event.get("slug") or ""),
        "family": contract["family"],
        "unit": contract["unit"],
        "resolution_source": str(event.get("resolutionSource") or ""),
        "winning_bucket": str(row["market_bucket"] or ""),
        "closed": bool(event.get("closed")) and row["market_state"] == "RESOLVED",
    }


def repair(
    rows: list[dict[str, Any]],
    events_by_slug: dict[str, dict[str, Any]],
    *,
    db: Path,
    captures: Path,
    max_repairs: int,
    client: Any,
) -> list[dict[str, Any]]:
    """Re-settle each disagreement from its market's page (charter 225)."""

    disagreements = [row for row in rows if row["verdict"] in DISAGREEMENTS]
    results: list[dict[str, Any]] = []
    for row in disagreements[max_repairs:]:
        results.append({"station_id": row["station_id"], "event_date": row["event_date"],
                        "verdict": row["verdict"], "state": "deferred_repair_cap"})
    targets = disagreements[:max_repairs]
    if not targets:
        return results
    captures.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(captures).free < MIN_FREE_BYTES:
        return results + [{"station_id": row["station_id"], "event_date": row["event_date"],
                           "verdict": row["verdict"], "state": "refused",
                           "reason": "free_disk_below_floor"} for row in targets]
    conn = sqlite3.connect(str(db), timeout=600.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=600000")
    try:
        for row in targets:
            key = {"station_id": row["station_id"], "event_date": row["event_date"],
                   "verdict": row["verdict"], "market_bucket": row["market_bucket"],
                   "our_bucket": row["our_bucket"]}
            try:
                event = events_by_slug[row["slug"]]
                market = _market_of(event, row)
                zone = settlement_timezone_for_scope(
                    conn, station_id=row["station_id"], event_date=row["event_date"])
                if not zone:
                    raise ContractError("settlement_source_settlement_timezone_unresolved")
                # Wunderground's page is read directly; NOAA's series is
                # origin-locked to weather.gov, so a NOAA day is evidenced by
                # the market's own resolution.
                if market["family"] == WU_FAMILY:
                    capture(client, market=market, station_id=row["station_id"],
                            event_date=row["event_date"], captures=captures)
                else:
                    capture_resolution(event, station_id=row["station_id"],
                                       event_date=row["event_date"], captures=captures)
                case = load_case(market=market, station_id=row["station_id"],
                                 event_date=row["event_date"], captures=captures,
                                 settlement_timezone=zone, root=ROOT)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    receipt = materialize_settlement_source_truth(
                        conn, case=case, as_of_utc=_now())
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                state = "repaired" if receipt.get("wrote") else str(receipt["state"])
                results.append({**key, "state": state, **{
                    field: receipt.get(field) for field in (
                        "basis", "official_tmax_c", "truth_id", "revision_no")}})
            except Exception as exc:  # noqa: BLE001 - one day never stops the pass
                results.append({**key, "state": "refused",
                                "reason": f"{type(exc).__name__}:{str(exc)[:160]}"})
            assert_process_memory_budget()
    finally:
        conn.close()
    return results


def _append_repair_history(path: Path, results: list[dict[str, Any]], *, checked_at: str) -> int:
    seen: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["key"])
            except (ValueError, KeyError, TypeError):
                continue
    fresh = []
    for row in results:
        key = "|".join(str(row.get(field)) for field in (
            "station_id", "event_date", "state", "market_bucket", "truth_id", "reason"))
        if key not in seen:
            seen.add(key)
            fresh.append({"key": key, "first_seen_at": checked_at, **row})
    if fresh:
        with path.open("a", encoding="utf-8") as handle:
            for row in fresh:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(fresh)


def _events(start: date, end: date) -> list[dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    day = start
    while day <= end:
        last = min(end, day + timedelta(days=GAMMA_DAYS_PER_CALL - 1))
        for event in fetch_gamma_events_window(start_date=day, end_date=last, max_pages=4):
            events[str(event.get("slug") or "")] = event
        day = last + timedelta(days=1)
    return [events[slug] for slug in sorted(events) if slug]


def _standing(db: Path, floor: str) -> tuple[dict[tuple[str, str], dict[str, Any]], Any]:
    """Standing labels, plus a reader for one truth's evidence document."""

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10.0)
    rows, _counts = standing_labels(conn, floor_event_date=floor, only_unlabelled=False)
    ours = {
        (str(row[0]), str(row[1])): {
            "revision": int(row[2]), "bucket": str(row[3]), "tmax_c": float(row[4]),
        }
        for row in rows
    }

    def evidence_of(station: str, day: str) -> Any:
        found = conn.execute(
            "SELECT observation_ids_json FROM calibration_station_daily_high_truth"
            " WHERE station_id=? AND local_date=? AND qc_status='ACCEPTED'"
            " ORDER BY revision_no DESC LIMIT 1",
            (station, day),
        ).fetchone()
        try:
            return json.loads(found[0]) if found else None
        except (TypeError, ValueError):
            return None

    return ours, (conn, evidence_of)


def _client() -> Any:
    import httpx  # noqa: PLC0415 - only the repair pass touches the pages

    from official_nwp_https_transport import official_nwp_proxy_url  # noqa: PLC0415

    return httpx.Client(
        proxy=official_nwp_proxy_url() or None, trust_env=False,
        timeout=httpx.Timeout(60.0, connect=20.0), follow_redirects=False,
    )


def _append_history(path: Path, disagreements: list[dict[str, Any]], *, checked_at: str) -> int:
    seen: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["key"])
            except (ValueError, KeyError, TypeError):
                continue
    fresh = []
    for row in disagreements:
        key = "|".join(str(row.get(field)) for field in (
            "station_id", "event_date", "verdict", "our_bucket", "market_bucket", "our_revision",
        ))
        if key not in seen:
            fresh.append({"key": key, "first_seen_at": checked_at, **row})
    if fresh:
        with path.open("a", encoding="utf-8") as handle:
            for row in fresh:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(fresh)


def _reconcile(db: Path, start: date, events: list[dict[str, Any]],
               city_stations: dict[str, str]) -> list[dict[str, Any]]:
    ours, (conn, evidence_of) = _standing(db, start.isoformat())
    try:
        return reconcile(
            events=events, ours=ours, city_stations=city_stations, evidence_of=evidence_of,
        )
    finally:
        conn.close()


def run(
    *, days: int, db: Path, status_path: Path, history_path: Path, dry_run: bool,
    repair_pass: bool = False, captures: Path = CAPTURES, max_repairs: int = MAX_REPAIRS,
    repair_status_path: Path = REPAIR_STATUS_JSON,
    repair_history_path: Path = REPAIR_HISTORY_JSONL,
    client_factory: Any = None,
) -> dict[str, Any]:
    days = max(1, min(MAX_DAYS, int(days)))
    today = datetime.now(timezone.utc).date()
    start, end = today - timedelta(days=days), today - timedelta(days=1)
    config = load_config()
    cities = config["settlement_source_contract"]["cities"]
    city_stations = {city: str(entry["station_id"]) for city, entry in cities.items()}
    events = _events(start, end)
    rows = _reconcile(db, start, events, city_stations)
    repairs: list[dict[str, Any]] | None = None
    if repair_pass and not dry_run:
        events_by_slug = {str(event.get("slug") or ""): event for event in events}
        with (client_factory or _client)() as client:
            repairs = repair(rows, events_by_slug, db=db, captures=captures,
                             max_repairs=max(0, int(max_repairs)), client=client)
        # What the board reads is the state after the repairs.
        rows = _reconcile(db, start, events, city_stations)
    checked_at = _now()
    summary = summarize(rows)
    status = {
        "schema": SCHEMA,
        "checked_at": checked_at,
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "event_count": len(events),
        "station_day_count": len(rows),
        **summary,
        "writes_ssot": repairs is not None,
        "calls_order_gateway": False,
    }
    if dry_run:
        return status
    if repairs is not None:
        states: dict[str, int] = {}
        for row in repairs:
            states[row["state"]] = states.get(row["state"], 0) + 1
        repair_status = {
            "schema": "settlement_source_repair_status_v1",
            "charter": 225,
            "checked_at": checked_at,
            "window": status["window"],
            "attempted": len(repairs),
            "states": dict(sorted(states.items())),
            "results": repairs,
            "remaining_disagreements": status["disagreements"],
            "history_appended": _append_repair_history(
                repair_history_path, repairs, checked_at=checked_at),
            "calls_order_gateway": False,
        }
        temporary = repair_status_path.with_name(f".{repair_status_path.name}.tmp.{os.getpid()}")
        temporary.write_text(json.dumps(repair_status, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(temporary, repair_status_path)
        status["repair"] = {"attempted": len(repairs), "states": repair_status["states"]}
    status["history_appended"] = _append_history(
        history_path, summary["disagreements"], checked_at=checked_at,
    )
    temporary = status_path.with_name(f".{status_path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temporary, status_path)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--status-out", type=Path, default=STATUS_JSON)
    parser.add_argument("--history", type=Path, default=HISTORY_JSONL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repair", action="store_true",
                        help="charter 225: re-settle disagreements from the settlement page")
    parser.add_argument("--max-repairs", type=int, default=MAX_REPAIRS)
    parser.add_argument("--captures", type=Path, default=CAPTURES)
    args = parser.parse_args()
    status = run(
        days=args.days, db=args.db, status_path=args.status_out,
        history_path=args.history, dry_run=args.dry_run,
        repair_pass=args.repair, captures=args.captures, max_repairs=args.max_repairs,
    )
    print(json.dumps(
        {key: status[key] for key in ("checked_at", "window", "counts", "agree_share")}
        | {"disagreement_count": len(status["disagreements"])}
        | ({"repair": status["repair"]} if "repair" in status else {}),
        ensure_ascii=False, sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
