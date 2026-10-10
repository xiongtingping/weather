"""Supply watch (charter 197): can we reach the world, and is the served P0 fresh?

2026-09-21 16:04Z to 2026-09-22 07:49Z the local proxy tunnel stopped carrying
traffic.  Every upstream failed together -- the ECMWF GCS replica, the GEFS and
ECMWF S3 buckets, ECCC, DWD, aviationweather/tgftp, the Polymarket book socket
and CLOB -- with transport errors (``ConnectError``, ``PoolTimeout``, TLS EOF),
never an HTTP status.  The capture ladder held every family on the 06Z run it
already had, the packets it kept re-merging carried that run's clock, and at
21:37Z the last 2026-09-23 packet passed its six-hour window.  From then until
08:55Z the next morning no P0 was served for D+1, fleet-wide, and nothing on
the board said so: the rotation cursor kept advancing (green), the packets
still carried four families (green), the production cycle kept committing
(green).  Every light measured that a lane was moving; none measured whether
the thing it moves had arrived.

This module measures the two things that failed, directly:

* **egress** -- one bounded HEAD to each upstream through the same explicit
  proxy production uses.  Any HTTP answer is reachability; a transport error
  is not; 429/503 is a throttle, which is a different fault with a different
  cure.  All targets failing together is the tunnel, not a provider.
* **served P0 supply** -- for every listed future event date, how many fleet
  stations hold a P0/P0-M whose ``fresh_until_utc`` has not passed, read off
  ``basket_probability_bridge_latest`` (the rows decisions actually read) by
  primary key.  D+1 with zero fresh is the fleet-wide outage; it is red.
* **P0-M flow** (2026-09-23) -- whether the P0 roots committed lately were
  confirmed by P0-M where the root before them was.  A fresh P0 keeps the
  supply reading green while P0-M is dead (2026-09-20 03:27-05:26Z); three
  or more station-days waiting over 30 minutes is red when no P0-M has been
  committed anywhere for 30 minutes, amber when others still get theirs.

Reported, never acted on: the output is a small JSON file whose ``notify``
block is ready for whatever push channel the operator authorises.  It changes
no serving arithmetic, writes no database, places no order.  The capture
scheduler may read ``recapture_priority`` (charter 195's lane owns that).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "supply_watch_v1"
DEFAULT_OUTPUT = ROOT / "data" / "runtime" / "supply_watch.json"
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_REGISTRY = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "market_registry_forward.json"
)
DEFAULT_STATUS = ROOT / "data" / "weather_basket_yes_status.json"
DEFAULT_TAF_ARCHIVE_DIR = ROOT / "data" / "runtime" / "taf_raw_archive"

# (key, label, url).  Roots only: a HEAD on a bucket or site root is the
# cheapest request each provider answers, and any HTTP answer at all proves
# the path through the tunnel to that provider is open.
EGRESS_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("ecmwf_gcs", "ECMWF GCS 镜像", "https://storage.googleapis.com/ecmwf-open-data/"),
    ("ecmwf_s3", "ECMWF S3", "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/"),
    ("gefs_s3", "GEFS S3", "https://noaa-gefs-pds.s3.amazonaws.com/"),
    ("eccc", "ECCC", "https://dd.weather.gc.ca/"),
    ("dwd", "DWD", "https://opendata.dwd.de/"),
    ("awc", "AWC 报文", "https://aviationweather.gov/"),
    ("clob", "Polymarket CLOB", "https://clob.polymarket.com/"),
)
EGRESS_TIMEOUT_SEC = 12.0
THROTTLE_STATUS_CODES = frozenset({429, 503})
# One failed probe is a blip until it is not: amber at once, red once the
# outage has outlived one more probe interval (the sweep runs every 300s).
EGRESS_RED_AFTER_SEC = 240.0
# Below this many listed fleet stations a date is not a fleet; a two-station
# early listing going stale is not the outage this light is for.
MIN_FLEET_STATIONS = 10
THIN_FRESH_SHARE = 0.5
P0_STAGES = ("P0", "P0-M")

ProbeFn = Callable[[str, str, float], dict[str, Any]]


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------- egress


def classify_transport_error(exc: BaseException) -> str:
    """Name the failure the way the reader has to act on it."""

    text = f"{type(exc).__name__}: {exc}"
    if "Connection refused" in text or "Errno 61" in text:
        # Nothing listening on the proxy port: the proxy app itself is down.
        return "proxy_port_refused"
    return type(exc).__name__


def http_head_probe(url: str, proxy: str, timeout_sec: float) -> dict[str, Any]:
    """One HEAD through the explicit production proxy; never raises."""

    try:
        with httpx.Client(
            proxy=proxy or None,
            trust_env=False,
            follow_redirects=False,
            timeout=timeout_sec,
        ) as client:
            response = client.head(url, headers={"User-Agent": "weather-supply-watch/1"})
        status = int(response.status_code)
        return {
            "outcome": "throttled" if status in THROTTLE_STATUS_CODES else "ok",
            "http_status": status,
        }
    except Exception as exc:  # noqa: BLE001 -- the error class IS the answer
        return {"outcome": "transport_error", "error": classify_transport_error(exc)}


def probe_egress(
    *,
    proxy: str,
    probe: ProbeFn = http_head_probe,
    targets: Sequence[tuple[str, str, str]] = EGRESS_TARGETS,
    timeout_sec: float = EGRESS_TIMEOUT_SEC,
) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        futures = {
            key: pool.submit(probe, url, proxy, timeout_sec)
            for key, _label, url in targets
        }
        results = []
        for key, label, url in targets:
            outcome = dict(futures[key].result())
            results.append({"key": key, "label": label, "url": url, **outcome})
    failed = [r for r in results if r["outcome"] == "transport_error"]
    throttled = [r for r in results if r["outcome"] == "throttled"]
    if results and len(failed) == len(results):
        refused = all(r.get("error") == "proxy_port_refused" for r in failed)
        state = "proxy_down" if refused else "down"
    elif failed:
        state = "partial"
    elif throttled:
        state = "throttled"
    else:
        state = "ok"
    return {
        "state": state,
        "proxy": proxy or "direct",
        "targets": results,
        "failed": [r["key"] for r in failed],
        "throttled": [r["key"] for r in throttled],
    }


# ---------------------------------------------------------------- P0 supply


def fleet_station_ids() -> list[str]:
    from bayes_basket_target.station_registry import coordinate_registry_manifest

    return sorted(str(s) for s in coordinate_registry_manifest()["station_ids"])


def listed_scopes(
    registry_path: Path, *, fleet: Iterable[str]
) -> dict[str, set[str]]:
    """Fleet stations the forward market registry lists, by event date."""

    fleet_set = set(fleet)
    try:
        payload = json.loads(Path(registry_path).read_text())
    except (OSError, ValueError):
        return {}
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    listed: dict[str, set[str]] = {}
    for row in rows or []:
        if not isinstance(row, Mapping) or not row.get("market_active"):
            continue
        station = str(row.get("station_id") or "")
        event_date = str(row.get("event_date") or "")
        if station in fleet_set and event_date:
            listed.setdefault(event_date, set()).add(station)
    return listed


BRIDGE_ROWS_SQL = (
    "SELECT station_id, stage, decision_at_utc, fresh_until_utc, "
    "settlement_timezone "
    "FROM basket_probability_bridge_latest "
    "WHERE station_id IN ({placeholders}) AND event_date=?"
)


def bridge_rows_query(station_count: int) -> str:
    return BRIDGE_ROWS_SQL.format(
        placeholders=",".join("?" for _ in range(max(1, station_count)))
    )


def _local_day_started(day: date, zone_name: Any, now: datetime) -> bool:
    try:
        zone = ZoneInfo(str(zone_name or ""))
    except (ValueError, ZoneInfoNotFoundError):
        return False
    return datetime.combine(day, dt_time(0), tzinfo=zone) <= now


def p0_supply(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    listed: Mapping[str, set[str]],
) -> list[dict[str, Any]]:
    """Fresh served P0/P0-M per listed future event date (primary-key reads)."""

    today = now.astimezone(timezone.utc).date()
    out: list[dict[str, Any]] = []
    for event_date in sorted(listed):
        try:
            day = date.fromisoformat(event_date)
        except ValueError:
            continue
        lead_days = (day - today).days
        if lead_days < 1:
            # The event day belongs to P1; its P0 going stale is expected.
            continue
        stations = sorted(listed[event_date])
        rows = conn.execute(
            bridge_rows_query(len(stations)), [*stations, event_date]
        ).fetchall()
        # "D+1" is a UTC date, but a station's day starts at its own local
        # midnight.  By 22Z on the eve, 31 of 48 stations are already inside
        # their event day and serve P1 -- counted here, they read as "P0 not
        # fresh" and turned the date amber every night (2026-10-09 23Z:
        # "16/48 fresh", the 16 being exactly the stations west of UTC).
        # Those stations are out of this light, not stale in it; a station
        # with no bridge row has no zone to judge by and stays in.
        started = {
            str(r[0]) for r in rows if _local_day_started(day, r[4], now)
        }
        stations = [s for s in stations if s not in started]
        rows = [r for r in rows if str(r[0]) not in started]
        stage_rows = [r for r in rows if str(r[1]) in P0_STAGES]
        until = [(r, parse_utc(r[3])) for r in stage_rows]
        fresh = [r for r, moment in until if moment is not None and moment > now]
        newest_until = max(
            (moment for _r, moment in until if moment is not None), default=None
        )
        newest_decision = max(
            (m for m in (parse_utc(r[2]) for r in stage_rows) if m is not None),
            default=None,
        )
        listed_count = len(stations)
        fresh_count = len({str(r[0]) for r in fresh})
        health = "green"
        verdict = "ok"
        if listed_count >= MIN_FLEET_STATIONS and fresh_count == 0 and (
            lead_days == 1 or stage_rows
        ):
            # D+1 with nothing fresh is the outage whether or not it ever
            # had a P0; D+2 only once what it had has expired (a far date
            # legitimately waits for its first run).
            health = "red" if lead_days == 1 else "amber"
            verdict = "fleet_expired"
        elif (
            listed_count >= MIN_FLEET_STATIONS
            and fresh_count < THIN_FRESH_SHARE * listed_count
            and lead_days == 1
        ):
            health = "amber"
            verdict = "thin"
        out.append({
            "event_date": event_date,
            "lead_days": lead_days,
            "listed": listed_count,
            "in_event_day": len(started),
            "with_p0_row": len({str(r[0]) for r in stage_rows}),
            "fresh": fresh_count,
            "newest_fresh_until_utc": _utc_iso(newest_until) if newest_until else None,
            "newest_decision_at_utc": _utc_iso(newest_decision) if newest_decision else None,
            "health": health,
            "verdict": verdict,
        })
    return out


# ---------------------------------------------------------------- P0-M flow
#
# 2026-09-20 03:27Z-05:26Z no station got a P0-M.  Three weatherless TGFTP
# captures made the TAF ingest raise, the chain failed after P0, and P0 kept
# minting roots that nothing confirmed -- two hours, found by a person reading
# code, not by a light.  P0 supply stayed green throughout: a fresh P0 is a
# fresh P0/P0-M row.  This reading asks the other question: are the roots P0
# commits being confirmed?
#
# Commit clocks, not decision clocks: a P0-M rebuilt late is stamped back onto
# its root (``_p0m_replay_decision_at``), so on decision time the outage reads
# as P0-M arriving on time.

#: A root older than this with no P0-M after it is uncovered.  The chain
#: confirms a root within the same cycle (measured 0.1-0.8 min); a resident
#: restart between the two costs 8-16 min, so 30 leaves room for one.
P0M_STALL_MINUTES = 30.0
#: Roots this far back are read: the one being judged and the one before it.
P0M_ROOT_WINDOW_HOURS = 24.0
#: One or two station-days without a P0-M is a TAF lapse; three is the lane.
P0M_STALL_MIN_STATIONS = 3

P0M_FLOW_SQL = (
    "SELECT station_id, event_date, created_at_utc "
    "FROM basket_probability_bridge_history "
    "WHERE stage=? AND decision_at_utc>=? AND state='COMMITTED'"
)


def p0m_flow(conn: sqlite3.Connection, *, now: datetime) -> dict[str, Any]:
    """Whether P0-M kept confirming the roots it was confirming.

    A root is expected to be confirmed when the root before it on the same
    station-day was: P0-M covers a station-day only inside its TAF's reach
    (D+2 roots, a lapsed TAF, the event day once P1 owns it get none, by
    design), so "a root with no P0-M" alone is most of the fleet most of the
    time.  What an outage changes is the lane stopping where it was working.
    """

    floor = (now - timedelta(hours=P0M_ROOT_WINDOW_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )

    def rows(stage: str) -> dict[tuple[str, str], list[datetime]]:
        out: dict[tuple[str, str], list[datetime]] = {}
        for station, day, created in conn.execute(P0M_FLOW_SQL, (stage, floor)):
            moment = parse_utc(created)
            # ``<= now`` keeps a replay at a past instant honest; live, every
            # committed row is already in the past.
            if moment is not None and moment <= now:
                out.setdefault((str(station), str(day)), []).append(moment)
        return out

    confirmations = rows("P0-M")
    last_p0m = max(
        (m for moments in confirmations.values() for m in moments), default=None
    )
    stall = timedelta(minutes=P0M_STALL_MINUTES)
    uncovered: list[tuple[str, str, datetime]] = []
    for scope, moments in rows("P0").items():
        confirmed = confirmations.get(scope, [])
        ordered = sorted(moments)
        for before, root in zip(ordered, ordered[1:]):
            if now - root < stall:
                continue
            # The root before was confirmed while it was the live root.
            if not any(before <= m < root for m in confirmed):
                continue
            if not any(m >= root for m in confirmed):
                uncovered.append((*scope, root))
    stations = sorted({station for station, _d, _m in uncovered})
    oldest = min((m for _s, _d, m in uncovered), default=None)
    silent_min = (
        None if last_p0m is None
        else round((now - last_p0m).total_seconds() / 60.0, 1)
    )
    health, verdict = "green", "ok"
    if len(stations) >= P0M_STALL_MIN_STATIONS:
        if silent_min is None or silent_min >= P0M_STALL_MINUTES:
            health, verdict = "red", "fleet_stalled"
        else:
            health, verdict = "amber", "partial_stalled"
    return {
        "health": health,
        "verdict": verdict,
        "last_p0m_committed_at_utc": _utc_iso(last_p0m) if last_p0m else None,
        "p0m_silent_minutes": silent_min,
        "uncovered_station_count": len(stations),
        "uncovered_stations": stations[:12],
        "uncovered_root_count": len(uncovered),
        "oldest_uncovered_root_at_utc": _utc_iso(oldest) if oldest else None,
        "stall_minutes": P0M_STALL_MINUTES,
    }


# ---------------------------------------------------------------- evidence


def _held_class(reason: str) -> str:
    text = str(reason)
    if "transport_failed" in text or "ConnectError" in text or "Timeout" in text:
        return "网络"
    if "503" in text or "Slow Down" in text or "429" in text:
        return "限流"
    if "404" in text or "not_published" in text or "HTTPStatusError" in text:
        return "未发布"
    if "scope_invalid" in text:
        return "本轮不覆盖"
    return "其他"


def capture_evidence(status_path: Path) -> dict[str, Any]:
    """What the resident last recorded about capture and compile, if readable."""

    try:
        status = json.loads(Path(status_path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(status, Mapping):
        return {}
    held: dict[str, dict[str, str]] = {}
    by_date = status.get("official_native_capture_by_date")
    if isinstance(by_date, Mapping):
        for event_date, outcome in by_date.items():
            if not isinstance(outcome, Mapping):
                continue
            families = outcome.get("families_held_on_earlier_run")
            if isinstance(families, Mapping) and families:
                held[str(event_date)] = {
                    str(name): _held_class(str(reason))
                    for name, reason in families.items()
                }
    refusals: dict[str, int] = {}
    ingress = (status.get("production") or {}).get("native_ensemble_ingress")
    replay = ingress.get("active_activation_p0_replay") if isinstance(ingress, Mapping) else None
    if isinstance(replay, Mapping):
        for failure in replay.get("failures") or []:
            if isinstance(failure, Mapping):
                reason = str(failure.get("reason") or "").split("FAIL_CLOSED_NO_P0:")[-1]
                refusals[reason] = refusals.get(reason, 0) + 1
    return {
        "heartbeat_at_utc": status.get("heartbeat_at"),
        "families_held_by_date": held,
        "p0_compile_refusals": refusals,
    }


def latest_book_quote_utc(conn: sqlite3.Connection) -> str | None:
    # Covering index on quote_observed_at: one seek, not a scan.
    row = conn.execute(
        "SELECT MAX(quote_observed_at) FROM basket_official_ws_snapshot"
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def latest_taf_receipt_utc(archive_dir: Path, *, now: datetime) -> str | None:
    """Newest ``received_at_utc`` in today's (or yesterday's) TAF raw archive."""

    for offset in (0, 1):
        path = Path(archive_dir) / f"{(now.date() - timedelta(days=offset)).isoformat()}.jsonl"
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 65536))
                tail = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        newest = ""
        for line in tail:
            try:
                received = str(json.loads(line).get("received_at_utc") or "")
            except (ValueError, AttributeError):
                continue
            newest = max(newest, received)
        if newest:
            return newest
    return None


# ---------------------------------------------------------------- verdict


def _age_minutes(value: str | None, now: datetime) -> float | None:
    moment = parse_utc(value)
    return None if moment is None else round((now - moment).total_seconds() / 60.0, 1)


def evaluate(
    *,
    now: datetime,
    conn: sqlite3.Connection | None,
    listed: Mapping[str, set[str]],
    egress: Mapping[str, Any] | None,
    evidence: Mapping[str, Any] | None = None,
    passive: Mapping[str, Any] | None = None,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure verdict from the readings; ``previous`` carries the clocks."""

    previous = previous if isinstance(previous, Mapping) else {}
    prev_egress = previous.get("egress") if isinstance(previous.get("egress"), Mapping) else {}
    rows: list[dict[str, Any]] = []

    egress_block: dict[str, Any] = dict(egress or {"state": "not_probed"})
    state = str(egress_block.get("state") or "not_probed")
    if state in {"down", "proxy_down", "partial", "throttled"}:
        since = (
            prev_egress.get("since_utc")
            if prev_egress.get("state") == state and prev_egress.get("since_utc")
            else _utc_iso(now)
        )
        egress_block["since_utc"] = since
        outage_sec = (now - (parse_utc(since) or now)).total_seconds()
        if state in {"down", "proxy_down"}:
            health = "red" if outage_sec >= EGRESS_RED_AFTER_SEC else "amber"
            what = (
                "本地代理端口无人监听（代理程序没在跑）"
                if state == "proxy_down"
                else "全部上游同时不可达（代理/隧道出口断，不是某家限流）"
            )
            note = f"{what}，自 {since} 起 {outage_sec / 60:.0f} 分钟"
            if health == "red":
                # Charter 197 amendment: what red does, said where it is read.
                note += "；新单暂停发送，已挂单不动"
        elif state == "partial":
            health = "amber"
            labels = [t["label"] for t in egress_block.get("targets", []) if t["outcome"] == "transport_error"]
            note = f"部分上游不可达：{'、'.join(labels)}（自 {since}）"
        else:
            health = "amber"
            labels = [t["label"] for t in egress_block.get("targets", []) if t["outcome"] == "throttled"]
            note = f"上游限流 429/503：{'、'.join(labels)}（自 {since}）"
        rows.append({"code": f"egress_{state}", "health": health, "label": "外网出口", "note": note})
    else:
        if prev_egress.get("state") in {"down", "proxy_down"} and state == "ok":
            egress_block["recovered_at_utc"] = _utc_iso(now)
            egress_block["outage_since_utc"] = prev_egress.get("since_utc")
        elif prev_egress.get("recovered_at_utc") and state == "ok":
            egress_block["recovered_at_utc"] = prev_egress.get("recovered_at_utc")
            egress_block["outage_since_utc"] = prev_egress.get("outage_since_utc")

    supply = p0_supply(conn, now=now, listed=listed) if conn is not None else []
    held_by_date = (evidence or {}).get("families_held_by_date") or {}
    refusals = (evidence or {}).get("p0_compile_refusals") or {}
    for day in supply:
        if day["health"] == "green":
            continue
        held = held_by_date.get(day["event_date"]) or {}
        cause = (
            "；采集持有旧 run：" + "、".join(f"{k}（{v}）" for k, v in sorted(held.items()))
            if held else ""
        )
        if day["verdict"] == "fleet_expired":
            expired = day.get("newest_fresh_until_utc")
            note = (
                f"D+{day['lead_days']} 全队 P0 过期：{day['listed']} 站挂牌、0 站新鲜"
                + (f"，最后一份 {expired} 过期" if expired else "，从未出包")
                + cause
            )
        else:
            note = f"D+{day['lead_days']} 只有 {day['fresh']}/{day['listed']} 站 P0 新鲜{cause}"
        if refusals:
            note += "；编译拒绝：" + "、".join(f"{k}×{v}" for k, v in sorted(refusals.items()))
        rows.append({
            "code": f"p0_{day['verdict']}_{day['event_date']}",
            "health": day["health"],
            "label": f"P0 供给 {day['event_date']}",
            "note": note,
        })

    flow: dict[str, Any] = {"health": "green", "verdict": "not_read"}
    if conn is not None:
        try:
            flow = p0m_flow(conn, now=now)
        except sqlite3.Error as exc:
            flow = {"health": "amber", "verdict": "unreadable",
                    "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if flow["health"] != "green":
        if flow["verdict"] == "unreadable":
            note = f"P0-M 断流守望读不到概率桥：{flow.get('error')}"
        else:
            last = flow.get("last_p0m_committed_at_utc") or "从未"
            silent = flow.get("p0m_silent_minutes")
            head = (
                f"全队 P0-M 断流：最后一个 P0-M 提交于 {last}"
                + (f"（{silent:.0f} 分钟前）" if silent is not None else "")
                if flow["verdict"] == "fleet_stalled"
                else f"部分站 P0-M 落后（全队最后一个 P0-M 在 {last}，其余站仍在出）"
            )
            note = (
                f"{head}；{flow['uncovered_station_count']} 个站的新 P0 根超过 "
                f"{flow['stall_minutes']:.0f} 分钟没有被 P0-M 确认"
                f"（{'、'.join(flow['uncovered_stations'][:8])}"
                + ("等" if flow["uncovered_station_count"] > 8 else "")
                + "）；P1 只能接在 P0 上，有 TAF 的站日被决策闸挡住"
            )
        rows.append({
            "code": f"p0m_{flow['verdict']}",
            "health": flow["health"],
            "label": "P0-M 断流",
            "note": note,
        })

    overall = (
        "red" if any(r["health"] == "red" for r in rows)
        else "amber" if rows else "green"
    )
    # Recapture order once the egress is back: nearest date first, a date
    # with nothing fresh before a thin one.  Advisory for the capture lane.
    recapture = [
        {
            "event_date": d["event_date"], "lead_days": d["lead_days"],
            "fresh": d["fresh"], "listed": d["listed"],
            "families_held": held_by_date.get(d["event_date"]) or {},
        }
        for d in sorted(
            (d for d in supply if d["health"] != "green"),
            key=lambda d: (d["lead_days"], d["fresh"]),
        )
    ]
    prev_notify = previous.get("notify") if isinstance(previous.get("notify"), Mapping) else {}
    red_codes = sorted(r["code"] for r in rows if r["health"] == "red")
    incident_opened = None
    if overall == "red":
        incident_opened = (
            prev_notify.get("incident_opened_at_utc")
            if previous.get("overall") == "red" and prev_notify.get("incident_opened_at_utc")
            else _utc_iso(now)
        )
    key = (
        hashlib.sha256(json.dumps([incident_opened, red_codes]).encode()).hexdigest()[:16]
        if overall == "red" else ""
    )
    text = "；".join(f"{r['label']}：{r['note']}" for r in rows if r["health"] == "red")
    required = bool(key) and key != prev_notify.get("key")
    if not key and previous.get("overall") == "red":
        # The all-clear is worth one message too: the reader of the red one
        # is otherwise left to find out that it ended.
        key = "cleared:" + str(prev_notify.get("key") or "")
        required = True
        text = (
            f"已恢复（事件自 {prev_notify.get('incident_opened_at_utc') or '?'}）；"
            + ("仍需留意：" + "；".join(f"{r['label']}：{r['note']}" for r in rows) if rows else "出口通、未来事件日 P0 均有新鲜包")
        )
    notify = {
        "incident_opened_at_utc": incident_opened,
        "key": key,
        # A new key is a new thing to say; the same key is the same incident.
        "required": required,
        "text": text,
    }
    return {
        "schema": SCHEMA,
        "generated_at_utc": _utc_iso(now),
        "overall": overall,
        "alerts": rows,
        # The one reading the send gate acts on (charter 197 amendment).
        "egress_health": next(
            (r["health"] for r in rows if str(r["code"]).startswith("egress_")),
            "green",
        ),
        "egress": egress_block,
        "p0_supply": supply,
        "p0m_flow": flow,
        "evidence": dict(evidence or {}),
        "passive": {
            key: {"at_utc": value, "age_minutes": _age_minutes(value, now)}
            for key, value in (passive or {}).items()
        },
        "recapture_priority": recapture,
        "notify": notify,
        "calls_order_gateway": False,
        "writes_database": False,
    }


def read_previous(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1))
    tmp.replace(path)


def run(
    *,
    now: datetime | None = None,
    db_path: Path = DEFAULT_DB,
    registry_path: Path = DEFAULT_REGISTRY,
    status_path: Path = DEFAULT_STATUS,
    taf_archive_dir: Path = DEFAULT_TAF_ARCHIVE_DIR,
    output_path: Path | None = DEFAULT_OUTPUT,
    previous_path: Path | None = None,
    probe: ProbeFn | None = http_head_probe,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Read everything, decide, and (unless ``output_path`` is None) write.

    The clocks (outage since, incident opened, last notify key) are carried
    from ``previous_path`` -- by default the output file itself.
    """

    now = now or datetime.now(timezone.utc)
    carry_from = previous_path if previous_path is not None else output_path
    previous = read_previous(carry_from) if carry_from is not None else {}
    if probe is None:
        egress = None
    else:
        if proxy is None:
            from official_nwp_https_transport import official_nwp_proxy_url

            try:
                proxy = official_nwp_proxy_url()
            except ValueError:
                proxy = ""
        egress = probe_egress(proxy=proxy, probe=probe)
    listed = listed_scopes(registry_path, fleet=fleet_station_ids())
    passive: dict[str, Any] = {"taf_last_received": latest_taf_receipt_utc(taf_archive_dir, now=now)}
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        passive["book_last_quote"] = latest_book_quote_utc(conn)
        payload = evaluate(
            now=now, conn=conn, listed=listed, egress=egress,
            evidence=capture_evidence(status_path), passive=passive,
            previous=previous,
        )
    finally:
        if conn is not None:
            conn.close()
    if output_path is not None:
        write_atomic(output_path, payload)
    return payload


# ---------------------------------------------------------------- send gate
#
# Charter 197 amendment (user ruling 2026-09-22: 「网络闪断期间暂停发新单：出口
# 红灯时不发新单，已有的单不动」).  01:26-01:31Z that morning the resident posted
# into a tunnel that was up for five minutes out of fifteen hours: two
# UNKNOWN_POSTs, one still reconcile_required hours later.  A NEW order is
# held while the egress reading is red; nothing already at the venue is
# touched -- cancels, lookups, reconciliation and expiry never read this.
#
# Fail-safe is OPEN.  The watch is a sweep on the resident's clock; if it is
# not running (not yet registered, starved behind a long cycle, the file
# gone), holding would stop all trading on the watch's silence rather than
# on the network's.  So a missing, unreadable or stale file never holds --
# it is reported (``watch`` on the verdict, on the send's own event, and as
# 「守望失效」 on the board) and the send goes ahead.

WATCH_PATH_ENV = "PM_SUPPLY_WATCH_PATH"
#: A file older than this is a watch that stopped, not a reading: three
#: passes of the 300 s sweep.
WATCH_FRESH_SEC = 15.0 * 60.0
EGRESS_HOLD_REASON = "egress_red_hold"


def egress_held_sql(intent_alias: str) -> str:
    """SQL: this intent never left because the egress was red.

    Budgets that count sends (a lane's per-station-day cap, the recycle
    lane's failed-send cap, the tail lane's one take per bucket) must not
    be spent by an order the network held back -- the hold is not a
    verdict on the book, and a lane that burns its one take on it has lost
    the trade to the outage after all.  Seeks by the events' intent index.
    """

    return (
        "EXISTS (SELECT 1 FROM basket_target_order_events held"
        f" WHERE held.order_intent_id={intent_alias}.order_intent_id"
        " AND held.event_type='NEVER_REACHED_EXCHANGE'"
        f" AND json_extract(held.event_json,'$.reason')='{EGRESS_HOLD_REASON}')"
    )


def egress_health(payload: Mapping[str, Any]) -> str:
    """Red/amber/green for the egress reading alone (not the P0 supply)."""

    declared = payload.get("egress_health")
    if declared in {"red", "amber", "green"}:
        return str(declared)
    for row in payload.get("alerts") or []:
        if isinstance(row, Mapping) and str(row.get("code") or "").startswith("egress_"):
            return str(row.get("health") or "green")
    return "green"


def egress_send_gate(
    *, now: datetime | None = None, path: Path | str | None = None,
) -> dict[str, Any]:
    """May a NEW order leave the process now?  Read fresh on every call.

    ``hold`` is True only on a fresh file whose egress reading is red.
    ``watch`` is ``fresh``, ``missing``, ``unreadable``, ``stale`` or
    ``disabled`` (``PM_SUPPLY_WATCH_PATH=off``, tests); ``watch_invalid``
    marks the three that a person should hear about.
    """

    now = now or datetime.now(timezone.utc)
    configured = path if path is not None else os.environ.get(WATCH_PATH_ENV)
    if str(configured or "").strip().lower() == "off":
        return {"hold": False, "reason": "", "watch": "disabled", "watch_invalid": False}
    target = Path(configured) if configured else DEFAULT_OUTPUT
    verdict: dict[str, Any] = {"hold": False, "reason": "", "watch_path": str(target)}
    try:
        payload = json.loads(target.read_text())
    except FileNotFoundError:
        return {**verdict, "watch": "missing", "watch_invalid": True}
    except (OSError, ValueError):
        return {**verdict, "watch": "unreadable", "watch_invalid": True}
    generated = parse_utc(payload.get("generated_at_utc")) if isinstance(payload, Mapping) else None
    if generated is None:
        return {**verdict, "watch": "unreadable", "watch_invalid": True}
    age_sec = (now - generated).total_seconds()
    verdict["generated_at_utc"] = _utc_iso(generated)
    verdict["age_sec"] = round(age_sec, 1)
    if age_sec > WATCH_FRESH_SEC:
        return {**verdict, "watch": "stale", "watch_invalid": True}
    egress = payload.get("egress") if isinstance(payload.get("egress"), Mapping) else {}
    health = egress_health(payload)
    verdict.update({
        "watch": "fresh",
        "watch_invalid": False,
        "egress_health": health,
        "egress_state": egress.get("state"),
        "egress_since_utc": egress.get("since_utc"),
    })
    if health == "red":
        verdict.update({"hold": True, "reason": EGRESS_HOLD_REASON})
    return verdict


def send_audit(gate: Mapping[str, Any]) -> dict[str, Any]:
    """What a send's own event carries about the gate: the hold, or an
    invalid watch the send went ahead without.  Empty otherwise, so an
    ordinary send's event is byte-for-byte what it was before this gate."""

    if gate.get("hold") or gate.get("watch_invalid"):
        return {"egress_gate": {
            key: gate[key]
            for key in ("watch", "egress_state", "egress_since_utc", "generated_at_utc", "age_sec")
            if gate.get(key) is not None
        }}
    return {}


def summary_line(payload: Mapping[str, Any]) -> str:
    rows = payload.get("alerts") or []
    head = {"green": "正常", "amber": "留意", "red": "告警"}.get(str(payload.get("overall")), "?")
    egress = (payload.get("egress") or {}).get("state")
    body = "；".join(f"{r['label']}：{r['note']}" for r in rows) or "出口通、未来事件日 P0 均有新鲜包"
    return f"断供守望 [{head}] 出口={egress}；{body}"
