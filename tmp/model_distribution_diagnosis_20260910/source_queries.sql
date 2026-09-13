SELECT ensemble_system_id,target_local_date,archive_lead_bucket,run_cycle,archive_query_at_utc,member_id,raw_tmax_c,content_sha256,transport_provider_id,native_run_init_utc,offline_oos_eligible,extraction_version FROM calibration_native_ensemble_historical_member_run WHERE station_id=? AND qc_status='ACCEPTED' AND authority_scope='CALIBRATION_TRAIN_ONLY' AND holdout_eligible=0 AND transport_provider_id IN ('AWC_METAR_PHYSICAL_PATH','AWC_TAF_DATA_API','DWD_OPENDATA_ICON_EPS','DWD_OPENDATA_ICON_EU','ECCC_DD_WEATHER','ECMWF_OPEN_DATA_S3','NOAA_GEFS_PDS_S3','NOAA_HRRR_PDS_S3','NOAA_NBM_PDS_S3','RISH_KYOTO_JMA_ARCHIVE') ORDER BY target_local_date,archive_query_at_utc,member_id;

select distinct station_id from calibration_station_market_timezone_binding;

select json_extract(lineage_json,'$.lead_bucket'),max(decision_at_utc) from basket_probability_envelope where station_id=? and event_date=? and stage='P0' and state='COMMITTED' and decision_at_utc<='2026-09-09T16:36:32Z' group by json_extract(lineage_json,'$.lead_bucket');

select envelope_id,probabilities_json,bucket_ordinals_json,lineage_json from basket_probability_envelope where station_id=? and event_date=? and decision_at_utc=? and stage='P0' and state='COMMITTED' order by rowid desc limit 1;

select ensemble_system_id,station_id,season_bin,lead_bucket,run_cycle,bias_parameters_json,spread_parameters_json,reliability_parameters_json,train_start_date,train_end_date,holdout_end_date from calibration_native_ensemble_system_artifact where content_sha256=?;