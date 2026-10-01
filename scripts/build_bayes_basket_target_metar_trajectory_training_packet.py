#!/usr/bin/env python3
"""Build strict-as-of RKPK METAR trajectory folds from local SSOT sources."""

from __future__ import annotations

import argparse
import hashlib
from datetime import date, datetime, time, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import (
    MAX_REPORT_TRANSMISSION_LEAD_SECONDS,
    ContractError,
    content_sha256,
)
from bayes_basket_target.metar_path_state import (
    PATH_STATE_SCHEMA,
    compile_conditional_metar_path_state,
)
from bayes_basket_target.metar_trajectory import (
    METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
    METAR_TRAJECTORY_PHYSICAL_ABSOLUTE_TMAX_METHOD_VERSION,
    MINIMUM_EFFECTIVE_SAMPLE_N,
)
from bayes_basket_target.materialization import (
    LINEAGE_DOCUMENT_REF_PREFIX,
    rehydrate_lineage_documents,
)
from bayes_basket_target.resource_bounds import write_bounded_json
from bayes_basket_target.truth_exclusions import is_excluded
from bayes_basket_target.future_temperature_curve import (
    build_root_future_temperature_curve,
)
from bayes_basket_target.settlement_time import settlement_day_window
from bayes_basket_target.station_season import station_season_bin


SCHEMA = "bayes_basket_metar_trajectory_training_packet_v1"
MAX_OBSERVATION_ROWS = 50_000
MAX_TRUTH_ROWS = 10_000
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
BUCKET_START_HOUR = {
    "NIGHT": 0,
    "MORNING": 6,
    "AFTERNOON": 12,
    "EVENING": 18,
}


def _parsed_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ContractError(
            "metar_trajectory_fold_timestamp_timezone_required"
        )
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _poll_receipt_verified(row: Mapping[str, Any]) -> bool:
    """Require an immutable official AWC poll receipt for production folds.

    Historical observation rows predate this guard and the small pure-domain
    unit fixtures intentionally omit poll columns.  Callers opt in through
    ``require_source_poll_receipt``; when enabled, a row is usable only when
    its joined poll is an AWC HTTP-200 response, completed at a valid UTC
    time, and contains the exact raw report/issue time already bound to the
    observation.  No transport or report timestamp is inferred.
    """

    if str(row.get("source_lane") or "").strip().lower() != "awc":
        return False
    if str(row.get("poll_source_lane") or "").strip().lower() != "awc":
        return False
    try:
        if int(row.get("poll_response_status")) != 200:
            return False
    except (TypeError, ValueError):
        return False
    completed = str(row.get("poll_response_completed_at") or "").strip()
    try:
        completed_at = _parsed_utc(completed)
    except (TypeError, ValueError):
        return False
    raw_text = str(row.get("raw_text") or "")
    if not raw_text:
        return False
    poll_json = row.get("poll_json")
    try:
        payload = json.loads(str(poll_json or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    reports = payload.get("reports") if isinstance(payload, Mapping) else None
    if not isinstance(reports, list):
        return False
    try:
        valid_at = _parsed_utc(str(row.get("valid_at") or ""))
    except (TypeError, ValueError):
        return False
    for report in reports:
        if not isinstance(report, Mapping):
            continue
        if str(report.get("station_id") or "").strip().upper() != str(
            row.get("station_id") or ""
        ).strip().upper():
            continue
        if str(report.get("raw_text") or "") != raw_text:
            continue
        issue = str(report.get("source_report_time") or "").strip()
        if not issue:
            continue
        try:
            if _parsed_utc(issue) != valid_at:
                continue
        except (ContractError, TypeError, ValueError):
            continue
        return True
    return False


def _read_truth(
    db: Path,
    *,
    station_id: str,
) -> list[dict[str, Any]]:
    with sqlite3.connect(
        f"file:{db.resolve()}?mode=ro", uri=True, timeout=5.0
    ) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT truth_id,station_id,local_date,official_tmax_c,
                   source_lane,settlement_definition,revision_no,
                   canonical_committed_at_utc,timestamps_complete,
                   qc_status,authority_scope,content_sha256
              FROM calibration_station_daily_high_truth
             WHERE station_id=?
             ORDER BY local_date,revision_no,canonical_committed_at_utc
            """,
            (station_id,),
        ).fetchall()
    if len(rows) > MAX_TRUTH_ROWS:
        raise ContractError(
            "metar_trajectory_fold_truth_resource_limit_exceeded"
        )
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = dict(row)
        if (
            str(value.get("source_lane") or "").upper() != "AWC"
            or str(value.get("qc_status") or "").upper() != "ACCEPTED"
            or int(value.get("timestamps_complete") or 0) != 1
            or not str(value.get("canonical_committed_at_utc") or "")
            # Settled before its own local afternoon by the retired ingest and
            # unrepairable for want of a canonical fact.  A fold labelled with
            # a partial morning teaches the trajectory that the day stops
            # warming, which is the opposite of what it is for.
            or is_excluded(station_id, str(value.get("local_date") or ""))
        ):
            continue
        latest[str(value["local_date"])] = value
    return [latest[key] for key in sorted(latest)]


def _read_observations(
    db: Path,
    *,
    station_id: str,
) -> list[dict[str, Any]]:
    with sqlite3.connect(
        f"file:{db.resolve()}?mode=ro", uri=True, timeout=5.0
    ) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT o.observation_id,o.source_lane,o.station_id,o.report_type,
                   o.valid_at,o.reported_temperature_c,o.raw_sha256,o.raw_text,
                   o.canonical_committed_at,o.correction_suspected,
                   o.source_poll_id,p.source_lane AS poll_source_lane,
                   p.response_status AS poll_response_status,
                   p.response_completed_at AS poll_response_completed_at,
                   p.poll_json
              FROM reported_trajectory_observations o
              LEFT JOIN reported_trajectory_source_polls p
                ON p.poll_id=o.source_poll_id
             WHERE o.station_id=?
               AND lower(o.source_lane)='awc'
               AND o.report_type IN ('METAR','SPECI')
             ORDER BY o.valid_at,o.canonical_committed_at,o.observation_id
            """,
            (station_id,),
        ).fetchall()
    if len(rows) > MAX_OBSERVATION_ROWS:
        raise ContractError(
            "metar_trajectory_fold_observation_resource_limit_exceeded"
        )
    return [dict(row) for row in rows]


ROOT_COLUMNS = (
    "stage", "checkpoint_id", "station_id", "event_date", "settlement_timezone",
    "decision_at_utc", "bucket_ordinals_json", "probabilities_json",
    "posterior_sha256", "lineage_json", "content_sha256",
)
ROOTS_SQL = (
    "SELECT " + ",".join(ROOT_COLUMNS) + " FROM basket_probability_envelope"
    " WHERE station_id=? AND stage IN ('P0','P0-M') AND state='COMMITTED'"
)
ROOT_COUNTS_SQL = (
    "SELECT event_date, COUNT(*) FROM basket_probability_envelope"
    " WHERE station_id=? AND stage IN ('P0','P0-M') AND state='COMMITTED'"
    " GROUP BY event_date"
)

# --------------------------------------------------------------------------
# The root cache (2026-10-01 「做」).
#
# Production runs this packet for one due station a cycle, and every run read
# every P0/P0-M the station ever had -- lineage and all -- which kept every
# envelope in the SSOT.  A station-day ROOT_FREEZE_LAG_DAYS past is copied
# once, and later runs read it from the cache.  What is kept of a row is
# every column and, of its lineage, the keys this file reads
# (CACHED_LINEAGE_KEYS; a test scans for ``lineage.get`` so a new key cannot
# be read off a cached row unseen).  The packet built either way is the same
# bytes (verified 2026-10-01 on the live store).
# --------------------------------------------------------------------------

ROOT_CACHE_PATH = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "metar_trajectory_roots.db"
)
ROOT_CACHE_SCHEMA = "metar_trajectory_root_cache_v1"
ROOT_FREEZE_LAG_DAYS = 3
CACHED_LINEAGE_KEYS = (
    "bucket_centers_c_json",
    "bucket_intervals_c_json",
    "conditional_future_temperature_curve_json",
    "conditional_future_temperature_curve_root_checkpoint_id",
    "conditional_future_temperature_curve_root_probability_sha256",
    "conditional_future_temperature_curve_root_stage",
    "conditional_future_temperature_curve_sha256",
    "p0_root_checkpoint_id",
    "p0_root_posterior_sha256",
)
ROOT_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS frozen_day (
  station_id TEXT NOT NULL, event_date TEXT NOT NULL, row_count INTEGER NOT NULL,
  schema TEXT NOT NULL, frozen_at_utc TEXT NOT NULL,
  PRIMARY KEY(station_id, event_date)
);
CREATE TABLE IF NOT EXISTS root_row (
  station_id TEXT NOT NULL, event_date TEXT NOT NULL, seq INTEGER NOT NULL,
  stage, checkpoint_id, settlement_timezone, decision_at_utc,
  bucket_ordinals_sha, probabilities_sha, posterior_sha256, lineage_sha, content_sha256,
  PRIMARY KEY(station_id, event_date, seq)
);
CREATE TABLE IF NOT EXISTS blob (sha TEXT PRIMARY KEY, body);
"""


def _project_lineage(raw: Any) -> Any:
    """The keys this file reads, verbatim; anything unparseable kept whole."""

    try:
        lineage = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return raw
    if not isinstance(lineage, dict):
        return raw
    return json.dumps(
        {key: lineage[key] for key in CACHED_LINEAGE_KEYS if key in lineage},
        sort_keys=True, ensure_ascii=False,
    )


def _blob(cache: sqlite3.Connection, value: Any) -> str | None:
    if value is None:
        return None
    body = value if isinstance(value, (str, bytes)) else json.dumps(value)
    digest = hashlib.sha256(body.encode("utf-8") if isinstance(body, str) else body).hexdigest()
    cache.execute("INSERT OR IGNORE INTO blob VALUES(?,?)", (digest, body))
    return digest


def open_root_cache(path: Path | str = ROOT_CACHE_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = sqlite3.connect(str(path), timeout=60.0, isolation_level=None)
    cache.execute("PRAGMA journal_mode=WAL")
    cache.executescript(ROOT_CACHE_DDL)
    return cache


def _cached_rows(cache: sqlite3.Connection, station_id: str, event_date: str) -> list[dict[str, Any]]:
    return [
        dict(zip(ROOT_COLUMNS, (
            stage, checkpoint, station_id, event_date, tz, decided, ordinals, probabilities,
            posterior, lineage, content,
        )))
        for stage, checkpoint, tz, decided, ordinals, probabilities, posterior, lineage, content
        in cache.execute(
            "SELECT r.stage, r.checkpoint_id, r.settlement_timezone, r.decision_at_utc,"
            " o.body, p.body, r.posterior_sha256, l.body, r.content_sha256 FROM root_row r"
            " LEFT JOIN blob o ON o.sha = r.bucket_ordinals_sha"
            " LEFT JOIN blob p ON p.sha = r.probabilities_sha"
            " LEFT JOIN blob l ON l.sha = r.lineage_sha"
            " WHERE r.station_id=? AND r.event_date=? ORDER BY r.seq",
            (station_id, event_date),
        )
    ]


def _freeze_day(
    cache: sqlite3.Connection, station_id: str, event_date: str,
    rows: list[dict[str, Any]], *, now_utc: str,
) -> None:
    cache.execute("BEGIN IMMEDIATE")
    try:
        cache.execute("DELETE FROM root_row WHERE station_id=? AND event_date=?", (station_id, event_date))
        cache.execute("DELETE FROM frozen_day WHERE station_id=? AND event_date=?", (station_id, event_date))
        for seq, row in enumerate(rows):
            cache.execute(
                "INSERT INTO root_row VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    station_id, event_date, seq, row["stage"], row["checkpoint_id"],
                    row["settlement_timezone"], row["decision_at_utc"],
                    _blob(cache, row["bucket_ordinals_json"]),
                    _blob(cache, row["probabilities_json"]), row["posterior_sha256"],
                    _blob(cache, _project_lineage(row["lineage_json"])), row["content_sha256"],
                ),
            )
        cache.execute(
            "INSERT INTO frozen_day VALUES(?,?,?,?,?)",
            (station_id, event_date, len(rows), ROOT_CACHE_SCHEMA, now_utc),
        )
        cache.execute("COMMIT")
    except BaseException:
        if cache.in_transaction:
            cache.execute("ROLLBACK")
        raise


def _root_order(row: Mapping[str, Any]) -> tuple:
    return tuple(
        "" if row.get(key) is None else str(row.get(key))
        for key in ("event_date", "decision_at_utc", "checkpoint_id")
    )


def _read_probability_roots(
    db: Path,
    *,
    station_id: str,
    cache_path: Path | str | None = None,
    now_utc: str | None = None,
) -> list[dict[str, Any]]:
    """Every P0/P0-M of the station, in (event_date, decision, checkpoint) order.

    With ``cache_path``, frozen station-days come from the root cache and the
    rest from the SSOT; a station-day whose SSOT row count has changed since
    it was frozen is frozen again.  Without it, every row is read off the SSOT.
    """

    with sqlite3.connect(
        f"file:{db.resolve()}?mode=ro", uri=True, timeout=5.0
    ) as conn:
        conn.row_factory = sqlite3.Row
        if cache_path is None:
            rows = conn.execute(
                ROOTS_SQL + " ORDER BY event_date,decision_at_utc,checkpoint_id",
                (station_id,),
            ).fetchall()
            return _rehydrated(conn, [dict(row) for row in rows])
        counts = {str(day): int(n) for day, n in conn.execute(ROOT_COUNTS_SQL, (station_id,))}
        now = now_utc or _iso_utc(datetime.now(timezone.utc))
        frozen_through = (
            date.fromisoformat(now[:10]) - timedelta(days=ROOT_FREEZE_LAG_DAYS)
        ).isoformat()
        cache = open_root_cache(cache_path)
        try:
            frozen = {
                str(day): (int(n), str(schema))
                for day, n, schema in cache.execute(
                    "SELECT event_date, row_count, schema FROM frozen_day WHERE station_id=?",
                    (station_id,),
                )
            }
            # A frozen day is read from the cache unless the SSOT still holds
            # it and holds a different number of rows (or the cache is from
            # another schema); a day the SSOT no longer holds stays as frozen.
            usable = {
                day for day, (n, schema) in frozen.items()
                if schema == ROOT_CACHE_SCHEMA and counts.get(day, n) == n
            }
            from_ssot = sorted(day for day in counts if day not in usable)
            ssot_rows: dict[str, list[dict[str, Any]]] = {}
            for start in range(0, len(from_ssot), 400):
                chunk = from_ssot[start:start + 400]
                for row in conn.execute(
                    ROOTS_SQL + f" AND event_date IN ({','.join('?' * len(chunk))})"
                    " ORDER BY event_date,decision_at_utc,checkpoint_id",
                    (station_id, *chunk),
                ):
                    ssot_rows.setdefault(str(row["event_date"]), []).append(dict(row))
            for day in from_ssot:
                if day <= frozen_through:
                    _freeze_day(cache, station_id, day, ssot_rows.get(day, []), now_utc=now)
            out: list[dict[str, Any]] = []
            for day in sorted(usable | set(from_ssot)):
                out.extend(ssot_rows.get(day, []) if day in ssot_rows or day in from_ssot
                           else _cached_rows(cache, station_id, day))
        finally:
            cache.close()
        out.sort(key=_root_order)
        return _rehydrated(conn, out)


def _rehydrated(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each root's lineage with its ``@ref/`` documents put back.

    Since 2026-09-01 (926dfa29) the big lineage bodies -- the conditional
    future-temperature curve among them -- are stored once and referenced by
    hash; this packet read the reference itself and failed to parse it, so
    every bucket whose folds reached a curve failed and the night bucket
    carried no fold (found 2026-10-01).  Production's own
    ``rehydrate_lineage_documents`` puts them back; a missing body fails
    closed there.  The cache keeps the references: the bodies stay in the
    SSOT's document table.
    """

    prefix = LINEAGE_DOCUMENT_REF_PREFIX
    for row in rows:
        raw = row.get("lineage_json")
        if not isinstance(raw, str) or prefix not in raw:
            continue
        try:
            lineage = json.loads(raw)
        except ValueError:
            continue
        if isinstance(lineage, dict):
            row["lineage_json"] = json.dumps(
                rehydrate_lineage_documents(conn, lineage), sort_keys=True, ensure_ascii=False,
            )
    return rows


CURVE_HISTORY_DIR = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "native_curve_history"
)
POSTERIOR_HISTORY_DIR = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "native_posterior_history"
)
HISTORICAL_FOLD_EVIDENCE_CLASS = "HISTORICAL_ARCHIVE_CURVE_FOLD"
# The lead the reconstruction reads.  d1's 00Z run is ready (init + 8h
# publication margin) the morning *before* the event day, so it strictly
# precedes every local-time-bucket cutoff of the day itself.
HISTORICAL_CURVE_LEAD = "d1"
_HISTORICAL_CACHE: dict[str, Any] = {}


def _deduplicate_prefix(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Collapse reports sharing a valid time, mirroring settled truth.

    26 of 45 stations publish paired reports for one valid time -- usually two
    SPECIs, sometimes a METAR reissue -- and the path-state compiler requires
    strictly increasing observation times, so an undeduplicated prefix raised
    ``metar_path_observation_time_rewind`` and took the station's entire packet
    down.

    Settled truth already answers this: two reports for one valid time that
    agree on temperature state the same fact and either may be kept, while a
    genuine disagreement is not resolvable.  This mirrors that rule exactly
    rather than inventing a second one.  Returns ``(rows, ok)``; ``ok`` is
    False when a disagreement makes the day unusable, which skips that fold
    instead of the packet.
    """

    seen: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for row in rows:
        valid_at = str(row.get("valid_at") or "")
        previous = seen.get(valid_at)
        if previous is None:
            seen[valid_at] = row
            ordered.append(row)
            continue
        if float(previous["reported_temperature_c"]) != float(
            row["reported_temperature_c"]
        ):
            return [], False
    return ordered, True


def _historical_fold_binding(
    *,
    station: str,
    event_date: str,
    settlement_timezone: str,
    cutoff_iso: str,
    prefix_rows: list,
    running_high: float,
    truth_tmax_c: float,
) -> dict[str, Any] | None:
    """Bind a fold to archive-reconstructed evidence when no root committed.

    Committed curve-carrying P0 roots exist only from 2026-08-02, which left
    every earlier settled day unusable as a fold and the promotion gate ~17
    days away at one fold per day.  The information for those days exists: the
    d1 run genuinely published the morning before each day, and the expanding
    -window posterior is fitted only on days strictly before it.  This binding
    is labelled ``HISTORICAL_ARCHIVE_CURVE_FOLD`` and the posterior source is
    ``TRAINING_ONLY_NEVER_P0`` -- it can train the trajectory artifact, and the
    artifact still has to pass the unchanged n>=20 / stable-gain gate.

    Returns None when either reconstruction input is absent, leaving the fold
    exactly as it was: present but without a conditional path state.
    """

    key = f"posterior::{station}"
    if key not in _HISTORICAL_CACHE:
        path = POSTERIOR_HISTORY_DIR / f"{station}_{HISTORICAL_CURVE_LEAD}.json"
        _HISTORICAL_CACHE[key] = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.is_file()
            else None
        )
    posterior_file = _HISTORICAL_CACHE[key]
    if not posterior_file:
        return None
    entry = next(
        (
            row
            for row in posterior_file.get("posteriors") or []
            if str(row.get("event_date")) == event_date
        ),
        None,
    )
    if entry is None:
        return None
    curve_key = f"curves::{event_date}"
    if curve_key not in _HISTORICAL_CACHE:
        path = CURVE_HISTORY_DIR / f"{event_date}_{HISTORICAL_CURVE_LEAD}.json"
        _HISTORICAL_CACHE[curve_key] = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.is_file()
            else None
        )
    curve_file = _HISTORICAL_CACHE[curve_key]
    if not curve_file:
        return None
    weights = {
        str(k): float(v)
        for k, v in (entry.get("system_weights") or {}).items()
    }
    bias = {
        str(k): float(v)
        for k, v in (entry.get("system_bias_c") or {}).items()
    }
    family_curves: dict[str, dict[str, Any]] = {}
    for system, per_station in (
        curve_file.get("curves_by_system") or {}
    ).items():
        curve = (per_station or {}).get(station)
        if curve and system in weights:
            family_curves[str(system)] = curve
    if len(family_curves) < 2:
        return None
    # Combine exactly the production shape: weighted sum of bias-corrected
    # family means at the common valid times.
    times_by_system = {
        system: {
            str(point["valid_time_utc"]): float(point["temperature_c"])
            for point in curve.get("points") or []
        }
        for system, curve in family_curves.items()
    }
    common_times = sorted(
        set.intersection(*(set(v) for v in times_by_system.values()))
    )
    if len(common_times) < 3:
        return None
    total_weight = sum(weights[s] for s in family_curves)
    probabilities = {
        str(k): float(v)
        for k, v in (entry.get("root_probabilities") or {}).items()
    }
    centers = {
        str(k): float(v)
        for k, v in (entry.get("bucket_centers_c") or {}).items()
    }
    if not probabilities or set(probabilities) != set(centers):
        return None
    posterior_sha = content_sha256(probabilities)
    checkpoint_id = f"hist-ew-{station.lower()}-{event_date}"
    # The run really became complete at init + margin the previous morning;
    # the download timestamp on the retained curve is transport, not
    # availability.
    run_day = (
        date.fromisoformat(event_date) - timedelta(days=1)
    ).isoformat()
    available_at = f"{run_day}T08:00:00.000Z"
    if available_at > cutoff_iso:
        return None
    sealed = build_root_future_temperature_curve({
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "root_checkpoint_id": checkpoint_id,
        "root_probability_sha256": posterior_sha,
        "available_at_utc": available_at,
        "source_snapshot_sha256": content_sha256({
            "family_curve_sha256_by_system": {
                system: str(curve.get("content_sha256") or "")
                for system, curve in sorted(family_curves.items())
            },
            "system_weights": weights,
            "system_bias_c": bias,
        }),
        "independence_family_ids": sorted(
            str(curve.get("independence_family_id") or system)
            for system, curve in family_curves.items()
        ),
        "points": [
            {
                "valid_time_utc": valid_time,
                "temperature_c": sum(
                    (weights[system] / total_weight)
                    * (
                        times_by_system[system][valid_time]
                        + bias.get(system, 0.0)
                    )
                    for system in family_curves
                ),
            }
            for valid_time in common_times
        ],
    })
    ordered = sorted(centers, key=lambda key: (centers[key], key))
    truth_bucket = min(
        ordered,
        key=lambda key: (
            abs(centers[key] - truth_tmax_c),
            centers[key],
            key,
        ),
    )
    try:
        path_state = compile_conditional_metar_path_state(
            [
                {
                    "observation_id": str(row["observation_id"]),
                    "observed_at_utc": str(row["valid_at"]),
                    "temperature_c": float(row["reported_temperature_c"]),
                    "raw_text": str(row.get("raw_text") or ""),
                }
                for row in prefix_rows
            ],
            as_of_utc=cutoff_iso,
            settlement_timezone=settlement_timezone,
            root_probabilities=probabilities,
            bucket_centers_c=centers,
            future_curve=sealed.to_dict(),
            root_checkpoint_id=checkpoint_id,
            root_probability_sha256=posterior_sha,
            station_id=station,
            event_date=event_date,
        )
    except ContractError:
        # A fold that cannot be reconstructed is not a build failure -- the
        # commonest case is a late bucket whose cutoff leaves fewer than two
        # curve points ahead of it, which is a true statement about that day
        # rather than a defect.  Returning None leaves the fold exactly as the
        # committed-root pass left it; raising here would take the whole
        # station's packet down over one unusable day.
        return None
    return {
        "root_checkpoint_id": checkpoint_id,
        "root_content_sha256": str(posterior_file.get("content_sha256") or ""),
        "root_stage": "HISTORICAL_EXPANDING_WINDOW",
        "evidence_class": HISTORICAL_FOLD_EVIDENCE_CLASS,
        "root_probabilities": probabilities,
        "bucket_centers_c": centers,
        "bucket_upper_bounds_c": {
            key: centers[key] + 0.499999 for key in ordered
        },
        "truth_bucket": truth_bucket,
        "running_high_c": running_high,
        "conditional_path_state": path_state.to_dict(),
    }


def build_training_packet(
    *,
    truth_rows: list[dict[str, Any]],
    observation_rows: list[dict[str, Any]],
    probability_rows: list[dict[str, Any]],
    station_id: str,
    settlement_timezone: str,
    local_time_bucket: str,
    minimum_prior_truths: int,
    minimum_effective_sample_n: float,
    decided_at_utc: str,
    valid_from_utc: str,
    valid_until_utc: str,
    generation: int,
    anchor_to_first_report: bool = False,
    require_source_poll_receipt: bool = False,
    method: str = "hourly_curve",
) -> dict[str, Any]:
    if method not in {"hourly_curve", "physical"}:
        raise ContractError("metar_trajectory_fold_method_invalid")
    station = station_id.strip().upper()
    bucket = local_time_bucket.strip().upper()
    if bucket not in BUCKET_START_HOUR:
        raise ContractError(
            "metar_trajectory_fold_local_time_bucket_invalid"
        )
    if (
        isinstance(minimum_prior_truths, bool)
        or minimum_prior_truths < 1
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise ContractError(
            "metar_trajectory_fold_parameter_invalid"
        )
    # ``decided_at_utc`` is the strict historical evaluation cut.  A fold is
    # OOS only when its target settlement day is already closed and its truth
    # was committed by this cut.  Future/forward rows are retained below as
    # observational diagnostics, but never enter ``folds`` or any training
    # count.
    evaluation_as_of = _parsed_utc(decided_at_utc)
    zone = ZoneInfo(settlement_timezone)
    observations_by_date: dict[str, list[dict[str, Any]]] = {}
    correction_dates: set[str] = set()
    for row in observation_rows:
        valid = _parsed_utc(str(row.get("valid_at") or ""))
        local_date = valid.astimezone(zone).date().isoformat()
        observations_by_date.setdefault(local_date, []).append(row)
        if int(row.get("correction_suspected") or 0) == 1:
            correction_dates.add(local_date)
    folds: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    observational_candidates: list[dict[str, str]] = []
    ordered_truth = sorted(
        truth_rows,
        key=lambda row: (
            str(row.get("local_date") or ""),
            int(row.get("revision_no") or 0),
        ),
    )
    for truth in ordered_truth:
        event_date = str(truth.get("local_date") or "")
        bucket_start = datetime.combine(
            datetime.fromisoformat(event_date).date(),
            time(BUCKET_START_HOUR[bucket], 0),
            tzinfo=zone,
        )
        local_cutoff = bucket_start
        cutoff = local_cutoff.astimezone(timezone.utc)
        truth_settled = _parsed_utc(
            str(truth.get("canonical_committed_at_utc") or "")
        )
        settlement_closed = _parsed_utc(
            settlement_day_window(event_date, settlement_timezone)[
                "target_day_end_utc"
            ]
        )
        if settlement_closed > evaluation_as_of:
            candidate = {
                "truth_id": str(truth.get("truth_id") or ""),
                "event_date": event_date,
                "evaluation_as_of_utc": _iso_utc(evaluation_as_of),
                "settlement_day_end_utc": _iso_utc(settlement_closed),
                "truth_settled_at_utc": _iso_utc(truth_settled),
                "training_cutoff_utc": _iso_utc(cutoff),
                "evidence_role": "observational_candidate_not_oos",
                "reason": "future_event_not_oos",
            }
            observational_candidates.append(candidate)
            excluded.append({
                "event_date": event_date,
                "reason": "future_event_not_oos",
            })
            continue
        if truth_settled > evaluation_as_of:
            candidate = {
                "truth_id": str(truth.get("truth_id") or ""),
                "event_date": event_date,
                "evaluation_as_of_utc": _iso_utc(evaluation_as_of),
                "settlement_day_end_utc": _iso_utc(settlement_closed),
                "truth_settled_at_utc": _iso_utc(truth_settled),
                "training_cutoff_utc": _iso_utc(cutoff),
                "evidence_role": "observational_candidate_not_oos",
                "reason": "future_truth_not_oos",
            }
            observational_candidates.append(candidate)
            excluded.append({
                "event_date": event_date,
                "reason": "future_truth_not_oos",
            })
            continue
        if cutoff > evaluation_as_of:
            candidate = {
                "truth_id": str(truth.get("truth_id") or ""),
                "event_date": event_date,
                "evaluation_as_of_utc": _iso_utc(evaluation_as_of),
                "settlement_day_end_utc": _iso_utc(settlement_closed),
                "truth_settled_at_utc": _iso_utc(truth_settled),
                "training_cutoff_utc": _iso_utc(cutoff),
                "evidence_role": "observational_candidate_not_oos",
                "reason": "future_training_cutoff_not_oos",
            }
            observational_candidates.append(candidate)
            excluded.append({
                "event_date": event_date,
                "reason": "future_training_cutoff_not_oos",
            })
            continue
        if truth_settled <= cutoff:
            excluded.append({
                "event_date": event_date,
                "reason": "evaluation_truth_visible_at_cutoff",
            })
            continue
        if event_date in correction_dates:
            excluded.append({
                "event_date": event_date,
                "reason": "correction_suspected",
            })
            continue
        prefix = [
            row
            for row in observations_by_date.get(event_date, ())
            if _parsed_utc(str(row.get("valid_at") or "")) <= cutoff
            and _parsed_utc(
                str(row.get("canonical_committed_at") or "")
            )
            <= cutoff
        ]
        if anchor_to_first_report:
            # Runtime P1 is evaluated after a real report enters the AWC
            # lane, not at an arbitrary local 00/06/12/18 boundary.  Anchor
            # each historical fold to the first report whose valid time is at
            # or after the bucket start and whose canonical receipt is already
            # visible.  This remains strict-as-of and prevents the NIGHT
            # bucket from requiring a report that cannot exist before 00:00.
            candidates = [
                row
                for row in observations_by_date.get(event_date, ())
                if _parsed_utc(str(row.get("valid_at") or "")) >= cutoff
                and _parsed_utc(str(row.get("canonical_committed_at") or ""))
                <= evaluation_as_of
                and (
                    not require_source_poll_receipt
                    or _poll_receipt_verified(row)
                )
            ]
            if candidates:
                # Anchor on the *earliest report*, ordered by valid time, and
                # take that one report's receipt.  Taking ``min`` over every
                # candidate's ``canonical_committed_at`` was a different
                # quantity: on days whose observations were backfilled, the
                # receipt records when the row was written, not when the
                # report existed, so the anchor jumped to the backfill.  WMKK
                # 2026-08-04 anchored at 2026-08-10T09:27Z -- six days after
                # its own event date, identically for all three buckets --
                # which is neither the bucket it claims nor strictly as-of.
                # The reconstruction already treats retained transport
                # timestamps as transport rather than availability for the
                # curve side; observations get the same treatment here.
                anchor = min(
                    candidates,
                    key=lambda row: (
                        _parsed_utc(str(row.get("valid_at") or "")),
                        _parsed_utc(
                            str(row.get("canonical_committed_at") or "")
                        ),
                    ),
                )
                # The later of the two keeps the fold strictly as-of on the
                # live lane, where the receipt trails the report by seconds.
                cutoff = max(
                    _parsed_utc(str(anchor.get("valid_at") or "")),
                    min(
                        _parsed_utc(
                            str(anchor.get("canonical_committed_at") or "")
                        ),
                        _parsed_utc(str(anchor.get("valid_at") or ""))
                        + timedelta(
                            seconds=MAX_REPORT_TRANSMISSION_LEAD_SECONDS
                        ),
                    ),
                )
                prefix = [
                    row
                    for row in observations_by_date.get(event_date, ())
                    if _parsed_utc(str(row.get("valid_at") or "")) <= cutoff
                    and _parsed_utc(
                        str(row.get("canonical_committed_at") or "")
                    )
                    <= cutoff
                    and (
                        not require_source_poll_receipt
                        or _poll_receipt_verified(row)
                    )
                ]
        elif require_source_poll_receipt:
            prefix = [row for row in prefix if _poll_receipt_verified(row)]
        prefix, prefix_consistent = _deduplicate_prefix(prefix)
        if not prefix_consistent:
            excluded.append({
                "event_date": event_date,
                "reason": "canonical_prefix_revision_ambiguous",
            })
            continue
        if not prefix:
            excluded.append({
                "event_date": event_date,
                "reason": "canonical_prefix_missing_at_bucket_start",
            })
            continue
        prior_truths = [
            row
            for row in ordered_truth
            if str(row.get("local_date") or "") < event_date
            and _parsed_utc(
                str(row.get("canonical_committed_at_utc") or "")
            )
            <= cutoff
        ]
        if len(prior_truths) < minimum_prior_truths:
            excluded.append({
                "event_date": event_date,
                "reason": "strict_asof_prior_truth_count_insufficient",
            })
            continue
        running_high = max(
            float(row["reported_temperature_c"]) for row in prefix
        )
        remaining = max(
            0.0, float(truth["official_tmax_c"]) - running_high
        )
        quantized = float(round(remaining))
        if quantized > 12.0 or abs(remaining - quantized) > 0.5000001:
            excluded.append({
                "event_date": event_date,
                "reason": "remaining_warming_outside_support",
            })
            continue
        sequence_identity = [{
            "observation_id": str(row["observation_id"]),
            "valid_at_utc": str(row["valid_at"]),
            "canonical_committed_at_utc": str(
                row["canonical_committed_at"]
            ),
            "temperature_c": float(row["reported_temperature_c"]),
            "raw_sha256": str(row["raw_sha256"]),
            "raw_text": str(row.get("raw_text") or ""),
            "source_poll_id": str(row.get("source_poll_id") or ""),
            "poll_source_lane": str(row.get("poll_source_lane") or ""),
            "poll_response_status": row.get("poll_response_status"),
            "poll_response_completed_at": str(
                row.get("poll_response_completed_at") or ""
            ),
        } for row in prefix]
        fold = {
            "fold_id": f"{station}-{bucket}-{event_date}",
            "target_local_date": event_date,
            "evaluation_truth_id": str(truth["truth_id"]),
            "evaluation_truth_settled_at_utc": _iso_utc(
                truth_settled
            ),
            "training_cutoff_utc": _iso_utc(cutoff),
            "training_cutoff_policy": (
                "FIRST_OFFICIAL_AWC_REPORT_RECEIPT_AFTER_BUCKET_START"
                if anchor_to_first_report
                else "LOCAL_BUCKET_START"
            ),
            "trajectory_training_truths": [{
                "truth_id": str(row["truth_id"]),
                "target_local_date": str(row["local_date"]),
                "settled_at_utc": _iso_utc(
                    _parsed_utc(
                        str(row["canonical_committed_at_utc"])
                    )
                ),
            } for row in prior_truths],
            "canonical_observation_sequence_sha256": content_sha256(
                sequence_identity
            ),
            "remaining_warming_c": quantized,
            "weight": 1.0,
        }
        eligible_roots = [
            row
            for row in probability_rows
            if str(row.get("event_date") or "") == event_date
            and str(row.get("settlement_timezone") or "")
            == settlement_timezone
            and _parsed_utc(
                str(row.get("decision_at_utc") or "")
            )
            <= cutoff
        ]
        if eligible_roots:
            ranked_roots = sorted(
                eligible_roots,
                key=lambda row: (
                    1 if str(row.get("stage")) == "P0-M" else 0,
                    _parsed_utc(str(row.get("decision_at_utc"))),
                    str(row.get("checkpoint_id") or ""),
                ),
                reverse=True,
            )
            for root in ranked_roots:
                probabilities = json.loads(
                    str(root.get("probabilities_json") or "{}")
                )
                lineage = json.loads(
                    str(root.get("lineage_json") or "{}")
                )
                centers = json.loads(
                    str(
                        lineage.get("bucket_centers_c_json")
                        or "{}"
                    )
                )
                if not centers:
                    # The dominant native P0 path publishes the bucket
                    # geometry as intervals only -- 426 of 482 committed P0
                    # roots carry ``bucket_intervals_c_json`` with no
                    # ``bucket_centers_c_json`` -- and requiring the centers
                    # key silently disqualified every such root, which left
                    # every fold without a conditional path state and the
                    # hourly-curve OOS gate permanently unsatisfiable.  The
                    # centers are fully determined by the intervals: the
                    # midpoint, or the finite edge offset by half a degree for
                    # an open-ended bucket, matching the runtime fallback in
                    # ``p1_compiler._bucket_geometry_c``.
                    root_intervals = json.loads(
                        str(
                            lineage.get("bucket_intervals_c_json")
                            or "{}"
                        )
                    )
                    if isinstance(root_intervals, dict):
                        derived: dict[str, float] = {}
                        for interval_bucket, bounds in root_intervals.items():
                            if (
                                not isinstance(bounds, (list, tuple))
                                or len(bounds) != 2
                            ):
                                derived = {}
                                break
                            lower, upper = bounds
                            if lower is not None and upper is not None:
                                derived[str(interval_bucket)] = (
                                    float(lower) + float(upper)
                                ) / 2.0
                            elif upper is not None:
                                derived[str(interval_bucket)] = (
                                    float(upper) - 0.5
                                )
                            elif lower is not None:
                                derived[str(interval_bucket)] = (
                                    float(lower) + 0.5
                                )
                            else:
                                derived = {}
                                break
                        centers = derived
                if not (
                    isinstance(probabilities, dict)
                    and isinstance(centers, dict)
                    and probabilities
                    and set(probabilities) == set(centers)
                ):
                    continue
                ordered = sorted(
                    centers, key=lambda key: (float(centers[key]), key)
                )
                truth_bucket = min(
                    ordered,
                    key=lambda key: (
                        abs(
                            float(centers[key])
                            - float(truth["official_tmax_c"])
                        ),
                        float(centers[key]),
                        key,
                    ),
                )
                future_curve = None
                # Serving reads the curve only under the hourly-curve method
                # (``metar_trajectory`` builds the path state with
                # ``future_curve=None`` for the physical conditioner); a
                # physical fold reading it anyway failed every evening fold
                # whose curve had run out (2026-10-01).
                future_curve_raw = (
                    lineage.get("conditional_future_temperature_curve_json")
                    if method != "physical" else None
                )
                if future_curve_raw:
                    future_curve = json.loads(str(future_curve_raw))
                    declared_curve_sha = str(
                        lineage.get(
                            "conditional_future_temperature_curve_sha256"
                        )
                        or ""
                    ).strip().lower()
                    if (
                        not isinstance(future_curve, dict)
                        or str(
                            future_curve.get("content_sha256") or ""
                        ).lower()
                        != declared_curve_sha
                    ):
                        raise ContractError(
                            "metar_trajectory_fold_future_curve_sha256_mismatch"
                        )
                    curve_root_stage = str(
                        lineage.get(
                            "conditional_future_temperature_curve_root_stage"
                        )
                        or root.get("stage")
                        or ""
                    )
                    curve_root_checkpoint = str(
                        lineage.get(
                            "conditional_future_temperature_curve_root_checkpoint_id"
                        )
                        or root.get("checkpoint_id")
                        or ""
                    )
                    curve_root_probability = str(
                        lineage.get(
                            "conditional_future_temperature_curve_root_probability_sha256"
                        )
                        or root.get("posterior_sha256")
                        or ""
                    )
                    direct_binding = (
                        curve_root_stage == str(root.get("stage") or "")
                        and curve_root_checkpoint
                        == str(root.get("checkpoint_id") or "")
                        and curve_root_probability
                        == str(root.get("posterior_sha256") or "")
                    )
                    p0_ancestor_binding = (
                        str(root.get("stage") or "") == "P0-M"
                        and curve_root_stage == "P0"
                        and curve_root_checkpoint
                        == str(
                            lineage.get("p0_root_checkpoint_id") or ""
                        )
                        and curve_root_probability
                        == str(
                            lineage.get("p0_root_posterior_sha256") or ""
                        )
                    )
                    if not direct_binding and not p0_ancestor_binding:
                        raise ContractError(
                            "metar_trajectory_fold_future_curve_binding_invalid"
                        )
                fold.update({
                    "root_checkpoint_id": str(
                        root.get("checkpoint_id") or ""
                    ),
                    "root_content_sha256": str(
                        root.get("content_sha256") or ""
                    ),
                    "root_stage": str(root.get("stage") or ""),
                    "root_probabilities": probabilities,
                    "bucket_centers_c": centers,
                    "bucket_upper_bounds_c": {
                        key: float(centers[key]) + 0.499999
                        for key in ordered
                    },
                    "truth_bucket": truth_bucket,
                    "running_high_c": running_high,
                })
                path_state = compile_conditional_metar_path_state(
                    [
                        {
                            "observation_id": str(row["observation_id"]),
                            "observed_at_utc": str(row["valid_at"]),
                            "temperature_c": float(
                                row["reported_temperature_c"]
                            ),
                            "raw_text": str(row.get("raw_text") or ""),
                        }
                        for row in prefix
                    ],
                    as_of_utc=_iso_utc(cutoff),
                    settlement_timezone=settlement_timezone,
                    root_probabilities=probabilities,
                    bucket_centers_c=centers,
                    future_curve=future_curve,
                    # Serving binds the curve's root only when it reads a curve.
                    root_checkpoint_id=str(
                        (lineage.get(
                            "conditional_future_temperature_curve_root_checkpoint_id"
                        ) if future_curve is not None else None)
                        or root.get("checkpoint_id")
                        or ""
                    ),
                    root_probability_sha256=str(
                        (lineage.get(
                            "conditional_future_temperature_curve_root_probability_sha256"
                        ) if future_curve is not None else None)
                        or root.get("posterior_sha256")
                        or ""
                    ),
                    station_id=station,
                    event_date=event_date,
                )
                fold["conditional_path_state"] = path_state.to_dict()
                break
        committed_state = fold.get("conditional_path_state") or {}
        if not committed_state.get("future_curve_complete"):
            # Fires both when no root bound at all and when the bound root
            # predates the curve-carrying compiler (2026-08-02): a fold whose
            # path state lacks a complete future curve is dropped by the
            # hourly-curve fit either way, so the labelled reconstruction is
            # strictly more evidence, never less.
            historical = _historical_fold_binding(
                station=station,
                event_date=event_date,
                settlement_timezone=settlement_timezone,
                cutoff_iso=_iso_utc(cutoff),
                prefix_rows=prefix,
                running_high=running_high,
                truth_tmax_c=float(truth["official_tmax_c"]),
            )
            if historical is not None:
                fold.update(historical)
        folds.append(fold)
    support = {str(value): 1.0 / 13.0 for value in range(13)}
    # ``trained_through`` is a claim about training *content*, not about when
    # the truth rows were materialized.  The serve-side leakage gate compares
    # it against the served day's first observation, and stamping the truth
    # rows' canonical-commit wall clock here locked every station whose event
    # day had already begun by the time the nightly truth sweep ran (14
    # stations for the whole of 2026-08-20).  The last instant the folds can
    # have seen is the close of the newest settled fold day; one second
    # inside it keeps the strict-less-than gate open for stations whose first
    # report of the next day is stamped exactly on the day boundary (hourly
    # :00 METARs at whole-hour timezones).
    trained_through = max(
        (
            _parsed_utc(
                str(
                    settlement_day_window(
                        str(row["local_date"]), settlement_timezone
                    )["target_day_end_utc"]
                )
            )
            - timedelta(seconds=1)
            for row in ordered_truth
            if any(
                fold["evaluation_truth_id"] == row["truth_id"]
                for fold in folds
            )
        ),
        default=_parsed_utc(decided_at_utc),
    )
    source_manifest = {
        "schema": "bayes_basket_metar_trajectory_fold_source_v1",
        "station_id": station,
        "settlement_timezone": settlement_timezone,
        "local_time_bucket": bucket,
        "truth_row_count": len(truth_rows),
        "observation_row_count": len(observation_rows),
        "probability_root_row_count": len(probability_rows),
        "evaluation_as_of_utc": _iso_utc(evaluation_as_of),
        "training_cutoff_policy": (
            "FIRST_OFFICIAL_AWC_REPORT_RECEIPT_AFTER_BUCKET_START"
            if anchor_to_first_report
            else "LOCAL_BUCKET_START"
        ),
        "source_poll_receipt_required": bool(require_source_poll_receipt),
        "eligible_fold_count": len(folds),
        "v3_eligible_fold_count": sum(
            bool(
                fold.get("conditional_path_state", {}).get(
                    "future_curve_complete"
                )
            )
            for fold in folds
        ),
        "v3_incomplete_future_curve_fold_count": sum(
            not bool(
                fold.get("conditional_path_state", {}).get(
                    "future_curve_complete"
                )
            )
            for fold in folds
        ),
        "observational_candidate_count": len(observational_candidates),
        "observational_candidates": observational_candidates,
        "excluded": excluded,
    }
    source_manifest["content_sha256"] = content_sha256(
        source_manifest
    )
    identity = {
        "schema": SCHEMA,
        "source_manifest": source_manifest,
        "folds": folds,
        "smoothing_candidates": [0.1, 0.5, 1.0, 2.0, 5.0, 10.0],
        "minimum_training_folds": 2,
        "minimum_effective_sample_n": float(
            minimum_effective_sample_n
        ),
        "artifact_parameters": {
            # One id per fitted artifact, not one per scope.  The activation
            # table keys on this, so a fixed per-scope id can be inserted
            # exactly once: the second artifact for the same station and
            # bucket collides, the store raises, and the whole commit rolls
            # back -- taking the artifact and its promotion receipt with it.
            # That is how 2707 artifacts produced 76 activations, and why a
            # scope that ever activated could never be re-fitted.  The
            # evidence date makes it unique per training run and keeps it
            # readable.
            # The physical lane carries its own tag: both methods share the
            # scope key, and a same-day refit of each would otherwise collide
            # on this id in the activation store and roll the commit back.
            # The date made this unique per *day*, not per fit, so a second
            # fit of the same station and bucket on one day reuses an id the
            # first already took.  The activation store keys on it, so the
            # second activation is refused on the primary key while its
            # artifact -- inserted before it -- stays.  That is how the
            # season migration produced six artifacts and zero activations
            # on 2026-08-28: the resident refit after the restart, and every
            # id it wanted was already held by that morning's JJA row.
            # Same lesson the delta-v2 re-mint learned: name the id after
            # the content, so a genuinely new artifact is a new row and an
            # unchanged one is a no-op.
            "activation_id": (
                f"metar-trajectory-{station}-{bucket.lower()}"
                + ("-physical" if method == "physical" else "")
                + f"-{_iso_utc(trained_through)[:10].replace('-', '')}"
                + "-" + content_sha256({
                    "station_id": station,
                    "bucket": bucket,
                    "method": method,
                    "season_bin": station_season_bin(
                        station, valid_from_utc[:10]
                    ),
                    "trained_through_utc": _iso_utc(trained_through),
                    "valid_from_utc": valid_from_utc,
                })[:12]
            ),
            "station_id": station,
            "source_lane": "awc",
            # Charter 62's station-local label, not the calendar trigram this
            # was hardcoded to.  The consumer builds the same scope key from
            # the P0 root's own lineage, which moved to PEAK/DESCENT/TROUGH/
            # ASCENT/ALLYEAR on 2026-08-25 -- so from that day every lookup
            # asked for ``awc|PEAK|…`` while all 6,642 artifacts answered to
            # ``awc|JJA|…``.  Nothing raised: the miss falls back to applied
            # strength 0, so P1's soft likelihood went from 72% coverage on
            # 08-23 to 14.5% on the 25th and 0% from the 26th, silently.
            # The disjoint vocabularies were meant to make exactly this join
            # fail loudly; a hardcoded literal on one side is how it failed
            # quietly instead.
            # Keyed on the serving window's start, not the training window:
            # charter 62 is explicit that these labels say which serving
            # dates an artifact was emitted for, and that the fit itself is
            # a rolling tail window, cross-season by construction.
            "season_bin": station_season_bin(station, valid_from_utc[:10]),
            "local_time_bucket": bucket,
            "maturity_bin": "D0",
            "settlement_definition": "official_daily_tmax_c",
            "settlement_timezone": settlement_timezone,
            "feature_definitions": (
                {
                    "running_high_c": (
                        "max canonical AWC METAR/SPECI temperature available "
                        "at local bucket start"
                    ),
                    "remaining_warming_c": (
                        "final official tmax minus running high, nearest 1C"
                    ),
                    "conditional_path_state": (
                        "strict-as-of remaining time, full-prefix "
                        "temperature momentum and acceleration, and METAR "
                        "physical state; model-derived terms excluded"
                    ),
                }
                if method == "physical"
                else {
                    "running_high_c": (
                        "max canonical AWC METAR/SPECI temperature available "
                        "at local bucket start"
                    ),
                    "remaining_warming_c": (
                        "final official tmax minus running high, nearest 1C"
                    ),
                    "conditional_path_state": (
                        "strict-as-of remaining time, full-prefix temperature "
                        "momentum and acceleration, METAR physical state, and "
                        "model-root expected remaining warming"
                    ),
                    "conditional_future_temperature_curve": (
                        "same-capture, content-addressed hourly curve combined "
                        "from at least two activated independent model families"
                    ),
                }
            ),
            "missing_observation_policy": "DEGRADE_STRENGTH",
            "expected_interval_minutes": 60,
            "long_interval_minutes": 90,
            "max_missing_intervals": 4,
            "missing_interval_strength_factor": 0.8,
            "long_interval_strength_factor": 0.5,
            "path_feature_schema": PATH_STATE_SCHEMA,
            "path_model_coefficients": (
                {
                    "base_distribution_mean": 0.3,
                    "momentum_c_per_hour": 0.35,
                    "acceleration_c_per_hour2": 0.1,
                    "drawdown_from_high_c": -0.5,
                    "dewpoint_depression_c": 0.01,
                    "cloud_cover_fraction": -0.8,
                    "precipitation_present": -1.0,
                    "wind_speed_mps": -0.02,
                    "remaining_hours": 0.02,
                }
                if method == "physical"
                else {
                    "base_distribution_mean": 0.3,
                    "root_expected_remaining_warming": 0.5,
                    "momentum_c_per_hour": 0.35,
                    "acceleration_c_per_hour2": 0.1,
                    "drawdown_from_high_c": -0.5,
                    "dewpoint_depression_c": 0.01,
                    "cloud_cover_fraction": -0.8,
                    "precipitation_present": -1.0,
                    "wind_speed_mps": -0.02,
                    "remaining_hours": 0.02,
                    "predicted_peak_remaining_warming_c": 0.35,
                    "minutes_to_predicted_peak": 0.02,
                    "next_curve_delta_from_high_c": 0.15,
                    "future_curve_slope_c_per_hour": 0.10,
                }
            ),
            "path_model_sigma_c": 0.8,
            "hard_exclusion_baseline_distribution": support,
            "base_strength": 0.8,
            "maturity_strength": 0.75,
            "likelihood_ratio_floor": 0.25,
            "likelihood_ratio_ceiling": 4.0,
            "method_version": (
                METAR_TRAJECTORY_PHYSICAL_ABSOLUTE_TMAX_METHOD_VERSION
                if method == "physical"
                else METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION
            ),
            "trained_through_utc": _iso_utc(trained_through),
            "valid_from_utc": valid_from_utc,
            "valid_until_utc": valid_until_utc,
        },
        "generation": generation,
        "decided_at_utc": decided_at_utc,
        "boundary": {
            "execution_mode": "production_no_live",
            "writes_production_database": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        },
    }
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target-db",
        type=Path,
        default=ROOT / "data/bayes_basket_target_ssot.db",
    )
    parser.add_argument(
        "--archive-db",
        type=Path,
        default=ROOT / "data/archive.db",
    )
    parser.add_argument("--station-id", default="RKPK")
    parser.add_argument("--settlement-timezone", default="Asia/Seoul")
    parser.add_argument(
        "--local-time-bucket",
        choices=tuple(BUCKET_START_HOUR),
        required=True,
    )
    parser.add_argument("--minimum-prior-truths", type=int, default=3)
    parser.add_argument(
        "--minimum-effective-sample-n",
        type=float,
        default=MINIMUM_EFFECTIVE_SAMPLE_N,
    )
    parser.add_argument("--decided-at-utc", required=True)
    parser.add_argument("--valid-from-utc", required=True)
    parser.add_argument("--valid-until-utc", required=True)
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument(
        "--anchor-to-first-report",
        action="store_true",
        help=(
            "Anchor each historical fold to the first official AWC report "
            "receipt after the bucket start instead of the nominal bucket "
            "boundary."
        ),
    )
    parser.add_argument(
        "--require-source-poll-receipt",
        action="store_true",
        help=(
            "Require the joined AWC HTTP-200 poll receipt and exact raw "
            "report-time binding for every fold observation."
        ),
    )
    parser.add_argument(
        "--method",
        choices=("hourly_curve", "physical"),
        default="hourly_curve",
        help=(
            "Which conditioner the fitted artifact will serve under. "
            "'physical' conditions on observed METAR state alone -- no root "
            "curve, no model-derived features -- so its folds need no curve "
            "and its gate scores the physical conditioner."
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--root-cache", default=str(ROOT_CACHE_PATH),
        help="frozen station-days' P0/P0-M rows; '' reads every root off the SSOT",
    )
    args = parser.parse_args()
    packet = build_training_packet(
        truth_rows=_read_truth(
            args.target_db.expanduser().resolve(),
            station_id=args.station_id.upper(),
        ),
        observation_rows=_read_observations(
            args.archive_db.expanduser().resolve(),
            station_id=args.station_id.upper(),
        ),
        probability_rows=_read_probability_roots(
            args.target_db.expanduser().resolve(),
            station_id=args.station_id.upper(),
            cache_path=args.root_cache or None,
        ),
        station_id=args.station_id,
        settlement_timezone=args.settlement_timezone,
        local_time_bucket=args.local_time_bucket,
        minimum_prior_truths=args.minimum_prior_truths,
        minimum_effective_sample_n=args.minimum_effective_sample_n,
        decided_at_utc=args.decided_at_utc,
        valid_from_utc=args.valid_from_utc,
        valid_until_utc=args.valid_until_utc,
        generation=args.generation,
        anchor_to_first_report=bool(args.anchor_to_first_report),
        require_source_poll_receipt=bool(args.require_source_poll_receipt),
        method=args.method,
    )
    write_bounded_json(
        args.output.expanduser().resolve(),
        packet,
        limit=MAX_OUTPUT_BYTES,
    )
    print(packet["content_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
