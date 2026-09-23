"""Capture lanes do their network work outside any write transaction.

The resident used to count a running capture sweep as holding the writer for
its whole subprocess, so production could not start until the capture exited.
Measured 2026-09-13: capture_noaa_conus_forward ran 1140s and production
waited 1132s behind it -- and almost all of those seconds were HRRR/NBM
downloads that hold no database lock at all.

The resident may release production from waiting on these lanes only because
each of them commits before its next fetch: download and ingest are already
separate at the transaction level, so the SQLite lock is held for the ingest
seconds and nothing else.  These tests hold that precondition, since the
exemption is wrong the day a lane wraps a fetch in an open transaction.
"""

from __future__ import annotations

import ast
import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _daemon():
    spec = importlib.util.spec_from_file_location(
        "_capture_turn_daemon_under_test", ROOT / "scripts" / "weather_basket_yes_daemon.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def daemon():
    return _daemon()


# ------------------------------------------------ the precondition, behaviourally


def test_capture_p1_never_fetches_inside_an_open_write_transaction(tmp_path, monkeypatch):
    """Drive the shared P1 pass across several runs that each write rows, and
    look at the connection at the moment of every fetch."""

    from bayes_basket_target import dwd_icon_eu_point_archive as icon
    from bayes_basket_target import regional_point_lane as lane

    conn = sqlite3.connect(tmp_path / "ssot.db")
    lane.ensure_schema(conn)
    conn.commit()

    seen_in_transaction: list[bool] = []

    def fake_fetch(*, run_init, targets):
        seen_in_transaction.append(conn.in_transaction)
        return {
            (str(t["station_id"]), t["event_date"].isoformat()): {
                "hourly_c": {hour: 18.0 + hour / 10.0 for hour in range(24)},
                "hours_wanted": list(range(24)),
            }
            for t in targets
        }

    monkeypatch.setattr(icon, "fetch_run_day_maxima", fake_fetch)
    monkeypatch.setattr(icon, "file_url", lambda run_init, step: "https://example.invalid/icon")

    receipt = lane.capture_p1(
        conn,
        product="ICON_EU",
        now=datetime(2026, 9, 13, 3, 0, tzinfo=timezone.utc),
        stations=[("EDDM", 48.35, 11.79, "Europe/Berlin")],
        max_runs=3,
    )

    assert len(seen_in_transaction) >= 2, receipt
    assert receipt["written"] >= 1, receipt
    # The fetch after a run that wrote rows is the one that matters: it is
    # where an uncommitted ingest would still be holding the lock.
    assert seen_in_transaction == [False] * len(seen_in_transaction)


# ------------------------------------------------ the precondition, per script


def _insert_executes_are_committed_immediately(path: Path) -> list[int]:
    """Line numbers of INSERT executes NOT directly followed by conn.commit()."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[int] = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for index, stmt in enumerate(body):
            call = stmt.value if isinstance(stmt, ast.Expr) else None
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "execute"
                and call.args
            ):
                continue
            first = call.args[0]
            text = ""
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                text = first.value
            elif isinstance(first, ast.JoinedStr):
                text = "".join(
                    part.value for part in first.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
            if not text.lstrip().upper().startswith("INSERT"):
                continue
            nxt = body[index + 1] if index + 1 < len(body) else None
            committed = (
                isinstance(nxt, ast.Expr)
                and isinstance(nxt.value, ast.Call)
                and isinstance(nxt.value.func, ast.Attribute)
                and nxt.value.func.attr == "commit"
            )
            if not committed:
                offenders.append(stmt.lineno)
    return offenders


@pytest.mark.parametrize(
    "script",
    [
        "scripts/capture_noaa_conus_forward.py",
        "scripts/capture_jma_msm_forward.py",
        "scripts/capture_dwd_icon_eu_forward.py",
        "scripts/capture_deterministic_forward.py",
    ],
)
def test_each_capture_commits_every_insert_before_the_next_fetch(script):
    assert _insert_executes_are_committed_immediately(ROOT / script) == []


# ------------------------------------------------ the exemption itself


def test_the_exemption_covers_exactly_the_capture_lanes_that_meet_the_precondition(daemon):
    captures = {
        "capture_noaa_conus_forward",
        "capture_jma_msm_forward",
        "capture_dwd_icon_eu_forward",
        "capture_cma_grapes_forward",
        # Charter 201: the deterministic family's global members run the same
        # shared pass (``regional_point_lane.capture_p1``), commit before fetch.
        "capture_deterministic_forward",
    }
    assert set(daemon.WRITER_TURN_EXEMPT_SWEEPS) == captures
    hosted = {name for name, _s, _i, _t in daemon.HOSTED_SWEEPS}
    assert captures <= hosted


def test_an_exempt_capture_still_writes_the_ssot_and_never_starts_beside_an_ssot_sweep(daemon):
    """Exempt from the writer gate, not from the database: in one slot a
    capture and another SSOT sweep still never start together."""

    for name in daemon.WRITER_TURN_EXEMPT_SWEEPS:
        assert daemon.hosted_sweep_write_domain(name) == "ssot"
    due = [
        (5.0, 0, "capture_noaa_conus_forward", "a.sh", 1800.0),
        (4.0, 1, "settlement_truth", "b.sh", 1800.0),
    ]
    admitted = daemon.admissible_hosted_sweeps(due, running_names=[])
    assert [item[2] for item in admitted] == ["capture_noaa_conus_forward"]
    # And the other way round: a more overdue SSOT sweep keeps the capture out.
    due = [
        (5.0, 0, "settlement_truth", "b.sh", 1800.0),
        (4.0, 1, "capture_noaa_conus_forward", "a.sh", 1800.0),
    ]
    admitted = daemon.admissible_hosted_sweeps(due, running_names=[])
    assert [item[2] for item in admitted] == ["settlement_truth"]


def test_a_running_capture_holds_the_other_captures_not_the_ssot_queue(daemon):
    """2026-09-14: capture_noaa_conus_forward held the ssot domain 07:41-08:05Z
    while it downloaded, and the ledger went nine hours without a turn."""

    due = [
        (5.0, 0, "capture_jma_msm_forward", "c.sh", 1800.0),
        (4.0, 1, "paper_trading_ledger", "b.sh", 1800.0),
    ]
    admitted = daemon.admissible_hosted_sweeps(
        due, running_names=["capture_noaa_conus_forward"],
    )
    assert [item[2] for item in admitted] == ["paper_trading_ledger"]


def test_no_capture_starts_while_a_long_transaction_ssot_sweep_runs(daemon):
    due = [(9.0, 0, "capture_dwd_icon_eu_forward", "d.sh", 1800.0)]
    assert daemon.admissible_hosted_sweeps(
        due, running_names=["settlement_truth"],
    ) == []


def test_a_running_capture_no_longer_holds_production_out(daemon):
    assert daemon.writer_excluding_sweeps(["capture_noaa_conus_forward"]) == []


def test_a_long_transaction_ssot_sweep_still_holds_production_out(daemon):
    assert daemon.writer_excluding_sweeps(["settlement_truth"]) == ["settlement_truth"]


def test_a_capture_finishing_is_not_the_sweeps_lane_being_served(daemon):
    """2026-09-14: the capture's finish stamped the sweeps lane as served and
    no sweep slot opened for forty-five minutes after it."""

    for name in daemon.WRITER_TURN_EXEMPT_SWEEPS:
        assert daemon.sweep_finish_serves_writer_turn(name) is False
    assert daemon.sweep_finish_serves_writer_turn("paper_trading_ledger") is True
    assert daemon.sweep_finish_serves_writer_turn("forward_packet_retention") is True
