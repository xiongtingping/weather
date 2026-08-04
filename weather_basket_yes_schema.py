"""Additive SQLite schema for the AWC truth ingress and pure-YES basket."""

from __future__ import annotations

import json
import sqlite3


SCHEMA_VERSION = "weather_basket_yes_awc_truth_schema_v1"

REQUIRED_TABLES = frozenset({
    "reported_trajectory_market_registry",
    "reported_trajectory_market_registry_refresh_state",
    "reported_trajectory_source_polls",
    "reported_trajectory_observations",
    "reported_trajectory_observation_origins",
    "amsc_taf_snapshots",
    "official_nwp_station_snapshots",
    "daily_regime_snapshots",
    "reported_trajectory_station_day_bounds",
    "reported_trajectory_station_halts",
    "reported_trajectory_station_halt_reviews",
    "reported_trajectory_activation_state",
})

CORRECTION_REVIEW_DDL = """
CREATE TABLE IF NOT EXISTS reported_trajectory_station_halt_reviews (
  review_id TEXT PRIMARY KEY,
  source_lane TEXT NOT NULL CHECK(source_lane IN ('amsc','awc')),
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  triggering_observation_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(
    decision IN ('CLEAR_REFERENCE_ONLY','QUARANTINE_STATION_DAY')
  ),
  reviewer TEXT NOT NULL,
  rationale TEXT NOT NULL,
  reviewed_at TEXT NOT NULL,
  supersedes_review_id TEXT,
  evidence_json TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  FOREIGN KEY(triggering_observation_id)
    REFERENCES reported_trajectory_observations(observation_id),
  FOREIGN KEY(supersedes_review_id)
    REFERENCES reported_trajectory_station_halt_reviews(review_id)
);

CREATE INDEX IF NOT EXISTS idx_reported_trajectory_halt_reviews_scope
ON reported_trajectory_station_halt_reviews(
  source_lane,station_id,event_date,triggering_observation_id,reviewed_at
);

CREATE TRIGGER IF NOT EXISTS reported_trajectory_halt_reviews_no_update
BEFORE UPDATE ON reported_trajectory_station_halt_reviews
BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER IF NOT EXISTS reported_trajectory_halt_reviews_no_delete
BEFORE DELETE ON reported_trajectory_station_halt_reviews
BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""

DDL = """
CREATE TABLE IF NOT EXISTS reported_trajectory_market_registry (
  slug TEXT NOT NULL,
  event_date TEXT NOT NULL,
  outcome TEXT NOT NULL,
  city_slug TEXT NOT NULL,
  station_id TEXT,
  settlement_timezone TEXT,
  settlement_unit TEXT,
  source_lane TEXT NOT NULL CHECK(source_lane IN ('amsc','awc','blocked')),
  source_eligible INTEGER NOT NULL CHECK(source_eligible IN (0,1)),
  source_block_reason TEXT,
  resolution_url TEXT,
  rules_sha256 TEXT NOT NULL,
  yes_token TEXT,
  no_token TEXT,
  market_active INTEGER NOT NULL DEFAULT 0,
  market_accepting_orders INTEGER NOT NULL DEFAULT 0,
  registry_json TEXT NOT NULL,
  refreshed_at TEXT NOT NULL,
  PRIMARY KEY(slug,event_date,outcome)
);

CREATE INDEX IF NOT EXISTS idx_reported_trajectory_registry_station_day
ON reported_trajectory_market_registry(station_id,event_date,source_eligible,market_active);

CREATE TABLE IF NOT EXISTS reported_trajectory_market_registry_refresh_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  last_successful_refresh_at TEXT NOT NULL,
  semantic_sha256 TEXT NOT NULL CHECK(length(semantic_sha256)=64),
  official_market_count INTEGER NOT NULL CHECK(official_market_count>=0),
  eligible_market_count INTEGER NOT NULL CHECK(eligible_market_count>=0),
  registry_row_count INTEGER NOT NULL CHECK(registry_row_count>=0)
);

CREATE TABLE IF NOT EXISTS reported_trajectory_source_polls (
  poll_id TEXT PRIMARY KEY,
  source_lane TEXT NOT NULL CHECK(source_lane IN ('amsc','awc')),
  station_ids_json TEXT NOT NULL,
  request_started_at TEXT NOT NULL,
  response_started_at TEXT,
  response_completed_at TEXT NOT NULL,
  response_status INTEGER,
  duration_ms REAL NOT NULL,
  report_count INTEGER NOT NULL,
  new_report_count INTEGER NOT NULL,
  error_code TEXT,
  poll_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reported_trajectory_source_polls_lane_time
ON reported_trajectory_source_polls(source_lane,response_completed_at);

CREATE TABLE IF NOT EXISTS reported_trajectory_observations (
  observation_id TEXT PRIMARY KEY,
  source_lane TEXT NOT NULL CHECK(source_lane IN ('amsc','awc')),
  station_id TEXT NOT NULL,
  report_type TEXT NOT NULL CHECK(report_type IN ('METAR','SPECI')),
  valid_at TEXT NOT NULL,
  reported_temperature_c INTEGER NOT NULL,
  raw_sha256 TEXT NOT NULL,
  raw_text TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  parsed_at TEXT NOT NULL,
  canonical_committed_at TEXT NOT NULL,
  fetch_started_at TEXT NOT NULL,
  fetch_completed_at TEXT NOT NULL,
  source_poll_id TEXT NOT NULL,
  correction_suspected INTEGER NOT NULL DEFAULT 0 CHECK(correction_suspected IN (0,1)),
  UNIQUE(source_lane,station_id,valid_at,report_type,raw_sha256)
);

CREATE INDEX IF NOT EXISTS idx_reported_trajectory_observations_station_time
ON reported_trajectory_observations(station_id,valid_at);

-- Weak forecast sources are persisted separately from hard METAR/SPECI
-- observations.  Each TAF peak is immutable by source revision and target
-- event date, which lets the UI and posterior lineage point to one exact
-- provider message without making it eligible for hard-NO decisions.
CREATE TABLE IF NOT EXISTS amsc_taf_snapshots (
  source_revision_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  station_id TEXT NOT NULL,
  source_lane TEXT NOT NULL CHECK(source_lane='amsc_taf'),
  forecast_revision_kind TEXT NOT NULL,
  taf_modifier TEXT NOT NULL DEFAULT '',
  issue_time_utc TEXT NOT NULL,
  first_system_seen_at TEXT NOT NULL,
  validity_token TEXT NOT NULL DEFAULT '',
  peak_c REAL NOT NULL,
  peak_valid_time_utc TEXT NOT NULL,
  forecast_weather_regime TEXT NOT NULL,
  forecast_peaks_json TEXT NOT NULL,
  raw_sha256 TEXT NOT NULL,
  raw_text TEXT NOT NULL,
  source_report_time TEXT NOT NULL DEFAULT '',
  lineage_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('available','superseded','blocked')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(source_revision_id,event_date),
  UNIQUE(station_id,event_date,raw_sha256,peak_valid_time_utc)
);

CREATE INDEX IF NOT EXISTS idx_amsc_taf_snapshots_station_day
ON amsc_taf_snapshots(station_id,event_date,state,issue_time_utc);

-- Normalized official NWP point snapshots.  The raw provider object remains
-- the source of truth; this table is only the station/date projection used by
-- the read-only UI and by later weak-likelihood materialization.
CREATE TABLE IF NOT EXISTS official_nwp_station_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  loc_key TEXT NOT NULL,
  event_date TEXT NOT NULL,
  run_at_utc TEXT NOT NULL,
  valid_time_utc TEXT NOT NULL,
  temperature_2m_c REAL,
  normalized_features_json TEXT NOT NULL,
  raw_object_ref TEXT NOT NULL,
  raw_sha256 TEXT NOT NULL,
  source_snapshot_sha256 TEXT NOT NULL,
  lineage_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('available','blocked')),
  created_at TEXT NOT NULL,
  UNIQUE(provider_id,station_id,run_at_utc,valid_time_utc,source_snapshot_sha256)
);

CREATE INDEX IF NOT EXISTS idx_official_nwp_station_snapshots_station_day
ON official_nwp_station_snapshots(station_id,event_date,provider_id,valid_time_utc);

-- Daily regime is a weak, forecast-only projection for the read-only city
-- board. It is kept separate from posterior facts and hard METAR/SPECI.
CREATE TABLE IF NOT EXISTS daily_regime_snapshots (
  regime_snapshot_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_revisions_json TEXT NOT NULL,
  regime_json TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  as_of TEXT NOT NULL,
  created_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('available','blocked')),
  UNIQUE(station_id,event_date,source_sha256)
);

CREATE INDEX IF NOT EXISTS idx_daily_regime_snapshots_station_day
ON daily_regime_snapshots(station_id,event_date,as_of,created_at);

CREATE TABLE IF NOT EXISTS reported_trajectory_observation_origins (
  observation_id TEXT PRIMARY KEY,
  source_lane TEXT NOT NULL CHECK(source_lane IN ('amsc','awc')),
  station_id TEXT NOT NULL,
  origin_source_id TEXT NOT NULL,
  origin_country_code TEXT NOT NULL,
  origin_authority TEXT NOT NULL,
  origin_url TEXT NOT NULL,
  origin_transport TEXT NOT NULL,
  direct_country_official INTEGER NOT NULL CHECK(direct_country_official IN (0,1)),
  fallback_used INTEGER NOT NULL CHECK(fallback_used IN (0,1)),
  fallback_reason TEXT NOT NULL,
  preferred_origin_source_id TEXT NOT NULL,
  preferred_origin_authority TEXT NOT NULL,
  official_source_registry_sha256 TEXT NOT NULL,
  origin_json TEXT NOT NULL,
  committed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reported_trajectory_observation_origins_source_time
ON reported_trajectory_observation_origins(origin_source_id,committed_at);

CREATE TABLE IF NOT EXISTS reported_trajectory_station_day_bounds (
  source_lane TEXT NOT NULL CHECK(source_lane IN ('amsc','awc')),
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  reported_max_c INTEGER NOT NULL,
  max_observation_id TEXT NOT NULL,
  max_valid_at TEXT NOT NULL,
  bound_generation INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(source_lane,station_id,event_date)
);

CREATE TABLE IF NOT EXISTS reported_trajectory_station_halts (
  source_lane TEXT NOT NULL CHECK(source_lane IN ('amsc','awc')),
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','cleared')),
  reason_code TEXT NOT NULL,
  triggering_observation_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  cleared_at TEXT,
  PRIMARY KEY(source_lane,station_id,event_date)
);

CREATE TABLE IF NOT EXISTS reported_trajectory_activation_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  generation INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('disabled','enabled','paused')),
  activation_sha256 TEXT NOT NULL,
  config_sha256 TEXT NOT NULL,
  old_bayes_execution_disabled INTEGER NOT NULL CHECK(old_bayes_execution_disabled IN (0,1)),
  awc_amsc_disjoint INTEGER NOT NULL CHECK(awc_amsc_disjoint IN (0,1)),
  activation_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
"""


def weather_basket_yes_schema_status(conn: sqlite3.Connection) -> dict[str, object]:
    """Validate the production schema without acquiring a SQLite write lock.

    Resident services run against a schema installed by the controlled cutover.
    They must not repeat ``CREATE TABLE``/``ALTER TABLE`` while the UI or a
    source writer has a read transaction open: SQLite can report that
    contention as ``database is locked`` and launchd then surfaces only
    ``Bootstrap failed: 5``.
    """

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND (name LIKE 'reported_trajectory_%' "
            "OR name IN ('amsc_taf_snapshots','official_nwp_station_snapshots','daily_regime_snapshots'))"
        )
    }
    missing = sorted(REQUIRED_TABLES - tables)
    activation_columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(reported_trajectory_activation_state)"
        )
    }
    if "activation_json" not in activation_columns and "reported_trajectory_activation_state" in tables:
        missing.append("reported_trajectory_activation_state.activation_json")
    retired_hard_no_tables = sorted({
        "reported_trajectory_no_intents",
        "reported_trajectory_yes_liquidations",
        "reported_trajectory_capital_ledger",
        "reported_trajectory_exit_orders",
    }.intersection(tables))
    return {
        "schema": SCHEMA_VERSION,
        "ok": not missing,
        "missing_tables": missing,
        # Retained SQLite tables are archive-only: this module neither creates
        # nor writes them, so historical records remain readable without a
        # compatibility path for new NO strategy activity.
        "retired_hard_no_tables": retired_hard_no_tables,
        "hard_no_writes_enabled": False,
    }


def apply_weather_basket_yes_schema(conn: sqlite3.Connection) -> dict[str, object]:
    conn.executescript(DDL)
    apply_reported_trajectory_correction_review_schema(conn)
    historical_origins = {
        "amsc": {
            "origin_source_id": "cn_amsc",
            "origin_country_code": "CN",
            "origin_authority": "中国民用航空气象服务网（AMSC）",
            "origin_url": "https://www.amsc.net.cn/",
            "origin_transport": "official_amsc_batch_cccc",
            "direct_country_official": 1,
            "fallback_used": 0,
            "fallback_reason": "",
        },
        "awc": {
            "origin_source_id": "global_noaa_awc_relay",
            "origin_country_code": "ZZ",
            "origin_authority": "NOAA/NWS Aviation Weather Center worldwide relay",
            "origin_url": "https://aviationweather.gov/data/api/",
            "origin_transport": "official_awc_raw_metar_batch_direct",
            "direct_country_official": 0,
            "fallback_used": 1,
            "fallback_reason": "historical_observation_pre_country_router",
        },
    }
    for lane, origin in historical_origins.items():
        conn.execute(
            """INSERT OR IGNORE INTO reported_trajectory_observation_origins(
                 observation_id,source_lane,station_id,origin_source_id,origin_country_code,
                 origin_authority,origin_url,origin_transport,direct_country_official,
                 fallback_used,fallback_reason,preferred_origin_source_id,
                 preferred_origin_authority,official_source_registry_sha256,origin_json,committed_at
               )
               SELECT observation_id,source_lane,station_id,?,?,?,?,?,?,?,?,'','','',?,canonical_committed_at
               FROM reported_trajectory_observations WHERE source_lane=?""",
            (
                origin["origin_source_id"], origin["origin_country_code"], origin["origin_authority"],
                origin["origin_url"], origin["origin_transport"], origin["direct_country_official"],
                origin["fallback_used"], origin["fallback_reason"],
                json.dumps(origin, ensure_ascii=False, sort_keys=True, separators=(",", ":")), lane,
            ),
        )
    activation_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(reported_trajectory_activation_state)")
    }
    if "activation_json" not in activation_columns:
        conn.execute(
            "ALTER TABLE reported_trajectory_activation_state ADD COLUMN activation_json TEXT NOT NULL DEFAULT '{}'"
        )
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'reported_trajectory_%'"
        )
    }
    status = weather_basket_yes_schema_status(conn)
    status["schema"] = SCHEMA_VERSION
    return status


def apply_reported_trajectory_correction_review_schema(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Install only the additive review table without scanning legacy data."""

    try:
        conn.executescript(
            "BEGIN IMMEDIATE;\n"
            + CORRECTION_REVIEW_DDL
            + "\nCOMMIT;"
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='reported_trajectory_station_halt_reviews'"
    ).fetchone()
    triggers = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'reported_trajectory_halt_reviews_no_%'"
        )
    }
    expected = {
        "reported_trajectory_halt_reviews_no_update",
        "reported_trajectory_halt_reviews_no_delete",
    }
    return {
        "schema": "reported_trajectory_correction_review_schema_v1",
        "ok": table is not None and triggers == expected,
        "table_present": table is not None,
        "triggers": sorted(triggers),
        "performs_legacy_backfill": False,
    }


__all__ = [
    "CORRECTION_REVIEW_DDL",
    "DDL",
    "SCHEMA_VERSION",
    "REQUIRED_TABLES",
    "apply_reported_trajectory_correction_review_schema",
    "weather_basket_yes_schema_status",
    "apply_weather_basket_yes_schema",
]
