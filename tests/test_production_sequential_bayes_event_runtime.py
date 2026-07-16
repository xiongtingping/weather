from __future__ import annotations

import json
from pathlib import Path

from canonical_observation_runtime import canonicalize_observations
from canonical_observation_writer import persist_canonical_observation_proposal
from production_canonical_event_drain_runtime import drain_production_canonical_events
from production_initial_posterior_writer import commit_production_initial_posterior
from production_initial_posterior_checkpoint import rebase_production_initial_posterior_successor
from production_probability_p0_runtime import (
    build_production_p0_inputs,
    compile_production_p0_candidate,
)
from production_amsc_archive_probability_ingress import ensure_production_daily_p0
from production_sequential_bayes_event_runtime import (
    build_production_canonical_event_candidate,
    commit_production_canonical_event,
    load_production_probability_cycle,
)
from source_observation_runtime import build_source_observation
from tests.test_production_initial_posterior import _conn
from tests.test_production_probability_p0_runtime import _activation, _files


EVENT_DATE = "2026-07-14"
P0_AS_OF = "2026-07-13T12:00:00Z"


def _production_p0(tmp_path: Path):
    ncei, weak, resolution, curves = _files(tmp_path)
    inputs = build_production_p0_inputs(
        event_date=EVENT_DATE,
        decision_as_of=P0_AS_OF,
        source_observations=[],
        curve_root=curves,
        weak_artifact_path=weak,
        ncei_root=ncei,
        resolution_authority_path=resolution,
    )
    activation = _activation(inputs["runtime_artifacts"])
    compiled = compile_production_p0_candidate(activation=activation, inputs=inputs)
    return inputs, activation, compiled, resolution


def _canonical_speci(conn, *, target_identity: str) -> str:
    observation = build_source_observation(
        source_id="zuuu_metar_raw_amsc",
        station_id="ZUUU",
        event_date=EVENT_DATE,
        valid_time="2026-07-14T04:30:00Z",
        fetched_at="2026-07-14T04:31:00Z",
        temperature_c=33.0,
        dewpoint_c=24.0,
        raw_ref="SPECI ZUUU 140430Z 00000MPS 9999 FEW020 33/24 Q1004",
        raw_payload={"raw": "fixture"},
        payload={
            "report_type": "SPECI",
            "cloud_text": "FEW020",
            "weather_text": "",
        },
    ).as_dict()
    proposal = canonicalize_observations(
        [observation],
        target_identity=target_identity,
        event_date=EVENT_DATE,
        decision_as_of="2026-07-14T04:31:00Z",
    )
    persisted = persist_canonical_observation_proposal(conn, proposal)
    assert persisted["inserted_events"] == 1
    conn.commit()
    return str(proposal["events"][0]["canonical_observation_event_id"])


def _canonical_metar(conn, *, target_identity: str) -> str:
    observation = build_source_observation(
        source_id="zuuu_metar_raw_amsc",
        station_id="ZUUU",
        event_date=EVENT_DATE,
        valid_time="2026-07-14T05:00:00Z",
        fetched_at="2026-07-14T05:01:00Z",
        temperature_c=33.5,
        dewpoint_c=24.0,
        raw_ref="METAR ZUUU 140500Z 00000MPS 9999 SCT020 34/24 Q1004",
        raw_payload={"raw": "fixture-metar"},
        payload={"report_type": "METAR", "cloud_text": "SCT020", "weather_text": ""},
    ).as_dict()
    proposal = canonicalize_observations(
        [observation], target_identity=target_identity, event_date=EVENT_DATE,
        decision_as_of="2026-07-14T05:01:00Z",
    )
    persisted = persist_canonical_observation_proposal(conn, proposal)
    assert persisted["inserted_events"] == 1
    conn.commit()
    return str(proposal["events"][0]["canonical_observation_event_id"])


def test_speci_updates_the_unique_production_ssot_and_persists_termination_risk(tmp_path: Path) -> None:
    inputs, activation, compiled, resolution = _production_p0(tmp_path)
    conn = _conn()
    initial = compiled["candidate"]
    committed_p0 = commit_production_initial_posterior(
        conn,
        activation=activation,
        initial_checkpoint=initial,
        slug=f"physical-weather-day-zuuu-{EVENT_DATE}",
        event_date=EVENT_DATE,
        fact_as_of=P0_AS_OF,
        correlation_id="corr_p0_production_123456",
    )
    assert committed_p0["ok"] is True
    event_id = _canonical_speci(conn, target_identity=inputs["target_identity"])

    prepared = build_production_canonical_event_candidate(
        conn,
        activation=activation,
        event_date=EVENT_DATE,
        canonical_observation_event_id=event_id,
        started_at="2026-07-14T04:31:00.050Z",
        completed_at="2026-07-14T04:31:00.200Z",
        resolution_authority_path=resolution,
    )
    assert prepared["ok"] is True
    assert prepared["state"] == "physical_only_candidate_ready"
    assert prepared["candidate"]["strong_observation_update"]["termination_risk"]["updates_posterior"] is False

    committed = commit_production_canonical_event(
        conn,
        activation=activation,
        prepared=prepared,
        completed_at="2026-07-14T04:31:00.200Z",
    )
    assert committed["ok"] is True
    assert committed["state"] == "committed_physical_only"
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 2
    assert conn.execute("SELECT state FROM posterior_checkpoints ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "committed"
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
    fact = json.loads(conn.execute(
        """SELECT h.fact_json FROM event_fact_history h
           JOIN event_fact_latest l ON l.fact_id=h.id
           WHERE l.slug=? AND l.event_date=?""",
        (f"physical-weather-day-zuuu-{EVENT_DATE}", EVENT_DATE),
    ).fetchone()[0])
    ssot = fact["posterior_ssot_fact"]
    assert ssot["last_canonical_event_id"] == event_id
    assert ssot["termination_risk"]["state"] == "available"
    assert ssot["termination_risk"]["updates_posterior"] is False
    assert ssot["observed_max_lower_bound_c"] >= 32.5


def test_canonical_drain_consumes_speci_and_metar_in_order_without_order_side_effects(tmp_path: Path) -> None:
    inputs, activation, compiled, resolution = _production_p0(tmp_path)
    conn = _conn()
    commit_production_initial_posterior(
        conn, activation=activation, initial_checkpoint=compiled["candidate"],
        slug=f"physical-weather-day-zuuu-{EVENT_DATE}", event_date=EVENT_DATE,
        fact_as_of=P0_AS_OF, correlation_id="corr_p0_drain_123456",
    )
    speci_id = _canonical_speci(conn, target_identity=inputs["target_identity"])
    metar_id = _canonical_metar(conn, target_identity=inputs["target_identity"])
    timestamps = iter([
        "2026-07-14T04:31:00.050Z", "2026-07-14T04:31:00.200Z",
        "2026-07-14T05:01:00.050Z", "2026-07-14T05:01:00.250Z",
    ])

    result = drain_production_canonical_events(
        conn,
        activation=activation,
        event_date=EVENT_DATE,
        resolution_authority_path=resolution,
        clock=lambda: next(timestamps),
    )

    assert result["ok"] is True
    assert result["state"] == "drained"
    assert [row["canonical_observation_event_id"] for row in result["outcomes"]] == [speci_id, metar_id]
    assert all(row["three_second_target_met"] is True for row in result["outcomes"])
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0


def test_new_activation_generation_replays_events_consumed_by_failed_cutover(tmp_path: Path) -> None:
    inputs, first_activation, first_compiled, resolution = _production_p0(tmp_path)
    conn = _conn()
    first_p0 = commit_production_initial_posterior(
        conn, activation=first_activation, initial_checkpoint=first_compiled["candidate"],
        slug=f"physical-weather-day-zuuu-{EVENT_DATE}", event_date=EVENT_DATE,
        fact_as_of=P0_AS_OF, correlation_id="corr_failed_cutover_first",
    )
    speci_id = _canonical_speci(conn, target_identity=inputs["target_identity"])
    metar_id = _canonical_metar(conn, target_identity=inputs["target_identity"])
    first_clock = iter([
        "2026-07-14T04:31:00.050Z", "2026-07-14T04:31:00.200Z",
        "2026-07-14T05:01:00.050Z", "2026-07-14T05:01:00.250Z",
    ])
    assert drain_production_canonical_events(
        conn, activation=first_activation, event_date=EVENT_DATE,
        resolution_authority_path=resolution, clock=lambda: next(first_clock),
    )["ok"] is True

    second_inputs = build_production_p0_inputs(
        event_date=EVENT_DATE, decision_as_of="2026-07-14T06:00:00Z",
        source_observations=[], curve_root=tmp_path / "curves",
        weak_artifact_path=tmp_path / "weak.json", ncei_root=tmp_path / "ncei",
        resolution_authority_path=resolution,
    )
    second_activation = _activation(second_inputs["runtime_artifacts"])
    second_activation["activation_effective_at"] = "2026-07-14T06:00:00Z"
    second_compiled = compile_production_p0_candidate(
        activation=second_activation, inputs=second_inputs,
    )
    second_candidate = rebase_production_initial_posterior_successor(
        second_compiled["candidate"], replay_generation=1,
        supersedes_checkpoint_id=conn.execute(
            "SELECT posterior_checkpoint_id FROM posterior_checkpoints ORDER BY committed_at DESC,rowid DESC LIMIT 1"
        ).fetchone()[0],
    )
    commit_production_initial_posterior(
        conn, activation=second_activation, initial_checkpoint=second_candidate,
        slug=f"physical-weather-day-zuuu-{EVENT_DATE}", event_date=EVENT_DATE,
        fact_as_of="2026-07-14T06:00:00Z", correlation_id="corr_cutover_retry_second",
    )
    second_clock = iter([
        "2026-07-14T06:00:00.050Z", "2026-07-14T06:00:00.200Z",
        "2026-07-14T06:00:00.250Z", "2026-07-14T06:00:00.400Z",
    ])
    replay = drain_production_canonical_events(
        conn, activation=second_activation, event_date=EVENT_DATE,
        resolution_authority_path=resolution, clock=lambda: next(second_clock),
    )

    assert replay["ok"] is True
    assert [row["canonical_observation_event_id"] for row in replay["outcomes"]] == [speci_id, metar_id]
    assert conn.execute("SELECT COUNT(*) FROM production_initial_posterior_lineage").fetchone()[0] == 2
    assert conn.execute(
        "SELECT replay_generation FROM posterior_checkpoints ORDER BY committed_at DESC,rowid DESC LIMIT 1"
    ).fetchone()[0] == 1

    # Activation rollback must select its own immutable generation rather than
    # the globally newest checkpoint/public pointer left by a later epoch.
    first_cycle = load_production_probability_cycle(
        conn,
        activation=first_activation,
        event_date=EVENT_DATE,
        resolution_authority_path=resolution,
    )
    second_cycle = load_production_probability_cycle(
        conn,
        activation=second_activation,
        event_date=EVENT_DATE,
        resolution_authority_path=resolution,
    )
    assert first_cycle["ok"] is True
    assert first_cycle["active_replay_generation"] == 0
    assert first_cycle["previous_checkpoint"]["replay_generation"] == 0
    assert second_cycle["ok"] is True
    assert second_cycle["active_replay_generation"] == 1
    assert second_cycle["previous_checkpoint"]["replay_generation"] == 1


def test_cutover_replay_never_moves_bayes_time_before_the_p0_checkpoint(tmp_path: Path) -> None:
    ncei, weak, resolution, curves = _files(tmp_path)
    cutover_as_of = "2026-07-14T05:30:00Z"
    inputs = build_production_p0_inputs(
        event_date=EVENT_DATE, decision_as_of=cutover_as_of, source_observations=[],
        curve_root=curves, weak_artifact_path=weak, ncei_root=ncei,
        resolution_authority_path=resolution,
    )
    activation = _activation(inputs["runtime_artifacts"])
    compiled = compile_production_p0_candidate(activation=activation, inputs=inputs)
    conn = _conn()
    commit_production_initial_posterior(
        conn, activation=activation, initial_checkpoint=compiled["candidate"],
        slug=f"physical-weather-day-zuuu-{EVENT_DATE}", event_date=EVENT_DATE,
        fact_as_of=cutover_as_of, correlation_id="corr_cutover_replay_123456",
    )
    event_id = _canonical_speci(conn, target_identity=inputs["target_identity"])

    prepared = build_production_canonical_event_candidate(
        conn, activation=activation, event_date=EVENT_DATE,
        canonical_observation_event_id=event_id,
        started_at="2026-07-14T05:30:00.050Z",
        completed_at="2026-07-14T05:30:00.200Z",
        resolution_authority_path=resolution,
    )

    assert prepared["ok"] is True
    assert prepared["candidate"]["strong_observation_update"]["decision_as_of"] == "2026-07-14T05:30:00.000Z"


def test_incremental_update_refuses_to_fork_the_p0_public_slug(tmp_path: Path) -> None:
    inputs, activation, compiled, resolution = _production_p0(tmp_path)
    conn = _conn()
    frozen_slug = "highest-temperature-in-chengdu-on-july-14-2026"
    commit_production_initial_posterior(
        conn, activation=activation, initial_checkpoint=compiled["candidate"],
        slug=frozen_slug, event_date=EVENT_DATE, fact_as_of=P0_AS_OF,
        correlation_id="corr_public_slug_123456",
    )
    event_id = _canonical_speci(conn, target_identity=inputs["target_identity"])

    prepared = build_production_canonical_event_candidate(
        conn, activation=activation, event_date=EVENT_DATE,
        canonical_observation_event_id=event_id,
        started_at="2026-07-14T04:31:00.050Z",
        completed_at="2026-07-14T04:31:00.200Z",
        public_slug="different-public-slug",
        resolution_authority_path=resolution,
    )

    assert prepared["ok"] is False
    assert prepared["reason"] == "production_probability_public_slug_drift"


def test_daily_p0_rollover_is_market_independent_and_idempotent(tmp_path: Path) -> None:
    ncei, weak, resolution, curves = _files(tmp_path)
    inputs = build_production_p0_inputs(
        event_date=EVENT_DATE,
        decision_as_of=P0_AS_OF,
        source_observations=[],
        curve_root=curves,
        weak_artifact_path=weak,
        ncei_root=ncei,
        resolution_authority_path=resolution,
    )
    activation = _activation(inputs["runtime_artifacts"])
    conn = _conn()

    first = ensure_production_daily_p0(
        conn,
        activation=activation,
        event_date=EVENT_DATE,
        decision_as_of=P0_AS_OF,
        curve_root=curves,
        weak_artifact_path=weak,
        ncei_root=ncei,
        resolution_authority_path=resolution,
    )
    second = ensure_production_daily_p0(
        conn,
        activation=activation,
        event_date=EVENT_DATE,
        decision_as_of="2026-07-13T12:01:00Z",
        curve_root=curves,
        weak_artifact_path=weak,
        ncei_root=ncei,
        resolution_authority_path=resolution,
    )

    assert first["ok"] is True
    assert first["state"] == "initialized"
    assert first["market_discovery_required"] is False
    assert second["ok"] is True
    assert second["state"] == "already_initialized"
    assert second["posterior_checkpoint_id"] == first["posterior_checkpoint_id"]
    assert conn.execute("SELECT COUNT(*) FROM production_initial_posterior_lineage").fetchone()[0] == 1
    assert conn.execute(
        "SELECT slug FROM event_fact_latest WHERE event_date=?",
        (EVENT_DATE,),
    ).fetchone()[0] == "highest-temperature-in-chengdu-on-july-14-2026"
