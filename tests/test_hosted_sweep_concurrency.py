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


def test_boot_seed_puts_a_starved_member_first(daemon) -> None:
    """A restart must not reset who is overdue.

    2026-08-30: five deploy restarts each re-ran the boot stagger, and the
    settlement-truth sweep -- fourth in the stagger behind a seventeen-minute
    ledger round -- went 6.8 hours unserved because no generation lived long
    enough for its turn.  Seeded from the persisted completion clock, its
    due-time lands in the past by the true overdue amount and it outranks
    every fresher member at the first slot.
    """

    now_mono = 1000.0
    starved = daemon.seed_hosted_sweep_clock(
        now_mono=now_mono,
        now_wall_utc="2026-08-30T04:50:00Z",
        completed_at_utc="2026-08-29T22:01:24Z",   # 6.8h ago
        interval_sec=1800.0,
        stagger_sec=330.0,
    )
    fresh = daemon.seed_hosted_sweep_clock(
        now_mono=now_mono,
        now_wall_utc="2026-08-30T04:50:00Z",
        completed_at_utc="2026-08-30T04:45:00Z",   # 5 min ago, 300s interval
        interval_sec=300.0,
        stagger_sec=60.0,
    )
    # Both due, but the starved member's overdue (now - due) must dominate.
    assert now_mono - starved > now_mono - fresh
    assert now_mono - starved > 5 * 3600.0


def test_boot_seed_without_a_record_keeps_the_stagger(daemon) -> None:
    assert daemon.seed_hosted_sweep_clock(
        now_mono=1000.0,
        now_wall_utc="2026-08-30T04:50:00Z",
        completed_at_utc=None,
        interval_sec=1800.0,
        stagger_sec=330.0,
    ) == 1330.0


def test_boot_seed_honours_a_genuine_remainder(daemon) -> None:
    # Completed 10 minutes ago on a 30-minute interval: due in 20 minutes,
    # not at boot.
    due = daemon.seed_hosted_sweep_clock(
        now_mono=1000.0,
        now_wall_utc="2026-08-30T04:50:00Z",
        completed_at_utc="2026-08-30T04:40:00Z",
        interval_sec=1800.0,
        stagger_sec=330.0,
    )
    assert due == pytest.approx(1000.0 + 1200.0)


def test_boot_seed_rejects_future_and_garbage_stamps(daemon) -> None:
    for stamp in ("2026-08-30T05:50:00Z", "not-a-time"):
        assert daemon.seed_hosted_sweep_clock(
            now_mono=1000.0,
            now_wall_utc="2026-08-30T04:50:00Z",
            completed_at_utc=stamp,
            interval_sec=1800.0,
            stagger_sec=330.0,
        ) == 1330.0


def test_boot_seed_caps_the_lookback(daemon) -> None:
    # A record from last month must not seed a due-time so deep in the
    # monotonic past that it dwarfs every ranking forever.
    due = daemon.seed_hosted_sweep_clock(
        now_mono=1000.0,
        now_wall_utc="2026-08-30T04:50:00Z",
        completed_at_utc="2026-07-01T00:00:00Z",
        interval_sec=1800.0,
        stagger_sec=330.0,
    )
    assert due >= 1000.0 - 86_400.0


def test_a_short_contract_lane_outranks_a_long_one_it_is_behind_in_seconds(
    daemon,
) -> None:
    """Rank by intervals late, because that is the contract being broken.

    Measured 2026-09-01: ``pr12_awc_observation`` -- whose whole job is to
    canonically identify a METAR so P1 can root on it -- went eight hours
    without completing on a ten-minute clock, while hourly capture lanes that
    were fewer rounds behind ranked ahead of it on raw seconds.  Every P1
    committed in that window is permanently without its truth-revision sha,
    and its station-day is refused by the atomic-fact gate until a newer P1
    carries one.
    """

    intervals = {
        name: interval for name, _script, interval, _timeout
        in daemon.HOSTED_SWEEPS
    }
    now = 10_000_000.0
    behind_seconds = 8 * 3600.0
    # Both exactly eight hours late: the ten-minute lane has missed 48 rounds,
    # the hourly one 8.
    next_due = {
        "pr12_awc_observation": now - behind_seconds,
        "capture_jma_msm_forward": now - behind_seconds,
    }
    due = daemon.due_hosted_sweeps(now_mono=now, next_due=next_due)
    ranked = {name: overdue for overdue, _i, name, _s, _t in due}
    assert ranked["pr12_awc_observation"] == pytest.approx(
        behind_seconds / intervals["pr12_awc_observation"]
    )
    assert ranked["capture_jma_msm_forward"] == pytest.approx(
        behind_seconds / intervals["capture_jma_msm_forward"]
    )
    assert (
        ranked["pr12_awc_observation"] > ranked["capture_jma_msm_forward"]
    )
    # Both write the SSOT, so only one may start -- and it is the identity
    # lane, which under the old seconds rule it would not have been when the
    # capture lane had been idle marginally longer.
    admitted = daemon.admissible_hosted_sweeps(due, running_names=[])
    assert [item[2] for item in admitted] == ["pr12_awc_observation"]


def test_a_member_not_yet_due_is_not_in_the_queue(daemon) -> None:
    now = 10_000_000.0
    due = daemon.due_hosted_sweeps(
        now_mono=now,
        next_due={
            "pr12_awc_observation": now + 1.0,
            "maker_shadow_probe": now - 1.0,
        },
    )
    assert [item[2] for item in due] == ["maker_shadow_probe"]


def test_a_running_member_is_skipped(daemon) -> None:
    now = 10_000_000.0
    due = daemon.due_hosted_sweeps(
        now_mono=now,
        next_due={
            "pr12_awc_observation": now - 3600.0,
            "maker_shadow_probe": now - 3600.0,
        },
        skip_names=["pr12_awc_observation"],
    )
    assert [item[2] for item in due] == ["maker_shadow_probe"]
