from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target import ContractError, ingest_artifact_bundle
from bayes_basket_target.no_live_candidate_artifacts import (
    build_no_live_candidate_artifact_packet,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from scripts.build_station_policy_candidate_bundle import build_bundle
from scripts.build_bayes_basket_target_no_live_candidate_artifacts import (
    _registry_station_scope,
)


def _conn(*, include_station: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(conn)
    conn.execute(
        "CREATE TABLE basket_target_market_registry("
        "station_id TEXT, settlement_timezone TEXT)"
    )
    if include_station:
        conn.execute(
            "INSERT INTO basket_target_market_registry VALUES(?,?)",
            ("ZUUU", "Asia/Shanghai"),
        )
    return conn


def test_candidate_packet_is_unpromoted_no_live_and_idempotent() -> None:
    with _conn() as conn:
        packet = build_no_live_candidate_artifact_packet(
            conn,
            candidate_bundle=build_bundle(),
            station_ids={"ZUUU"},
        )
        assert packet["policy_authority_scope"] == "PRODUCTION_NO_LIVE_CANDIDATE"
        assert packet["cash_phase_boundary"]["changes_probability"] is False
        assert packet["cash_phase_policies"][0]["promoted"] is False
        cash_policy = packet["cash_phase_policies"][0]
        assert cash_policy["heavy_to_standard_local_time"] == "19:54"
        assert cash_policy["standard_to_tail_local_time"] == "09:58"
        assert cash_policy["cash_by_phase"] == {
            "STANDARD": 10.77,
            "TAIL": 0.25,
        }
        assert len(packet["cash_phase_authority_manifests"]) == 1
        cash_authority = packet["cash_phase_authority_manifests"][0]
        assert cash_authority["policy_version"] == cash_policy["policy_version"]
        assert cash_authority["artifact_sha256"] == cash_policy["content_sha256"]
        assert cash_authority["station_timezone"] == "Asia/Shanghai"
        assert cash_authority["authority_scope"] == (
            "PRODUCTION_NO_LIVE_CANDIDATE"
        )
        prior = packet["global_priors"][0]
        assert prior["global_prior_id"] == "target-no-live-global-prior-design-v3"
        assert {
            "allocation_mode",
            "base_budget_usdc",
            "bias_budget_usdc",
            "edge_budget_usdc",
        }.isdisjoint(prior["default_policy"])
        committed = ingest_artifact_bundle(conn, packet, commit=True)
        assert committed["counts"]["policy_artifacts"] == 3
        assert committed["counts"]["policy_promotions"] == 0
        assert committed["counts"]["cash_phase_authority_manifests"] == 1
        assert sum(ingest_artifact_bundle(conn, packet, commit=True)["counts"].values()) == 0


def test_untrained_p1_scope_uses_explicit_global_fallback_not_p0_artifacts() -> None:
    with _conn() as conn:
        packet = build_no_live_candidate_artifact_packet(
            conn,
            candidate_bundle=build_bundle(),
            station_ids={"ZUUU"},
            stages_by_station={"ZUUU": {"P0", "P1"}},
        )
        p1 = next(
            row for row in packet["policy_activations"]
            if row["station_id"] == "ZUUU" and row["stage"] == "P1"
        )
        assert p1["target_artifact_id"] is None
        assert p1["reserve_artifact_id"] is None
        assert p1["cadence_artifact_id"] is None
        assert set(p1["fallback_families"]) == {"target", "reserve", "cadence"}
        assert p1["promoted"] is False


def test_maturity_mismatch_uses_global_fallback_not_d1_artifacts() -> None:
    with _conn() as conn:
        packet = build_no_live_candidate_artifact_packet(
            conn,
            candidate_bundle=build_bundle(),
            station_ids={"ZUUU"},
            stages_by_station={"ZUUU": {"P0"}},
            maturity_bins_by_station_stage={
                ("ZUUU", "P0"): "d2_fallback",
            },
        )
        activation = next(
            row
            for row in packet["policy_activations"]
            if row["station_id"] == "ZUUU"
            and row["stage"] == "P0"
            and row["maturity_bin"] == "d2_fallback"
        )
        assert activation["target_artifact_id"] is None
        assert activation["reserve_artifact_id"] is None
        assert activation["cadence_artifact_id"] is None
        assert set(activation["fallback_families"]) == {
            "target",
            "reserve",
            "cadence",
        }


def test_unpromoted_packet_without_explicit_candidate_authority_is_rejected() -> None:
    with _conn() as conn:
        packet = build_no_live_candidate_artifact_packet(
            conn,
            candidate_bundle=build_bundle(),
            station_ids={"ZUUU"},
        )
        packet.pop("policy_authority_scope")
        with pytest.raises(ContractError, match="candidate_policy_authority_scope_required"):
            ingest_artifact_bundle(conn, packet, commit=True)


def test_candidate_packet_requires_formal_station_timezone() -> None:
    with _conn(include_station=False) as conn:
        with pytest.raises(ContractError, match="station_timezone_missing"):
            build_no_live_candidate_artifact_packet(
                conn,
                candidate_bundle=build_bundle(),
                station_ids={"ZUUU"},
            )


def test_registry_scope_can_prepare_all_probability_stages_before_p0_exists() -> None:
    station_ids, stages, maturity = _registry_station_scope(
        {
            "rows": [
                {
                    "station_id": "ZUUU",
                    "event_date": "2026-07-28",
                    "source_eligible": 1,
                    "market_active": 1,
                    "market_accepting_orders": 1,
                },
                {
                    "station_id": "RKPK",
                    "event_date": "2026-07-28",
                    "source_eligible": 1,
                    "market_active": 1,
                    "market_accepting_orders": 1,
                },
                {
                    "station_id": "FUTURE",
                    "event_date": "2026-07-29",
                    "source_eligible": 1,
                    "market_active": 1,
                    "market_accepting_orders": 1,
                },
            ]
        },
        event_date="2026-07-28",
        stages={"P0", "P0-M", "P1"},
        maturity_bin="d1",
    )
    assert station_ids == {"ZUUU", "RKPK"}
    assert stages == {
        "ZUUU": {"P0", "P0-M", "P1"},
        "RKPK": {"P0", "P0-M", "P1"},
    }
    assert maturity[("ZUUU", "P1")] == "d1"
    assert ("FUTURE", "P0") not in maturity
