"""The portfolio book behind the forward loop's quota is read once per cycle.

The forward loop asks for a quota once per scope, each time with that
scope's single station -- 61 times a cycle at the 2026-09-19 median -- and
every ask rebuilt the same inventory from the same ledger and persisted it:
10.5s of a 202s cycle (54.7s at the worst) and ten to seventeen thousand
inventory rows a day.  The book half does not depend on the station; the
station half (edge, allocation) is computed per call exactly as before, so
each scope's quota is the one it always got.

(The first cut keyed a whole-answer memo on the station list.  Every scope
asks with a different single station, so it never hit in production; the
test below that asks one station at a time is the one that would have said
so.)
"""

import ast
import inspect
import textwrap

from bayes_basket_target import production_worker as pw

INVENTORY = {
    "open_notional_usdc": 3.0, "portfolio_hhi": 0.2, "breaches": [],
    "new_intents_allowed": True, "drawdown": 0.0, "bankroll_usdc": 100.0,
}


def _worker(monkeypatch, *, inventory=None):
    worker = object.__new__(pw.TargetProductionWorker)
    worker._begin_cycle_scratch()
    worker.commit_conn = object()
    reads = {"inventory": 0, "persisted": 0, "edges": [], "allocations": []}

    class _Conn:
        def execute(self, sql, params=()):
            class _Row:
                def fetchone(self_inner):
                    return ('{"cash_clip_grid": [1.0, 2.0]}',)
            return _Row()

    worker.conn = _Conn()
    monkeypatch.setattr(pw, "load_regional_blocks", lambda: ({}, "b" * 64))
    monkeypatch.setattr(pw, "load_portfolio_capital_policy", lambda: (
        {"portfolio_risk_fraction": 0.1, "station_cap_fraction": 0.5}, "p" * 64))
    monkeypatch.setattr(pw, "_resolve_target_remaining_budget", lambda: 20.0)

    def build(conn, **kwargs):
        reads["inventory"] += 1
        return dict(inventory or INVENTORY)

    monkeypatch.setattr(pw, "build_portfolio_inventory", build)
    monkeypatch.setattr(pw, "persist_portfolio_inventory",
                        lambda conn, inv, created_at_utc: reads.__setitem__(
                            "persisted", reads["persisted"] + 1))
    monkeypatch.setattr(pw.TargetProductionWorker, "_station_edge_evidence",
                        lambda self, *, stations, minimum_clip_usdc: (
                            reads["edges"].append((tuple(stations), minimum_clip_usdc))
                            or {s: {"edge": 0.1} for s in stations}))

    def allocate(*, station_edges, **kwargs):
        reads["allocations"].append(tuple(station_edges))
        quota = {s: 7.0 for s in station_edges}
        return {"quota_usdc_by_station": quota, "funded_station_count": len(quota),
                "station_count": len(quota), "allocated_usdc": 7.0 * len(quota),
                "bankroll_usdc": 100.0, "portfolio_sigma_usdc": 1.0,
                "naive_sigma_usdc": 1.0}

    monkeypatch.setattr(pw, "allocate_portfolio_quota", allocate)
    monkeypatch.setattr(pw, "persist_portfolio_allocation", lambda *a, **k: None)
    monkeypatch.setattr(pw, "derive_minimum_bankroll", lambda **k: 0.0)
    return worker, reads


def test_one_station_at_a_time_reads_the_book_once(monkeypatch) -> None:
    worker, reads = _worker(monkeypatch)
    quotas = [
        worker._portfolio_quota(stations=[station], fallback_budget_usdc=20.0,
                                as_of_utc=f"2026-09-21T07:0{i}:00Z")
        for i, station in enumerate(["KLAX", "EPWA", "ZUUU", "KLAX"])
    ]
    assert quotas == [{"KLAX": 7.0}, {"EPWA": 7.0}, {"ZUUU": 7.0}, {"KLAX": 7.0}]
    assert reads["inventory"] == 1
    assert reads["persisted"] == 1
    # The station half still runs on every ask, with the book's clip.
    assert [e[0] for e in reads["edges"]] == [("KLAX",), ("EPWA",), ("ZUUU",), ("KLAX",)]
    assert {e[1] for e in reads["edges"]} == {1.0}
    assert len(reads["allocations"]) == 4
    assert worker._portfolio_book_cycle_memo["reused"] == 3
    assert worker._portfolio_book_cycle_memo["read_at_utc"] == "2026-09-21T07:00:00Z"


def test_a_breached_book_zeroes_every_ask_without_rereading(monkeypatch) -> None:
    breached = dict(INVENTORY, new_intents_allowed=False, breaches=["tier1"])
    worker, reads = _worker(monkeypatch, inventory=breached)
    for station in ("KLAX", "EPWA"):
        assert worker._portfolio_quota(stations=[station], fallback_budget_usdc=20.0,
                                       as_of_utc="t") == {station: 0.0}
    assert reads["inventory"] == 1
    assert reads["edges"] == []
    assert worker._portfolio_allocation_last["state"] == "PORTFOLIO_BOUND_BREACHED"


def test_a_failed_book_is_read_again_on_the_next_ask(monkeypatch) -> None:
    worker, reads = _worker(monkeypatch)
    calls = {"n": 0}

    def flaky(conn, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("ledger read failed")
        return dict(INVENTORY)

    monkeypatch.setattr(pw, "build_portfolio_inventory", flaky)
    assert worker._portfolio_quota(stations=["KLAX"], fallback_budget_usdc=20.0,
                                   as_of_utc="t") == {}
    assert worker._portfolio_allocation_last["state"] == "ALLOCATION_UNAVAILABLE"
    assert worker._portfolio_quota(stations=["KLAX"], fallback_budget_usdc=20.0,
                                   as_of_utc="t") == {"KLAX": 7.0}
    assert calls["n"] == 2


def test_the_next_cycle_reads_the_book_again(monkeypatch) -> None:
    worker, reads = _worker(monkeypatch)
    worker._portfolio_quota(stations=["KLAX"], fallback_budget_usdc=20.0, as_of_utc="t")
    worker._begin_cycle_scratch()
    worker._portfolio_quota(stations=["KLAX"], fallback_budget_usdc=20.0, as_of_utc="t")
    assert reads["inventory"] == 2
    assert worker._fast_lane_cycle_spent_sec == 0.0
    assert worker._fast_lane_budget_skip_noted is False


def test_run_opens_every_cycle_by_forgetting_the_scratch() -> None:
    # By AST, not by string: the call has to be in ``run``'s own body.
    tree = ast.parse(textwrap.dedent(inspect.getsource(pw.TargetProductionWorker.run)))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    }
    assert "_begin_cycle_scratch" in calls


def test_the_forward_loop_still_asks_with_one_station_per_scope() -> None:
    # The reason the memo sits on the book and not on the answer: if the
    # loop ever starts asking with the whole fleet, this pin moves with it.
    source = inspect.getsource(pw.TargetProductionWorker.materialize_forward_p0_preselections)
    assert "if value == requested_station" in source
