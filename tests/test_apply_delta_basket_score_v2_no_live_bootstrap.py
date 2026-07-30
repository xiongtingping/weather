from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from bayes_basket_target.basket_kernel.contracts import ContractError
from bayes_basket_target.delta_v2_artifact import resolve_delta_v2_policy
from bayes_basket_target.delta_v2_fill_artifact import (
    resolve_delta_v2_fill_distribution,
)
from scripts.apply_delta_basket_score_v2_no_live_bootstrap import (
    DEFAULT_MANIFEST,
    ROOT,
    apply_bootstrap,
)

GLOBAL_MANIFEST = (
    ROOT
    / "config/trading/"
    "delta_basket_score_v2_global_no_live_bootstrap_v1.json"
)


def _empty_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=0")


def test_bootstrap_materializes_no_live_before_oos_and_replays_zero_write(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _empty_db(db)

    first = apply_bootstrap(
        db_path=db,
        confirmed_writer_stopped=False,
    )
    second = apply_bootstrap(
        db_path=db,
        confirmed_writer_stopped=False,
    )

    assert first["rows_written"] == 14
    assert second["rows_written"] == 0
    assert first["maturity_bins"] == ["d1", "d2_fallback"]
    assert first["oos_gate_passed"] is False
    assert first["forward_oos_blocks_production_no_live"] is False
    assert first["forward_oos_blocks_live_or_parameter_promotion"] is True
    assert first["live_submit_authorized"] is False
    assert first["calls_order_gateway"] is False
    with sqlite3.connect(db) as conn:
        policy = resolve_delta_v2_policy(
            conn,
            station_id="RKPK",
            stage="P0",
            season_bin="summer",
            maturity_bin="d1",
            liquidity_bin="liquid",
            strategy_version="target-v1",
            evaluation_at_utc="2026-07-28T13:31:00Z",
        )
        fill = resolve_delta_v2_fill_distribution(
            conn,
            station_id="RKPK",
            stage="P0",
            season_bin="summer",
            maturity_bin="d1",
            liquidity_bin="liquid",
            bucket="34°C or below",
            price=0.05,
            timeout_seconds=60.0,
            queue_ahead_shares=10.0,
            spread=0.1,
            depth=100.0,
            quote_version="q-test",
            evaluation_at_utc="2026-07-28T13:31:00Z",
            policy_fill_distribution_artifact_sha256=(
                policy.fill_distribution_artifact_sha256
            ),
        )
        assert policy.oos_gate_passed is False
        assert fill.oos_gate_passed is False
        fallback_policy = resolve_delta_v2_policy(
            conn,
            station_id="RKPK",
            stage="P1",
            season_bin="summer",
            maturity_bin="d2_fallback",
            liquidity_bin="liquid",
            strategy_version="target-v1",
            evaluation_at_utc="2026-07-28T13:31:00Z",
        )
        assert fallback_policy.oos_gate_passed is False
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_bootstrap_boundary_rejects_any_oos_claim(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    _empty_db(db)
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    manifest["oos_gate_passed"] = True
    candidate = tmp_path / "bad-manifest.json"
    candidate.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ContractError, match="boundary_invalid"):
        apply_bootstrap(
            db_path=db,
            manifest_path=candidate,
            confirmed_writer_stopped=False,
        )


def test_global_no_live_bootstrap_resolves_new_station_without_claiming_oos(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _empty_db(db)

    result = apply_bootstrap(
        db_path=db,
        manifest_path=GLOBAL_MANIFEST,
        confirmed_writer_stopped=False,
    )

    assert result["station_id"] == "*"
    assert result["oos_gate_passed"] is False
    assert result["forward_oos_blocks_production_no_live"] is False
    assert result["forward_oos_blocks_live_or_parameter_promotion"] is True
    with sqlite3.connect(db) as conn:
        for station_id in ("EDDM", "EPWA"):
            policy = resolve_delta_v2_policy(
                conn,
                station_id=station_id,
                stage="P0-M",
                season_bin="summer",
                maturity_bin="d2",
                liquidity_bin="liquid",
                strategy_version="target-v2",
                evaluation_at_utc="2026-07-29T16:00:00Z",
            )
            assert policy.station_id == "*"
            assert policy.oos_gate_passed is False
            fill = resolve_delta_v2_fill_distribution(
                conn,
                station_id=station_id,
                stage="P0-M",
                season_bin="summer",
                maturity_bin="d2",
                liquidity_bin="liquid",
                bucket="30°C",
                price=0.211,
                timeout_seconds=60.0,
                queue_ahead_shares=10.0,
                spread=0.1,
                depth=100.0,
                quote_version="q-global-test",
                evaluation_at_utc="2026-07-29T16:00:00Z",
                policy_fill_distribution_artifact_sha256=(
                    policy.fill_distribution_artifact_sha256
                ),
            )
            assert fill.station_id == "*"
            assert fill.oos_gate_passed is False


def test_exact_station_no_live_authority_wins_over_global_fallback(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _empty_db(db)
    apply_bootstrap(
        db_path=db,
        manifest_path=GLOBAL_MANIFEST,
        confirmed_writer_stopped=False,
    )
    apply_bootstrap(
        db_path=db,
        manifest_path=(
            ROOT
            / "config/trading/"
            "delta_basket_score_v2_rkpk_no_live_bootstrap_v3.json"
        ),
        confirmed_writer_stopped=False,
    )

    with sqlite3.connect(db) as conn:
        rkpk = resolve_delta_v2_policy(
            conn,
            station_id="RKPK",
            stage="P1",
            season_bin="summer",
            maturity_bin="d2",
            liquidity_bin="liquid",
            strategy_version="target-v2",
            evaluation_at_utc="2026-07-29T16:00:00Z",
        )
        eddm = resolve_delta_v2_policy(
            conn,
            station_id="EDDM",
            stage="P1",
            season_bin="summer",
            maturity_bin="d2",
            liquidity_bin="liquid",
            strategy_version="target-v2",
            evaluation_at_utc="2026-07-29T16:00:00Z",
        )
        assert rkpk.station_id == "RKPK"
        assert rkpk.artifact_id == "delta-v2-policy-rkpk-summer-liquid-v3-d2"
        assert eddm.station_id == "*"


def test_bootstrap_failure_rolls_back_schema_and_authority_rows(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _empty_db(db)
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    manifest["fill_prior"]["fok_distribution"][
        "fill_probability_zero"
    ] = 0.5
    manifest["fill_prior"]["fok_distribution"][
        "fill_probability_full"
    ] = 0.5
    candidate = tmp_path / "bad-fill-manifest.json"
    candidate.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ContractError, match="fok_zero_forbidden"):
        apply_bootstrap(
            db_path=db,
            manifest_path=candidate,
            confirmed_writer_stopped=False,
        )
    with sqlite3.connect(db) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'basket_delta_v2_%'"
        ).fetchall()
        assert tables == []
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_target_v2_atomically_retires_only_known_target_v1_activations(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _empty_db(db)
    apply_bootstrap(
        db_path=db,
        manifest_path=DEFAULT_MANIFEST,
        confirmed_writer_stopped=False,
    )
    upgraded = apply_bootstrap(
        db_path=db,
        manifest_path=(
            ROOT
            / "config/trading/"
            "delta_basket_score_v2_rkpk_no_live_bootstrap_v2.json"
        ),
        confirmed_writer_stopped=False,
    )
    replay = apply_bootstrap(
        db_path=db,
        manifest_path=(
            ROOT
            / "config/trading/"
            "delta_basket_score_v2_rkpk_no_live_bootstrap_v2.json"
        ),
        confirmed_writer_stopped=False,
    )
    assert upgraded["superseded_activation_count"] == 2
    assert upgraded["rows_written"] == 599
    assert upgraded["maturity_bins"] == ["d1", "d2", "d2_fallback"]
    assert upgraded["state"] == "COMMITTED"
    assert replay["superseded_activation_count"] == 0
    assert replay["rows_written"] == 0
    assert replay["inactive_legacy_activation_count"] == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT maturity_bin,strategy_version,count(*) "
            "FROM basket_delta_v2_policy_activation "
            "WHERE strategy_version='target-v2' "
            "GROUP BY maturity_bin,strategy_version ORDER BY 1,2"
        ).fetchall() == [
            ("d1", "target-v2", 1),
            ("d2", "target-v2", 1),
            ("d2_fallback", "target-v2", 1),
        ]
        native_d2_policy = resolve_delta_v2_policy(
            conn,
            station_id="RKPK",
            stage="P0",
            season_bin="summer",
            maturity_bin="d2",
            liquidity_bin="liquid",
            strategy_version="target-v2",
            evaluation_at_utc="2026-07-28T18:00:00Z",
        )
        assert native_d2_policy.artifact_id == (
            "delta-v2-policy-rkpk-summer-liquid-v2-d2"
        )
        assert native_d2_policy.maturity_bin == "d2"
        assert native_d2_policy.strategy_version == "target-v2"
        assert native_d2_policy.oos_gate_passed is False
        assert conn.execute(
            "SELECT count(*) FROM basket_delta_v2_policy_activation "
            "WHERE strategy_version='target-v2' AND maturity_bin='d2'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT p.decision FROM basket_delta_v2_policy_promotion p "
            "WHERE p.artifact_id LIKE "
            "'delta-v2-policy-rkpk-summer-liquid-v1-%' "
            "AND p.decision_seq=("
            "SELECT max(q.decision_seq) "
            "FROM basket_delta_v2_policy_promotion q "
            "WHERE q.artifact_id=p.artifact_id) "
            "ORDER BY p.artifact_id"
        ).fetchall() == [("SUPERSEDED",), ("SUPERSEDED",)]
        # Immutable v1 artifacts remain as minimal audit lineage; only their
        # append-only activations remain for lineage and are made
        # unresolvable by a terminal SUPERSEDED promotion.
        assert conn.execute(
            "SELECT count(*) FROM basket_delta_v2_policy_artifact "
            "WHERE strategy_version='target-v1'"
        ).fetchone()[0] == 2


def test_target_v2_generation_two_covers_official_mill_tick_without_rounding(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _empty_db(db)
    v2_manifest = (
        ROOT
        / "config/trading/"
        "delta_basket_score_v2_rkpk_no_live_bootstrap_v2.json"
    )
    v3_manifest = (
        ROOT
        / "config/trading/"
        "delta_basket_score_v2_rkpk_no_live_bootstrap_v3.json"
    )
    apply_bootstrap(
        db_path=db,
        manifest_path=v2_manifest,
        confirmed_writer_stopped=False,
    )
    upgraded = apply_bootstrap(
        db_path=db,
        manifest_path=v3_manifest,
        confirmed_writer_stopped=False,
    )
    replay = apply_bootstrap(
        db_path=db,
        manifest_path=v3_manifest,
        confirmed_writer_stopped=False,
    )

    assert upgraded["state"] == "COMMITTED"
    assert upgraded["fill_artifact_count"] == 2_997
    assert upgraded["superseded_activation_count"] == 3
    assert replay["state"] == "UNCHANGED_ZERO_WRITE"
    assert replay["rows_written"] == 0
    with sqlite3.connect(db) as conn:
        policy = resolve_delta_v2_policy(
            conn,
            station_id="RKPK",
            stage="P1",
            season_bin="summer",
            maturity_bin="d2",
            liquidity_bin="liquid",
            strategy_version="target-v2",
            evaluation_at_utc="2026-07-29T01:16:00Z",
        )
        assert policy.artifact_id == (
            "delta-v2-policy-rkpk-summer-liquid-v3-d2"
        )
        exact = resolve_delta_v2_fill_distribution(
            conn,
            station_id="RKPK",
            stage="P1",
            season_bin="summer",
            maturity_bin="d2",
            liquidity_bin="liquid",
            bucket="40°C",
            price=0.004,
            timeout_seconds=60.0,
            queue_ahead_shares=97.95,
            spread=0.003,
            depth=565.0,
            quote_version="official-ws-test",
            evaluation_at_utc="2026-07-29T01:16:00Z",
            policy_fill_distribution_artifact_sha256=(
                policy.fill_distribution_artifact_sha256
            ),
        )
        assert exact.artifact_id.endswith("-p004of1000")
        with pytest.raises(ContractError, match="activation_scope_miss"):
            resolve_delta_v2_fill_distribution(
                conn,
                station_id="RKPK",
                stage="P1",
                season_bin="summer",
                maturity_bin="d2",
                liquidity_bin="liquid",
                bucket="40°C",
                price=0.0045,
                timeout_seconds=60.0,
                queue_ahead_shares=97.95,
                spread=0.003,
                depth=565.0,
                quote_version="official-ws-test",
                evaluation_at_utc="2026-07-29T01:16:00Z",
                policy_fill_distribution_artifact_sha256=(
                    policy.fill_distribution_artifact_sha256
                ),
            )
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
