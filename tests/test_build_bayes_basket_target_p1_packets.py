from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target.contracts import ProbabilityEnvelope, content_sha256
from scripts.build_bayes_basket_target_p1_packets import (
    _reported_hard_lower_in_unit,
    build_packets,
    canonical_materialization_packet,
    compile_station_p1_packets,
    main,
    materialization_packet,
    select_p1_replay_base,
)
from scripts.materialize_bayes_basket_target_probability import build_envelope


def _p0_root() -> ProbabilityEnvelope:
    return ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "p0",
        "station_id": "KATL",
        "event_date": "2026-07-26",
        "settlement_timezone": "America/New_York",
        "decision_at_utc": "2026-07-26T04:10:00Z",
        "fresh_until_utc": "2026-07-26T06:00:00Z",
        "bucket_ordinals": {"75°F or below": 0, "76-77°F": 1, "78°F or higher": 2},
        "probabilities": {"75°F or below": 0.2, "76-77°F": 0.5, "78°F or higher": 0.3},
        "posterior_version": "p0",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
        "lineage": {
            "model_run_ids": "r",
            "settlement_timezone": "America/New_York",
            "season_bin": "JJA",
            "settlement_definition": "station_local_calendar_day_tmax",
        },
    })


def _predecessor(
    p0_root: ProbabilityEnvelope | None = None,
    *,
    checkpoint_id: str | None = None,
    decision_at_utc: str = "2026-07-26T04:10:00Z",
) -> ProbabilityEnvelope:
    root = p0_root or _p0_root()
    return ProbabilityEnvelope.from_mapping({
        **root.to_dict(),
        "stage": "P0-M",
        "checkpoint_id": checkpoint_id or f"{root.checkpoint_id}-p0m",
        "decision_at_utc": decision_at_utc,
        "posterior_version": "p0m",
        "lineage": {
            **root.lineage,
            "p0_root_checkpoint_id": root.checkpoint_id,
            "p0_root_content_sha256": root.content_sha256,
        },
    })


def _existing_p1() -> ProbabilityEnvelope:
    p0m = _predecessor()
    return ProbabilityEnvelope.from_mapping({
        **p0m.to_dict(),
        "stage": "P1",
        "checkpoint_id": "p1-existing",
        "decision_at_utc": "2026-07-26T07:00:00Z",
        "fresh_until_utc": "2026-07-26T09:00:00Z",
        "lineage": {
            **p0m.lineage,
            "prediction_root_stage": "P0-M",
            "prediction_root_checkpoint_id": p0m.checkpoint_id,
            "prediction_root_content_sha256": p0m.content_sha256,
            "canonical_observation_event_id": "o1",
        },
    })


def _registry():
    return [
        {
            "station_id": "KATL",
            "event_date": "2026-07-26",
            "settlement_timezone": "America/New_York",
            "settlement_unit": "F",
            "outcome": outcome,
            "registry_json": json.dumps({"upper": upper}),
        }
        for outcome, upper in (
            ("75°F or below", 75.0),
            ("76-77°F", 77.0),
            ("78°F or higher", None),
        )
    ]


def _canonical_evidence(observations):
    payload = {
        "schema": "bayes_basket_target_canonical_observation_evidence_v1",
        "observations": observations,
    }
    return {
        **payload,
        "receipt_sha256": content_sha256(payload),
    }


def test_p1_registry_timezone_must_match_probability_scope() -> None:
    rows = [
        {
            **row,
            "settlement_timezone": "UTC",
        }
        for row in _registry()
    ]
    with pytest.raises(
        ContractError,
        match="p1_registry_settlement_timezone_mismatch",
    ):
        compile_station_p1_packets(
            predecessor=_predecessor(),
            observations=[
                _observation("o1", 24.0, "2026-07-26T04:00:00Z")
            ],
            registry_rows=rows,
            compilation_at_utc="2026-07-26T08:00:00Z",
        )


def _observation(observation_id: str, temperature_c: float, valid_at: str):
    return {
        "observation_id": observation_id,
        "station_id": "KATL",
        "event_date_local": "2026-07-26",
        "settlement_timezone": "America/New_York",
        "reported_temperature_c": temperature_c,
        "valid_at_utc": valid_at,
        "canonical_committed_at_utc": valid_at,
        "raw_sha256": ("1" if observation_id == "o1" else "2") * 64,
        "source_lane": "AWC",
        "report_type": "METAR",
        "evidence_role": "FORMAL",
        "qc_status": "ACCEPTED",
        "correction_suspected": False,
    }


def test_only_new_high_updates_p1_while_full_prefix_drives_likelihood() -> None:
    """Only a new high *advances*; the tail refresh restates the belief.

    The hard bound still moves only where the day's high moves -- that is
    what ``reported_high_advanced`` names, and the runtime reads it to decide
    whether a trading window may open.  What the walk no longer does is treat
    that same rule as permission for the *forecast* to change: the last step
    re-runs the whole chain on the current clock over the complete prefix,
    flagged as not advancing.
    """

    packets = compile_station_p1_packets(
        predecessor=_predecessor(),
        observations=[
            _observation("o1", 24.0, "2026-07-26T04:00:00Z"),
            _observation("equal", 24.0, "2026-07-26T05:00:00Z"),
            _observation("older", 23.0, "2026-07-26T06:00:00Z"),
            _observation("o2", 26.0, "2026-07-26T07:00:00Z"),
        ],
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T08:00:00Z",
    )

    advancing = [
        packet for packet in packets if packet["reported_high_advanced"]
    ]
    assert [packet["observation_id"] for packet in advancing] == ["o1", "o2"]
    assert [packet["observation_id"] for packet in packets] == [
        "o1", "o2", "o2"
    ]
    assert [
        packet["reported_high_advanced"] for packet in packets
    ] == [True, True, False]
    # The refresh answers "what do we believe now", so its clock is the
    # compile instant rather than the closing report's minute, and it names
    # the head it recomputes.
    assert packets[-1]["decision_at_utc"].startswith("2026-07-26T08:00:00")
    assert packets[-1]["state_refresh_of_checkpoint_id"] == (
        packets[-2]["expected_envelope"]["checkpoint_id"]
    )
    assert packets[-1]["expected_envelope"]["lineage"][
        "p1_state_refresh_of_checkpoint_id"
    ] == packets[-2]["expected_envelope"]["checkpoint_id"]
    # The bound the day's own reports put under the ladder does not move on a
    # refresh: same running high, same excluded buckets.
    assert (
        packets[-1]["hard_excluded_buckets"]
        == packets[-2]["hard_excluded_buckets"]
    )
    packets = advancing
    assert packets[-1]["trajectory_proof"]["canonical_observation_ids"] == [
        "o1", "equal", "older", "o2"
    ]
    assert packets[0]["hard_excluded_buckets"] == []
    assert packets[-1]["hard_excluded_buckets"] == [
        "75°F or below", "76-77°F"
    ]
    final = packets[-1]["expected_envelope"]
    assert final["probabilities"]["75°F or below"] == 0.0
    assert final["probabilities"]["76-77°F"] == 0.0
    assert final["probabilities"]["78°F or higher"] == 1.0
    assert final["lineage"]["observation_likelihood_mode"] == (
        "HARD_EXCLUSION_PLUS_CLOCK_CEILING_SOFT_LR_SUSPENDED_V6"
    )
    assert final["lineage"]["reported_high_bucket"] == "78°F or higher"
    assert final["lineage"]["reported_high_ordinal"] == "2"
    assert final["lineage"]["reported_high_c"] == "26"


def test_p1_event_clock_uses_canonical_commit_not_later_replay_clock() -> None:
    packets = compile_station_p1_packets(
        predecessor=_predecessor(),
        observations=[
            _observation("o1", 24.0, "2026-07-26T04:00:00Z"),
            {
                **_observation("o2", 26.0, "2026-07-26T05:00:00Z"),
                "canonical_committed_at_utc": "2026-07-26T06:00:00Z",
            },
        ],
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T08:00:00Z",
    )

    assert packets[0]["decision_at_utc"] == "2026-07-26T04:10:00.000001Z"
    assert packets[1]["decision_at_utc"] == "2026-07-26T06:00:00.000000Z"


def test_future_canonical_commit_is_rejected_not_silently_skipped() -> None:
    observation = {
        **_observation("future", 24.0, "2026-07-26T04:00:00Z"),
        "canonical_committed_at_utc": "2026-07-26T08:00:01Z",
    }
    with pytest.raises(
        ContractError,
        match="p1_canonical_commit_not_strict_asof",
    ):
        compile_station_p1_packets(
            predecessor=_predecessor(),
            observations=[observation],
            registry_rows=_registry(),
            compilation_at_utc="2026-07-26T08:00:00Z",
        )


def test_canonical_valid_time_cannot_follow_its_commit() -> None:
    """A report may lead its own commit by a transmission lead, no more.

    The local-midnight report commits ahead of its nominal minute on some
    stations (OEJN -6.4s, SBGR ~-30s, measured 2026-08-30), so a lead within
    ``MAX_REPORT_TRANSMISSION_LEAD_SECONDS`` is a real report and compiles;
    a valid time further ahead of its commit than any transmission lead is
    time reversal and is refused.
    """

    from bayes_basket_target.contracts import (
        MAX_REPORT_TRANSMISSION_LEAD_SECONDS,
    )

    within_lead = {
        **_observation("early-commit", 24.0, "2026-07-26T07:00:00Z"),
        "canonical_committed_at_utc": "2026-07-26T06:59:59Z",
    }
    packets = compile_station_p1_packets(
        predecessor=_predecessor(),
        observations=[within_lead],
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T08:00:00Z",
    )
    # The tail state refresh closes on the same report; the property under
    # test is which reports may be admitted at all.
    assert {packet["observation_id"] for packet in packets} == {
        "early-commit"
    }

    reversed_time = {
        **_observation("time-reversed", 24.0, "2026-07-26T07:00:00Z"),
        "canonical_committed_at_utc": (
            datetime.fromisoformat("2026-07-26T07:00:00+00:00")
            - timedelta(seconds=MAX_REPORT_TRANSMISSION_LEAD_SECONDS + 1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with pytest.raises(
        ContractError,
        match="p1_canonical_observation_time_order_invalid",
    ):
        compile_station_p1_packets(
            predecessor=_predecessor(),
            observations=[reversed_time],
            registry_rows=_registry(),
            compilation_at_utc="2026-07-26T08:00:00Z",
        )


def test_consumed_new_high_advances_running_high_without_duplicate_packet() -> None:
    packets = compile_station_p1_packets(
        predecessor=_predecessor(),
        observations=[
            _observation("o1", 24.0, "2026-07-26T04:00:00Z"),
            _observation("o2", 25.0, "2026-07-26T05:00:00Z"),
        ],
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T08:00:00Z",
        consumed_observation_ids={"o1"},
    )
    assert [
        packet["observation_id"]
        for packet in packets
        if packet["reported_high_advanced"]
    ] == ["o2"]


def test_p1_compiler_accepts_raw_p0_root_without_taf() -> None:
    packets = compile_station_p1_packets(
        predecessor=_p0_root(),
        observations=[_observation("o1", 24.0, "2026-07-26T04:00:00Z")],
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T08:00:00Z",
    )
    assert packets[0]["expected_envelope"]["lineage"]["prediction_root_stage"] == "P0"


def test_new_dynamic_p0_root_forces_full_canonical_replay_to_p1() -> None:
    old_p0 = _p0_root()
    old_p0m = _predecessor(old_p0)
    old_p1 = ProbabilityEnvelope.from_mapping({
        **old_p0m.to_dict(),
        "stage": "P1",
        "checkpoint_id": "p1-old-root",
        "decision_at_utc": "2026-07-26T08:00:00Z",
        "fresh_until_utc": "2026-07-26T12:00:00Z",
        "lineage": {
            **old_p0m.lineage,
            "prediction_root_stage": "P0-M",
            "prediction_root_checkpoint_id": old_p0m.checkpoint_id,
            "prediction_root_content_sha256": old_p0m.content_sha256,
            "canonical_observation_event_id": "o1",
        },
    })
    new_p0 = ProbabilityEnvelope.from_mapping({
        **old_p0.to_dict(),
        "checkpoint_id": "p0-model-refresh",
        "decision_at_utc": "2026-07-26T09:00:00Z",
        "fresh_until_utc": "2026-07-26T12:00:00Z",
        "probabilities": {
            "75°F or below": 0.1,
            "76-77°F": 0.3,
            "78°F or higher": 0.6,
        },
    })
    new_p0m = _predecessor(
        new_p0,
        checkpoint_id="p0m-model-refresh",
        decision_at_utc="2026-07-26T09:10:00Z",
    )
    prediction_root, predecessor, consumed, rebasing = select_p1_replay_base(
        [old_p0, old_p0m, old_p1, new_p0, new_p0m],
        as_of_utc="2026-07-26T10:00:00Z",
    )
    assert predecessor.checkpoint_id == new_p0m.checkpoint_id
    assert prediction_root.checkpoint_id == new_p0m.checkpoint_id
    assert consumed == set()
    assert rebasing is True

    packets = compile_station_p1_packets(
        predecessor=predecessor,
        prediction_root=prediction_root,
        observations=[
            _observation("o1", 24.0, "2026-07-26T04:00:00Z"),
            _observation("o2", 26.0, "2026-07-26T07:00:00Z"),
        ],
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T10:00:00Z",
        consumed_observation_ids=consumed,
    )
    assert [packet["observation_id"] for packet in packets] == ["o1", "o2"]
    assert packets[-1]["expected_envelope"]["lineage"]["p0_root_checkpoint_id"] == "p0-model-refresh"


def test_stale_checkpoint_remains_available_only_as_durable_p1_replay_cursor() -> None:
    p0 = _p0_root()
    stale_p0m = _predecessor(p0)
    prediction_root, predecessor, consumed, rebasing = select_p1_replay_base(
        [p0, stale_p0m],
        as_of_utc="2026-07-26T08:00:00Z",
    )

    assert predecessor.checkpoint_id == stale_p0m.checkpoint_id
    assert prediction_root.checkpoint_id == stale_p0m.checkpoint_id
    assert predecessor.is_fresh("2026-07-26T08:00:00Z") is False
    assert consumed == set()
    assert rebasing is False

    packets = compile_station_p1_packets(
        predecessor=predecessor,
        observations=[
            _observation("fresh-new-high", 26.0, "2026-07-26T07:30:00Z"),
        ],
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T08:00:00Z",
    )
    final = ProbabilityEnvelope.from_mapping(
        packets[-1]["expected_envelope"]
    )
    assert final.is_fresh("2026-07-26T08:00:00Z") is True
    assert stale_p0m.fresh_until_utc == "2026-07-26T06:00:00Z"


def test_replay_accepts_p0_root_and_ignores_legacy_p0_root_p1() -> None:
    p0 = _p0_root()
    root, predecessor, consumed, rebasing = select_p1_replay_base(
        [p0], as_of_utc="2026-07-26T08:00:00Z"
    )
    assert root.checkpoint_id == p0.checkpoint_id
    assert predecessor.checkpoint_id == p0.checkpoint_id
    assert consumed == set()
    assert rebasing is False

    p0m = _predecessor(p0)
    tampered_p0m = ProbabilityEnvelope.from_mapping({
        **p0m.to_dict(),
        "checkpoint_id": "tampered-p0m",
        "lineage": {
            **p0m.lineage,
            "p0_root_content_sha256": "f" * 64,
        },
    })
    with pytest.raises(
        ContractError,
        match="p1_replay_p0m_root_lineage_mismatch",
    ):
        select_p1_replay_base(
            [p0, tampered_p0m],
            as_of_utc="2026-07-26T08:00:00Z",
        )
    legacy_p1 = ProbabilityEnvelope.from_mapping({
        **p0.to_dict(),
        "stage": "P1",
        "checkpoint_id": "legacy-p0-root-p1",
        "decision_at_utc": "2026-07-26T07:00:00Z",
        "fresh_until_utc": "2026-07-26T09:00:00Z",
        "lineage": {
            **p0.lineage,
            "prediction_root_stage": "P0",
            "prediction_root_checkpoint_id": p0.checkpoint_id,
            "prediction_root_content_sha256": p0.content_sha256,
            "p0_root_checkpoint_id": p0.checkpoint_id,
            "canonical_observation_event_id": "legacy-observation",
        },
    })
    root, predecessor, consumed, rebasing = select_p1_replay_base(
        [p0, p0m, legacy_p1],
        as_of_utc="2026-07-26T08:00:00Z",
    )
    assert root.checkpoint_id == p0m.checkpoint_id
    assert predecessor.checkpoint_id == p0m.checkpoint_id
    assert consumed == set()
    assert rebasing is True


def test_fahrenheit_hard_lower_bound_accounts_for_metar_celsius_quantization() -> None:
    assert _reported_hard_lower_in_unit(24.0, "F") == 74
    assert _reported_hard_lower_in_unit(26.0, "F") == 78


def test_p1_builder_projects_exact_strict_materializer_packet() -> None:
    rich = compile_station_p1_packets(
        predecessor=_predecessor(),
        observations=[_observation("o1", 24.0, "2026-07-26T04:00:00Z")],
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T08:00:00Z",
    )[0]
    projected = materialization_packet(rich)
    rebuilt = build_envelope(projected)

    assert set(projected).isdisjoint({
        "compiled_bound",
        "expected_envelope",
        "hard_bound_id",
        "observation_id",
        "p0_rebase",
        "p0_root_checkpoint_id",
    })
    assert rebuilt.to_dict() == rich["expected_envelope"]


def test_canonical_packet_preserves_and_binds_fact_identities() -> None:
    rich = compile_station_p1_packets(
        predecessor=_predecessor(),
        observations=[
            _observation("o1", 24.0, "2026-07-26T04:00:00Z")
        ],
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T08:00:00Z",
    )[0]

    projected = canonical_materialization_packet(rich)

    assert projected["observation_id"] == "o1"
    assert projected["hard_bound_id"] == rich["hard_bound_id"]
    assert (
        rich["expected_envelope"]["lineage"][
            "canonical_observation_event_id"
        ]
        == projected["observation_id"]
    )
    assert (
        rich["expected_envelope"]["lineage"]["hard_bound_id"]
        == projected["hard_bound_id"]
    )


def test_cli_prints_manifest_without_runtime_name_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = {
        "blockers": [],
        "packet_count": 0,
        "boundary": {"calls_order_gateway": False},
    }
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.build_packets",
        lambda **_kwargs: expected,
    )

    assert main([
        "--db",
        str(tmp_path / "target.db"),
        "--canonical-evidence",
        str(tmp_path / "canonical.json"),
        "--event-date",
        "2026-07-27",
        "--as-of-utc",
        "2026-07-26T21:00:00Z",
        "--output-dir",
        str(tmp_path / "packets"),
    ]) == 0
    assert json.loads(capsys.readouterr().out) == expected


def test_builder_uses_started_registry_as_coverage_denominator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canonical = _canonical_evidence([
            _observation("o1", 24.0, "2026-07-26T04:00:00Z"),
            {
                **_observation(
                    "not-in-registry",
                    25.0,
                    "2026-07-26T04:00:00Z",
                ),
                "station_id": "KXXX",
            },
        ])
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.load_bounded_json",
        lambda _path: canonical,
    )

    def registry_rows(_conn, *, station_id="", **_kwargs):
        rows = [
            {"station_id": "KATL"},
            {"station_id": "KSEA"},
        ]
        return [
            row for row in rows
            if not station_id or row["station_id"] == station_id
        ]

    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.load_started_registry_rows",
        registry_rows,
    )
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.load_materialized_probability_envelopes",
        lambda *_args, **_kwargs: [_predecessor()],
    )
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.select_p1_replay_base",
        lambda *_args, **_kwargs: (_predecessor(), _predecessor(), set(), False),
    )
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.compile_station_p1_packets",
        lambda **_kwargs: [],
    )
    database = tmp_path / "target.db"
    database.touch()

    result = build_packets(
        db_path=database,
        canonical_evidence_path=tmp_path / "canonical.json",
        event_date="2026-07-26",
        as_of_utc="2026-07-26T08:00:00Z",
        output_dir=tmp_path / "packets",
    )

    assert result["registry_stations"] == ["KATL", "KSEA"]
    assert result["observed_stations"] == ["KATL", "KXXX"]
    assert result["registry_station_count"] == 2
    assert result["materialized_station_count"] == 0
    assert result["coverage_complete"] is False
    assert {
        (item["station_id"], item["reason"])
        for item in result["blockers"]
    } == {
        ("KATL", "p1_no_materializable_packet"),
        ("KSEA", "p1_formal_observation_missing"),
    }


def test_builder_fails_closed_when_started_registry_scope_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.load_bounded_json",
        lambda _path: _canonical_evidence([]),
    )
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.load_started_registry_rows",
        lambda *_args, **_kwargs: [],
    )
    database = tmp_path / "target.db"
    database.touch()

    result = build_packets(
        db_path=database,
        canonical_evidence_path=tmp_path / "canonical.json",
        event_date="2026-07-26",
        as_of_utc="2026-07-26T08:00:00Z",
        output_dir=tmp_path / "packets",
    )

    assert result["coverage_complete"] is False
    assert result["blockers"] == [{
        "station_id": "",
        "reason": "p1_started_registry_scope_empty",
    }]


def test_existing_p1_counts_as_materialized_when_replay_has_no_new_packet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.load_bounded_json",
        lambda _path: _canonical_evidence([
                _observation("o1", 24.0, "2026-07-26T04:00:00Z")
            ]),
    )
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.load_started_registry_rows",
        lambda *_args, **_kwargs: [{"station_id": "KATL"}],
    )
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.load_materialized_probability_envelopes",
        lambda *_args, **_kwargs: [_predecessor(), _existing_p1()],
    )
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.select_p1_replay_base",
        lambda *_args, **_kwargs: (
            _predecessor(),
            _existing_p1(),
            {"o1"},
            False,
        ),
    )
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.compile_station_p1_packets",
        lambda **_kwargs: [],
    )
    database = tmp_path / "target.db"
    database.touch()

    result = build_packets(
        db_path=database,
        canonical_evidence_path=tmp_path / "canonical.json",
        event_date="2026-07-26",
        as_of_utc="2026-07-26T08:00:00Z",
        output_dir=tmp_path / "packets",
    )

    assert result["coverage_complete"] is True
    assert result["materialized_stations"] == ["KATL"]
    assert result["packet_count"] == 0
    assert result["blockers"] == []


def test_builder_allowlist_excludes_other_registry_lanes_and_reports_missing_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.load_bounded_json",
        lambda _path: _canonical_evidence([]),
    )
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.load_started_registry_rows",
        lambda *_args, **_kwargs: [
            {"station_id": "KATL"},
            {"station_id": "ZBAA"},
        ],
    )
    database = tmp_path / "target.db"
    database.touch()

    result = build_packets(
        db_path=database,
        canonical_evidence_path=tmp_path / "canonical.json",
        event_date="2026-07-26",
        as_of_utc="2026-07-26T08:00:00Z",
        output_dir=tmp_path / "packets",
        station_ids={"KATL", "KSEA"},
    )

    assert result["station_scope"] == {
        "mode": "allowlist",
        "requested_station_ids": ["KATL", "KSEA"],
    }
    assert result["registry_stations"] == ["KATL"]
    assert {
        (item["station_id"], item["reason"])
        for item in result["blockers"]
    } == {
        ("KATL", "p1_formal_observation_missing"),
        ("KSEA", "p1_requested_station_not_in_started_registry"),
    }


def test_builder_rejects_tampered_canonical_evidence_before_db_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    evidence = _canonical_evidence([])
    evidence["observations"] = [{"station_id": "KATL"}]
    monkeypatch.setattr(
        "scripts.build_bayes_basket_target_p1_packets.load_bounded_json",
        lambda _path: evidence,
    )

    with pytest.raises(
        ContractError,
        match="p1_canonical_evidence_receipt_mismatch",
    ):
        build_packets(
            db_path=tmp_path / "does-not-exist.db",
            canonical_evidence_path=tmp_path / "canonical.json",
            event_date="2026-07-26",
            as_of_utc="2026-07-26T08:00:00Z",
            output_dir=tmp_path / "packets",
        )


def _midnight_observation(
    observation_id: str,
    temperature_c: float,
    *,
    valid_at: str,
    committed_at: str,
) -> dict:
    return {
        "observation_id": observation_id,
        "station_id": "KATL",
        "event_date_local": "2026-07-26",
        "settlement_timezone": "America/New_York",
        "reported_temperature_c": temperature_c,
        "valid_at_utc": valid_at,
        "canonical_committed_at_utc": committed_at,
        "raw_sha256": ("1" if observation_id == "o1" else "2") * 64,
        "source_lane": "AWC",
        "report_type": "METAR",
        "evidence_role": "FORMAL",
        "qc_status": "ACCEPTED",
        "correction_suspected": False,
    }


def test_midnight_report_committed_early_does_not_predate_its_event_day() -> None:
    """The local-midnight report may commit ahead of its nominal minute.

    OEJN commits ~6s early and SBGR ~30s early on most days.  Inheriting that
    commit as the packet's decision clock put the decision before the
    settlement day started, and the day-started assert in ``materialize_p1``
    fail-closed the whole station-day (SBGR lost the first seven hours of
    2026-08-29's event morning that way).  A decision about a day may not
    predate the day.
    """

    packets = compile_station_p1_packets(
        # A P0-M root frozen the evening before, so nothing else lifts the
        # stage clock past midnight before the first report arrives.
        predecessor=_predecessor(decision_at_utc="2026-07-25T22:00:00Z"),
        observations=[
            _midnight_observation(
                "o1",
                26.0,
                valid_at="2026-07-26T04:00:00Z",
                committed_at="2026-07-26T03:59:30Z",
            ),
        ],
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T08:00:00Z",
    )

    assert [
        packet["observation_id"]
        for packet in packets
        if packet["reported_high_advanced"]
    ] == ["o1"]
    # Floored at the event day's own start (00:00 America/New_York), not the
    # early commit clock.
    assert (
        packets[0]["expected_envelope"]["decision_at_utc"]
        == "2026-07-26T04:00:00.000000Z"
    )


def test_the_cooling_afternoon_reaches_the_served_p1_without_a_new_high() -> None:
    """The reports after the day's high are in the prefix that serves.

    The 2026-09-12 diagnosis: because the walk emitted only where the high
    advanced, the served envelope kept the clock and the physical state of
    the last new high.  ZGSZ 2026-09-11 is the shape -- the 20:00-local P1
    was written at 19:55 and still carried the 16:00 high with drawdown 0 and
    momentum +1C/h, while the 19:00 report had already come in 2C lower.  320
    of 550 served four-layer branches ran more than two hours behind their own
    closing report.

    Here the day peaks at 07:00Z and cools through 09:00Z.  The last packet
    that *advances* stops at the peak, as it must -- the hard bound is the
    high.  The packet that serves is the refresh behind it, and it has read
    the cooling.
    """

    packets = compile_station_p1_packets(
        predecessor=_predecessor(),
        observations=[
            _observation("o1", 24.0, "2026-07-26T04:00:00Z"),
            _observation("peak", 26.0, "2026-07-26T07:00:00Z"),
            _observation("cool1", 24.0, "2026-07-26T08:00:00Z"),
            _observation("cool2", 23.0, "2026-07-26T09:00:00Z"),
        ],
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T09:05:00Z",
    )

    advancing = [
        packet for packet in packets if packet["reported_high_advanced"]
    ]
    assert [packet["observation_id"] for packet in advancing] == ["o1", "peak"]
    assert advancing[-1]["trajectory_proof"]["canonical_observation_ids"] == [
        "o1", "peak"
    ]

    served = packets[-1]
    assert served["reported_high_advanced"] is False
    assert served["trajectory_proof"]["canonical_observation_ids"] == [
        "o1", "peak", "cool1", "cool2"
    ]
    # Its clock is the compile instant, not the peak's minute.
    assert served["trajectory_proof"]["as_of_utc"].startswith(
        "2026-07-26T09:05:00"
    )
    # The hard bound is still the day's high; only the belief moved.
    assert served["compiled_bound"]["reported_high_c"] == 26.0
    assert (
        served["expected_envelope"]["lineage"]["reported_high_c"]
        == advancing[-1]["expected_envelope"]["lineage"]["reported_high_c"]
    )


def test_a_second_refresh_waits_for_new_evidence_or_a_lapsed_cadence() -> None:
    """One refresh per new formal report, or per freshness window; not per call.

    The refresh becomes the head, and the head has already read the newest
    report, so recompiling the same evidence at the same cadence emits
    nothing more.  What re-opens it is another report, or the head lapsing
    its own grace -- which is the case the worker's copy-the-probabilities
    reaffirmation used to cover.
    """

    observations = [
        _observation("o1", 24.0, "2026-07-26T04:00:00Z"),
        _observation("peak", 26.0, "2026-07-26T07:00:00Z"),
        _observation("cool1", 24.0, "2026-07-26T08:00:00Z"),
    ]
    first = compile_station_p1_packets(
        predecessor=_predecessor(),
        observations=observations,
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T08:05:00Z",
    )
    head = ProbabilityEnvelope.from_mapping(first[-1]["expected_envelope"])
    assert first[-1]["reported_high_advanced"] is False

    consumed = {"o1", "peak", "cool1"}
    quiet = compile_station_p1_packets(
        predecessor=head,
        prediction_root=_predecessor(),
        observations=observations,
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T08:20:00Z",
        consumed_observation_ids=consumed,
    )
    assert quiet == []

    lapsed = compile_station_p1_packets(
        predecessor=head,
        prediction_root=_predecessor(),
        observations=observations,
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T09:30:00Z",
        consumed_observation_ids=consumed,
    )
    assert [packet["observation_id"] for packet in lapsed] == ["cool1"]
    assert lapsed[-1]["reported_high_advanced"] is False
    assert lapsed[-1]["state_refresh_of_checkpoint_id"] == head.checkpoint_id


def test_a_replay_head_already_standing_at_now_gets_no_refresh_behind_it() -> None:
    """A rebase re-clocks every step to now; there is nothing left to refresh.

    ``is_fresh`` is false both for a head whose grace elapsed and for one
    whose decision clock sits ahead of the compile instant -- which is every
    head a replay pass just wrote, because the walk chains a microsecond per
    step past its own floor.  Reading the second as the first wrote a refresh
    that restated its predecessor microsecond for microsecond: 10 of 41 on
    the first production pass after the refresh landed (2026-09-12).
    """

    observations = [
        _observation("o1", 24.0, "2026-07-26T04:00:00Z"),
        _observation("peak", 26.0, "2026-07-26T07:00:00Z"),
    ]
    # A floor at the compile instant is what a rebase hands the walk: every
    # step lands at floor + n microseconds, so the last one is already ahead
    # of now and closes on the newest formal report.
    packets = compile_station_p1_packets(
        predecessor=_predecessor(),
        observations=observations,
        registry_rows=_registry(),
        compilation_at_utc="2026-07-26T08:00:00Z",
        decision_floor_at_utc="2026-07-26T08:00:00Z",
    )
    assert [packet["observation_id"] for packet in packets] == ["o1", "peak"]
    assert all(packet["reported_high_advanced"] for packet in packets)
    assert not any(
        packet["state_refresh_of_checkpoint_id"] for packet in packets
    )
