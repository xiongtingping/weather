"""Unified SSOT schema for the Bayes × basket target lane.

The target lane uses ``apply_target_ssot_schema`` only from a
controlled migration only; the resident service merely checks the tables and
never installs DDL while it is running.  Artifact and evidence rows are
append-only, and the execution path has no order/gateway table in this schema.
"""

from __future__ import annotations

import json
import re
import sqlite3

from .canonical_ssot_schema import (
    canonical_state_at_least,
    inspect_canonical_ssot_schema,
)
from .contracts import content_sha256
from .probability_evidence_store import (
    DDL as PROBABILITY_EVIDENCE_DDL,
    TABLES as PROBABILITY_EVIDENCE_TABLES,
    probability_evidence_schema_status,
)


def _execute_sql_script_transactional(conn: sqlite3.Connection, script: str) -> None:
    """Execute a SQLite script without ``executescript``'s implicit commits."""

    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                try:
                    conn.execute(sql)
                except sqlite3.Error as exc:
                    # Name the statement.  A bare "no such column: cycle_id"
                    # against a script of several hundred statements says
                    # nothing about which one, and the DDL constants are large
                    # enough that finding it by hand means bisecting them.
                    raise type(exc)(
                        f"{exc} [statement: {' '.join(sql.split())[:200]}]"
                    ) from exc
            statement = ""
    if statement.strip():
        raise RuntimeError("sqlite_script_incomplete_statement")


# Words that can appear inside an index's column list without being a column.
_INDEX_SYNTAX_WORDS = frozenset({
    "ASC", "DESC", "COLLATE", "NOCASE", "BINARY", "RTRIM", "WHERE",
    "AND", "OR", "NOT", "NULL", "IS", "IN",
})


def _execute_index_script_where_table_exists(
    conn: sqlite3.Connection, script: str
) -> list[str]:
    """Create each index whose table this path has, in the shape it needs.

    Two ordinary reasons to skip, both of which mean "not yet", not "wrong":

    The table belongs to a different migration path.  The deferred block
    carries indexes for tables owned by more than one, so an absent table is
    expected.

    The table is here but still in its old shape.  ``CREATE TABLE IF NOT
    EXISTS`` leaves a pre-existing table alone, so on an upgrade the columns
    an index needs may not arrive until the ``_upgrade_*`` passes further
    down run.  A v3 image died here on
    ``no such column: cycle_id``: the v4 shape of
    ``basket_policy_training_fact`` is installed by
    ``_upgrade_empty_policy_training_fact_schema``, thirty-nine lines after
    the DDL that indexes it.  Calling this again after those passes is what
    makes the skip temporary rather than a silent loss.
    """

    created: list[str] = []
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip()
        statement = ""
        if not sql:
            continue
        match = re.search(
            r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
            r"([A-Za-z_0-9]+)\s+ON\s+([A-Za-z_0-9]+)",
            sql,
            re.IGNORECASE,
        )
        if match is None:
            raise RuntimeError("deferred_index_script_non_index_statement")
        table = match.group(2)
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if present is None:
            continue
        columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        indexed = {
            token
            for token in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", sql[match.end():])
            if token.upper() not in _INDEX_SYNTAX_WORDS
        }
        if not indexed.issubset(columns):
            continue
        conn.execute(sql)
        created.append(match.group(1))
    if statement.strip():
        raise RuntimeError("sqlite_script_incomplete_statement")
    return created

SCHEMA_VERSION = "bayes_basket_target_ssot_v14_canonical_p0_chain"

# A valid physical schema is intentionally weaker than a release decision.
# PR04 owns the only current-pointer publication; neither this installer nor
# its read-only status is authority to start a service, consume a basket, or
# promote a V14 P0 chain to formal/live.
CANONICAL_V14_RELEASE_STATE = "MATERIALIZED_UNPUBLISHED"

CANONICAL_FACT_GUARD_DDL = """
CREATE TRIGGER IF NOT EXISTS canonical_ssot_fact_no_update
BEFORE UPDATE ON canonical_ssot_fact
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS canonical_ssot_fact_no_delete
BEFORE DELETE ON canonical_ssot_fact
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS canonical_ssot_fact_ingress_only
BEFORE INSERT ON canonical_ssot_fact
WHEN canonical_ingress_authorized()!=1
BEGIN SELECT RAISE(ABORT,'canonical_ingress_only'); END;
"""

DDL = """
PRAGMA foreign_keys = ON;

-- One append-only storage surface for the canonical model/truth/residual/
-- activation contract.  Existing calibration_* tables remain projections for
-- legacy readers, but new facts must first pass the canonical ingress and
-- retain the exact JSON/hash/lineage that was validated there.
CREATE TABLE IF NOT EXISTS canonical_ssot_fact (
  fact_id TEXT PRIMARY KEY,
  fact_type TEXT NOT NULL CHECK(fact_type IN ('model','truth','residual','activation','activation_supersession','ensemble_member','native_arrival','ensemble_member_arrival_binding','ensemble_member_receipt_manifest','republisher_oos_artifact','republisher_cross_selection','republisher_no_live_activation','forward_applicability','probability_distribution_artifact','p0_snapshot','market_bucket_bridge','truth_observation_revision','truth_observation_day_bounds','model_oos_event','model_oos_cohort','p0_model_family_selection','p0_calibrated_distribution','p0_activation_root','p0_forecast_snapshot')),
  scope_key TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  fact_json TEXT NOT NULL,
  lineage_json TEXT NOT NULL,
  ingested_at_utc TEXT NOT NULL,
  UNIQUE(fact_type, content_sha256),
  UNIQUE(fact_type, scope_key)
);

CREATE TABLE IF NOT EXISTS calibration_station_registry (
  station_id TEXT PRIMARY KEY,
  icao_code TEXT NOT NULL UNIQUE,
  city_label TEXT NOT NULL,
  timezone_name TEXT NOT NULL,
  source_lane TEXT NOT NULL CHECK(source_lane IN ('AMSC','AWC')),
  settlement_definition TEXT NOT NULL,
  registry_version TEXT NOT NULL,
  source_role TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)
);

-- The station registry is an immutable historical identity record.  A source
-- authority correction must therefore be appended as a separately signed
-- projection, not applied as an UPDATE to an older AMSC row.  The only
-- current production lane is global AWC; retained AMSC records are history
-- and cannot be selected by this table.
CREATE TABLE IF NOT EXISTS calibration_station_source_authority (
  authority_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  source_lane TEXT NOT NULL CHECK(source_lane='AWC'),
  authority_version TEXT NOT NULL,
  activated_at_utc TEXT NOT NULL,
  legacy_registry_content_sha256 TEXT NOT NULL CHECK(length(legacy_registry_content_sha256)=64),
  lineage_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  UNIQUE(station_id,authority_version)
);

CREATE TABLE IF NOT EXISTS calibration_station_market_timezone_binding (
  binding_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL UNIQUE
    REFERENCES calibration_station_registry(station_id),
  settlement_timezone TEXT NOT NULL,
  market_registry_timezone_count INTEGER NOT NULL
    CHECK(market_registry_timezone_count=1),
  historical_truth_evidence_sha256 TEXT
    CHECK(historical_truth_evidence_sha256 IS NULL OR
      length(historical_truth_evidence_sha256)=64),
  binding_version TEXT NOT NULL,
  bound_at_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)
);

CREATE TABLE IF NOT EXISTS calibration_station_daily_high_truth (
  truth_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  local_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  official_tmax_c REAL NOT NULL,
  source_lane TEXT NOT NULL CHECK(source_lane IN ('AMSC','AWC')),
  settlement_definition TEXT NOT NULL,
  revision_no INTEGER NOT NULL CHECK(revision_no>=0),
  published_at_utc TEXT,
  received_at_utc TEXT,
  canonical_committed_at_utc TEXT,
  timestamps_complete INTEGER NOT NULL CHECK(timestamps_complete IN (0,1)),
  qc_status TEXT NOT NULL CHECK(qc_status IN ('ACCEPTED','QUARANTINED')),
  source_role TEXT NOT NULL,
  source_payload_sha256 TEXT NOT NULL CHECK(length(source_payload_sha256)=64),
  observation_ids_json TEXT NOT NULL,
  authority_scope TEXT NOT NULL CHECK(authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE')),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  UNIQUE(station_id,local_date,settlement_definition,revision_no)
);

CREATE TABLE IF NOT EXISTS calibration_historical_asof_model_run (
  model_run_id TEXT PRIMARY KEY,
  model_id TEXT NOT NULL CHECK(model_id IN ('ECMWF','GFS','ICON','GEM','CMA')),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  target_local_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  run_init_utc TEXT NOT NULL,
  published_at_utc TEXT,
  received_at_utc TEXT,
  timestamps_complete INTEGER NOT NULL CHECK(timestamps_complete IN (0,1)),
  lead_bucket TEXT NOT NULL,
  maturity_key TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  raw_tmax_c REAL NOT NULL,
  settlement_definition TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  provider_role TEXT NOT NULL,
  source_class TEXT NOT NULL,
  aggregator_or_republisher INTEGER NOT NULL CHECK(aggregator_or_republisher IN (0,1)),
  open_meteo INTEGER NOT NULL CHECK(open_meteo IN (0,1)),
  source_url TEXT NOT NULL,
  raw_object_ref TEXT NOT NULL,
  asof_status TEXT NOT NULL,
  capture_modes_json TEXT NOT NULL,
  historical_replay_eligible INTEGER NOT NULL CHECK(historical_replay_eligible IN (0,1)),
  prospective_strict_asof_eligible INTEGER NOT NULL CHECK(prospective_strict_asof_eligible IN (0,1)),
  authority_scope TEXT NOT NULL CHECK(authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE')),
  source_object_sha256 TEXT NOT NULL CHECK(length(source_object_sha256)=64),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  CHECK(authority_scope<>'FORMAL_NATIVE_ELIGIBLE' OR
    (timestamps_complete=1 AND aggregator_or_republisher=0 AND published_at_utc IS NOT NULL AND received_at_utc IS NOT NULL)),
  CHECK(aggregator_or_republisher=0 OR authority_scope='NO_LIVE_REPLAY_CANDIDATE'),
  UNIQUE(model_id,station_id,target_local_date,run_init_utc,run_cycle)
);

CREATE TABLE IF NOT EXISTS calibration_model_station_residual (
  residual_id TEXT PRIMARY KEY,
  truth_id TEXT NOT NULL REFERENCES calibration_station_daily_high_truth(truth_id),
  model_run_id TEXT NOT NULL REFERENCES calibration_historical_asof_model_run(model_run_id),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  model_id TEXT NOT NULL CHECK(model_id IN ('ECMWF','GFS','ICON','GEM','CMA')),
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  maturity_key TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  e_y_minus_f_raw_c REAL NOT NULL,
  paired_asof_utc TEXT NOT NULL,
  qc_status TEXT NOT NULL CHECK(qc_status IN ('ACCEPTED','QUARANTINED')),
  historical_replay_eligible INTEGER NOT NULL CHECK(historical_replay_eligible IN (0,1)),
  prospective_strict_asof_eligible INTEGER NOT NULL CHECK(prospective_strict_asof_eligible IN (0,1)),
  authority_scope TEXT NOT NULL CHECK(authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE')),
  pair_sha256 TEXT NOT NULL CHECK(length(pair_sha256)=64),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  UNIQUE(truth_id,model_run_id)
);

CREATE TABLE IF NOT EXISTS calibration_model_artifact (
  calibration_artifact_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  model_id TEXT NOT NULL CHECK(model_id IN ('ECMWF','GFS','ICON','GEM','CMA')),
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  settlement_definition TEXT NOT NULL,
  trained_through_utc TEXT NOT NULL,
  sample_n INTEGER NOT NULL CHECK(sample_n>=0),
  effective_sample_n REAL NOT NULL CHECK(effective_sample_n>=0),
  bias_c REAL NOT NULL,
  residual_scale_c REAL NOT NULL CHECK(residual_scale_c>0),
  tail_parameters_json TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  UNIQUE(station_id,model_id,season_bin,lead_bucket,run_cycle,content_sha256)
);

CREATE TABLE IF NOT EXISTS calibration_ensemble_artifact (
  ensemble_artifact_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  settlement_definition TEXT NOT NULL,
  candidate_models_json TEXT NOT NULL,
  supported_masks_json TEXT NOT NULL,
  residual_covariance_json TEXT NOT NULL,
  correlation_penalty REAL NOT NULL CHECK(correlation_penalty>=0),
  selection_evidence_sha256 TEXT CHECK(selection_evidence_sha256 IS NULL OR length(selection_evidence_sha256)=64),
  previous_ensemble_artifact_sha256 TEXT CHECK(previous_ensemble_artifact_sha256 IS NULL OR length(previous_ensemble_artifact_sha256)=64),
  weight_method TEXT,
  effective_sample_n REAL CHECK(effective_sample_n IS NULL OR effective_sample_n>0),
  max_weight_delta REAL CHECK(max_weight_delta IS NULL OR max_weight_delta>=0),
  train_start_date TEXT,
  train_end_date TEXT,
  recent_start_date TEXT,
  recent_end_date TEXT,
  holdout_start_date TEXT,
  holdout_end_date TEXT,
  oos_metrics_json TEXT,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)
);

CREATE TABLE IF NOT EXISTS calibration_ensemble_selection_evidence (
  selection_evidence_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  settlement_definition TEXT NOT NULL,
  method_version TEXT NOT NULL,
  train_start_date TEXT NOT NULL,
  train_end_date TEXT NOT NULL,
  recent_start_date TEXT NOT NULL,
  recent_end_date TEXT NOT NULL,
  holdout_start_date TEXT NOT NULL,
  holdout_end_date TEXT NOT NULL,
  candidate_scores_json TEXT NOT NULL,
  selected_models_json TEXT NOT NULL,
  selected_weights_json TEXT NOT NULL,
  oos_metrics_json TEXT NOT NULL,
  residual_covariance_json TEXT NOT NULL,
  residual_correlation_json TEXT NOT NULL,
  effective_sample_n REAL NOT NULL CHECK(effective_sample_n>0),
  previous_ensemble_artifact_sha256 TEXT CHECK(previous_ensemble_artifact_sha256 IS NULL OR length(previous_ensemble_artifact_sha256)=64),
  max_weight_delta REAL NOT NULL CHECK(max_weight_delta>=0),
  actual_max_weight_delta REAL NOT NULL CHECK(actual_max_weight_delta>=0 AND actual_max_weight_delta<=max_weight_delta),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64)
);

-- Long-term P0 authority.  These tables deliberately do not reuse the legacy
-- calibration_ensemble_* namespace: that namespace combines deterministic
-- model distributions, while this namespace preserves native members first.
CREATE TABLE IF NOT EXISTS calibration_native_ensemble_system (
  ensemble_system_id TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL UNIQUE,
  independence_family_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  source_class TEXT NOT NULL CHECK(source_class='NATIVE_ENSEMBLE_PROVIDER'),
  aggregator_or_republisher INTEGER NOT NULL DEFAULT 0
    CHECK(aggregator_or_republisher=0),
  expected_member_count INTEGER NOT NULL CHECK(expected_member_count>=1),
  member_identity_contract_version TEXT NOT NULL,
  publication_contract_version TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64)
);

CREATE TABLE IF NOT EXISTS calibration_native_ensemble_member_run (
  member_run_id TEXT PRIMARY KEY,
  ensemble_system_id TEXT NOT NULL
    REFERENCES calibration_native_ensemble_system(ensemble_system_id),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  target_local_date TEXT NOT NULL,
  run_init_utc TEXT,
  member_id TEXT NOT NULL,
  member_role TEXT NOT NULL CHECK(member_role IN ('CONTROL','PERTURBED')),
  published_at_utc TEXT,
  received_at_utc TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  raw_tmax_c REAL NOT NULL,
  settlement_definition TEXT NOT NULL,
  extraction_version TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  transport_provider_id TEXT NOT NULL,
  native_timestamps_complete INTEGER NOT NULL
    CHECK(native_timestamps_complete IN (0,1)),
  aggregator_snapshot_at_utc TEXT NOT NULL,
  source_url TEXT NOT NULL,
  raw_object_ref TEXT NOT NULL,
  source_object_sha256 TEXT NOT NULL CHECK(length(source_object_sha256)=64),
  qc_status TEXT NOT NULL CHECK(qc_status IN ('ACCEPTED','QUARANTINED')),
  authority_scope TEXT NOT NULL
    CHECK(authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','PRODUCTION_NO_LIVE_CANDIDATE')),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  CHECK(
    authority_scope<>'FORMAL_NATIVE_ELIGIBLE'
    OR (
      native_timestamps_complete=1
      AND run_init_utc IS NOT NULL
      AND published_at_utc IS NOT NULL
    )
  ),
  UNIQUE(
    ensemble_system_id,station_id,target_local_date,run_init_utc,
    member_id,extraction_version
  )
);

CREATE TABLE IF NOT EXISTS calibration_native_ensemble_member_bucket (
  member_bucket_id TEXT PRIMARY KEY,
  ensemble_system_id TEXT NOT NULL
    REFERENCES calibration_native_ensemble_system(ensemble_system_id),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  target_local_date TEXT NOT NULL,
  aggregator_snapshot_at_utc TEXT NOT NULL,
  member_set_sha256 TEXT NOT NULL CHECK(length(member_set_sha256)=64),
  member_count INTEGER NOT NULL CHECK(member_count>=2),
  bucket_contract_sha256 TEXT NOT NULL CHECK(length(bucket_contract_sha256)=64),
  bucket_intervals_json TEXT NOT NULL,
  raw_bucket_probabilities_json TEXT NOT NULL,
  source_role TEXT NOT NULL CHECK(source_role='aggregator_republisher'),
  authority_scope TEXT NOT NULL CHECK(
    authority_scope='PRODUCTION_NO_LIVE_CANDIDATE'
  ),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(
    ensemble_system_id,station_id,target_local_date,
    member_set_sha256,bucket_contract_sha256
  )
);

CREATE TABLE IF NOT EXISTS calibration_native_ensemble_historical_member_run (
  historical_member_run_id TEXT PRIMARY KEY,
  ensemble_system_id TEXT NOT NULL
    REFERENCES calibration_native_ensemble_system(ensemble_system_id),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  target_local_date TEXT NOT NULL,
  -- Hours ahead of the station's own local day (charter 47).  The calendar
  -- labels stay admissible, and the claim that used to stand here -- that
  -- nothing writes them any more -- was false for this table.  Measured
  -- 2026-08-26: METAR_PHYSICAL_PATH_TMAX appended d0 at 11:21:15Z and
  -- OFFICIAL_AIRPORT_TAF_TX appended d1 at 05:09:34Z, minutes before the
  -- check.  Only d2 is genuinely frozen (last written 2026-08-13).
  --
  -- For those two systems the token is not a lead at all: they are bucket-vote
  -- bystanders carrying an issue time rather than a run time, so their d0
  -- means "no lead applies" -- a third meaning stacked on the day offset and
  -- the hour bucket this one column already holds.  Read the column, never the
  -- name: filtering d1 returns rows that span three hour buckets, and
  -- filtering an hour bucket omits these two systems entirely.
  archive_lead_bucket TEXT NOT NULL CHECK(
    archive_lead_bucket IN (
      'h00_12','h12_24','h24_48','h48_plus','d0','d1','d2'
    )
  ),
  run_cycle TEXT NOT NULL CHECK(
    run_cycle IN (
      'AGGREGATOR_UNKNOWN_NATIVE_CYCLE','00Z','06Z','12Z','18Z'
    )
  ),
  member_id TEXT NOT NULL,
  member_role TEXT NOT NULL CHECK(member_role IN ('CONTROL','PERTURBED')),
  raw_tmax_c REAL NOT NULL,
  settlement_definition TEXT NOT NULL,
  extraction_version TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  -- RISH_KYOTO_JMA_ARCHIVE ruled in 2026-08-28: the JMA MSM lane through
  -- the Kyoto RISH mirror, byte-range reads of the native GRIB archive.
  -- The capture lane existed for weeks with its rows refused at INSERT.
  -- NOAA_HRRR_PDS_S3 and NOAA_NBM_PDS_S3 ruled in the same day, from the
  -- same 46-pair sweep: idx-sidecar byte-range reads of the NOAA open
  -- data buckets, one complete GRIB2 message per field.
  -- DWD_OPENDATA_ICON_EU the same day and sweep: whole-domain fields,
  -- one bz2 file per hour shared by every station, on the same
  -- no-history footing the ICON EPS transport already holds.
  -- A CHECK cannot be altered in place; the live table takes the widening
  -- via scripts/migrate_member_run_transport_allowlist.py.  Keep comments
  -- OUT of the IN(...) list itself: the capture pre-flight parses the live
  -- DDL for the allowlist, and a comment inside the parentheses reads as
  -- phantom members and truncates at the first ')' it happens to contain.
  transport_provider_id TEXT NOT NULL CHECK(
    transport_provider_id IN (
      'OPEN_METEO','NOAA_GEFS_PDS_S3','ECMWF_OPEN_DATA_S3',
      'ECCC_DD_WEATHER','DWD_OPENDATA_ICON_EPS','AWC_TAF_DATA_API',
      'AWC_METAR_PHYSICAL_PATH','RISH_KYOTO_JMA_ARCHIVE',
      'NOAA_HRRR_PDS_S3','NOAA_NBM_PDS_S3','DWD_OPENDATA_ICON_EU'
    )
  ),
  native_run_init_utc TEXT,
  offline_oos_eligible INTEGER NOT NULL DEFAULT 0
    CHECK(offline_oos_eligible IN (0,1)),
  archive_query_at_utc TEXT NOT NULL,
  source_url TEXT NOT NULL,
  raw_object_ref TEXT NOT NULL,
  source_object_sha256 TEXT NOT NULL CHECK(length(source_object_sha256)=64),
  qc_status TEXT NOT NULL CHECK(qc_status IN ('ACCEPTED','QUARANTINED')),
  authority_scope TEXT NOT NULL CHECK(authority_scope='CALIBRATION_TRAIN_ONLY'),
  holdout_eligible INTEGER NOT NULL CHECK(holdout_eligible=0),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  -- The OOS-eligibility list is narrower than the storage list on
  -- purpose: only a transport whose bytes can be re-fetched by range from
  -- the original archive may score in offline OOS.  RISH_KYOTO_JMA_ARCHIVE
  -- qualifies on exactly that ground (native GRIB, byte-range reads of the
  -- Kyoto mirror, run init carried) -- ruled in with the storage widening,
  -- 2026-08-28, after the first capture cleared the storage CHECK and
  -- failed here with the run already fetched.  The two NOAA S3 buckets
  -- qualify on the same ground and more so: the idx sidecar IS a byte
  -- range contract, published by the archive itself.
  CHECK(
    offline_oos_eligible=0
    OR (
      transport_provider_id IN (
        'NOAA_GEFS_PDS_S3','ECMWF_OPEN_DATA_S3',
        'ECCC_DD_WEATHER','DWD_OPENDATA_ICON_EPS','AWC_TAF_DATA_API',
        'AWC_METAR_PHYSICAL_PATH','RISH_KYOTO_JMA_ARCHIVE',
        'NOAA_HRRR_PDS_S3','NOAA_NBM_PDS_S3','DWD_OPENDATA_ICON_EU'
      )
      AND native_run_init_utc IS NOT NULL
      AND run_cycle!='AGGREGATOR_UNKNOWN_NATIVE_CYCLE'
    )
  ),
  UNIQUE(
    ensemble_system_id,station_id,target_local_date,
    archive_lead_bucket,member_id,extraction_version
  )
);

CREATE TABLE IF NOT EXISTS calibration_native_ensemble_historical_member_bucket (
  historical_member_bucket_id TEXT PRIMARY KEY,
  ensemble_system_id TEXT NOT NULL
    REFERENCES calibration_native_ensemble_system(ensemble_system_id),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  target_local_date TEXT NOT NULL,
  -- Hours ahead of the station's own local day (charter 47).  The calendar
  -- labels stay admissible so rows ingested before the vocabulary switch
  -- remain valid; nothing writes them to THIS table any more -- verified
  -- 2026-08-26, every d0/d1/d2 row here was last touched 2026-08-13.  The
  -- sibling member_run table is different: two systems still append calendar
  -- labels there daily, so do not carry this claim across.
  archive_lead_bucket TEXT NOT NULL CHECK(
    archive_lead_bucket IN (
      'h00_12','h12_24','h24_48','h48_plus','d0','d1','d2'
    )
  ),
  archive_query_at_utc TEXT NOT NULL,
  member_set_sha256 TEXT NOT NULL CHECK(length(member_set_sha256)=64),
  member_count INTEGER NOT NULL CHECK(member_count>=2),
  bucket_contract_sha256 TEXT NOT NULL CHECK(length(bucket_contract_sha256)=64),
  bucket_intervals_json TEXT NOT NULL,
  raw_bucket_probabilities_json TEXT NOT NULL,
  source_role TEXT NOT NULL CHECK(
    source_role IN ('aggregator_republisher','official_native_archive')
  ),
  authority_scope TEXT NOT NULL CHECK(authority_scope='CALIBRATION_TRAIN_ONLY'),
  holdout_eligible INTEGER NOT NULL CHECK(holdout_eligible=0),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(
    ensemble_system_id,station_id,target_local_date,
    archive_lead_bucket,member_set_sha256,bucket_contract_sha256
  )
);

CREATE TABLE IF NOT EXISTS calibration_native_ensemble_system_artifact (
  system_artifact_id TEXT PRIMARY KEY,
  ensemble_system_id TEXT NOT NULL
    REFERENCES calibration_native_ensemble_system(ensemble_system_id),
  station_id TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  settlement_definition TEXT NOT NULL,
  calibration_method TEXT NOT NULL,
  bias_parameters_json TEXT NOT NULL,
  spread_parameters_json TEXT NOT NULL,
  reliability_parameters_json TEXT NOT NULL,
  member_qc_contract_sha256 TEXT NOT NULL CHECK(length(member_qc_contract_sha256)=64),
  train_start_date TEXT NOT NULL,
  train_end_date TEXT NOT NULL,
  holdout_start_date TEXT NOT NULL,
  holdout_end_date TEXT NOT NULL,
  rolling_origin_fold_count INTEGER NOT NULL CHECK(rolling_origin_fold_count>=1),
  effective_sample_n REAL NOT NULL CHECK(effective_sample_n>0),
  oos_metrics_json TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  CHECK(train_end_date < holdout_start_date)
);

CREATE TABLE IF NOT EXISTS calibration_cross_native_ensemble_selection_evidence (
  selection_evidence_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  run_cycle_by_system_json TEXT,
  run_cycle_by_system_sha256 TEXT CHECK(
    run_cycle_by_system_sha256 IS NULL OR
    length(run_cycle_by_system_sha256)=64
  ),
  settlement_definition TEXT NOT NULL,
  method_version TEXT NOT NULL,
  candidate_system_ids_json TEXT NOT NULL,
  selected_system_ids_json TEXT NOT NULL,
  selected_system_count INTEGER NOT NULL CHECK(selected_system_count>=2),
  independent_family_count INTEGER NOT NULL CHECK(independent_family_count>=2),
  selected_weights_json TEXT NOT NULL,
  family_weights_json TEXT NOT NULL,
  family_weight_caps_json TEXT NOT NULL,
  supported_exact_masks_json TEXT NOT NULL,
  train_start_date TEXT NOT NULL,
  train_end_date TEXT NOT NULL,
  holdout_start_date TEXT NOT NULL,
  holdout_end_date TEXT NOT NULL,
  rolling_origin_fold_count INTEGER NOT NULL CHECK(rolling_origin_fold_count>=1),
  effective_sample_n REAL NOT NULL CHECK(effective_sample_n>0),
  oos_metrics_json TEXT NOT NULL,
  cross_system_residual_correlation_json TEXT NOT NULL,
  deterministic_baseline_metrics_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  CHECK(train_end_date < holdout_start_date)
);

CREATE TABLE IF NOT EXISTS calibration_cross_native_ensemble_artifact (
  cross_ensemble_artifact_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  run_cycle_by_system_json TEXT,
  run_cycle_by_system_sha256 TEXT CHECK(
    run_cycle_by_system_sha256 IS NULL OR
    length(run_cycle_by_system_sha256)=64
  ),
  settlement_definition TEXT NOT NULL,
  ensemble_system_ids_json TEXT NOT NULL,
  ensemble_system_count INTEGER NOT NULL CHECK(ensemble_system_count>=2),
  independent_family_count INTEGER NOT NULL CHECK(independent_family_count>=2),
  supported_exact_masks_json TEXT NOT NULL,
  selection_evidence_sha256 TEXT NOT NULL CHECK(length(selection_evidence_sha256)=64),
  combination_method TEXT NOT NULL,
  family_weight_caps_json TEXT NOT NULL,
  probability_pathway TEXT NOT NULL
    CHECK(probability_pathway='CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE'),
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64)
);

-- An OOS rejection is authority evidence too.  Retain every exact-mask
-- evaluation so a later rejection cannot be hidden behind a still-valid
-- activation produced by an older passing evaluation.
CREATE TABLE IF NOT EXISTS calibration_cross_native_ensemble_oos_evaluation (
  evaluation_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  run_cycle_by_system_json TEXT NOT NULL,
  run_cycle_by_system_sha256 TEXT NOT NULL
    CHECK(length(run_cycle_by_system_sha256)=64),
  settlement_definition TEXT NOT NULL,
  ensemble_system_ids_json TEXT NOT NULL,
  available_system_set_hash TEXT NOT NULL CHECK(
    length(available_system_set_hash)=64
  ),
  selection_evidence_sha256 TEXT NOT NULL CHECK(
    length(selection_evidence_sha256)=64
  ),
  oos_state TEXT NOT NULL CHECK(oos_state IN (
    'PRODUCTION_NO_LIVE_OOS_PASS','OOS_REJECTED'
  )),
  threshold_profile TEXT NOT NULL,
  min_train_days INTEGER NOT NULL CHECK(min_train_days>=1),
  min_holdout_days INTEGER NOT NULL CHECK(min_holdout_days>=1),
  train_end_date TEXT NOT NULL,
  holdout_start_date TEXT NOT NULL,
  holdout_end_date TEXT NOT NULL,
  oos_metrics_json TEXT NOT NULL,
  climatology_baseline_metrics_json TEXT NOT NULL,
  evaluated_at_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  CHECK(train_end_date < holdout_start_date)
);

CREATE TABLE IF NOT EXISTS calibration_cross_native_ensemble_oos_prediction (
  prediction_id TEXT PRIMARY KEY,
  cross_ensemble_artifact_id TEXT NOT NULL
    REFERENCES calibration_cross_native_ensemble_artifact(
      cross_ensemble_artifact_id
    ),
  selection_evidence_sha256 TEXT NOT NULL
    CHECK(length(selection_evidence_sha256)=64),
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  run_cycle_by_system_json TEXT,
  run_cycle_by_system_sha256 TEXT CHECK(
    run_cycle_by_system_sha256 IS NULL OR
    length(run_cycle_by_system_sha256)=64
  ),
  settlement_definition TEXT NOT NULL,
  trained_through_event_date TEXT NOT NULL,
  decision_at_utc TEXT NOT NULL,
  ensemble_system_ids_json TEXT NOT NULL,
  independence_family_ids_json TEXT NOT NULL,
  system_family_ids_json TEXT NOT NULL,
  system_weights_json TEXT NOT NULL,
  family_weight_caps_json TEXT NOT NULL,
  system_member_set_shas_json TEXT NOT NULL,
  probabilities_json TEXT NOT NULL,
  bucket_centers_c_json TEXT NOT NULL,
  prediction_oos INTEGER NOT NULL CHECK(prediction_oos=1),
  authority_scope TEXT NOT NULL CHECK(
    authority_scope='PRODUCTION_NO_LIVE_ONLY'
  ),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  CHECK(trained_through_event_date < event_date),
  UNIQUE(
    cross_ensemble_artifact_id,station_id,event_date,
    season_bin,lead_bucket,run_cycle
  )
);

CREATE TABLE IF NOT EXISTS calibration_forward_taf_capture (
  taf_capture_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  revision_kind TEXT NOT NULL,
  revision_sequence INTEGER NOT NULL CHECK(revision_sequence>=1),
  issued_at_utc TEXT NOT NULL,
  received_at_utc TEXT NOT NULL,
  provider_available_at_utc TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  source_object_sha256 TEXT NOT NULL CHECK(length(source_object_sha256)=64),
  source_capture_sha256 TEXT NOT NULL CHECK(length(source_capture_sha256)=64),
  tx_c REAL,
  tx_available INTEGER NOT NULL CHECK(tx_available IN (0,1)),
  cloud_cover_fraction REAL NOT NULL
    CHECK(cloud_cover_fraction>=0 AND cloud_cover_fraction<=1),
  precipitation_probability REAL NOT NULL
    CHECK(precipitation_probability>=0 AND precipitation_probability<=1),
  wind_speed_mps REAL NOT NULL CHECK(wind_speed_mps>=0),
  source_class TEXT NOT NULL CHECK(
    source_class='official_forward_native_receipt'
  ),
  authority_scope TEXT NOT NULL CHECK(
    authority_scope='PRODUCTION_NO_LIVE_ONLY'
  ),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  CHECK((tx_available=1 AND tx_c IS NOT NULL) OR
        (tx_available=0 AND tx_c IS NULL)),
  UNIQUE(station_id,event_date,revision_id)
);

CREATE TABLE IF NOT EXISTS calibration_historical_taf_capture (
  historical_taf_capture_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  decision_anchor_utc TEXT NOT NULL,
  issued_at_utc TEXT NOT NULL,
  provider_available_at_utc TEXT NOT NULL,
  tx_c REAL NOT NULL,
  tx_valid_at_utc TEXT NOT NULL,
  cloud_cover_fraction REAL NOT NULL
    CHECK(cloud_cover_fraction>=0 AND cloud_cover_fraction<=1),
  precipitation_probability REAL NOT NULL
    CHECK(precipitation_probability>=0 AND
          precipitation_probability<=1),
  wind_speed_mps REAL NOT NULL CHECK(wind_speed_mps>=0),
  raw_taf_sha256 TEXT NOT NULL CHECK(length(raw_taf_sha256)=64),
  taf_response_sha256 TEXT NOT NULL
    CHECK(length(taf_response_sha256)=64),
  source_sample_sha256 TEXT NOT NULL
    CHECK(length(source_sample_sha256)=64),
  source_class TEXT NOT NULL CHECK(
    source_class='official_date_addressed_historical_replay'
  ),
  truth_label_authority TEXT NOT NULL CHECK(
    truth_label_authority='FORMAL_STATION_LANE_TRUTH'
  ),
  authority_scope TEXT NOT NULL CHECK(
    authority_scope='CALIBRATION_TRAIN_ONLY'
  ),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  CHECK(issued_at_utc<=provider_available_at_utc),
  CHECK(provider_available_at_utc<=decision_anchor_utc),
  UNIQUE(station_id,event_date,source_sample_sha256)
);

-- A historical AMD/COR may already have been retained as a calibration
-- receipt before the stricter station-day halt was introduced.  Preserve the
-- receipt above, but append an explicit quarantine fact so every exact join
-- can exclude the entire station day without deleting or rewriting history.
CREATE TABLE IF NOT EXISTS calibration_historical_taf_correction_quarantine (
  historical_taf_correction_quarantine_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  correction_kind TEXT NOT NULL CHECK(correction_kind IN ('AMD','COR')),
  raw_taf_sha256 TEXT NOT NULL CHECK(length(raw_taf_sha256)=64),
  source_sample_sha256 TEXT NOT NULL CHECK(length(source_sample_sha256)=64),
  source_artifact_sha256 TEXT NOT NULL CHECK(length(source_artifact_sha256)=64),
  source_class TEXT NOT NULL CHECK(
    source_class='official_date_addressed_historical_replay'
  ),
  authority_scope TEXT NOT NULL CHECK(
    authority_scope='CALIBRATION_TRAIN_ONLY'
  ),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(station_id,event_date,source_sample_sha256)
);

CREATE TABLE IF NOT EXISTS calibration_native_ensemble_promotion (
  promotion_decision_id TEXT PRIMARY KEY,
  artifact_kind TEXT NOT NULL CHECK(
    artifact_kind IN ('NATIVE_ENSEMBLE_SYSTEM_CALIBRATION','CROSS_NATIVE_ENSEMBLE')
  ),
  artifact_id TEXT NOT NULL,
  decision_seq INTEGER NOT NULL CHECK(decision_seq>=1),
  decision TEXT NOT NULL CHECK(decision IN ('PROMOTED','REJECTED','SUPERSEDED')),
  gate_version TEXT NOT NULL,
  decided_at_utc TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(artifact_kind,artifact_id,gate_version,decision_seq)
);

CREATE TABLE IF NOT EXISTS calibration_native_ensemble_activation (
  activation_id TEXT PRIMARY KEY,
  artifact_kind TEXT NOT NULL CHECK(
    artifact_kind IN ('NATIVE_ENSEMBLE_SYSTEM_CALIBRATION','CROSS_NATIVE_ENSEMBLE')
  ),
  artifact_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  ensemble_system_id_or_star TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  run_cycle_by_system_json TEXT,
  run_cycle_by_system_sha256 TEXT CHECK(
    run_cycle_by_system_sha256 IS NULL OR
    length(run_cycle_by_system_sha256)=64
  ),
  settlement_definition TEXT NOT NULL,
  available_system_set_hash_or_star TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  activated_at_utc TEXT NOT NULL,
  authority_scope TEXT NOT NULL CHECK(
    authority_scope IN ('FORMAL_PROMOTED','PRODUCTION_NO_LIVE_ONLY')
  ),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(
    artifact_kind,station_id,ensemble_system_id_or_star,season_bin,
    lead_bucket,run_cycle,available_system_set_hash_or_star,generation
  )
);

CREATE TABLE IF NOT EXISTS calibration_artifact_promotion (
  promotion_decision_id TEXT PRIMARY KEY,
  artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('MODEL_CALIBRATION','MODEL_ENSEMBLE')),
  artifact_id TEXT NOT NULL,
  decision_seq INTEGER NOT NULL CHECK(decision_seq>=1),
  decision TEXT NOT NULL CHECK(decision IN ('PROMOTED','REJECTED','SUPERSEDED')),
  gate_version TEXT NOT NULL,
  decided_at_utc TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  UNIQUE(artifact_kind,artifact_id,gate_version,decision_seq)
);

CREATE TABLE IF NOT EXISTS calibration_activation (
  activation_id TEXT PRIMARY KEY,
  artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('MODEL_CALIBRATION','MODEL_ENSEMBLE')),
  artifact_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  model_id_or_star TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  settlement_definition TEXT NOT NULL,
  available_model_set_hash_or_star TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  activated_at_utc TEXT NOT NULL,
  authority_scope TEXT NOT NULL DEFAULT 'FORMAL_PROMOTED'
    CHECK(authority_scope IN ('FORMAL_PROMOTED','PRODUCTION_NO_LIVE_ONLY')),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  UNIQUE(artifact_kind,station_id,model_id_or_star,season_bin,lead_bucket,run_cycle,available_model_set_hash_or_star,generation)
);

CREATE TABLE IF NOT EXISTS basket_policy_training_fact (
  fact_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  decision_at_utc TEXT NOT NULL,
  decision_scope_key TEXT NOT NULL UNIQUE,
  captured_at_utc TEXT NOT NULL,
  source_sequence INTEGER NOT NULL CHECK(source_sequence>=0),
  quote_observed_at TEXT NOT NULL,
  quote_expires_at TEXT NOT NULL,
  quote_freshness_policy_sha256 TEXT NOT NULL
    CHECK(length(quote_freshness_policy_sha256)=64),
  stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
  probability_envelope_id TEXT NOT NULL,
  posterior_version TEXT NOT NULL,
  posterior_sha256 TEXT NOT NULL CHECK(length(posterior_sha256)=64),
  official_exclusion_mask_sha256 TEXT NOT NULL CHECK(length(official_exclusion_mask_sha256)=64),
  ws_snapshot_id TEXT NOT NULL,
  quote_version TEXT NOT NULL,
  quote_sha256 TEXT NOT NULL CHECK(length(quote_sha256)=64),
  inventory_snapshot_id TEXT NOT NULL,
  inventory_version TEXT NOT NULL,
  inventory_sha256 TEXT NOT NULL CHECK(length(inventory_sha256)=64),
  order_state_snapshot_id TEXT NOT NULL,
  order_state_version TEXT NOT NULL,
  order_state_sha256 TEXT NOT NULL CHECK(length(order_state_sha256)=64),
  cycle_id TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  decision_sha256 TEXT NOT NULL CHECK(length(decision_sha256)=64),
  plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256)=64),
  intent_count INTEGER NOT NULL CHECK(intent_count>=0),
  intent_set_sha256 TEXT NOT NULL CHECK(length(intent_set_sha256)=64),
  policy_version_vector_json TEXT NOT NULL,
  feature_json TEXT NOT NULL,
  partition_key TEXT NOT NULL,
  authority_scope TEXT NOT NULL
    CHECK(authority_scope='PRODUCTION_NO_LIVE_FORWARD'),
  created_at_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  UNIQUE(
    probability_envelope_id,ws_snapshot_id,inventory_snapshot_id,
    order_state_snapshot_id,cycle_id,decision_id
  )
);

CREATE INDEX IF NOT EXISTS basket_policy_training_fact_scope_idx
ON basket_policy_training_fact(
  station_id,event_date,stage,decision_at_utc,partition_key
);

CREATE TABLE IF NOT EXISTS basket_policy_training_label (
  label_id TEXT PRIMARY KEY,
  fact_id TEXT NOT NULL,
  truth_id TEXT NOT NULL,
  truth_revision_no INTEGER NOT NULL CHECK(truth_revision_no>=0),
  truth_sha256 TEXT NOT NULL CHECK(length(truth_sha256)=64),
  official_tmax_c REAL NOT NULL,
  winning_bucket TEXT NOT NULL,
  settlement_closed_at_utc TEXT NOT NULL,
  labeled_at_utc TEXT NOT NULL,
  outcome_json TEXT NOT NULL,
  authority_scope TEXT NOT NULL
    CHECK(authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE')),
  supersedes_label_id TEXT,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  UNIQUE(fact_id,truth_id)
);

CREATE TABLE IF NOT EXISTS basket_global_prior_artifact (
  global_prior_id TEXT PRIMARY KEY,
  strategy_version TEXT NOT NULL,
  family_parameters_json TEXT NOT NULL,
  default_policy_json TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)
);

CREATE TABLE IF NOT EXISTS basket_station_policy_artifact (
  artifact_id TEXT PRIMARY KEY,
  family TEXT NOT NULL CHECK(family IN ('target','reserve','cadence')),
  station_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  maturity_bin TEXT NOT NULL,
  liquidity_bin TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  theta_local_json TEXT NOT NULL,
  effective_sample_n REAL NOT NULL CHECK(effective_sample_n>=0),
  kappa REAL NOT NULL CHECK(kappa>=0),
  trained_through_utc TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)
);

CREATE TABLE IF NOT EXISTS basket_policy_promotion (
  promotion_id TEXT PRIMARY KEY,
  artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('GLOBAL_PRIOR','STATION_POLICY','STATION_ACTIVATION')),
  artifact_id TEXT NOT NULL,
  decision_seq INTEGER NOT NULL CHECK(decision_seq>=1),
  decision TEXT NOT NULL CHECK(decision IN ('PROMOTED','REJECTED','SUPERSEDED')),
  gate_version TEXT NOT NULL,
  decided_at_utc TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
  UNIQUE(artifact_kind,artifact_id,gate_version,decision_seq)
);

CREATE TABLE IF NOT EXISTS basket_policy_activation (
  activation_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  maturity_bin TEXT NOT NULL,
  liquidity_bin TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  global_prior_id TEXT NOT NULL,
  target_artifact_id TEXT,
  reserve_artifact_id TEXT,
  cadence_artifact_id TEXT,
  fallback_families_json TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  activated_at_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  UNIQUE(station_id,stage,season_bin,maturity_bin,liquidity_bin,strategy_version,generation)
);

CREATE INDEX IF NOT EXISTS calibration_model_scope_idx ON calibration_model_artifact(station_id,model_id,season_bin,lead_bucket,run_cycle,valid_from_utc,valid_until_utc);
CREATE INDEX IF NOT EXISTS calibration_ensemble_scope_idx ON calibration_ensemble_artifact(station_id,season_bin,lead_bucket,run_cycle,valid_from_utc,valid_until_utc);
CREATE INDEX IF NOT EXISTS calibration_ensemble_selection_scope_idx ON calibration_ensemble_selection_evidence(station_id,season_bin,lead_bucket,run_cycle,train_end_date,holdout_end_date);
CREATE INDEX IF NOT EXISTS calibration_activation_asof_idx ON calibration_activation(station_id,artifact_kind,season_bin,lead_bucket,run_cycle,valid_from_utc,valid_until_utc,generation);
CREATE INDEX IF NOT EXISTS calibration_native_member_scope_idx ON calibration_native_ensemble_member_run(ensemble_system_id,station_id,target_local_date,run_init_utc,member_id);
CREATE INDEX IF NOT EXISTS calibration_native_member_bucket_scope_idx ON calibration_native_ensemble_member_bucket(ensemble_system_id,station_id,target_local_date,aggregator_snapshot_at_utc);
CREATE INDEX IF NOT EXISTS calibration_native_historical_member_scope_idx ON calibration_native_ensemble_historical_member_run(ensemble_system_id,station_id,target_local_date,archive_lead_bucket,member_id);
-- The dispersion/hit-rate board groups this table by system, station, day,
-- run and lead over a rolling window; the scope index above leads with the
-- wrong keys for that shape, so 1.16M rows went through a temp B-tree on
-- every render -- 23.7 seconds of one collector, measured 2026-08-29.
-- Leading with the filter keys and carrying the grouping keys and the value
-- makes it a covering scan: the collector runs in 3.6 seconds.
CREATE INDEX IF NOT EXISTS calibration_native_member_skill_idx ON calibration_native_ensemble_historical_member_run(ensemble_system_id,transport_provider_id,qc_status,target_local_date,station_id,native_run_init_utc,run_cycle,archive_lead_bucket,raw_tmax_c);
CREATE INDEX IF NOT EXISTS calibration_native_historical_bucket_scope_idx ON calibration_native_ensemble_historical_member_bucket(ensemble_system_id,station_id,target_local_date,archive_query_at_utc);
CREATE INDEX IF NOT EXISTS calibration_native_system_artifact_scope_idx ON calibration_native_ensemble_system_artifact(ensemble_system_id,station_id,season_bin,lead_bucket,run_cycle,valid_from_utc,valid_until_utc);
CREATE INDEX IF NOT EXISTS calibration_cross_native_artifact_scope_idx ON calibration_cross_native_ensemble_artifact(station_id,season_bin,lead_bucket,run_cycle,valid_from_utc,valid_until_utc);
CREATE INDEX IF NOT EXISTS calibration_cross_native_oos_evaluation_scope_idx ON calibration_cross_native_ensemble_oos_evaluation(station_id,season_bin,lead_bucket,run_cycle,available_system_set_hash,evaluated_at_utc,evaluation_id);
CREATE INDEX IF NOT EXISTS calibration_cross_native_oos_prediction_scope_idx ON calibration_cross_native_ensemble_oos_prediction(station_id,event_date,season_bin,lead_bucket,run_cycle,decision_at_utc);
CREATE INDEX IF NOT EXISTS calibration_forward_taf_capture_scope_idx ON calibration_forward_taf_capture(station_id,event_date,provider_available_at_utc,revision_sequence);
CREATE INDEX IF NOT EXISTS calibration_historical_taf_capture_scope_idx ON calibration_historical_taf_capture(station_id,event_date,provider_available_at_utc);
CREATE INDEX IF NOT EXISTS calibration_historical_taf_correction_quarantine_scope_idx ON calibration_historical_taf_correction_quarantine(station_id,event_date,settlement_timezone);
CREATE INDEX IF NOT EXISTS calibration_native_activation_asof_idx ON calibration_native_ensemble_activation(station_id,artifact_kind,season_bin,lead_bucket,run_cycle,valid_from_utc,valid_until_utc,generation);
CREATE INDEX IF NOT EXISTS calibration_station_source_authority_latest_idx ON calibration_station_source_authority(station_id,activated_at_utc,authority_id);
CREATE INDEX IF NOT EXISTS basket_policy_activation_asof_idx ON basket_policy_activation(station_id,stage,season_bin,maturity_bin,liquidity_bin,strategy_version,valid_from_utc,valid_until_utc,generation);

CREATE TRIGGER IF NOT EXISTS calibration_station_registry_no_update BEFORE UPDATE ON calibration_station_registry BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_station_registry_no_delete BEFORE DELETE ON calibration_station_registry BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_station_source_authority_no_update BEFORE UPDATE ON calibration_station_source_authority BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_station_source_authority_no_delete BEFORE DELETE ON calibration_station_source_authority BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_station_market_timezone_binding_no_update BEFORE UPDATE ON calibration_station_market_timezone_binding BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_station_market_timezone_binding_no_delete BEFORE DELETE ON calibration_station_market_timezone_binding BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_truth_no_update BEFORE UPDATE ON calibration_station_daily_high_truth BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_truth_no_delete BEFORE DELETE ON calibration_station_daily_high_truth BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_model_run_no_update BEFORE UPDATE ON calibration_historical_asof_model_run BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_model_run_no_delete BEFORE DELETE ON calibration_historical_asof_model_run BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_residual_no_update BEFORE UPDATE ON calibration_model_station_residual BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_residual_no_delete BEFORE DELETE ON calibration_model_station_residual BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_model_artifact_no_update BEFORE UPDATE ON calibration_model_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_model_artifact_no_delete BEFORE DELETE ON calibration_model_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_ensemble_artifact_no_update BEFORE UPDATE ON calibration_ensemble_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_ensemble_artifact_no_delete BEFORE DELETE ON calibration_ensemble_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_ensemble_selection_evidence_no_update BEFORE UPDATE ON calibration_ensemble_selection_evidence BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_ensemble_selection_evidence_no_delete BEFORE DELETE ON calibration_ensemble_selection_evidence BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_system_no_update BEFORE UPDATE ON calibration_native_ensemble_system BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_system_no_delete BEFORE DELETE ON calibration_native_ensemble_system BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_member_run_no_update BEFORE UPDATE ON calibration_native_ensemble_member_run BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_member_run_no_delete BEFORE DELETE ON calibration_native_ensemble_member_run BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_member_bucket_no_update BEFORE UPDATE ON calibration_native_ensemble_member_bucket BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_member_bucket_no_delete BEFORE DELETE ON calibration_native_ensemble_member_bucket BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_historical_member_no_update BEFORE UPDATE ON calibration_native_ensemble_historical_member_run BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_historical_member_no_delete BEFORE DELETE ON calibration_native_ensemble_historical_member_run BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_historical_bucket_no_update BEFORE UPDATE ON calibration_native_ensemble_historical_member_bucket BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_historical_bucket_no_delete BEFORE DELETE ON calibration_native_ensemble_historical_member_bucket BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_system_artifact_no_update BEFORE UPDATE ON calibration_native_ensemble_system_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_system_artifact_no_delete BEFORE DELETE ON calibration_native_ensemble_system_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_cross_native_selection_no_update BEFORE UPDATE ON calibration_cross_native_ensemble_selection_evidence BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_cross_native_selection_no_delete BEFORE DELETE ON calibration_cross_native_ensemble_selection_evidence BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_cross_native_artifact_no_update BEFORE UPDATE ON calibration_cross_native_ensemble_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_cross_native_artifact_no_delete BEFORE DELETE ON calibration_cross_native_ensemble_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_cross_native_oos_evaluation_no_update BEFORE UPDATE ON calibration_cross_native_ensemble_oos_evaluation BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_cross_native_oos_evaluation_no_delete BEFORE DELETE ON calibration_cross_native_ensemble_oos_evaluation BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_cross_native_oos_prediction_no_update BEFORE UPDATE ON calibration_cross_native_ensemble_oos_prediction BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_cross_native_oos_prediction_no_delete BEFORE DELETE ON calibration_cross_native_ensemble_oos_prediction BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_forward_taf_capture_no_update BEFORE UPDATE ON calibration_forward_taf_capture BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_forward_taf_capture_no_delete BEFORE DELETE ON calibration_forward_taf_capture BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_historical_taf_capture_no_update BEFORE UPDATE ON calibration_historical_taf_capture BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_historical_taf_capture_no_delete BEFORE DELETE ON calibration_historical_taf_capture BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_historical_taf_correction_quarantine_no_update BEFORE UPDATE ON calibration_historical_taf_correction_quarantine BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_historical_taf_correction_quarantine_no_delete BEFORE DELETE ON calibration_historical_taf_correction_quarantine BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_promotion_no_update BEFORE UPDATE ON calibration_native_ensemble_promotion BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_promotion_no_delete BEFORE DELETE ON calibration_native_ensemble_promotion BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_activation_no_update BEFORE UPDATE ON calibration_native_ensemble_activation BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_native_activation_no_delete BEFORE DELETE ON calibration_native_ensemble_activation BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_promotion_no_update BEFORE UPDATE ON calibration_artifact_promotion BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_promotion_no_delete BEFORE DELETE ON calibration_artifact_promotion BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_activation_no_update BEFORE UPDATE ON calibration_activation BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_activation_no_delete BEFORE DELETE ON calibration_activation BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_training_fact_no_update BEFORE UPDATE ON basket_policy_training_fact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_training_fact_no_delete BEFORE DELETE ON basket_policy_training_fact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_training_label_no_update BEFORE UPDATE ON basket_policy_training_label BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_training_label_no_delete BEFORE DELETE ON basket_policy_training_label BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_global_prior_no_update BEFORE UPDATE ON basket_global_prior_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_global_prior_no_delete BEFORE DELETE ON basket_global_prior_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_policy_artifact_no_update BEFORE UPDATE ON basket_station_policy_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_policy_artifact_no_delete BEFORE DELETE ON basket_station_policy_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_policy_promotion_no_update BEFORE UPDATE ON basket_policy_promotion BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_policy_promotion_no_delete BEFORE DELETE ON basket_policy_promotion BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_policy_activation_no_update BEFORE UPDATE ON basket_policy_activation BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_policy_activation_no_delete BEFORE DELETE ON basket_policy_activation BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


# The exact price x timeout grid has a separate authority namespace.  Its
# activation scope is intentionally only the five public fields; the two
# microstructure bins live on each immutable point inside artifact_json.
MAKER_GRID_OOS_AUTHORITY_DDL = """
CREATE TABLE IF NOT EXISTS maker_grid_oos_artifact_body (
  artifact_id TEXT PRIMARY KEY,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  schema TEXT NOT NULL CHECK(schema='target_maker_price_timeout_grid_oos_artifact_v1'),
  artifact_json TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  station_id TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  issuance TEXT NOT NULL,
  liquidity_bin TEXT NOT NULL,
  as_of_utc TEXT NOT NULL,
  grid_size INTEGER NOT NULL CHECK(grid_size>=1 AND grid_size<=256),
  curve_sha256 TEXT NOT NULL CHECK(length(curve_sha256)=64),
  stored_at_utc TEXT NOT NULL,
  CHECK(length(artifact_json)<=1000000),
  CHECK(length(scope_json)<=4096)
);

CREATE TABLE IF NOT EXISTS maker_grid_oos_promotion_evidence (
  promotion_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL REFERENCES maker_grid_oos_artifact_body(artifact_id),
  artifact_content_sha256 TEXT NOT NULL CHECK(length(artifact_content_sha256)=64),
  decision TEXT NOT NULL CHECK(decision IN ('PROMOTED','REJECTED')),
  gate_version TEXT NOT NULL,
  decided_at_utc TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
  oos_metrics_sha256 TEXT NOT NULL CHECK(length(oos_metrics_sha256)=64),
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
  evidence_json TEXT NOT NULL,
  stored_at_utc TEXT NOT NULL,
  CHECK(length(evidence_json)<=65536)
);

CREATE TABLE IF NOT EXISTS maker_grid_oos_activation_manifest (
  activation_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL REFERENCES maker_grid_oos_artifact_body(artifact_id),
  artifact_content_sha256 TEXT NOT NULL CHECK(length(artifact_content_sha256)=64),
  promotion_decision_id TEXT NOT NULL REFERENCES maker_grid_oos_promotion_evidence(promotion_id),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  manifest_json TEXT NOT NULL,
  station_id TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  issuance TEXT NOT NULL,
  liquidity_bin TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  activated_at_utc TEXT NOT NULL,
  pointer_sha256 TEXT NOT NULL CHECK(length(pointer_sha256)=64),
  CHECK(length(manifest_json)<=65536),
  CHECK(valid_from_utc<valid_until_utc),
  UNIQUE(station_id,season_bin,lead_bucket,issuance,liquidity_bin,generation)
);

CREATE TABLE IF NOT EXISTS maker_grid_oos_activation_pointer (
  pointer_sha256 TEXT PRIMARY KEY CHECK(length(pointer_sha256)=64),
  activation_id TEXT NOT NULL REFERENCES maker_grid_oos_activation_manifest(activation_id),
  artifact_id TEXT NOT NULL REFERENCES maker_grid_oos_artifact_body(artifact_id),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  pointer_json TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  CHECK(length(pointer_json)<=65536)
);

CREATE INDEX IF NOT EXISTS maker_grid_oos_activation_scope_asof_idx
ON maker_grid_oos_activation_manifest(
  station_id,season_bin,lead_bucket,issuance,liquidity_bin,
  valid_from_utc,valid_until_utc,generation
);
CREATE INDEX IF NOT EXISTS maker_grid_oos_promotion_artifact_decision_idx
ON maker_grid_oos_promotion_evidence(artifact_id,decision,decided_at_utc);

CREATE TRIGGER IF NOT EXISTS maker_grid_oos_artifact_body_no_update
BEFORE UPDATE ON maker_grid_oos_artifact_body BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS maker_grid_oos_artifact_body_no_delete
BEFORE DELETE ON maker_grid_oos_artifact_body BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS maker_grid_oos_promotion_no_update
BEFORE UPDATE ON maker_grid_oos_promotion_evidence BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS maker_grid_oos_promotion_no_delete
BEFORE DELETE ON maker_grid_oos_promotion_evidence BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS maker_grid_oos_manifest_no_update
BEFORE UPDATE ON maker_grid_oos_activation_manifest BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS maker_grid_oos_manifest_no_delete
BEFORE DELETE ON maker_grid_oos_activation_manifest BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS maker_grid_oos_pointer_no_update
BEFORE UPDATE ON maker_grid_oos_activation_pointer BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS maker_grid_oos_pointer_no_delete
BEFORE DELETE ON maker_grid_oos_activation_pointer BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""

def _regime_artifact_table_sql(table: str) -> str:
    if table not in {
        "basket_regime_artifact",
        "basket_regime_artifact_superseded_migration_v1",
    }:
        raise RuntimeError("regime_artifact_table_name_invalid")
    return f"""
CREATE TABLE {table} (
  artifact_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
  checkpoint_id TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  probability_sha256 TEXT NOT NULL CHECK(length(probability_sha256)=64),
  as_of_utc TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  artifact_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  state TEXT NOT NULL
    CHECK(state IN ('COMMITTED','QUARANTINED','SUPERSEDED')),
  created_at TEXT NOT NULL,
  CHECK(valid_from_utc<valid_until_utc)
);
"""


REGIME_ARTIFACT_GUARD_DDL = """
CREATE INDEX IF NOT EXISTS basket_regime_artifact_scope_idx
ON basket_regime_artifact(
  station_id,event_date,stage,checkpoint_id,probability_sha256,
  state,valid_from_utc,valid_until_utc
);

CREATE UNIQUE INDEX IF NOT EXISTS basket_regime_artifact_one_committed_idx
ON basket_regime_artifact(
  station_id,event_date,stage,checkpoint_id,probability_sha256
) WHERE state='COMMITTED';

CREATE TRIGGER IF NOT EXISTS basket_regime_artifact_controlled_update
BEFORE UPDATE ON basket_regime_artifact
WHEN NOT (
  OLD.state='COMMITTED' AND NEW.state='SUPERSEDED'
  AND NEW.artifact_id IS OLD.artifact_id
  AND NEW.station_id IS OLD.station_id
  AND NEW.event_date IS OLD.event_date
  AND NEW.stage IS OLD.stage
  AND NEW.checkpoint_id IS OLD.checkpoint_id
  AND NEW.settlement_timezone IS OLD.settlement_timezone
  AND NEW.probability_sha256 IS OLD.probability_sha256
  AND NEW.as_of_utc IS OLD.as_of_utc
  AND NEW.valid_from_utc IS OLD.valid_from_utc
  AND NEW.valid_until_utc IS OLD.valid_until_utc
  AND NEW.artifact_json IS OLD.artifact_json
  AND NEW.content_sha256 IS OLD.content_sha256
  AND NEW.created_at IS OLD.created_at
)
BEGIN SELECT RAISE(ABORT,'regime_update_forbidden'); END;

CREATE TRIGGER IF NOT EXISTS basket_regime_artifact_no_delete
BEFORE DELETE ON basket_regime_artifact
BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


REGIME_ARTIFACT_DDL = (
    _regime_artifact_table_sql("basket_regime_artifact").replace(
        "CREATE TABLE basket_regime_artifact",
        "CREATE TABLE IF NOT EXISTS basket_regime_artifact",
        1,
    )
    + REGIME_ARTIFACT_GUARD_DDL
)


# These are the *materialized* production inputs consumed by the basket lane.
# They deliberately do not reuse posterior_checkpoints or market tick
# history: the target lane needs a stage-versioned, content-addressed p(B)
# vector and one official CLOB WS snapshot with the inventory/order-state
# generations it was evaluated against.
PROBABILITY_BRIDGE_DDL = """
CREATE TABLE IF NOT EXISTS basket_probability_bridge_history (
  history_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
  checkpoint_id TEXT NOT NULL,
  decision_at_utc TEXT NOT NULL,
  fresh_until_utc TEXT NOT NULL,
  envelope_content_sha256 TEXT NOT NULL
    CHECK(length(envelope_content_sha256)=64),
  bridge_sha256 TEXT NOT NULL CHECK(length(bridge_sha256)=64),
  bucket_map_sha256 TEXT NOT NULL CHECK(length(bucket_map_sha256)=64),
  bucket_intervals_json TEXT NOT NULL,
  projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256)=64),
  envelope_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state='COMMITTED'),
  created_at_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(checkpoint_id,envelope_content_sha256,bucket_map_sha256)
);

CREATE INDEX IF NOT EXISTS basket_probability_bridge_history_asof_idx
ON basket_probability_bridge_history(
  station_id,event_date,state,decision_at_utc,stage
);

-- The pointer is chosen from stage and decision time alone, so the choice
-- should never touch a row.  history_id on the end makes the ranking query
-- covering: the index answers it and the 8 to 28 MB of envelope_json in
-- those rows stays on disk.  Only the winner is then loaded.
CREATE INDEX IF NOT EXISTS basket_probability_bridge_history_rank_idx
ON basket_probability_bridge_history(
  station_id,event_date,state,decision_at_utc,stage,checkpoint_id,history_id
);

-- Both of the above lead with the station-day, which is right for everything
-- that asks about one scope.  The centre/width refit asks across all stations
-- for one stage since an evidence floor -- exactly this index's two columns --
-- and with neither able to serve it, it read all 287k rows to keep the
-- thousand or so that the floor admits.  That is the part of the refit whose
-- cost tracks total history rather than the evidence window, and the table
-- grows 8.78% a day, so it is also the part that would have put the round
-- back over its sweep timeout in about eighteen days.
CREATE INDEX IF NOT EXISTS basket_probability_bridge_history_stage_decided_idx
ON basket_probability_bridge_history(
  stage,decision_at_utc
);

-- The fits ask a second question this table could not answer without a
-- sort: "the first decision of each station-day, for one stage".  The
-- index above leads with stage and then time, so the group-by fell to a
-- temp B-tree over every row of the stage -- 332k rows re-sorted on every
-- call, measured 2026-08-29 at 37.3 seconds per stage for the band
-- serving lane and 111 seconds for the residual audit.  Leading with the
-- grouping keys makes it a covering scan: the same lane runs in 1.7
-- seconds and the audit in 4.5.
CREATE INDEX IF NOT EXISTS basket_probability_bridge_history_stage_scope_idx
ON basket_probability_bridge_history(
  stage,station_id,event_date,decision_at_utc
);

CREATE TABLE IF NOT EXISTS basket_probability_bridge_latest (
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  history_id TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
  checkpoint_id TEXT NOT NULL,
  decision_at_utc TEXT NOT NULL,
  fresh_until_utc TEXT NOT NULL,
  envelope_content_sha256 TEXT NOT NULL
    CHECK(length(envelope_content_sha256)=64),
  projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256)=64),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  updated_at_utc TEXT NOT NULL,
  PRIMARY KEY(station_id,event_date)
);

CREATE TABLE IF NOT EXISTS basket_probability_bridge_bucket_probability (
  history_id TEXT NOT NULL,
  bucket TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  lower_c REAL,
  upper_c REAL,
  probability REAL NOT NULL CHECK(probability>=0.0 AND probability<=1.0),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  PRIMARY KEY(history_id,bucket),
  UNIQUE(history_id,ordinal)
);

CREATE TRIGGER IF NOT EXISTS basket_probability_bridge_history_no_update
BEFORE UPDATE ON basket_probability_bridge_history
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_probability_bridge_history_no_delete
BEFORE DELETE ON basket_probability_bridge_history
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_probability_bridge_bucket_no_update
BEFORE UPDATE ON basket_probability_bridge_bucket_probability
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_probability_bridge_bucket_no_delete
BEFORE DELETE ON basket_probability_bridge_bucket_probability
BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


PRODUCTION_TARGET_DDL = """
CREATE TABLE IF NOT EXISTS basket_probability_envelope (
  envelope_id TEXT PRIMARY KEY,
  stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
  checkpoint_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  decision_at_utc TEXT NOT NULL,
  fresh_until_utc TEXT NOT NULL,
  bucket_ordinals_json TEXT NOT NULL,
  probabilities_json TEXT NOT NULL,
  posterior_version TEXT NOT NULL,
  posterior_sha256 TEXT NOT NULL CHECK(length(posterior_sha256)=64),
  bridge_sha256 TEXT NOT NULL CHECK(length(bridge_sha256)=64),
  lineage_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  state TEXT NOT NULL CHECK(state IN ('COMMITTED','QUARANTINED')),
  created_at TEXT NOT NULL,
  UNIQUE(station_id,event_date,stage,checkpoint_id,content_sha256)
);

CREATE INDEX IF NOT EXISTS basket_probability_envelope_asof_idx
ON basket_probability_envelope(station_id,event_date,state,decision_at_utc,fresh_until_utc,stage);

-- checkpoint_id is how the P0 drain re-reads a committed envelope, once per
-- pending envelope per cycle.  It is unique-constrained above but only as
-- part of a composite, which SQLite cannot use for a bare checkpoint_id
-- probe, so every lookup was a full scan of a table whose rows carry
-- megabyte lineage blobs -- 5.6 seconds per envelope at 38,830 rows on
-- 2026-08-15, which turned a compile backlog into an unfinishable cycle.
CREATE INDEX IF NOT EXISTS basket_probability_envelope_checkpoint_idx
ON basket_probability_envelope(checkpoint_id);

-- "The newest committed envelope for this station at this stage" is asked
-- once per station per cycle, and the asof index above cannot serve it: its
-- second column is event_date, which this query does not constrain, so the
-- planner fell back to the station-only autoindex, read every row that
-- station has -- each carrying a lineage blob -- and sorted them all in a
-- TEMP B-TREE to take one.  Measured 2026-08-22: 900ms uncontended and 15
-- to 42 seconds behind a competing writer, times forty-five stations.  With
-- decision_at_utc in the index and in the right order, the LIMIT 1 is the
-- first entry of a seek and nothing is sorted: 900ms -> 1.8ms.
CREATE INDEX IF NOT EXISTS basket_probability_envelope_station_stage_recency_idx
ON basket_probability_envelope(station_id,stage,state,decision_at_utc DESC);

-- The centre/width evidence floor asks one thing of this table, once per
-- history row of its stage: what posterior_version does this envelope carry.
-- posterior_version sits behind bucket_ordinals_json and probabilities_json,
-- so answering it from the row meant traversing overflow pages 165k times on
-- an 8GB machine against a 29GB file.  Covering it costs one small index and
-- took that probe from 74.1s to 30.6s.  What is left is the 165k b-tree
-- descents themselves, which only a different query could remove -- reading
-- the version straight off this table would need history and envelope proven
-- equal on decision_at_utc first, and that is a semantic change, not an index.
CREATE INDEX IF NOT EXISTS basket_probability_envelope_version_probe_idx
ON basket_probability_envelope(envelope_id,posterior_version);
""" + PROBABILITY_BRIDGE_DDL + REGIME_ARTIFACT_DDL + """
CREATE TABLE IF NOT EXISTS basket_official_ws_snapshot (
  snapshot_id TEXT PRIMARY KEY,
  market_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK(sequence>=0),
  observed_at TEXT NOT NULL,
  quote_version TEXT NOT NULL,
  quote_observed_at TEXT NOT NULL,
  quote_expires_at TEXT NOT NULL,
  quote_freshness_policy_sha256 TEXT NOT NULL
    CHECK(length(quote_freshness_policy_sha256)=64),
  quote_source TEXT NOT NULL CHECK(quote_source='OFFICIAL_CLOB_WS'),
  inventory_version TEXT NOT NULL,
  order_state_version TEXT NOT NULL,
  remaining_budget_usdc REAL NOT NULL CHECK(remaining_budget_usdc>=0),
  buckets_json TEXT NOT NULL,
  cash_phase_input_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  state TEXT NOT NULL CHECK(state IN ('COMMITTED','QUARANTINED')),
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS basket_official_ws_snapshot_asof_idx
ON basket_official_ws_snapshot(station_id,event_date,state,observed_at,sequence);

-- The calibration and weight tables are read the same way on the same path,
-- and were scanned the same way.  The member archive is the expensive one:
-- 818k rows, read per station-day, 1,170ms a scope -- one station cost more
-- than the whole cycle's budget.  Measured 2026-08-15 before and after:
-- member archive 1170ms -> 2.6ms, selection evidence 176ms -> 0.6ms, market
-- registry 237ms -> 1.9ms, system artifact 37ms -> 0.9ms.
--
-- These grow with every capture and every refit, so an unindexed read here
-- degrades on its own rather than staying merely slow.
CREATE INDEX IF NOT EXISTS
  calibration_native_ensemble_historical_member_run_scope_idx
ON calibration_native_ensemble_historical_member_run(
  station_id,target_local_date,ensemble_system_id,archive_lead_bucket
);

CREATE INDEX IF NOT EXISTS
  calibration_cross_native_ensemble_selection_evidence_scope_idx
ON calibration_cross_native_ensemble_selection_evidence(
  station_id,lead_bucket,run_cycle,train_end_date
);

CREATE INDEX IF NOT EXISTS
  calibration_native_ensemble_member_run_authority_idx
ON calibration_native_ensemble_member_run(
  authority_scope,station_id,target_local_date
);

-- Every remaining append-only table read by (station, day).  None of these
-- was wrong when written; they become wrong as rows accumulate, so they are
-- indexed on arrival rather than after the next stall.  The rule that found
-- all of them: list tables over a thousand rows carrying station_id and a
-- date, and index any whose leading index is not that pair.
CREATE INDEX IF NOT EXISTS calibration_native_ensemble_member_run_scope_idx
ON calibration_native_ensemble_member_run(station_id,target_local_date);

CREATE INDEX IF NOT EXISTS
  calibration_native_ensemble_historical_member_bucket_scope_idx
ON calibration_native_ensemble_historical_member_bucket(
  station_id,target_local_date
);

CREATE INDEX IF NOT EXISTS calibration_historical_asof_model_run_scope_idx
ON calibration_historical_asof_model_run(station_id,target_local_date);

CREATE INDEX IF NOT EXISTS calibration_native_ensemble_member_bucket_scope_idx
ON calibration_native_ensemble_member_bucket(station_id,target_local_date);

CREATE INDEX IF NOT EXISTS
  calibration_native_ensemble_system_artifact_scope_idx
ON calibration_native_ensemble_system_artifact(
  station_id,lead_bucket,run_cycle,ensemble_system_id,train_end_date
);


-- Evidence-only WS cache.  This is deliberately not a production input
-- table: it keeps only the newest complete quote evidence per target scope so
-- a connected, non-authorizing WS cannot be mistaken for an executable
-- market snapshot or grow the target DB without bound.
CREATE TABLE IF NOT EXISTS basket_official_ws_evidence_cache (
  scope_key TEXT PRIMARY KEY,
  evidence_id TEXT NOT NULL UNIQUE,
  market_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  quote_version TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  buckets_json TEXT NOT NULL,
  quote_source TEXT NOT NULL CHECK(quote_source='OFFICIAL_CLOB_WS'),
  evidence_only INTEGER NOT NULL CHECK(evidence_only=1),
  authorizes_order INTEGER NOT NULL CHECK(authorizes_order=0),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS basket_official_ws_evidence_cache_scope_idx
ON basket_official_ws_evidence_cache(station_id,event_date,observed_at);

CREATE TABLE IF NOT EXISTS basket_confirmed_inventory_snapshot (
  inventory_snapshot_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  inventory_version TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  complete INTEGER NOT NULL CHECK(complete IN (0,1)),
  inventory_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  state TEXT NOT NULL CHECK(state IN ('COMMITTED','QUARANTINED')),
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS basket_inventory_snapshot_asof_idx
ON basket_confirmed_inventory_snapshot(station_id,event_date,state,observed_at);

-- The reader resolves an inventory by (station, day, version) and treats two
-- matches as unresolvable -- delta_v2_inventory_snapshot_ambiguous, which
-- blocked eleven of thirteen forward scopes on 2026-08-16.  Nothing declared
-- that tuple unique, so the ambiguity was a state the writer was free to
-- create and the reader could only refuse after the fact.  Declaring it here
-- moves the failure to the write that would cause it, where it can be seen
-- and attributed.  Existing rows already satisfy it.
CREATE UNIQUE INDEX IF NOT EXISTS
  basket_confirmed_inventory_snapshot_scope_version_uidx
ON basket_confirmed_inventory_snapshot(
  station_id,event_date,inventory_version,state
);

CREATE TABLE IF NOT EXISTS basket_order_state_snapshot (
  order_state_snapshot_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  order_state_version TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  known INTEGER NOT NULL CHECK(known IN (0,1)),
  order_state_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  state TEXT NOT NULL CHECK(state IN ('COMMITTED','QUARANTINED')),
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS basket_order_state_snapshot_asof_idx
ON basket_order_state_snapshot(station_id,event_date,state,observed_at);

CREATE TABLE IF NOT EXISTS basket_cash_phase_policy_artifact (
  policy_version TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  station_timezone TEXT NOT NULL,
  heavy_to_standard_local_time TEXT NOT NULL,
  standard_to_tail_local_time TEXT NOT NULL,
  cash_by_phase_json TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  evidence_status TEXT NOT NULL,
  promoted INTEGER NOT NULL CHECK(promoted IN (0,1)),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)
);

CREATE INDEX IF NOT EXISTS basket_cash_phase_policy_scope_idx
ON basket_cash_phase_policy_artifact(station_id,valid_from_utc,valid_until_utc,policy_version);

CREATE TABLE IF NOT EXISTS basket_cash_phase_authority_manifest (
  manifest_sha256 TEXT PRIMARY KEY CHECK(length(manifest_sha256)=64),
  station_id TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
  station_timezone TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  authority_scope TEXT NOT NULL CHECK(authority_scope IN ('FORMAL_PROMOTED','PRODUCTION_NO_LIVE_CANDIDATE')),
  CHECK(valid_from_utc<valid_until_utc),
  FOREIGN KEY(policy_version) REFERENCES basket_cash_phase_policy_artifact(policy_version)
);
CREATE INDEX IF NOT EXISTS basket_cash_phase_authority_scope_idx
ON basket_cash_phase_authority_manifest(station_id,authority_scope,valid_from_utc,valid_until_utc);

CREATE TABLE IF NOT EXISTS basket_strict_delta_policy_artifact (
  artifact_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('*','P0','P0-M','P1')),
  season_bin TEXT NOT NULL,
  maturity_bin TEXT NOT NULL,
  liquidity_bin TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  trained_through_utc TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  authority_scope TEXT NOT NULL
    CHECK(authority_scope IN ('PRODUCTION_NO_LIVE_CANDIDATE','FORMAL_PROMOTED')),
  artifact_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64)
);

CREATE INDEX IF NOT EXISTS basket_strict_delta_artifact_scope_idx
ON basket_strict_delta_policy_artifact(
  station_id,stage,season_bin,maturity_bin,liquidity_bin,strategy_version,
  valid_from_utc,valid_until_utc
);

CREATE TABLE IF NOT EXISTS basket_strict_delta_policy_promotion (
  promotion_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  decision_seq INTEGER NOT NULL CHECK(decision_seq>=1),
  decision TEXT NOT NULL CHECK(decision IN ('CANDIDATE','PROMOTED','REJECTED','SUPERSEDED')),
  gate_version TEXT NOT NULL,
  decided_at_utc TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(artifact_id,decision_seq)
);

CREATE TABLE IF NOT EXISTS basket_strict_delta_policy_activation (
  activation_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('*','P0','P0-M','P1')),
  season_bin TEXT NOT NULL,
  maturity_bin TEXT NOT NULL,
  liquidity_bin TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  activated_at_utc TEXT NOT NULL,
  authority_scope TEXT NOT NULL
    CHECK(authority_scope IN ('PRODUCTION_NO_LIVE_CANDIDATE','FORMAL_PROMOTED')),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(
    station_id,stage,season_bin,maturity_bin,liquidity_bin,strategy_version,
    authority_scope,generation
  )
);

CREATE INDEX IF NOT EXISTS basket_strict_delta_activation_scope_idx
ON basket_strict_delta_policy_activation(
  station_id,stage,season_bin,maturity_bin,liquidity_bin,strategy_version,
  authority_scope,valid_from_utc,valid_until_utc,generation
);

-- P0 may select an initial basket before the target ET day starts. This is a
-- non-authorizing plan SSOT, not an execution decision: P1 remains the final
-- authority and only auto_trade_decisions may block/authorize execution.
CREATE TABLE IF NOT EXISTS basket_p0_preselection_plan (
  preselection_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  checkpoint_id TEXT NOT NULL,
  probability_stage TEXT NOT NULL CHECK(probability_stage IN ('P0','P0-M')),
  decision_at_utc TEXT NOT NULL,
  quote_observed_at_utc TEXT NOT NULL,
  quote_manifest_sha256 TEXT NOT NULL CHECK(length(quote_manifest_sha256)=64),
  policy_activation_id TEXT NOT NULL,
  policy_authority_scope TEXT NOT NULL CHECK(policy_authority_scope='PRODUCTION_NO_LIVE_CANDIDATE'),
  plan_json TEXT NOT NULL,
  plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256)=64),
  state TEXT NOT NULL CHECK(state='PRELIMINARY_NO_LIVE'),
  final_authority TEXT NOT NULL CHECK(final_authority='P1'),
  live_submit_authorized INTEGER NOT NULL CHECK(live_submit_authorized=0),
  calls_order_gateway INTEGER NOT NULL CHECK(calls_order_gateway=0),
  orders_submitted INTEGER NOT NULL CHECK(orders_submitted=0),
  created_at TEXT NOT NULL,
  UNIQUE(station_id,event_date,checkpoint_id,quote_manifest_sha256)
);

CREATE INDEX IF NOT EXISTS basket_p0_preselection_scope_idx
ON basket_p0_preselection_plan(station_id,event_date,decision_at_utc,quote_observed_at_utc);

CREATE TRIGGER IF NOT EXISTS basket_probability_envelope_no_update BEFORE UPDATE ON basket_probability_envelope BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_probability_envelope_no_delete BEFORE DELETE ON basket_probability_envelope BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_official_ws_snapshot_no_update BEFORE UPDATE ON basket_official_ws_snapshot BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_official_ws_snapshot_no_delete BEFORE DELETE ON basket_official_ws_snapshot BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_inventory_snapshot_no_update BEFORE UPDATE ON basket_confirmed_inventory_snapshot BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_inventory_snapshot_no_delete BEFORE DELETE ON basket_confirmed_inventory_snapshot BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_order_state_snapshot_no_update BEFORE UPDATE ON basket_order_state_snapshot BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_order_state_snapshot_no_delete BEFORE DELETE ON basket_order_state_snapshot BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_cash_phase_policy_no_update BEFORE UPDATE ON basket_cash_phase_policy_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_cash_phase_policy_no_delete BEFORE DELETE ON basket_cash_phase_policy_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_cash_phase_authority_no_update BEFORE UPDATE ON basket_cash_phase_authority_manifest BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_cash_phase_authority_no_delete BEFORE DELETE ON basket_cash_phase_authority_manifest BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_strict_delta_artifact_no_update BEFORE UPDATE ON basket_strict_delta_policy_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_strict_delta_artifact_no_delete BEFORE DELETE ON basket_strict_delta_policy_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_strict_delta_promotion_no_update BEFORE UPDATE ON basket_strict_delta_policy_promotion BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_strict_delta_promotion_no_delete BEFORE DELETE ON basket_strict_delta_policy_promotion BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_strict_delta_activation_no_update BEFORE UPDATE ON basket_strict_delta_policy_activation BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_strict_delta_activation_no_delete BEFORE DELETE ON basket_strict_delta_policy_activation BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_p0_preselection_no_update BEFORE UPDATE ON basket_p0_preselection_plan BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_p0_preselection_no_delete BEFORE DELETE ON basket_p0_preselection_plan BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""

# M5 is a separate, explicitly migrated ledger boundary.  It is part of the
# target SSOT contract, but no resident service calls this DDL while running.
# ``auto_trade_decisions`` is deliberately the only table that carries an
# execution-blocking decision; the other tables only carry immutable facts or
# lifecycle state.

# Indexes whose tables are created by a later DDL block, or by a different
# migration path entirely.
#
# SQLite refuses ``CREATE INDEX`` on a table that does not exist yet, and the
# statements are executed one at a time, so a forward reference is a hard
# failure rather than something the ``IF NOT EXISTS`` absorbs.  Every one of
# these lived in ``PRODUCTION_TARGET_DDL``, which runs before
# ``PRODUCTION_LEDGER_DDL`` and ``PROBABILITY_EVIDENCE_DDL`` and does not run
# ``REPUBLISHER_ENSEMBLE_DDL`` at all -- so a schema built from scratch died
# on ``no such table: main.auto_trade_decisions``, and the six republisher
# indexes below could never be created by any entry point.  Production
# databases carry all eleven from older piecemeal migrations, which is why
# nothing in service noticed; what it cost was every test that builds a
# schema, and it would have cost the next fresh install its indexes -- the
# same class of loss as ``envelope-checkpoint-index-stall``.
#
# Applied after every table exists, and skipping any whose table this
# particular path does not create.  ``apply_target_ssot_schema`` builds the
# republisher tables and ``apply_production_ssot_schema`` does not; each gets
# the indexes for the tables it actually has.
DEFERRED_INDEX_DDL = """
-- The decision and cycle ledgers are read per station-day on every
-- production cycle, and both were scanned end to end: 37k rows each, forty
-- five stations a pass, and the pass stopped returning at all.  Measured
-- 2026-08-15 the two scans cost 399ms and 119ms per scope against 1.6ms for
-- the indexed snapshot read beside them, and the cycle sat in sqlite3_step
-- for minutes writing nothing.  Both are append-only and grow every cycle,
-- so the scan gets worse on its own.
CREATE INDEX IF NOT EXISTS auto_trade_decisions_scope_idx
ON auto_trade_decisions(station_id,event_date,created_at);
CREATE INDEX IF NOT EXISTS basket_target_execution_cycles_scope_idx
ON basket_target_execution_cycles(station_id,event_date,created_at);
-- The republisher archive is read on the same path and is the largest table
-- in the database: 1.08M rows, scanned per station-day at 2,899ms a scope --
-- more than twice the native archive it sits beside, and the reason the
-- cycle still would not return after the native indexes landed.  Measured
-- 2026-08-15: 2899ms -> 8.1ms and 636ms -> 0.1ms.
CREATE INDEX IF NOT EXISTS
  calibration_republisher_ensemble_historical_member_run_scope_idx
ON calibration_republisher_ensemble_historical_member_run(
  station_id,target_local_date,ensemble_system_id
);
-- The d2 coverage check counts distinct systems for one target day, and the
-- scope index above leads with station_id, which that question does not
-- constrain.  So it fell back to a UNIQUE autoindex and swept 1.3M rows to
-- return the number 3: 2.3 seconds uncontended, measured at 142 seconds
-- behind a competing writer on 2026-08-22.  Leading with the day makes it a
-- covering seek: 2300ms -> 0.6ms.
CREATE INDEX IF NOT EXISTS
  calibration_republisher_historical_d2_coverage_idx
ON calibration_republisher_ensemble_historical_member_run(
  target_local_date,archive_lead_bucket,extraction_version,ensemble_system_id
);
CREATE INDEX IF NOT EXISTS
  calibration_republisher_ensemble_member_run_scope_idx
ON calibration_republisher_ensemble_member_run(
  station_id,target_local_date,ensemble_system_id
);
-- The OOS sweep asks both member-run lanes the same two questions every
-- cycle -- which stations are candidates, and what truth each one has -- and
-- both start from a UNION filtered on authority_scope, which no index led
-- with.  That is a scan of 467k rows twice per cycle (measured 2026-08-16:
-- 2.07s and 1.60s).  Leading with authority_scope and carrying the scope
-- pair makes both queries covering.
CREATE INDEX IF NOT EXISTS
  calibration_republisher_ensemble_member_run_authority_idx
ON calibration_republisher_ensemble_member_run(
  authority_scope,station_id,target_local_date
);
CREATE INDEX IF NOT EXISTS basket_target_observation_facts_scope_idx
ON basket_target_observation_facts(station_id,event_date);
CREATE INDEX IF NOT EXISTS basket_probability_update_proof_scope_idx
ON basket_probability_update_proof(station_id,event_date);
CREATE INDEX IF NOT EXISTS
  calibration_republisher_ensemble_member_bucket_scope_idx
ON calibration_republisher_ensemble_member_bucket(
  station_id,target_local_date
);
CREATE INDEX IF NOT EXISTS
  calibration_republisher_historical_oos_contract_scope_idx
ON calibration_republisher_historical_oos_contract(
  station_id,target_local_date
);
CREATE INDEX IF NOT EXISTS
  calibration_republisher_ensemble_historical_member_bucket_scope_idx
ON calibration_republisher_ensemble_historical_member_bucket(
  station_id,target_local_date
);

-- Every blocked-scope decision re-reads its own cycle's fact binding, and
-- (cycle_id,decision_id) led no index -- so each of the 45 scopes in a cycle
-- scanned the whole table, whose rows carry policy_version_vector_json and
-- feature_json in overflow pages.  It runs inside ``BEGIN IMMEDIATE``, which
-- made it the single largest holder of the one writer lock in the system:
-- measured 2026-08-17, 45 calls x 2,295ms = 103s of held lock per cycle,
-- 61% of all lock time and 99.7% of the transaction it sits in.  Carrying
-- the three selected columns makes it covering, and leading fact_id after
-- the scope pair also serves the query's ORDER BY.
CREATE INDEX IF NOT EXISTS basket_policy_training_fact_cycle_idx
ON basket_policy_training_fact(
  cycle_id,decision_id,fact_id,content_sha256,decision_scope_key
);

-- The decision sweep lists every committed scope once per cycle and needs
-- settlement_timezone to decide whether the day is still open.  That one
-- column is the only thing the as-of index above does not carry, so the plan
-- stayed an index scan but paid a table lookup per entry -- and the rows it
-- lands on carry probabilities/lineage JSON in overflow pages.  Measured
-- 2026-08-16 at 59,120 rows: 13.0-15.6s per cycle, against 0.48s for the
-- identical query with settlement_timezone dropped.  Leading with state and
-- then the sweep's own ORDER BY also retires the temp b-tree.
CREATE INDEX IF NOT EXISTS basket_probability_envelope_committed_scope_idx
ON basket_probability_envelope(state,event_date,station_id,settlement_timezone);
-- The resident WS publisher's scope query selects five more columns than the
-- index above carries, so it kept paying a table lookup per entry onto rows
-- whose probabilities/lineage JSON sits in overflow pages.  Measured
-- 2026-08-21 at 148,578 committed rows: 36-40s per call -- and that call sits
-- in the publisher's socket event loop on a 5s probe, so the loop was blocked
-- for longer than the venue's patience.  The books went 75-250s between
-- observations against a 60s lease and the venue twice closed us with
-- "1013 slow consumer: send buffer full".  Carrying every column the query
-- reads keeps the plan inside the index.
CREATE INDEX IF NOT EXISTS basket_probability_envelope_publisher_scope_idx
ON basket_probability_envelope(
  state,event_date,station_id,decision_at_utc,stage,envelope_id,
  settlement_timezone,checkpoint_id,fresh_until_utc
);
"""


DELTA_V2_AUTHORITY_DDL = """
CREATE TABLE IF NOT EXISTS basket_delta_v2_policy_artifact (
  artifact_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('*','P0','P0-M','P1')),
  season_bin TEXT NOT NULL,
  maturity_bin TEXT NOT NULL,
  liquidity_bin TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  trained_through_utc TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  authority_scope TEXT NOT NULL
    CHECK(authority_scope IN ('PRODUCTION_NO_LIVE','FORMAL_PROMOTED')),
  oos_gate_passed INTEGER NOT NULL CHECK(oos_gate_passed IN (0,1)),
  artifact_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64)
);
CREATE INDEX IF NOT EXISTS basket_delta_v2_artifact_scope_idx
ON basket_delta_v2_policy_artifact(
  station_id,stage,season_bin,maturity_bin,liquidity_bin,strategy_version,
  valid_from_utc,valid_until_utc
);
CREATE TABLE IF NOT EXISTS basket_delta_v2_policy_promotion (
  promotion_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  decision_seq INTEGER NOT NULL CHECK(decision_seq>=1),
  decision TEXT NOT NULL
    CHECK(decision IN (
      'PREREGISTERED','NO_LIVE_ACTIVATED','PROMOTED','REJECTED','SUPERSEDED'
    )),
  gate_version TEXT NOT NULL,
  decided_at_utc TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(artifact_id,decision_seq)
);
CREATE TABLE IF NOT EXISTS basket_delta_v2_policy_activation (
  activation_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('*','P0','P0-M','P1')),
  season_bin TEXT NOT NULL,
  maturity_bin TEXT NOT NULL,
  liquidity_bin TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  activated_at_utc TEXT NOT NULL,
  authority_scope TEXT NOT NULL
    CHECK(authority_scope IN ('PRODUCTION_NO_LIVE','FORMAL_PROMOTED')),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(
    station_id,stage,season_bin,maturity_bin,liquidity_bin,strategy_version,
    authority_scope,generation
  )
);
CREATE INDEX IF NOT EXISTS basket_delta_v2_activation_scope_idx
ON basket_delta_v2_policy_activation(
  station_id,stage,season_bin,maturity_bin,liquidity_bin,strategy_version,
  authority_scope,valid_from_utc,valid_until_utc,generation
);
CREATE TRIGGER IF NOT EXISTS basket_delta_v2_artifact_no_update BEFORE UPDATE ON basket_delta_v2_policy_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_delta_v2_artifact_no_delete BEFORE DELETE ON basket_delta_v2_policy_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_delta_v2_promotion_no_update BEFORE UPDATE ON basket_delta_v2_policy_promotion BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_delta_v2_promotion_no_delete BEFORE DELETE ON basket_delta_v2_policy_promotion BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_delta_v2_activation_no_update BEFORE UPDATE ON basket_delta_v2_policy_activation BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_delta_v2_activation_no_delete BEFORE DELETE ON basket_delta_v2_policy_activation BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


_DELTA_V2_PROMOTION_COLUMNS = (
    "promotion_id",
    "artifact_id",
    "decision_seq",
    "decision",
    "gate_version",
    "decided_at_utc",
    "receipt_sha256",
    "content_sha256",
)


def _upgrade_delta_v2_promotion_decision_constraint(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Add the no-live lifecycle decision without rewriting authority rows."""

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='basket_delta_v2_policy_promotion'"
    ).fetchone()
    if row is None:
        raise RuntimeError("delta_v2_policy_promotion_table_missing")
    table_sql = str(row[0] or "")
    if "NO_LIVE_ACTIVATED" in table_sql:
        return {"state": "unchanged", "rows_preserved": 0}

    actual_columns = tuple(
        str(info[1])
        for info in conn.execute(
            "PRAGMA table_info(basket_delta_v2_policy_promotion)"
        )
    )
    if actual_columns != _DELTA_V2_PROMOTION_COLUMNS:
        raise RuntimeError("delta_v2_policy_promotion_columns_incompatible")

    archive = "basket_delta_v2_policy_promotion_constraint_v1_archive"
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? LIMIT 1", (archive,)
    ).fetchone() is not None:
        raise RuntimeError("delta_v2_policy_promotion_migration_name_collision")
    row_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM basket_delta_v2_policy_promotion"
        ).fetchone()[0]
    )
    names = ",".join(_DELTA_V2_PROMOTION_COLUMNS)
    savepoint = f"delta_v2_promotion_constraint_{id(conn):x}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        conn.execute(
            "DROP TRIGGER IF EXISTS basket_delta_v2_promotion_no_update"
        )
        conn.execute(
            "DROP TRIGGER IF EXISTS basket_delta_v2_promotion_no_delete"
        )
        conn.execute(
            "ALTER TABLE basket_delta_v2_policy_promotion "
            f"RENAME TO {archive}"
        )
        conn.execute(
            """
            CREATE TABLE basket_delta_v2_policy_promotion (
              promotion_id TEXT PRIMARY KEY,
              artifact_id TEXT NOT NULL,
              decision_seq INTEGER NOT NULL CHECK(decision_seq>=1),
              decision TEXT NOT NULL CHECK(decision IN (
                'PREREGISTERED','NO_LIVE_ACTIVATED','PROMOTED',
                'REJECTED','SUPERSEDED'
              )),
              gate_version TEXT NOT NULL,
              decided_at_utc TEXT NOT NULL,
              receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
              content_sha256 TEXT NOT NULL UNIQUE
                CHECK(length(content_sha256)=64),
              UNIQUE(artifact_id,decision_seq)
            )
            """
        )
        conn.execute(
            f"INSERT INTO basket_delta_v2_policy_promotion({names}) "
            f"SELECT {names} FROM {archive}"
        )
        copied = int(
            conn.execute(
                "SELECT COUNT(*) FROM basket_delta_v2_policy_promotion"
            ).fetchone()[0]
        )
        if copied != row_count:
            raise RuntimeError(
                "delta_v2_policy_promotion_row_count_drift"
            )
        conn.execute(f"DROP TABLE {archive}")
        conn.execute(
            "CREATE TRIGGER basket_delta_v2_promotion_no_update "
            "BEFORE UPDATE ON basket_delta_v2_policy_promotion "
            "BEGIN SELECT RAISE(ABORT,'append_only'); END"
        )
        conn.execute(
            "CREATE TRIGGER basket_delta_v2_promotion_no_delete "
            "BEFORE DELETE ON basket_delta_v2_policy_promotion "
            "BEGIN SELECT RAISE(ABORT,'append_only'); END"
        )
    except BaseException:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    conn.execute(f"RELEASE {savepoint}")
    return {"state": "migrated", "rows_preserved": row_count}


PRODUCTION_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS basket_target_observation_facts (
  observation_fact_id TEXT PRIMARY KEY,
  observation_id TEXT NOT NULL UNIQUE,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  -- AMSC is retained history; AWC is the registry lane; MADIS_HFMETAR and
  -- ASOS_1MIN are the scheduled ASOS substitute carriers, admitted with
  -- their exact report-type pairing (enforced in the ledger code path).
  source_lane TEXT NOT NULL
    CHECK(source_lane IN ('AMSC','AWC','MADIS_HFMETAR','ASOS_1MIN')),
  report_type TEXT NOT NULL
    CHECK(report_type IN ('METAR','SPECI','ASOS_HF','ASOS_1MIN')),
  observed_at_utc TEXT,
  raw_sha256 TEXT NOT NULL CHECK(length(raw_sha256)=64),
  awc_source_receipt_sha256 TEXT,
  canonical_committed_at TEXT NOT NULL,
  truth_observation_revision_content_sha256 TEXT
    CHECK(length(truth_observation_revision_content_sha256)=64),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS basket_target_bound_facts (
  bound_fact_id TEXT PRIMARY KEY,
  bound_id TEXT NOT NULL UNIQUE,
  observation_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  bound_generation INTEGER NOT NULL CHECK(bound_generation>=0),
  hard_lower_bound INTEGER,
  bound_json TEXT NOT NULL,
  truth_observation_revision_content_sha256 TEXT
    CHECK(length(truth_observation_revision_content_sha256)=64),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  created_at TEXT NOT NULL,
  FOREIGN KEY(observation_id) REFERENCES basket_target_observation_facts(observation_id)
);

-- The chain root for a stage that has not observed anything yet.  The
-- observation/bound pair above is METAR-only by construction -- its CHECK
-- constraints name the report types -- so P0 opening on the NWP ensemble and
-- P0-M re-confirming on a TAF revision had nowhere to record what they stand
-- on.  These two tables are that record, and they are deliberately separate
-- from the observed pair rather than a relaxation of it: an observed high
-- rules buckets out permanently, and nothing here may ever weaken that.
CREATE TABLE IF NOT EXISTS basket_target_stage_evidence_roots (
  evidence_root_id TEXT PRIMARY KEY,
  probability_stage TEXT NOT NULL CHECK(probability_stage IN ('P0','P0-M','P1')),
  position_role TEXT NOT NULL CHECK(position_role IN ('OPEN','ADJUST')),
  bound_kind TEXT NOT NULL CHECK(
    bound_kind IN ('MODEL_ENVELOPE_SOFT','OBSERVED_REPORTED_HIGH_HARD')
  ),
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  checkpoint_id TEXT NOT NULL,
  posterior_sha256 TEXT NOT NULL CHECK(length(posterior_sha256)=64),
  evidence_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS basket_target_stage_bound_facts (
  stage_bound_fact_id TEXT PRIMARY KEY,
  bound_id TEXT NOT NULL UNIQUE,
  evidence_root_id TEXT NOT NULL,
  probability_stage TEXT NOT NULL CHECK(probability_stage IN ('P0','P0-M')),
  bound_kind TEXT NOT NULL CHECK(bound_kind='MODEL_ENVELOPE_SOFT'),
  soft_lower_ordinal INTEGER NOT NULL,
  soft_upper_ordinal INTEGER NOT NULL
    CHECK(soft_upper_ordinal>=soft_lower_ordinal),
  admitted_buckets_json TEXT NOT NULL,
  excluded_buckets_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  created_at TEXT NOT NULL,
  FOREIGN KEY(evidence_root_id)
    REFERENCES basket_target_stage_evidence_roots(evidence_root_id)
);

CREATE TRIGGER IF NOT EXISTS basket_target_stage_evidence_roots_append_only
BEFORE UPDATE ON basket_target_stage_evidence_roots
BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER IF NOT EXISTS basket_target_stage_bound_facts_append_only
BEFORE UPDATE ON basket_target_stage_bound_facts
BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE IF NOT EXISTS basket_target_execution_cycles (
  cycle_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL UNIQUE,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  strategy_lane TEXT NOT NULL CHECK(strategy_lane='basket'),
  plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256)=64),
  requested_reserve_usdc REAL NOT NULL CHECK(requested_reserve_usdc>=0),
  state TEXT NOT NULL CHECK(state IN ('prepared','submitting','submitted','reconcile_required','settled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auto_trade_decisions (
  decision_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL UNIQUE,
  plan_id TEXT NOT NULL UNIQUE,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  strategy_lane TEXT NOT NULL CHECK(strategy_lane='basket'),
  decided_state TEXT NOT NULL CHECK(decided_state IN ('prepared','blocked','submitting','submitted','reconcile_required','settled')),
  will_order INTEGER NOT NULL CHECK(will_order IN (0,1)),
  target_execution_hard_deadline_at TEXT,
  reason_codes_json TEXT NOT NULL,
  details_json TEXT NOT NULL,
  decision_sha256 TEXT NOT NULL CHECK(length(decision_sha256)=64),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS basket_target_order_intents (
  order_intent_id TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL,
  cycle_id TEXT NOT NULL,
  bucket TEXT NOT NULL,
  token_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side='YES'),
  order_action TEXT NOT NULL CHECK(order_action IN ('BUY','SELL')),
  limit_price REAL NOT NULL CHECK(limit_price>0 AND limit_price<1),
  requested_size REAL NOT NULL CHECK(requested_size>0),
  requested_notional_usdc REAL NOT NULL CHECK(requested_notional_usdc>0),
  state TEXT NOT NULL CHECK(state IN ('prepared','submitting','submitted','reconcile_required','filled','cancelled','failed')),
  -- Widened by the 2026-08-30 go-live migration on the production
  -- database; the DDL that creates a new one was left at =0, so a
  -- rebuilt or test database silently reverted to no-live and the
  -- ledger could not write the live flag it had just been taught to
  -- carry.  A migrated table and the statement that creates its
  -- replacement have to say the same thing.
  live_submit_authorized INTEGER NOT NULL CHECK(live_submit_authorized IN (0,1)),
  target_execution_hard_deadline_at TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  intent_json TEXT NOT NULL,
  intent_sha256 TEXT NOT NULL CHECK(length(intent_sha256)=64),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS basket_target_capital_pool (
  pool_id INTEGER PRIMARY KEY CHECK(pool_id=1),
  balance_usdc REAL NOT NULL CHECK(balance_usdc>=0),
  reserved_usdc REAL NOT NULL CHECK(reserved_usdc>=0),
  capital_epoch_id TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version>=0),
  updated_at TEXT NOT NULL,
  CHECK(reserved_usdc<=balance_usdc)
);

CREATE TABLE IF NOT EXISTS basket_target_capital_reservations (
  reservation_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL UNIQUE,
  strategy_lane TEXT NOT NULL CHECK(strategy_lane='basket'),
  amount_usdc REAL NOT NULL CHECK(amount_usdc>=0),
  state TEXT NOT NULL CHECK(state IN ('active','released','consumed','expired')),
  idempotency_key TEXT NOT NULL UNIQUE,
  reservation_sha256 TEXT NOT NULL CHECK(length(reservation_sha256)=64),
  release_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Pre-pure-YES databases may still contain historical hard-NO rows. Keep
-- them readable, but fence every future write at the database boundary as
-- well as in ``atomic_ledger``. INSERT-only guards never mutate legacy rows.
CREATE TRIGGER IF NOT EXISTS basket_target_cycles_no_retired_lane_insert
BEFORE INSERT ON basket_target_execution_cycles
WHEN lower(NEW.strategy_lane) <> 'basket'
BEGIN SELECT RAISE(ABORT,'strategy_lane_hard_no_retired'); END;

CREATE TRIGGER IF NOT EXISTS basket_target_decisions_no_retired_lane_insert
BEFORE INSERT ON auto_trade_decisions
WHEN lower(NEW.strategy_lane) <> 'basket'
BEGIN SELECT RAISE(ABORT,'strategy_lane_hard_no_retired'); END;

CREATE TRIGGER IF NOT EXISTS basket_target_reservations_no_retired_lane_insert
BEFORE INSERT ON basket_target_capital_reservations
WHEN lower(NEW.strategy_lane) <> 'basket'
BEGIN SELECT RAISE(ABORT,'strategy_lane_hard_no_retired'); END;

CREATE TRIGGER IF NOT EXISTS basket_target_intents_no_no_side_insert
BEFORE INSERT ON basket_target_order_intents
WHEN upper(NEW.side) <> 'YES'
BEGIN SELECT RAISE(ABORT,'no_order_side_retired'); END;

CREATE TABLE IF NOT EXISTS basket_target_prepared_supersession_receipts (
  supersession_id TEXT PRIMARY KEY,
  old_cycle_id TEXT NOT NULL UNIQUE,
  old_decision_id TEXT NOT NULL,
  old_training_fact_id TEXT NOT NULL,
  old_decision_scope_key TEXT NOT NULL,
  new_cycle_id TEXT NOT NULL UNIQUE,
  new_decision_id TEXT NOT NULL UNIQUE,
  new_decision_scope_key TEXT NOT NULL,
  reservation_id TEXT NOT NULL,
  released_amount_usdc REAL NOT NULL CHECK(released_amount_usdc>=0),
  cancelled_intent_ids_json TEXT NOT NULL,
  reason TEXT NOT NULL CHECK(reason='authority_input_superseded'),
  superseded_at TEXT NOT NULL,
  receipt_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  FOREIGN KEY(old_cycle_id) REFERENCES basket_target_execution_cycles(cycle_id),
  FOREIGN KEY(old_decision_id) REFERENCES auto_trade_decisions(decision_id),
  FOREIGN KEY(old_training_fact_id) REFERENCES basket_policy_training_fact(fact_id),
  FOREIGN KEY(new_cycle_id) REFERENCES basket_target_execution_cycles(cycle_id),
  FOREIGN KEY(new_decision_id) REFERENCES auto_trade_decisions(decision_id),
  FOREIGN KEY(reservation_id) REFERENCES basket_target_capital_reservations(reservation_id),
  CHECK(old_decision_scope_key<>new_decision_scope_key)
);

CREATE TABLE IF NOT EXISTS basket_target_capital_epoch_migrations (
  migration_id TEXT PRIMARY KEY,
  schema TEXT NOT NULL,
  previous_epoch_id TEXT NOT NULL,
  next_epoch_id TEXT NOT NULL UNIQUE,
  previous_balance_usdc REAL NOT NULL CHECK(previous_balance_usdc>=0),
  next_balance_usdc REAL NOT NULL CHECK(next_balance_usdc>0),
  previous_version INTEGER NOT NULL CHECK(previous_version>=0),
  next_version INTEGER NOT NULL CHECK(next_version=previous_version+1),
  zero_reservations_released INTEGER NOT NULL
    CHECK(zero_reservations_released>=0),
  runtime_contract_sha256 TEXT NOT NULL
    CHECK(length(runtime_contract_sha256)=64),
  migration_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS basket_target_execution_version_vectors (
  order_intent_id TEXT PRIMARY KEY,
  cycle_id TEXT,
  decision_id TEXT,
  training_fact_id TEXT,
  vector_policy TEXT NOT NULL
    CHECK(vector_policy IN ('EXECUTION','NO_EXECUTION')),
  target_price_id TEXT,
  quote_observed_at TEXT,
  quote_expires_at TEXT,
  probability_sha256 TEXT,
  official_ws_quote_sha256 TEXT,
  inventory_sha256 TEXT,
  order_state_sha256 TEXT,
  station_policy_sha256 TEXT,
  cash_policy_decision_sha256 TEXT,
  strict_delta_artifact_sha256 TEXT,
  economic_policy_schema TEXT,
  economic_policy_artifact_sha256 TEXT,
  vector_json TEXT NOT NULL DEFAULT '{}',
  vector_sha256 TEXT NOT NULL CHECK(length(vector_sha256)=64),
  created_at TEXT NOT NULL,
  FOREIGN KEY(order_intent_id) REFERENCES basket_target_order_intents(order_intent_id),
  FOREIGN KEY(cycle_id) REFERENCES basket_target_execution_cycles(cycle_id),
  FOREIGN KEY(decision_id) REFERENCES auto_trade_decisions(decision_id),
  CHECK(
    (
      vector_policy='EXECUTION'
      AND order_intent_id IS NOT NULL
      AND cycle_id IS NOT NULL
      AND decision_id IS NOT NULL
      AND probability_sha256 IS NOT NULL
      AND official_ws_quote_sha256 IS NOT NULL
      AND inventory_sha256 IS NOT NULL
      AND order_state_sha256 IS NOT NULL
      AND station_policy_sha256 IS NOT NULL
      AND cash_policy_decision_sha256 IS NOT NULL
      AND (
        (
          strict_delta_artifact_sha256 IS NOT NULL
          AND economic_policy_schema IS NULL
          AND economic_policy_artifact_sha256 IS NULL
        )
        OR
        (
          strict_delta_artifact_sha256 IS NULL
          AND economic_policy_schema='delta_basket_score_policy_artifact_v2'
          AND economic_policy_artifact_sha256 IS NOT NULL
        )
      )
      AND target_price_id IS NOT NULL
      AND quote_observed_at IS NOT NULL
      AND quote_expires_at IS NOT NULL
    )
    OR (
      vector_policy='NO_EXECUTION'
      AND order_intent_id IS NULL
      AND cycle_id IS NOT NULL
      AND decision_id IS NOT NULL
      AND target_price_id IS NULL
      AND quote_observed_at IS NULL
      AND quote_expires_at IS NULL
      AND strict_delta_artifact_sha256 IS NULL
      AND economic_policy_schema IS NULL
      AND economic_policy_artifact_sha256 IS NULL
    )
  )
);

CREATE TABLE IF NOT EXISTS basket_target_order_events (
  event_id TEXT PRIMARY KEY,
  order_intent_id TEXT NOT NULL,
  exchange_order_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN ('POST_ACK','FILL','PARTIAL_FILL','CANCEL_ACK','UNKNOWN_POST','LATE_FILL','EXPIRED')),
  observed_at TEXT NOT NULL,
  matched_size REAL NOT NULL CHECK(matched_size>=0),
  matched_notional_usdc REAL NOT NULL CHECK(matched_notional_usdc>=0),
  event_json TEXT NOT NULL,
  event_sha256 TEXT NOT NULL CHECK(length(event_sha256)=64),
  UNIQUE(order_intent_id,event_sha256)
);

CREATE TABLE IF NOT EXISTS basket_target_order_reconciliations (
  order_intent_id TEXT PRIMARY KEY,
  exchange_order_id TEXT,
  state TEXT NOT NULL CHECK(state IN ('pending','unknown','resolved_filled','resolved_cancelled','resolved_partial')),
  filled_size REAL NOT NULL CHECK(filled_size>=0),
  filled_notional_usdc REAL NOT NULL CHECK(filled_notional_usdc>=0),
  last_event_id TEXT,
  blocking_reason TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(order_intent_id) REFERENCES basket_target_order_intents(order_intent_id)
);

-- Bounded target-owned writer telemetry.  Keep one aggregate row instead of
-- an append-only event log so diagnostics cannot consume the target disk.
CREATE TABLE IF NOT EXISTS basket_target_writer_lock_wait_telemetry (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  sample_count INTEGER NOT NULL CHECK(sample_count>=1),
  total_wait_ms REAL NOT NULL CHECK(total_wait_ms>=0),
  max_wait_ms REAL NOT NULL CHECK(max_wait_ms>=0),
  last_wait_ms REAL NOT NULL CHECK(last_wait_ms>=0),
  last_operation TEXT NOT NULL,
  last_acquired_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)
);

CREATE INDEX IF NOT EXISTS basket_target_decision_blocker_idx
ON auto_trade_decisions(decided_state,will_order,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS basket_target_no_execution_vector_cycle_idx
ON basket_target_execution_version_vectors(cycle_id)
WHERE vector_policy='NO_EXECUTION';

-- The index above is partial, so it serves only queries that themselves
-- promise vector_policy='NO_EXECUTION'.  The blocked-decision path does not
-- make that promise -- it reads the row by cycle_id precisely in order to
-- check the policy -- so both of its lookups fell back to scanning all
-- 41,841 rows, and the rows carry vector_json.  Both run inside
-- ``BEGIN IMMEDIATE``, 45 blocked scopes a cycle, which is how a decision
-- write came to hold the single writer lock for seconds at a time.
-- Measured 2026-08-16: 408-553ms cold per lookup, 6ms warm.
CREATE INDEX IF NOT EXISTS basket_target_execution_version_vector_cycle_idx
ON basket_target_execution_version_vectors(cycle_id);
CREATE INDEX IF NOT EXISTS basket_target_order_intent_decision_idx
ON basket_target_order_intents(decision_id,cycle_id,created_at);
CREATE INDEX IF NOT EXISTS basket_target_reservation_state_idx
ON basket_target_capital_reservations(state,updated_at);
CREATE INDEX IF NOT EXISTS basket_target_prepared_supersession_new_scope_idx
ON basket_target_prepared_supersession_receipts(
  new_decision_scope_key,superseded_at
);
CREATE INDEX IF NOT EXISTS basket_target_order_event_intent_idx
ON basket_target_order_events(order_intent_id,observed_at,event_id);
CREATE INDEX IF NOT EXISTS basket_target_reconciliation_state_idx
ON basket_target_order_reconciliations(state,updated_at);
CREATE INDEX IF NOT EXISTS basket_target_writer_lock_wait_updated_idx
ON basket_target_writer_lock_wait_telemetry(updated_at);
CREATE TRIGGER IF NOT EXISTS basket_target_observation_no_update
BEFORE UPDATE ON basket_target_observation_facts BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_target_observation_no_delete
BEFORE DELETE ON basket_target_observation_facts BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_target_observation_canonical_insert_only
BEFORE INSERT ON basket_target_observation_facts
WHEN target_canonical_projection_ingress_authorized()<>1
BEGIN SELECT RAISE(ABORT,'canonical_projection_ingress_required'); END;
CREATE TRIGGER IF NOT EXISTS basket_target_bound_no_update
BEFORE UPDATE ON basket_target_bound_facts BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_target_bound_no_delete
BEFORE DELETE ON basket_target_bound_facts BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_target_bound_canonical_insert_only
BEFORE INSERT ON basket_target_bound_facts
WHEN target_canonical_projection_ingress_authorized()<>1
BEGIN SELECT RAISE(ABORT,'canonical_projection_ingress_required'); END;
CREATE TRIGGER IF NOT EXISTS auto_trade_decisions_no_update
BEFORE UPDATE ON auto_trade_decisions BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS auto_trade_decisions_no_delete
BEFORE DELETE ON auto_trade_decisions BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_target_version_vector_no_update
BEFORE UPDATE ON basket_target_execution_version_vectors BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_target_version_vector_no_delete
BEFORE DELETE ON basket_target_execution_version_vectors BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_target_order_event_no_update
BEFORE UPDATE ON basket_target_order_events BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_target_order_event_no_delete
BEFORE DELETE ON basket_target_order_events BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_target_prepared_supersession_no_update
BEFORE UPDATE ON basket_target_prepared_supersession_receipts BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_target_prepared_supersession_no_delete
BEFORE DELETE ON basket_target_prepared_supersession_receipts BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


# Open-Meteo is an explicitly labelled republisher.  These tables deliberately
# have no foreign key into the native-only system registry: sharing the model
# name is product identity, not proof of an official transport or arrival.
REPUBLISHER_ENSEMBLE_DDL = """
CREATE TABLE IF NOT EXISTS calibration_republisher_ensemble_product (
  ensemble_system_id TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL UNIQUE,
  independence_family_id TEXT NOT NULL,
  upstream_provider_id TEXT NOT NULL,
  transport_provider_id TEXT NOT NULL CHECK(transport_provider_id='OPEN_METEO'),
  source_role TEXT NOT NULL CHECK(source_role='aggregator_republisher'),
  expected_member_count INTEGER NOT NULL CHECK(expected_member_count>=2),
  member_identity_contract_version TEXT NOT NULL,
  publication_contract_version TEXT NOT NULL,
  native_arrival_index_eligible INTEGER NOT NULL DEFAULT 0
    CHECK(native_arrival_index_eligible=0),
  formal_p0_eligible INTEGER NOT NULL DEFAULT 0
    CHECK(formal_p0_eligible=0),
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64)
);

-- A republisher row is eligible for historical OOS only through an explicit,
-- immutable contract.  The contract is deliberately separate from the
-- native-arrival tables: a closed station day and an AWC truth as-of receipt
-- prove that the sample is historical, but never prove native publication or
-- formal/live authority.  New contract versions are appended when evidence
-- is corrected; existing versions are never updated in place.
CREATE TABLE IF NOT EXISTS calibration_republisher_historical_oos_contract (
  historical_oos_contract_id TEXT PRIMARY KEY,
  contract_version TEXT NOT NULL,
  ensemble_system_id TEXT NOT NULL
    REFERENCES calibration_republisher_ensemble_product(ensemble_system_id),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  target_local_date TEXT NOT NULL,
  -- d2 as well as d0.  UKMO, GOOGLE_WEATHERNEXT and NOAA_AIGEFS have no
  -- official native archive at all, so this republisher is their only route to
  -- a d2 member set, and d2 is the lead the cross-ensemble selector scores on.
  -- Admitting the lead here does not admit the evidence: eligibility still
  -- requires a separately versioned historical OOS contract.
  archive_lead_bucket TEXT NOT NULL
    CHECK(archive_lead_bucket IN ('d0','d1','d2')),
  run_cycle TEXT NOT NULL CHECK(run_cycle='AGGREGATOR_UNKNOWN_NATIVE_CYCLE'),
  station_day_closed INTEGER NOT NULL CHECK(station_day_closed=1),
  station_day_end_utc TEXT NOT NULL,
  station_day_closed_at_utc TEXT NOT NULL,
  awc_truth_id TEXT NOT NULL
    REFERENCES calibration_station_daily_high_truth(truth_id),
  awc_truth_revision_no INTEGER NOT NULL CHECK(awc_truth_revision_no>=0),
  awc_truth_asof_utc TEXT NOT NULL,
  awc_truth_source_lane TEXT NOT NULL CHECK(awc_truth_source_lane='AWC'),
  receipt_class TEXT NOT NULL
    CHECK(receipt_class='HISTORICAL_ASOF_REPUBLISHER_RECEIPT'),
  temporal_scope TEXT NOT NULL
    CHECK(temporal_scope='CLOSED_HISTORICAL_AWC_TRUTH'),
  future_event_count INTEGER NOT NULL CHECK(future_event_count=0),
  historical_oos_eligible INTEGER NOT NULL
    CHECK(historical_oos_eligible IN (0,1)),
  republisher_receipt_sha256 TEXT NOT NULL
    CHECK(length(republisher_receipt_sha256)=64),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  created_at_utc TEXT NOT NULL,
  CHECK(
    historical_oos_eligible=0
    OR (
      station_day_closed=1
      AND length(trim(station_day_end_utc))>0
      AND length(trim(station_day_closed_at_utc))>0
      AND awc_truth_source_lane='AWC'
      AND receipt_class='HISTORICAL_ASOF_REPUBLISHER_RECEIPT'
      AND temporal_scope='CLOSED_HISTORICAL_AWC_TRUTH'
      AND future_event_count=0
    )
  ),
  UNIQUE(
    ensemble_system_id,station_id,target_local_date,archive_lead_bucket,
    run_cycle,contract_version
  )
);

-- Bind the contract to the actual station/day/revision rather than trusting
-- a caller-provided source-lane label.  This check is intentionally limited
-- to the republisher contract and cannot alter native-arrival eligibility.
CREATE TRIGGER IF NOT EXISTS calibration_republisher_historical_oos_contract_truth_guard
BEFORE INSERT ON calibration_republisher_historical_oos_contract
WHEN NEW.historical_oos_eligible=1
  AND (
    NOT EXISTS (
      SELECT 1
      FROM calibration_station_daily_high_truth AS t
      WHERE t.truth_id=NEW.awc_truth_id
        AND t.station_id=NEW.station_id
        AND t.local_date=NEW.target_local_date
        AND t.revision_no=NEW.awc_truth_revision_no
        AND t.source_lane='AWC'
        AND t.qc_status='ACCEPTED'
        AND t.canonical_committed_at_utc IS NOT NULL
        AND julianday(t.canonical_committed_at_utc)
            <= julianday(NEW.awc_truth_asof_utc)
    )
    OR julianday(NEW.station_day_end_utc) IS NULL
    OR julianday(NEW.station_day_closed_at_utc) IS NULL
    OR julianday(NEW.awc_truth_asof_utc) IS NULL
    OR julianday(NEW.station_day_closed_at_utc)
       < julianday(NEW.station_day_end_utc)
    OR julianday(NEW.awc_truth_asof_utc)
       < julianday(NEW.station_day_end_utc)
  )
BEGIN
  SELECT RAISE(ABORT,'historical_republisher_oos_truth_binding_invalid');
END;

CREATE TABLE IF NOT EXISTS calibration_republisher_ensemble_member_run (
  republisher_member_run_id TEXT PRIMARY KEY,
  ensemble_system_id TEXT NOT NULL
    REFERENCES calibration_republisher_ensemble_product(ensemble_system_id),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  target_local_date TEXT NOT NULL,
  member_id TEXT NOT NULL,
  member_role TEXT NOT NULL CHECK(member_role IN ('CONTROL','PERTURBED')),
  received_at_utc TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  raw_tmax_c REAL NOT NULL,
  settlement_definition TEXT NOT NULL,
  extraction_version TEXT NOT NULL,
  upstream_provider_id TEXT NOT NULL,
  transport_provider_id TEXT NOT NULL CHECK(transport_provider_id='OPEN_METEO'),
  aggregator_snapshot_at_utc TEXT NOT NULL,
  source_url TEXT NOT NULL,
  raw_object_ref TEXT NOT NULL,
  source_object_sha256 TEXT NOT NULL CHECK(length(source_object_sha256)=64),
  qc_status TEXT NOT NULL CHECK(qc_status IN ('ACCEPTED','QUARANTINED')),
  authority_scope TEXT NOT NULL CHECK(
    authority_scope='PRODUCTION_NO_LIVE_CANDIDATE'
  ),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(
    ensemble_system_id,station_id,target_local_date,member_id,
    extraction_version,source_object_sha256
  )
);

CREATE TABLE IF NOT EXISTS calibration_republisher_ensemble_member_bucket (
  republisher_member_bucket_id TEXT PRIMARY KEY,
  ensemble_system_id TEXT NOT NULL
    REFERENCES calibration_republisher_ensemble_product(ensemble_system_id),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  target_local_date TEXT NOT NULL,
  aggregator_snapshot_at_utc TEXT NOT NULL,
  member_set_sha256 TEXT NOT NULL CHECK(length(member_set_sha256)=64),
  member_count INTEGER NOT NULL CHECK(member_count>=2),
  bucket_contract_sha256 TEXT NOT NULL CHECK(length(bucket_contract_sha256)=64),
  bucket_intervals_json TEXT NOT NULL,
  raw_bucket_probabilities_json TEXT NOT NULL,
  source_role TEXT NOT NULL CHECK(source_role='aggregator_republisher'),
  authority_scope TEXT NOT NULL CHECK(
    authority_scope='PRODUCTION_NO_LIVE_CANDIDATE'
  ),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(
    ensemble_system_id,station_id,target_local_date,
    member_set_sha256,bucket_contract_sha256
  )
);

CREATE TABLE IF NOT EXISTS calibration_republisher_ensemble_historical_member_run (
  historical_member_run_id TEXT PRIMARY KEY,
  ensemble_system_id TEXT NOT NULL
    REFERENCES calibration_republisher_ensemble_product(ensemble_system_id),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  target_local_date TEXT NOT NULL,
  -- d2 as well as d0.  UKMO, GOOGLE_WEATHERNEXT and NOAA_AIGEFS have no
  -- official native archive at all, so this republisher is their only route to
  -- a d2 member set, and d2 is the lead the cross-ensemble selector scores on.
  -- Admitting the lead here does not admit the evidence: eligibility still
  -- requires a separately versioned historical OOS contract.
  archive_lead_bucket TEXT NOT NULL
    CHECK(archive_lead_bucket IN ('d0','d1','d2')),
  run_cycle TEXT NOT NULL CHECK(run_cycle='AGGREGATOR_UNKNOWN_NATIVE_CYCLE'),
  member_id TEXT NOT NULL,
  member_role TEXT NOT NULL CHECK(member_role IN ('CONTROL','PERTURBED')),
  raw_tmax_c REAL NOT NULL,
  settlement_definition TEXT NOT NULL,
  extraction_version TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  transport_provider_id TEXT NOT NULL CHECK(transport_provider_id='OPEN_METEO'),
  native_run_init_utc TEXT CHECK(native_run_init_utc IS NULL),
  offline_oos_eligible INTEGER NOT NULL DEFAULT 0
    CHECK(offline_oos_eligible=0),
  archive_query_at_utc TEXT NOT NULL,
  source_url TEXT NOT NULL,
  raw_object_ref TEXT NOT NULL,
  source_object_sha256 TEXT NOT NULL CHECK(length(source_object_sha256)=64),
  qc_status TEXT NOT NULL CHECK(qc_status IN ('ACCEPTED','QUARANTINED')),
  authority_scope TEXT NOT NULL CHECK(authority_scope='CALIBRATION_TRAIN_ONLY'),
  historical_oos_contract_id TEXT
    REFERENCES calibration_republisher_historical_oos_contract(
      historical_oos_contract_id
    ),
  historical_oos_eligible INTEGER NOT NULL DEFAULT 0
    CHECK(historical_oos_eligible IN (0,1)),
  holdout_eligible INTEGER NOT NULL CHECK(holdout_eligible=0),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  CHECK(historical_oos_eligible=0 OR historical_oos_contract_id IS NOT NULL),
  UNIQUE(
    ensemble_system_id,station_id,target_local_date,archive_lead_bucket,
    member_id,extraction_version,source_object_sha256
  )
);

CREATE TABLE IF NOT EXISTS calibration_republisher_ensemble_historical_member_bucket (
  historical_member_bucket_id TEXT PRIMARY KEY,
  ensemble_system_id TEXT NOT NULL
    REFERENCES calibration_republisher_ensemble_product(ensemble_system_id),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  target_local_date TEXT NOT NULL,
  -- d2 as well as d0.  UKMO, GOOGLE_WEATHERNEXT and NOAA_AIGEFS have no
  -- official native archive at all, so this republisher is their only route to
  -- a d2 member set, and d2 is the lead the cross-ensemble selector scores on.
  -- Admitting the lead here does not admit the evidence: eligibility still
  -- requires a separately versioned historical OOS contract.
  archive_lead_bucket TEXT NOT NULL
    CHECK(archive_lead_bucket IN ('d0','d1','d2')),
  archive_query_at_utc TEXT NOT NULL,
  member_set_sha256 TEXT NOT NULL CHECK(length(member_set_sha256)=64),
  member_count INTEGER NOT NULL CHECK(member_count>=2),
  bucket_contract_sha256 TEXT NOT NULL CHECK(length(bucket_contract_sha256)=64),
  bucket_intervals_json TEXT NOT NULL,
  raw_bucket_probabilities_json TEXT NOT NULL,
  source_role TEXT NOT NULL CHECK(source_role='aggregator_republisher'),
  authority_scope TEXT NOT NULL CHECK(authority_scope='CALIBRATION_TRAIN_ONLY'),
  historical_oos_contract_id TEXT
    REFERENCES calibration_republisher_historical_oos_contract(
      historical_oos_contract_id
    ),
  historical_oos_eligible INTEGER NOT NULL DEFAULT 0
    CHECK(historical_oos_eligible IN (0,1)),
  holdout_eligible INTEGER NOT NULL CHECK(holdout_eligible=0),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  CHECK(historical_oos_eligible=0 OR historical_oos_contract_id IS NOT NULL),
  UNIQUE(
    ensemble_system_id,station_id,target_local_date,archive_lead_bucket,
    member_set_sha256,bucket_contract_sha256
  )
);

CREATE INDEX IF NOT EXISTS calibration_republisher_member_scope_idx
ON calibration_republisher_ensemble_member_run(
  ensemble_system_id,station_id,target_local_date,aggregator_snapshot_at_utc,member_id
);
CREATE INDEX IF NOT EXISTS calibration_republisher_member_bucket_scope_idx
ON calibration_republisher_ensemble_member_bucket(
  ensemble_system_id,station_id,target_local_date,aggregator_snapshot_at_utc
);
CREATE INDEX IF NOT EXISTS calibration_republisher_historical_member_scope_idx
ON calibration_republisher_ensemble_historical_member_run(
  ensemble_system_id,station_id,target_local_date,archive_lead_bucket,member_id
);
CREATE INDEX IF NOT EXISTS calibration_republisher_historical_oos_contract_scope_idx
ON calibration_republisher_historical_oos_contract(
  ensemble_system_id,station_id,target_local_date,archive_lead_bucket,run_cycle,
  historical_oos_eligible,contract_version
);
CREATE INDEX IF NOT EXISTS calibration_republisher_historical_bucket_scope_idx
ON calibration_republisher_ensemble_historical_member_bucket(
  ensemble_system_id,station_id,target_local_date,archive_lead_bucket,archive_query_at_utc
);

CREATE TRIGGER IF NOT EXISTS calibration_republisher_product_no_update
BEFORE UPDATE ON calibration_republisher_ensemble_product
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_republisher_product_no_delete
BEFORE DELETE ON calibration_republisher_ensemble_product
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_republisher_member_run_no_update
BEFORE UPDATE ON calibration_republisher_ensemble_member_run
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_republisher_member_run_no_delete
BEFORE DELETE ON calibration_republisher_ensemble_member_run
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_republisher_member_bucket_no_update
BEFORE UPDATE ON calibration_republisher_ensemble_member_bucket
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_republisher_member_bucket_no_delete
BEFORE DELETE ON calibration_republisher_ensemble_member_bucket
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_republisher_historical_member_run_no_update
BEFORE UPDATE ON calibration_republisher_ensemble_historical_member_run
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_republisher_historical_member_run_no_delete
BEFORE DELETE ON calibration_republisher_ensemble_historical_member_run
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_republisher_historical_oos_contract_no_update
BEFORE UPDATE ON calibration_republisher_historical_oos_contract
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_republisher_historical_oos_contract_no_delete
BEFORE DELETE ON calibration_republisher_historical_oos_contract
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_republisher_historical_member_bucket_no_update
BEFORE UPDATE ON calibration_republisher_ensemble_historical_member_bucket
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS calibration_republisher_historical_member_bucket_no_delete
BEFORE DELETE ON calibration_republisher_ensemble_historical_member_bucket
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS canonical_ssot_fact_no_update
BEFORE UPDATE ON canonical_ssot_fact
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS canonical_ssot_fact_no_delete
BEFORE DELETE ON canonical_ssot_fact
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS canonical_ssot_fact_ingress_only
BEFORE INSERT ON canonical_ssot_fact
WHEN canonical_ingress_authorized()!=1
BEGIN SELECT RAISE(ABORT,'canonical_ingress_only'); END;
"""


CANONICAL_CONTRACT_TABLES = ("canonical_ssot_fact",)
CALIBRATION_TABLES = (
    "calibration_station_registry",
    "calibration_station_source_authority",
    "calibration_station_market_timezone_binding",
    "calibration_station_daily_high_truth",
    "calibration_historical_asof_model_run",
    "calibration_model_station_residual",
    "calibration_model_artifact",
    "calibration_ensemble_artifact",
    "calibration_ensemble_selection_evidence",
    "calibration_artifact_promotion",
    "calibration_activation",
)
NATIVE_ENSEMBLE_FOUNDATION_TABLES = (
    "calibration_native_ensemble_system",
    "calibration_native_ensemble_member_run",
    "calibration_native_ensemble_member_bucket",
    "calibration_native_ensemble_historical_member_run",
    "calibration_native_ensemble_historical_member_bucket",
    "calibration_native_ensemble_system_artifact",
    "calibration_cross_native_ensemble_selection_evidence",
    "calibration_cross_native_ensemble_artifact",
    "calibration_cross_native_ensemble_oos_evaluation",
    "calibration_cross_native_ensemble_oos_prediction",
    "calibration_forward_taf_capture",
    "calibration_native_ensemble_promotion",
    "calibration_native_ensemble_activation",
)
NATIVE_ENSEMBLE_ADDITIVE_TABLES = (
    "calibration_historical_taf_capture",
    "calibration_historical_taf_correction_quarantine",
)
NATIVE_ENSEMBLE_SCHEMA_TABLES = (
    *NATIVE_ENSEMBLE_FOUNDATION_TABLES,
    *NATIVE_ENSEMBLE_ADDITIVE_TABLES,
)
REPUBLISHER_ENSEMBLE_TABLES = (
    "calibration_republisher_ensemble_product",
    "calibration_republisher_historical_oos_contract",
    "calibration_republisher_ensemble_member_run",
    "calibration_republisher_ensemble_member_bucket",
    "calibration_republisher_ensemble_historical_member_run",
    "calibration_republisher_ensemble_historical_member_bucket",
)
NATIVE_ENSEMBLE_RUN_CYCLE_MAPPING_TABLES = {
    "calibration_cross_native_ensemble_selection_evidence": (
        "selection_evidence_id",
        "selected_system_ids_json",
    ),
    "calibration_cross_native_ensemble_artifact": (
        "cross_ensemble_artifact_id",
        "ensemble_system_ids_json",
    ),
    "calibration_cross_native_ensemble_oos_evaluation": (
        "evaluation_id",
        "ensemble_system_ids_json",
    ),
    "calibration_cross_native_ensemble_oos_prediction": (
        "prediction_id",
        "ensemble_system_ids_json",
    ),
    "calibration_native_ensemble_activation": (
        "activation_id",
        None,
    ),
}
NATIVE_ENSEMBLE_RUN_CYCLE_MAPPING_COLUMNS = frozenset({
    "run_cycle_by_system_json",
    "run_cycle_by_system_sha256",
})
NATIVE_ENSEMBLE_MIXED_RUN_CYCLE = "MIXED_NATIVE_CYCLES"
HISTORICAL_TAF_TRUTH_LABEL_AUTHORITIES = frozenset({
    "FORMAL_STATION_LANE_TRUTH",
})


def native_ensemble_foundation_schema_status(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    rows = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = sorted(set(NATIVE_ENSEMBLE_FOUNDATION_TABLES) - rows)
    required_member_columns = {
        "run_init_utc",
        "published_at_utc",
        "received_at_utc",
        "transport_provider_id",
        "native_timestamps_complete",
        "aggregator_snapshot_at_utc",
        "authority_scope",
        "content_sha256",
    }
    member_columns = (
        {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(calibration_native_ensemble_member_run)"
            )
        }
        if "calibration_native_ensemble_member_run" in rows
        else set()
    )
    missing_member_columns = sorted(
        required_member_columns - member_columns
    )
    missing_run_cycle_mapping_columns = {}
    for table in NATIVE_ENSEMBLE_RUN_CYCLE_MAPPING_TABLES:
        if table not in rows:
            continue
        table_columns = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        missing_columns = sorted(
            NATIVE_ENSEMBLE_RUN_CYCLE_MAPPING_COLUMNS - table_columns
        )
        if missing_columns:
            missing_run_cycle_mapping_columns[table] = missing_columns
    historical_taf_columns = (
        {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(calibration_historical_taf_capture)"
            )
        }
        if "calibration_historical_taf_capture" in rows
        else set()
    )
    missing_historical_taf_columns = sorted(
        {"truth_label_authority"} - historical_taf_columns
    )
    return {
        "ok": (
            not missing
            and not missing_member_columns
            and not missing_run_cycle_mapping_columns
            and not missing_historical_taf_columns
        ),
        "missing_tables": missing,
        "missing_member_columns": missing_member_columns,
        "missing_run_cycle_mapping_columns":
            missing_run_cycle_mapping_columns,
        "missing_historical_taf_columns": missing_historical_taf_columns,
        "tables": list(NATIVE_ENSEMBLE_FOUNDATION_TABLES),
    }


def republisher_ensemble_schema_status(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Check the explicit Open-Meteo candidate lane, never native authority."""

    rows = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = sorted(set(REPUBLISHER_ENSEMBLE_TABLES) - rows)
    return {
        "ok": not missing,
        "missing_tables": missing,
        "tables": list(REPUBLISHER_ENSEMBLE_TABLES),
    }


def _migrate_republisher_historical_oos_contract(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Add the immutable historical Open-Meteo OOS contract to old DBs.

    The republisher lane originally had only a train-only flag.  Existing
    rows are preserved exactly and remain ineligible (the new columns default
    to zero).  A row can become historical-OOS eligible only when a caller
    appends a separately versioned contract bound to a closed station day and
    an AWC truth as-of receipt.  This migration never changes the native
    tables or promotes any existing evidence.
    """

    contract_table = "calibration_republisher_historical_oos_contract"
    contract_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (contract_table,),
    ).fetchone()
    if contract_exists is None:
        # ``REPUBLISHER_ENSEMBLE_DDL`` is normally executed by the caller
        # first.  Keep this helper safe for a direct controlled migration too.
        _execute_sql_script_transactional(conn, REPUBLISHER_ENSEMBLE_DDL)

    # Legacy databases may already have a same-named table created before the
    # truth-binding trigger was introduced.  ``IF NOT EXISTS`` is safe for the
    # canonical table and keeps the migration append-only.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS
        calibration_republisher_historical_oos_contract_truth_guard
        BEFORE INSERT ON calibration_republisher_historical_oos_contract
        WHEN NEW.historical_oos_eligible=1
          AND (
            NOT EXISTS (
              SELECT 1
              FROM calibration_station_daily_high_truth AS t
              WHERE t.truth_id=NEW.awc_truth_id
                AND t.station_id=NEW.station_id
                AND t.local_date=NEW.target_local_date
                AND t.revision_no=NEW.awc_truth_revision_no
                AND t.source_lane='AWC'
                AND t.qc_status='ACCEPTED'
                AND t.canonical_committed_at_utc IS NOT NULL
                AND julianday(t.canonical_committed_at_utc)
                    <= julianday(NEW.awc_truth_asof_utc)
            )
            OR julianday(NEW.station_day_end_utc) IS NULL
            OR julianday(NEW.station_day_closed_at_utc) IS NULL
            OR julianday(NEW.awc_truth_asof_utc) IS NULL
            OR julianday(NEW.station_day_closed_at_utc)
               < julianday(NEW.station_day_end_utc)
            OR julianday(NEW.awc_truth_asof_utc)
               < julianday(NEW.station_day_end_utc)
          )
        BEGIN
          SELECT RAISE(ABORT,'historical_republisher_oos_truth_binding_invalid');
        END
        """
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS "
        "calibration_republisher_historical_oos_contract_no_update "
        "BEFORE UPDATE ON calibration_republisher_historical_oos_contract "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS "
        "calibration_republisher_historical_oos_contract_no_delete "
        "BEFORE DELETE ON calibration_republisher_historical_oos_contract "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END"
    )

    added_columns: dict[str, list[str]] = {}
    for table in (
        "calibration_republisher_ensemble_historical_member_run",
        "calibration_republisher_ensemble_historical_member_bucket",
    ):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            continue
        columns = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        additions: list[str] = []
        if "historical_oos_contract_id" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN "
                "historical_oos_contract_id TEXT "
                "REFERENCES calibration_republisher_historical_oos_contract("
                "historical_oos_contract_id)"
            )
            additions.append("historical_oos_contract_id")
        if "historical_oos_eligible" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN "
                "historical_oos_eligible INTEGER NOT NULL DEFAULT 0 "
                "CHECK(historical_oos_eligible IN (0,1))"
            )
            additions.append("historical_oos_eligible")
        if additions:
            added_columns[table] = additions

        # Old tables may predate the table-level CHECK added above.  These
        # insert guards preserve the append-only migration without rebuilding
        # a populated immutable table.
        is_member_run = table.endswith("member_run")
        guard = (
            "calibration_republisher_historical_member_run_oos_contract_guard"
            if is_member_run
            else "calibration_republisher_historical_member_bucket_oos_contract_guard"
        )
        scope_match = (
            "c.ensemble_system_id=NEW.ensemble_system_id "
            "AND c.station_id=NEW.station_id "
            "AND c.target_local_date=NEW.target_local_date "
            "AND c.archive_lead_bucket=NEW.archive_lead_bucket "
            "AND c.run_cycle=NEW.run_cycle"
            if is_member_run
            else
            "c.ensemble_system_id=NEW.ensemble_system_id "
            "AND c.station_id=NEW.station_id "
            "AND c.target_local_date=NEW.target_local_date "
            "AND c.archive_lead_bucket=NEW.archive_lead_bucket"
        )
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {guard} "
            f"BEFORE INSERT ON {table} "
            "WHEN NEW.historical_oos_eligible=1 "
            "AND (NEW.historical_oos_contract_id IS NULL OR NOT EXISTS ("
            "SELECT 1 FROM calibration_republisher_historical_oos_contract AS c "
            "WHERE c.historical_oos_contract_id=NEW.historical_oos_contract_id "
            "AND c.historical_oos_eligible=1 AND "
            + scope_match
            + ")) "
            "BEGIN SELECT RAISE(ABORT,'historical_republisher_oos_contract_required'); END"
        )

    # The contract itself is append-only in the canonical DDL.  Validate the
    # required columns before reporting readiness so a partially-created
    # legacy table fails closed instead of silently accepting an under-specified
    # OOS claim.
    contract_columns = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({contract_table})")
    }
    required_contract_columns = {
        "historical_oos_contract_id",
        "contract_version",
        "ensemble_system_id",
        "station_id",
        "target_local_date",
        "archive_lead_bucket",
        "run_cycle",
        "station_day_closed",
        "station_day_end_utc",
        "station_day_closed_at_utc",
        "awc_truth_id",
        "awc_truth_revision_no",
        "awc_truth_asof_utc",
        "awc_truth_source_lane",
        "receipt_class",
        "temporal_scope",
        "future_event_count",
        "historical_oos_eligible",
        "republisher_receipt_sha256",
        "content_sha256",
        "created_at_utc",
    }
    missing_contract_columns = sorted(
        required_contract_columns - contract_columns
    )
    if missing_contract_columns:
        raise RuntimeError(
            "republisher_historical_oos_contract_schema_incomplete:"
            + ",".join(missing_contract_columns)
        )
    return {
        "state": "migrated" if added_columns else "already_current",
        "added_columns": added_columns,
        "historical_oos_contract_table": contract_table,
        "historical_oos_contract_columns": sorted(contract_columns),
        "existing_rows_promoted": 0,
    }


_D1_LEAD_TABLES = (
    "calibration_native_ensemble_historical_member_run",
    "calibration_native_ensemble_historical_member_bucket",
    "calibration_republisher_historical_oos_contract",
    "calibration_republisher_ensemble_historical_member_run",
    "calibration_republisher_ensemble_historical_member_bucket",
)


def _migrate_historical_d1_lead(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Admit d1 alongside d0 and d2 in the historical member tables.

    P0 follows the freshest run available, so its lead shortens from d2 to d1
    to d0 as the event day approaches.  The tables were built when only the two
    endpoints existed, and their CHECK rejects every d1 row outright -- which
    silently removes the middle of that progression: the day before the event,
    when a fresher run exists but the event has not yet started.

    Rows are copied verbatim.  This widens what may be stored, not what may be
    trusted: a d1 row still needs weights trained at d1, and P0 compilation
    fails closed on a lead whose OOS binding does not match rather than
    borrowing d2's.
    """

    rebuilt: list[str] = []
    for table in _D1_LEAD_TABLES:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if row is None or "'d1'" in str(row[0] or ""):
            continue
        for kind in ("trigger", "index"):
            for name, in conn.execute(
                "SELECT name FROM sqlite_master WHERE type=? AND tbl_name=? "
                "AND sql IS NOT NULL",
                (kind, table),
            ).fetchall():
                conn.execute(f"DROP {kind.upper()} IF EXISTS {name}")
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_pre_d1")
        rebuilt.append(table)

    if not rebuilt:
        return {"rebuilt_tables": [], "copied_rows": {}}

    # Both scripts are CREATE ... IF NOT EXISTS, so replaying them only
    # recreates what the rename above removed.
    _execute_sql_script_transactional(conn, DDL)
    _execute_sql_script_transactional(conn, REPUBLISHER_ENSEMBLE_DDL)

    copied: dict[str, int] = {}
    for table in rebuilt:
        columns = [
            str(item[1])
            for item in conn.execute(f"PRAGMA table_info({table})")
        ]
        legacy = {
            str(item[1])
            for item in conn.execute(f"PRAGMA table_info({table}_pre_d1)")
        }
        shared = [name for name in columns if name in legacy]
        joined = ",".join(shared)
        conn.execute(
            f"INSERT INTO {table}({joined}) "
            f"SELECT {joined} FROM {table}_pre_d1"
        )
        copied[table] = int(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        conn.execute(f"DROP TABLE {table}_pre_d1")
    return {"rebuilt_tables": rebuilt, "copied_rows": copied}


_REPUBLISHER_D2_TABLES = (
    "calibration_republisher_historical_oos_contract",
    "calibration_republisher_ensemble_historical_member_run",
    "calibration_republisher_ensemble_historical_member_bucket",
)


def _migrate_republisher_historical_d2_lead(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Let the republisher history tables hold d2 as well as d0.

    UKMO, GOOGLE_WEATHERNEXT and NOAA_AIGEFS publish no official native archive
    at all, so this aggregator is the only route by which they can ever supply
    a d2 member set -- and d2 is the lead the cross-ensemble selector scores on.
    The tables were built when the lane carried d0 only, and their CHECK
    constraint rejects every d2 row outright.

    Existing rows are copied verbatim and keep their eligibility flags; this
    widens what may be stored, not what may be trusted.  A d2 row still needs a
    separately versioned historical OOS contract before it counts as evidence,
    and the contract guard trigger is rebuilt with the table.
    """

    rebuilt: list[str] = []
    for table in _REPUBLISHER_D2_TABLES:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if row is None or "'d2'" in str(row[0] or ""):
            continue
        columns = [
            str(item[1])
            for item in conn.execute(f"PRAGMA table_info({table})")
        ]
        for kind in ("trigger", "index"):
            for name, in conn.execute(
                "SELECT name FROM sqlite_master WHERE type=? AND tbl_name=? "
                "AND sql IS NOT NULL",
                (kind, table),
            ).fetchall():
                conn.execute(f"DROP {kind.upper()} IF EXISTS {name}")
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_pre_d2")
        rebuilt.append(table)

    if not rebuilt:
        return {"rebuilt_tables": [], "copied_rows": {}}

    # The canonical DDL now carries the widened CHECK, so recreating from it
    # keeps migrated databases byte-identical to freshly created ones.
    _execute_sql_script_transactional(conn, REPUBLISHER_ENSEMBLE_DDL)

    copied: dict[str, int] = {}
    for table in rebuilt:
        columns = [
            str(item[1])
            for item in conn.execute(f"PRAGMA table_info({table})")
        ]
        legacy = {
            str(item[1])
            for item in conn.execute(f"PRAGMA table_info({table}_pre_d2)")
        }
        shared = [name for name in columns if name in legacy]
        joined = ",".join(shared)
        conn.execute(
            f"INSERT INTO {table}({joined}) "
            f"SELECT {joined} FROM {table}_pre_d2"
        )
        copied[table] = int(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        conn.execute(f"DROP TABLE {table}_pre_d2")
    return {"rebuilt_tables": rebuilt, "copied_rows": copied}


def _migrate_native_historical_source_contract(
    conn: sqlite3.Connection,
) -> None:
    """Broaden the old Open-Meteo-only history tables without changing facts."""

    member_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND "
        "name='calibration_native_ensemble_historical_member_run'"
    ).fetchone()
    member_sql = (
        str(member_sql_row[0]) if member_sql_row is not None else ""
    )
    if member_sql_row is not None and (
        "offline_oos_eligible" not in member_sql
        or "'d2'" not in member_sql
        or "ECMWF_OPEN_DATA_S3" not in member_sql
        or "ECCC_DD_WEATHER" not in member_sql
        # DWD ICON EPS is the fourth official native transport.  Without it the
        # CHECK constraint rejects every ICON member row, so the system can
        # never accumulate the d2 history that admits it as an independent
        # candidate family.
        or "DWD_OPENDATA_ICON_EPS" not in member_sql
        # The forecaster's TAF TX enters the same archive as a single-member
        # point system; its transport is the TAF capture lane.
        or "AWC_TAF_DATA_API" not in member_sql
        # The METAR physical daily-maximum point rides the same archive; a
        # missing transport here silently zeroes its every insert (OR IGNORE
        # swallows the CHECK), which is how it shipped registered, contracted
        # and producing nothing.
        or "AWC_METAR_PHYSICAL_PATH" not in member_sql
    ):
        had_offline_columns = "offline_oos_eligible" in member_sql
        conn.execute(
            "DROP TRIGGER IF EXISTS "
            "calibration_native_historical_member_no_update"
        )
        conn.execute(
            "DROP TRIGGER IF EXISTS "
            "calibration_native_historical_member_no_delete"
        )
        conn.execute(
            "DROP INDEX IF EXISTS "
            "calibration_native_historical_member_scope_idx"
        )
        conn.execute(
            "ALTER TABLE calibration_native_ensemble_historical_member_run "
            "RENAME TO calibration_native_ensemble_historical_member_run_v1"
        )
        conn.execute("""
            CREATE TABLE calibration_native_ensemble_historical_member_run (
              historical_member_run_id TEXT PRIMARY KEY,
              ensemble_system_id TEXT NOT NULL
                REFERENCES calibration_native_ensemble_system(ensemble_system_id),
              station_id TEXT NOT NULL
                REFERENCES calibration_station_registry(station_id),
              target_local_date TEXT NOT NULL,
              archive_lead_bucket TEXT NOT NULL
                CHECK(archive_lead_bucket IN ('d0','d1','d2')),
              run_cycle TEXT NOT NULL CHECK(
                run_cycle IN (
                  'AGGREGATOR_UNKNOWN_NATIVE_CYCLE',
                  '00Z','06Z','12Z','18Z'
                )
              ),
              member_id TEXT NOT NULL,
              member_role TEXT NOT NULL
                CHECK(member_role IN ('CONTROL','PERTURBED')),
              raw_tmax_c REAL NOT NULL,
              settlement_definition TEXT NOT NULL,
              extraction_version TEXT NOT NULL,
              provider_id TEXT NOT NULL,
              transport_provider_id TEXT NOT NULL CHECK(
                transport_provider_id IN (
                  'OPEN_METEO','NOAA_GEFS_PDS_S3',
                  'ECMWF_OPEN_DATA_S3','ECCC_DD_WEATHER',
                  'DWD_OPENDATA_ICON_EPS','AWC_TAF_DATA_API',
                  'AWC_METAR_PHYSICAL_PATH'
                )
              ),
              native_run_init_utc TEXT,
              offline_oos_eligible INTEGER NOT NULL DEFAULT 0
                CHECK(offline_oos_eligible IN (0,1)),
              archive_query_at_utc TEXT NOT NULL,
              source_url TEXT NOT NULL,
              raw_object_ref TEXT NOT NULL,
              source_object_sha256 TEXT NOT NULL
                CHECK(length(source_object_sha256)=64),
              qc_status TEXT NOT NULL
                CHECK(qc_status IN ('ACCEPTED','QUARANTINED')),
              authority_scope TEXT NOT NULL
                CHECK(authority_scope='CALIBRATION_TRAIN_ONLY'),
              holdout_eligible INTEGER NOT NULL CHECK(holdout_eligible=0),
              content_sha256 TEXT NOT NULL UNIQUE
                CHECK(length(content_sha256)=64),
              CHECK(
                offline_oos_eligible=0
                OR (
                  transport_provider_id IN (
                    'NOAA_GEFS_PDS_S3','ECMWF_OPEN_DATA_S3',
                    'ECCC_DD_WEATHER','DWD_OPENDATA_ICON_EPS',
                    'AWC_TAF_DATA_API','AWC_METAR_PHYSICAL_PATH'
                  )
                  AND native_run_init_utc IS NOT NULL
                  AND run_cycle!='AGGREGATOR_UNKNOWN_NATIVE_CYCLE'
                )
              ),
              UNIQUE(
                ensemble_system_id,station_id,target_local_date,
                archive_lead_bucket,member_id,extraction_version
              )
            )
        """)
        destination_columns = """
              historical_member_run_id,ensemble_system_id,station_id,
              target_local_date,archive_lead_bucket,run_cycle,member_id,
              member_role,raw_tmax_c,settlement_definition,
              extraction_version,provider_id,transport_provider_id,
              native_run_init_utc,offline_oos_eligible,
              archive_query_at_utc,source_url,raw_object_ref,
              source_object_sha256,qc_status,authority_scope,
              holdout_eligible,content_sha256
        """
        if had_offline_columns:
            source_columns = destination_columns
        else:
            source_columns = """
              historical_member_run_id,ensemble_system_id,station_id,
              target_local_date,archive_lead_bucket,run_cycle,member_id,
              member_role,raw_tmax_c,settlement_definition,
              extraction_version,provider_id,transport_provider_id,
              NULL,0,archive_query_at_utc,source_url,raw_object_ref,
              source_object_sha256,qc_status,authority_scope,
              holdout_eligible,content_sha256
            """
        conn.execute(
            "INSERT INTO "
            "calibration_native_ensemble_historical_member_run("
            + destination_columns
            + ") SELECT "
            + source_columns
            + " FROM "
            "calibration_native_ensemble_historical_member_run_v1"
        )
        conn.execute(
            "DROP TABLE "
            "calibration_native_ensemble_historical_member_run_v1"
        )

    bucket_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND "
        "name='calibration_native_ensemble_historical_member_bucket'"
    ).fetchone()
    bucket_sql = (
        str(bucket_sql_row[0]) if bucket_sql_row is not None else ""
    )
    if bucket_sql_row is not None and (
        "official_native_archive" not in bucket_sql
        or "'d2'" not in bucket_sql
    ):
        conn.execute(
            "DROP TRIGGER IF EXISTS "
            "calibration_native_historical_bucket_no_update"
        )
        conn.execute(
            "DROP TRIGGER IF EXISTS "
            "calibration_native_historical_bucket_no_delete"
        )
        conn.execute(
            "DROP INDEX IF EXISTS "
            "calibration_native_historical_bucket_scope_idx"
        )
        conn.execute(
            "ALTER TABLE calibration_native_ensemble_historical_member_bucket "
            "RENAME TO calibration_native_ensemble_historical_member_bucket_v1"
        )
        conn.execute("""
            CREATE TABLE calibration_native_ensemble_historical_member_bucket (
              historical_member_bucket_id TEXT PRIMARY KEY,
              ensemble_system_id TEXT NOT NULL
                REFERENCES calibration_native_ensemble_system(ensemble_system_id),
              station_id TEXT NOT NULL
                REFERENCES calibration_station_registry(station_id),
              target_local_date TEXT NOT NULL,
              archive_lead_bucket TEXT NOT NULL
                CHECK(archive_lead_bucket IN ('d0','d1','d2')),
              archive_query_at_utc TEXT NOT NULL,
              member_set_sha256 TEXT NOT NULL
                CHECK(length(member_set_sha256)=64),
              member_count INTEGER NOT NULL CHECK(member_count>=2),
              bucket_contract_sha256 TEXT NOT NULL
                CHECK(length(bucket_contract_sha256)=64),
              bucket_intervals_json TEXT NOT NULL,
              raw_bucket_probabilities_json TEXT NOT NULL,
              source_role TEXT NOT NULL CHECK(
                source_role IN (
                  'aggregator_republisher','official_native_archive'
                )
              ),
              authority_scope TEXT NOT NULL
                CHECK(authority_scope='CALIBRATION_TRAIN_ONLY'),
              holdout_eligible INTEGER NOT NULL CHECK(holdout_eligible=0),
              content_sha256 TEXT NOT NULL UNIQUE
                CHECK(length(content_sha256)=64),
              UNIQUE(
                ensemble_system_id,station_id,target_local_date,
                archive_lead_bucket,member_set_sha256,
                bucket_contract_sha256
              )
            )
        """)
        conn.execute("""
            INSERT INTO calibration_native_ensemble_historical_member_bucket
            SELECT *
            FROM calibration_native_ensemble_historical_member_bucket_v1
        """)
        conn.execute(
            "DROP TABLE "
            "calibration_native_ensemble_historical_member_bucket_v1"
        )


RETIRED_HISTORICAL_TAF_TRUTH_LABEL = "TEMPORARY_AWC_CALIBRATION_LABEL"

# The formal replacement row was re-captured from the same historical TAF, so
# every payload column must match byte for byte; only the sample envelope
# hashes may differ because the label participates in them.
_HISTORICAL_TAF_TWIN_PAYLOAD_COLUMNS = (
    "station_id",
    "event_date",
    "settlement_timezone",
    "decision_anchor_utc",
    "issued_at_utc",
    "provider_available_at_utc",
    "tx_c",
    "tx_valid_at_utc",
    "cloud_cover_fraction",
    "precipitation_probability",
    "wind_speed_mps",
    "raw_taf_sha256",
)


def _historical_taf_capture_canonical_ddl_statements() -> list[str]:
    statements: list[str] = []
    statement = ""
    for line in DDL.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip()
        statement = ""
        if sql and "calibration_historical_taf_capture" in sql:
            statements.append(sql)
    if len(statements) != 4:  # table, scope index, two append-only triggers
        raise RuntimeError("historical_taf_capture_canonical_ddl_incomplete")
    return statements


def _retire_historical_taf_temporary_awc_rows(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    """Archive the since-retired temporary-AWC label rows off the hot table.

    Eight Chinese stations (2026-07-01..2026-07-29) were calibrated against
    AWC truth under a one-off, since-retired authorization while their AMSC
    history was unavailable.  The mandated rebuild re-captured every affected
    station day under ``FORMAL_STATION_LANE_TRUTH``, but the append-only
    triggers kept the retired rows in place, so the canonical FORMAL-only
    CHECK could never be installed.  Move the retired rows into an immutable
    archive table and rebuild the capture table on the canonical DDL — only
    after proving each retired row has a payload-identical formal
    replacement, so nothing here guesses legacy semantics.
    """

    table = "calibration_historical_taf_capture"
    retired = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE truth_label_authority=?",
            (RETIRED_HISTORICAL_TAF_TRUTH_LABEL,),
        ).fetchone()[0]
    )
    if retired == 0:
        return {"retired_rows_quarantined": 0}
    unlabelled = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} "
            "WHERE truth_label_authority IS NULL"
        ).fetchone()[0]
    )
    if unlabelled > 0:
        # NULL rows cannot survive the canonical NOT NULL rebuild; their
        # capture must be rebuilt first, exactly as the column migration
        # already demands.
        raise RuntimeError(
            "historical_taf_retired_temporary_awc_unlabelled_rows_block_rebuild"
        )
    twin_match = " AND ".join(
        f"f.{column}=t.{column}"
        for column in _HISTORICAL_TAF_TWIN_PAYLOAD_COLUMNS
    )
    unreplaced = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} t "
            "WHERE t.truth_label_authority=? "
            f"AND NOT EXISTS (SELECT 1 FROM {table} f "
            "WHERE f.truth_label_authority='FORMAL_STATION_LANE_TRUTH' "
            f"AND {twin_match})",
            (RETIRED_HISTORICAL_TAF_TRUTH_LABEL,),
        ).fetchone()[0]
    )
    if unreplaced > 0:
        raise RuntimeError(
            "historical_taf_retired_temporary_awc_rows_unreplaced"
        )
    legacy = f"{table}_retired_temporary_awc_v1"
    collision = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (legacy,),
    ).fetchone()
    if collision is not None:
        raise RuntimeError(
            "historical_taf_retired_temporary_awc_legacy_collision"
        )
    conn.execute(
        f"""
        CREATE TABLE {legacy} (
          historical_taf_capture_id TEXT PRIMARY KEY,
          station_id TEXT NOT NULL,
          event_date TEXT NOT NULL,
          settlement_timezone TEXT NOT NULL,
          decision_anchor_utc TEXT NOT NULL,
          issued_at_utc TEXT NOT NULL,
          provider_available_at_utc TEXT NOT NULL,
          tx_c REAL NOT NULL,
          tx_valid_at_utc TEXT NOT NULL,
          cloud_cover_fraction REAL NOT NULL
            CHECK(cloud_cover_fraction>=0 AND cloud_cover_fraction<=1),
          precipitation_probability REAL NOT NULL
            CHECK(precipitation_probability>=0 AND
                  precipitation_probability<=1),
          wind_speed_mps REAL NOT NULL CHECK(wind_speed_mps>=0),
          raw_taf_sha256 TEXT NOT NULL CHECK(length(raw_taf_sha256)=64),
          taf_response_sha256 TEXT NOT NULL
            CHECK(length(taf_response_sha256)=64),
          source_sample_sha256 TEXT NOT NULL
            CHECK(length(source_sample_sha256)=64),
          source_class TEXT NOT NULL CHECK(
            source_class='official_date_addressed_historical_replay'
          ),
          truth_label_authority TEXT NOT NULL CHECK(
            truth_label_authority='TEMPORARY_AWC_CALIBRATION_LABEL'
          ),
          authority_scope TEXT NOT NULL CHECK(
            authority_scope='CALIBRATION_TRAIN_ONLY'
          ),
          content_sha256 TEXT NOT NULL UNIQUE
            CHECK(length(content_sha256)=64),
          CHECK(issued_at_utc<=provider_available_at_utc),
          CHECK(provider_available_at_utc<=decision_anchor_utc),
          UNIQUE(station_id,event_date,source_sample_sha256)
        )
        """
    )
    legacy_columns = [
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({legacy})")
    ]
    column_list = ",".join(legacy_columns)
    conn.execute(
        f"INSERT INTO {legacy}({column_list}) "
        f"SELECT {column_list} FROM {table} "
        "WHERE truth_label_authority=?",
        (RETIRED_HISTORICAL_TAF_TRUTH_LABEL,),
    )
    moved = int(
        conn.execute(f"SELECT COUNT(*) FROM {legacy}").fetchone()[0]
    )
    if moved != retired:
        raise RuntimeError(
            "historical_taf_retired_temporary_awc_copy_count_mismatch"
        )
    conn.execute(
        f"CREATE TRIGGER {legacy}_no_update BEFORE UPDATE ON {legacy} "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END"
    )
    conn.execute(
        f"CREATE TRIGGER {legacy}_no_delete BEFORE DELETE ON {legacy} "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END"
    )
    formal = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} "
            "WHERE truth_label_authority='FORMAL_STATION_LANE_TRUTH'"
        ).fetchone()[0]
    )
    total = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    if formal + retired != total:
        raise RuntimeError(
            "historical_taf_retired_temporary_awc_row_partition_mismatch"
        )
    pre_rebuild = f"{table}_pre_temporary_awc_retirement"
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (pre_rebuild,),
    ).fetchone() is not None:
        raise RuntimeError(
            "historical_taf_retired_temporary_awc_rebuild_collision"
        )
    # ALTER..RENAME keeps trigger and index names bound to the renamed
    # table, which would make the canonical IF NOT EXISTS statements no-ops
    # and leave the rebuilt table unguarded.  Drop them first.
    conn.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
    conn.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
    conn.execute(f"DROP INDEX IF EXISTS {table}_scope_idx")
    conn.execute(f"ALTER TABLE {table} RENAME TO {pre_rebuild}")
    for sql in _historical_taf_capture_canonical_ddl_statements():
        conn.execute(sql)
    canonical_columns = [
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
    ]
    canonical_list = ",".join(canonical_columns)
    conn.execute(
        f"INSERT INTO {table}({canonical_list}) "
        f"SELECT {canonical_list} FROM {pre_rebuild} "
        "WHERE truth_label_authority='FORMAL_STATION_LANE_TRUTH'"
    )
    rebuilt = int(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    )
    if rebuilt != formal:
        raise RuntimeError(
            "historical_taf_retired_temporary_awc_rebuild_count_mismatch"
        )
    conn.execute(f"DROP TABLE {pre_rebuild}")
    return {
        "retired_rows_quarantined": moved,
        "formal_rows_rebuilt": rebuilt,
    }


def _migrate_historical_taf_truth_label_authority(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    """Add the historical TAF label without guessing legacy rows."""

    table = "calibration_historical_taf_capture"
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        return {"column_added": 0, "unlabelled_rows": 0}
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
    }
    added = 0
    if "truth_label_authority" not in columns:
        # SQLite cannot add a NOT NULL column to a populated table without a
        # guessed value.  Legacy rows remain NULL and are rejected by ingress
        # and exact join until their capture is rebuilt.
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN truth_label_authority TEXT"
        )
        added = 1
    retirement = _retire_historical_taf_temporary_awc_rows(conn)
    invalid = conn.execute(
        f"SELECT COUNT(*) FROM {table} "
        "WHERE truth_label_authority IS NOT NULL "
        "AND truth_label_authority!='FORMAL_STATION_LANE_TRUTH'"
    ).fetchone()
    if invalid is not None and int(invalid[0]) > 0:
        raise RuntimeError("historical_taf_truth_label_authority_invalid_rows")
    unlabelled = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE truth_label_authority IS NULL"
    ).fetchone()
    return {
        "column_added": added,
        "unlabelled_rows": int(unlabelled[0]) if unlabelled is not None else 0,
        "retired_temporary_awc_rows_quarantined": (
            retirement["retired_rows_quarantined"]
        ),
    }


def _canonical_native_run_cycle_mapping(
    *,
    system_ids_json: str,
    run_cycle: str,
) -> tuple[str, str]:
    try:
        raw_system_ids = json.loads(str(system_ids_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "native_ensemble_run_cycle_mapping_system_ids_invalid"
        ) from exc
    if (
        not isinstance(raw_system_ids, list)
        or not raw_system_ids
        or any(not str(system_id).strip() for system_id in raw_system_ids)
    ):
        raise RuntimeError(
            "native_ensemble_run_cycle_mapping_system_ids_invalid"
        )
    system_ids = sorted(str(system_id) for system_id in raw_system_ids)
    if len(system_ids) != len(set(system_ids)):
        raise RuntimeError(
            "native_ensemble_run_cycle_mapping_system_ids_invalid"
        )
    cycle = str(run_cycle or "").strip()
    if not cycle or cycle == NATIVE_ENSEMBLE_MIXED_RUN_CYCLE:
        raise RuntimeError(
            "native_ensemble_same_cycle_backfill_scope_invalid"
        )
    mapping = {system_id: cycle for system_id in system_ids}
    mapping_json = json.dumps(
        mapping,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return mapping_json, content_sha256(mapping)


def _validate_native_run_cycle_mapping(
    *,
    system_ids_json: str,
    run_cycle: str,
    mapping_json: str,
    mapping_sha256: str,
) -> None:
    try:
        raw_system_ids = json.loads(str(system_ids_json))
        raw_mapping = json.loads(str(mapping_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "native_ensemble_run_cycle_mapping_invalid"
        ) from exc
    if (
        not isinstance(raw_system_ids, list)
        or not isinstance(raw_mapping, dict)
    ):
        raise RuntimeError(
            "native_ensemble_run_cycle_mapping_invalid"
        )
    system_ids = sorted(str(system_id) for system_id in raw_system_ids)
    mapping = {
        str(system_id): str(cycle)
        for system_id, cycle in raw_mapping.items()
    }
    if (
        len(system_ids) != len(set(system_ids))
        or sorted(mapping) != system_ids
        or any(not cycle.strip() for cycle in mapping.values())
        or mapping_sha256 != content_sha256(mapping)
        or str(mapping_json) != json.dumps(
            mapping,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    ):
        raise RuntimeError(
            "native_ensemble_run_cycle_mapping_invalid"
        )
    run_cycle_value = str(run_cycle)
    cycles = set(mapping.values())
    if run_cycle_value == NATIVE_ENSEMBLE_MIXED_RUN_CYCLE:
        if len(cycles) < 2:
            raise RuntimeError(
                "native_ensemble_mixed_run_cycle_mapping_invalid"
            )
    elif cycles != {run_cycle_value}:
        raise RuntimeError(
            "native_ensemble_same_run_cycle_mapping_invalid"
        )


def _migrate_native_run_cycle_mapping(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    """Add mapping columns and backfill only deterministic old scopes."""

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    counts = {
        "columns_added": 0,
        "same_cycle_rows_backfilled": 0,
        "legacy_mixed_rows_left_unset": 0,
    }
    for table in NATIVE_ENSEMBLE_RUN_CYCLE_MAPPING_TABLES:
        if table not in tables:
            continue
        columns = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if "run_cycle_by_system_json" not in columns:
            conn.execute(
                f"ALTER TABLE {table} "
                "ADD COLUMN run_cycle_by_system_json TEXT"
            )
            counts["columns_added"] += 1
        if "run_cycle_by_system_sha256" not in columns:
            conn.execute(
                f"ALTER TABLE {table} "
                "ADD COLUMN run_cycle_by_system_sha256 TEXT "
                "CHECK(run_cycle_by_system_sha256 IS NULL OR "
                "length(run_cycle_by_system_sha256)=64)"
            )
            counts["columns_added"] += 1

    update_triggers = {
        "calibration_cross_native_ensemble_selection_evidence":
            "calibration_cross_native_selection_no_update",
        "calibration_cross_native_ensemble_artifact":
            "calibration_cross_native_artifact_no_update",
        "calibration_cross_native_ensemble_oos_evaluation":
            "calibration_cross_native_oos_evaluation_no_update",
        "calibration_cross_native_ensemble_oos_prediction":
            "calibration_cross_native_oos_prediction_no_update",
        "calibration_native_ensemble_activation":
            "calibration_native_activation_no_update",
    }
    for trigger in update_triggers.values():
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")

    for table, (primary_key, systems_column) in (
        NATIVE_ENSEMBLE_RUN_CYCLE_MAPPING_TABLES.items()
    ):
        if table not in tables:
            continue
        if table == "calibration_native_ensemble_activation":
            rows = conn.execute(
                "SELECT activation_id,artifact_kind,artifact_id,"
                "ensemble_system_id_or_star,run_cycle,"
                "run_cycle_by_system_json,run_cycle_by_system_sha256 "
                "FROM calibration_native_ensemble_activation"
            ).fetchall()
            normalized_rows = []
            for row in rows:
                if str(row[1]) == "CROSS_NATIVE_ENSEMBLE":
                    artifact = conn.execute(
                        "SELECT ensemble_system_ids_json "
                        "FROM calibration_cross_native_ensemble_artifact "
                        "WHERE cross_ensemble_artifact_id=?",
                        (str(row[2]),),
                    ).fetchone()
                    if artifact is None:
                        raise RuntimeError(
                            "native_ensemble_activation_artifact_missing"
                        )
                    system_ids_json = str(artifact[0])
                else:
                    system_ids_json = json.dumps(
                        [str(row[3])],
                        separators=(",", ":"),
                    )
                normalized_rows.append((
                    str(row[0]),
                    str(row[4]),
                    system_ids_json,
                    row[5],
                    row[6],
                ))
        else:
            rows = conn.execute(
                f"SELECT {primary_key},run_cycle,{systems_column},"
                "run_cycle_by_system_json,run_cycle_by_system_sha256 "
                f"FROM {table}"
            ).fetchall()
            normalized_rows = [
                (
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    row[3],
                    row[4],
                )
                for row in rows
            ]
        for (
            row_id,
            run_cycle,
            system_ids_json,
            mapping_json,
            mapping_sha,
        ) in normalized_rows:
            if (mapping_json is None) != (mapping_sha is None):
                raise RuntimeError(
                    "native_ensemble_run_cycle_mapping_partial"
                )
            if mapping_json is not None:
                _validate_native_run_cycle_mapping(
                    system_ids_json=system_ids_json,
                    run_cycle=run_cycle,
                    mapping_json=str(mapping_json),
                    mapping_sha256=str(mapping_sha),
                )
                continue
            if run_cycle == NATIVE_ENSEMBLE_MIXED_RUN_CYCLE:
                counts["legacy_mixed_rows_left_unset"] += 1
                continue
            canonical_json, canonical_sha = (
                _canonical_native_run_cycle_mapping(
                    system_ids_json=system_ids_json,
                    run_cycle=run_cycle,
                )
            )
            conn.execute(
                f"UPDATE {table} SET run_cycle_by_system_json=?,"
                f"run_cycle_by_system_sha256=? WHERE {primary_key}=?",
                (canonical_json, canonical_sha, row_id),
            )
            counts["same_cycle_rows_backfilled"] += 1

    for table, trigger in update_triggers.items():
        if table in tables:
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {trigger} "
                f"BEFORE UPDATE ON {table} "
                "BEGIN SELECT RAISE(ABORT,'append_only'); END"
            )
    return counts


def _copy_legacy_open_meteo_republisher_rows(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Copy immutable legacy Open-Meteo rows into the republisher lane.

    Earlier ingress versions stored Open-Meteo evidence in the native
    candidate tables.  The rows are retained for audit, but the runtime must
    read them through a clearly labelled republisher table.  This migration is
    additive and idempotent: it never updates or deletes the legacy rows and
    uses the original content-addressed identifiers.
    """

    product_rows = conn.execute(
        "SELECT s.ensemble_system_id,s.canonical_name,"
        "s.independence_family_id,s.provider_id,s.expected_member_count,"
        "s.member_identity_contract_version,"
        "s.publication_contract_version,s.valid_from_utc,s.valid_until_utc,"
        "s.content_sha256 "
        "FROM calibration_native_ensemble_system AS s "
        "WHERE EXISTS (SELECT 1 FROM calibration_native_ensemble_member_run m "
        "WHERE m.ensemble_system_id=s.ensemble_system_id "
        "AND m.transport_provider_id='OPEN_METEO') "
        "OR EXISTS (SELECT 1 FROM calibration_native_ensemble_member_bucket b "
        "WHERE b.ensemble_system_id=s.ensemble_system_id "
        "AND b.source_role='aggregator_republisher') "
        "OR EXISTS (SELECT 1 FROM calibration_native_ensemble_historical_member_run h "
        "WHERE h.ensemble_system_id=s.ensemble_system_id "
        "AND h.transport_provider_id='OPEN_METEO') "
        "OR EXISTS (SELECT 1 FROM calibration_native_ensemble_historical_member_bucket hb "
        "WHERE hb.ensemble_system_id=s.ensemble_system_id "
        "AND hb.source_role='aggregator_republisher') "
        "ORDER BY s.ensemble_system_id"
    ).fetchall()
    products = 0
    for row in product_rows:
        conn.execute(
            "INSERT OR IGNORE INTO calibration_republisher_ensemble_product("
            "ensemble_system_id,canonical_name,independence_family_id,"
            "upstream_provider_id,transport_provider_id,source_role,"
            "expected_member_count,member_identity_contract_version,"
            "publication_contract_version,native_arrival_index_eligible,"
            "formal_p0_eligible,valid_from_utc,valid_until_utc,content_sha256"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(row[0]), str(row[1]), str(row[2]), str(row[3]),
                "OPEN_METEO", "aggregator_republisher", int(row[4]),
                str(row[5]), str(row[6]), 0, 0, str(row[7]), str(row[8]),
                str(row[9]),
            ),
        )
        products += int(conn.execute("SELECT changes()").fetchone()[0])

    member_runs_before = int(conn.execute(
        "SELECT count(*) FROM calibration_republisher_ensemble_member_run"
    ).fetchone()[0])
    conn.execute(
        "INSERT OR IGNORE INTO calibration_republisher_ensemble_member_run("
        "republisher_member_run_id,ensemble_system_id,station_id,"
        "target_local_date,member_id,member_role,received_at_utc,lead_bucket,"
        "raw_tmax_c,settlement_definition,extraction_version,"
        "upstream_provider_id,transport_provider_id,"
        "aggregator_snapshot_at_utc,source_url,raw_object_ref,"
        "source_object_sha256,qc_status,authority_scope,content_sha256"
        ") SELECT member_run_id,ensemble_system_id,station_id,"
        "target_local_date,member_id,member_role,received_at_utc,lead_bucket,"
        "raw_tmax_c,settlement_definition,extraction_version,provider_id,"
        "'OPEN_METEO',aggregator_snapshot_at_utc,source_url,raw_object_ref,"
        "source_object_sha256,qc_status,authority_scope,content_sha256 "
        "FROM calibration_native_ensemble_member_run "
        "WHERE transport_provider_id='OPEN_METEO'"
    )
    member_runs = int(conn.execute("SELECT changes()").fetchone()[0])
    member_buckets_before = int(conn.execute(
        "SELECT count(*) FROM calibration_republisher_ensemble_member_bucket"
    ).fetchone()[0])
    conn.execute(
        "INSERT OR IGNORE INTO calibration_republisher_ensemble_member_bucket("
        "republisher_member_bucket_id,ensemble_system_id,station_id,"
        "target_local_date,aggregator_snapshot_at_utc,member_set_sha256,"
        "member_count,bucket_contract_sha256,bucket_intervals_json,"
        "raw_bucket_probabilities_json,source_role,authority_scope,"
        "content_sha256"
        ") SELECT member_bucket_id,ensemble_system_id,station_id,"
        "target_local_date,aggregator_snapshot_at_utc,member_set_sha256,"
        "member_count,bucket_contract_sha256,bucket_intervals_json,"
        "raw_bucket_probabilities_json,'aggregator_republisher',"
        "authority_scope,content_sha256 "
        "FROM calibration_native_ensemble_member_bucket "
        "WHERE source_role='aggregator_republisher'"
    )
    member_buckets = int(conn.execute("SELECT changes()").fetchone()[0])

    historical_runs_before = int(conn.execute(
        "SELECT count(*) FROM "
        "calibration_republisher_ensemble_historical_member_run"
    ).fetchone()[0])
    conn.execute(
        "INSERT OR IGNORE INTO "
        "calibration_republisher_ensemble_historical_member_run("
        "historical_member_run_id,ensemble_system_id,station_id,"
        "target_local_date,archive_lead_bucket,run_cycle,member_id,"
        "member_role,raw_tmax_c,settlement_definition,extraction_version,"
        "provider_id,transport_provider_id,native_run_init_utc,"
        "offline_oos_eligible,archive_query_at_utc,source_url,raw_object_ref,"
        "source_object_sha256,qc_status,authority_scope,holdout_eligible,"
        "content_sha256"
        ") SELECT historical_member_run_id,ensemble_system_id,station_id,"
        "target_local_date,archive_lead_bucket,run_cycle,member_id,"
        "member_role,raw_tmax_c,settlement_definition,extraction_version,"
        "provider_id,'OPEN_METEO',native_run_init_utc,0,"
        "archive_query_at_utc,source_url,raw_object_ref,source_object_sha256,"
        "qc_status,authority_scope,0,content_sha256 "
        "FROM calibration_native_ensemble_historical_member_run "
        "WHERE transport_provider_id='OPEN_METEO' "
        "AND archive_lead_bucket='d0' "
        "AND run_cycle='AGGREGATOR_UNKNOWN_NATIVE_CYCLE' "
        "AND offline_oos_eligible=0 "
        "AND holdout_eligible=0"
    )
    historical_runs = int(conn.execute("SELECT changes()").fetchone()[0])
    historical_buckets_before = int(conn.execute(
        "SELECT count(*) FROM "
        "calibration_republisher_ensemble_historical_member_bucket"
    ).fetchone()[0])
    conn.execute(
        "INSERT OR IGNORE INTO "
        "calibration_republisher_ensemble_historical_member_bucket("
        "historical_member_bucket_id,ensemble_system_id,station_id,"
        "target_local_date,archive_lead_bucket,archive_query_at_utc,"
        "member_set_sha256,member_count,bucket_contract_sha256,"
        "bucket_intervals_json,raw_bucket_probabilities_json,source_role,"
        "authority_scope,holdout_eligible,content_sha256"
        ") SELECT historical_member_bucket_id,ensemble_system_id,station_id,"
        "target_local_date,archive_lead_bucket,archive_query_at_utc,"
        "member_set_sha256,member_count,bucket_contract_sha256,"
        "bucket_intervals_json,raw_bucket_probabilities_json,"
        "'aggregator_republisher',authority_scope,0,content_sha256 "
        "FROM calibration_native_ensemble_historical_member_bucket "
        "WHERE source_role='aggregator_republisher' "
        "AND archive_lead_bucket='d0' AND holdout_eligible=0"
    )
    historical_buckets = int(conn.execute("SELECT changes()").fetchone()[0])
    return {
        "state": "copied" if any((products, member_runs, member_buckets,
                                    historical_runs, historical_buckets))
        else "already_current",
        "products": products,
        "member_runs": member_runs,
        "member_buckets": member_buckets,
        "historical_member_runs": historical_runs,
        "historical_member_buckets": historical_buckets,
        "existing_before": {
            "member_runs": member_runs_before,
            "member_buckets": member_buckets_before,
            "historical_member_runs": historical_runs_before,
            "historical_member_buckets": historical_buckets_before,
        },
    }


def apply_native_ensemble_foundation_schema(
    conn: sqlite3.Connection,
    *,
    commit: bool = True,
    copy_republisher_legacy: bool = False,
) -> dict[str, object]:
    """Install native and explicitly separated republisher tables.

    Existing Open-Meteo rows are copied only when the controlled migration
    explicitly opts into ``copy_republisher_legacy``.  The legacy rows remain
    immutable audit evidence in their original tables.
    """

    conn.execute("SAVEPOINT native_ensemble_foundation")
    try:
        _execute_sql_script_transactional(conn, REPUBLISHER_ENSEMBLE_DDL)
        republisher_historical_oos_migration = (
            _migrate_republisher_historical_oos_contract(conn)
        )
        _migrate_native_historical_source_contract(conn)
        statement = ""
        for line in DDL.splitlines(keepends=True):
            statement += line
            if not sqlite3.complete_statement(statement):
                continue
            sql = statement.strip()
            statement = ""
            if sql and any(
                table in sql
                for table in NATIVE_ENSEMBLE_SCHEMA_TABLES
            ):
                conn.execute(sql)
        if statement.strip():
            raise RuntimeError(
                "native_ensemble_schema_source_incomplete"
            )
        historical_taf_truth_label_migration = (
            _migrate_historical_taf_truth_label_authority(conn)
        )
        run_cycle_mapping_migration = (
            _migrate_native_run_cycle_mapping(conn)
        )
        status = native_ensemble_foundation_schema_status(conn)
        if status["ok"] is not True:
            raise RuntimeError(
                "native_ensemble_foundation_schema_incomplete"
            )
        additive_rows = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing_additive = sorted(
            set(NATIVE_ENSEMBLE_ADDITIVE_TABLES) - additive_rows
        )
        if missing_additive:
            raise RuntimeError(
                "native_ensemble_additive_schema_incomplete"
            )
        republisher_copy = {
            "state": "not_requested",
            "products": 0,
            "member_runs": 0,
            "member_buckets": 0,
            "historical_member_runs": 0,
            "historical_member_buckets": 0,
        }
        if copy_republisher_legacy:
            republisher_copy = _copy_legacy_open_meteo_republisher_rows(
                conn
            )
    except Exception:
        conn.execute("ROLLBACK TO native_ensemble_foundation")
        conn.execute("RELEASE native_ensemble_foundation")
        raise
    conn.execute("RELEASE native_ensemble_foundation")
    if commit:
        conn.commit()
    return {
        "schema": "native_ensemble_foundation_schema_v1",
        "state": "READY",
        **status,
        "additive_tables": list(NATIVE_ENSEMBLE_ADDITIVE_TABLES),
        "run_cycle_mapping_migration":
            run_cycle_mapping_migration,
        "historical_taf_truth_label_migration": (
            historical_taf_truth_label_migration
        ),
        "republisher_historical_oos_migration": (
            republisher_historical_oos_migration
        ),
        "republisher_tables": list(REPUBLISHER_ENSEMBLE_TABLES),
        "republisher_legacy_copy": republisher_copy,
        "performs_legacy_backfill": bool(
            run_cycle_mapping_migration[
                "same_cycle_rows_backfilled"
            ]
        ),
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }
CALIBRATION_FIVE_MODEL_TABLES = (
    "calibration_historical_asof_model_run",
    "calibration_model_station_residual",
    "calibration_model_artifact",
)
CALIBRATION_FIVE_MODEL_CHECK = (
    "CHECK(MODEL_IDIN('ECMWF','GFS','ICON','GEM','CMA'))"
)
POLICY_TABLES = (
    "basket_policy_training_fact",
    "basket_policy_training_label",
    "basket_global_prior_artifact",
    "basket_station_policy_artifact",
    "basket_policy_promotion",
    "basket_policy_activation",
)
MAKER_GRID_OOS_AUTHORITY_TABLES = (
    "maker_grid_oos_artifact_body",
    "maker_grid_oos_promotion_evidence",
    "maker_grid_oos_activation_manifest",
    "maker_grid_oos_activation_pointer",
)

PRODUCTION_TARGET_TABLES = (
    "basket_probability_envelope",
    "basket_probability_bridge_history",
    "basket_probability_bridge_latest",
    "basket_probability_bridge_bucket_probability",
    "basket_regime_artifact",
    "basket_official_ws_snapshot",
    "basket_confirmed_inventory_snapshot",
    "basket_order_state_snapshot",
    "basket_cash_phase_policy_artifact",
    "basket_cash_phase_authority_manifest",
    "basket_strict_delta_policy_artifact",
    "basket_strict_delta_policy_promotion",
    "basket_strict_delta_policy_activation",
    "basket_delta_v2_policy_artifact",
    "basket_delta_v2_policy_promotion",
    "basket_delta_v2_policy_activation",
    "basket_delta_v2_fill_artifact",
    "basket_delta_v2_fill_promotion",
    "basket_p0_preselection_plan",
    "basket_target_observation_facts",
    "basket_target_bound_facts",
    "basket_target_execution_cycles",
    "auto_trade_decisions",
    "basket_target_order_intents",
    "basket_target_capital_pool",
    "basket_target_capital_reservations",
    "basket_target_capital_epoch_migrations",
    "basket_target_execution_version_vectors",
    "basket_target_order_events",
    "basket_target_order_reconciliations",
    "basket_target_writer_lock_wait_telemetry",
)


def apply_maker_grid_oos_authority_schema(
    conn: sqlite3.Connection, *, commit: bool = False
) -> dict[str, object]:
    """Install only the independent exact-grid authority on a caller DB.

    This function is intentionally separate from the legacy Maker authority,
    performs no data migration, and writes zero data rows.
    """

    savepoint = f"maker_grid_oos_schema_{id(conn):x}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        _execute_sql_script_transactional(conn, MAKER_GRID_OOS_AUTHORITY_DDL)
        present = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'maker_grid_oos_%'"
            )
        }
        missing = sorted(set(MAKER_GRID_OOS_AUTHORITY_TABLES) - present)
        if missing:
            raise RuntimeError("maker_grid_oos_authority_schema_incomplete:" + ",".join(missing))
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    if commit:
        conn.commit()
    return {
        "schema": "maker_grid_oos_authority_v1",
        "ok": not missing,
        "tables": list(MAKER_GRID_OOS_AUTHORITY_TABLES),
        "rows_written": 0,
        "promotion_performed": False,
        "migrated_existing_rows": 0,
    }


def apply_delta_v2_authority_schema(
    conn: sqlite3.Connection,
    *,
    commit: bool = False,
) -> dict[str, object]:
    """Install only the append-only v2 authority with no legacy backfill."""

    from .delta_v2_fill_artifact import (
        FILL_DISTRIBUTION_DDL,
        upgrade_fill_promotion_decision_constraint,
    )

    expected = {
        "basket_delta_v2_policy_artifact",
        "basket_delta_v2_policy_promotion",
        "basket_delta_v2_policy_activation",
        "basket_delta_v2_fill_artifact",
        "basket_delta_v2_fill_promotion",
    }
    savepoint = f"delta_v2_schema_{id(conn):x}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        _execute_sql_script_transactional(
            conn,
            DELTA_V2_AUTHORITY_DDL + FILL_DISTRIBUTION_DDL,
        )
        policy_promotion_upgrade = (
            _upgrade_delta_v2_promotion_decision_constraint(conn)
        )
        fill_promotion_upgrade = (
            upgrade_fill_promotion_decision_constraint(conn)
        )
        present = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'basket_delta_v2_%'"
            )
        }
        missing = sorted(expected - present)
        if missing:
            raise RuntimeError(
                "delta_v2_authority_schema_incomplete:" + ",".join(missing)
            )
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    if commit:
        conn.commit()
    return {
        "schema": "delta_basket_score_authority_schema_v2",
        "state": "READY",
        "tables": sorted(expected),
        "performs_legacy_backfill": False,
        "rows_written": 0,
        "policy_promotion_upgrade": policy_promotion_upgrade,
        "fill_promotion_upgrade": fill_promotion_upgrade,
        "writes_order_state": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }


def apply_target_ssot_schema(conn: sqlite3.Connection, *, commit: bool = True) -> dict[str, object]:
    """Apply the schema to the caller-owned connection, never a production DB."""

    _execute_sql_script_transactional(
        conn,
        DDL
        + REPUBLISHER_ENSEMBLE_DDL
        + MAKER_GRID_OOS_AUTHORITY_DDL
        + PROBABILITY_EVIDENCE_DDL,
    )
    # The republisher indexes were only ever written into
    # ``PRODUCTION_TARGET_DDL``, which this path does not run, so a target
    # schema built here has never had them.
    _execute_index_script_where_table_exists(conn, DEFERRED_INDEX_DDL)
    republisher_historical_oos_migration = (
        _migrate_republisher_historical_oos_contract(conn)
    )
    historical_taf_truth_label_migration = (
        _migrate_historical_taf_truth_label_authority(conn)
    )
    forward_taf_optional_tx_upgrade = (
        _upgrade_forward_taf_optional_tx_schema(conn)
    )
    _upgrade_calibration_activation_authority(conn)
    _upgrade_calibration_ensemble_governance(conn)
    _upgrade_empty_calibration_base_fact_schema(conn)
    calibration_timezone_identity_upgrade = (
        _upgrade_calibration_timezone_identity_columns(conn)
    )
    calibration_model_pool_upgrade = (
        _upgrade_empty_calibration_model_pool_schema(conn)
    )
    _upgrade_empty_policy_training_fact_schema(conn)
    if commit:
        conn.commit()
    tables = tuple(
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND "
            "(name LIKE 'calibration_%' OR name LIKE 'basket_%' OR "
            "name LIKE 'maker_grid_oos_%') "
            "ORDER BY name"
        )
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": set(tables) == set(
            CALIBRATION_TABLES
            + NATIVE_ENSEMBLE_SCHEMA_TABLES
            + REPUBLISHER_ENSEMBLE_TABLES
            + POLICY_TABLES
            + MAKER_GRID_OOS_AUTHORITY_TABLES
            + PROBABILITY_EVIDENCE_TABLES
        ),
        "tables": list(tables),
        "calibration_model_pool_upgrade": calibration_model_pool_upgrade,
        "calibration_timezone_identity_upgrade": (
            calibration_timezone_identity_upgrade
        ),
        "forward_taf_optional_tx_upgrade": (
            forward_taf_optional_tx_upgrade
        ),
        "historical_taf_truth_label_migration": (
            historical_taf_truth_label_migration
        ),
        "republisher_historical_oos_migration": (
            republisher_historical_oos_migration
        ),
        "live_order_authorized": False,
        "writes_production_db": False,
    }


def _install_execution_vector_guards(conn: sqlite3.Connection) -> None:
    """Install the exact-table uniqueness and append-only guards."""

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "basket_target_no_execution_vector_cycle_idx "
        "ON basket_target_execution_version_vectors(cycle_id) "
        "WHERE vector_policy='NO_EXECUTION'"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS basket_target_version_vector_no_update "
        "BEFORE UPDATE ON basket_target_execution_version_vectors "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS basket_target_version_vector_no_delete "
        "BEFORE DELETE ON basket_target_execution_version_vectors "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END"
    )


def upgrade_execution_vector_contract(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Remove the retired LEGACY_CAS shape without inventing evidence.

    Exact ``EXECUTION`` and ``NO_EXECUTION`` rows can be copied losslessly
    because all authoritative fields already exist in the predecessor table.
    Any actual LEGACY_CAS row fails closed and requires an explicit offline
    rebuild; it cannot be promoted into the exact contract by inference.
    """

    table = "basket_target_execution_version_vectors"
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if table_row is None:
        return {"state": "table_absent", "rows_migrated": 0}
    table_sql = str(table_row[0] or "")
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
    }
    retired_columns = {
        "posterior_version",
        "posterior_sha256",
        "snapshot_sha256",
        "quote_version",
        "inventory_version",
        "order_state_version",
    }
    dual_columns = {
        "economic_policy_schema",
        "economic_policy_artifact_sha256",
    }
    if (
        not retired_columns.intersection(columns)
        and "LEGACY_CAS" not in table_sql
        and dual_columns.issubset(columns)
    ):
        _install_execution_vector_guards(conn)
        return {"state": "already_dual_schema", "rows_migrated": 0}
    create_statement = ""
    statement = ""
    for line in PRODUCTION_LEDGER_DDL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql.startswith(
                "CREATE TABLE IF NOT EXISTS "
                "basket_target_execution_version_vectors"
            ):
                create_statement = sql
                break
            statement = ""
    if not create_statement:
        raise RuntimeError("execution_vector_exact_ddl_missing")
    row_count = int(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    )
    if "vector_policy" not in columns:
        if row_count:
            raise RuntimeError(
                "legacy_execution_vectors_require_offline_rebuild:"
                "vector_policy_missing"
            )
        conn.execute(
            "DROP TRIGGER IF EXISTS basket_target_version_vector_no_update"
        )
        conn.execute(
            "DROP TRIGGER IF EXISTS basket_target_version_vector_no_delete"
        )
        conn.execute(
            "DROP INDEX IF EXISTS basket_target_execution_vector_cycle_idx"
        )
        conn.execute(
            f"ALTER TABLE {table} RENAME TO "
            "basket_target_execution_version_vectors_retired"
        )
        conn.execute(create_statement)
        conn.execute(
            "DROP TABLE basket_target_execution_version_vectors_retired"
        )
        _install_execution_vector_guards(conn)
        return {"state": "migrated_empty_legacy", "rows_migrated": 0}
    policies = {
        str(row[0] or "")
        for row in conn.execute(
            f"SELECT DISTINCT vector_policy FROM {table}"
        ).fetchall()
    }
    unsupported = sorted(policies.difference({"EXECUTION", "NO_EXECUTION"}))
    if unsupported:
        raise RuntimeError(
            "legacy_execution_vectors_require_offline_rebuild:"
            + ",".join(unsupported)
        )
    source_columns = (
        "order_intent_id",
        "cycle_id",
        "decision_id",
        "training_fact_id",
        "vector_policy",
        "target_price_id",
        "quote_observed_at",
        "quote_expires_at",
        "probability_sha256",
        "official_ws_quote_sha256",
        "inventory_sha256",
        "order_state_sha256",
        "station_policy_sha256",
        "cash_policy_decision_sha256",
        "strict_delta_artifact_sha256",
        "vector_json",
        "vector_sha256",
        "created_at",
    )
    missing = sorted(set(source_columns).difference(columns))
    if missing:
        raise RuntimeError(
            "execution_vector_exact_migration_columns_missing:"
            + ",".join(missing)
        )
    conn.execute("DROP TRIGGER IF EXISTS basket_target_version_vector_no_update")
    conn.execute("DROP TRIGGER IF EXISTS basket_target_version_vector_no_delete")
    conn.execute("DROP INDEX IF EXISTS basket_target_execution_vector_cycle_idx")
    conn.execute(
        f"ALTER TABLE {table} RENAME TO "
        "basket_target_execution_version_vectors_retired"
    )
    conn.execute(create_statement)
    target_columns = source_columns[:-4] + (
        "strict_delta_artifact_sha256",
        "economic_policy_schema",
        "economic_policy_artifact_sha256",
        "vector_json",
        "vector_sha256",
        "created_at",
    )
    names = ",".join(target_columns)
    source_prefix = ",".join(source_columns[:-4])
    economic_schema_expr = (
        "economic_policy_schema"
        if "economic_policy_schema" in columns
        else "NULL"
    )
    economic_artifact_expr = (
        "economic_policy_artifact_sha256"
        if "economic_policy_artifact_sha256" in columns
        else "NULL"
    )
    conn.execute(
        f"INSERT INTO {table}({names}) "
        f"SELECT {source_prefix},strict_delta_artifact_sha256,"
        f"{economic_schema_expr},{economic_artifact_expr},"
        "vector_json,vector_sha256,created_at "
        "FROM basket_target_execution_version_vectors_retired"
    )
    conn.execute("DROP TABLE basket_target_execution_version_vectors_retired")
    _install_execution_vector_guards(conn)
    return {"state": "migrated_exact", "rows_migrated": row_count}


REGIME_ARTIFACT_COLUMNS = (
    "artifact_id",
    "station_id",
    "event_date",
    "stage",
    "checkpoint_id",
    "settlement_timezone",
    "probability_sha256",
    "as_of_utc",
    "valid_from_utc",
    "valid_until_utc",
    "artifact_json",
    "content_sha256",
    "state",
    "created_at",
)


def inspect_regime_superseded_schema(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='basket_regime_artifact'"
    ).fetchone()
    if table_row is None:
        return {
            "ok": False,
            "table_present": False,
            "missing_columns": list(REGIME_ARTIFACT_COLUMNS),
            "supports_superseded": False,
            "controlled_update_trigger_ok": False,
            "no_delete_trigger_ok": False,
            "committed_partial_unique_ok": False,
        }
    table_sql = " ".join(str(table_row[0] or "").upper().split())
    columns = tuple(
        str(row[1])
        for row in conn.execute("PRAGMA table_info(basket_regime_artifact)")
    )
    triggers = {
        str(row[0]): " ".join(str(row[1] or "").upper().split())
        for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='basket_regime_artifact'"
        )
    }
    controlled_sql = triggers.get(
        "basket_regime_artifact_controlled_update",
        "",
    )
    immutable_columns = tuple(
        column for column in REGIME_ARTIFACT_COLUMNS if column != "state"
    )
    controlled_update_ok = bool(
        "OLD.STATE='COMMITTED'" in controlled_sql
        and "NEW.STATE='SUPERSEDED'" in controlled_sql
        and "REGIME_UPDATE_FORBIDDEN" in controlled_sql
        and all(
            f"NEW.{column.upper()} IS OLD.{column.upper()}" in controlled_sql
            for column in immutable_columns
        )
    )
    partial_unique_ok = False
    for index_row in conn.execute(
        "PRAGMA index_list(basket_regime_artifact)"
    ):
        if not bool(index_row[2]):
            continue
        index_name = str(index_row[1])
        index_columns = tuple(
            str(row[2])
            for row in conn.execute(
                f"PRAGMA index_info('{index_name}')"
            )
        )
        if (
            index_columns
            == (
                "station_id",
                "event_date",
                "stage",
                "checkpoint_id",
                "probability_sha256",
            )
            and bool(index_row[4] if len(index_row) > 4 else False)
        ):
            index_sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            index_sql = " ".join(
                str(index_sql_row[0] if index_sql_row else "").upper().split()
            )
            partial_unique_ok = "WHERE STATE='COMMITTED'" in index_sql
            if partial_unique_ok:
                break
    missing_columns = sorted(set(REGIME_ARTIFACT_COLUMNS) - set(columns))
    exact_columns = columns == REGIME_ARTIFACT_COLUMNS
    supports_superseded = "SUPERSEDED" in table_sql
    no_delete_ok = (
        "basket_regime_artifact_no_delete" in triggers
        and "RAISE(ABORT,'APPEND_ONLY')" in triggers[
            "basket_regime_artifact_no_delete"
        ]
    )
    legacy_no_update_absent = (
        "basket_regime_artifact_no_update" not in triggers
    )
    ok = bool(
        exact_columns
        and supports_superseded
        and controlled_update_ok
        and no_delete_ok
        and legacy_no_update_absent
        and partial_unique_ok
    )
    return {
        "ok": ok,
        "table_present": True,
        "columns_exact": exact_columns,
        "missing_columns": missing_columns,
        "supports_superseded": supports_superseded,
        "controlled_update_trigger_ok": controlled_update_ok,
        "no_delete_trigger_ok": no_delete_ok,
        "legacy_no_update_absent": legacy_no_update_absent,
        "committed_partial_unique_ok": partial_unique_ok,
    }


def upgrade_regime_superseded_schema(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Atomically rebuild only the regime table into its supersession form."""

    before = inspect_regime_superseded_schema(conn)
    if before.get("ok") is True:
        row_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM basket_regime_artifact"
            ).fetchone()[0]
        )
        return {
            "state": "already_current",
            "rows_preserved": row_count,
            "before": before,
            "after": before,
        }
    if before.get("table_present") is not True:
        _execute_sql_script_transactional(conn, REGIME_ARTIFACT_DDL)
        after = inspect_regime_superseded_schema(conn)
        if after.get("ok") is not True:
            raise RuntimeError("regime_superseded_schema_create_failed")
        return {
            "state": "created",
            "rows_preserved": 0,
            "before": before,
            "after": after,
        }
    columns = tuple(
        str(row[1])
        for row in conn.execute("PRAGMA table_info(basket_regime_artifact)")
    )
    if columns != REGIME_ARTIFACT_COLUMNS:
        raise RuntimeError("regime_superseded_schema_columns_not_exact")
    invalid_states = [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT state FROM basket_regime_artifact "
            "WHERE state NOT IN ('COMMITTED','QUARANTINED','SUPERSEDED') "
            "LIMIT 2"
        )
    ]
    if invalid_states:
        raise RuntimeError("regime_superseded_schema_state_invalid")
    duplicate = conn.execute(
        "SELECT 1 FROM basket_regime_artifact WHERE state='COMMITTED' "
        "GROUP BY station_id,event_date,stage,checkpoint_id,probability_sha256 "
        "HAVING COUNT(*)>1 LIMIT 1"
    ).fetchone()
    if duplicate is not None:
        raise RuntimeError("regime_committed_checkpoint_not_unique")
    incoming_fk_tables = []
    for table_row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'"
    ):
        table = str(table_row[0])
        if table == "basket_regime_artifact":
            continue
        if any(
            str(fk_row[2]) == "basket_regime_artifact"
            for fk_row in conn.execute(f'PRAGMA foreign_key_list("{table}")')
        ):
            incoming_fk_tables.append(table)
    if incoming_fk_tables:
        raise RuntimeError(
            "regime_superseded_incoming_fk_requires_explicit_rebuild:"
            + ",".join(sorted(incoming_fk_tables))
        )
    archive = "basket_regime_artifact_superseded_source_v1"
    temporary = "basket_regime_artifact_superseded_migration_v1"
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name IN (?,?) LIMIT 1",
        (archive, temporary),
    ).fetchone() is not None:
        raise RuntimeError("regime_superseded_migration_name_collision")
    row_count = int(
        conn.execute("SELECT COUNT(*) FROM basket_regime_artifact").fetchone()[0]
    )
    names = ",".join(REGIME_ARTIFACT_COLUMNS)
    conn.execute("SAVEPOINT regime_superseded_schema_upgrade")
    try:
        conn.execute(
            f"ALTER TABLE basket_regime_artifact RENAME TO {archive}"
        )
        conn.execute(_regime_artifact_table_sql(temporary))
        conn.execute(
            f"INSERT INTO {temporary}({names}) "
            f"SELECT {names} FROM {archive}"
        )
        copied = int(
            conn.execute(f"SELECT COUNT(*) FROM {temporary}").fetchone()[0]
        )
        if copied != row_count:
            raise RuntimeError("regime_superseded_row_count_drift")
        conn.execute(f"DROP TABLE {archive}")
        conn.execute(
            f"ALTER TABLE {temporary} RENAME TO basket_regime_artifact"
        )
        _execute_sql_script_transactional(conn, REGIME_ARTIFACT_GUARD_DDL)
        foreign_key_errors = conn.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_errors:
            raise RuntimeError("regime_superseded_foreign_key_check_failed")
        after = inspect_regime_superseded_schema(conn)
        if after.get("ok") is not True:
            raise RuntimeError("regime_superseded_post_schema_invalid")
    except Exception:
        conn.execute("ROLLBACK TO regime_superseded_schema_upgrade")
        conn.execute("RELEASE regime_superseded_schema_upgrade")
        raise
    conn.execute("RELEASE regime_superseded_schema_upgrade")
    return {
        "state": "migrated",
        "rows_preserved": row_count,
        "before": before,
        "after": after,
    }


def apply_production_ssot_schema(conn: sqlite3.Connection, *, commit: bool = True) -> dict[str, object]:
    """Apply the unified artifact/input schema during a controlled migration.

    This function is intentionally not called by the resident daemon.  It is
    safe for an explicit migration connection and does not create orders,
    intents, outbox rows, or gateway state.
    """

    from .delta_v2_fill_artifact import (
        FILL_DISTRIBUTION_DDL,
        upgrade_fill_promotion_decision_constraint,
    )

    canonical_preflight = inspect_canonical_ssot_schema(conn)
    canonical_empty_upgrade: dict[str, object] = {"state": "not_required"}
    if canonical_preflight["state"] in {"V3", "V4", "V5", "V6", "V7", "V8", "V9", "V10", "V11", "V12", "V13"}:
        row_count = int(
            conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0]
        )
        if row_count:
            required = {
                "V3": "v4",
                "V4": "v5",
                "V5": "v6",
                "V6": "v7",
                "V7": "v8",
                "V8": "v9",
                "V9": "v10",
                "V10": "v11",
                "V11": "v12",
                "V12": "v13",
                "V13": "v14",
            }[str(canonical_preflight["state"])]
            raise RuntimeError(
                f"canonical_contract_schema_{required}_migration_required"
            )
        # Some controlled installers create the canonical table before this
        # full production schema pass.  An empty historical table has no fact
        # identity to preserve, so replace only that empty shell with V14 DDL.
        # Any non-empty store must use the separately receipted migration.
        for trigger_name in canonical_preflight.get("trigger_names", ()):
            conn.execute(f"DROP TRIGGER {trigger_name}")
        conn.execute("DROP TABLE canonical_ssot_fact")
        canonical_empty_upgrade = {
            "state": f"empty_{str(canonical_preflight['state']).lower()}_replaced_with_v14",
            "rows_preserved": 0,
        }
    elif canonical_preflight["state"] not in {"ABSENT", "V14"}:
        raise RuntimeError("canonical_contract_schema_v14_migration_required")

    _assert_target_decision_table_compatible(conn)
    execution_vector_upgrade = upgrade_execution_vector_contract(conn)
    _execute_sql_script_transactional(
        conn,
        DDL
        + MAKER_GRID_OOS_AUTHORITY_DDL
        + PRODUCTION_TARGET_DDL
        + DELTA_V2_AUTHORITY_DDL
        + FILL_DISTRIBUTION_DDL
        + PRODUCTION_LEDGER_DDL
        + PROBABILITY_EVIDENCE_DDL,
        # Keep the canonical table and its two append-only guards one schema
        # unit; REPUBLISHER_ENSEMBLE_DDL is not part of this migration path.
    )
    deferred_indexes = _execute_index_script_where_table_exists(
        conn, DEFERRED_INDEX_DDL
    )
    _execute_sql_script_transactional(conn, CANONICAL_FACT_GUARD_DDL)
    historical_taf_truth_label_migration = (
        _migrate_historical_taf_truth_label_authority(conn)
    )
    forward_taf_optional_tx_upgrade = (
        _upgrade_forward_taf_optional_tx_schema(conn)
    )
    delta_v2_promotion_upgrade = (
        _upgrade_delta_v2_promotion_decision_constraint(conn)
    )
    delta_v2_fill_promotion_upgrade = (
        upgrade_fill_promotion_decision_constraint(conn)
    )
    regime_superseded_upgrade = upgrade_regime_superseded_schema(conn)
    execution_deadline_upgrade = _upgrade_target_execution_deadline_columns(conn)
    _upgrade_calibration_activation_authority(conn)
    _upgrade_calibration_ensemble_governance(conn)
    _upgrade_empty_calibration_base_fact_schema(conn)
    calibration_timezone_identity_upgrade = (
        _upgrade_calibration_timezone_identity_columns(conn)
    )
    calibration_model_pool_upgrade = (
        _upgrade_empty_calibration_model_pool_schema(conn)
    )
    _upgrade_empty_policy_training_fact_schema(conn)
    _upgrade_probability_envelope_stage_constraint(conn)
    strict_delta_candidate_upgrade = (
        _upgrade_strict_delta_candidate_decision_constraint(conn)
    )
    settlement_timezone_upgrade = _upgrade_settlement_timezone_columns(conn)
    observation_fact_timezone_upgrade = (
        _upgrade_basket_target_observation_fact_settlement_timezone(conn)
    )
    quote_freshness_upgrade = _upgrade_quote_freshness_columns(conn)
    # Second pass, after every upgrade above has put its table into the shape
    # the DDL describes.  The first call runs before them and skips any index
    # whose table or columns have not arrived yet; without this the skip
    # would be permanent on exactly the databases that need the index most --
    # the ones being upgraded.  It has to sit after the *last* upgrade, not a
    # convenient one in the middle: settlement_timezone is restored here, and
    # two of the envelope indexes read it.
    deferred_indexes = sorted(
        set(deferred_indexes)
        | set(_execute_index_script_where_table_exists(conn, DEFERRED_INDEX_DDL))
    )
    if commit:
        conn.commit()
    tables = tuple(
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND "
                "(name LIKE 'calibration_%' OR name LIKE 'basket_%' OR "
                "name LIKE 'maker_grid_oos_%' "
                "OR name='auto_trade_decisions' OR name='canonical_ssot_fact') ORDER BY name"
        )
    )
    expected = set(
        CANONICAL_CONTRACT_TABLES
        + CALIBRATION_TABLES
        + NATIVE_ENSEMBLE_SCHEMA_TABLES
        + POLICY_TABLES
        + MAKER_GRID_OOS_AUTHORITY_TABLES
        + PRODUCTION_TARGET_TABLES
        + PROBABILITY_EVIDENCE_TABLES
    )
    canonical_inspection = inspect_canonical_ssot_schema(conn)
    canonical_contract_schema_ok = (
        canonical_inspection["ok"] is True
        # V14 is the minimum this consumer needs, not the exact version it
        # runs on.  Later canonical versions are supersets and stay valid.
        and canonical_state_at_least(canonical_inspection["state"], "V14")
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": expected.issubset(set(tables)) and canonical_contract_schema_ok,
        "tables": list(tables),
        "target_tables": list(PRODUCTION_TARGET_TABLES),
        "canonical_contract_schema_ok": canonical_contract_schema_ok,
        "canonical_contract_schema_errors": canonical_inspection["errors"],
        "canonical_empty_upgrade": canonical_empty_upgrade,
        "settlement_timezone_upgrade": settlement_timezone_upgrade,
        "observation_fact_timezone_upgrade": observation_fact_timezone_upgrade,
        "quote_freshness_upgrade": quote_freshness_upgrade,
        "execution_deadline_upgrade": execution_deadline_upgrade,
        "execution_vector_upgrade": execution_vector_upgrade,
        "regime_superseded_upgrade": regime_superseded_upgrade,
        "calibration_model_pool_upgrade": calibration_model_pool_upgrade,
        "calibration_timezone_identity_upgrade": (
            calibration_timezone_identity_upgrade
        ),
        "forward_taf_optional_tx_upgrade": (
            forward_taf_optional_tx_upgrade
        ),
        "historical_taf_truth_label_migration": (
            historical_taf_truth_label_migration
        ),
        "strict_delta_candidate_upgrade": strict_delta_candidate_upgrade,
        "canonical_release_state": CANONICAL_V14_RELEASE_STATE,
        "current_pointer_switched": False,
        "service_start_authorized": False,
        "basket_consumption_authorized": False,
        "formal_live_authorized": False,
        "live_order_authorized": False,
        "writes_orders": False,
        "writes_production_db": True,
    }


def _upgrade_forward_taf_optional_tx_schema(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Preserve native TAF receipts when the bulletin omits optional TX."""

    table = "calibration_forward_taf_capture"
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        return {"state": "not_present", "rows_copied": 0}
    columns = {
        str(row[1]): {
            "notnull": int(row[3]),
        }
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if (
        "tx_available" in columns
        and columns.get("tx_c", {}).get("notnull") == 0
    ):
        return {"state": "already_current", "rows_copied": 0}

    legacy = "calibration_forward_taf_capture_legacy_required_tx_v1"
    collision = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (legacy,),
    ).fetchone()
    if collision is not None:
        raise RuntimeError("forward_taf_optional_tx_legacy_collision")
    row_count = int(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    )
    conn.execute("DROP TRIGGER IF EXISTS calibration_forward_taf_capture_no_update")
    conn.execute("DROP TRIGGER IF EXISTS calibration_forward_taf_capture_no_delete")
    conn.execute("DROP INDEX IF EXISTS calibration_forward_taf_capture_scope_idx")
    conn.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
    conn.execute(
        f"""
        CREATE TABLE {table} (
          taf_capture_id TEXT PRIMARY KEY,
          station_id TEXT NOT NULL,
          event_date TEXT NOT NULL,
          settlement_timezone TEXT NOT NULL,
          revision_id TEXT NOT NULL,
          revision_kind TEXT NOT NULL,
          revision_sequence INTEGER NOT NULL CHECK(revision_sequence>=1),
          issued_at_utc TEXT NOT NULL,
          received_at_utc TEXT NOT NULL,
          provider_available_at_utc TEXT NOT NULL,
          valid_from_utc TEXT NOT NULL,
          valid_until_utc TEXT NOT NULL,
          source_object_sha256 TEXT NOT NULL
            CHECK(length(source_object_sha256)=64),
          source_capture_sha256 TEXT NOT NULL
            CHECK(length(source_capture_sha256)=64),
          tx_c REAL,
          tx_available INTEGER NOT NULL CHECK(tx_available IN (0,1)),
          cloud_cover_fraction REAL NOT NULL
            CHECK(cloud_cover_fraction>=0 AND cloud_cover_fraction<=1),
          precipitation_probability REAL NOT NULL
            CHECK(precipitation_probability>=0 AND
                  precipitation_probability<=1),
          wind_speed_mps REAL NOT NULL CHECK(wind_speed_mps>=0),
          source_class TEXT NOT NULL CHECK(
            source_class='official_forward_native_receipt'
          ),
          authority_scope TEXT NOT NULL CHECK(
            authority_scope='PRODUCTION_NO_LIVE_ONLY'
          ),
          content_sha256 TEXT NOT NULL UNIQUE
            CHECK(length(content_sha256)=64),
          CHECK((tx_available=1 AND tx_c IS NOT NULL) OR
                (tx_available=0 AND tx_c IS NULL)),
          UNIQUE(station_id,event_date,revision_id)
        )
        """
    )
    old_columns = [
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({legacy})")
    ]
    copied_columns = [
        name
        for name in old_columns
        if name != "tx_available"
    ]
    conn.execute(
        f"INSERT INTO {table}("
        + ",".join(copied_columns)
        + ",tx_available) SELECT "
        + ",".join(copied_columns)
        + f",1 FROM {legacy}"
    )
    copied = int(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    )
    if copied != row_count:
        raise RuntimeError("forward_taf_optional_tx_copy_count_mismatch")
    conn.execute(f"DROP TABLE {legacy}")
    conn.execute(
        "CREATE INDEX calibration_forward_taf_capture_scope_idx "
        "ON calibration_forward_taf_capture("
        "station_id,event_date,provider_available_at_utc,"
        "revision_sequence)"
    )
    conn.execute(
        "CREATE TRIGGER calibration_forward_taf_capture_no_update "
        "BEFORE UPDATE ON calibration_forward_taf_capture "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END"
    )
    conn.execute(
        "CREATE TRIGGER calibration_forward_taf_capture_no_delete "
        "BEFORE DELETE ON calibration_forward_taf_capture "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END"
    )
    return {"state": "upgraded", "rows_copied": copied}


def _upgrade_target_execution_deadline_columns(conn: sqlite3.Connection) -> dict[str, object]:
    """Add the target deadline columns during a controlled schema migration."""

    upgraded: list[str] = []
    for table in ("auto_trade_decisions", "basket_target_order_intents"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            continue
        columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if "target_execution_hard_deadline_at" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN target_execution_hard_deadline_at TEXT"
            )
            upgraded.append(table)
    return {"state": "upgraded" if upgraded else "already_current", "tables": upgraded}


def _assert_target_decision_table_compatible(conn: sqlite3.Connection) -> None:
    """Prevent the target DDL from attaching triggers to the legacy table.

    ``auto_trade_decisions`` predates the target ledger and has the same name
    but a different primary key and lifecycle.  ``CREATE TABLE IF NOT EXISTS``
    would otherwise appear successful, then leave M5 unusable while adding
    target append-only triggers to the old writer surface.  A controlled
    migration must first quiesce/rename that legacy table in its own packet.
    """

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_trade_decisions'"
    ).fetchone()
    if row is None:
        return
    required = {
        "decision_id", "cycle_id", "plan_id", "station_id", "event_date",
        "strategy_lane", "decided_state", "will_order", "reason_codes_json",
        "details_json", "decision_sha256", "created_at",
    }
    columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(auto_trade_decisions)")}
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(
            "target_ledger_auto_trade_decisions_legacy_collision:" + ",".join(missing)
        )


def production_ssot_schema_status(conn: sqlite3.Connection) -> dict[str, object]:
    """Read-only status used by the daemon and acceptance scripts."""

    rows = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = sorted(
        set(
            CANONICAL_CONTRACT_TABLES
            + CALIBRATION_TABLES
            + NATIVE_ENSEMBLE_SCHEMA_TABLES
            + POLICY_TABLES
            + MAKER_GRID_OOS_AUTHORITY_TABLES
            + PRODUCTION_TARGET_TABLES
            + PROBABILITY_EVIDENCE_TABLES
        )
        - rows
    )
    maker_grid_oos_missing_tables = sorted(
        set(MAKER_GRID_OOS_AUTHORITY_TABLES) - rows
    )
    maker_grid_oos_schema_ok = not maker_grid_oos_missing_tables
    canonical_inspection = inspect_canonical_ssot_schema(conn)
    canonical_contract_schema_ok = (
        canonical_inspection["ok"] is True
        # V14 is the minimum this consumer needs, not the exact version it
        # runs on.  Later canonical versions are supersets and stay valid.
        and canonical_state_at_least(canonical_inspection["state"], "V14")
    )
    probability_evidence_status = probability_evidence_schema_status(conn)
    probability_evidence_schema_ok = (
        probability_evidence_status.get("ok") is True
    )
    probability_sql = next(
        (
            str(row[0])
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='basket_probability_envelope'"
            ).fetchall()
            if row[0]
        ),
        "",
    )
    probability_stage_contract_ok = (
        "'P0'" in probability_sql
        and "'P0-M'" in probability_sql
        and "'P1'" in probability_sql
        and "'P0-F'" not in probability_sql
    )
    strict_delta_promotion_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='basket_strict_delta_policy_promotion'"
    ).fetchone()
    strict_delta_candidate_contract_ok = (
        strict_delta_promotion_sql_row is not None
        and "'CANDIDATE'" in str(strict_delta_promotion_sql_row[0] or "")
    )
    regime_required_columns = {
        "artifact_id", "station_id", "event_date", "stage", "checkpoint_id",
        "settlement_timezone", "probability_sha256", "as_of_utc",
        "valid_from_utc", "valid_until_utc", "artifact_json",
        "content_sha256", "state", "created_at",
    }
    regime_missing_columns = (
        sorted(
            regime_required_columns.difference({
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(basket_regime_artifact)"
                )
            })
        )
        if "basket_regime_artifact" in rows
        else sorted(regime_required_columns)
    )
    regime_superseded_status = inspect_regime_superseded_schema(conn)
    regime_schema_ok = (
        not regime_missing_columns
        and regime_superseded_status.get("ok") is True
    )
    ledger_missing_columns = {
        "basket_target_observation_facts": sorted(
            {"settlement_timezone"}
            - {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(basket_target_observation_facts)"
                )
            }
        ) if "basket_target_observation_facts" in rows else ["table_missing"],
        "auto_trade_decisions": sorted(
            {"decision_id", "cycle_id", "plan_id", "decided_state", "will_order", "target_execution_hard_deadline_at", "reason_codes_json", "decision_sha256"}
            - {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(auto_trade_decisions)")
            }
        ),
        "basket_target_order_intents": sorted(
            {"order_intent_id", "decision_id", "cycle_id", "target_execution_hard_deadline_at", "intent_json", "intent_sha256"}
            - {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(basket_target_order_intents)")
            }
        ) if "basket_target_order_intents" in rows else ["table_missing"],
    } if "auto_trade_decisions" in rows else {"auto_trade_decisions": ["table_missing"]}
    ledger_schema_ok = all(not values for values in ledger_missing_columns.values()) and all(
        table in rows for table in (
            "basket_target_observation_facts",
            "basket_target_bound_facts",
            "basket_target_execution_cycles",
            "auto_trade_decisions",
            "basket_target_order_intents",
            "basket_target_capital_pool",
            "basket_target_capital_reservations",
            "basket_target_capital_epoch_migrations",
        )
    )
    calibration_required_columns = {
        "calibration_station_registry": {"source_role"},
        "calibration_station_source_authority": {
            "authority_version", "activated_at_utc",
            "legacy_registry_content_sha256", "lineage_json",
        },
        "calibration_station_daily_high_truth": {
            "canonical_committed_at_utc", "timestamps_complete", "source_role",
            "observation_ids_json", "authority_scope", "settlement_timezone",
        },
        "calibration_historical_asof_model_run": {
            "timestamps_complete", "maturity_key", "provider_id", "provider_role",
            "source_class", "aggregator_or_republisher", "open_meteo",
            "source_url", "raw_object_ref", "asof_status", "capture_modes_json",
            "historical_replay_eligible", "prospective_strict_asof_eligible",
            "authority_scope", "settlement_timezone",
        },
        "calibration_model_station_residual": {
            "maturity_key", "historical_replay_eligible",
            "prospective_strict_asof_eligible", "authority_scope", "content_sha256",
        },
        "calibration_ensemble_artifact": {
            "selection_evidence_sha256", "previous_ensemble_artifact_sha256",
            "weight_method", "effective_sample_n", "max_weight_delta",
            "train_start_date", "train_end_date", "recent_start_date",
            "recent_end_date", "holdout_start_date", "holdout_end_date",
            "oos_metrics_json",
        },
    }
    calibration_missing_columns = {
        table: sorted(required - {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        })
        for table, required in calibration_required_columns.items()
        if table in rows
    }
    calibration_base_schema_ok = (
        all(table in rows for table in calibration_required_columns)
        and all(not missing_columns for missing_columns in calibration_missing_columns.values())
    )
    calibration_model_pool_schema = inspect_calibration_model_pool_schema(conn)
    calibration_model_pool_schema_ok = (
        calibration_model_pool_schema.get("ok") is True
    )
    settlement_timezone_missing_columns = {
        table: ["settlement_timezone"]
        for table in (
            "basket_probability_envelope",
            "basket_official_ws_snapshot",
            "basket_official_ws_evidence_cache",
        )
        if table in rows
        and "settlement_timezone"
        not in {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
    }
    quote_freshness_required_columns = {
        "quote_expires_at",
        "quote_freshness_policy_sha256",
    }
    quote_freshness_missing_columns = (
        sorted(
            quote_freshness_required_columns.difference(
                {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(basket_official_ws_snapshot)"
                    )
                }
            )
        )
        if "basket_official_ws_snapshot" in rows
        else sorted(quote_freshness_required_columns)
    )
    policy_training_required_columns = {
    "basket_policy_training_fact": {
            "settlement_timezone", "decision_scope_key", "captured_at_utc",
            "source_sequence", "quote_observed_at", "quote_expires_at",
            "quote_freshness_policy_sha256", "probability_envelope_id",
            "posterior_version", "official_exclusion_mask_sha256",
            "ws_snapshot_id", "quote_version", "inventory_snapshot_id",
            "inventory_version", "order_state_snapshot_id",
            "order_state_version", "cycle_id", "decision_id",
            "decision_sha256", "plan_sha256", "intent_count",
            "intent_set_sha256", "policy_version_vector_json",
            "feature_json", "authority_scope", "created_at_utc",
        },
        "basket_policy_training_label": {
            "fact_id", "truth_id", "truth_revision_no", "truth_sha256",
            "winning_bucket", "settlement_closed_at_utc",
            "supersedes_label_id", "content_sha256",
        },
    }
    policy_training_missing_columns = {
        table: sorted(required - {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        })
        for table, required in policy_training_required_columns.items()
        if table in rows
    }
    policy_training_constraint_ok = _policy_training_fact_constraints_ok(conn)
    policy_training_schema_ok = (
        all(table in rows for table in policy_training_required_columns)
        and all(
            not missing_columns
            for missing_columns in policy_training_missing_columns.values()
        )
        and policy_training_constraint_ok
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": (
            not missing
            and canonical_contract_schema_ok
            and probability_stage_contract_ok
            and calibration_base_schema_ok
            and calibration_model_pool_schema_ok
            and not settlement_timezone_missing_columns
            and not quote_freshness_missing_columns
            and policy_training_schema_ok
            and maker_grid_oos_schema_ok
            and probability_evidence_schema_ok
            and regime_schema_ok
            and strict_delta_candidate_contract_ok
        ),
        "missing_tables": missing,
        "canonical_contract_schema_ok": canonical_contract_schema_ok,
        "canonical_contract_schema_errors": canonical_inspection["errors"],
        "canonical_release_state": CANONICAL_V14_RELEASE_STATE,
        "current_pointer_switched": False,
        "service_start_authorized": False,
        "basket_consumption_authorized": False,
        "formal_live_authorized": False,
        "probability_evidence_schema": probability_evidence_status,
        "target_tables_present": not (set(PRODUCTION_TARGET_TABLES) - rows),
        "probability_stage_contract_ok": probability_stage_contract_ok,
        "strict_delta_candidate_contract_ok": (
            strict_delta_candidate_contract_ok
        ),
        "ledger_schema_ok": ledger_schema_ok,
        "ledger_missing_columns": ledger_missing_columns,
        "calibration_base_schema_ok": calibration_base_schema_ok,
        "calibration_missing_columns": calibration_missing_columns,
        "calibration_model_pool_schema_ok": calibration_model_pool_schema_ok,
        "calibration_model_pool_schema": calibration_model_pool_schema,
        "settlement_timezone_missing_columns": settlement_timezone_missing_columns,
        "quote_freshness_missing_columns": quote_freshness_missing_columns,
        "policy_training_schema_ok": policy_training_schema_ok,
        "policy_training_constraint_ok": policy_training_constraint_ok,
        "policy_training_missing_columns": policy_training_missing_columns,
        "maker_grid_oos_schema_ok": maker_grid_oos_schema_ok,
        "maker_grid_oos_missing_tables": maker_grid_oos_missing_tables,
        "regime_schema_ok": regime_schema_ok,
        "regime_missing_columns": regime_missing_columns,
        "regime_superseded_schema": regime_superseded_status,
    }


def inspect_calibration_model_pool_schema(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Read the exact five-model CHECK contract from all three authorities."""

    table_status: dict[str, dict[str, object]] = {}
    for table in CALIBRATION_FIVE_MODEL_TABLES:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        normalized = (
            "".join(str(row[0] or "").upper().split())
            if row is not None
            else ""
        )
        exact_check = CALIBRATION_FIVE_MODEL_CHECK in normalized
        table_status[table] = {
            "present": row is not None,
            "five_model_check": exact_check,
        }
    missing_or_legacy = sorted(
        table
        for table, status in table_status.items()
        if status["present"] is not True or status["five_model_check"] is not True
    )
    return {
        "ok": not missing_or_legacy,
        "candidate_models": ["ECMWF", "GFS", "ICON", "GEM", "CMA"],
        "tables": table_status,
        "missing_or_legacy_tables": missing_or_legacy,
    }


def upgrade_calibration_model_pool_schema_preserving_rows(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Replace only the three model-id CHECKs while preserving every row."""

    before = inspect_calibration_model_pool_schema(conn)
    if before["ok"] is True:
        return {
            "state": "already_current",
            "candidate_model_count": 5,
            "rows_preserved": {
                table: int(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in CALIBRATION_FIVE_MODEL_TABLES
            },
        }
    if any(
        status["present"] is not True
        for status in before["tables"].values()
    ):
        raise RuntimeError("calibration_model_pool_migration_table_missing")

    unexpected_inbound: list[str] = []
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        table = str(row[0])
        for foreign_key in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
            parent = str(foreign_key[2])
            if parent not in CALIBRATION_FIVE_MODEL_TABLES:
                continue
            if not (
                table == "calibration_model_station_residual"
                and parent == "calibration_historical_asof_model_run"
            ):
                unexpected_inbound.append(f"{table}->{parent}")
    if unexpected_inbound:
        raise RuntimeError(
            "calibration_model_pool_migration_unknown_foreign_keys:"
            + ",".join(sorted(unexpected_inbound))
        )

    row_counts = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in CALIBRATION_FIVE_MODEL_TABLES
    }
    for trigger in (
        "calibration_model_run_no_update",
        "calibration_model_run_no_delete",
        "calibration_residual_no_update",
        "calibration_residual_no_delete",
        "calibration_model_artifact_no_update",
        "calibration_model_artifact_no_delete",
    ):
        conn.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
    conn.execute("DROP INDEX IF EXISTS calibration_model_scope_idx")

    _execute_sql_script_transactional(
        conn,
        """
CREATE TABLE calibration_historical_asof_model_run__five_model_new (
  model_run_id TEXT PRIMARY KEY,
  model_id TEXT NOT NULL CHECK(model_id IN ('ECMWF','GFS','ICON','GEM','CMA')),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  target_local_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  run_init_utc TEXT NOT NULL,
  published_at_utc TEXT,
  received_at_utc TEXT,
  timestamps_complete INTEGER NOT NULL CHECK(timestamps_complete IN (0,1)),
  lead_bucket TEXT NOT NULL,
  maturity_key TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  raw_tmax_c REAL NOT NULL,
  settlement_definition TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  provider_role TEXT NOT NULL,
  source_class TEXT NOT NULL,
  aggregator_or_republisher INTEGER NOT NULL CHECK(aggregator_or_republisher IN (0,1)),
  open_meteo INTEGER NOT NULL CHECK(open_meteo IN (0,1)),
  source_url TEXT NOT NULL,
  raw_object_ref TEXT NOT NULL,
  asof_status TEXT NOT NULL,
  capture_modes_json TEXT NOT NULL,
  historical_replay_eligible INTEGER NOT NULL CHECK(historical_replay_eligible IN (0,1)),
  prospective_strict_asof_eligible INTEGER NOT NULL CHECK(prospective_strict_asof_eligible IN (0,1)),
  authority_scope TEXT NOT NULL CHECK(authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE')),
  source_object_sha256 TEXT NOT NULL CHECK(length(source_object_sha256)=64),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  CHECK(authority_scope<>'FORMAL_NATIVE_ELIGIBLE' OR
    (timestamps_complete=1 AND aggregator_or_republisher=0 AND published_at_utc IS NOT NULL AND received_at_utc IS NOT NULL)),
  CHECK(aggregator_or_republisher=0 OR authority_scope='NO_LIVE_REPLAY_CANDIDATE'),
  UNIQUE(model_id,station_id,target_local_date,run_init_utc,run_cycle)
);
CREATE TABLE calibration_model_station_residual__five_model_new (
  residual_id TEXT PRIMARY KEY,
  truth_id TEXT NOT NULL REFERENCES calibration_station_daily_high_truth(truth_id),
  model_run_id TEXT NOT NULL REFERENCES calibration_historical_asof_model_run__five_model_new(model_run_id),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  model_id TEXT NOT NULL CHECK(model_id IN ('ECMWF','GFS','ICON','GEM','CMA')),
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  maturity_key TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  e_y_minus_f_raw_c REAL NOT NULL,
  paired_asof_utc TEXT NOT NULL,
  qc_status TEXT NOT NULL CHECK(qc_status IN ('ACCEPTED','QUARANTINED')),
  historical_replay_eligible INTEGER NOT NULL CHECK(historical_replay_eligible IN (0,1)),
  prospective_strict_asof_eligible INTEGER NOT NULL CHECK(prospective_strict_asof_eligible IN (0,1)),
  authority_scope TEXT NOT NULL CHECK(authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE')),
  pair_sha256 TEXT NOT NULL CHECK(length(pair_sha256)=64),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  UNIQUE(truth_id,model_run_id)
);
CREATE TABLE calibration_model_artifact__five_model_new (
  calibration_artifact_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  model_id TEXT NOT NULL CHECK(model_id IN ('ECMWF','GFS','ICON','GEM','CMA')),
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  settlement_definition TEXT NOT NULL,
  trained_through_utc TEXT NOT NULL,
  sample_n INTEGER NOT NULL CHECK(sample_n>=0),
  effective_sample_n REAL NOT NULL CHECK(effective_sample_n>=0),
  bias_c REAL NOT NULL,
  residual_scale_c REAL NOT NULL CHECK(residual_scale_c>0),
  tail_parameters_json TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  UNIQUE(station_id,model_id,season_bin,lead_bucket,run_cycle,content_sha256)
);
"""
    )
    for table in CALIBRATION_FIVE_MODEL_TABLES:
        columns = ",".join(
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        )
        conn.execute(
            f"INSERT INTO {table}__five_model_new({columns}) "
            f"SELECT {columns} FROM {table}"
        )
    conn.execute("DROP TABLE calibration_model_station_residual")
    conn.execute("DROP TABLE calibration_model_artifact")
    conn.execute("DROP TABLE calibration_historical_asof_model_run")
    conn.execute(
        "ALTER TABLE calibration_historical_asof_model_run__five_model_new "
        "RENAME TO calibration_historical_asof_model_run"
    )
    conn.execute(
        "ALTER TABLE calibration_model_station_residual__five_model_new "
        "RENAME TO calibration_model_station_residual"
    )
    conn.execute(
        "ALTER TABLE calibration_model_artifact__five_model_new "
        "RENAME TO calibration_model_artifact"
    )
    _execute_sql_script_transactional(conn, DDL)
    after = inspect_calibration_model_pool_schema(conn)
    if after["ok"] is not True:
        raise RuntimeError("calibration_model_pool_migration_postcheck_failed")
    after_counts = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in CALIBRATION_FIVE_MODEL_TABLES
    }
    if after_counts != row_counts:
        raise RuntimeError("calibration_model_pool_migration_row_count_drift")
    return {
        "state": "migrated",
        "candidate_model_count": 5,
        "rows_preserved": after_counts,
    }


def _policy_training_fact_constraints_ok(conn: sqlite3.Connection) -> bool:
    """Return whether the canonical forward-fact constraints are present.

    ``PRAGMA table_info`` exposes the NOT NULL flag but not table-level CHECKs
    or the columns covered by a UNIQUE index.  Read all three pieces so a
    legacy table with merely additive columns cannot satisfy readiness.
    """

    table = "basket_policy_training_fact"
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if table_row is None:
        return False
    table_sql = "".join(str(table_row[0] or "").lower().split())
    if "check(length(quote_freshness_policy_sha256)=64)" not in table_sql:
        return False
    required_not_null = {
        "decision_scope_key",
        "captured_at_utc",
        "source_sequence",
        "quote_observed_at",
        "quote_expires_at",
        "quote_freshness_policy_sha256",
    }
    info = {
        str(row[1]): int(row[3] or 0)
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if any(info.get(column) != 1 for column in required_not_null):
        return False
    for index in conn.execute(f"PRAGMA index_list({table})"):
        if int(index[2] or 0) != 1:
            continue
        index_name = str(index[1])
        index_columns = [
            str(row[2])
            for row in conn.execute(f"PRAGMA index_info({index_name})")
        ]
        if index_columns == ["decision_scope_key"]:
            return True
    return False


def _upgrade_empty_calibration_base_fact_schema(conn: sqlite3.Connection) -> None:
    """Rebuild only the empty pre-release calibration base tables for v2.

    These tables are append-only and therefore cannot be altered in place.
    Any persisted row makes the upgrade fail closed; a populated deployment
    requires a separately reviewed content-preserving migration packet.
    """

    table = "calibration_historical_asof_model_run"
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if "authority_scope" in columns:
        return
    base_tables = (
        "calibration_model_station_residual",
        "calibration_historical_asof_model_run",
        "calibration_station_daily_high_truth",
        "calibration_station_registry",
    )
    populated = [
        name
        for name in base_tables
        if int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) > 0
    ]
    if populated:
        raise RuntimeError(
            "calibration_base_authority_upgrade_requires_empty_tables:" + ",".join(populated)
        )
    for name in base_tables:
        conn.execute(f"DROP TABLE {name}")
    _execute_sql_script_transactional(conn, DDL)


def _upgrade_empty_calibration_model_pool_schema(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Upgrade the append-only model CHECKs from three to five candidates.

    SQLite cannot alter CHECK constraints in place.  Empty pre-production
    tables are rebuilt deterministically; any persisted calibration/model
    artifact row fails closed and requires a separately reviewed,
    content-preserving migration packet.
    """

    constrained_tables = (
        "calibration_model_station_residual",
        "calibration_historical_asof_model_run",
        "calibration_model_artifact",
    )
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        ("calibration_historical_asof_model_run",),
    ).fetchone()
    if table_sql is None:
        return {"state": "not_installed"}
    normalized = "".join(str(table_sql[0] or "").upper().split())
    if "'GEM'" in normalized and "'CMA'" in normalized:
        return {"state": "already_current", "candidate_model_count": 5}
    populated = [
        table
        for table in constrained_tables
        if int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) > 0
    ]
    if populated:
        raise RuntimeError(
            "calibration_five_model_pool_upgrade_requires_reviewed_migration:"
            + ",".join(populated)
        )
    for table in constrained_tables:
        conn.execute(f"DROP TABLE {table}")
    _execute_sql_script_transactional(conn, DDL)
    return {"state": "rebuilt_empty_tables", "candidate_model_count": 5}


def _upgrade_empty_policy_training_fact_schema(
    conn: sqlite3.Connection,
) -> None:
    """Replace only an empty forward-fact table that lacks the v4 contract.

    The v3 table could not bind a decision to the exact probability, WS,
    inventory, order-state, policy and intent versions, and its inline
    ``outcome_json`` made post-settlement labeling incompatible with the
    append-only contract.  The forward-capture fields add the decision scope,
    capture cursor and quote-freshness evidence needed for durable replay.
    Persisted rows are therefore never relabelled, defaulted or guessed: a
    populated legacy table requires an explicit reviewed migration and fails
    closed here.
    """

    table = "basket_policy_training_fact"
    required = {
        "settlement_timezone",
        "decision_scope_key",
        "captured_at_utc",
        "source_sequence",
        "quote_observed_at",
        "quote_expires_at",
        "quote_freshness_policy_sha256",
        "probability_envelope_id",
        "posterior_version",
        "official_exclusion_mask_sha256",
        "ws_snapshot_id",
        "quote_version",
        "inventory_snapshot_id",
        "inventory_version",
        "order_state_snapshot_id",
        "order_state_version",
        "cycle_id",
        "decision_id",
        "decision_sha256",
        "plan_sha256",
        "intent_count",
        "intent_set_sha256",
        "policy_version_vector_json",
        "feature_json",
        "authority_scope",
        "created_at_utc",
    }
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if required.issubset(columns) and _policy_training_fact_constraints_ok(conn):
        return
    row_count = int(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    )
    label_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM basket_policy_training_label"
        ).fetchone()[0]
    )
    if row_count or label_count:
        raise RuntimeError(
            "policy_training_exact_version_upgrade_requires_empty_tables"
        )
    conn.execute("DROP TRIGGER IF EXISTS basket_training_fact_no_update")
    conn.execute("DROP TRIGGER IF EXISTS basket_training_fact_no_delete")
    conn.execute("DROP TRIGGER IF EXISTS basket_training_label_no_update")
    conn.execute("DROP TRIGGER IF EXISTS basket_training_label_no_delete")
    conn.execute("DROP INDEX IF EXISTS basket_policy_training_fact_scope_idx")
    conn.execute("DROP TABLE basket_policy_training_label")
    conn.execute(f"DROP TABLE {table}")
    _execute_sql_script_transactional(conn, DDL)


def _upgrade_calibration_activation_authority(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(calibration_activation)")
    }
    if "authority_scope" not in columns:
        conn.execute(
            "ALTER TABLE calibration_activation ADD COLUMN authority_scope TEXT NOT NULL "
            "DEFAULT 'FORMAL_PROMOTED' CHECK(authority_scope IN "
            "('FORMAL_PROMOTED','PRODUCTION_NO_LIVE_ONLY'))"
        )


def _upgrade_calibration_ensemble_governance(conn: sqlite3.Connection) -> None:
    """Add nullable governance columns while preserving legacy audit rows.

    A legacy ensemble remains readable but has a NULL selection evidence hash
    and therefore cannot pass the no-live activation gate.  New artifacts must
    populate every governance column and bind an immutable selection row.
    """

    columns = {
        str(row[1]) for row in conn.execute(
            "PRAGMA table_info(calibration_ensemble_artifact)"
        )
    }
    additions = {
        "selection_evidence_sha256": "TEXT",
        "previous_ensemble_artifact_sha256": "TEXT",
        "weight_method": "TEXT",
        "effective_sample_n": "REAL",
        "max_weight_delta": "REAL",
        "train_start_date": "TEXT",
        "train_end_date": "TEXT",
        "recent_start_date": "TEXT",
        "recent_end_date": "TEXT",
        "holdout_start_date": "TEXT",
        "holdout_end_date": "TEXT",
        "oos_metrics_json": "TEXT",
    }
    for name, sql_type in additions.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE calibration_ensemble_artifact ADD COLUMN {name} {sql_type}"
            )


def _upgrade_probability_envelope_stage_constraint(conn: sqlite3.Connection) -> None:
    """Upgrade an empty pre-release table to P0 → P0-M → P1.

    SQLite cannot alter a CHECK constraint in place.  The initial additive
    migration used the superseded P0-M/P0-F/P1 names. Rebuild is allowed only
    when the table is empty; populated tables require the controlled stage
    migration so evidence is never silently relabelled.
    """

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='basket_probability_envelope'"
    ).fetchone()
    sql = str(row[0] or "") if row else ""
    if (
        "'P0'" in sql
        and "'P0-M'" in sql
        and "'P1'" in sql
        and "'P0-F'" not in sql
    ):
        return
    count = int(
        conn.execute("SELECT COUNT(*) FROM basket_probability_envelope").fetchone()[0]
    )
    if count:
        raise RuntimeError("probability_stage_constraint_upgrade_requires_empty_table")
    conn.execute("DROP TRIGGER IF EXISTS basket_probability_envelope_no_update")
    conn.execute("DROP TRIGGER IF EXISTS basket_probability_envelope_no_delete")
    conn.execute("DROP INDEX IF EXISTS basket_probability_envelope_asof_idx")
    conn.execute("DROP TABLE basket_probability_envelope")
    _execute_sql_script_transactional(conn, PRODUCTION_TARGET_DDL)


def _upgrade_strict_delta_candidate_decision_constraint(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Add the pre-registered no-live candidate lifecycle state.

    This is a schema-only governance split. Existing promotion receipts are
    copied byte-for-byte; no decision is relabelled and no OOS evidence is
    synthesized. SQLite CHECK constraints require a bounded table rebuild.
    """

    table = "basket_strict_delta_policy_promotion"
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    sql = str(row[0] or "") if row else ""
    if "'CANDIDATE'" in sql:
        return {
            "schema": "strict_delta_candidate_decision_upgrade_v1",
            "state": "already_ready",
            "rows_preserved": int(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            ),
        }
    legacy = table + "_pre_candidate_v1"
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (legacy,),
    ).fetchone() is not None:
        raise RuntimeError("strict_delta_candidate_legacy_table_collision")
    columns = (
        "promotion_id,artifact_id,decision_seq,decision,gate_version,"
        "decided_at_utc,receipt_sha256,content_sha256"
    )
    rows_preserved = int(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    )
    conn.execute(
        "SAVEPOINT strict_delta_candidate_decision_upgrade"
    )
    try:
        conn.execute(
            "DROP TRIGGER IF EXISTS basket_strict_delta_promotion_no_update"
        )
        conn.execute(
            "DROP TRIGGER IF EXISTS basket_strict_delta_promotion_no_delete"
        )
        conn.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
        _execute_sql_script_transactional(conn, PRODUCTION_TARGET_DDL)
        conn.execute(
            f"INSERT INTO {table}({columns}) SELECT {columns} FROM {legacy}"
        )
        copied = int(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        if copied != rows_preserved:
            raise RuntimeError(
                "strict_delta_candidate_decision_row_count_drift"
            )
        conn.execute(f"DROP TABLE {legacy}")
    except Exception:
        conn.execute(
            "ROLLBACK TO strict_delta_candidate_decision_upgrade"
        )
        conn.execute(
            "RELEASE strict_delta_candidate_decision_upgrade"
        )
        raise
    conn.execute("RELEASE strict_delta_candidate_decision_upgrade")
    return {
        "schema": "strict_delta_candidate_decision_upgrade_v1",
        "state": "migrated",
        "rows_preserved": rows_preserved,
    }


def _upgrade_quote_freshness_columns(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Retire WS snapshots whose expiry policy cannot be reconstructed."""

    table = "basket_official_ws_snapshot"
    required = {
        "quote_expires_at",
        "quote_freshness_policy_sha256",
    }
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if required.issubset(columns):
        return {
            "schema": "bayes_basket_quote_freshness_upgrade_v1",
            "state": "already_ready",
            "retired_row_count": 0,
        }
    archive = "basket_official_ws_snapshot_legacy_no_quote_expiry_v1"
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (archive,),
    ).fetchone() is not None:
        raise RuntimeError(
            "quote_freshness_legacy_archive_collision:" + archive
        )
    row_count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    conn.execute("DROP TRIGGER IF EXISTS basket_official_ws_snapshot_no_update")
    conn.execute("DROP TRIGGER IF EXISTS basket_official_ws_snapshot_no_delete")
    conn.execute("DROP INDEX IF EXISTS basket_official_ws_snapshot_asof_idx")
    if row_count:
        conn.execute(f"ALTER TABLE {table} RENAME TO {archive}")
        state = "retired_populated_legacy"
    else:
        conn.execute(f"DROP TABLE {table}")
        state = "replaced_empty_legacy"
    _execute_sql_script_transactional(conn, PRODUCTION_TARGET_DDL)
    return {
        "schema": "bayes_basket_quote_freshness_upgrade_v1",
        "state": state,
        "retired_row_count": row_count,
        "audit_table": archive if row_count else None,
    }


def _upgrade_settlement_timezone_columns(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Retire pre-timezone inputs instead of assigning a false default.

    The old tables contain no trustworthy settlement-timezone binding in
    either storage or lineage.  They cannot be upgraded row-by-row: assigning
    ET would corrupt non-ET markets, while nullable columns would leave old
    ``COMMITTED`` rows on the formal read path.  A controlled migration
    therefore preserves each populated table under a deterministic audit-only
    name and recreates the sole formal table from the current DDL.
    """

    specs = {
        "basket_probability_envelope": {
            "archive": "basket_probability_envelope_legacy_no_settlement_timezone_v1",
            "indexes": ("basket_probability_envelope_asof_idx",),
            "triggers": (
                "basket_probability_envelope_no_update",
                "basket_probability_envelope_no_delete",
            ),
        },
        "basket_official_ws_snapshot": {
            "archive": "basket_official_ws_snapshot_legacy_no_settlement_timezone_v1",
            "indexes": ("basket_official_ws_snapshot_asof_idx",),
            "triggers": (
                "basket_official_ws_snapshot_no_update",
                "basket_official_ws_snapshot_no_delete",
            ),
        },
        "basket_official_ws_evidence_cache": {
            "archive": "basket_official_ws_evidence_cache_legacy_no_settlement_timezone_v1",
            "indexes": ("basket_official_ws_evidence_cache_scope_idx",),
            "triggers": (),
        },
        "basket_target_execution_cycles": {
            "archive": "basket_target_execution_cycles_legacy_no_settlement_timezone_v1",
            "indexes": (),
            "triggers": (),
        },
        "auto_trade_decisions": {
            "archive": "auto_trade_decisions_legacy_no_settlement_timezone_v1",
            "indexes": ("basket_target_decision_blocker_idx",),
            "triggers": (
                "auto_trade_decisions_no_update",
                "auto_trade_decisions_no_delete",
            ),
        },
    }
    result: dict[str, object] = {
        "schema": "bayes_basket_target_settlement_timezone_upgrade_v1",
        "retired_tables": [],
        "dropped_empty_tables": [],
        "preserved_row_counts": {},
    }
    changed = False
    ledger_changed = False
    for table, spec in specs.items():
        columns = {
            str(row[1]) for row in conn.execute(
                f"PRAGMA table_info({table})"
            )
        }
        if "settlement_timezone" in columns:
            continue
        archive = str(spec["archive"])
        archive_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (archive,),
        ).fetchone() is not None
        if archive_exists:
            raise RuntimeError(
                f"settlement_timezone_legacy_archive_collision:{archive}"
            )
        row_count = int(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        for trigger in spec["triggers"]:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        for index in spec["indexes"]:
            conn.execute(f"DROP INDEX IF EXISTS {index}")
        if row_count:
            conn.execute(f"ALTER TABLE {table} RENAME TO {archive}")
            result["retired_tables"].append({
                "formal_table": table,
                "audit_table": archive,
                "row_count": row_count,
            })
            result["preserved_row_counts"][archive] = row_count
        else:
            conn.execute(f"DROP TABLE {table}")
            result["dropped_empty_tables"].append(table)
        changed = True
        ledger_changed = ledger_changed or table in {
            "basket_target_execution_cycles",
            "auto_trade_decisions",
        }
    if changed:
        _execute_sql_script_transactional(conn, PRODUCTION_TARGET_DDL)
    if ledger_changed:
        _execute_sql_script_transactional(conn, PRODUCTION_LEDGER_DDL)
    return result


def _upgrade_calibration_timezone_identity_columns(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Add only an unbound timezone field to legacy calibration facts.

    Old calibration truth/model rows did not persist the market settlement
    timezone.  There is no safe inferred value: station metadata, ET defaults
    and a current registry snapshot are all different facts.  The migration
    therefore adds a nullable column without a default.  New DDL makes the
    field ``NOT NULL`` and ingress rejects legacy ``NULL`` references, leaving
    old rows available solely for offline audit until an independently
    evidenced canonical rebuild exists.
    """

    specs = (
        "calibration_station_daily_high_truth",
        "calibration_historical_asof_model_run",
    )
    result: dict[str, object] = {
        "schema": "bayes_basket_target_calibration_timezone_identity_v1",
        "added_nullable_without_backfill": [],
        "legacy_rows_left_unbound": {},
    }
    for table in specs:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            continue
        columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if "settlement_timezone" not in columns:
            row_count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN settlement_timezone TEXT"
            )
            result["added_nullable_without_backfill"].append(table)
            result["legacy_rows_left_unbound"][table] = row_count
            continue
        result["legacy_rows_left_unbound"][table] = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE settlement_timezone IS NULL OR trim(settlement_timezone)=''"
            ).fetchone()[0]
        )
    return result


def _upgrade_basket_target_observation_fact_settlement_timezone(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Add the observation timezone column without fabricating legacy facts.

    A legacy observation identity has no recoverable settlement-timezone
    binding.  SQLite cannot add a required column to a populated table without
    a default, so this additive migration intentionally leaves prior rows NULL.
    The P1 bridge rejects such rows; no historical default or registry lookup
    is permitted to turn them into formal truth.
    """

    table = "basket_target_observation_facts"
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if "settlement_timezone" in columns:
        return {
            "schema": "basket_target_observation_fact_settlement_timezone_v1",
            "state": "already_present",
            "legacy_rows_left_unbound": 0,
        }
    legacy_rows = int(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    )
    conn.execute(
        f"ALTER TABLE {table} ADD COLUMN settlement_timezone TEXT"
    )
    return {
        "schema": "basket_target_observation_fact_settlement_timezone_v1",
        "state": "added_nullable_without_backfill",
        "legacy_rows_left_unbound": legacy_rows,
    }


__all__ = [
    "CANONICAL_CONTRACT_TABLES",
    "CALIBRATION_FIVE_MODEL_TABLES",
    "CALIBRATION_TABLES",
    "NATIVE_ENSEMBLE_FOUNDATION_TABLES",
    "NATIVE_ENSEMBLE_SCHEMA_TABLES",
    "apply_native_ensemble_foundation_schema",
    "native_ensemble_foundation_schema_status",
    "DDL",
    "MAKER_GRID_OOS_AUTHORITY_DDL",
    "MAKER_GRID_OOS_AUTHORITY_TABLES",
    "POLICY_TABLES",
    "PROBABILITY_EVIDENCE_DDL",
    "PROBABILITY_EVIDENCE_TABLES",
    "PRODUCTION_TARGET_DDL",
    "PRODUCTION_LEDGER_DDL",
    "PRODUCTION_TARGET_TABLES",
    "REGIME_ARTIFACT_COLUMNS",
    "REGIME_ARTIFACT_DDL",
    "REGIME_ARTIFACT_GUARD_DDL",
    "SCHEMA_VERSION",
    "apply_production_ssot_schema",
    "apply_maker_grid_oos_authority_schema",
    "apply_target_ssot_schema",
    "inspect_regime_superseded_schema",
    "inspect_calibration_model_pool_schema",
    "production_ssot_schema_status",
    "upgrade_calibration_model_pool_schema_preserving_rows",
    "upgrade_regime_superseded_schema",
]
