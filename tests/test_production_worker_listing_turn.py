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
        "decided_at_utc": "2026-09-02T14:00:00Z",
    }))
    assert stormed._take_listing_scope_turn(now_utc=late)


def test_request_stop_is_reported_on_the_cycle(tmp_path: Path) -> None:
    root = tmp_path / "nwp"
    root.mkdir()
    worker = _worker(tmp_path, root)
    assert worker.stop_requested is False
    worker.request_stop()
    assert worker.stop_requested is True
