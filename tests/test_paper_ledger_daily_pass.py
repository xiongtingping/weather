"""The paper ledger's daily pass walks one settled event day per run.

2026-09-25: the pass re-walked seven event days every day -- ~12,000 basket
decisions, each a production compile at 1-5 s of SSOT reads on this machine --
against a 90-minute slot; it was stopped at 69 minutes, stored nothing, and
the lane sat red (user: 「没有会话在改，你改」).  Now a pass takes the oldest
settled day in its window that no daily pass has stored, asks the resident
for the next pass while days are left, and keeps every failure with its reason
so one day that cannot be replayed does not hold the lane red for ever.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import paper_ledger_aligned as L  # noqa: E402
from bayes_basket_target.pass_deadline import HOSTED_SWEEP_CONTINUE_MARKER  # noqa: E402

TODAY = date(2026, 9, 26)
# Window of three settled days ending two days back.
WINDOW = ["2026-09-22", "2026-09-23", "2026-09-24"]


def _ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    for ddl in L.LEDGER_DDL:
        conn.execute(ddl)
    conn.execute(L.DAILY_PASS_DDL)
    conn.commit()
    conn.close()
    return path


def _stored_run(path: Path, day: str, label: str = L.DAILY_PASS_LABEL) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
            (f"run_{day}_{label}", L.SCHEMA, "2026-09-26T05:00:00Z", "2026-09-26T06:00:00Z",
             "head", json.dumps({"label": label, "from_event_date": day, "to_event_date": day}),
             "{}"),
        )
    conn.close()


def _failed(path: Path, day: str, times: int) -> None:
    conn = sqlite3.connect(path)
    with conn:
        for _ in range(times):
            conn.execute(
                "INSERT INTO daily_pass_attempts VALUES(?,?,?,?,?)",
                (day, "2026-09-26T05:00:00Z", "failed", None, "RuntimeError: x"),
            )
    conn.close()


def _args(path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        ledger_db=str(path), window_days=3, dry_run=False, since_rowid=0,
        until_rowid=None, stations="", limit=0, label="", from_event_date=None,
        to_event_date=None,
    )


def test_the_window_is_the_settled_days_oldest_first(tmp_path) -> None:
    pending, walked, failed = L.daily_pass_plan(
        tmp_path / "absent.db", today=TODAY, window_days=3,
    )
    assert pending == WINDOW and walked == [] and failed == {}


def test_a_day_a_daily_pass_stored_is_not_walked_again(tmp_path) -> None:
    path = _ledger(tmp_path)
    _stored_run(path, "2026-09-22")
    # A hand run over the same day under another label does not count.
    _stored_run(path, "2026-09-23", label="arm:base")
    pending, walked, _ = L.daily_pass_plan(path, today=TODAY, window_days=3)
    assert pending == ["2026-09-23", "2026-09-24"]
    assert walked == ["2026-09-22"]


def test_a_day_that_failed_twice_is_left_behind(tmp_path) -> None:
    path = _ledger(tmp_path)
    _failed(path, "2026-09-22", 2)
    _failed(path, "2026-09-23", 1)
    pending, _, failed = L.daily_pass_plan(path, today=TODAY, window_days=3)
    assert pending == ["2026-09-23", "2026-09-24"]
    assert failed == {"2026-09-22": 2, "2026-09-23": 1}


def test_a_pass_walks_one_day_and_asks_for_the_next_while_any_are_left(
    tmp_path, monkeypatch, capsys,
) -> None:
    path = _ledger(tmp_path)
    walked: list[tuple[str, str, str]] = []

    def fake_run(args):
        walked.append((args.from_event_date, args.to_event_date, args.label))
        _stored_run(path, args.from_event_date)
        return {"run_id": f"run_{args.from_event_date}", "seconds": 1.0}

    monkeypatch.setattr(L, "run", fake_run)
    assert L.daily_pass(_args(path), today=TODAY) == 0
    assert walked == [("2026-09-22", "2026-09-22", L.DAILY_PASS_LABEL)]
    assert HOSTED_SWEEP_CONTINUE_MARKER in capsys.readouterr().out.splitlines()

    # Drain the rest: the last day does not ask for another pass.
    assert L.daily_pass(_args(path), today=TODAY) == 0
    capsys.readouterr()
    assert L.daily_pass(_args(path), today=TODAY) == 0
    last = capsys.readouterr().out.splitlines()
    assert HOSTED_SWEEP_CONTINUE_MARKER not in last
    assert [day for day, _, _ in walked] == WINDOW
    # Nothing owed: a no-op that still succeeds.
    assert L.daily_pass(_args(path), today=TODAY) == 0
    assert json.loads(capsys.readouterr().out)["daily_pass"] == "up_to_date"
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT COUNT(*) FROM daily_pass_attempts WHERE state='stored'"
    ).fetchone()[0] == 3


def test_a_failed_day_is_recorded_with_its_reason_and_fails_the_pass(
    tmp_path, monkeypatch, capsys,
) -> None:
    path = _ledger(tmp_path)

    def broken_run(args):
        raise KeyError("registry snapshot")

    monkeypatch.setattr(L, "run", broken_run)
    assert L.daily_pass(_args(path), today=TODAY) == 2
    out = capsys.readouterr()
    assert json.loads(out.out.splitlines()[0])["daily_pass"] == "failed"
    assert "KeyError" in out.err
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT event_date, state, reason FROM daily_pass_attempts"
    ).fetchall()
    assert rows == [("2026-09-22", "failed", "KeyError: 'registry snapshot'")]


def test_the_resident_sweep_runs_the_daily_pass() -> None:
    """The sweep's one invocation of the ledger, continuation lines joined."""

    script = (ROOT / "scripts" / "paper_ledger_aligned_sweep.sh").read_text(encoding="utf-8")
    lines = [line for line in script.splitlines() if not line.lstrip().startswith("#")]
    start = next(
        i for i, line in enumerate(lines)
        if '"$PY" scripts/paper_ledger_aligned.py' in line
    )
    command = []
    for line in lines[start:]:
        command.append(line.rstrip("\\").strip())
        if not line.rstrip().endswith("\\"):
            break
    words = " ".join(command).split()
    assert "--daily-pass" in words
    assert "--from-event-date" not in words and "--to-event-date" not in words
