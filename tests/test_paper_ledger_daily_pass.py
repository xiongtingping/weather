"""The paper ledger's daily pass: every owed settled day, each attempted once.

2026-09-25: the pass re-walked seven event days every day -- ~12,000 basket
decisions, each a production compile at 1-5 s of SSOT reads on this machine --
against a 90-minute slot; it was stopped at 69 minutes, stored nothing, and
the lane sat red (user: 「没有会话在改，你改」).  Then a pass took one day and
asked the resident for the next.

2026-09-26 (user: 「纸面账本不要阻断生产。失败即跳过。」): the pass is its own
LaunchAgent, not a resident sweep; it walks the owed days oldest first, each
attempted once -- an error, its deadline, SIGTERM or a kill skips the day with
the reason and the pass goes on -- and between decisions it gives way to an
overdue production cycle or a WAL past the resident's truncate line.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import paper_ledger_aligned as L  # noqa: E402

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


def _attempt(path: Path, day: str, state: str, at: str = "2026-09-26T05:00:00+00:00",
             reason: str | None = None) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "INSERT INTO daily_pass_attempts VALUES(?,?,?,?,?)", (day, at, state, None, reason),
        )
    conn.close()


def _attempts(path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(path)
    try:
        return [
            (str(day), str(state)) for day, state in conn.execute(
                "SELECT event_date, state FROM daily_pass_attempts ORDER BY rowid"
            )
        ]
    finally:
        conn.close()


def _args(path: Path, tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        ledger_db=str(path), window_days=3, dry_run=False, since_rowid=0,
        until_rowid=None, stations="", limit=0, label="", from_event_date=None,
        to_event_date=None, pass_deadline_sec=L.DAILY_PASS_DEADLINE_SEC,
        status_json=str(tmp_path / "status.json"),
    )


def test_the_window_is_the_settled_days_oldest_first(tmp_path) -> None:
    pending, walked, skipped = L.daily_pass_plan(
        tmp_path / "absent.db", today=TODAY, window_days=3,
    )
    assert pending == WINDOW and walked == [] and skipped == {}


def test_a_day_a_daily_pass_stored_is_not_walked_again(tmp_path) -> None:
    path = _ledger(tmp_path)
    _stored_run(path, "2026-09-22")
    # A hand run over the same day under another label does not count.
    _stored_run(path, "2026-09-23", label="arm:base")
    pending, walked, _ = L.daily_pass_plan(path, today=TODAY, window_days=3)
    assert pending == ["2026-09-23", "2026-09-24"]
    assert walked == ["2026-09-22"]


def test_a_day_is_attempted_once(tmp_path) -> None:
    """失败即跳过: a skipped day is never owed again, and neither is one whose
    walk started and has not said how it ended."""

    path = _ledger(tmp_path)
    _attempt(path, "2026-09-22", "started")
    _attempt(path, "2026-09-22", "skipped", reason="KeyError: 'x'")
    _attempt(path, "2026-09-23", "started")
    pending, _, skipped = L.daily_pass_plan(path, today=TODAY, window_days=3)
    assert pending == ["2026-09-24"]
    assert skipped == {"2026-09-22": "KeyError: 'x'"}


def test_a_pass_walks_every_owed_day_oldest_first(tmp_path, monkeypatch, capsys) -> None:
    path = _ledger(tmp_path)
    walked: list[tuple[str, str, str]] = []

    def fake_run(args):
        walked.append((args.from_event_date, args.to_event_date, args.label))
        assert args.deadline_monotonic is not None
        _stored_run(path, args.from_event_date)
        return {"run_id": f"run_{args.from_event_date}", "seconds": 1.0}

    monkeypatch.setattr(L, "run", fake_run)
    assert L.daily_passes(_args(path, tmp_path), today=TODAY) == 0
    assert walked == [(day, day, L.DAILY_PASS_LABEL) for day in WINDOW]
    assert _attempts(path) == [
        (day, state) for day in WINDOW for state in ("started", "stored")
    ]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["daily_pass"] for line in lines] == ["stored", "stored", "stored", "up_to_date"]
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["daily_pass"] == "up_to_date" and status["walked"] == WINDOW
    # Nothing owed: a no-op that still succeeds.
    assert L.daily_passes(_args(path, tmp_path), today=TODAY) == 0
    assert walked == [(day, day, L.DAILY_PASS_LABEL) for day in WINDOW]


def test_a_failed_day_is_skipped_with_its_reason_and_the_pass_goes_on(
    tmp_path, monkeypatch, capsys,
) -> None:
    path = _ledger(tmp_path)
    walked: list[str] = []

    def run_that_breaks_on_the_first_day(args):
        walked.append(args.from_event_date)
        if args.from_event_date == WINDOW[0]:
            raise KeyError("registry snapshot")
        _stored_run(path, args.from_event_date)
        return {"run_id": f"run_{args.from_event_date}", "seconds": 1.0}

    monkeypatch.setattr(L, "run", run_that_breaks_on_the_first_day)
    assert L.daily_passes(_args(path, tmp_path), today=TODAY) == 0
    assert walked == WINDOW
    out = capsys.readouterr()
    first = json.loads(out.out.splitlines()[0])
    assert first == {
        "daily_pass": "skipped", "event_date": WINDOW[0],
        "reason": "KeyError: 'registry snapshot'", "remaining": WINDOW[1:],
    }
    assert "KeyError" in out.err
    # Never retried: the next pass owes nothing.
    assert L.daily_passes(_args(path, tmp_path), today=TODAY) == 0
    assert walked == WINDOW
    _, _, skipped = L.daily_pass_plan(path, today=TODAY, window_days=3)
    assert skipped == {WINDOW[0]: "KeyError: 'registry snapshot'"}


def test_the_deadline_skips_the_day(tmp_path, monkeypatch) -> None:
    path = _ledger(tmp_path)

    def slow_run(args):
        raise L.PassDeadline("deadline reached after 812 decisions (0s given way to production)")

    monkeypatch.setattr(L, "run", slow_run)
    args = _args(path, tmp_path)
    args.window_days = 1
    assert L.daily_passes(args, today=TODAY) == 0
    _, _, skipped = L.daily_pass_plan(path, today=TODAY, window_days=1)
    assert skipped == {"2026-09-24": "PassDeadline: deadline reached after 812 decisions"
                       " (0s given way to production)"}


def test_sigterm_skips_the_day_in_hand_and_ends_the_pass(tmp_path, monkeypatch) -> None:
    path = _ledger(tmp_path)
    walked: list[str] = []

    def terminated_run(args):
        walked.append(args.from_event_date)
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("the handler raises before this line")

    monkeypatch.setattr(L, "run", terminated_run)
    before = signal.getsignal(signal.SIGTERM)
    assert L.daily_passes(_args(path, tmp_path), today=TODAY) == 0
    assert walked == [WINDOW[0]]
    assert signal.getsignal(signal.SIGTERM) == before
    _, _, skipped = L.daily_pass_plan(path, today=TODAY, window_days=3)
    assert skipped == {WINDOW[0]: "PassTerminated: terminated (SIGTERM)"}


def test_a_walk_that_died_without_a_word_is_closed_as_skipped(tmp_path) -> None:
    path = _ledger(tmp_path)
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    deadline = 3600.0
    old = (now - timedelta(seconds=deadline + L.DEAD_WALK_GRACE_SEC + 1)).isoformat()
    recent = (now - timedelta(seconds=60)).isoformat()
    _attempt(path, "2026-09-22", "started", at=old)
    _attempt(path, "2026-09-23", "started", at=recent)
    _attempt(path, "2026-09-24", "started", at=old)
    _attempt(path, "2026-09-24", "stored", at=old)
    assert L.close_dead_walks(path, deadline_sec=deadline, now=now) == ["2026-09-22"]
    assert L.close_dead_walks(path, deadline_sec=deadline, now=now) == []
    _, _, skipped = L.daily_pass_plan(path, today=TODAY, window_days=3)
    assert list(skipped) == ["2026-09-22"]
    assert "killed" in skipped["2026-09-22"]


def test_a_dry_run_records_nothing_and_walks_one_day(tmp_path, monkeypatch) -> None:
    path = _ledger(tmp_path)
    walked: list[str] = []
    monkeypatch.setattr(L, "run", lambda args: walked.append(args.from_event_date) or {})
    args = _args(path, tmp_path)
    args.dry_run = True
    assert L.daily_passes(args, today=TODAY) == 0
    assert walked == [WINDOW[0]]
    assert _attempts(path) == []
    assert not (tmp_path / "status.json").exists()


# ---------------------------------------------------------------------------
# Giving way to production.
# ---------------------------------------------------------------------------


@pytest.fixture
def resident(tmp_path, monkeypatch):
    """A resident store and status file under tmp_path."""

    ssot = tmp_path / "ssot.db"
    ssot.write_bytes(b"")
    status = tmp_path / "status.json"
    monkeypatch.setattr(L, "SSOT_DB", ssot)
    monkeypatch.setattr(L, "RESIDENT_STATUS", status)

    def write(*, overdue: bool, heartbeat_age_sec: float = 10.0) -> None:
        beat = datetime.now(timezone.utc) - timedelta(seconds=heartbeat_age_sec)
        status.write_text(json.dumps({
            "heartbeat_at": beat.isoformat().replace("+00:00", "Z"),
            "production_inflight": {"elapsed_sec": 800.0, "overdue": overdue},
        }))

    return ssot, write


def test_the_walk_waits_while_a_production_cycle_is_overdue(resident) -> None:
    ssot, write = resident
    write(overdue=False)
    assert L.production_blocker(ssot) is None
    write(overdue=True)
    assert L.production_blocker(ssot) == "production cycle overdue"
    # A status file too old to say is not a reason to wait.
    write(overdue=True, heartbeat_age_sec=L.RESIDENT_STATUS_FRESH_SEC + 60)
    assert L.production_blocker(ssot) is None


def test_only_the_residents_store_waits_on_its_cycle(resident, tmp_path) -> None:
    _ssot, write = resident
    write(overdue=True)
    copy = tmp_path / "copy.db"
    copy.write_bytes(b"")
    assert L.production_blocker(copy) is None


def test_the_walk_waits_while_the_wal_is_past_the_line(resident, monkeypatch) -> None:
    ssot, write = resident
    write(overdue=False)
    monkeypatch.setattr(L, "YIELD_WAL_BYTES", 10)
    Path(f"{ssot}-wal").write_bytes(b"x" * 11)
    assert L.production_blocker(ssot) == "wal 11"


def test_the_gate_holds_until_production_is_clear(monkeypatch) -> None:
    answers = iter(["production cycle overdue", "production cycle overdue", None])
    monkeypatch.setattr(L, "production_blocker", lambda _path: next(answers))
    slept: list[float] = []
    clock = [0.0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(L.time, "sleep", sleep)
    monkeypatch.setattr(L.time, "monotonic", lambda: clock[0])
    gate = L.ProductionGate("ssot.db")
    gate.wait(walked=0)
    assert slept == [L.YIELD_POLL_SEC, L.YIELD_POLL_SEC]
    assert gate.waited_sec == 2 * L.YIELD_POLL_SEC


def test_the_gate_raises_at_the_deadline_even_while_waiting(monkeypatch) -> None:
    monkeypatch.setattr(L, "production_blocker", lambda _path: "production cycle overdue")
    clock = [0.0]
    monkeypatch.setattr(L.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(L.time, "monotonic", lambda: clock[0])
    gate = L.ProductionGate("ssot.db", deadline=12.0)
    with pytest.raises(L.PassDeadline, match="after 7 decisions"):
        gate.wait(walked=7)
    assert clock[0] >= 12.0


# ---------------------------------------------------------------------------
# The sweep script.
# ---------------------------------------------------------------------------

SWEEP = ROOT / "scripts" / "paper_ledger_aligned_sweep.sh"


def _ledger_invocation() -> list[str]:
    """The sweep's one invocation of the ledger, continuation lines joined."""

    lines = [
        line for line in SWEEP.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    start = next(
        i for i, line in enumerate(lines)
        if '"$PY" scripts/paper_ledger_aligned.py' in line
    )
    command = []
    for line in lines[start:]:
        command.append(line.rstrip("\\").strip())
        if not line.rstrip().endswith("\\"):
            break
    return " ".join(command).split()


def test_the_sweep_runs_the_daily_pass_with_its_deadline() -> None:
    words = _ledger_invocation()
    assert "--daily-pass" in words and "--pass-deadline-sec" in words
    assert "--from-event-date" not in words and "--to-event-date" not in words


def test_the_sweep_refuses_a_start_handed_a_writer_lease() -> None:
    """The resident's hosted start: exits at once, before any python runs."""

    env = {"PATH": "/usr/bin:/bin", "PM_WRITER_LEASE_SEC": "240", "HOME": str(ROOT)}
    result = subprocess.run(
        ["/bin/bash", str(SWEEP)], env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert "com.weather.paper_ledger_aligned" in result.stdout
    assert "weekly re-pin" not in result.stdout
