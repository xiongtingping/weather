"""Read only the physical-X labels eligible to form the climate prior.

The physical highest-temperature prior must be independent of forecast,
same-day observations, settlement, markets and orders.  This reader therefore
admits only two explicit AMSC sources:

* forward closed station-day X in the isolated final-label registry; and
* complete historical AMSC station-day sequences in the H archive.

The historical archive is useful climatological evidence, but its retrieval
time is real and is retained.  It cannot masquerade as strict-as-of training
or an observation that was available on the historical day.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from amsc_historical_physical_x_samples import build_amsc_historical_physical_x_samples
from resolution_final_source_runtime import PHYSICAL_ROLE, PHYSICAL_SOURCE_ID


SCHEMA_VERSION = "physical_x_prior_record_reader_v1"
DEFAULT_FINAL_LABEL_DB = Path(__file__).resolve().parent / "data" / "shadow" / "resolution_final_labels.db"
DEFAULT_HISTORICAL_ARCHIVE = Path(__file__).resolve().parent / "data" / "shadow" / "amsc_historical_backfill"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _read_gzip_json(path: Path) -> Mapping[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    return _mapping(value)


def _forward_records(db_path: Path, *, station_id: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    excluded: dict[str, int] = {}
    if not db_path.is_file():
        return [], {"forward_final_label_db_unavailable": 1}
    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        rows = conn.execute(
            """
            SELECT source_observation_id, source_id, role, station_id, event_date,
                   raw_ref, raw_sha256, observation_json, created_at
            FROM resolution_final_source_observations
            WHERE source_id=? AND role=? AND station_id=?
            ORDER BY event_date, created_at, source_observation_id
            """,
            (PHYSICAL_SOURCE_ID, PHYSICAL_ROLE, station_id),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return [], {"forward_final_label_db_read_failed": 1}
    records: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_observation_id, source_id, role, station, event_date, raw_ref, raw_sha, observation_json, created_at = row
        try:
            observation = _mapping(json.loads(str(observation_json)))
        except (TypeError, ValueError, json.JSONDecodeError):
            excluded["forward_final_observation_json_invalid"] = excluded.get("forward_final_observation_json_invalid", 0) + 1
            continue
        payload = _mapping(observation.get("payload"))
        high = _finite(payload.get("final_physical_high_c"))
        available = _time(created_at) or _time(observation.get("fetched_at"))
        if high is None:
            excluded["forward_final_physical_x_missing"] = excluded.get("forward_final_physical_x_missing", 0) + 1
            continue
        if _text(payload.get("label_finality")) != "final":
            excluded["forward_final_label_not_final"] = excluded.get("forward_final_label_not_final", 0) + 1
            continue
        if available is None:
            excluded["forward_final_available_at_invalid"] = excluded.get("forward_final_available_at_invalid", 0) + 1
            continue
        if len(_text(raw_sha)) != 64:
            excluded["forward_final_raw_sha_invalid"] = excluded.get("forward_final_raw_sha_invalid", 0) + 1
            continue
        candidate = {
            "event_date": _text(event_date)[:10],
            "station_id": _text(station).upper(),
            "available_at": _iso(available),
            "final_physical_high_c": high,
            "raw_sha256": _text(raw_sha),
            "source_kind": "amsc_physical_final",
            "source_observation_id": _text(source_observation_id),
            "raw_ref": _text(raw_ref),
        }
        # There is one final authoritative source per station-day.  A duplicate
        # label is ambiguous rather than an opportunity to choose the latest.
        if candidate["event_date"] in records:
            excluded["forward_final_station_day_duplicate"] = excluded.get("forward_final_station_day_duplicate", 0) + 1
            records.pop(candidate["event_date"], None)
            continue
        records[candidate["event_date"]] = candidate
    return [records[key] for key in sorted(records)], excluded


def _historical_days(root: Path, *, station_id: str) -> set[str]:
    days: set[str] = set()
    for path in root.glob("objects/*/*.json.gz"):
        try:
            row = _read_gzip_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if _text(row.get("station_id")).upper() != station_id:
            continue
        local = _text(row.get("verified_valid_time_local"))
        if len(local) >= 10:
            days.add(local[:10])
    return days


def _historical_available_at_by_sequence(root: Path, *, sequence_sha256s: set[str]) -> datetime | None:
    candidates: list[datetime] = []
    for path in root.glob("manifests/**/*.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        raw_objects = manifest.get("raw_objects") if isinstance(manifest.get("raw_objects"), list) else []
        manifest_sha256s = {
            _text(item.get("raw_sha256"))
            for item in raw_objects
            if isinstance(item, Mapping) and _text(item.get("raw_sha256"))
        }
        if sequence_sha256s and sequence_sha256s.issubset(manifest_sha256s):
            value = _time(manifest.get("query_received_at"))
            if value is not None:
                candidates.append(value)
    return min(candidates) if candidates else None


def _historical_records(root: Path, *, station_id: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    excluded: dict[str, int] = {}
    if not root.is_dir():
        return [], {"historical_amsc_archive_unavailable": 1}
    records: list[dict[str, Any]] = []
    for event_date in sorted(_historical_days(root, station_id=station_id)):
        batch = build_amsc_historical_physical_x_samples(root, event_date=event_date)
        if batch.get("ok") is not True:
            reason = _text((batch.get("block_reasons") or ["historical_sequence_unavailable"])[0])
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        samples = batch.get("samples") if isinstance(batch.get("samples"), list) else []
        if not samples:
            excluded["historical_sample_batch_empty"] = excluded.get("historical_sample_batch_empty", 0) + 1
            continue
        last = _mapping(samples[-1])
        label = _mapping(last.get("label"))
        high = _finite(label.get("physical_x_c"))
        sha256s = {
            _text(value)
            for value in last.get("physical_x_raw_sha256_list") or []
            if _text(value)
        }
        available = _historical_available_at_by_sequence(root, sequence_sha256s=sha256s)
        if high is None or not sha256s or available is None:
            reason = "historical_physical_x_or_available_at_missing"
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        records.append(
            {
                "event_date": event_date,
                "station_id": station_id,
                "available_at": _iso(available),
                "final_physical_high_c": high,
                "raw_sha256": _sha(sorted(sha256s)),
                "source_kind": "amsc_historical_complete",
                "historical_sequence_sha256s": sorted(sha256s),
                "historical_data_class": "H",
                "strict_asof_training_eligible": False,
            }
        )
    return records, excluded


def load_physical_x_prior_records(
    *,
    final_label_db: Path | str = DEFAULT_FINAL_LABEL_DB,
    historical_archive_dir: Path | str = DEFAULT_HISTORICAL_ARCHIVE,
    station_id: str = "ZUUU",
) -> dict[str, Any]:
    """Return deduplicated AMSC-only records usable by the physical-X prior."""

    station = _text(station_id).upper()
    if not station:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["physical_x_prior_station_id_missing"],
            "read_only": True,
        }
    forward, forward_excluded = _forward_records(Path(final_label_db).expanduser(), station_id=station)
    historical, historical_excluded = _historical_records(Path(historical_archive_dir).expanduser(), station_id=station)
    by_day: dict[str, dict[str, Any]] = {row["event_date"]: row for row in historical}
    # Forward daily X has the authoritative final-source role and takes
    # precedence over auxiliary H only for the same completed station day.
    for row in forward:
        by_day[row["event_date"]] = row
    records = [by_day[key] for key in sorted(by_day)]
    core = {
        "schema": SCHEMA_VERSION,
        "station_id": station,
        "records": records,
        "forward_record_count": len(forward),
        "historical_record_count": len(historical),
        "deduplicated_record_count": len(records),
        "excluded_counts": dict(sorted({**forward_excluded, **historical_excluded}.items())),
        "input_locations": {
            "forward_final_label_db": str(Path(final_label_db).expanduser()),
            "historical_archive_dir": str(Path(historical_archive_dir).expanduser()),
        },
    }
    return {
        **core,
        "record_batch_sha256": _sha(core),
        "ok": True,
        "state": "available" if records else "empty",
        "read_only": True,
        "shadow_only": True,
        "boundary": {
            "physical_x_only": True,
            "source_allowlist": ["amsc_physical_final", "amsc_historical_complete"],
            "settlement_market_forecast_d0_observation_and_orders_excluded": True,
            "historical_is_not_strict_asof_training_evidence": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = [
    "DEFAULT_FINAL_LABEL_DB",
    "DEFAULT_HISTORICAL_ARCHIVE",
    "SCHEMA_VERSION",
    "load_physical_x_prior_records",
]
