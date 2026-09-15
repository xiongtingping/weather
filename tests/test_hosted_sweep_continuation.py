"""A hosted sweep that stopped on its budget with work left is due again soon.

2026-09-14: a rule change sent the paper ledger into a whole-history
re-derive.  The 11:32Z pass was killed at its 1800s timeout with the book
emptied; the slice-and-resume repair (6858f017) made each pass safe but left
the book partial between passes, and the ledger's interval is 10800s -- the
walk needs about two budgets, so the book stayed half-built for three hours
while two more rule changes landed.  The member now says on its stdout that it
stopped with repair left, and the resident brings it due after a short delay.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import paper_trading_ledger as ledger  # noqa: E402


@pytest.fixture(scope="module")
def daemon():
    spec = importlib.util.spec_from_file_location(
        "weather_basket_yes_daemon_continuation",
        ROOT / "scripts" / "weather_basket_yes_daemon.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ledger_and_resident_speak_the_same_line(daemon) -> None:
    assert ledger.HOSTED_SWEEP_CONTINUE_MARKER == (
        daemon.HOSTED_SWEEP_CONTINUE_MARKER
    )


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


def _run_main(monkeypatch, tmp_path, *, slices, remaining) -> str:
    """Drive ``main --backfill`` over scripted slice outcomes."""

    db = tmp_path / "book.db"
    db.write_bytes(b"")
    monkeypatch.setattr(
        ledger, "_connect", lambda *_a, **_kw: sqlite3.connect(":memory:")
    )
    outcomes = iter(slices)
    monkeypatch.setattr(ledger, "backfill", lambda *_a, **_kw: next(outcomes))
    monkeypatch.setattr(
        ledger, "_repair_slice_through", lambda *_a, **_kw: remaining
    )
    monkeypatch.setattr(ledger, "REDERIVE_PASS_BUDGET_SEC", 3600.0)
    monkeypatch.setattr(
        sys, "argv", ["paper_trading_ledger.py", "--backfill", "--db", str(db)]
    )
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert ledger.main() == 0
    return buffer.getvalue()


def _marker_printed(output: str) -> bool:
    return ledger.HOSTED_SWEEP_CONTINUE_MARKER in {
        line.strip() for line in output.splitlines()
    }


def test_a_pass_that_moved_the_repair_and_has_more_asks_to_continue(
    monkeypatch, tmp_path
) -> None:
    out = _run_main(
        monkeypatch, tmp_path,
        slices=[
            {"repair_slice_through": "2026-08-19"},
            {"repair_slice_through": "2026-08-24"},
            {},
        ],
        remaining="2026-08-29",
    )
    assert _marker_printed(out)


def test_a_finished_repair_or_a_plain_pass_does_not(monkeypatch, tmp_path) -> None:
    assert not _marker_printed(_run_main(
        monkeypatch, tmp_path,
        slices=[{"repair_slice_through": "2026-09-14"}, {}],
        remaining=None,
    ))
    assert not _marker_printed(_run_main(
        monkeypatch, tmp_path, slices=[{}], remaining="2026-08-19",
    ))


def test_a_stalled_repair_stays_on_the_interval(monkeypatch, tmp_path) -> None:
    out = _run_main(
        monkeypatch, tmp_path,
        slices=[
            {"repair_slice_through": "2026-08-19"},
            {"repair_slice_through": "2026-08-19"},
        ],
        remaining="2026-08-19",
    )
    assert not _marker_printed(out)


def test_a_repair_pass_never_holds_production_past_its_overdue_line(
    daemon,
) -> None:
    """Continuation makes repair passes back-to-back, so each one's budget is
    what production waits behind the SSOT write domain every time.  The loop
    only starts a slice it expects to finish (1.5x the last), so a pass ends
    inside its budget plus at most one slice; the budget itself must leave
    production's overdue line standing."""

    assert daemon.hosted_sweep_write_domain("paper_trading_ledger") == "ssot"
    assert ledger.REDERIVE_PASS_BUDGET_SEC < daemon.PRODUCTION_CYCLE_OVERDUE_SEC
