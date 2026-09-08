"""Charter 48(e): the listed days' turn survives a restart and cannot starve.

Held in memory the turn was reseeded to "current day first" by every
restart; with nineteen restarts on 2026-09-02 the listed day almost never
got a cycle.  Persisted, a restart resumes the alternation, and a listed
day unserved for LISTING_SCOPE_TURN_STARVATION_SEC takes the next cycle.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bayes_basket_target.production_worker import (
    LISTING_SCOPE_TURN_STARVATION_SEC,
    TargetProductionWorker,
)


def _worker(tmp_path: Path, root: Path) -> TargetProductionWorker:
    db = tmp_path / "target-read.db"
    if not db.exists():
        with sqlite3.connect(db) as source:
            source.execute("CREATE TABLE marker(value TEXT)")
    return TargetProductionWorker(
        db,
        commit_conn=None,
        nwp_runtime_root=root,
    )


def test_turn_alternates_and_is_persisted(tmp_path: Path) -> None:
    root = tmp_path / "nwp"
    root.mkdir()
    worker = _worker(tmp_path, root)
    # Nothing recorded: the listed days are starved by definition and go
    # first; then strict alternation.
    assert worker._take_listing_scope_turn(now_utc="2026-09-02T14:00:00Z")
    assert not worker._take_listing_scope_turn(
        now_utc="2026-09-02T14:07:00Z"
    )
    assert worker._take_listing_scope_turn(now_utc="2026-09-02T14:14:00Z")
    record = json.loads((root / "listing_scope_turn.json").read_text())
    assert record["listing_turn"] is True
    assert record["listing_served_at_utc"] == "2026-09-02T14:14:00Z"


def test_restart_resumes_the_alternation_instead_of_reseeding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "nwp"
    root.mkdir()
    first = _worker(tmp_path, root)
    assert first._take_listing_scope_turn(now_utc="2026-09-02T14:00:00Z")
    # A new process (the restart) reads the file: the listed days were just
    # served, so the current day goes first -- exactly one skipped turn,
    # not a reseed on every restart.
    restarted = _worker(tmp_path, root)
    assert not restarted._take_listing_scope_turn(
        now_utc="2026-09-02T14:05:00Z"
    )
    assert restarted._take_listing_scope_turn(
        now_utc="2026-09-02T14:12:00Z"
    )


def test_a_starved_listed_day_takes_the_next_cycle(tmp_path: Path) -> None:
    root = tmp_path / "nwp"
    root.mkdir()
    worker = _worker(tmp_path, root)
    assert worker._take_listing_scope_turn(now_utc="2026-09-02T14:00:00Z")
    # Restart storm: every new process finds "served last" and would skip.
    # Past the starvation bound it serves regardless of the alternation.
    late = "2026-09-02T14:%02d:00Z" % (int(LISTING_SCOPE_TURN_STARVATION_SEC) // 60 + 1)
    stormed = _worker(tmp_path, root)
    (root / "listing_scope_turn.json").write_text(json.dumps({
        "schema": "bayes_basket_target_listing_scope_turn_v1",
        "listing_turn": True,
        "listing_served_at_utc": "2026-09-02T14:00:00Z",
        # The storm is a storm of SHORT cycles -- the current day keeps
        # getting served, which is why the listed days are the ones
        # starving.  Recorded since 2026-09-08: without it the override
        # cannot tell this case from the one where cycles have grown so
        # long that BOTH lanes are starved, and taking every cycle in that
        # state is what left the current day unserved for over an hour.
        "current_served_at_utc": "2026-09-02T14:14:00Z",
        "decided_at_utc": "2026-09-02T14:14:00Z",
    }))
    assert stormed._take_listing_scope_turn(now_utc=late)


def test_request_stop_is_reported_on_the_cycle(tmp_path: Path) -> None:
    root = tmp_path / "nwp"
    root.mkdir()
    worker = _worker(tmp_path, root)
    assert worker.stop_requested is False
    worker.request_stop()
    assert worker.stop_requested is True


def test_a_long_cycle_does_not_let_the_listed_days_take_every_turn(
    tmp_path: Path,
) -> None:
    """The starvation override inverted when a cycle outran it (2026-09-08).

    ``listing_served_at_utc`` only advances on a listing cycle, so once a
    cycle takes longer than the bound the listed days are starved at every
    decision and take every cycle forever.  Measured that day: the current
    day's last decision was 10:32:15Z and it was still unserved an hour
    later, its interval having widened all day -- 31, 37, 52, 40 minutes --
    while the listed days were served at 11:28.  The guard meant to stop a
    lane starving was starving the other one.
    """

    root = tmp_path / "nwp"
    root.mkdir()
    worker = _worker(tmp_path, root)
    step = int(LISTING_SCOPE_TURN_STARVATION_SEC) // 60 + 5  # a long cycle
    served = []
    minute = 0
    for _ in range(8):
        stamp = "2026-09-08T%02d:%02d:00Z" % (10 + minute // 60, minute % 60)
        served.append(worker._take_listing_scope_turn(now_utc=stamp))
        minute += step
    # Every cycle outruns the bound, so both lanes are starved at every
    # decision.  Strict alternation is what that has to fall back to.
    assert served == [True, False, True, False, True, False, True, False]
    record = json.loads((root / "listing_scope_turn.json").read_text())
    assert record["current_served_at_utc"], "the current day's serve is recorded"


def test_the_override_still_rescues_a_genuinely_starved_listed_day(
    tmp_path: Path,
) -> None:
    """The 2026-09-02 case the override exists for is unchanged: short
    cycles, a restart storm reseeding "current day first", and the listed
    days going unserved past the bound."""

    root = tmp_path / "nwp"
    root.mkdir()
    worker = _worker(tmp_path, root)
    # Listed days served, then the current day served repeatedly on short
    # cycles -- so the current day is NOT starved and the listed days are.
    assert worker._take_listing_scope_turn(now_utc="2026-09-02T14:00:00Z")
    assert not worker._take_listing_scope_turn(now_utc="2026-09-02T14:05:00Z")
    (root / "listing_scope_turn.json").write_text(json.dumps({
        "schema": "bayes_basket_target_listing_scope_turn_v1",
        "listing_turn": True,
        "listing_served_at_utc": "2026-09-02T14:00:00Z",
        "current_served_at_utc": "2026-09-02T14:20:00Z",
        "decided_at_utc": "2026-09-02T14:20:00Z",
    }))
    late = "2026-09-02T14:%02d:00Z" % (
        int(LISTING_SCOPE_TURN_STARVATION_SEC) // 60 + 1
    )
    stormed = _worker(tmp_path, root)
    assert stormed._take_listing_scope_turn(now_utc=late)
