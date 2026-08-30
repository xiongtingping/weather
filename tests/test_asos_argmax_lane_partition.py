"""The ASOS argmax lane produces evidence and places no orders.

Operator ruling 2026-08-31: there is no separate ASOS order lane.  The system
has one mainline -- the three-stage P0 / P0-M / P1 basket ladder -- and the
charter always said so ("生产执行唯一为纯 YES 篮子").  The lane had drifted
into being a second one in fact: its own strategy ids, its own intents table,
its own budget, its own gateway admission, and a submission step running every
resident cycle.

What survives is the reason the worker exists at all.  ASOS is a canonical
observation source feeding P1, and it must not share a failure boundary with
AWC: the lane previously ran at the tail of the AWC canonical ingress
function, on the AWC connection, inside the AWC transaction, so a recurring
``disk I/O error`` in AWC ingress silently suppressed every entry fact for a
whole event day.  These tests pin both the partition and the retirement.
"""

import inspect
import re
from pathlib import Path

from scripts import weather_basket_yes_daemon as daemon


_COMMENT = re.compile(r"^\s*#.*$", re.M)


def _code_only(source: str) -> str:
    """Source with comments stripped.

    An assertion about what the worker *calls* must not be satisfied by a
    comment explaining what it no longer calls -- which is exactly how this
    file's execution assertion stayed green through the retirement.
    """
    return _COMMENT.sub("", source)


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


def test_entry_worker_materializes_evidence_and_never_orders() -> None:
    run_source = _code_only(inspect.getsource(daemon.AsosArgmaxEntryWorker.run))

    assert "materialize_argmax_entry_facts" in run_source
    # The order step is gone, not merely disabled behind a flag.
    assert "process_pending_argmax_orders" not in run_source
    assert '"calls_order_gateway": False' in run_source
    assert '"places_orders": False' in run_source
    assert '"orders_submitted": 0' in run_source


def test_the_daemon_does_not_import_the_order_step_at_all() -> None:
    source = _code_only(Path(daemon.__file__).read_text(encoding="utf-8"))

    assert "from asos_argmax_execution import" not in source
    assert "process_pending_argmax_orders" not in source


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


def test_lane_reports_no_order_activity_whatever_the_entry_result(monkeypatch) -> None:
    calls: list[str] = []

    for entry_result in (
        {"ok": True, "inserted_entry_fact_count": 0},
        {"ok": True, "inserted_entry_fact_count": 3},
    ):
        calls.clear()
        monkeypatch.setattr(
            daemon,
            "materialize_argmax_entry_facts",
            lambda _conn, _stations, _r=entry_result: (calls.append("entry"), _r)[1],
        )
        worker = object.__new__(daemon.AsosArgmaxEntryWorker)
        worker.conn = object()
        result = worker.run()

        assert calls == ["entry"]
        assert result["ok"] is True
        assert result["calls_order_gateway"] is False
        assert result["places_orders"] is False
        assert result["orders_submitted"] == 0
        assert result["state"] == "entry_facts_only_order_lane_retired"


def test_lane_is_not_ok_when_entry_materialization_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon, "materialize_argmax_entry_facts", lambda _conn, _stations: {"ok": False}
    )

    worker = object.__new__(daemon.AsosArgmaxEntryWorker)
    worker.conn = object()

    # Evidence is the lane's whole job now, so a failure to materialize it is
    # the lane failing.
    assert worker.run()["ok"] is False
