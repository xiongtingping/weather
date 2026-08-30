"""Hosted sweeps may overlap, but never two writers on one database.

The sweeps were taken off launchd on 2026-08-24 because four ledger rounds
ran at once, three held the SSOT write lock, and the resident's
``BEGIN IMMEDIATE`` queued 22s behind them -- P1 fail-closed for 45 minutes.
Hosting them fixed that by serving one at a time, which then made the lane the
bottleneck: twelve members come due at about seven per ten minutes and one was
served per turn, so a 600s member waited a measured 43 minutes at the median.

Concurrency here is granted by write domain, so the invariant that outage
taught -- one routine writer per database -- is what bounds it, not a count.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _daemon():
    spec = importlib.util.spec_from_file_location(
        "weather_basket_yes_daemon",
        ROOT / "scripts" / "weather_basket_yes_daemon.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def daemon():
    return _daemon()


def _due(*names, module):
    """Build the scheduler's ``(overdue, index, name, script, timeout)`` rows.

    Overdue descends with position so the first name given is the most
    overdue, which is the order the scheduler ranks on.
    """

    index_of = {
        name: index
        for index, (name, _, _, _) in enumerate(module.HOSTED_SWEEPS)
    }
    return [
        (600.0 - 10.0 * offset, index_of[name], name, f"scripts/{name}.sh", 900.0)
        for offset, name in enumerate(names)
    ]


def test_every_sweep_declares_a_write_domain(daemon) -> None:
    declared = set(daemon.HOSTED_SWEEP_WRITE_DOMAIN)
    listed = {name for name, _, _, _ in daemon.HOSTED_SWEEPS}
    assert declared == listed
    assert set(daemon.HOSTED_SWEEP_WRITE_DOMAIN.values()) <= {
        "ssot", "archive", "none",
    }


def test_an_undeclared_sweep_is_assumed_to_write_the_ssot(daemon) -> None:
    # Fail safe: a member added without a decision loses throughput, never
    # the invariant.
    assert daemon.hosted_sweep_write_domain("a_sweep_added_tomorrow") == "ssot"


def test_two_ssot_writers_never_start_together(daemon) -> None:
    admitted = daemon.admissible_hosted_sweeps(
        _due("maker_shadow_probe", "paper_trading_ledger", module=daemon),
        running_names=[],
    )
    assert [item[2] for item in admitted] == ["maker_shadow_probe"]


def test_disjoint_domains_run_together(daemon) -> None:
    admitted = daemon.admissible_hosted_sweeps(
        _due(
            "maker_shadow_probe",            # ssot
            "correction_review_auto_clear",  # archive
            "forward_packet_retention",      # no database
            module=daemon,
        ),
        running_names=[],
    )
    assert [item[2] for item in admitted] == [
        "maker_shadow_probe",
        "correction_review_auto_clear",
        "forward_packet_retention",
    ]


def test_a_running_sweep_reserves_its_database(daemon) -> None:
    admitted = daemon.admissible_hosted_sweeps(
        _due(
            "maker_shadow_probe",            # ssot -- taken
            "correction_review_auto_clear",  # archive -- free
            module=daemon,
        ),
        running_names=["paper_trading_ledger"],
    )
    assert [item[2] for item in admitted] == ["correction_review_auto_clear"]


def test_most_overdue_still_wins_its_domain(daemon) -> None:
    # Ordering is unchanged: the ledger is listed earlier but the probe is
    # more overdue here, so the probe takes the SSOT slot.
    admitted = daemon.admissible_hosted_sweeps(
        _due("settlement_truth", "maker_shadow_probe", module=daemon),
        running_names=[],
    )
    assert [item[2] for item in admitted] == ["settlement_truth"]


def test_ties_fall_back_to_list_order(daemon) -> None:
    index_of = {
        name: index
        for index, (name, _, _, _) in enumerate(daemon.HOSTED_SWEEPS)
    }
    tied = [
        (300.0, index_of[name], name, "s.sh", 900.0)
        for name in ("settlement_truth", "maker_shadow_probe")
    ]
    admitted = daemon.admissible_hosted_sweeps(tied, running_names=[])
    # maker_shadow_probe sits at index 0, so it wins an equal overdue.
    assert [item[2] for item in admitted] == ["maker_shadow_probe"]


def test_total_concurrency_is_capped(daemon) -> None:
    admitted = daemon.admissible_hosted_sweeps(
        _due(
            "maker_shadow_probe",
            "correction_review_auto_clear",
            "forward_packet_retention",
            module=daemon,
        ),
        running_names=[],
        max_concurrency=2,
    )
    assert len(admitted) == 2


def test_running_sweeps_count_against_the_cap(daemon) -> None:
    admitted = daemon.admissible_hosted_sweeps(
        _due("correction_review_auto_clear", module=daemon),
        running_names=["paper_trading_ledger", "forward_packet_retention"],
        max_concurrency=2,
    )
    assert admitted == []


def test_a_generator_of_running_names_is_not_consumed_twice(daemon) -> None:
    # The scheduler passes a live dict view; a generator must behave the same
    # rather than reading as empty on the second use.
    admitted = daemon.admissible_hosted_sweeps(
        _due("maker_shadow_probe", module=daemon),
        running_names=(name for name in ["paper_trading_ledger"]),
    )
    assert admitted == []


def test_nothing_due_admits_nothing(daemon) -> None:
    assert daemon.admissible_hosted_sweeps([], running_names=[]) == []
