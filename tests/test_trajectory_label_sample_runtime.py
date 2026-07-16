from trajectory_label_sample_runtime import build_trajectory_label_sample, build_trajectory_label_sample_manifest


def _row(valid_time: str, temperature_c: float, *, first_seen_at: str | None = None, suffix: str = ""):
    hour_minute = valid_time[11:16].replace(":", "")
    day = valid_time[8:10]
    return {
        "source_id": "zuuu_metar_raw_amsc",
        "source_class": "hard_observation_candidate",
        "station_id": "ZUUU",
        "temperature_c": temperature_c,
        "valid_time": valid_time,
        "fetched_at": first_seen_at or valid_time,
        "parsed_at": first_seen_at or valid_time,
        "raw_sha256": f"sha-{hour_minute}{suffix}",
        "raw_object_ref": f"objects/aa/sha-{hour_minute}{suffix}.json.gz",
        "source_observation_id": f"srcobs-{hour_minute}{suffix}",
        "parser_version": "amsc_zuuu_metar_taf_fetcher_v1",
        "payload": {
            "raw_text": f"METAR ZUUU {day}{hour_minute}Z 00000MPS CAVOK {int(temperature_c)}/20 Q1000=",
            "first_seen_at": first_seen_at or valid_time,
            "arrival_time_quality": "observed_first_seen",
        },
    }


def _coverage(event_date: str = "2026-07-05", *, eligible: bool = True):
    return {
        "event_date": event_date,
        "station_id": "ZUUU",
        "coverage_sha256": "coverage-hash",
        "replay_eligibility": {"a_class_as_of_replay_eligible": eligible},
    }


def test_formal_label_sample_keeps_label_outside_features_and_is_a_class_only_with_complete_arrival_lineage():
    sample = build_trajectory_label_sample(
        station_id="ZUUU",
        event_date="2026-07-05",
        cutoff_as_of="2026-07-05T03:05:00Z",
        source_observations=[
            _row("2026-07-05T02:00:00Z", 34, first_seen_at="2026-07-05T02:01:00Z"),
            _row("2026-07-05T02:30:00Z", 35, first_seen_at="2026-07-05T02:31:00Z"),
            _row("2026-07-05T03:00:00Z", 36, first_seen_at="2026-07-05T03:01:00Z"),
            _row("2026-07-05T03:30:00Z", 37, first_seen_at="2026-07-05T03:31:00Z"),
        ],
        label={
            "label_kind": "formal_settlement",
            "settlement_actual": 38.0,
            "resolution_source": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
            "event_run_id": 99,
            "label_version": "event_runs_market_closed_v1",
        },
        coverage_manifest=_coverage(),
        fact_snapshot_ids=["fact-1"],
    )

    assert sample["quality_grade"] == "A"
    assert sample["evaluation_eligible"] is True
    assert sample["observed_today_max_c_at_cutoff"] == 36.0
    assert sample["label"]["remaining_increment_t"] == 2.0
    assert len(sample["canonical_metar_sequence"]) == 3
    assert all("settlement_actual" not in point for point in sample["canonical_metar_sequence"])
    assert sample["guards"]["final_label_used_as_feature"] is False


def test_late_or_untrusted_coverage_is_b_class_and_never_evaluation_eligible():
    sample = build_trajectory_label_sample(
        station_id="ZUUU",
        event_date="2026-07-05",
        cutoff_as_of="2026-07-05T03:05:00Z",
        source_observations=[_row("2026-07-05T03:00:00Z", 36, first_seen_at="2026-07-05T03:01:00Z")],
        label={"label_kind": "formal_settlement", "settlement_actual": 38.0},
        coverage_manifest=_coverage(eligible=False),
    )

    assert sample["quality_grade"] == "B"
    assert sample["evaluation_eligible"] is False
    assert sample["shadow_diagnostic_eligible"] is True


def test_unpersisted_live_coverage_summary_is_not_sufficient_for_a_class_replay():
    coverage = _coverage(eligible=True)
    coverage["coverage_snapshot_persisted"] = False
    sample = build_trajectory_label_sample(
        station_id="ZUUU",
        event_date="2026-07-05",
        cutoff_as_of="2026-07-05T03:05:00Z",
        source_observations=[_row("2026-07-05T03:00:00Z", 36, first_seen_at="2026-07-05T03:01:00Z")],
        label={"label_kind": "formal_settlement", "settlement_actual": 38.0},
        coverage_manifest=coverage,
    )

    assert sample["quality_grade"] == "B"
    assert sample["coverage_manifest_persisted"] is False
    assert "daily_coverage_manifest_not_persisted" in sample["issues"]


def test_diagnostic_label_and_manifest_are_deterministic_and_not_evaluation_samples():
    sample = build_trajectory_label_sample(
        station_id="ZUUU",
        event_date="2026-07-05",
        cutoff_as_of="2026-07-05T03:05:00Z",
        source_observations=[_row("2026-07-05T03:00:00Z", 36, first_seen_at="2026-07-05T03:01:00Z")],
        label={"label_kind": "amsc_self_max_diagnostic", "final_daily_max_c": 37.0},
        coverage_manifest=_coverage(),
    )
    first = build_trajectory_label_sample_manifest(
        requested_event_dates=["2026-07-05"],
        samples=[sample],
        excluded=[{"event_date": "2026-07-04", "reason": "raw_archive_missing"}],
    )
    second = build_trajectory_label_sample_manifest(
        requested_event_dates=["2026-07-05"],
        samples=[sample],
        excluded=[{"event_date": "2026-07-04", "reason": "raw_archive_missing"}],
    )

    assert sample["quality_grade"] == "C"
    assert sample["evaluation_eligible"] is False
    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert first["evaluation_sample_count"] == 0
    assert first["guards"]["writes_facts_sqlite"] is False
