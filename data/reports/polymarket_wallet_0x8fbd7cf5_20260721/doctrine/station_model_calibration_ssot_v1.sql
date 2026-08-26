-- station_model_calibration_ssot_v1
-- TARGET CANDIDATE / SHADOW ONLY / NON-PRODUCTION REFERENCE DDL
-- One logical calibration SSOT. Never create one authority database per station.

PRAGMA foreign_keys = ON;

CREATE TABLE calibration_station_registry (
  station_id TEXT PRIMARY KEY,
  icao_code TEXT NOT NULL UNIQUE,
  city_label TEXT NOT NULL,
  timezone_name TEXT NOT NULL,
  source_lane TEXT NOT NULL CHECK (source_lane IN ('AMSC','AWC')),
  settlement_definition TEXT NOT NULL,
  registry_version TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64)
) STRICT;

CREATE TABLE calibration_station_daily_high_truth (
  truth_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  local_date TEXT NOT NULL,
  official_tmax_c REAL NOT NULL,
  source_lane TEXT NOT NULL CHECK (source_lane IN ('AMSC','AWC')),
  settlement_definition TEXT NOT NULL,
  revision_no INTEGER NOT NULL CHECK (revision_no>=0),
  published_at_utc TEXT NOT NULL,
  received_at_utc TEXT NOT NULL,
  qc_status TEXT NOT NULL CHECK (qc_status IN ('ACCEPTED','QUARANTINED')),
  source_payload_sha256 TEXT NOT NULL CHECK (length(source_payload_sha256)=64),
  content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
  UNIQUE (station_id,local_date,settlement_definition,revision_no)
) STRICT;

CREATE TABLE calibration_historical_asof_model_run (
  model_run_id TEXT PRIMARY KEY,
  model_id TEXT NOT NULL CHECK (model_id IN ('ECMWF','GFS','ICON')),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  target_local_date TEXT NOT NULL,
  run_init_utc TEXT NOT NULL,
  published_at_utc TEXT NOT NULL,
  received_at_utc TEXT NOT NULL,
  lead_hours INTEGER NOT NULL CHECK (lead_hours>=0),
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  member_id TEXT NOT NULL DEFAULT 'CONTROL',
  raw_tmax_c REAL NOT NULL,
  interpolation_method TEXT NOT NULL,
  extraction_version TEXT NOT NULL,
  extraction_sha256 TEXT NOT NULL CHECK (length(extraction_sha256)=64),
  source_object_sha256 TEXT NOT NULL CHECK (length(source_object_sha256)=64),
  qc_status TEXT NOT NULL CHECK (qc_status IN ('ACCEPTED','QUARANTINED')),
  content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
  UNIQUE (model_id,station_id,target_local_date,run_init_utc,member_id,extraction_version)
) STRICT;

CREATE INDEX calibration_model_run_lookup_idx
  ON calibration_historical_asof_model_run
  (station_id,model_id,target_local_date,run_cycle,lead_bucket,published_at_utc,received_at_utc);

CREATE TABLE calibration_model_station_residual (
  residual_id TEXT PRIMARY KEY,
  truth_id TEXT NOT NULL REFERENCES calibration_station_daily_high_truth(truth_id),
  model_run_id TEXT NOT NULL REFERENCES calibration_historical_asof_model_run(model_run_id),
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  model_id TEXT NOT NULL CHECK (model_id IN ('ECMWF','GFS','ICON')),
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  e_y_minus_f_raw_c REAL NOT NULL,
  paired_asof_utc TEXT NOT NULL,
  qc_status TEXT NOT NULL CHECK (qc_status IN ('ACCEPTED','QUARANTINED')),
  pair_sha256 TEXT NOT NULL CHECK (length(pair_sha256)=64),
  UNIQUE (truth_id,model_run_id)
) STRICT;

CREATE INDEX calibration_residual_training_idx
  ON calibration_model_station_residual
  (station_id,model_id,season_bin,lead_bucket,run_cycle,qc_status,paired_asof_utc);

CREATE TABLE calibration_model_artifact (
  calibration_artifact_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  model_id TEXT NOT NULL CHECK (model_id IN ('ECMWF','GFS','ICON')),
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  settlement_definition TEXT NOT NULL,
  train_start_date TEXT NOT NULL,
  train_end_date TEXT NOT NULL,
  trained_through_utc TEXT NOT NULL,
  sample_n INTEGER NOT NULL CHECK (sample_n>=0),
  effective_sample_n REAL NOT NULL CHECK (effective_sample_n>=0),
  bias_c REAL NOT NULL,
  residual_scale_c REAL NOT NULL CHECK (residual_scale_c>0),
  tail_parameters_json TEXT NOT NULL,
  shrinkage_parent_key TEXT,
  fit_config_sha256 TEXT NOT NULL CHECK (length(fit_config_sha256)=64),
  training_lineage_root_sha256 TEXT NOT NULL CHECK (length(training_lineage_root_sha256)=64),
  oos_metrics_json TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
  CHECK (valid_from_utc<valid_until_utc),
  CHECK (train_start_date<=train_end_date)
) STRICT;

CREATE INDEX calibration_model_artifact_scope_idx
  ON calibration_model_artifact
  (station_id,model_id,season_bin,lead_bucket,run_cycle,settlement_definition,valid_from_utc,valid_until_utc);

CREATE TABLE calibration_ensemble_selection_evidence (
  selection_evidence_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
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
  effective_sample_n REAL NOT NULL CHECK (effective_sample_n>0),
  previous_ensemble_artifact_sha256 TEXT CHECK (previous_ensemble_artifact_sha256 IS NULL OR length(previous_ensemble_artifact_sha256)=64),
  max_weight_delta REAL NOT NULL CHECK (max_weight_delta>=0),
  actual_max_weight_delta REAL NOT NULL CHECK (actual_max_weight_delta>=0 AND actual_max_weight_delta<=max_weight_delta),
  content_sha256 TEXT NOT NULL UNIQUE CHECK (length(content_sha256)=64),
  CHECK (train_start_date<=recent_start_date),
  CHECK (recent_start_date<=recent_end_date),
  CHECK (recent_end_date<=train_end_date),
  CHECK (train_end_date<holdout_start_date),
  CHECK (holdout_start_date<=holdout_end_date)
) STRICT;

CREATE TABLE calibration_ensemble_artifact (
  ensemble_artifact_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  settlement_definition TEXT NOT NULL,
  candidate_models_json TEXT NOT NULL,
  supported_masks_json TEXT NOT NULL,
  residual_covariance_json TEXT NOT NULL,
  correlation_penalty REAL NOT NULL CHECK (correlation_penalty>=0),
  weight_shrinkage REAL NOT NULL CHECK (weight_shrinkage>=0),
  selection_evidence_sha256 TEXT NOT NULL REFERENCES calibration_ensemble_selection_evidence(content_sha256),
  previous_ensemble_artifact_sha256 TEXT CHECK (previous_ensemble_artifact_sha256 IS NULL OR length(previous_ensemble_artifact_sha256)=64),
  weight_method TEXT NOT NULL,
  effective_sample_n REAL NOT NULL CHECK (effective_sample_n>0),
  max_weight_delta REAL NOT NULL CHECK (max_weight_delta>=0),
  train_start_date TEXT NOT NULL,
  train_end_date TEXT NOT NULL,
  recent_start_date TEXT NOT NULL,
  recent_end_date TEXT NOT NULL,
  holdout_start_date TEXT NOT NULL,
  holdout_end_date TEXT NOT NULL,
  oos_metrics_json TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
  CHECK (valid_from_utc<valid_until_utc),
  CHECK (train_start_date<=train_end_date),
  CHECK (train_start_date<=recent_start_date),
  CHECK (recent_start_date<=recent_end_date),
  CHECK (recent_end_date<=train_end_date),
  CHECK (holdout_start_date<=holdout_end_date),
  CHECK (train_end_date<holdout_start_date)
) STRICT;

CREATE INDEX calibration_ensemble_artifact_scope_idx
  ON calibration_ensemble_artifact
  (station_id,season_bin,lead_bucket,run_cycle,settlement_definition,valid_from_utc,valid_until_utc);

CREATE TABLE calibration_artifact_promotion (
  promotion_decision_id TEXT PRIMARY KEY,
  artifact_kind TEXT NOT NULL CHECK (artifact_kind IN ('MODEL_CALIBRATION','MODEL_ENSEMBLE')),
  artifact_id TEXT NOT NULL,
  decision_seq INTEGER NOT NULL CHECK (decision_seq>=1),
  decision TEXT NOT NULL CHECK (decision IN ('PROMOTED','REJECTED','SUPERSEDED')),
  gate_version TEXT NOT NULL,
  metrics_sha256 TEXT NOT NULL CHECK (length(metrics_sha256)=64),
  decided_at_utc TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL CHECK (length(receipt_sha256)=64),
  content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
  UNIQUE (artifact_kind,artifact_id,gate_version,decision_seq)
) STRICT;

CREATE INDEX calibration_promotion_lookup_idx
  ON calibration_artifact_promotion
  (artifact_kind,artifact_id,decision_seq,decided_at_utc);

CREATE TABLE calibration_activation (
  activation_id TEXT PRIMARY KEY,
  artifact_kind TEXT NOT NULL CHECK (artifact_kind IN ('MODEL_CALIBRATION','MODEL_ENSEMBLE')),
  artifact_id TEXT NOT NULL,
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  model_id_or_star TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  settlement_definition TEXT NOT NULL,
  available_model_set_hash_or_star TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK (generation>=1),
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  activated_at_utc TEXT NOT NULL,
  supersedes_activation_id TEXT REFERENCES calibration_activation(activation_id),
  content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
  CHECK (valid_from_utc<valid_until_utc),
  CHECK (
    (artifact_kind='MODEL_CALIBRATION' AND model_id_or_star IN ('ECMWF','GFS','ICON') AND available_model_set_hash_or_star='*')
    OR
    (artifact_kind='MODEL_ENSEMBLE' AND model_id_or_star='*' AND available_model_set_hash_or_star<>'*')
  ),
  UNIQUE (
    artifact_kind,station_id,model_id_or_star,season_bin,lead_bucket,run_cycle,
    settlement_definition,available_model_set_hash_or_star,generation
  )
) STRICT;

CREATE INDEX calibration_activation_asof_idx
  ON calibration_activation
  (artifact_kind,station_id,model_id_or_star,season_bin,lead_bucket,run_cycle,
   settlement_definition,available_model_set_hash_or_star,valid_from_utc,valid_until_utc,generation);

-- Append-only enforcement. Revisions are new rows and new content hashes.
CREATE TRIGGER calibration_station_registry_no_update BEFORE UPDATE ON calibration_station_registry BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_station_registry_no_delete BEFORE DELETE ON calibration_station_registry BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_truth_no_update BEFORE UPDATE ON calibration_station_daily_high_truth BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_truth_no_delete BEFORE DELETE ON calibration_station_daily_high_truth BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_model_run_no_update BEFORE UPDATE ON calibration_historical_asof_model_run BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_model_run_no_delete BEFORE DELETE ON calibration_historical_asof_model_run BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_residual_no_update BEFORE UPDATE ON calibration_model_station_residual BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_residual_no_delete BEFORE DELETE ON calibration_model_station_residual BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_model_artifact_no_update BEFORE UPDATE ON calibration_model_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_model_artifact_no_delete BEFORE DELETE ON calibration_model_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_ensemble_artifact_no_update BEFORE UPDATE ON calibration_ensemble_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_ensemble_artifact_no_delete BEFORE DELETE ON calibration_ensemble_artifact BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_promotion_no_update BEFORE UPDATE ON calibration_artifact_promotion BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_promotion_no_delete BEFORE DELETE ON calibration_artifact_promotion BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_activation_no_update BEFORE UPDATE ON calibration_activation BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_activation_no_delete BEFORE DELETE ON calibration_activation BEGIN SELECT RAISE(ABORT,'append_only'); END;

-- Runtime lookup is always parameterized by decision_at_utc. Never SELECT "latest".
-- Calibration scope:
--   station_id + model_id + season_bin + lead_bucket + run_cycle
--   + settlement_definition + decision_at_utc
-- Ensemble scope:
--   station_id + season_bin + lead_bucket + run_cycle + settlement_definition
--   + available_model_set_hash + decision_at_utc
