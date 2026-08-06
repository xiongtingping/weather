"""The 10-station ASOS argmax lane must not share a failure boundary with AWC.

The charter requires the side lane's state, budget, intents and receipts to be
partitioned from the main line so that a main-line fault blocks only the main
line.  The lane previously ran at the tail of the AWC canonical ingress
function, on the AWC connection, inside the AWC transaction: a recurring
``disk I/O error`` in AWC ingress therefore silently suppressed every entry fact
and every order for a whole event day.  These tests pin the partition.
"""

import inspect
from pathlib import Path

from scripts import weather_basket_yes_daemon as daemon


def test_awc_ingress_never_materializes_entries_or_reaches_the_gateway() -> None:
    source = inspect.getsource(daemon.run_once)

    assert "materialize_argmax_entry_facts" not in source
    assert "process_pending_argmax_orders" not in source


def test_awc_result_reports_no_order_activity() -> None:
    source = inspect.getsource(daemon.run_once)

    # The AWC lane result must state plainly that it placed nothing, rather
    # than deriving those fields from a side-lane execution result.
    assert '"calls_order_gateway": False' in source
    assert '"places_orders": False' in source
    assert '"orders_submitted": 0' in source


def test_entry_worker_owns_its_own_connection() -> None:
    init_source = inspect.getsource(daemon.AsosArgmaxEntryWorker.__init__)

    assert "_open_runtime" in init_source


def test_entry_worker_runs_entry_and_execution_together() -> None:
    run_source = inspect.getsource(daemon.AsosArgmaxEntryWorker.run)

    # One causal step: a new entry fact must reach the gateway inside the same
    # frozen argmax period that authorized it.
    assert "materialize_argmax_entry_facts" in run_source
    assert "process_pending_argmax_orders" in run_source


def test_entry_worker_can_rebuild_its_connection(tmp_path: Path, monkeypatch) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    old_connection = FakeConnection()
    new_connection = FakeConnection()
    monkeypatch.setattr(daemon, "_open_runtime", lambda _path: new_connection)

    worker = object.__new__(daemon.AsosArgmaxEntryWorker)
    worker.db_path = tmp_path / "archive.db"
    worker.conn = old_connection

    worker.reopen()

    assert old_connection.closed is True
    assert worker.conn is new_connection


def test_execution_runs_even_when_no_entry_fact_was_materialized(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        daemon,
        "materialize_argmax_entry_facts",
        lambda _conn, _stations: (calls.append("entry"), {"ok": True, "inserted_entry_fact_count": 0})[1],
    )
    monkeypatch.setattr(
        daemon,
        "process_pending_argmax_orders",
        lambda _conn, *, root: (calls.append("execution"), {
            "ok": True, "gateway_call_count": 0, "submission_attempt_count": 0,
        })[1],
    )

    worker = object.__new__(daemon.AsosArgmaxEntryWorker)
    worker.conn = object()
    result = worker.run()

    # Prepared intents, unresolved submissions and exits from an earlier cycle
    # must keep converging even on a cycle that produced no new entry fact.
    assert calls == ["entry", "execution"]
    assert result["ok"] is True
    assert result["orders_submitted"] == 0
    assert result["places_orders"] is False


def test_lane_reports_gateway_activity_from_execution(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon, "materialize_argmax_entry_facts", lambda _conn, _stations: {"ok": True}
    )
    monkeypatch.setattr(
        daemon,
        "process_pending_argmax_orders",
        lambda _conn, *, root: {
            "ok": True, "gateway_call_count": 2, "submission_attempt_count": 1,
        },
    )

    worker = object.__new__(daemon.AsosArgmaxEntryWorker)
    worker.conn = object()
    result = worker.run()

    assert result["calls_order_gateway"] is True
    assert result["places_orders"] is True
    assert result["orders_submitted"] == 1


def test_lane_is_not_ok_when_execution_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon, "materialize_argmax_entry_facts", lambda _conn, _stations: {"ok": True}
    )
    monkeypatch.setattr(
        daemon,
        "process_pending_argmax_orders",
        lambda _conn, *, root: {"ok": False, "gateway_call_count": 0, "submission_attempt_count": 0},
    )

    worker = object.__new__(daemon.AsosArgmaxEntryWorker)
    worker.conn = object()

    assert worker.run()["ok"] is False
