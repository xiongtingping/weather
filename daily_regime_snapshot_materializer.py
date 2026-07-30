"""Persist daily forecast regime projections for every configured city.

The projection is deliberately separate from posterior/fact tables. Official
NWP regime labels consume only direct physical fields (humidity, wind, cloud,
precipitation and convection geometry); temperature peaks are never used to
classify regime. AMSC TAF is retained as a declared weak-source component and
is used only when the direct NWP regime is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from official_nwp_physical_regime_runtime import (
    aggregate_official_nwp_physical_regime,
    derive_official_nwp_physical_regime,
)


SCHEMA_VERSION = "daily_regime_snapshot_materializer_v1"
CHINA_TZ = ZoneInfo("Asia/Shanghai")

# These are deliberately small METAR token classifiers.  A regime projection
# may consume only the weather-description tokens from the immutable raw
# report; it never reparses the temperature as a weather type or settlement
# fact.  The hard-NO reader remains the owner of temperature semantics.
_PRECIP_TOKEN_RE = re.compile(
    r"^(?:[-+]?)(?:(?:MI|BC|PR|DR|BL|SH|TS|FZ)?(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP)|VCSH|VCTS)$",
    re.IGNORECASE,
)
_CLOUD_TOKEN_RE = re.compile(r"^(?:FEW|SCT|BKN|OVC|VV)\d{3}(?:CB|TCU)?$", re.IGNORECASE)
_CLEAR_CLOUD_TOKENS = frozenset({"CAVOK", "NSC", "SKC", "CLR"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _metar_weather_tokens(raw_text: Any) -> tuple[list[str], list[str]]:
    """Return precipitation and cloud tokens without interpreting temperature."""

    tokens = [item.strip().upper().rstrip("=") for item in _text(raw_text).split()]
    precipitation = [item for item in tokens if _PRECIP_TOKEN_RE.fullmatch(item)]
    clouds = [item for item in tokens if item in _CLEAR_CLOUD_TOKENS or _CLOUD_TOKEN_RE.fullmatch(item)]
    return precipitation, clouds


def _materialized_amsc_observed_regime(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
) -> dict[str, Any] | None:
    """Build a day regime from the persisted official AMSC METAR/SPECI lane.

    This is intentionally a materializer-side projection.  The UI never
    parses raw reports, and the AWC lane is not a fallback for China stations.
    Once any official report exists for the target local day, it outranks
    forecast context for the day's displayed regime.
    """

    target = _text(event_date)[:10]
    try:
        local_start = datetime.fromisoformat(target).replace(tzinfo=CHINA_TZ)
    except ValueError:
        return None
    start_utc = local_start.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    end_utc = (local_start + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    try:
        rows = conn.execute(
            """SELECT observation_id,source_lane,report_type,valid_at,
                      reported_temperature_c,raw_sha256,raw_text,
                      first_seen_at,canonical_committed_at,correction_suspected
                 FROM reported_trajectory_observations
                WHERE station_id=? AND source_lane='amsc'
                  AND report_type IN ('METAR','SPECI')
                  AND valid_at>=? AND valid_at<?
                  AND correction_suspected=0
                ORDER BY valid_at,observation_id""",
            (_text(station_id).upper(), start_utc, end_utc),
        ).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None

    reports: list[dict[str, Any]] = []
    precip_count = 0
    convective_count = 0
    clear_count = 0
    cloud_count = 0
    for row in rows:
        precipitation, clouds = _metar_weather_tokens(row[6])
        has_precipitation = bool(precipitation)
        is_convective = any("TS" in token for token in precipitation)
        if has_precipitation:
            precip_count += 1
        if is_convective:
            convective_count += 1
        if any(token in _CLEAR_CLOUD_TOKENS or token.startswith("FEW") for token in clouds):
            clear_count += 1
        if clouds:
            cloud_count += 1
        reports.append({
            "source_kind": "official_amsc_metar_observation",
            "source_lane": _text(row[1]),
            "observation_id": _text(row[0]),
            "report_type": _text(row[2]),
            "valid_at": _text(row[3]),
            "raw_sha256": _text(row[5]),
            "first_seen_at": _text(row[7]),
            "canonical_committed_at": _text(row[8]),
            "precipitation_tokens": precipitation,
            "cloud_tokens": clouds,
            "has_precipitation": has_precipitation,
            "is_convective": is_convective,
        })

    if convective_count:
        primary = "convection_or_showers"
    elif precip_count:
        primary = "wet_precipitation"
    elif clear_count and clear_count >= max(1, (len(reports) + 1) // 2):
        primary = "clear_dry"
    elif cloud_count:
        primary = "cloudy_stable"
    else:
        primary = "unknown_forecast_regime"
    latest = reports[-1]
    return {
        "schema": "official_amsc_metar_daily_observed_regime_v1",
        "state": "available",
        "primary": primary,
        "forecast_weather_regime": primary,
        "source": "official_amsc_metar_observation",
        "report_count": len(reports),
        "precipitation_report_count": precip_count,
        "convective_report_count": convective_count,
        "clear_report_count": clear_count,
        "cloud_report_count": cloud_count,
        "latest_report": latest,
        "reports": reports,
        "boundary": {
            "temperature_fields_consumed": False,
            "temperature_peak_removed_from_regime": True,
            "official_observation_overrides_forecast_context": True,
            "awc_fallback_used": False,
            "hard_no_reachable": False,
        },
    }


def _latest_run_rows(rows: Sequence[sqlite3.Row]) -> list[sqlite3.Row]:
    """Keep one complete-ish latest run per provider; never mix old runs."""

    latest_by_provider: dict[str, str] = {}
    for row in rows:
        provider = _text(row["provider_id"])
        run_at = _text(row["run_at_utc"])
        if provider and run_at and run_at > latest_by_provider.get(provider, ""):
            latest_by_provider[provider] = run_at
    return [
        row for row in rows
        if _text(row["provider_id"]) in latest_by_provider
        and _text(row["run_at_utc"]) == latest_by_provider[_text(row["provider_id"])]
    ]


def materialize_daily_regime_snapshot(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    now_utc: str | None = None,
) -> dict[str, Any]:
    station = _text(station_id).upper()
    target = _text(event_date)[:10]
    checked_at = _text(now_utc) or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    nwp_rows = conn.execute(
        """SELECT snapshot_id,provider_id,run_at_utc,valid_time_utc,created_at,
                  source_snapshot_sha256,normalized_features_json
             FROM official_nwp_station_snapshots
            WHERE station_id=? AND event_date=? AND state='available'
            ORDER BY run_at_utc,valid_time_utc,snapshot_id""",
        (station, target),
    ).fetchall()
    nwp_rows = _latest_run_rows(nwp_rows)
    point_regimes: list[dict[str, Any]] = []
    nwp_revisions: list[dict[str, Any]] = []
    for row in nwp_rows:
        try:
            features = json.loads(_text(row["normalized_features_json"]) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            features = {}
        if not isinstance(features, Mapping):
            features = {}
        point_regime = derive_official_nwp_physical_regime(features)
        # Keep provider/valid-time metadata only for safe accumulation
        # differencing inside the aggregate; it is not a weather feature.
        point_regime["_provider_id"] = _text(row["provider_id"])
        point_regime["_valid_time_utc"] = _text(row["valid_time_utc"])
        point_regimes.append(point_regime)
        nwp_revisions.append({
            "source_kind": "official_nwp_station_snapshot",
            "provider_id": _text(row["provider_id"]),
            "snapshot_id": _text(row["snapshot_id"]),
            "source_snapshot_sha256": _text(row["source_snapshot_sha256"]),
            "run_at_utc": _text(row["run_at_utc"]),
            "valid_time_utc": _text(row["valid_time_utc"]),
            "created_at": _text(row["created_at"]),
        })
    nwp_regime = aggregate_official_nwp_physical_regime(point_regimes) if point_regimes else {
        "schema": "official_nwp_daily_physical_regime_v2",
        "state": "insufficient_physical_fields",
        "basin_regime": "unknown",
        "basin_regime_confidence": 0.0,
        "forecast_weather_regime": "unknown_forecast_regime",
        "forecast_weather_regime_origin": "official_nwp_direct_physical_fields_v2",
        "point_count": 0,
        "known_point_count": 0,
        "known_point_share": 0.0,
        "boundary": {"temperature_fields_consumed": False, "temperature_curve_weather_type_inference_forbidden": True},
    }

    taf_rows = conn.execute(
        """SELECT source_revision_id,issue_time_utc,first_system_seen_at,
                  forecast_weather_regime,raw_sha256,created_at
             FROM amsc_taf_snapshots
            WHERE station_id=? AND event_date=? AND state IN ('available','superseded')
            ORDER BY issue_time_utc DESC,created_at DESC,source_revision_id DESC""",
        (station, target),
    ).fetchall()
    taf = None
    if taf_rows:
        row = taf_rows[0]
        taf = {
            "source_kind": "official_amsc_taf",
            "source_revision_id": _text(row["source_revision_id"]),
            "issue_time_utc": _text(row["issue_time_utc"]),
            "first_system_seen_at": _text(row["first_system_seen_at"]),
            "forecast_weather_regime": _text(row["forecast_weather_regime"]) or "unknown_forecast_regime",
            "raw_sha256": _text(row["raw_sha256"]),
            "created_at": _text(row["created_at"]),
        }

    observed = _materialized_amsc_observed_regime(
        conn,
        station_id=station,
        event_date=target,
    )

    nwp_primary = _text(nwp_regime.get("forecast_weather_regime"))
    nwp_basin = _text(nwp_regime.get("basin_regime"))
    if observed is not None and _text(observed.get("primary")):
        # A report for the target local day is the current day's weather
        # state.  Forecast/NWP/TAF remains lineage context, never the primary
        # label after official observations have arrived.
        primary = _text(observed.get("primary"))
        primary_source = "official_amsc_metar_observation"
    elif nwp_primary and nwp_primary != "unknown_forecast_regime":
        primary = nwp_primary
        primary_source = "official_nwp_direct_physical_fields"
    elif nwp_basin and nwp_basin != "unknown":
        primary = nwp_basin
        primary_source = "official_nwp_direct_physical_fields"
    elif taf is not None:
        primary = _text(taf.get("forecast_weather_regime")) or "unknown_forecast_regime"
        primary_source = "official_amsc_taf_declared_regime"
    else:
        primary = "unknown_forecast_regime"
        primary_source = "no_materialized_regime_source"

    revisions: list[dict[str, Any]] = [*nwp_revisions]
    if taf is not None:
        revisions.append(dict(taf))
    if observed is not None:
        revisions.append({
            "source_kind": "official_amsc_metar_observation_regime",
            "source_lane": "amsc",
            "station_id": station,
            "event_date": target,
            "primary": _text(observed.get("primary")),
            "report_count": int(observed.get("report_count") or 0),
            "reports": [
                {
                    "observation_id": _text(item.get("observation_id")),
                    "report_type": _text(item.get("report_type")),
                    "valid_at": _text(item.get("valid_at")),
                    "raw_sha256": _text(item.get("raw_sha256")),
                    "first_seen_at": _text(item.get("first_seen_at")),
                }
                for item in observed.get("reports") or []
                if isinstance(item, Mapping)
            ],
        })
    if not revisions:
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "waiting",
            "station_id": station,
            "event_date": target,
            "reason": "regime_source_not_materialized",
            "places_orders": False,
        }
    source_identity = {
        "station_id": station,
        "event_date": target,
        "revisions": revisions,
        "primary": primary,
        "nwp_regime": nwp_regime,
        "observed_regime": observed or {},
    }
    source_sha = _sha(source_identity)
    snapshot_id = "dreg_" + source_sha[:24]
    if observed is not None:
        # Freshness of an observed-day regime follows the newest official
        # report, not a future valid time from a stale forecast curve.
        source_times = [
            _time(item.get(key))
            for item in observed.get("reports") or []
            for key in ("valid_at", "first_seen_at", "canonical_committed_at")
            if _time(item.get(key)) is not None
        ]
    else:
        source_times = [
            _time(item.get(key))
            for item in revisions
            for key in ("created_at", "issue_time_utc", "valid_time_utc")
            if _time(item.get(key)) is not None
        ]
    as_of = max(source_times).isoformat(timespec="milliseconds").replace("+00:00", "Z") if source_times else checked_at
    regime = {
        "schema": "daily_city_regime_v1",
        "state": "available",
        "primary": primary,
        "primary_source": primary_source,
        "forecast_weather_regime": primary,
        "observed_weather_regime": _text((observed or {}).get("primary")),
        "observed_regime": observed or {},
        "declared_taf_regime": _text((taf or {}).get("forecast_weather_regime")),
        "nwp_regime": nwp_regime,
        "source_count": len(revisions),
        "nwp_point_count": len(nwp_rows),
        "boundary": {
            "forecast_only": observed is None,
            "official_observation_present": observed is not None,
            "temperature_fields_consumed": False,
            "temperature_peak_removed_from_regime": True,
            "hard_observation_eligible": False,
            "hard_no_reachable": False,
        },
    }
    snapshot_source_kind = (
        "official_amsc_metar_taf_and_nwp"
        if observed is not None
        else "official_amsc_taf_and_nwp"
    )
    conn.execute(
        """INSERT OR IGNORE INTO daily_regime_snapshots(
             regime_snapshot_id,station_id,event_date,source_kind,
             source_revisions_json,regime_json,source_sha256,as_of,created_at,state
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            snapshot_id, station, target, snapshot_source_kind,
            _json(revisions), _json(regime), source_sha, as_of, checked_at, "available",
        ),
    )
    inserted = int(conn.execute("SELECT changes()").fetchone()[0] or 0)
    conn.commit()
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "materialized" if inserted else "already_materialized",
        "station_id": station,
        "event_date": target,
        "snapshot_id": snapshot_id,
        "source_sha256": source_sha,
        "source_count": len(revisions),
        "nwp_point_count": len(nwp_rows),
        "primary": primary,
        "primary_source": primary_source,
        "source_kind": snapshot_source_kind,
        "as_of": as_of,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "materialize_daily_regime_snapshot"]
