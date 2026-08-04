"""Bounded deterministic event replay for isolated target-lane evidence.

This is the generic B00-B18/property replay store.  The adapter that replays
the real production target SSOT lives separately in ``target_ssot_replay``.
Both paths are read/no-live evidence and never call an order gateway.
"""

from __future__ import annotations

import json
import math
import os
import random
import resource
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import ContractError, canonical_json, content_sha256
from .database import SHARED_LIVE_DB
from .resource_bounds import (
    MAX_TARGET_DB_BYTES,
    MAX_TARGET_PROCESS_RSS_BYTES,
    MAX_TARGET_WAL_BYTES,
)


SCHEMA = "bayes_basket_target_durable_replay_v1"
EVENT_SCHEMA = "bayes_basket_target_durable_replay_event_v1"
B_CODES = tuple(f"B{index:02d}" for index in range(19))


class ReplayError(ContractError):
    """Invalid replay input or isolated event-store state."""


class ResourceBoundError(ReplayError):
    """Replay resource contract was exceeded or could not be probed."""


@dataclass(frozen=True)
class ReplayLimits:
    max_events: int = 8192
    max_event_bytes: int = 256 * 1024
    max_total_event_bytes: int = 16 * 1024 * 1024
    max_payload_depth: int = 24
    max_payload_nodes: int = 250_000
    max_receipt_bytes: int = 16 * 1024 * 1024
    max_sqlite_bytes: int = MAX_TARGET_DB_BYTES
    max_wal_bytes: int = MAX_TARGET_WAL_BYTES
    max_rss_bytes: int = MAX_TARGET_PROCESS_RSS_BYTES
    min_free_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ResourceBoundError(f"replay_limit_invalid:{name}")


DEFAULT_LIMITS = ReplayLimits()


def _rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # Darwin reports bytes; Linux reports KiB.
    return value if os.uname().sysname == "Darwin" else value * 1024


def _resource_probe(limits: ReplayLimits) -> None:
    rss = _rss_bytes()
    if rss < 0 or rss > limits.max_rss_bytes:
        raise ResourceBoundError("replay_rss_bytes_exceeded")


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ReplayError(f"replay_{name}_missing")
    return result


def _utc(name: str, value: Any) -> datetime:
    raw = _text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayError(f"replay_{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReplayError(f"replay_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _payload_shape(value: Any, *, limits: ReplayLimits) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_payload_nodes:
            raise ResourceBoundError("replay_payload_nodes_exceeded")
        if depth > limits.max_payload_depth:
            raise ResourceBoundError("replay_payload_depth_exceeded")
        if isinstance(current, Mapping):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise ReplayError("replay_payload_nonfinite_number")


def normalize_event(
    raw: Mapping[str, Any],
    *,
    limits: ReplayLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ReplayError("replay_event_mapping_required")
    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        raise ReplayError("replay_event_payload_mapping_required")
    _payload_shape(payload, limits=limits)
    event_date = _text("event_date", raw.get("event_date"))
    try:
        date.fromisoformat(event_date)
    except ValueError as exc:
        raise ReplayError("replay_event_date_invalid") from exc
    sequence = raw.get("sequence", 0)
    revision = raw.get("revision", 0)
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise ReplayError("replay_event_sequence_or_revision_invalid")
    event_time = _utc("event_time_utc", raw.get("event_time_utc"))
    available = _utc("available_at_utc", raw.get("available_at_utc"))
    if available < event_time:
        raise ReplayError("replay_event_available_before_event")
    identity = {
        "event_id": _text("event_id", raw.get("event_id")),
        "scope_id": _text("scope_id", raw.get("scope_id")),
        "station_id": _text("station_id", raw.get("station_id")).upper(),
        "event_date": event_date,
        "settlement_timezone": _text(
            "settlement_timezone", raw.get("settlement_timezone")
        ),
        "event_type": _text("event_type", raw.get("event_type")).upper(),
        "event_time_utc": _utc_text(event_time),
        "available_at_utc": _utc_text(available),
        "sequence": sequence,
        "revision": revision,
        "payload": dict(payload),
    }
    encoded = canonical_json({"schema": EVENT_SCHEMA, **identity}).encode("utf-8")
    if len(encoded) > limits.max_event_bytes:
        raise ResourceBoundError("replay_event_bytes_exceeded")
    digest = content_sha256({"schema": EVENT_SCHEMA, **identity})
    supplied = raw.get("event_sha256")
    if supplied not in (None, "") and str(supplied) != digest:
        raise ReplayError("replay_event_content_sha_mismatch")
    return {**identity, "event_sha256": digest}


def _logical_key(event: Mapping[str, Any]) -> tuple[str, str, str, int]:
    payload = event["payload"]
    logical = (
        payload.get("logical_key")
        or payload.get("entity_key")
        or payload.get("label_key")
        or event["event_id"]
    )
    revision = int(payload.get("label_revision", event["revision"]))
    return (
        str(event["scope_id"]),
        str(event["event_type"]),
        str(logical),
        revision,
    )


def _settlement_window(day: str, timezone_name: str) -> dict[str, str]:
    try:
        zone = ZoneInfo(timezone_name)
        local_start = datetime.combine(
            date.fromisoformat(day), datetime.min.time(), tzinfo=zone
        )
    except (ValueError, KeyError) as exc:
        raise ReplayError("replay_settlement_timezone_invalid") from exc
    local_end = local_start + timedelta(days=1)
    return {
        "settlement_timezone": timezone_name,
        "target_day_start_utc": _utc_text(local_start),
        "target_day_end_utc": _utc_text(local_end),
    }


def _season(day: str) -> str:
    month = date.fromisoformat(day).month
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


def _window_summary(
    events: Sequence[Mapping[str, Any]],
    *,
    anchor_date: str | None,
) -> dict[str, Any]:
    requested = [3, 7, 15, 30]
    if anchor_date is None:
        return {
            "requested_days": requested,
            "mutually_exclusive": True,
            "by_window": {
                str(days): {"event_count": 0} for days in requested
            },
        }
    anchor = date.fromisoformat(anchor_date)
    bins = ((0, 3, 3), (3, 7, 7), (7, 15, 15), (15, 30, 30))
    result: dict[str, dict[str, Any]] = {}
    for lower, upper, name in bins:
        selected = [
            event for event in events
            if lower <= (anchor - date.fromisoformat(event["event_date"])).days < upper
        ]
        result[str(name)] = {
            "event_count": len(selected),
            "event_sha256": content_sha256(
                [event["event_sha256"] for event in selected]
            ),
        }
    return {
        "requested_days": requested,
        "mutually_exclusive": True,
        "by_window": result,
    }


def replay_memory(
    events: Sequence[Mapping[str, Any]],
    *,
    as_of_utc: str,
    registry_rows: Sequence[Mapping[str, Any]] | None = None,
    limits: ReplayLimits = DEFAULT_LIMITS,
    window_anchor_date: str | None = None,
) -> dict[str, Any]:
    _resource_probe(limits)
    if not isinstance(events, Sequence) or isinstance(
        events, (str, bytes, bytearray)
    ):
        raise ReplayError("replay_events_list_required")
    if len(events) > limits.max_events:
        raise ResourceBoundError("replay_events_rows_exceeded")
    as_of = _utc("as_of_utc", as_of_utc)
    normalized: list[dict[str, Any]] = []
    by_id: dict[str, str] = {}
    logical: dict[tuple[str, str, str, int], str] = {}
    duplicate_ids: list[str] = []
    total_bytes = 0
    for raw in events:
        event = normalize_event(raw, limits=limits)
        total_bytes += len(canonical_json(event).encode("utf-8"))
        if total_bytes > limits.max_total_event_bytes:
            raise ResourceBoundError("replay_events_total_bytes_exceeded")
        known = by_id.get(event["event_id"])
        if known is not None:
            if known != event["event_sha256"]:
                raise ReplayError("replay_event_id_content_conflict")
            duplicate_ids.append(event["event_id"])
            continue
        key = _logical_key(event)
        logical_known = logical.get(key)
        if logical_known is not None and logical_known != event["event_sha256"]:
            raise ReplayError("replay_logical_key_revision_conflict")
        by_id[event["event_id"]] = event["event_sha256"]
        logical[key] = event["event_sha256"]
        normalized.append(event)

    timezones = {event["settlement_timezone"] for event in normalized}
    if len(timezones) > 1:
        raise ReplayError("replay_settlement_timezone_conflict")
    registry = list(registry_rows or [])
    registry_map: dict[tuple[str, str], str] = {}
    for row in registry:
        if not isinstance(row, Mapping):
            raise ReplayError("replay_registry_row_invalid")
        key = (
            _text("registry_station_id", row.get("station_id")).upper(),
            _text("registry_event_date", row.get("event_date")),
        )
        timezone_name = _text(
            "registry_settlement_timezone", row.get("settlement_timezone")
        )
        if key in registry_map and registry_map[key] != timezone_name:
            raise ReplayError("replay_registry_timezone_conflict")
        registry_map[key] = timezone_name
    for event in normalized:
        expected = registry_map.get(
            (event["station_id"], event["event_date"])
        )
        if expected is not None and expected != event["settlement_timezone"]:
            raise ReplayError("replay_registry_timezone_mismatch")

    future = sorted(
        event["event_id"] for event in normalized
        if _utc("event_available", event["available_at_utc"]) > as_of
        or _utc("event_time", event["event_time_utc"]) > as_of
    )
    visible = [
        event for event in normalized if event["event_id"] not in set(future)
    ]
    visible.sort(key=lambda event: (
        event["event_time_utc"],
        event["available_at_utc"],
        int(event["sequence"]),
        int(event["revision"]),
        event["event_id"],
    ))

    labels: dict[str, dict[str, Any]] = {}
    checkpoint_history: list[dict[str, Any]] = []
    for event in visible:
        payload = event["payload"]
        if event["event_type"] == "LABEL":
            key = str(payload.get("label_key") or payload.get("logical_key") or event["event_id"])
            revision = int(payload.get("label_revision", event["revision"]))
            current = labels.get(key)
            if current is None or revision > int(current["revision"]):
                labels[key] = {
                    **dict(payload),
                    "revision": revision,
                    "event_id": event["event_id"],
                }
        if "checkpoint_id" in payload:
            checkpoint_history.append({
                "checkpoint_id": str(payload["checkpoint_id"]),
                "value": payload.get("value"),
                "event_id": event["event_id"],
                "event_time_utc": event["event_time_utc"],
            })

    settlement_windows: dict[str, dict[str, str]] = {}
    for event in visible:
        day = event["event_date"]
        window = _settlement_window(day, event["settlement_timezone"])
        old = settlement_windows.get(day)
        if old is not None and old != window:
            raise ReplayError("replay_settlement_window_conflict")
        settlement_windows[day] = window

    base_contracts = {
        code: {
            "status": (
                "PASS"
                if any(event["event_type"] == code for event in visible)
                else "MISSING"
            ),
            "strict_asof": True,
            "contract": {
                "invariant": f"{code}_deterministic_strict_asof_replay",
            },
        }
        for code in B_CODES
    }
    coverage = {
        "base_contracts": base_contracts,
        "all_base_contracts_present": all(
            row["status"] == "PASS" for row in base_contracts.values()
        ),
    }
    state = {
        "labels": labels,
        "checkpoint_history": checkpoint_history,
        "settlement_windows": settlement_windows,
        "last_event_id": visible[-1]["event_id"] if visible else None,
    }
    windows = _window_summary(visible, anchor_date=window_anchor_date)
    seasons = sorted({_season(event["event_date"]) for event in visible})
    semantic = {
        "schema": SCHEMA,
        "as_of_utc": _utc_text(as_of),
        "strict_asof": True,
        "visible_event_sha256s": [
            event["event_sha256"] for event in visible
        ],
        "state": state,
        "coverage": coverage,
        "windows": windows,
        "seasons": seasons,
        "boundary": {
            "production_no_live": True,
            "places_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
            "writes_production_db": False,
            "network_access": False,
        },
    }
    replay_sha = content_sha256(semantic)
    result = {
        **semantic,
        "status": (
            "PASS" if coverage["all_base_contracts_present"] else "PARTIAL"
        ),
        "input_audit": {
            "input_event_count": len(events),
            "unique_event_count": len(normalized),
            "visible_event_count": len(visible),
            "duplicate_event_ids": sorted(duplicate_ids),
            "future_event_ids": future,
            "total_event_bytes": total_bytes,
        },
        "replay_sha256": replay_sha,
    }
    if len(canonical_json(result).encode("utf-8")) > limits.max_receipt_bytes:
        raise ResourceBoundError("replay_receipt_bytes_exceeded")
    return result


_DDL = """
CREATE TABLE IF NOT EXISTS durable_replay_events(
  event_id TEXT PRIMARY KEY,
  event_json TEXT NOT NULL,
  event_sha256 TEXT NOT NULL CHECK(length(event_sha256)=64),
  created_at_utc TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS durable_replay_events_no_update
BEFORE UPDATE ON durable_replay_events BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS durable_replay_events_no_delete
BEFORE DELETE ON durable_replay_events BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


def _assert_store_capacity(path: Path, limits: ReplayLimits) -> None:
    if path.exists() and path.stat().st_size > limits.max_sqlite_bytes:
        raise ResourceBoundError("replay_sqlite_bytes_exceeded")
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size > limits.max_wal_bytes:
        raise ResourceBoundError("replay_wal_bytes_exceeded")
    if shutil.disk_usage(path.parent).free < limits.min_free_bytes:
        raise ResourceBoundError("replay_minimum_free_disk_not_met")


def open_event_store(
    db_path: Path | str,
    *,
    limits: ReplayLimits = DEFAULT_LIMITS,
) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if path == Path(SHARED_LIVE_DB).expanduser().resolve():
        raise ReplayError("replay_shared_live_database_forbidden")
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_store_capacity(path, limits)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_DDL)
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def append_events(
    conn: sqlite3.Connection,
    events: Sequence[Mapping[str, Any]],
    *,
    limits: ReplayLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    if len(events) > limits.max_events:
        raise ResourceBoundError("replay_events_rows_exceeded")
    duplicates: list[str] = []
    normalized = [normalize_event(event, limits=limits) for event in events]
    conn.execute("BEGIN IMMEDIATE")
    try:
        for event in normalized:
            old = conn.execute(
                "SELECT event_sha256 FROM durable_replay_events "
                "WHERE event_id=?",
                (event["event_id"],),
            ).fetchone()
            if old is not None:
                if str(old[0]) != event["event_sha256"]:
                    raise ReplayError("replay_event_id_content_conflict")
                duplicates.append(event["event_id"])
                continue
            conn.execute(
                "INSERT INTO durable_replay_events VALUES(?,?,?,?)",
                (
                    event["event_id"],
                    canonical_json(event),
                    event["event_sha256"],
                    datetime.now(timezone.utc).isoformat().replace(
                        "+00:00", "Z"
                    ),
                ),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    count = int(conn.execute(
        "SELECT COUNT(*) FROM durable_replay_events"
    ).fetchone()[0])
    return {
        "stored_event_count": count,
        "duplicate_event_ids": sorted(duplicates),
    }


def replay_durable_events(
    events: Sequence[Mapping[str, Any]],
    *,
    db_path: Path | str,
    as_of_utc: str,
    registry_rows: Sequence[Mapping[str, Any]] | None = None,
    limits: ReplayLimits = DEFAULT_LIMITS,
    window_anchor_date: str | None = None,
) -> dict[str, Any]:
    path = Path(db_path).expanduser().resolve()
    conn = open_event_store(path, limits=limits)
    try:
        append_receipt = append_events(conn, events, limits=limits)
        stored = [
            json.loads(str(row[0]))
            for row in conn.execute(
                "SELECT event_json FROM durable_replay_events "
                "ORDER BY event_id"
            )
        ]
        result = replay_memory(
            stored,
            as_of_utc=as_of_utc,
            registry_rows=registry_rows,
            limits=limits,
            window_anchor_date=window_anchor_date,
        )
        _assert_store_capacity(path, limits)
    finally:
        conn.close()
    identity = {
        **result,
        "durable_store": {
            "database": str(path),
            "stored_event_count": append_receipt["stored_event_count"],
            "duplicate_event_ids": append_receipt["duplicate_event_ids"],
            "isolated": True,
        },
    }
    return {
        **identity,
        "receipt_sha256": content_sha256(identity),
    }


def compare_replay_results(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> dict[str, Any]:
    fields = (
        "replay_sha256", "state", "coverage", "windows", "seasons",
        "strict_asof",
    )
    drift = [field for field in fields if first.get(field) != second.get(field)]
    return {"ok": not drift, "drift_fields": drift}


def build_generated_events(
    *,
    seed: int,
    station_id: str = "RKPK",
    settlement_timezone: str = "Asia/Seoul",
    anchor_date: str = "2026-07-27",
    days: int = 55,
    include_b_contract_events: bool = True,
) -> list[dict[str, Any]]:
    if isinstance(days, bool) or not isinstance(days, int) or days < 30:
        raise ReplayError("replay_generated_days_invalid")
    rng = random.Random(seed)
    anchor = date.fromisoformat(anchor_date)
    events: list[dict[str, Any]] = []
    for offset in range(days):
        day = anchor - timedelta(days=offset)
        event_time = datetime.combine(
            day, datetime.min.time(), tzinfo=timezone.utc
        ) + timedelta(hours=12)
        event_id = f"generated-{seed}-{offset:04d}"
        events.append({
            "event_id": event_id,
            "scope_id": f"{station_id}:{day.isoformat()}",
            "station_id": station_id,
            "event_date": day.isoformat(),
            "settlement_timezone": settlement_timezone,
            "event_type": "GENERATED",
            "event_time_utc": _utc_text(event_time),
            "available_at_utc": _utc_text(event_time),
            "sequence": offset,
            "revision": 0,
            "payload": {
                "logical_key": event_id,
                "value": rng.randrange(0, 1_000_000),
            },
        })
    if include_b_contract_events:
        base = datetime.combine(
            anchor, datetime.min.time(), tzinfo=timezone.utc
        ) + timedelta(hours=1)
        for index, code in enumerate(B_CODES):
            moment = base + timedelta(seconds=index)
            events.append({
                "event_id": f"generated-{seed}-{code}",
                "scope_id": f"{station_id}:{anchor_date}",
                "station_id": station_id,
                "event_date": anchor_date,
                "settlement_timezone": settlement_timezone,
                "event_type": code,
                "event_time_utc": _utc_text(moment),
                "available_at_utc": _utc_text(moment),
                "sequence": index,
                "revision": 0,
                "payload": {
                    "b_code": code,
                    "checkpoint_id": f"generated-{code}",
                    "value": index,
                },
            })
    return events


def build_blocked_receipt(reason: str, *, as_of_utc: str) -> dict[str, Any]:
    identity = {
        "schema": SCHEMA,
        "status": "BLOCKED_FAIL_CLOSED",
        "as_of_utc": str(as_of_utc),
        "blockers": [str(reason)],
        "boundary": {
            "production_no_live": True,
            "places_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
            "writes_production_db": False,
            "network_access": False,
        },
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


__all__ = [
    "B_CODES",
    "DEFAULT_LIMITS",
    "ReplayError",
    "ReplayLimits",
    "ResourceBoundError",
    "append_events",
    "build_blocked_receipt",
    "build_generated_events",
    "compare_replay_results",
    "normalize_event",
    "open_event_store",
    "replay_durable_events",
    "replay_memory",
]
