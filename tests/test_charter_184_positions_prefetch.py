"""Charter 184 (user, 2026-09-21: 「挪后台」): the positions read runs in the background.

Each account capture read the data API's ``/value`` in line -- one proxied
round trip, 7-12s of a cycle at 11:45-11:51Z.  A background reader keeps the
latest number; a capture takes it when it is no older than the capture
interval (the capture already reads at most once a minute) and reads in
line otherwise.  When the number was read is recorded with the row.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from bayes_basket_target import account_equity as AE
from tests.test_charter_141_capital_layer import _ledger, _write


@pytest.fixture(autouse=True)
def _no_prefetch_left_running():
    AE._POSITIONS_PREFETCH.shutdown()
    yield
    AE._POSITIONS_PREFETCH.shutdown()


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _source(conn) -> dict:
    return json.loads(conn.execute(f"SELECT source_json FROM {AE.TABLE}").fetchone()[0])


def test_a_fresh_background_read_is_used_and_its_clock_recorded(monkeypatch) -> None:
    started = threading.Event()

    def reader(**_kw):
        started.set()
        return 44.52

    assert AE.ensure_positions_prefetch(user="0xabc", reader=reader, interval=60)
    assert _wait_for(lambda: AE._POSITIONS_PREFETCH.fresh(
        user="0xabc", data_api_host=AE.DEFAULT_DATA_API_HOST, max_age=60) is not None)
    monkeypatch.setattr(AE, "read_positions_value", lambda **_kw: pytest.fail("read in line"))
    conn = _ledger()
    receipt = AE.capture_account_equity(
        conn, as_of_utc="2026-09-16T16:00:00.000Z", write=_write, user="0xabc",
        cash_reader=lambda **k: (55.28, "2026-09-16T15:59:30.000Z"),
    )
    assert receipt["state"] == "recorded"
    assert receipt["positions_value_usdc"] == pytest.approx(44.52)
    observed = _source(conn)["positions_observed_at_utc"]
    assert observed == AE._POSITIONS_PREFETCH.fetched_at_utc and observed.endswith("Z")


def test_a_stale_or_missing_background_read_is_read_in_line(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        AE, "read_positions_value", lambda **_kw: calls.append(1) or 12.0,
    )
    conn = _ledger()
    receipt = AE.capture_account_equity(
        conn, as_of_utc="2026-09-16T16:00:00.000Z", write=_write, user="0xabc",
        cash_reader=lambda **k: (55.28, "2026-09-16T15:59:30.000Z"),
    )
    assert calls == [1]
    assert receipt["positions_value_usdc"] == pytest.approx(12.0)
    assert _source(conn)["positions_observed_at_utc"] == "2026-09-16T16:00:00.000Z"
    # a background number older than the bound is not used either
    AE._POSITIONS_PREFETCH.key = ("0xabc", AE.DEFAULT_DATA_API_HOST)
    AE._POSITIONS_PREFETCH.value = 99.0
    AE._POSITIONS_PREFETCH.fetched_monotonic = (
        time.monotonic() - AE.POSITIONS_PREFETCH_MAX_AGE_SECONDS - 1
    )
    assert AE._POSITIONS_PREFETCH.fresh(
        user="0xabc", data_api_host=AE.DEFAULT_DATA_API_HOST,
        max_age=AE.POSITIONS_PREFETCH_MAX_AGE_SECONDS,
    ) is None


def test_an_injected_reader_is_never_replaced_by_the_background_number() -> None:
    AE._POSITIONS_PREFETCH.key = ("0xabc", AE.DEFAULT_DATA_API_HOST)
    AE._POSITIONS_PREFETCH.value = 99.0
    AE._POSITIONS_PREFETCH.fetched_monotonic = time.monotonic()
    conn = _ledger()
    receipt = AE.capture_account_equity(
        conn, as_of_utc="2026-09-16T16:00:00.000Z", write=_write, user="0xabc",
        cash_reader=lambda **k: (55.28, "2026-09-16T15:59:30.000Z"),
        positions_reader=lambda **k: 44.52,
    )
    assert receipt["positions_value_usdc"] == pytest.approx(44.52)


def test_a_failed_background_read_keeps_the_last_number() -> None:
    stop = threading.Event()
    calls: list[int] = []

    def reader(**_kw):
        calls.append(1)
        if len(calls) == 1:
            return 10.0
        stop.set()
        raise RuntimeError("proxy down")

    prefetch = AE._PositionsPrefetch()
    key = ("0xabc", AE.DEFAULT_DATA_API_HOST)
    prefetch.key = key
    prefetch._loop(key, 0.01, stop, reader)
    assert len(calls) == 2
    assert prefetch.value == 10.0
    assert "proxy down" in prefetch.error


def test_no_funder_address_starts_nothing(monkeypatch) -> None:
    monkeypatch.delenv("POLY_DATA_API_USER", raising=False)
    monkeypatch.delenv("PM_FUNDER_ADDRESS", raising=False)
    assert AE.ensure_positions_prefetch(reader=lambda **_k: 1.0) is False
    assert AE._POSITIONS_PREFETCH.thread is None


def test_only_the_live_resident_starts_the_background_reader(monkeypatch) -> None:
    from bayes_basket_target import production_worker as W

    started = []
    monkeypatch.setattr(W, "ensure_positions_prefetch", lambda **_k: started.append(1) or True)
    monkeypatch.setattr(W, "capture_account_equity", lambda conn, **kw: {"state": "recorded"})
    worker = object.__new__(W.TargetProductionWorker)
    worker.commit_conn = _ledger()
    worker.conn = worker.commit_conn
    monkeypatch.delenv("PM_TARGET_EXECUTION_MODE", raising=False)
    worker._capture_account_equity(as_of_utc="2026-09-16T16:00:00.000Z")
    assert started == []
    monkeypatch.setenv("PM_TARGET_EXECUTION_MODE", "production_live")
    worker._account_equity_last_capture_monotonic = None
    worker._capture_account_equity(as_of_utc="2026-09-16T16:01:00.000Z")
    assert started == [1]
    monkeypatch.setenv("PM_ACCOUNT_POSITIONS_PREFETCH", "0")
    worker._account_equity_last_capture_monotonic = None
    worker._capture_account_equity(as_of_utc="2026-09-16T16:02:00.000Z")
    assert started == [1]
