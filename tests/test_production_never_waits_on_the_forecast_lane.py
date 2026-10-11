"""The forecast prefetch runs beside production; it is not a writer lane.

Between 04:58Z and 06:57Z on 2026-09-15 production waited 2,389s for the
writer over twenty cycles, 1,875s of it behind ``forecast_prefetch``.  The
prefetch had been made a writer lane on 2026-08-26, when it still ingested
TAF rows itself; since then its thread has written no SSOT -- the rotation,
the TAF round and the calibration audit read the store and write JSON files
-- yet "longest ago" handed it every other slot.  The gates below are read
off the run loop's own source, because the loop has no seam to run.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

from scripts import weather_basket_yes_daemon as daemon

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(
    encoding="utf-8"
)


def _main() -> ast.FunctionDef:
    return next(
        node for node in ast.parse(SOURCE).body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )


def _gates_of(submit_text: str) -> list[ast.If]:
    """Every innermost ``if`` whose body submits the named future."""

    return [
        node for node in ast.walk(_main())
        if isinstance(node, ast.If) and any(
            submit_text in ast.unparse(stmt) for stmt in node.body
        ) and not any(
            isinstance(inner, ast.If)
            and inner is not node
            and any(submit_text in ast.unparse(s) for s in inner.body)
            for inner in ast.walk(node)
        )
    ]


def _gate_of(submit_text: str) -> ast.If:
    """The ``if`` whose body submits the named future."""

    for node in ast.walk(_main()):
        if isinstance(node, ast.If) and any(
            submit_text in ast.unparse(stmt) for stmt in node.body
        ) and not any(
            isinstance(inner, ast.If)
            and inner is not node
            and any(submit_text in ast.unparse(s) for s in inner.body)
            for inner in ast.walk(node)
        ):
            return node
    raise AssertionError(submit_text)


def test_the_writer_lanes_are_production_and_the_sweeps() -> None:
    assert daemon.WRITER_LANES == ("production", "sweeps")
    assert "forecast" not in daemon.WRITER_LANES


def test_the_forecast_gate_reads_only_its_own_clock() -> None:
    gate = _gate_of("forecast_executor.submit(")
    condition = ast.unparse(gate.test)
    assert "next_forecast" in condition
    assert "forecast_future is None" in condition
    for foreign in ("writer_turn", "production_future", "writer_excluding_sweeps"):
        assert foreign not in condition, condition


def test_the_production_gate_does_not_wait_for_the_forecast() -> None:
    gate = _gate_of("production_future = executor.submit(")
    condition = ast.unparse(gate.test)
    assert 'writer_turn == \'production\'' in condition
    assert "forecast_future" not in condition, condition


def test_the_sweep_gate_does_not_wait_for_the_forecast() -> None:
    # Two gates submit sweeps since 2026-10-05 (308ab7de): the writer-turn
    # gate and the writer-independent one (due is enough).  Neither waits for
    # the forecast; the writer-turn one is the sweeps lane's turn.
    conditions = [ast.unparse(gate.test) for gate in _gates_of("sweep_executor.submit(")]
    assert len(conditions) == 2, conditions
    assert any("writer_turn == 'sweeps'" in condition for condition in conditions), conditions
    for condition in conditions:
        assert "forecast_future" not in condition, condition


def test_the_forecast_is_never_a_writer_candidate() -> None:
    main = _main()
    appended = {
        ast.unparse(node.args[0])
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append"
        and ast.unparse(node.func.value) == "writer_candidates"
    }
    assert appended == {"'production'", "'sweeps'"}


def test_the_production_floor_still_holds_between_the_two_lanes() -> None:
    served = {"production": 100.0, "sweeps": 50.0}
    # Sweeps were served longer ago, but took the last slot: production goes.
    assert daemon.writer_turn_for(
        ["production", "sweeps"], served, "sweeps"
    ) == "production"
    # ...and after production ran, sweeps get theirs.
    assert daemon.writer_turn_for(
        ["production", "sweeps"], served, "production"
    ) == "sweeps"
    assert daemon.writer_turn_for(["sweeps"], served, "production") == "sweeps"
    assert daemon.writer_turn_for([], served, "") == ""


def test_the_wait_record_names_every_blocker_it_saw() -> None:
    """The last iteration's blocker was the hand-off ('writer_turn:none'),
    not the sweep that had held the writer for the previous seven minutes."""

    main = _main()
    branch = next(
        node for node in ast.walk(main)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "production_ready and production_blockers"
    )
    body = ast.unparse(branch)
    assert "production_wait_blockers = production_blockers" not in body
    assert "production_wait_blockers.append(blocker)" in body


def test_two_threads_cannot_lose_each_others_p0_packets(tmp_path, monkeypatch) -> None:
    """The one shared file both lanes rewrite, now that they overlap: the
    merge is a read-modify-write, and the lock is what keeps one thread's
    packets from being rewritten over by the other's."""

    import bayes_basket_target.production_worker as pw

    path = tmp_path / "p0_inbox.json"
    path.write_text("[]", encoding="utf-8")

    def read_modify_write(target, *, compiled_packets, generated_at_utc):
        import json as _json
        import time as _time

        rows = _json.loads(target.read_text(encoding="utf-8"))
        _time.sleep(0.002)                       # the window a lost update needs
        rows.extend(compiled_packets)
        target.write_text(_json.dumps(rows), encoding="utf-8")
        return {"wrote": True, "accepted_count": len(compiled_packets)}

    monkeypatch.setattr(pw, "_upsert_native_p0_inbox_unlocked", read_modify_write)
    errors: list[BaseException] = []

    def writer(station: str) -> None:
        try:
            for index in range(20):
                pw._upsert_native_p0_inbox(
                    path, compiled_packets=[f"{station}-{index}"],
                    generated_at_utc="2026-09-15T06:00:00Z",
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(s,)) for s in ("KMIA", "EGLC")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    import json as _json

    assert len(_json.loads(path.read_text(encoding="utf-8"))) == 40
