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
        # A domain is a file the sweeps of that name serialise against.
        # ``paper_ledger`` (charter 155) and ``maker_shadow`` (charter 169)
        # are books that left the SSOT: each is its own file, so a sweep
        # writing one excludes no SSOT writer and costs production no turn.
        "ssot", "archive", "none", "paper_ledger", "maker_shadow",
        # Charter 199: the fusion blend's training facts, its own file.
        "fusion_blend",
    }


def test_an_undeclared_sweep_is_assumed_to_write_the_ssot(daemon) -> None:
    # Fail safe: a member added without a decision loses throughput, never
    # the invariant.
    assert daemon.hosted_sweep_write_domain("a_sweep_added_tomorrow") == "ssot"


def test_two_ssot_writers_never_start_together(daemon) -> None:
    admitted = daemon.admissible_hosted_sweeps(
        _due("pr12_awc_observation", "settlement_truth", module=daemon),
        running_names=[],
    )
    assert [item[2] for item in admitted] == ["pr12_awc_observation"]


def test_disjoint_domains_run_together(daemon) -> None:
    admitted = daemon.admissible_hosted_sweeps(
        _due(
            "pr12_awc_observation",            # ssot
            "correction_review_auto_clear",  # archive
            "forward_packet_retention",      # no database
            module=daemon,
        ),
        running_names=[],
    )
    assert [item[2] for item in admitted] == [
        "pr12_awc_observation",
        "correction_review_auto_clear",
        "forward_packet_retention",
    ]


def test_a_running_sweep_reserves_its_database(daemon) -> None:
    admitted = daemon.admissible_hosted_sweeps(
        _due(
            "pr12_awc_observation",            # ssot -- taken
            "correction_review_auto_clear",  # archive -- free
            module=daemon,
        ),
        running_names=["settlement_truth"],
    )
    assert [item[2] for item in admitted] == ["correction_review_auto_clear"]


def test_most_overdue_still_wins_its_domain(daemon) -> None:
    # Ordering is unchanged: the ledger is listed earlier but the probe is
    # more overdue here, so the probe takes the SSOT slot.
    admitted = daemon.admissible_hosted_sweeps(
        _due("settlement_truth", "pr12_awc_observation", module=daemon),
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
        for name in ("settlement_truth", "pr12_awc_observation")
    ]
    admitted = daemon.admissible_hosted_sweeps(tied, running_names=[])
    # pr12_awc_observation sits at index 0, so it wins an equal overdue.
    assert [item[2] for item in admitted] == ["pr12_awc_observation"]


def test_total_concurrency_is_capped(daemon) -> None:
    admitted = daemon.admissible_hosted_sweeps(
        _due(
            "pr12_awc_observation",
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
        _due("pr12_awc_observation", module=daemon),
        running_names=(name for name in ["settlement_truth"]),
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

    Measured 2026-09-01: ``pr12_awc_observation`` -- then a ten-minute lane --
    went eight hours without completing while hourly capture lanes that were
    fewer rounds behind ranked ahead of it on raw seconds.  Every P1 committed
    in that window is permanently without its truth-revision sha.  (That lane
    now runs inside the production cycle, which is the other half of the fix;
    the ordering rule stands for whoever is shortest-contract next.)
    """

    intervals = {
        name: interval for name, _script, interval, _timeout
        in daemon.HOSTED_SWEEPS
    }
    short, long_ = "settled_day_cycle_close", "calibration_refit"
    assert intervals[short] < intervals[long_]
    now = 10_000_000.0
    behind_seconds = 8 * 3600.0
    next_due = {short: now - behind_seconds, long_: now - behind_seconds}
    due = daemon.due_hosted_sweeps(now_mono=now, next_due=next_due)
    ranked = {name: overdue for overdue, _i, name, _s, _t in due}
    assert ranked[short] == pytest.approx(behind_seconds / intervals[short])
    assert ranked[long_] == pytest.approx(behind_seconds / intervals[long_])
    # Equal in seconds, and the one whose contract is more broken wins.
    assert ranked[short] > ranked[long_]
    # Both write the SSOT, so only one may start.
    admitted = daemon.admissible_hosted_sweeps(due, running_names=[])
    assert [item[2] for item in admitted] == [short]


def test_a_member_not_yet_due_is_not_in_the_queue(daemon) -> None:
    now = 10_000_000.0
    due = daemon.due_hosted_sweeps(
        now_mono=now,
        next_due={
            "pr12_awc_observation": now + 1.0,
            "pr12_awc_observation": now - 1.0,
        },
    )
    assert [item[2] for item in due] == ["pr12_awc_observation"]


def test_a_running_member_is_skipped(daemon) -> None:
    now = 10_000_000.0
    due = daemon.due_hosted_sweeps(
        now_mono=now,
        next_due={
            "pr12_awc_observation": now - 3600.0,
            "settlement_truth": now - 3600.0,
        },
        skip_names=["pr12_awc_observation"],
    )
    assert [item[2] for item in due] == ["settlement_truth"]


#: The cycle time this table is sized against.  Comfortably above the 340s
#: steady state measured 2026-09-01 and below the cycle's own 720s overdue
#: bound, so the queue drains at a cycle time the resident is expected to
#: hold rather than at its best one.
SSOT_QUEUE_REFERENCE_CYCLE_SEC = 600.0


def test_the_ssot_sweep_queue_fits_its_drain_rate(daemon) -> None:
    """Demand on the one exclusive writer must not exceed what it drains.

    ``_EXCLUSIVE_WRITE_DOMAINS`` admits one SSOT writer at a time, and the
    production floor admits the sweeps lane at most once between production
    slots -- deliberately, because without it the lane is due at every instant
    and once served sweeps for two straight hours while production got none.
    So supply is one member per production cycle and cannot be raised without
    re-opening that starvation.

    Demand is the sum of 3600/interval over the SSOT members.  Measured
    2026-09-01 it was 18.0 an hour against a supply of 10.6 at the best cycle
    time of the day and 4.2 at the worst: every member permanently late, the
    ordering deciding only who.  That is why this lane kept needing another
    fix -- the ordering was being tuned while the queue was over capacity.

    This is the check that stops it recurring.  Adding a member, or shortening
    an interval, has to be paid for by lengthening another -- or by moving the
    work into the production cycle, which is where anything the live decision
    chain blocks on belongs in the first place.
    """

    supply_per_hour = 3600.0 / SSOT_QUEUE_REFERENCE_CYCLE_SEC
    members = [
        (name, interval)
        for name, _script, interval, _timeout in daemon.HOSTED_SWEEPS
        if daemon.hosted_sweep_write_domain(name) == "ssot"
    ]
    assert members, "the SSOT queue cannot be empty; the domain map is wrong"
    demand_per_hour = sum(3600.0 / interval for _name, interval in members)
    breakdown = "\n".join(
        f"    {name:32s} {interval:8.0f}s  {3600.0 / interval:5.2f}/h"
        for name, interval in sorted(members, key=lambda item: -1 / item[1])
    )
    assert demand_per_hour <= supply_per_hour, (
        f"the SSOT sweep queue asks for {demand_per_hour:.2f} runs an hour and"
        f" drains {supply_per_hour:.2f} at a {SSOT_QUEUE_REFERENCE_CYCLE_SEC:.0f}s"
        f" cycle.\n{breakdown}\n"
        "  Lengthen an interval, or move the work into the production cycle."
    )


def test_nothing_the_live_chain_blocks_on_is_left_to_the_sweep_queue(
    daemon,
) -> None:
    """A lane the live decision refuses without belongs in the cycle.

    The queue is best-effort by construction: one member per production cycle,
    ordered by lateness, with no member guaranteed a turn inside its own
    contract.  Work the chain fail-closes without cannot live there, and the
    2026-09-01 outage is the proof -- pr12_awc_observation went eight hours
    without a turn and every P1 in that window was committed without its
    truth-revision sha, permanently, taking fifteen station-days out of the
    book on target_atomic_lineage_incomplete.

    So the identity lane is a backstop here and the cycle owns it.  This
    names the members that may never again be the *only* producer of
    something the chain reads.
    """

    intervals = {
        name: interval
        for name, _script, interval, _timeout in daemon.HOSTED_SWEEPS
    }
    # Owned by the production cycle; the sweep is the backstop for rounds
    # where the cycle is fail-closed before it reaches the call.
    assert "pr12_awc_observation" in intervals
    assert intervals["pr12_awc_observation"] >= 3600.0, (
        "pr12_awc_observation is a backstop -- the cycle runs it every round."
        "  A short interval here re-enters it into the queue it was moved out"
        " of."
    )
    # The live edge of truth is materialized inside the cycle too
    # (_materialize_truth_increment); this sweep keeps the deep window.
    assert intervals["settlement_truth"] >= 3600.0


def test_the_cycle_declares_a_target_and_not_only_a_failure_line(
    daemon,
) -> None:
    """A bound to alarm at is not the same as a length to aim for.

    Until 2026-09-01 the only number was PRODUCTION_CYCLE_OVERDUE_SEC, so the
    cycle length was emergent -- it re-arms fifteen seconds after it starts
    and takes as long as the work takes -- and a drift from 350s to 850s
    produced no signal until it hit 720.  That drift happened and cost the
    day: at 846s the compile loop outran a sixty-second quote lease and 21 of
    28 scopes died on production_quote_expired.

    The target is not arbitrary.  The SSOT sweep queue drains one member per
    production cycle against a declared demand of 5.71 an hour, so a cycle
    above 630s starves it; the target has to leave room under that.
    """

    assert daemon.PRODUCTION_CYCLE_TARGET_SEC < daemon.PRODUCTION_CYCLE_WARN_SEC
    assert daemon.PRODUCTION_CYCLE_WARN_SEC < daemon.PRODUCTION_CYCLE_OVERDUE_SEC

    # The target must keep the sweep queue solvent with room to spare.
    ssot_demand_per_hour = sum(
        3600.0 / interval
        for name, _script, interval, _timeout in daemon.HOSTED_SWEEPS
        if daemon.hosted_sweep_write_domain(name) == "ssot"
    )
    supply_at_target = 3600.0 / daemon.PRODUCTION_CYCLE_TARGET_SEC
    assert supply_at_target >= 2.0 * ssot_demand_per_hour, (
        f"at the {daemon.PRODUCTION_CYCLE_TARGET_SEC:.0f}s target the queue"
        f" drains {supply_at_target:.1f}/h against demand"
        f" {ssot_demand_per_hour:.2f}/h -- less than the 2x margin a cycle"
        " that sometimes runs long needs"
    )
    # And the warn line must still leave the queue solvent at all.
    assert 3600.0 / daemon.PRODUCTION_CYCLE_WARN_SEC >= ssot_demand_per_hour


def test_a_failing_member_retries_on_its_interval_not_on_every_restart(
    daemon,
) -> None:
    """The 72-hour instance this argument came from (2026-09-09).

    ``capture_noaa_conus_forward`` had not completed since 2026-09-06: every
    attempt ran the full 1,800-second budget, was ``timeout_killed``, and so
    never advanced its completion stamp.  On each restart it was therefore
    maximally overdue, ranked first, and took the exclusive SSOT write domain
    for another half hour -- while six other SSOT members sat 12 to 13 hours
    late on a 3-hour interval (settlement truth, the paper ledger, the maker
    shadow probe among them).

    The completion stamp still tells the truth: it has not succeeded.  The
    ATTEMPT stamp is what the retry cadence reads.
    """
    now = 1000.0
    stale_completion = "2026-09-06T08:12:07Z"
    wall = "2026-09-09T08:30:00Z"

    starved = daemon.seed_hosted_sweep_clock(
        now_mono=now, now_wall_utc=wall,
        completed_at_utc=stale_completion, attempted_at_utc=None,
        interval_sec=10_800.0, stagger_sec=60.0,
    )
    paced = daemon.seed_hosted_sweep_clock(
        now_mono=now, now_wall_utc=wall,
        completed_at_utc=stale_completion,
        attempted_at_utc="2026-09-09T08:28:00Z",
        interval_sec=10_800.0, stagger_sec=60.0,
    )
    # Without the attempt stamp it is due in the past by a full day of
    # overdue credit; with it, it waits out its interval like anyone else.
    assert starved < now
    assert paced > now + 10_000.0

    # A member that genuinely succeeded an interval ago is still due now --
    # the fix must not slow down the healthy case.
    healthy = daemon.seed_hosted_sweep_clock(
        now_mono=now, now_wall_utc=wall,
        completed_at_utc="2026-09-09T05:00:00Z",
        attempted_at_utc="2026-09-09T05:00:00Z",
        interval_sec=10_800.0, stagger_sec=60.0,
    )
    assert healthy < now

    # And a record with no stamps at all keeps the boot stagger.
    assert daemon.seed_hosted_sweep_clock(
        now_mono=now, now_wall_utc=wall, completed_at_utc=None,
        attempted_at_utc=None, interval_sec=10_800.0, stagger_sec=60.0,
    ) == now + 60.0


def test_every_attempt_advances_the_attempt_stamp_and_only_success_the_other(
    daemon,
) -> None:
    """Asked of the entry the loop writes (``hosted_sweep_clock_entry``)."""

    before = {"completed_at_utc": "2026-09-09T00:00:00Z",
              "attempted_at_utc": "2026-09-09T00:00:00Z"}
    killed = daemon.hosted_sweep_clock_entry(
        before, {"state": "timeout_killed", "duration_sec": 1800.0},
        name="capture_noaa_conus_forward", now_utc="2026-09-09T03:00:00Z",
    )
    assert killed["attempted_at_utc"] == "2026-09-09T03:00:00Z"
    assert killed["completed_at_utc"] == "2026-09-09T00:00:00Z"
    assert killed["last_state"] == "timeout_killed"
    ok = daemon.hosted_sweep_clock_entry(
        killed, {"state": "ok", "duration_sec": 900.0},
        name="capture_noaa_conus_forward", now_utc="2026-09-09T04:00:00Z",
    )
    assert ok["attempted_at_utc"] == ok["completed_at_utc"] == "2026-09-09T04:00:00Z"


# Which upstream products each capture lane is responsible for.  Named here
# rather than derived, because the mapping is the claim: a lane's interval is
# honest only against the cadence of the models it actually fetches.
CAPTURE_LANE_PRODUCTS = {
    "capture_noaa_conus_forward": ("HRRR", "NBM"),
    "capture_jma_msm_forward": ("MSM",),
    "capture_dwd_icon_eu_forward": ("ICON_EU",),
    "capture_cma_grapes_forward": ("CMA",),
}


def test_a_capture_interval_is_never_shorter_than_its_upstream_cadence(
    daemon,
) -> None:
    """A pass that runs more often than its model publishes is empty by
    construction, and an empty pass is not free.

    Each one takes the exclusive SSOT write domain for a turn to discover
    there is no new run.  Measured 2026-09-09: MSM and ICON_EU publish eight
    runs a day and GRAPES four, while all three were swept hourly -- 67%,
    67% and 83% of their passes could not have found anything.  Demand on the
    one SSOT writer was 7.75/h against a drain of 6.00, and that standing
    overdraft is what turned one stuck member into a 72-hour outage.

    The bound is the model's own cycle, not a guess: an interval at or under
    it still visits every run, and publication lag (hours, on every product)
    means nothing is missed by arriving late within the cycle.

    The FLOOR was "no faster than the cadence" until 2026-09-16, and the two
    premises it rested on have since gone:

      * "an empty pass takes the exclusive SSOT write domain" -- a capture is
        writer-turn exempt since 2026-09-14, so a running one holds the other
        captures and not the queue.  What an empty pass still costs is an
        admission slot, and that is priced by
        ``test_the_ssot_sweep_queue_fits_its_drain_rate``, not here.
      * "an empty pass is empty by construction" -- ``plan_p1`` refuses an
        already-stored cycle before a single request, so an empty CMA pass is
        a handful of indexed reads.

    And against them stands what equal-to-cadence costs a transport that
    serves only the run it currently holds: exactly ONE attempt per cycle,
    so a pass that is blocked or loses the queue loses that cycle for good.
    ``capture_cma_grapes_forward`` was the measurement.  It ran every 7 to
    19.5 hours against its 6-hour contract over 2026-09-11..16 -- the queue
    ranks by intervals late and it held the longest interval on that queue,
    so twelve minutes past due scored 0.037 where an hourly member scored
    0.25 -- and 2026-09-15 lost a cycle outright: nothing was captured
    between 07:54Z and the next day.

    So the floor is now half the cadence: one spare attempt per cycle, and
    no more.  A lane asking for a third would be buying a second chance it
    has already got, and would owe the drain-rate budget for it.
    """
    from bayes_basket_target.regional_point_lane import RUN_CYCLE_HOURS

    intervals = {
        name: interval for name, _script, interval, _timeout in daemon.HOSTED_SWEEPS
    }
    for lane, products in CAPTURE_LANE_PRODUCTS.items():
        assert lane in intervals, f"{lane} is not a hosted sweep any more"
        # The fastest product decides: the lane has to keep up with whichever
        # of its models issues most often.
        cadence_sec = min(
            (24 / len(RUN_CYCLE_HOURS[product])) * 3600.0
            for product in products
        )
        assert intervals[lane] <= cadence_sec + 1.0, (
            f"{lane} sweeps every {intervals[lane]:.0f}s but its fastest"
            f" product issues every {cadence_sec:.0f}s -- runs would be missed"
        )
        assert intervals[lane] >= cadence_sec / 2.0 - 1.0, (
            f"{lane} sweeps every {intervals[lane]:.0f}s against a"
            f" {cadence_sec:.0f}s cadence -- more than one spare attempt per"
            " cycle, and every surplus pass still takes an admission slot"
        )
        # A lane that does go faster than its cadence must be one a running
        # pass does not hold the queue for, or the first premise above is
        # back and the floor was the thing protecting the queue.
        if intervals[lane] < cadence_sec - 1.0:
            assert lane in daemon.WRITER_TURN_EXEMPT_SWEEPS, (
                f"{lane} sweeps faster than its cadence but holds the writer"
                " turn: its empty passes cost what this floor existed to stop"
            )


# ---------------------------------------------------------------- writer gates
#
# The sweep-to-sweep rule above always read the write domain.  The four writer
# gates -- production start, the prefetch, WAL maintenance and the turn
# rotation -- tested for *any* running sweep instead, so a sweep that writes no
# database held production out for as long as it ran.


def test_a_sweep_that_writes_no_database_excludes_no_writer(daemon) -> None:
    none_domain = [
        name for name, _s, _i, _t in daemon.HOSTED_SWEEPS
        if daemon.hosted_sweep_write_domain(name) == "none"
    ]
    assert none_domain, "the table should still hold at least one none-domain sweep"

    assert daemon.writer_excluding_sweeps(none_domain) == []


def test_ssot_and_archive_sweeps_still_exclude_the_writer_lanes(daemon) -> None:
    by_domain: dict[str, str] = {}
    for name, _s, _i, _t in daemon.HOSTED_SWEEPS:
        # Capture lanes are exempt from the writer gate by design, so the
        # ssot representative has to be a lane that is not.
        if name in daemon.WRITER_TURN_EXEMPT_SWEEPS:
            continue
        by_domain.setdefault(daemon.hosted_sweep_write_domain(name), name)
    running = [by_domain["ssot"], by_domain["archive"], by_domain["none"]]

    assert daemon.writer_excluding_sweeps(running) == sorted(
        [by_domain["ssot"], by_domain["archive"]]
    )


def test_an_undeclared_sweep_still_excludes_the_writer(daemon) -> None:
    # Unknown names are assumed to write the SSOT, so the fail-safe direction
    # is kept: an undeclared lane blocks production rather than racing it.
    assert daemon.writer_excluding_sweeps(["a_lane_nobody_declared"]) == [
        "a_lane_nobody_declared"
    ]


def test_every_writer_gate_reads_the_domain_not_bare_membership() -> None:
    """Read as code: no writer gate may test ``not hosted_sweep_futures``.

    A gate that tests bare membership is exactly the defect -- it counts a
    none-domain sweep as a writer.  Checked on the parsed source so a new gate
    written the old way fails here rather than on the board.
    """

    import ast

    source = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and isinstance(node.operand, ast.Name)
        and node.operand.id == "hosted_sweep_futures"
    ]
    assert offenders == [], f"writer gates testing bare sweep membership at lines {offenders}"
