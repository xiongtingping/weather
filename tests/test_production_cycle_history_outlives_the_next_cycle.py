"""A slow production cycle's breakdown survives the cycle after it.

2026-09-15: the board reported a 1330s cycle (probability_p0m 469s,
live_observation_identity 240s, probability_p1 180s).  By the time it was
read the status file held a 459s cycle instead, and nothing could say whether
the slow one was the first cycle after the 01:06Z restart or a regression.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def daemon():
    spec = importlib.util.spec_from_file_location(
        "weather_basket_yes_daemon_cycle_history",
        ROOT / "scripts" / "weather_basket_yes_daemon.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(total: float) -> dict:
    return {
        "state": "committed",
        "stage_timings_ms": {
            "cycle_total": total,
            "probability_p0m": 469000.0,
            "live_observation_identity": 240000.0,
            "stage_span_ms": {
                "p0m_scope_loop": {"count": 1, "total_ms": 400000.0},
                "decision_loop_compile": {"count": 90, "total_ms": 21904.7},
            },
        },
    }


def test_a_cycle_row_keeps_its_stages_its_spans_and_where_it_sat_in_the_process(
    daemon,
) -> None:
    row = daemon.production_cycle_history_row(
        _result(1330000.0),
        cycle_index=1,
        process_started_at_utc="2026-09-15T01:06:43Z",
        completed_at_utc="2026-09-15T01:28:49Z",
        wait={"waited_sec": 742.0, "blocked_by": ["forecast_prefetch"]},
    )
    assert row["cycle_total_ms"] == 1330000.0
    assert row["cycle_index_since_start"] == 1
    assert row["stages_ms"] == {
        "probability_p0m": 469000.0,
        "live_observation_identity": 240000.0,
    }
    assert list(row["top_spans_ms"]) == ["p0m_scope_loop", "decision_loop_compile"]
    assert row["waited_sec"] == 742.0
    assert row["wait_blocked_by"] == ["forecast_prefetch"]


def test_a_result_without_timings_is_not_a_cycle_row(daemon) -> None:
    assert daemon.production_cycle_history_row(
        {"state": "blocked_fail_closed"},
        cycle_index=3,
        process_started_at_utc="",
        completed_at_utc="",
    ) is None


def test_the_history_keeps_only_the_newest_rows(daemon, tmp_path) -> None:
    path = tmp_path / "runtime" / "history.jsonl"
    for index in range(7):
        daemon.append_production_cycle_history(
            path, {"cycle_index_since_start": index}, keep=5
        )
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["cycle_index_since_start"] for row in rows] == [2, 3, 4, 5, 6]


def test_every_finished_cycle_is_appended_where_its_receipt_is_kept() -> None:
    source = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(
        encoding="utf-8"
    )
    main = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    assigns_receipt = [
        node for node in ast.walk(main)
        if isinstance(node, ast.Assign)
        and ast.unparse(node.targets[0]) == "state['production']"
        and ast.unparse(node.value) == "production_result"
    ]
    assert len(assigns_receipt) == 1
    calls = {
        node.func.id for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"production_cycle_history_row", "append_production_cycle_history"} <= calls
