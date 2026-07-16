import pytest

from lineage_identity_runtime import (
    LINEAGE_ID_PREFIXES,
    LineageIds,
    build_fact_snapshot_id,
    build_posterior_run_id,
    build_posterior_checkpoint_id,
    build_realization_state_id,
    build_canonical_observation_event_id,
    build_source_event_id,
    build_source_observation_id,
    build_strong_observation_update_id,
    build_training_sample_id,
    canonical_observation_key_from_parts,
    lineage_id_from_parts,
    lineage_ids_from_mapping,
    validate_lineage_id,
)


def test_lineage_id_from_parts_is_stable_and_prefixed():
    first = lineage_id_from_parts("correlation_id", {"slug": "x", "event_date": "2026-07-03"})
    second = lineage_id_from_parts("correlation_id", {"event_date": "2026-07-03", "slug": "x"})

    assert first == second
    assert first.startswith(f"{LINEAGE_ID_PREFIXES['correlation_id']}_")
    assert validate_lineage_id("correlation_id", first)


def test_lineage_id_rejects_wrong_kind_or_prefix():
    value = lineage_id_from_parts("signal_id", "a")

    assert validate_lineage_id("signal_id", value)
    assert not validate_lineage_id("decision_id", value)
    with pytest.raises(ValueError):
        lineage_id_from_parts("not_a_kind", "x")


def test_lineage_ids_report_missing_and_invalid_fields():
    ids = lineage_ids_from_mapping(
        {
            "correlation_id": lineage_id_from_parts("correlation_id", "chain"),
            "signal_id": "bad",
        }
    )

    assert ids.missing(["correlation_id", "source_event_id"]) == ["source_event_id"]
    assert ids.invalid() == ["signal_id"]


def test_builders_capture_source_and_fact_natural_keys():
    src_a = build_source_event_id(
        source_id="zuuu_metar_raw_awc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T09:00:00Z",
        fetched_at="2026-07-03T09:05:30Z",
        raw_sha256="abc",
    )
    src_b = build_source_event_id(
        source_id="zuuu_metar_raw_awc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T10:00:00Z",
        fetched_at="2026-07-03T10:05:30Z",
        raw_sha256="abc",
    )
    fact = build_fact_snapshot_id(
        slug="highest-temperature-in-chengdu-on-july-3",
        event_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:31Z",
        source_event_ids=(src_a,),
    )

    assert src_a != src_b
    assert validate_lineage_id("source_event_id", src_a)
    assert validate_lineage_id("fact_snapshot_id", fact)
    assert LineageIds(source_event_id=src_a, fact_snapshot_id=fact).invalid() == []


def test_build_source_observation_id_is_stable_for_normalized_observation():
    first = build_source_observation_id(
        source_id="awc_cache_metars_csv_gz",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T09:00:00Z",
        fetched_at="2026-07-03T09:05:30Z",
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref="https://aviationweather.gov/data/cache/metars.cache.csv.gz",
        raw_sha256="abc",
    )
    second = build_source_observation_id(
        source_id="awc_cache_metars_csv_gz",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T09:00:00Z",
        fetched_at="2026-07-03T09:06:10Z",
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref="https://aviationweather.gov/data/cache/metars.cache.csv.gz",
        raw_sha256="abc",
    )
    changed_value = build_source_observation_id(
        source_id="awc_cache_metars_csv_gz",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T09:00:00Z",
        temperature_c=31.0,
        dewpoint_c=22.0,
        raw_ref="https://aviationweather.gov/data/cache/metars.cache.csv.gz",
        raw_sha256="abc",
    )

    assert first == second
    assert first != changed_value
    assert first.startswith(f"{LINEAGE_ID_PREFIXES['source_observation_id']}_")
    assert validate_lineage_id("source_observation_id", first)


def test_build_posterior_run_id_captures_fact_and_distribution_context():
    first = build_posterior_run_id(
        slug="highest-temperature-in-chengdu-on-july-3",
        event_date="2026-07-03",
        fact_snapshot_id="fact_1234567890abcdef",
        source_event_ids=("srcevt_a", "srcevt_b"),
        posterior_hash="abc",
        phase="D0_midday",
    )
    second = build_posterior_run_id(
        slug="highest-temperature-in-chengdu-on-july-3",
        event_date="2026-07-03",
        fact_snapshot_id="fact_1234567890abcdef",
        source_event_ids=("srcevt_a", "srcevt_b"),
        posterior_hash="abc",
        phase="D0_midday",
    )
    changed = build_posterior_run_id(
        slug="highest-temperature-in-chengdu-on-july-3",
        event_date="2026-07-03",
        fact_snapshot_id="fact_1234567890abcdef",
        source_event_ids=("srcevt_a", "srcevt_b"),
        posterior_hash="changed",
        phase="D0_midday",
    )

    assert first == second
    assert first != changed
    assert validate_lineage_id("posterior_run_id", first)


def test_sequential_bayes_lineage_ids_are_stable_and_separate_by_contract_identity():
    canonical_key = canonical_observation_key_from_parts(
        target_identity="highest-temperature-in-chengdu-on-july-3-2026",
        station_id="ZUUU",
        report_type="METAR",
        valid_time="2026-07-03T06:30:00Z",
    )
    canonical_event = build_canonical_observation_event_id(
        canonical_observation_key=canonical_key,
        revision_no=1,
        normalized_physical_sha256="physical-abc",
    )
    update = build_strong_observation_update_id(
        previous_posterior_run_id="post_1234567890abcdef",
        previous_posterior_sha256="posterior-before",
        canonical_observation_event_id=canonical_event,
        forecast_evidence_snapshot_id="forecast-1",
        model_artifact_sha256="artifact-1",
    )
    checkpoint = build_posterior_checkpoint_id(
        target_identity="highest-temperature-in-chengdu-on-july-3-2026",
        posterior_run_id="post_abcdef1234567890",
        replay_generation=2,
        joint_posterior_sha256="joint-2",
    )
    realization = build_realization_state_id(
        posterior_run_id="post_abcdef1234567890",
        fact_snapshot_id="fact_abcdef1234567890",
        state_sha256="state-2",
    )
    sample = build_training_sample_id(
        target_identity="highest-temperature-in-chengdu-on-july-3-2026",
        canonical_observation_event_id=canonical_event,
        cutoff_as_of="2026-07-03T06:31:00Z",
        artifact_sha256="artifact-1",
    )

    assert canonical_key.startswith("canokey_")
    for kind, value in (
        ("canonical_observation_event_id", canonical_event),
        ("strong_observation_update_id", update),
        ("posterior_checkpoint_id", checkpoint),
        ("realization_state_id", realization),
        ("training_sample_id", sample),
    ):
        assert validate_lineage_id(kind, value)
    assert update != build_strong_observation_update_id(
        previous_posterior_run_id="post_1234567890abcdef",
        previous_posterior_sha256="posterior-before",
        canonical_observation_event_id=canonical_event,
        forecast_evidence_snapshot_id="forecast-1",
        model_artifact_sha256="artifact-2",
    )
