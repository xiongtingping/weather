"""The delta-v2 execution authority never simply ends.

On 2026-09-17 the served fill batch had 4.5 days left and the policy
activations 10.6 and 14.1, all minted with thirty days by scripts nothing
re-ran on a clock -- and either end refuses every candidate production
prices.  These pin the three rules that replaced the cliff: renew the same
content before the end, renew a policy whose pin is current, and hand a refit
off to the policies at one instant.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from bayes_basket_target import delta_v2_authority_clock as clock  # noqa: E402
from bayes_basket_target.basket_kernel.contracts import (  # noqa: E402
    ContractError,
    content_sha256,
)
from bayes_basket_target.delta_v2_artifact import (  # noqa: E402
    ACTIVATION_SCHEMA,
    ARTIFACT_SCHEMA,
    PROMOTION_SCHEMA,
    commit_delta_v2_authority,
    resolve_delta_v2_policy,
)
from bayes_basket_target.delta_v2_fill_artifact import (  # noqa: E402
    _BATCH_FINGERPRINT_CACHE,
    _fill_batch_fingerprint,
    resolve_delta_v2_fill_distribution,
)
from bayes_basket_target.ssot import apply_delta_v2_authority_schema  # noqa: E402
import fit_delta_v2_fill_from_maker_shadow as fitter  # noqa: E402

T0 = datetime(2026, 8, 22, 4, 37, 38, tzinfo=timezone.utc)
SCOPE = dict(
    station_id="*", stage="*", season_bin="summer", maturity_bin="d1",
    liquidity_bin="liquid",
)


def setup_function(_fn):
    _BATCH_FINGERPRINT_CACHE.clear()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_delta_v2_authority_schema(conn, commit=True)
    return conn


def _fitobj(bid_rate: float, ask_rate: float) -> dict:
    band = (0.49, 0.51)

    def cell(rate):
        return {
            "rate": rate, "sample_n": 500, "holdout_n": 100,
            "holdout_rate": rate, "absolute_error": 0.0,
            "baseline_absolute_error_v1": 0.05, "beats_every_baseline": True,
        }

    return {
        "train_days": ["2026-08-17", "2026-08-20"],
        "holdout_days": ["2026-08-21", "2026-08-22"],
        "cells": {
            (band, 60, ("bid", -0.5, 0.5)): cell(bid_rate),
            (band, 60, ("ask", 50.0, 100000.0)): cell(ask_rate),
        },
    }


def _mint(conn, *, now, bid_rate=0.12, ask_rate=0.88, days=None, starts_at=None):
    artifacts = fitter.mint(
        _fitobj(bid_rate, ask_rate), now=now, starts_at=starts_at
    )
    if days is not None:
        # The thirty-day vintage the cliff came from.
        for row in artifacts:
            row.pop("content_sha256")
            row["valid_until_utc"] = clock.utc_text(now + timedelta(days=days))
            row["content_sha256"] = content_sha256(row)
    clock.write_fill_batch(conn, artifacts, promoted_at=clock.utc_text(now))
    return artifacts


def _fingerprint(conn, valid_from):
    return _fill_batch_fingerprint(
        conn, **SCOPE, valid_from_utc=valid_from,
        authority_scope="PRODUCTION_NO_LIVE",
    )


def _policy(conn, *, pin, now, days):
    artifact = {
        "schema": ARTIFACT_SCHEMA,
        "artifact_id": "delta-v2-global-d1",
        **SCOPE,
        "strategy_version": "target-v2",
        "value_weights": {
            "lambda_profit_floor": 0.5, "lambda_cvar": 0.5,
            "lambda_shortfall": 0.1, "lambda_breakthrough": 0.1,
            "lambda_overfill": 0.2, "lambda_excess_hhi": 0.2,
            "cvar_alpha": 0.1, "hhi_max": 0.5, "expected_pnl_scale": 1.0,
            "profit_floor_scale": 1.0, "cvar_scale": 1.0,
            "coverage_scale": 1.0, "overfill_scale": 1.0,
            "excess_hhi_scale": 1.0,
        },
        "execution_weights": {
            "epsilon_score": 0.001, "lambda_wait": 0.1, "lambda_queue": 0.1,
            "lambda_adverse_selection": 0.1, "lambda_reprice": 0.1,
            "lambda_capital_lock": 0.1, "wait_scale": 1.0,
            "queue_scale": 1.0, "adverse_selection_scale": 1.0,
            "reprice_scale": 1.0, "capital_lock_scale": 1.0,
            "maximum_candidates": 16,
        },
        "cash_clip_grid": [0.5, 1.0],
        "price_grid": [0.5],
        "timeout_grid_seconds": [3600.0],
        "fill_distribution_artifact_sha256": pin,
        "training_evidence_sha256": "2" * 64,
        "train_start_date": "2026-05-01",
        "train_end_date": "2026-05-31",
        "holdout_start_date": "2026-06-01",
        "holdout_end_date": "2026-06-30",
        "sample_n": 0, "holdout_n": 0, "consecutive_forward_windows": 0,
        "compared_baselines": [], "oos_metrics": {},
        "oos_gate_version": "delta-v2-forward-oos-v1",
        "oos_gate_passed": False,
        "trained_through_utc": clock.utc_text(now),
        "valid_from_utc": clock.utc_text(now + timedelta(minutes=1)),
        "valid_until_utc": clock.utc_text(now + timedelta(days=days)),
        "authority_scope": "PRODUCTION_NO_LIVE",
    }
    artifact["content_sha256"] = content_sha256(artifact)
    promotion = {
        "schema": PROMOTION_SCHEMA,
        "promotion_id": "promotion-" + artifact["artifact_id"],
        "artifact_id": artifact["artifact_id"], "decision_seq": 1,
        "decision": "NO_LIVE_ACTIVATED",
        "gate_version": artifact["oos_gate_version"],
        "decided_at_utc": clock.utc_text(now), "receipt_sha256": "3" * 64,
    }
    promotion["content_sha256"] = content_sha256(promotion)
    activation = {
        "schema": ACTIVATION_SCHEMA,
        "activation_id": "activation-" + artifact["artifact_id"],
        "artifact_id": artifact["artifact_id"], **SCOPE,
        "strategy_version": "target-v2", "generation": 1,
        "valid_from_utc": artifact["valid_from_utc"],
        "valid_until_utc": artifact["valid_until_utc"],
        "activated_at_utc": clock.utc_text(now),
        "authority_scope": "PRODUCTION_NO_LIVE",
    }
    activation["content_sha256"] = content_sha256(activation)
    commit_delta_v2_authority(
        conn, artifact=artifact, promotion=promotion, activation=activation
    )
    conn.commit()


def _resolve_policy(conn, at):
    return resolve_delta_v2_policy(
        conn, **SCOPE, strategy_version="target-v2",
        evaluation_at_utc=clock.utc_text(at),
    )


def _resolve_fill(conn, at, pin):
    return resolve_delta_v2_fill_distribution(
        conn, **{k: SCOPE[k] for k in (
            "station_id", "season_bin", "maturity_bin", "liquidity_bin")},
        stage="P0", bucket="31°C", price=0.5, timeout_seconds=3600.0,
        queue_ahead_shares=0.0, spread=0.02, depth=10.0, quote_version="q1",
        evaluation_at_utc=clock.utc_text(at), rung_offset_ticks=0.0,
        policy_fill_distribution_artifact_sha256=pin,
    )


def _served_book(days=30):
    conn = _db()
    artifacts = _mint(conn, now=T0, days=days)
    batch_from = artifacts[0]["valid_from_utc"]
    _policy(conn, pin=_fingerprint(conn, batch_from), now=T0, days=days)
    return conn


def test_the_thirty_day_vintage_is_the_cliff_this_replaces():
    conn = _served_book()
    after = T0 + timedelta(days=31)
    with pytest.raises(ContractError):
        _resolve_policy(conn, after)
    pin = _resolve_policy(conn, T0 + timedelta(days=1)).fill_distribution_artifact_sha256
    with pytest.raises(ContractError, match="not_active"):
        _resolve_fill(conn, after, pin)


def test_renewal_keeps_the_numbers_and_the_pin_across_the_old_end():
    conn = _served_book()
    now = T0 + timedelta(days=26)
    old_pin = _resolve_policy(conn, now).fill_distribution_artifact_sha256
    before = _resolve_fill(conn, now, old_pin)

    fill = clock.renew_expiring_fill_batches(conn, now=now, commit=True, log=lambda _: None)
    policy = clock.repin_policies(conn, now=now, commit=True, log=lambda _: None)
    assert fill["renewed"] == 1 and fill["rows_written"] > 0
    assert policy == {"minted": 1, "skipped": 0, "renewed": 1, "repinned": 0}

    later = T0 + timedelta(days=200)
    served = _resolve_policy(conn, later)
    # Same pin: the renewed batch carries exactly the hashes it copied.
    assert served.fill_distribution_artifact_sha256 == old_pin
    after = _resolve_fill(conn, later, served.fill_distribution_artifact_sha256)
    assert after.artifact_id.startswith(clock.RENEWAL_ID_PREFIX)
    assert after.to_kernel_fill_distribution("MAKER") == before.to_kernel_fill_distribution("MAKER")
    assert after.trained_through_utc == before.trained_through_utc
    assert after.valid_until_utc == clock.utc_text(now + timedelta(days=clock.VALIDITY_DAYS))


def test_nothing_is_renewed_far_from_its_end_and_a_second_pass_is_a_no_op():
    conn = _served_book(days=365)
    now = T0 + timedelta(days=1)
    assert clock.renew_expiring_fill_batches(conn, now=now, commit=True, log=lambda _: None)["renewed"] == 0
    assert clock.repin_policies(conn, now=now, commit=True, log=lambda _: None)["minted"] == 0

    conn = _served_book()
    now = T0 + timedelta(days=26)
    clock.renew_expiring_fill_batches(conn, now=now, commit=True, log=lambda _: None)
    clock.repin_policies(conn, now=now, commit=True, log=lambda _: None)
    # The renewed batch has not started yet; the pass must not stack another.
    again = clock.renew_expiring_fill_batches(conn, now=now + timedelta(minutes=5), commit=True, log=lambda _: None)
    assert again["renewed"] == 0
    assert clock.repin_policies(conn, now=now + timedelta(hours=1), commit=True, log=lambda _: None)["minted"] == 0


def test_a_refit_hands_off_the_batch_and_the_pin_at_one_instant():
    conn = _served_book(days=365)
    now = T0 + timedelta(days=7)
    handoff = now + timedelta(minutes=clock.HANDOFF_LEAD_MINUTES)
    _mint(conn, now=now, bid_rate=0.20, ask_rate=0.70, starts_at=handoff)
    receipt = clock.repin_policies(
        conn, now=now, commit=True, handoff_at=handoff, log=lambda _: None,
    )
    assert receipt["repinned"] == 1

    just_before = handoff - timedelta(seconds=1)
    old = _resolve_policy(conn, just_before)
    new = _resolve_policy(conn, handoff)
    assert old.fill_distribution_artifact_sha256 != new.fill_distribution_artifact_sha256
    # Each side of the instant resolves its own batch under its own pin.
    assert _resolve_fill(conn, just_before, old.fill_distribution_artifact_sha256).trained_through_utc == clock.utc_text(T0)
    assert _resolve_fill(conn, handoff, new.fill_distribution_artifact_sha256).trained_through_utc == clock.utc_text(now)
    with pytest.raises(ContractError, match="binding_mismatch"):
        _resolve_fill(conn, handoff, old.fill_distribution_artifact_sha256)


def test_the_refit_clock_reads_the_newest_batch_start():
    conn = _served_book(days=365)
    start = clock.newest_fill_batch_start(conn, now=T0 + timedelta(days=3))
    assert start == clock.utc_text(T0 + timedelta(minutes=1))
    handoff = T0 + timedelta(days=8)
    _mint(conn, now=T0 + timedelta(days=8) - timedelta(minutes=30), starts_at=handoff, bid_rate=0.3)
    assert clock.newest_fill_batch_start(conn, now=T0 + timedelta(days=8)) == clock.utc_text(handoff)


def test_the_board_goes_red_before_the_end_and_green_after_renewal():
    conn = _served_book()
    now = T0 + timedelta(days=26)
    health = clock.authority_health(conn, now=now)
    assert health["health"] == "red"
    assert health["fill_days_left"] == pytest.approx(4.0, abs=0.01)
    assert "到期" in health["note"]

    clock.renew_expiring_fill_batches(conn, now=now, commit=True, log=lambda _: None)
    clock.repin_policies(conn, now=now, commit=True, log=lambda _: None)
    # Before the renewal starts the old batch still serves, and the scope is
    # already covered by the one that follows it.
    assert clock.authority_health(conn, now=now)["health"] == "green"
    later = now + timedelta(hours=1)
    health = clock.authority_health(conn, now=later)
    assert health["health"] == "green", health
    assert health["fill_days_left"] > 360 and health["policy_days_left"] > 360

    # No refit clock (the maker shadow book that fed it was retired on
    # 2026-10-02): a renewed batch weeks old reads green, not amber.
    later_still = clock.authority_health(conn, now=now + timedelta(days=15))
    assert later_still["health"] == "green" and "没有新批次" not in later_still["note"]


def _shadow_book(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE maker_shadow_order (limit_price REAL, horizon_minutes"
        " INTEGER, state TEXT, placed_at_utc TEXT, expires_at_utc TEXT,"
        " price_rule TEXT, reference_bid REAL, reference_tick REAL)"
    )
    conn.executemany(
        "INSERT INTO maker_shadow_order VALUES (?,?,?,?,?,?,?,?)", rows
    )
    return conn


def test_the_streamed_fit_counts_each_cell_either_side_of_the_day_cut():
    rows = []
    # Five days at bid+1t in one band: day d fills d of ten.  The holdout is
    # the last two days (20% of five rounds to one, floored at two).
    for day in range(1, 6):
        for i in range(10):
            placed = f"2026-09-0{day}T12:00:00Z"
            rows.append((
                0.06, 60, "FILLED" if i < day else "EXPIRED", placed,
                f"2026-09-0{day}T13:00:00Z", "bid+1t", 0.05, 0.01,
            ))
    rows.append((0.06, 60, "OPEN", "2026-09-05T12:00:00Z",
                 "2026-09-05T13:00:00Z", "bid+1t", 0.05, 0.01))
    result = fitter.fit(_shadow_book(rows))
    key = ((0.05, 0.10), 60, ("bid+1t", 0.5, 1.5))
    cell = result["cells"][key]
    assert result["resolved_intents"] == 50
    assert result["train_days"] == ["2026-09-01", "2026-09-02", "2026-09-03"]
    assert result["holdout_days"] == ["2026-09-04", "2026-09-05"]
    assert (cell["sample_n"], cell["holdout_n"]) == (30, 20)
    assert cell["rate"] == pytest.approx(6 / 30)
    assert cell["holdout_rate"] == pytest.approx(9 / 20)

    windowed = fitter.fit(_shadow_book(rows), since_utc="2026-09-02T00:00:00Z")
    assert windowed["resolved_intents"] == 40
    assert windowed["train_days"][0] == "2026-09-02"
