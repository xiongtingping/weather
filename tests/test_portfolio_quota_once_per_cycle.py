"""The forward loop's portfolio quota is computed once per cycle.

It was asked for once per forward scope -- 61 times a cycle -- and each
answer rebuilt the same inventory from the same ledger and persisted it:
10.5s of a 202s cycle at the median, 54.7s at the worst, and ten to
seventeen thousand inventory snapshot rows a day.
"""

import ast
import inspect
import textwrap

from bayes_basket_target import production_worker as pw


def _worker():
    worker = object.__new__(pw.TargetProductionWorker)
    worker._begin_cycle_scratch()
    return worker


def test_the_same_stations_are_priced_once(monkeypatch) -> None:
    worker = _worker()
    calls = []

    def uncached(self, *, stations, fallback_budget_usdc, as_of_utc):
        calls.append((tuple(stations), as_of_utc))
        return {station: 1.0 for station in stations}

    monkeypatch.setattr(pw.TargetProductionWorker, "_portfolio_quota_uncached", uncached)
    first = worker._portfolio_quota(
        stations=["KLAX", "EPWA"], fallback_budget_usdc=20.0, as_of_utc="t1"
    )
    second = worker._portfolio_quota(
        stations=["EPWA", "klax"], fallback_budget_usdc=20.0, as_of_utc="t2"
    )
    assert first == second == {"KLAX": 1.0, "EPWA": 1.0}
    assert len(calls) == 1
    assert worker._portfolio_quota_cycle_memo["reused"] == 1
    # The answer handed out is a copy: a caller cannot edit the memo.
    second["KLAX"] = 0.0
    assert worker._portfolio_quota(
        stations=["KLAX", "EPWA"], fallback_budget_usdc=20.0, as_of_utc="t3"
    )["KLAX"] == 1.0


def test_a_different_station_list_or_budget_is_priced_again(monkeypatch) -> None:
    worker = _worker()
    calls = []
    monkeypatch.setattr(
        pw.TargetProductionWorker, "_portfolio_quota_uncached",
        lambda self, *, stations, fallback_budget_usdc, as_of_utc: (
            calls.append(1) or {s: 1.0 for s in stations}
        ),
    )
    worker._portfolio_quota(stations=["KLAX"], fallback_budget_usdc=20.0, as_of_utc="t")
    worker._portfolio_quota(stations=["KLAX", "KMIA"], fallback_budget_usdc=20.0, as_of_utc="t")
    worker._portfolio_quota(stations=["KLAX", "KMIA"], fallback_budget_usdc=25.0, as_of_utc="t")
    assert len(calls) == 3


def test_the_next_cycle_starts_without_the_memo(monkeypatch) -> None:
    worker = _worker()
    calls = []
    monkeypatch.setattr(
        pw.TargetProductionWorker, "_portfolio_quota_uncached",
        lambda self, **kwargs: calls.append(1) or {},
    )
    worker._portfolio_quota(stations=["KLAX"], fallback_budget_usdc=20.0, as_of_utc="t")
    worker._begin_cycle_scratch()
    worker._portfolio_quota(stations=["KLAX"], fallback_budget_usdc=20.0, as_of_utc="t")
    assert len(calls) == 2
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
