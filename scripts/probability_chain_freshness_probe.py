#!/usr/bin/env python3
"""Read-only freshness probe for the production probability display chain.

The probe deliberately does not import a worker, materializer, market client,
or the UI server.  It reads one SQLite snapshot and, when requested, performs
one short HTTP GET against the already-running UI.  Its job is to identify the
first broken edge, not to repair or refresh anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


SCHEMA_VERSION = "probability_chain_freshness_probe_v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "archive.db"
DEFAULT_ACTIVATION = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "production_probability_runtime_activation.json"
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _seconds(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds(), 3)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _first_time(row: Mapping[str, Any], *fields: str) -> tuple[str | None, datetime | None]:
    for field in fields:
        value = _text(row.get(field))
        parsed = _parse_time(value)
        if parsed is not None:
            return value, parsed
    return None, None


def _segment(
    *,
    as_of: str | None,
    created_at: str | None,
    identifier: str | int | None,
    now: datetime,
    previous_time: datetime | None = None,
    hash_match: bool | None = None,
    blocker: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current_time = _parse_time(as_of) or _parse_time(created_at)
    raw_lag = _seconds(previous_time, current_time)
    lag = max(0.0, raw_lag) if raw_lag is not None else None
    result: dict[str, Any] = {
        "as_of": as_of,
        "created_at": created_at,
        "id": identifier,
        "hash_match": hash_match,
        "behind_previous": bool(raw_lag is not None and raw_lag > 0),
        "lag_seconds": lag,
        "age_seconds": (
            max(0.0, _seconds(now, current_time))
            if _seconds(now, current_time) is not None
            else None
        ),
        "blocker": blocker,
    }
    if extra:
        result.update(dict(extra))
    return result


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _load_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(_text(value))
    except (TypeError, ValueError):
        return None


def _fact_ssot(fact_payload: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("posterior_ssot_fact", "posterior_ssot", "ssot"):
        value = fact_payload.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _city_from_slug(slug: str) -> str:
    match = re.match(
        r"highest-temperature-in-(.+)-on-(?:\d{4}-\d{2}-\d{2}|[a-z]+-\d{1,2}-\d{4})$",
        slug,
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _canonical_ui_items(payload: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            found.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return found


def _same_id(value: Any, expected: Any) -> bool:
    return _text(value) != "" and _text(value) == _text(expected)


def _find_ui_identity(
    payload: Any,
    *,
    fact_id: Any,
    fact_snapshot_id: str,
    checkpoint_id: str,
    slug: str,
    event_date: str,
) -> dict[str, Any]:
    def first_value(item: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return value
        return None

    candidates = []
    for item in _canonical_ui_items(payload):
        item_slug = _text(item.get("slug") or item.get("market_slug"))
        item_date = _text(item.get("event_date") or item.get("date"))
        item_fact_id = first_value(item, "fact_id", "primary_fact_id")
        item_snapshot_id = first_value(
            item, "fact_snapshot_id", "primary_fact_snapshot_id"
        )
        item_checkpoint_id = first_value(
            item, "posterior_checkpoint_id", "primary_posterior_checkpoint_id"
        )
        id_match = (
            _same_id(item_fact_id, fact_id)
            or _same_id(item_snapshot_id, fact_snapshot_id)
            or _same_id(item_checkpoint_id, checkpoint_id)
        )
        scope_match = (not slug or not item_slug or item_slug == slug) and (
            not event_date or not item_date or item_date == event_date
        )
        if id_match and scope_match:
            candidates.append(item)
    if not candidates and isinstance(payload, Mapping):
        candidates = [payload]
    candidate = candidates[0] if candidates else {}
    found_fact_id = first_value(candidate, "fact_id", "primary_fact_id")
    found_snapshot = _text(
        first_value(candidate, "fact_snapshot_id", "primary_fact_snapshot_id")
    )
    found_checkpoint = _text(
        first_value(
            candidate, "posterior_checkpoint_id", "primary_posterior_checkpoint_id"
        )
    )
    # Summary payloads are allowed to omit fact_id/checkpoint_id. The SSOT
    # pointer that must be identical at the UI boundary is fact_snapshot_id;
    # optional IDs, when present, must not contradict it.
    matched = bool(
        _same_id(found_snapshot, fact_snapshot_id)
        and (found_fact_id in (None, "") or _same_id(found_fact_id, fact_id))
        and (not found_checkpoint or _same_id(found_checkpoint, checkpoint_id))
    )
    return {
        "matched": matched,
        "fact_id": found_fact_id,
        "fact_snapshot_id": found_snapshot,
        "posterior_checkpoint_id": found_checkpoint,
        "candidate_count": len(candidates),
    }


def _source_revision_ids(row: Mapping[str, Any]) -> set[str]:
    """Collect the stable revision ID and its transport aliases from one row."""

    values = {_text(row.get("source_event_id"))}
    raw_ref = _text(row.get("raw_ref"))
    match = re.search(r"((?:nwprev|tafrev)_[A-Za-z0-9]+)", raw_ref)
    if match:
        values.add(match.group(1))
    payload = _load_json(row.get("observation_json"))

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if _text(key).lower() in {"source_revision_id", "forecast_revision_id"}:
                    if _text(child):
                        values.add(_text(child))
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return {value for value in values if value}


def _source_kind(value: Any) -> str:
    """Map storage source ids to the weak-evidence source universe."""

    source = _text(value).lower()
    if source in {"ecmwf", "icon", "gfs"}:
        return source
    if "taf" in source:
        return "taf"
    return source


def _snapshot_station_id(snapshot: Mapping[str, Any]) -> str:
    """Return the station bound to a weak snapshot, never a global default."""

    target_identity = _text(snapshot.get("target_identity"))
    target_parts = target_identity.split(":")
    # Production multicity identities are physical_weather_day_v1:ICAO:DATE.
    if len(target_parts) >= 3 and re.fullmatch(r"[A-Za-z0-9]{4}", target_parts[-2] or ""):
        return target_parts[-2].upper()
    declared = _load_json(snapshot.get("source_revisions_json"))
    if isinstance(declared, list):
        for row in declared:
            if not isinstance(row, Mapping):
                continue
            provenance = row.get("provenance")
            if isinstance(provenance, Mapping) and _text(provenance.get("station_id")):
                return _text(provenance.get("station_id")).upper()
    return ""


def _official_station_revision(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    provider: str,
    clock: datetime,
) -> dict[str, Any] | None:
    """Rebuild one station/provider revision using the producer's identity rule."""

    try:
        rows = conn.execute(
            "SELECT * FROM official_nwp_station_snapshots "
            "WHERE station_id=? AND event_date=? AND provider_id=? AND state='available' "
            "ORDER BY valid_time_utc,created_at,snapshot_id",
            (station_id, event_date, provider),
        ).fetchall()
    except sqlite3.Error:
        return None
    by_valid: dict[str, sqlite3.Row] = {}
    for raw in rows:
        row = raw
        created = _parse_time(row["created_at"])
        valid_time = _text(row["valid_time_utc"])
        if created is None or created > clock or not valid_time:
            continue
        previous = by_valid.get(valid_time)
        if previous is None or (_text(row["created_at"]), _text(row["snapshot_id"])) > (
            _text(previous["created_at"]),
            _text(previous["snapshot_id"]),
        ):
            by_valid[valid_time] = row
    if not by_valid:
        return None
    ordered = [by_valid[key] for key in sorted(by_valid)]
    source_digest = _sha([_text(row["source_snapshot_sha256"]) for row in ordered])
    revision_id = f"multicity-{station_id.lower()}-{provider}-{source_digest[:24]}"
    available_at = max(_text(row["created_at"]) for row in ordered)
    return {
        "source_id": provider,
        "source_event_id": revision_id,
        "source_revision_id": revision_id,
        "as_of": available_at,
        "created_at": available_at,
        "raw_sha256": source_digest,
        "source_path": "official_nwp_station_snapshots",
        "station_id": station_id,
        "source_snapshot_ids": [_text(row["snapshot_id"]) for row in ordered],
    }


def _official_station_taf_revision(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    clock: datetime,
) -> dict[str, Any] | None:
    try:
        rows = conn.execute(
            "SELECT * FROM amsc_taf_snapshots "
            "WHERE station_id=? AND event_date=? AND state='available' "
            "ORDER BY issue_time_utc DESC,created_at DESC",
            (station_id, event_date),
        ).fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        created = _parse_time(row["created_at"])
        if created is None or created > clock:
            continue
        revision_id = _text(row["source_revision_id"])
        if not revision_id:
            continue
        return {
            "source_id": "taf",
            "source_event_id": revision_id,
            "source_revision_id": revision_id,
            "as_of": _text(row["created_at"]),
            "created_at": _text(row["created_at"]),
            "raw_sha256": _text(row["raw_sha256"]),
            "source_path": "amsc_taf_snapshots",
            "station_id": station_id,
        }
    return None


def _source_revision_rows(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    snapshot: Mapping[str, Any],
    clock: datetime,
    source_age_sec: float = 60.0,
) -> tuple[list[dict[str, Any]], set[str], dict[str, Any]]:
    """Resolve the source side of the snapshot from its own weak-source set.

    A physical posterior snapshot consumes forecast revisions (ECMWF/ICON/GFS
    and TAF), not the latest hard METAR row.  The old probe selected the newest
    row from the entire ``source_observations`` table and the global curve
    directory, so a fresh METAR or another station's revision could falsely
    determine freshness.  This resolver follows the snapshot's declared source
    universe and independently finds the newest visible revision for the exact
    station/day lineage.
    """

    declared = _load_json(snapshot.get("source_revisions_json"))
    declared_rows = [dict(row) for row in declared if isinstance(row, Mapping)] if isinstance(declared, list) else []
    declared_by_kind = {
        _source_kind(row.get("source_id")): row
        for row in declared_rows
        if _source_kind(row.get("source_id"))
    }
    declared_ids = {
        _text(value)
        for value in (_load_json(snapshot.get("source_revision_ids_json")) or [])
        if _text(value)
    }

    candidates: dict[str, list[dict[str, Any]]] = {}
    station_id = _snapshot_station_id(snapshot)
    declared_kinds = set(declared_by_kind)
    declared_revision_ids_by_kind = {
        kind: _text(row.get("source_revision_id"))
        for kind, row in declared_by_kind.items()
    }
    # The source revision is station/day scoped.  Reading the old global curve
    # directory here allowed ZUUU's revision to stand in for ZSPD (and vice
    # versa), which made both fresh and stale states untrustworthy.
    if station_id:
        for provider in ("ecmwf", "icon"):
            if provider in declared_kinds and declared_revision_ids_by_kind.get(provider, "").startswith("multicity-"):
                revision = _official_station_revision(
                    conn,
                    station_id=station_id,
                    event_date=event_date,
                    provider=provider,
                    clock=clock,
                )
                if revision is not None:
                    candidates.setdefault(provider, []).append(revision)
        if "taf" in declared_kinds and declared_revision_ids_by_kind.get("taf", "").startswith("tafrev_"):
            revision = _official_station_taf_revision(
                conn, station_id=station_id, event_date=event_date, clock=clock
            )
            if revision is not None:
                candidates.setdefault("taf", []).append(revision)

    # ZUUU's original v6 chain consumes the immutable daily-curve revision
    # files directly (nwprev_*), while the other configured stations consume
    # station-projected multicity revisions.  Keep this compatibility at the
    # resolver boundary only; never let these ZUUU files satisfy another ICAO.
    if station_id == "ZUUU":
        curve_root = PROJECT_ROOT / "data" / "runtime" / "official_nwp_curve_sources_v2"
        for provider in ("ecmwf", "icon"):
            if provider not in declared_kinds:
                continue
            if declared_revision_ids_by_kind.get(provider, "").startswith("multicity-"):
                continue
            for path in sorted((curve_root / provider / "daily_peak_revisions").glob("*.json")):
                try:
                    row = _load_json(path.read_text(encoding="utf-8")) if path.is_file() else None
                except (OSError, UnicodeDecodeError, ValueError):
                    row = None
                if not isinstance(row, Mapping):
                    continue
                if _text(row.get("source_id")) != provider or _text(row.get("event_date"))[:10] != event_date:
                    continue
                available = _parse_time(row.get("available_at"))
                if available is None or available > clock:
                    continue
                candidates.setdefault(provider, []).append({
                    "source_id": provider,
                    "source_event_id": _text(row.get("source_revision_id")),
                    "source_revision_id": _text(row.get("source_revision_id")),
                    "as_of": _text(row.get("available_at")),
                    "created_at": _text(row.get("available_at")),
                    "raw_sha256": _text(row.get("raw_sha256")),
                    "source_path": str(path),
                    "station_id": station_id,
                })

    # TAF and any test/independent weak source remain in source_observations.
    for raw in conn.execute(
        "SELECT * FROM source_observations WHERE event_date=?", (event_date,)
    ).fetchall():
        row = _row(raw)
        if row is None:
            continue
        kind = _source_kind(row.get("source_id"))
        if station_id and _text(row.get("station_id")) and _text(row.get("station_id")).upper() != station_id:
            continue
        if kind not in declared_by_kind and declared_rows:
            continue
        as_of, parsed = _first_time(row, "response_completed_at", "fetched_at", "parsed_at", "created_at")
        if parsed is None or parsed > clock:
            continue
        candidates.setdefault(kind, []).append({
            **row,
            "source_id": kind,
            "source_revision_id": _text(row.get("source_event_id")),
            "as_of": as_of,
            "source_path": "source_observations",
        })

    # The in-memory/unit-test contract has only source_revision_ids_json.  If
    # there is no expanded source_revisions_json, use IDs resolved from source
    # observations rather than broadening back to hard METAR rows.
    if not declared_rows:
        allowed_ids = declared_ids
        for kind, rows in list(candidates.items()):
            candidates[kind] = [
                row for row in rows
                if _text(row.get("source_revision_id")) in allowed_ids
                or _text(row.get("source_event_id")) in allowed_ids
            ]

    selected: list[dict[str, Any]] = []
    missing_sources: list[str] = []
    source_kinds = set(declared_by_kind) or {
        kind for kind, rows in candidates.items() if rows and kind
    }
    for kind in sorted(source_kinds):
        rows = candidates.get(kind) or []
        if not rows:
            missing_sources.append(kind)
            continue
        selected.append(max(rows, key=lambda row: _parse_time(row.get("as_of")) or datetime.min.replace(tzinfo=timezone.utc)))

    known_ids = set()
    for rows in candidates.values():
        for row in rows:
            known_ids.update(_source_revision_ids(row))
            if _text(row.get("source_revision_id")):
                known_ids.add(_text(row.get("source_revision_id")))
    coverage = bool(declared_ids) and declared_ids.issubset(known_ids)
    declared_identity = {
        (
            _source_kind(row.get("source_id")),
            _text(row.get("source_revision_id")),
            _text(row.get("raw_sha256")),
        )
        for row in declared_rows
        if _source_kind(row.get("source_id")) and _text(row.get("source_revision_id"))
    }
    selected_identity = {
        (
            _source_kind(row.get("source_id")),
            _text(row.get("source_revision_id")),
            _text(row.get("raw_sha256")),
        )
        for row in selected
    }
    if declared_identity:
        coverage = declared_identity.issubset(selected_identity)
    age_by_source = {
        _text(row.get("source_id")): round(max(0.0, _seconds(clock, _parse_time(row.get("as_of"))) or 0.0), 3)
        for row in selected
        if _text(row.get("source_id")) and _parse_time(row.get("as_of")) is not None
    }
    stale_source_ids = sorted(
        source_id for source_id, age in age_by_source.items() if age > source_age_sec
    )
    metadata = {
        "station_id": station_id,
        "declared_source_ids": sorted(declared_by_kind),
        "selected_source_ids": sorted(_text(row.get("source_id")) for row in selected),
        "missing_source_ids": sorted(set(missing_sources)),
        "consumed_revision_ids": sorted(declared_ids),
        "consumed_revision_coverage": coverage,
        "source_paths": sorted({str(row.get("source_path")) for row in selected}),
        "source_age_seconds_by_source": age_by_source,
        "stale_source_ids": stale_source_ids,
    }
    return selected, declared_ids, metadata


def _default_ui_url(*, slug: str, event_date: str) -> str:
    city = _city_from_slug(slug)
    query = {"limit": "24", "run_date": event_date}
    if city:
        query["city"] = city
    # Follow the same local UI contract as weatherctl/ui_server.  3137 was a
    # retired development port and made a healthy 3002 server look stale.
    port = str(os.environ.get("PM_UI_PORT") or "3002").strip()
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        port = "3002"
    return f"http://127.0.0.1:{port}/api/ui_snapshot_summary?" + urllib.parse.urlencode(query)


def _fetch_ui_json(url: str, timeout: float) -> tuple[Any | None, dict[str, Any]]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            return payload, {"ok": True, "http_status": int(response.status), "bytes": len(raw)}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return None, {"ok": False, "error": f"ui_fetch_failed:{type(exc).__name__}:{exc}"}


def _manifest_probe(
    *,
    activation_path: Path,
    activation_payload: Mapping[str, Any] | None,
    runtime_manifest_sha256: str | None,
    root: Path,
) -> dict[str, Any]:
    try:
        if activation_payload is None:
            activation_payload = _load_json(activation_path.read_text(encoding="utf-8"))
        activation = dict(activation_payload) if isinstance(activation_payload, Mapping) else {}
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from production_probability_activation_contract import activation_contract_sha256
        from production_probability_v6_runtime_manifest import v6_runtime_manifest_sha256

        expected_contract_hash = activation_contract_sha256(activation)
        actual_contract_hash = _text(activation.get("activation_contract_sha256"))
        installed_runtime = runtime_manifest_sha256 or v6_runtime_manifest_sha256(root)
        authorized_runtime = _text(activation.get("runtime_manifest_sha256"))
        contract_self_match = bool(actual_contract_hash and actual_contract_hash == expected_contract_hash)
        runtime_match = bool(authorized_runtime and authorized_runtime == installed_runtime)
        blocker = None if contract_self_match and runtime_match else "manifest_drift"
        return {
            "activation_hash": actual_contract_hash,
            "activation_contract_hash_expected": expected_contract_hash,
            "runtime_manifest_sha256": installed_runtime,
            "authorized_runtime_manifest_sha256": authorized_runtime,
            "hash_match": bool(contract_self_match and runtime_match),
            "blocker": blocker,
            "contract_self_match": contract_self_match,
            "runtime_match": runtime_match,
        }
    except Exception as exc:  # the probe must report a blocked contract, not mutate or crash
        return {
            "activation_hash": "",
            "activation_contract_hash_expected": "",
            "runtime_manifest_sha256": _text(runtime_manifest_sha256),
            "authorized_runtime_manifest_sha256": "",
            "hash_match": False,
            "blocker": f"manifest_probe_failed:{type(exc).__name__}",
            "contract_self_match": False,
            "runtime_match": False,
        }


def probe_chain(
    *,
    db_path: Path | str = DEFAULT_DB,
    event_date: str | None = None,
    slug: str | None = None,
    ui_url: str | None = None,
    source_age_sec: float = 60.0,
    ui_timeout_sec: float = 2.0,
    include_ui: bool = True,
    now: datetime | None = None,
    connection: sqlite3.Connection | None = None,
    ui_fetcher: Callable[[str, float], tuple[Any | None, dict[str, Any]]] | None = None,
    ui_identity_override: Mapping[str, Any] | None = None,
    activation_path: Path | str = DEFAULT_ACTIVATION,
    activation_payload: Mapping[str, Any] | None = None,
    runtime_manifest_sha256: str | None = None,
    runtime_root: Path | str | None = None,
) -> dict[str, Any]:
    """Return a JSON-safe, read-only chain diagnosis."""

    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": _iso(clock),
        "read_only": True,
        "query": {"event_date": event_date or "", "slug": slug or ""},
        "state": "blocked",
        "latest_checkpoint": None,
        "latest_fact": None,
        "lag_seconds": None,
        "broken_at": None,
        "blockers": [],
    }
    blockers: list[dict[str, str]] = []
    conn = connection
    owns_connection = False
    try:
        if conn is None:
            path = Path(db_path).expanduser().resolve()
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
            conn.row_factory = sqlite3.Row
            owns_connection = True
        else:
            conn.row_factory = sqlite3.Row
        required_tables = (
            "source_observations",
            "weak_forecast_evidence_snapshots",
            "posterior_checkpoints",
            "event_fact_latest",
            "event_fact_history",
        )
        missing_tables = [table for table in required_tables if not _table_exists(conn, table)]
        if missing_tables:
            blockers.append({"at": "database", "reason": "missing_tables:" + ",".join(missing_tables)})
            result["broken_at"] = "database"
            result["blockers"] = blockers
            return result

        if slug:
            latest_row = conn.execute(
                "SELECT * FROM event_fact_latest WHERE slug=? ORDER BY updated_at DESC LIMIT 1", (slug,)
            ).fetchone()
        elif event_date:
            latest_row = conn.execute(
                "SELECT * FROM event_fact_latest WHERE event_date=? ORDER BY updated_at DESC LIMIT 1",
                (event_date,),
            ).fetchone()
        else:
            latest_row = conn.execute(
                "SELECT * FROM event_fact_latest ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        latest = _row(latest_row)
        if latest is None:
            blockers.append({"at": "fact_latest", "reason": "fact_latest_missing"})
            result["broken_at"] = "fact_latest"
            result["blockers"] = blockers
            return result
        slug = _text(latest.get("slug"))
        event_date = _text(latest.get("event_date"))
        result["query"].update({"event_date": event_date, "slug": slug})

        fact_id = latest.get("fact_id")
        fact_row = conn.execute("SELECT * FROM event_fact_history WHERE id=?", (fact_id,)).fetchone()
        fact = _row(fact_row)
        fact_payload = _load_json(fact.get("fact_json")) if fact else None
        fact_payload = fact_payload if isinstance(fact_payload, Mapping) else {}
        ssot = _fact_ssot(fact_payload)
        fact_checkpoint_id = _text(ssot.get("posterior_checkpoint_id"))
        checkpoint_id = fact_checkpoint_id
        fact_snapshot_id = _text(latest.get("fact_snapshot_id") or (fact or {}).get("fact_snapshot_id"))
        fact_as_of = _text((fact or {}).get("fact_as_of") or latest.get("updated_at"))
        fact_created_at = _text((fact or {}).get("created_at") or latest.get("updated_at"))
        fact_checkpoint_as_of = _parse_time(fact_as_of) or _parse_time(fact_created_at)
        result["latest_fact"] = fact_snapshot_id or fact_id
        if not fact or not checkpoint_id:
            result["fact_latest"] = _segment(
                as_of=fact_as_of,
                created_at=fact_created_at,
                identifier=fact_snapshot_id or fact_id,
                now=clock,
                hash_match=False,
                blocker="fact_checkpoint_lineage_missing",
                extra={"fact_id": fact_id, "posterior_checkpoint_id": checkpoint_id},
            )
            blockers.append({"at": "fact_latest", "reason": "fact_checkpoint_lineage_missing"})
            result["broken_at"] = "fact_latest"
            result["blockers"] = blockers
            return result

        checkpoint_row = conn.execute(
            "SELECT * FROM posterior_checkpoints WHERE posterior_checkpoint_id=?", (checkpoint_id,)
        ).fetchone()
        if checkpoint_row is None:
            # Keep a stale pointer diagnosable as ssot_stale instead of
            # masking it as a missing checkpoint. Resolve the current row by
            # immutable target/run identity when available.
            runtime_identity = ssot.get("probability_runtime_identity")
            runtime_identity = runtime_identity if isinstance(runtime_identity, Mapping) else {}
            target_identity = _text(runtime_identity.get("target_identity"))
            if target_identity:
                checkpoint_row = conn.execute(
                    "SELECT * FROM posterior_checkpoints WHERE target_identity=? "
                    "ORDER BY replay_generation DESC, rowid DESC LIMIT 1",
                    (target_identity,),
                ).fetchone()
            if checkpoint_row is None:
                checkpoint_row = conn.execute(
                    "SELECT * FROM posterior_checkpoints WHERE posterior_run_id=? "
                    "ORDER BY replay_generation DESC, rowid DESC LIMIT 1",
                    (_text((fact or {}).get("posterior_run_id")),),
                ).fetchone()
            if checkpoint_row is not None:
                checkpoint_id = _text(checkpoint_row["posterior_checkpoint_id"])
        checkpoint = _row(checkpoint_row)
        if checkpoint is None:
            blockers.append({"at": "checkpoint", "reason": "checkpoint_missing"})
            result["broken_at"] = "checkpoint"
            result["blockers"] = blockers
            return result
        checkpoint_as_of, checkpoint_time = _first_time(checkpoint, "committed_at", "created_at")
        checkpoint_snapshot_id = _text(checkpoint.get("forecast_evidence_snapshot_id"))
        snapshot_row = conn.execute(
            "SELECT * FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id=?",
            (checkpoint_snapshot_id,),
        ).fetchone()
        snapshot = _row(snapshot_row)
        if snapshot is None:
            blockers.append({"at": "snapshot", "reason": "snapshot_missing"})
            result["broken_at"] = "snapshot"
            result["blockers"] = blockers
            return result
        snapshot_as_of, snapshot_time = _first_time(snapshot, "decision_as_of", "created_at")
        snapshot_payload = _load_json(snapshot.get("snapshot_json"))
        snapshot_hash_ok = bool(
            isinstance(snapshot_payload, (Mapping, list))
            and _text(snapshot.get("snapshot_sha256"))
            and _sha(snapshot_payload) == _text(snapshot.get("snapshot_sha256"))
        )
        source_revision_ids = _load_json(snapshot.get("source_revision_ids_json"))
        source_revision_ids = [
            _text(value) for value in source_revision_ids if _text(value)
        ] if isinstance(source_revision_ids, list) else []

        source_rows, declared_source_ids, source_meta = _source_revision_rows(
            conn,
            event_date=event_date,
            snapshot=snapshot,
            clock=clock,
            source_age_sec=source_age_sec,
        )
        source_times = [
            ((_text(row.get("as_of")), _parse_time(row.get("as_of"))), row)
            for row in source_rows
        ]
        source_times = [(time_pair, row) for time_pair, row in source_times if time_pair[1] is not None]
        source_time_pair, source_latest_row = max(
            source_times, key=lambda item: item[0][1], default=((None, None), {})
        )
        source_as_of, source_time = source_time_pair
        source_revision_coverage = bool(source_meta.get("consumed_revision_coverage"))
        source_hash_ok = bool(source_rows) and all(
            bool(re.fullmatch(r"[0-9a-fA-F]{64}", _text(row.get("raw_sha256"))))
            for row in source_rows
        )
        source_blocker = "source_stale" if (
            source_time is None
            or _seconds(clock, source_time) is None
            or _seconds(clock, source_time) > source_age_sec
            or source_meta.get("stale_source_ids")
        ) else None
        if source_time is None or source_meta.get("missing_source_ids"):
            source_blocker = "source_missing"
        snapshot_blocker = None
        if source_time is None or snapshot_time is None or snapshot_time < source_time:
            snapshot_blocker = "snapshot_stale"
        posterior_blocker = None
        if snapshot_time is None or checkpoint_time is None or checkpoint_time < snapshot_time:
            posterior_blocker = "posterior_stale"
        ssot_match = (
            _text(latest.get("fact_snapshot_id")) == fact_snapshot_id
            and fact_checkpoint_id == checkpoint_id
            and _text(ssot.get("posterior_run_id")) == _text(checkpoint.get("posterior_run_id"))
        )
        ssot_blocker = None if ssot_match else "ssot_stale"
        checkpoint_hash_ok = bool(
            _text(checkpoint.get("joint_posterior_sha256"))
            and isinstance(_load_json(checkpoint.get("joint_posterior_json")), (Mapping, list))
            and _sha(_load_json(checkpoint.get("joint_posterior_json")))
            == _text(checkpoint.get("joint_posterior_sha256"))
        )

        result["source"] = _segment(
            as_of=source_as_of,
            created_at=_text(source_latest_row.get("created_at")) or None,
            identifier=source_latest_row.get("source_event_id"),
            now=clock,
            hash_match=bool(source_hash_ok and source_revision_coverage),
            blocker=source_blocker,
            extra={
                "source_id": _text(source_latest_row.get("source_id")),
                "source_age_threshold_seconds": source_age_sec,
                "consumed_revision_count": len(declared_source_ids),
                "consumed_revision_coverage": source_revision_coverage,
                "raw_sha256": _text(source_latest_row.get("raw_sha256")),
                **source_meta,
            },
        )
        result["snapshot"] = _segment(
            as_of=snapshot_as_of,
            created_at=_text(snapshot.get("created_at")) or None,
            identifier=checkpoint_snapshot_id,
            now=clock,
            previous_time=source_time,
            hash_match=bool(snapshot_hash_ok and checkpoint_snapshot_id == _text(snapshot.get("forecast_evidence_snapshot_id"))),
            blocker=snapshot_blocker,
            extra={"snapshot_sha256": _text(snapshot.get("snapshot_sha256")), "target_identity": _text(snapshot.get("target_identity"))},
        )
        result["checkpoint"] = _segment(
            as_of=checkpoint_as_of,
            created_at=_text(checkpoint.get("created_at")) or None,
            identifier=checkpoint_id,
            now=clock,
            previous_time=snapshot_time,
            hash_match=bool(checkpoint_hash_ok and checkpoint_snapshot_id == _text(snapshot.get("forecast_evidence_snapshot_id"))),
            blocker=posterior_blocker,
            extra={
                "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
                "replay_generation": checkpoint.get("replay_generation"),
                "forecast_evidence_snapshot_id": checkpoint_snapshot_id,
                "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
            },
        )
        result["fact_latest"] = _segment(
            as_of=fact_as_of,
            created_at=fact_created_at,
            identifier=fact_snapshot_id or fact_id,
            now=clock,
            previous_time=checkpoint_time,
            hash_match=ssot_match,
            blocker=ssot_blocker,
            extra={"fact_id": fact_id, "posterior_checkpoint_id": fact_checkpoint_id, "resolved_checkpoint_id": checkpoint_id, "posterior_run_id": _text((fact or {}).get("posterior_run_id"))},
        )
        result["latest_checkpoint"] = checkpoint_id

        root = Path(runtime_root).expanduser().resolve() if runtime_root else Path(__file__).resolve().parents[1]
        result["manifest"] = _manifest_probe(
            activation_path=Path(activation_path).expanduser().resolve(),
            activation_payload=activation_payload,
            runtime_manifest_sha256=runtime_manifest_sha256,
            root=root,
        )
        if result["manifest"].get("blocker"):
            blockers.append({"at": "manifest", "reason": _text(result["manifest"]["blocker"])})

        target_ui_url = ui_url or _default_ui_url(slug=slug, event_date=event_date)
        fetcher = ui_fetcher or _fetch_ui_json
        if include_ui:
            payload, fetch_meta = fetcher(target_ui_url, ui_timeout_sec)
        elif ui_identity_override is not None:
            payload, fetch_meta = dict(ui_identity_override), {
                "ok": True,
                "skipped": True,
                "source": "ui_payload_override",
            }
        else:
            payload, fetch_meta = None, {
                "ok": False,
                "skipped": True,
                "error": "ui_not_checked",
            }
        ui_identity = _find_ui_identity(
            payload,
            fact_id=fact_id,
            fact_snapshot_id=fact_snapshot_id,
            checkpoint_id=checkpoint_id,
            slug=slug,
            event_date=event_date,
        ) if payload is not None else {}
        ui_as_of = None
        ui_created_at = None
        if isinstance(payload, Mapping):
            ui_as_of = _text(payload.get("as_of") or payload.get("updated_at") or payload.get("generated_at")) or None
            ui_created_at = _text(payload.get("created_at")) or None
        ui_blocker = None if fetch_meta.get("ok") and ui_identity.get("matched") else (
            "ui_unchecked" if fetch_meta.get("error") == "ui_not_checked"
            else ("ui_skipped" if fetch_meta.get("skipped") else "ui_stale")
        )
        # ``--no-ui`` is an intentional read-only scope choice, not a failed
        # UI request.  Keep that distinction so a source/checkpoint probe can
        # report its own state without manufacturing a UI break.
        if not fetch_meta.get("ok") and not fetch_meta.get("skipped"):
            ui_blocker = "ui_stale"
        result["ui"] = _segment(
            as_of=ui_as_of,
            created_at=ui_created_at,
            identifier=ui_identity.get("fact_snapshot_id") or ui_identity.get("fact_id"),
            now=clock,
            previous_time=fact_checkpoint_as_of,
            hash_match=bool(fetch_meta.get("ok") and ui_identity.get("matched")),
            blocker=ui_blocker,
            extra={"url": target_ui_url, "fetch": fetch_meta, "identity": ui_identity},
        )
        if ui_blocker and ui_blocker not in {"ui_unchecked", "ui_skipped"}:
            blockers.append({"at": "ui", "reason": ui_blocker})
        for key in ("source", "snapshot", "checkpoint", "fact_latest"):
            segment_blocker = _text(result.get(key, {}).get("blocker"))
            if segment_blocker:
                blockers.append({"at": key, "reason": segment_blocker})
        pipeline_order = {"manifest": -1, "source": 0, "snapshot": 1, "checkpoint": 2, "fact_latest": 3, "ui": 4}
        blockers = sorted(blockers, key=lambda item: pipeline_order.get(item["at"], 99))
        result["blockers"] = blockers
        result["broken_at"] = blockers[0]["at"] if blockers else None
        result["state"] = "blocked" if any(item["at"] == "manifest" for item in blockers) else ("stale" if blockers else "fresh")
        latest_time = _parse_time(fact_as_of) or checkpoint_time or snapshot_time or source_time
        result["lag_seconds"] = max(0.0, round(_seconds(clock, latest_time) or 0.0, 3))
        return result
    except (sqlite3.Error, OSError, ValueError) as exc:
        result["broken_at"] = result.get("broken_at") or "database"
        blockers.append({"at": result["broken_at"], "reason": f"probe_failed:{type(exc).__name__}:{exc}"})
        result["blockers"] = blockers
        return result
    finally:
        if owns_connection and conn is not None:
            conn.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only probability chain freshness probe")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--event-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--ui-url", default="")
    parser.add_argument("--no-ui", action="store_true")
    parser.add_argument("--source-age-sec", type=float, default=60.0)
    parser.add_argument("--ui-timeout-sec", type=float, default=2.0)
    parser.add_argument("--activation", default=str(DEFAULT_ACTIVATION))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = probe_chain(
        db_path=args.db,
        event_date=args.event_date or None,
        slug=args.slug or None,
        ui_url=None if args.no_ui else (args.ui_url or None),
        source_age_sec=args.source_age_sec,
        ui_timeout_sec=args.ui_timeout_sec,
        include_ui=not args.no_ui,
        activation_path=args.activation,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("state") == "fresh" else 1


if __name__ == "__main__":
    sys.exit(main())
