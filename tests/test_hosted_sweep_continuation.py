"""A hosted sweep that stopped on its budget with work left is due again soon.

2026-09-14: a rule change sent the old paper ledger into a whole-history
re-derive that needed about two budgets, and its 10800s interval left the book
half-built for three hours.  A member now says on its stdout that it stopped
with work left, and the resident brings it due after a short delay.  (That
ledger retired on 2026-09-25; the resident's side of the contract stands.)
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(scope="module")
def daemon():
    spec = importlib.util.spec_from_file_location(
        "weather_basket_yes_daemon_continuation",
        ROOT / "scripts" / "weather_basket_yes_daemon.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "sweep.sh"
    path.write_text("#!/bin/bash\n" + body + "\n", encoding="utf-8")
    return str(path)


def test_a_clean_run_that_names_work_left_continues(daemon, tmp_path) -> None:
    result = daemon.run_hosted_sweep(
        _script(
            tmp_path,
            'echo "{\\"counts\\": 1}"\n'
            f'echo "{daemon.HOSTED_SWEEP_CONTINUE_MARKER}"\n'
            'echo "artifact copied"',
        ),
        30.0,
    )
    assert result["state"] == "ok"
    assert result["continue"] is True
    assert daemon.hosted_sweep_interval_after(result, 10800.0) == (
        daemon.HOSTED_SWEEP_CONTINUATION_DELAY_SEC
    )


def test_a_failed_run_keeps_its_interval_whatever_it_printed(
    daemon, tmp_path
) -> None:
    result = daemon.run_hosted_sweep(
        _script(
            tmp_path,
            f'echo "{daemon.HOSTED_SWEEP_CONTINUE_MARKER}"\nexit 3',
        ),
        30.0,
    )
    assert result["state"] == "failed"
    assert result["continue"] is False
    assert daemon.hosted_sweep_interval_after(result, 10800.0) == 10800.0


def test_a_run_that_only_mentions_the_marker_inside_a_line_does_not(
    daemon, tmp_path
) -> None:
    result = daemon.run_hosted_sweep(
        _script(
            tmp_path,
            f'echo "note: {daemon.HOSTED_SWEEP_CONTINUE_MARKER} was not due"',
        ),
        30.0,
    )
    assert result["continue"] is False


def test_the_delay_never_lengthens_a_short_member(daemon) -> None:
    assert daemon.hosted_sweep_interval_after(
        {"state": "ok", "continue": True}, 60.0
    ) == 60.0
    assert daemon.hosted_sweep_interval_after({}, 600.0) == 600.0


def test_a_restart_mid_repair_keeps_the_short_delay(daemon) -> None:
    """Seeded off the persisted request, the member is due within minutes."""

    interval = daemon.hosted_sweep_interval_after(
        {"state": "ok", "continue": True}, 10800.0
    )
    due = daemon.seed_hosted_sweep_clock(
        now_mono=100_000.0,
        now_wall_utc="2026-09-14T13:00:00Z",
        completed_at_utc="2026-09-14T12:50:00Z",
        attempted_at_utc="2026-09-14T12:50:00Z",
        interval_sec=interval,
        stagger_sec=60.0,
    )
    assert due <= 100_000.0 + 60.0
    plain = daemon.seed_hosted_sweep_clock(
        now_mono=100_000.0,
        now_wall_utc="2026-09-14T13:00:00Z",
        completed_at_utc="2026-09-14T12:50:00Z",
        attempted_at_utc="2026-09-14T12:50:00Z",
        interval_sec=10800.0,
        stagger_sec=60.0,
    )
    assert plain > 100_000.0 + 10_000.0


def test_a_restart_after_a_blocked_write_keeps_the_short_retry(daemon) -> None:
    """2026-09-18: CMA blocked at 00:43Z, restarted at 01:03Z, seeded at +3h."""

    blocked = {
        "attempted_at_utc": "2026-09-18T00:43:42Z",
        "completed_at_utc": "2026-09-17T21:29:27Z",
        "last_state": daemon.HOSTED_SWEEP_WRITE_BLOCKED_STATE,
        "continue_requested": False,
    }
    due = daemon.seed_hosted_sweep_clock(
        now_mono=100_000.0,
        now_wall_utc="2026-09-18T01:03:34Z",
        completed_at_utc=blocked["completed_at_utc"],
        attempted_at_utc=blocked["attempted_at_utc"],
        interval_sec=daemon.hosted_sweep_interval_after(
            daemon.hosted_sweep_boot_outcome(blocked), 10800.0
        ),
        stagger_sec=60.0,
    )
    assert due <= 100_000.0 + 60.0
    # A failure still waits its whole interval: only the blocked state and a
    # continuation request earn the short delay.
    failed = dict(blocked, last_state="failed")
    assert daemon.hosted_sweep_interval_after(
        daemon.hosted_sweep_boot_outcome(failed), 10800.0
    ) == 10800.0
    assert daemon.hosted_sweep_boot_outcome(None) == {}


def test_completion_and_boot_both_read_the_run_outcome(daemon) -> None:
    source = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    uses = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "hosted_sweep_interval_after"
    ]
    # One where a run finishes, one inside the boot seed.
    assert len(uses) >= 2
    seeds = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "seed_hosted_sweep_clock"
    ]
    assert any(
        "hosted_sweep_interval_after" in ast.unparse(keyword.value)
        for seed in seeds
        for keyword in seed.keywords
        if keyword.arg == "interval_sec"
    )


def test_a_run_refused_at_the_wal_ceiling_retries_in_minutes(
    daemon, tmp_path
) -> None:
    # 2026-10-27 settlement_truth: the guard raised before any write, the
    # receipt said ``failed``, and the lane sat out three hours.
    result = daemon.run_hosted_sweep(
        _script(
            tmp_path,
            'echo "bayes_basket_target.resource_bounds.ResourceBoundError: '
            'target_database_wal_bytes_exceeded" >&2\nexit 1',
        ),
        30.0,
    )
    assert result["state"] == daemon.HOSTED_SWEEP_WRITE_BLOCKED_STATE
    assert daemon.hosted_sweep_interval_after(result, 10800.0) == (
        daemon.HOSTED_SWEEP_CONTINUATION_DELAY_SEC
    )
