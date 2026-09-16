"""A contended SSOT write stops a capture pass; it does not destroy one.

Measured 2026-09-15.  ``capture_jma_msm_forward`` spent 955 seconds of
ranged reads against the RISH mirror and ``capture_cma_grapes_forward`` 115
against its aggregator, and both ended the same way:

    sqlite3.OperationalError: database is locked

raised out of the INSERT, through the pass, out of ``main``.  Everything the
pass had fetched and not yet committed went with it, the lane's clock read
``failed``, and the next attempt was one whole interval away -- three hours
for MSM, six for GRAPES, whose transport serves only the run it currently
holds, so the cycle in between is gone for good.

The lock is not the lane's fault and not the lane's to fix: production
cycles run 125 to 427 seconds on this file and a paper-ledger round
re-deriving the whole book runs far longer, against a ``busy_timeout`` of
60 seconds.  What the lane owes is to behave when it happens -- keep what it
committed, stop rather than fetch runs it cannot store, say so where both a
human and the resident can see it, and come back in minutes.

These tests hold the four halves of that.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import regional_point_lane as lane  # noqa: E402
from bayes_basket_target import sqlite_busy  # noqa: E402
from bayes_basket_target.pass_deadline import (  # noqa: E402
    HOSTED_SWEEP_WRITE_BLOCKED_MARKER,
    announce_write_blocked,
)


def _daemon():
    spec = importlib.util.spec_from_file_location(
        "_write_blocked_daemon_under_test",
        ROOT / "scripts" / "weather_basket_yes_daemon.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def daemon():
    return _daemon()


# --------------------------------------------------------- the classification


def test_only_a_contended_lock_becomes_write_blocked():
    """A lane's own faults have to keep travelling as themselves.

    The conversion exists to say "stop this pass, not this lane".  A CHECK
    constraint, a UNIQUE, a missing column: those say the opposite, and a
    lane that answered them with "the file was busy, try again in five
    minutes" would retry that fault forever while reading blocked.
    """

    with pytest.raises(sqlite_busy.WriteBlocked):
        with sqlite_busy.blocked_when_busy():
            raise sqlite3.OperationalError("database is locked")

    for other in (
        sqlite3.OperationalError("no such table: calibration_regional_point_run"),
        sqlite3.IntegrityError("UNIQUE constraint failed"),
        ValueError("not a database fault at all"),
    ):
        with pytest.raises(type(other)):
            with sqlite_busy.blocked_when_busy():
                raise other


def test_the_two_lock_faults_are_told_apart_by_their_extended_code():
    """Waiting fixes one of them and can never fix the other."""

    starved = sqlite3.OperationalError("database is locked")
    starved.sqlite_errorcode = sqlite_busy.SQLITE_BUSY_PRIMARY
    stale = sqlite3.OperationalError("database is locked")
    stale.sqlite_errorcode = sqlite_busy.SQLITE_BUSY_SNAPSHOT

    assert sqlite_busy.busy_kind(starved) == (
        sqlite_busy.LOCK_STARVED, sqlite_busy.SQLITE_BUSY_PRIMARY
    )
    assert sqlite_busy.busy_kind(stale) == (
        sqlite_busy.STALE_READ_SNAPSHOT, sqlite_busy.SQLITE_BUSY_SNAPSHOT
    )
    # The kind reaches the caller, so a lane can report which one it met.
    assert sqlite_busy.WriteBlocked(*sqlite_busy.busy_kind(stale)).kind == (
        sqlite_busy.STALE_READ_SNAPSHOT
    )


def test_production_reads_the_same_classifier_under_its_own_names():
    """One copy of the distinction, two vocabularies over it.

    Production names its faults ``target_writer_*`` and branches on the
    stale-snapshot one.  If that branch and the classifier ever drift, a
    fault retrying cannot fix gets retried three times a cycle.
    """

    from bayes_basket_target import production_worker

    stale = sqlite3.OperationalError("database is locked")
    stale.sqlite_errorcode = sqlite_busy.SQLITE_BUSY_SNAPSHOT
    name, code = production_worker._sqlite_busy_kind(stale)
    assert name == production_worker.WRITER_STALE_READ_SNAPSHOT
    assert code == sqlite_busy.SQLITE_BUSY_SNAPSHOT


# ------------------------------------------------------- the pass keeps its work


def _lane_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "ssot.db")
    lane.ensure_schema(conn)
    conn.commit()
    return conn


def _icon_pass(conn, monkeypatch, *, fetched, max_runs=4):
    from bayes_basket_target import dwd_icon_eu_point_archive as icon

    def fake_fetch(*, run_init, targets):
        fetched.append(run_init)
        return {
            (str(t["station_id"]), t["event_date"].isoformat()): {
                "hourly_c": {hour: 18.0 + hour / 10.0 for hour in range(24)},
                "hours_wanted": list(range(24)),
            }
            for t in targets
        }

    monkeypatch.setattr(icon, "fetch_run_day_maxima", fake_fetch)
    monkeypatch.setattr(
        icon, "file_url", lambda run_init, step: "https://example.invalid/icon"
    )
    return lane.capture_p1(
        conn,
        product="ICON_EU",
        now=datetime(2026, 9, 15, 21, 0, tzinfo=timezone.utc),
        stations=[("EDDM", 48.35, 11.79, "Europe/Berlin")],
        max_runs=max_runs,
    )


def _hold_the_write_lock(tmp_path):
    """A second connection holding the file's write lock, as production does.

    A real holder rather than a patched method: the whole question is what
    SQLite does to a write it will not admit, and a stub that raises on cue
    would be this file asserting its own idea of that.
    """

    blocker = sqlite3.connect(tmp_path / "ssot.db", isolation_level=None)
    blocker.execute("PRAGMA busy_timeout=0")
    blocker.execute("BEGIN IMMEDIATE")
    return blocker


def test_a_blocked_write_keeps_the_runs_that_already_landed(tmp_path, monkeypatch):
    """The rows committed before the lock are the pass's, and it keeps them."""

    conn = _lane_db(tmp_path)
    fetched: list[datetime] = []
    first = _icon_pass(conn, monkeypatch, fetched=fetched, max_runs=1)
    assert first["written"] >= 1 and not first["write_blocked"]
    landed = conn.execute(f"SELECT COUNT(*) FROM {lane.TABLE}").fetchone()[0]
    assert landed == first["written"]

    # Now somebody else has the file, as a production cycle or a ledger
    # round re-deriving the book does, for longer than this connection will
    # wait.
    conn.execute("PRAGMA busy_timeout=0")
    blocker = _hold_the_write_lock(tmp_path)
    try:
        blocked = _icon_pass(conn, monkeypatch, fetched=fetched, max_runs=4)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert blocked["write_blocked"], blocked
    assert sqlite_busy.LOCK_STARVED in blocked["write_blocked"], blocked
    # Nothing claimed that did not land.
    assert blocked["written"] == 0
    assert conn.execute(
        f"SELECT COUNT(*) FROM {lane.TABLE}"
    ).fetchone()[0] == landed


def test_a_blocked_pass_stops_fetching_runs_it_cannot_store(tmp_path, monkeypatch):
    """One fetch discovers the lock; the rest of the plan is not downloaded.

    This is the half that keeps the short retry affordable.  A pass that
    kept fetching would spend its whole budget -- 30-50 MB a run on the
    CONUS lane -- to arrive at the same INSERT, and would do it again every
    five minutes for as long as the holder held.
    """

    conn = _lane_db(tmp_path)
    conn.execute("PRAGMA busy_timeout=0")
    fetched: list[datetime] = []
    blocker = _hold_the_write_lock(tmp_path)
    try:
        receipt = _icon_pass(conn, monkeypatch, fetched=fetched, max_runs=4)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert receipt["write_blocked"]
    assert len(fetched) == 1, fetched
    # And the plan really did hold more than one run, or the assertion above
    # would pass for the wrong reason.
    assert receipt["runs_planned"] > 1, receipt


class _CommitBlocks(sqlite3.Connection):
    """Takes the inserts and refuses the COMMIT, which is its own lock."""

    def commit(self):  # noqa: D102
        exc = sqlite3.OperationalError("database is locked")
        exc.sqlite_errorcode = sqlite_busy.SQLITE_BUSY_PRIMARY
        raise exc


def test_a_blocked_commit_counts_as_blocked_not_as_written(tmp_path, monkeypatch):
    """A COMMIT that waits out busy_timeout leaves the run's rows pending.

    The inserts are per run and the commit closes the run, so a pass that
    counted rows at INSERT time would report a run written whose rows the
    file never took.
    """

    plain = sqlite3.connect(tmp_path / "ssot.db")
    lane.ensure_schema(plain)
    plain.commit()
    plain.close()

    conn = sqlite3.connect(tmp_path / "ssot.db", factory=_CommitBlocks)
    fetched: list[datetime] = []
    receipt = _icon_pass(conn, monkeypatch, fetched=fetched, max_runs=4)

    assert receipt["write_blocked"], receipt
    assert receipt["written"] == 0, receipt
    conn.close()
    after = sqlite3.connect(tmp_path / "ssot.db")
    assert after.execute(f"SELECT COUNT(*) FROM {lane.TABLE}").fetchone()[0] == 0


def test_prepare_answers_the_lock_before_any_network_is_spent(tmp_path, monkeypatch):
    """The lane's setup writes, so it is also the pass's free pre-flight."""

    conn = sqlite3.connect(tmp_path / "ssot.db")
    calls: list[str] = []

    def locked(*_args, **_kwargs):
        calls.append("register")
        exc = sqlite3.OperationalError("database is locked")
        exc.sqlite_errorcode = sqlite_busy.SQLITE_BUSY_PRIMARY
        raise exc

    from bayes_basket_target import native_ensemble_training_facts as facts

    monkeypatch.setattr(facts, "register_regional_point_systems", locked)
    with pytest.raises(sqlite_busy.WriteBlocked):
        lane.prepare(conn)
    assert calls == ["register"]


# --------------------------------------------------- the lane says so, and returns


CAPTURE_LANES = (
    "capture_jma_msm_forward",
    "capture_cma_grapes_forward",
    "capture_dwd_icon_eu_forward",
    "capture_noaa_conus_forward",
)


@pytest.mark.parametrize("name", CAPTURE_LANES)
def test_every_capture_lane_reports_a_blocked_write(name):
    """All four write the SSOT the same way, so all four must answer it.

    Read off the source because the alternative is driving four networked
    passes; what is asserted is a call, not a spelling -- the two lines the
    resident and a reader need are inside ``announce_write_blocked``, and
    a lane that prints its own pair instead of calling it is exactly the
    drift this checks for.
    """

    source = (ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8")
    assert "announce_write_blocked(" in source, name
    assert "WriteBlocked" in source, name
    # The marker is never spelled by a lane; only the shared announcer says
    # it, so the human line and the machine line cannot come apart.
    assert HOSTED_SWEEP_WRITE_BLOCKED_MARKER not in source, name


def test_the_announcer_prints_both_halves(capsys):
    announce_write_blocked("CMA 2026-09-15T18Z lock_starved:sqlite_errorcode=5")
    printed = capsys.readouterr().out.splitlines()
    assert printed[-1].strip() == HOSTED_SWEEP_WRITE_BLOCKED_MARKER
    assert "lock_starved" in printed[-2]


# ------------------------------------------------------- what the resident does


def test_the_resident_reads_the_marker_as_its_own_state(daemon, tmp_path):
    """Not ``ok``: the pass stored nothing and the clock must go on saying so.

    ``collect_hosted_sweeps`` exists because a lane that never got a turn is
    indistinguishable from a healthy one unless somebody divides two numbers
    by hand.  A blocked lane reported as ``ok`` would be the same shape: the
    board green while the lane captures nothing.
    """

    script = tmp_path / "blocked.sh"
    script.write_text(
        "#!/bin/bash\n"
        "echo 'P1 CMA: ...'\n"
        f"echo '{HOSTED_SWEEP_WRITE_BLOCKED_MARKER}'\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    result = daemon.run_hosted_sweep(str(script), 30.0)

    assert result["state"] == daemon.HOSTED_SWEEP_WRITE_BLOCKED_STATE
    assert result["returncode"] == 0
    assert result["continue"] is False

    entry = daemon.hosted_sweep_clock_entry(
        {"completed_at_utc": "2026-09-15T07:54:18.659Z"},
        result,
        name="capture_cma_grapes_forward",
        now_utc="2026-09-15T21:01:02.241Z",
    )
    assert entry["attempted_at_utc"] == "2026-09-15T21:01:02.241Z"
    # The completion stamp is what "has this member succeeded since" means.
    assert entry["completed_at_utc"] == "2026-09-15T07:54:18.659Z"
    assert entry["last_state"] == daemon.HOSTED_SWEEP_WRITE_BLOCKED_STATE


def test_a_blocked_lane_comes_due_in_minutes_not_in_its_whole_interval(daemon):
    """Six hours is the whole window for a transport that keeps no history."""

    blocked = {"state": daemon.HOSTED_SWEEP_WRITE_BLOCKED_STATE}
    assert daemon.hosted_sweep_interval_after(blocked, 21_600.0) == (
        daemon.HOSTED_SWEEP_CONTINUATION_DELAY_SEC
    )
    # A lane whose interval is already shorter keeps its own.
    assert daemon.hosted_sweep_interval_after(blocked, 120.0) == 120.0
    # Everything else is unchanged: a genuine failure still waits, which is
    # what stops a broken member from taking its slot every few minutes.
    assert daemon.hosted_sweep_interval_after(
        {"state": "failed"}, 21_600.0
    ) == 21_600.0
    assert daemon.hosted_sweep_interval_after(
        {"state": "timeout_killed"}, 21_600.0
    ) == 21_600.0
    assert daemon.hosted_sweep_interval_after({"state": "ok"}, 21_600.0) == 21_600.0


def test_the_blocked_state_is_only_reachable_on_a_clean_exit(daemon, tmp_path):
    """A crash that happens to print the marker is still a crash."""

    script = tmp_path / "crashed.sh"
    script.write_text(
        "#!/bin/bash\n"
        f"echo '{HOSTED_SWEEP_WRITE_BLOCKED_MARKER}'\n"
        "exit 1\n"
    )
    script.chmod(0o755)
    assert daemon.run_hosted_sweep(str(script), 30.0)["state"] == "failed"


def test_the_short_retry_is_only_safe_for_lanes_that_hold_no_writer(daemon):
    """The reason a blocked lane may come back in five minutes.

    A capture is writer-turn exempt -- running, it holds the other captures
    and not the SSOT queue -- so retrying it soon takes no turn from
    production.  If a lane that DOES hold the writer ever learns to report
    this state, the delay stops being free and this pairing has to be
    re-decided.
    """

    reporting = {
        name for name in CAPTURE_LANES
        if "announce_write_blocked(" in
        (ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8")
    }
    assert reporting == set(CAPTURE_LANES)
    assert reporting <= daemon.WRITER_TURN_EXEMPT_SWEEPS
    for name in reporting:
        assert not daemon.sweep_finish_serves_writer_turn(name)
