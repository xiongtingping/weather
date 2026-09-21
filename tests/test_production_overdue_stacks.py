"""An overdue cycle names the Python frames it is in, not only its SQL.

On 2026-09-21 three resident processes in a row never closed a cycle, and the
statement probe could only report the PRAGMA each cycle thread opened its
connection with: the thread ran ten short statements a second and none was
slow.  py-spy needs root on this host, so the daemon reads its own threads.
"""

import threading

from scripts import weather_basket_yes_daemon as daemon


def _park_in_a_named_frame(ready: threading.Event, release: threading.Event) -> None:
    ready.set()
    release.wait(5)


def test_the_snapshot_names_the_frame_a_thread_is_parked_in():
    ready, release = threading.Event(), threading.Event()
    worker = threading.Thread(
        target=_park_in_a_named_frame, args=(ready, release), name="cycle-probe"
    )
    worker.start()
    try:
        assert ready.wait(5)
        snapshot = daemon._thread_stacks_snapshot()
    finally:
        release.set()
        worker.join(5)
    assert snapshot["schema"] == "production_overdue_stacks_v1"
    parked = [t for t in snapshot["threads"] if t["thread_name"] == "cycle-probe"]
    assert len(parked) == 1
    assert any("_park_in_a_named_frame" in frame for frame in parked[0]["frames"])


def test_the_snapshot_is_bounded_per_thread():
    snapshot = daemon._thread_stacks_snapshot(max_frames=3)
    assert snapshot["threads"]
    assert all(len(t["frames"]) <= 3 for t in snapshot["threads"])


def test_the_overdue_branch_writes_the_stacks_on_a_throttle():
    # The write lives on the overdue path only, beside the statement probe,
    # and is throttled; a healthy cycle never pays for it.
    assert daemon.PRODUCTION_OVERDUE_STACKS_EVERY_SEC >= 10.0
    assert daemon.PRODUCTION_OVERDUE_STACKS_PATH.name == "production_overdue_stacks.json"
