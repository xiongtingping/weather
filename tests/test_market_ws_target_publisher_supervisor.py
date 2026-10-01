"""The listener restarts a target publisher that died or stopped writing.

2026-09-30T10:02Z the publisher task ended silently inside the listener and
its manifest froze at ``state: connected``; every lane reading it refused
``book_stale`` for fifteen hours.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from scripts.market_ws_listener import _supervise_target_publisher


def _run(coro, seconds: float) -> None:
    async def bounded() -> None:
        try:
            await asyncio.wait_for(coro, timeout=seconds)
        except asyncio.TimeoutError:
            pass

    asyncio.run(bounded())


def test_a_publisher_that_dies_is_started_again(tmp_path: Path, capsys) -> None:
    starts: list[int] = []

    async def publisher() -> None:
        starts.append(1)
        raise asyncio.CancelledError  # BaseException: escaped every except

    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    _run(_supervise_target_publisher(
        publisher, manifest_path=manifest, stall_seconds=60, check_seconds=0.01,
    ), 2.5)
    assert len(starts) >= 2
    assert "publisher_restarted:cancelled" in capsys.readouterr().out


def test_a_publisher_that_stops_writing_is_cancelled_and_restarted(
    tmp_path: Path, capsys,
) -> None:
    starts: list[int] = []
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    old = time.time() - 3600
    os.utime(manifest, (old, old))

    async def publisher() -> None:
        starts.append(1)
        while True:  # alive, connected, writing nothing
            await asyncio.sleep(0.01)

    _run(_supervise_target_publisher(
        publisher, manifest_path=manifest, stall_seconds=0.05, check_seconds=0.02,
        cancel_grace_seconds=0.1,
    ), 2.5)
    assert len(starts) >= 2
    assert "publisher_restarted:stalled:manifest_age=" in capsys.readouterr().out


def test_a_writing_publisher_is_left_alone(tmp_path: Path, capsys) -> None:
    starts: list[int] = []
    manifest = tmp_path / "manifest.json"

    async def publisher() -> None:
        starts.append(1)
        while True:
            manifest.write_text("{}")
            await asyncio.sleep(0.01)

    _run(_supervise_target_publisher(
        publisher, manifest_path=manifest, stall_seconds=0.2, check_seconds=0.02,
    ), 1.0)
    assert starts == [1]
    assert "publisher_restarted" not in capsys.readouterr().out


def test_a_publisher_ignoring_cancellation_is_abandoned(tmp_path: Path, capsys) -> None:
    starts: list[int] = []
    manifest = tmp_path / "manifest.json"

    async def publisher() -> None:
        starts.append(1)
        swallowed = False
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                if swallowed:  # let asyncio.run's teardown finish
                    raise
                swallowed = True  # the wedge: swallows its cancellation

    _run(_supervise_target_publisher(
        publisher, manifest_path=manifest, stall_seconds=0.05, check_seconds=0.02,
        cancel_grace_seconds=0.05,
    ), 2.0)
    assert len(starts) >= 2
    assert "manifest_age=missing" in capsys.readouterr().out
