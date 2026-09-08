"""A tier may never make a tighter tier late.

That one sentence is what the cycle turns on, and until 2026-09-07 it was
written nowhere.  It was approximated instead, repeatedly, by reordering
stages inside one serial body and tuning constants: settlement moved after
the decision loop, the quote-refresh cap went 44 -> 1 -> 6, the dashboard
render moved to its own process.  Each fixed the case in front of it and
left the rule unstated, so the next stage added inherited nothing -- and
the cycle reached 4284.6s against a 60s quote lease with 86 of 90 scopes
dying on books that went stale while it was busy.

These tests make the rule checkable:

  * every stage the cycle times has a declared tier, and an undeclared one
    is charged to R0 -- the safe default, and a visible one;
  * every deferrable stage actually has a gate at its call site, so the
    table cannot claim a budget nobody spends;
  * the budget is the R0 deadline itself, read from the activation rather
    than typed twice.
"""

from __future__ import annotations

import inspect
import re
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.production_worker import (
    MAINTENANCE_BUDGET_SEC,
    MAINTENANCE_STAGES,
    STAGE_TIER,
    STAGE_TIER_R0_DECISION,
    STAGE_TIER_R2_PERIODIC,
    TargetProductionWorker,
    classify_scope_refusal,
    split_refusals_by_capability,
)

SOURCE = inspect.getsource(TargetProductionWorker._run_stages)
MARKED = set(re.findall(r'self\._mark\("([a-z0-9_]+)"\)', SOURCE)) | set(
    re.findall(
        r'self\._mark\("([a-z0-9_]+)"\)',
        inspect.getsource(TargetProductionWorker._materialize_resident_probability_chain),
    )
)


def _worker(tmp_path: Path) -> TargetProductionWorker:
    db = tmp_path / "target-read.db"
    if not db.exists():
        with sqlite3.connect(db) as source:
            source.execute("CREATE TABLE marker(value TEXT)")
    runtime = tmp_path / "nwp"
    runtime.mkdir(exist_ok=True)
    return TargetProductionWorker(db, commit_conn=None, nwp_runtime_root=runtime)


def test_every_timed_stage_declares_a_tier() -> None:
    """An unclassified stage delays a decision until someone classifies it.

    Marked stages are the cycle's own timeline, so this is the complete
    list by construction -- a stage added without a tier fails here rather
    than quietly joining the decision path.
    """

    missing = sorted(name for name in MARKED if name not in STAGE_TIER)
    assert not missing, f"stages with no declared deadline tier: {missing}"


#: A stage whose mark and whose gate carry different names.  Two of them
#: exist and both are deliberate: the settlement mark times a lane whose
#: budget is taken under the name of the work inside it, and the OOS
#: candidate scan is admitted by the sweep's gate because scanning for
#: candidates it cannot afford to sweep is the waste, not the scan.
GATE_ALIASES = {
    "settlement_lane": "delta_v2_settlement",
    "oos_candidate_scan": "native_ensemble_oos",
}


def test_every_deferrable_stage_is_actually_gated() -> None:
    """The table must not claim a budget nobody spends.

    A stage declared R2 with no gate is worse than one never declared: the
    receipt reports a deferrable tier while the cycle pays for it in full,
    which is exactly the gap between what the code said and what it did
    before any of this existed.
    """

    for stage, tier in sorted(STAGE_TIER.items()):
        if tier < STAGE_TIER_R2_PERIODIC or stage not in MARKED:
            continue
        gate = GATE_ALIASES.get(stage, stage)
        assert gate in MAINTENANCE_STAGES, (
            f"{stage} is declared R{tier} but nothing gates it"
        )
        assert f'self._maintenance_admits("{gate}")' in SOURCE, (
            f"{stage} claims tier R{tier} with no admission call"
        )


def test_every_gated_stage_is_declared_deferrable() -> None:
    """And the reverse: a gate on a stage the table calls R0 would defer
    part of the decision path, which is the one thing the rule forbids."""

    for stage in MAINTENANCE_STAGES:
        assert STAGE_TIER[stage] >= STAGE_TIER_R2_PERIODIC, stage


def test_no_decision_stage_is_deferrable() -> None:
    """R0 is the tier the quote lease is racing; nothing in it may wait."""

    for stage in MAINTENANCE_STAGES:
        assert STAGE_TIER.get(stage) != STAGE_TIER_R0_DECISION, stage


def test_the_budget_is_the_quote_lease_not_a_second_copy_of_it(
    tmp_path: Path,
) -> None:
    """Two copies of one number drift, and the copy in the activation is
    the one production enforces."""

    worker = _worker(tmp_path)
    assert worker._maintenance_budget() == MAINTENANCE_BUDGET_SEC

    worker._record_quote_lease_seconds(
        {"quote_freshness_policy": {"max_quote_age_seconds": 45.0}}
    )
    assert worker._maintenance_budget() == 45.0

    # An unreadable policy falls back to deferring more, not less.
    worker._record_quote_lease_seconds({"quote_freshness_policy": None})
    assert worker._maintenance_budget() == MAINTENANCE_BUDGET_SEC


def test_the_receipt_charges_every_tier_and_names_the_unclassified(
    tmp_path: Path,
) -> None:
    """R0's total is what the lease is racing; every tier below it is a
    claim on that total, and a stage nobody classified is charged to R0."""

    worker = _worker(tmp_path)
    worker._maintenance_spent_sec = 0.0
    worker._maintenance_admissions = {}
    worker._stage_timings_ms = {
        "decision_loop": 120_000.0,
        "market_ingress": 30_000.0,
        "truth_increment": 40_000.0,
        "forward_p0_loop": 10_000.0,
        "a_stage_nobody_classified": 5_000.0,
        "cycle_total": 205_000.0,
        "unattributed": 0.0,
    }
    report = worker._tier_spend_report()
    assert report["seconds_by_tier"]["R0"] == 155.0
    assert report["seconds_by_tier"]["R1"] == 10.0
    assert report["seconds_by_tier"]["R2"] == 40.0
    assert report["unclassified_stages"] == ["a_stage_nobody_classified"]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("production_quote_expired", "incapacity"),
        ("resident_p0m_replay_required", "incapacity"),
        ("latest_stage_stale_fail_closed", "incapacity"),
        ("OperationalError:database is locked", "incapacity"),
        ("delta_v2_support_empty", "verdict"),
        ("p1_entry_window_not_open", "verdict"),
        ("", "verdict"),
    ],
)
def test_a_refusal_says_whether_we_declined_or_missed(
    code: str, expected: str
) -> None:
    """Rerun an incapacity with nothing about the market changed and it
    should go through.  Rerun a verdict and nothing changes.  One of those
    belongs in an SLO and the other in a business report."""

    assert classify_scope_refusal(code) == expected


def test_the_split_separates_the_slo_number_from_the_business_one() -> None:
    """2026-09-07's actual cycle: "90 candidates, 88 blocked" sounds like a
    fleet with nothing to trade.  83 of the 88 were books that went stale
    while the cycle was busy."""

    split = split_refusals_by_capability({
        "production_quote_expired": 83,
        "resident_p0m_replay_required": 5,
        "delta_v2_support_empty": 11,
        "p1_entry_window_not_open": 4,
    })
    assert split["incapacity_count"] == 88
    assert split["verdict_count"] == 15
    assert split["incapacity_share"] == pytest.approx(0.8544, abs=1e-4)
    assert "production_quote_expired" in split["incapacity_reasons"]
    assert "delta_v2_support_empty" in split["verdict_reasons"]


def test_the_cycle_publishes_the_split() -> None:
    """Counted apart and reported apart, or the split changes nothing."""

    assert "split_refusals_by_capability(" in SOURCE


def test_the_reconcile_mark_is_split_into_its_four_lanes() -> None:
    """One mark over four lanes is how the largest stage in the cycle went
    unexplained.

    ``intent_reconcile`` times four separate lanes -- reconciling intents
    the exchange has answered, repricing the resting ladder, retiring
    stranded intents, settling live cycles -- and had no span inside any of
    them.  Measured 2026-09-07 it was 139.1s of a 400.8s cycle, and the
    profile could not say which lane that was.  It was one of them, at
    112.1s.
    """

    for span in (
        "reconcile_unresolved_intents",
        "reprice_resting_rungs",
        "retire_stranded_intents",
        "settle_live_cycles",
    ):
        assert f'self._span("{span}")' in SOURCE, span
