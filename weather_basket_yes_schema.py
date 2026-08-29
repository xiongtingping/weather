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
    "reported_trajectory_asos_one_minute_observations",
    "reported_trajectory_asos_station_day_coverage",
    "reported_trajectory_madis_hfmetar_objects",
    "reported_trajectory_madis_hfmetar_observations",
    "reported_trajectory_asos_metar_match_facts",
    "reported_trajectory_asos_p1_oos_artifacts",
    "reported_trajectory_asos_argmax_windows",
    "reported_trajectory_asos_argmax_fetch_state",
    "reported_trajectory_asos_argmax_taf_references",
    "reported_trajectory_asos_argmax_lane_state",
    "reported_trajectory_asos_argmax_entry_facts",
    "reported_trajectory_asos_argmax_order_intents",
    "reported_trajectory_asos_argmax_exit_order_intents",
    "reported_trajectory_asos_argmax_rotation_sell_intents",
    "reported_trajectory_canonical_station_day_bounds",
    "reported_trajectory_activation_state",
})

ASOS_OBSERVATION_DDL = """
CREATE TABLE IF NOT EXISTS reported_trajectory_asos_one_minute_observations (
  observation_id TEXT PRIMARY KEY,
  source_lane TEXT NOT NULL CHECK(source_lane='asos_1min'),
  station_id TEXT NOT NULL,
  provider_station_id TEXT NOT NULL,
  valid_at TEXT NOT NULL,
  temperature_c REAL NOT NULL CHECK(temperature_c BETWEEN -100.0 AND 70.0),
  provider_temperature_f REAL NOT NULL,
  measurement_semantics TEXT NOT NULL
    CHECK(measurement_semantics='ASOS_5_MINUTE_TEMPERATURE_PRODUCT_PUBLISHED_EACH_MINUTE_TMPF'),
  provider_id TEXT NOT NULL CHECK(provider_id='IEM_ASOS1MIN'),
  source_class TEXT NOT NULL CHECK(source_class='aggregator_republisher'),
  upstream_authority TEXT NOT NULL CHECK(upstream_authority='NOAA/NCEI ASOS'),
  origin_url TEXT NOT NULL,
  raw_record_sha256 TEXT NOT NULL CHECK(length(raw_record_sha256)=64),
  source_receipt_sha256 TEXT NOT NULL CHECK(length(source_receipt_sha256)=64),
  fetch_started_at TEXT NOT NULL,
  fetch_completed_at TEXT NOT NULL,
  canonical_committed_at TEXT NOT NULL,
  UNIQUE(source_lane,station_id,valid_at,raw_record_sha256)
);

CREATE INDEX IF NOT EXISTS idx_asos_one_minute_station_time
ON reported_trajectory_asos_one_minute_observations(station_id,valid_at);

CREATE TABLE IF NOT EXISTS reported_trajectory_madis_hfmetar_objects (
  compressed_sha256 TEXT PRIMARY KEY CHECK(length(compressed_sha256)=64),
  object_hour_utc TEXT NOT NULL,
  object_url TEXT NOT NULL,
  compressed_bytes INTEGER NOT NULL CHECK(compressed_bytes BETWEEN 1 AND 8388608),
  retained_path TEXT NOT NULL,
  source_lane TEXT NOT NULL CHECK(source_lane='MADIS_HFMETAR_ASOS'),
  source_class TEXT NOT NULL CHECK(source_class='official_republisher'),
  receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(receipt_sha256)=64),
  fetch_started_at_utc TEXT NOT NULL,
  fetch_completed_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reported_trajectory_madis_hfmetar_observations (
  content_sha256 TEXT PRIMARY KEY CHECK(length(content_sha256)=64),
  compressed_sha256 TEXT NOT NULL,
  station_id TEXT NOT NULL,
  provider_station_id TEXT NOT NULL,
  observed_at_utc TEXT NOT NULL,
  temperature_c REAL NOT NULL CHECK(temperature_c BETWEEN -100.0 AND 70.0),
  temperature_qc_summary TEXT NOT NULL,
  temperature_sensor_status INTEGER NOT NULL,
  station_type TEXT NOT NULL CHECK(station_type='HF-METAR'),
  data_provider TEXT NOT NULL CHECK(data_provider='HF-METAR'),
  measurement_semantics TEXT NOT NULL CHECK(
    measurement_semantics='ASOS_RUNNING_5_MINUTE_AVERAGE_DISSEMINATED_EVERY_5_MINUTES'
  ),
  raw_message_sha256 TEXT NOT NULL CHECK(length(raw_message_sha256)=64),
  qc_eligible INTEGER NOT NULL CHECK(qc_eligible IN (0,1)),
  canonical_committed_at_utc TEXT NOT NULL,
  FOREIGN KEY(compressed_sha256)
    REFERENCES reported_trajectory_madis_hfmetar_objects(compressed_sha256),
  UNIQUE(station_id,observed_at_utc,compressed_sha256)
);

CREATE INDEX IF NOT EXISTS idx_madis_hfmetar_station_time
ON reported_trajectory_madis_hfmetar_observations(station_id,observed_at_utc);

CREATE TABLE IF NOT EXISTS reported_trajectory_asos_metar_match_facts (
  content_sha256 TEXT PRIMARY KEY CHECK(length(content_sha256)=64),
  station_id TEXT NOT NULL,
  metar_observation_id TEXT NOT NULL,
  metar_observed_at_utc TEXT NOT NULL,
  match_state TEXT NOT NULL CHECK(match_state IN (
    'EXACT_ONE_MINUTE_MATCH','MADIS_PRIOR_5MIN_APPROXIMATION','ASOS_MATCH_MISSING'
  )),
  fact_json TEXT NOT NULL,
  canonical_committed_at_utc TEXT NOT NULL,
  UNIQUE(station_id,metar_observation_id)
);

CREATE TABLE IF NOT EXISTS reported_trajectory_asos_p1_oos_artifacts (
  content_sha256 TEXT PRIMARY KEY CHECK(length(content_sha256)=64),
  station_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('OOS_READY','BLOCKED')),
  authority_scope TEXT NOT NULL CHECK(authority_scope='PRODUCTION_NO_LIVE_ONLY'),
  train_count INTEGER NOT NULL CHECK(train_count>=0),
  holdout_count INTEGER NOT NULL CHECK(holdout_count>=0),
  artifact_json TEXT NOT NULL,
  canonical_committed_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reported_trajectory_asos_station_day_coverage (
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  day_start_utc TEXT NOT NULL,
  day_end_utc TEXT NOT NULL,
  covered_through_utc TEXT NOT NULL,
  sample_count INTEGER NOT NULL CHECK(sample_count>=0),
  expected_minute_count INTEGER NOT NULL CHECK(expected_minute_count>=1),
  maximum_gap_seconds INTEGER,
  coverage_complete INTEGER NOT NULL CHECK(coverage_complete IN (0,1)),
  reported_max_c REAL,
  max_observation_id TEXT,
  max_valid_at TEXT,
  failure_code TEXT,
  source_receipt_sha256 TEXT NOT NULL CHECK(length(source_receipt_sha256)=64),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(station_id,event_date)
);

CREATE TABLE IF NOT EXISTS reported_trajectory_canonical_station_day_bounds (
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  selected_source_lane TEXT NOT NULL CHECK(selected_source_lane IN ('asos_1min','madis_hfmetar','awc')),
  source_priority INTEGER NOT NULL CHECK(source_priority IN (100,210,220)),
  observed_max_c REAL NOT NULL,
  max_observation_id TEXT NOT NULL,
  max_valid_at TEXT NOT NULL,
  selection_reason TEXT NOT NULL,
  source_receipt_sha256 TEXT NOT NULL CHECK(length(source_receipt_sha256)=64),
  priority_contract_version TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(station_id,event_date)
);

CREATE TABLE IF NOT EXISTS reported_trajectory_asos_argmax_windows (
  window_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  provider_id TEXT NOT NULL CHECK(provider_id='NOAA_NWS_GRIDPOINT_HOURLY'),
  source_class TEXT NOT NULL CHECK(source_class='official_forecast_product'),
  forecast_generated_at_utc TEXT NOT NULL,
  fetched_at_utc TEXT NOT NULL,
  argmax_window_start_utc TEXT NOT NULL,
  argmax_window_end_utc TEXT NOT NULL,
  argmax_temperature_c REAL NOT NULL CHECK(argmax_temperature_c BETWEEN -100.0 AND 70.0),
  argmax_periods_json TEXT NOT NULL,
  points_receipt_sha256 TEXT NOT NULL CHECK(length(points_receipt_sha256)=64),
  forecast_receipt_sha256 TEXT NOT NULL CHECK(length(forecast_receipt_sha256)=64),
  raw_object_ref TEXT NOT NULL,
  coordinate_registry_sha256 TEXT NOT NULL CHECK(length(coordinate_registry_sha256)=64),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  state TEXT NOT NULL CHECK(state='FROZEN'),
  timing_only INTEGER NOT NULL CHECK(timing_only=1),
  provides_temperature_bucket INTEGER NOT NULL CHECK(provides_temperature_bucket=0),
  provides_probability INTEGER NOT NULL CHECK(provides_probability=0),
  enters_p0 INTEGER NOT NULL CHECK(enters_p0=0),
  enters_p0m INTEGER NOT NULL CHECK(enters_p0m=0),
  UNIQUE(station_id,event_date)
);

CREATE INDEX IF NOT EXISTS idx_asos_argmax_scope
ON reported_trajectory_asos_argmax_windows(station_id,event_date,argmax_window_start_utc);

CREATE TRIGGER IF NOT EXISTS reported_trajectory_asos_argmax_windows_no_update
BEFORE UPDATE ON reported_trajectory_asos_argmax_windows
BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER IF NOT EXISTS reported_trajectory_asos_argmax_windows_no_delete
BEFORE DELETE ON reported_trajectory_asos_argmax_windows
BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE IF NOT EXISTS reported_trajectory_asos_argmax_fetch_state (
  station_id TEXT PRIMARY KEY,
  attempted_at_utc TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('AVAILABLE','BLOCKED')),
  reason TEXT,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reported_trajectory_asos_argmax_taf_references (
  reference_id TEXT PRIMARY KEY,
  window_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  source_lane TEXT NOT NULL CHECK(source_lane='awc_taf'),
  source_class TEXT NOT NULL CHECK(source_class='official_forward_native_receipt'),
  evidence_role TEXT NOT NULL CHECK(evidence_role='auxiliary_timing_reference_only'),
  forecast_revision_id TEXT NOT NULL,
  issue_time_utc TEXT NOT NULL,
  local_received_at_utc TEXT NOT NULL,
  raw_taf_sha256 TEXT NOT NULL CHECK(length(raw_taf_sha256)=64),
  tx_c REAL NOT NULL CHECK(tx_c BETWEEN -100.0 AND 70.0),
  tx_valid_time_utc TEXT NOT NULL,
  comparison_state TEXT NOT NULL CHECK(comparison_state IN (
    'CONFIRMS_NWS_ARGMAX','CONFLICTS_WITH_NWS_ARGMAX'
  )),
  modifies_nws_window INTEGER NOT NULL CHECK(modifies_nws_window=0),
  blocks_entry INTEGER NOT NULL CHECK(blocks_entry=0),
  reference_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  created_at_utc TEXT NOT NULL,
  UNIQUE(window_id,forecast_revision_id,tx_valid_time_utc),
  FOREIGN KEY(window_id) REFERENCES reported_trajectory_asos_argmax_windows(window_id)
);

CREATE INDEX IF NOT EXISTS idx_asos_argmax_taf_reference_scope
ON reported_trajectory_asos_argmax_taf_references(station_id,event_date,local_received_at_utc);

CREATE TRIGGER IF NOT EXISTS reported_trajectory_asos_argmax_taf_references_no_update
BEFORE UPDATE ON reported_trajectory_asos_argmax_taf_references
BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER IF NOT EXISTS reported_trajectory_asos_argmax_taf_references_no_delete
BEFORE DELETE ON reported_trajectory_asos_argmax_taf_references
BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE IF NOT EXISTS reported_trajectory_asos_argmax_lane_state (
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  source_lane TEXT NOT NULL CHECK(source_lane IN ('asos_1min','madis_hfmetar')),
  armed_at_utc TEXT NOT NULL,
  running_high_c REAL NOT NULL CHECK(running_high_c BETWEEN -100.0 AND 70.0),
  last_observed_at_utc TEXT NOT NULL,
  last_block_reason TEXT,
  updated_at_utc TEXT NOT NULL,
  PRIMARY KEY(station_id,event_date)
);

CREATE TABLE IF NOT EXISTS reported_trajectory_asos_argmax_entry_facts (
  entry_fact_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  source_lane TEXT NOT NULL CHECK(source_lane IN ('asos_1min','madis_hfmetar')),
  observation_id TEXT NOT NULL,
  observed_at_utc TEXT NOT NULL,
  received_at_utc TEXT NOT NULL,
  prior_high_c REAL NOT NULL,
  new_high_c REAL NOT NULL CHECK(new_high_c>prior_high_c),
  raw_sha256 TEXT NOT NULL CHECK(length(raw_sha256)=64),
  source_receipt_sha256 TEXT NOT NULL CHECK(length(source_receipt_sha256)=64),
  window_id TEXT NOT NULL,
  argmax_window_start_utc TEXT NOT NULL,
  market_slug TEXT NOT NULL,
  outcome TEXT NOT NULL,
  yes_token TEXT NOT NULL,
  settlement_scalar INTEGER NOT NULL,
  settlement_unit TEXT NOT NULL CHECK(settlement_unit IN ('C','F')),
  reason TEXT NOT NULL CHECK(reason='ASOS_NEW_HIGH_RECEIVED_DURING_ARGMAX_PERIOD'),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  gateway_submitted INTEGER NOT NULL CHECK(gateway_submitted=0),
  created_at_utc TEXT NOT NULL,
  UNIQUE(station_id,event_date,source_lane,observation_id),
  FOREIGN KEY(window_id) REFERENCES reported_trajectory_asos_argmax_windows(window_id)
);

CREATE TRIGGER IF NOT EXISTS reported_trajectory_asos_argmax_entry_facts_no_update
BEFORE UPDATE ON reported_trajectory_asos_argmax_entry_facts
BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER IF NOT EXISTS reported_trajectory_asos_argmax_entry_facts_no_delete
BEFORE DELETE ON reported_trajectory_asos_argmax_entry_facts
BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE IF NOT EXISTS reported_trajectory_asos_argmax_order_intents (
  order_intent_id TEXT PRIMARY KEY,
  entry_fact_id TEXT NOT NULL UNIQUE,
  strategy_id TEXT NOT NULL CHECK(strategy_id='asos_argmax_new_high_yes_v1'),
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  yes_token TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side='BUY'),
  order_type TEXT NOT NULL CHECK(order_type='FOK'),
  limit_price REAL NOT NULL CHECK(limit_price>0 AND limit_price<1),
  size REAL NOT NULL CHECK(size>0),
  requested_notional REAL NOT NULL CHECK(requested_notional>0 AND requested_notional<=2.0),
  idempotency_key TEXT NOT NULL UNIQUE,
  ws_snapshot_sha256 TEXT NOT NULL CHECK(length(ws_snapshot_sha256)=64),
  guard_snapshot_sha256 TEXT NOT NULL CHECK(length(guard_snapshot_sha256)=64),
  guard_snapshot_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN (
    'PREPARED','SUBMITTING','EXCHANGE_ACCEPTED','RELEASED','RECONCILE_REQUIRED'
  )),
  gateway_request_json TEXT NOT NULL,
  gateway_response_json TEXT NOT NULL,
  exchange_order_id TEXT,
  size_matched REAL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  FOREIGN KEY(entry_fact_id)
    REFERENCES reported_trajectory_asos_argmax_entry_facts(entry_fact_id)
);

CREATE INDEX IF NOT EXISTS idx_asos_argmax_intent_budget
ON reported_trajectory_asos_argmax_order_intents(station_id,event_date,state);

CREATE TABLE IF NOT EXISTS reported_trajectory_asos_argmax_exit_order_intents (
  exit_order_intent_id TEXT PRIMARY KEY,
  buy_order_intent_id TEXT NOT NULL UNIQUE,
  entry_fact_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL CHECK(strategy_id='asos_argmax_yes_exit_099_v1'),
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  yes_token TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side='SELL'),
  order_type TEXT NOT NULL CHECK(order_type='GTC'),
  limit_price REAL NOT NULL CHECK(limit_price=0.99),
  size REAL NOT NULL CHECK(size>0),
  source_buy_size_matched REAL NOT NULL CHECK(source_buy_size_matched>0),
  idempotency_key TEXT NOT NULL UNIQUE,
  guard_snapshot_sha256 TEXT,
  guard_snapshot_json TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL CHECK(state IN (
    'PREPARED','SUBMITTING','EXCHANGE_ACCEPTED','RELEASED','RECONCILE_REQUIRED'
  )),
  gateway_request_json TEXT NOT NULL DEFAULT '{}',
  gateway_response_json TEXT NOT NULL DEFAULT '{}',
  exchange_order_id TEXT,
  size_matched REAL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  FOREIGN KEY(buy_order_intent_id)
    REFERENCES reported_trajectory_asos_argmax_order_intents(order_intent_id),
  FOREIGN KEY(entry_fact_id)
    REFERENCES reported_trajectory_asos_argmax_entry_facts(entry_fact_id),
  CHECK(size=source_buy_size_matched),
  CHECK(guard_snapshot_sha256 IS NULL OR length(guard_snapshot_sha256)=64)
);

CREATE INDEX IF NOT EXISTS idx_asos_argmax_exit_state
ON reported_trajectory_asos_argmax_exit_order_intents(state,updated_at_utc);

CREATE TABLE IF NOT EXISTS reported_trajectory_asos_argmax_rotation_sell_intents (
  rotation_sell_intent_id TEXT PRIMARY KEY,
  target_entry_fact_id TEXT NOT NULL,
  source_buy_order_intent_id TEXT NOT NULL,
  source_exit_order_intent_id TEXT,
  strategy_id TEXT NOT NULL CHECK(strategy_id='asos_argmax_rotation_sell_fak_v1'),
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  old_yes_token TEXT NOT NULL,
  new_yes_token TEXT NOT NULL CHECK(new_yes_token<>old_yes_token),
  side TEXT NOT NULL CHECK(side='SELL'),
  order_type TEXT NOT NULL CHECK(order_type='FAK'),
  limit_price REAL CHECK(limit_price IS NULL OR (limit_price>0 AND limit_price<1)),
  size REAL CHECK(size IS NULL OR size>0),
  ws_snapshot_sha256 TEXT,
  guard_snapshot_sha256 TEXT,
  state TEXT NOT NULL CHECK(state IN (
    'PREPARED','NO_POSITION','SUBMITTING','FILLED','RELEASED','RECONCILE_REQUIRED'
  )),
  idempotency_key TEXT NOT NULL UNIQUE,
  cancellation_receipt_json TEXT NOT NULL DEFAULT '{}',
  ws_snapshot_json TEXT NOT NULL DEFAULT '{}',
  guard_snapshot_json TEXT NOT NULL DEFAULT '{}',
  gateway_request_json TEXT NOT NULL DEFAULT '{}',
  gateway_response_json TEXT NOT NULL DEFAULT '{}',
  exchange_order_id TEXT,
  shares_matched REAL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  UNIQUE(target_entry_fact_id,source_buy_order_intent_id),
  FOREIGN KEY(target_entry_fact_id)
    REFERENCES reported_trajectory_asos_argmax_entry_facts(entry_fact_id),
  FOREIGN KEY(source_buy_order_intent_id)
    REFERENCES reported_trajectory_asos_argmax_order_intents(order_intent_id),
  FOREIGN KEY(source_exit_order_intent_id)
    REFERENCES reported_trajectory_asos_argmax_exit_order_intents(exit_order_intent_id),
  CHECK(ws_snapshot_sha256 IS NULL OR length(ws_snapshot_sha256)=64),
  CHECK(guard_snapshot_sha256 IS NULL OR length(guard_snapshot_sha256)=64)
);

CREATE INDEX IF NOT EXISTS idx_asos_argmax_rotation_target
ON reported_trajectory_asos_argmax_rotation_sell_intents(target_entry_fact_id,state);
"""

CORRECTION_REVIEW_DDL = """
CREATE TABLE IF NOT EXISTS reported_trajectory_station_halt_reviews (
  review_id TEXT PRIMARY KEY,
  source_lane TEXT NOT NULL CHECK(source_lane IN ('amsc','awc')),
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  triggering_observation_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(
    decision IN (
      'CLEAR_REFERENCE_ONLY',
      'CLEAR_WITH_REPLACEMENT',
      'QUARANTINE_STATION_DAY'
    )
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
  -- amsc/awc are the formal report lanes; asos_1min/madis_hfmetar carry the
  -- SOFT_PATH intra-hour readings (2026-08-29 soft-in-hard-out ruling) with
  -- their exact report-type pairing enforced in the reader and compiler.
  source_lane TEXT NOT NULL
    CHECK(source_lane IN ('amsc','awc','asos_1min','madis_hfmetar')),
  station_id TEXT NOT NULL,
  report_type TEXT NOT NULL
    CHECK(report_type IN ('METAR','SPECI','ASOS_HF','ASOS_1MIN')),
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
  source_lane TEXT NOT NULL
    CHECK(source_lane IN ('amsc','awc','asos_1min','madis_hfmetar')),
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
    entry_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='reported_trajectory_asos_argmax_entry_facts'"
    ).fetchone()
    entry_sql = str(entry_sql_row[0] or "") if entry_sql_row else ""
    if "ASOS_NEW_HIGH_RECEIVED_BEFORE_ARGMAX_WINDOW" in entry_sql:
        entry_count = int(conn.execute(
            "SELECT COUNT(*) FROM reported_trajectory_asos_argmax_entry_facts"
        ).fetchone()[0])
        intent_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='reported_trajectory_asos_argmax_order_intents'"
        ).fetchone()
        intent_count = int(conn.execute(
            "SELECT COUNT(*) FROM reported_trajectory_asos_argmax_order_intents"
        ).fetchone()[0]) if intent_table else 0
        if entry_count or intent_count:
            raise sqlite3.IntegrityError(
                "argmax_period_contract_migration_nonempty_fail_closed"
            )
        # The superseded table is empty by contract. Rebuild it instead of
        # retaining a misleading CHECK value or silently rewriting facts.
        conn.executescript(
            """BEGIN IMMEDIATE;
            DROP TABLE IF EXISTS reported_trajectory_asos_argmax_order_intents;
            DROP TRIGGER IF EXISTS reported_trajectory_asos_argmax_entry_facts_no_update;
            DROP TRIGGER IF EXISTS reported_trajectory_asos_argmax_entry_facts_no_delete;
            DROP TABLE reported_trajectory_asos_argmax_entry_facts;
            COMMIT;"""
        )
    conn.executescript(DDL)
    conn.executescript(ASOS_OBSERVATION_DDL)
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
