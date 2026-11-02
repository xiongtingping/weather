"""The production cycle puts probability first and runs no strategy while live
is off (user 2026-11-02 「生产周期里：生产概率（p0、p1）优先！，实盘关闭就跳过策略环节」).
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from pathlib import Path

from bayes_basket_target import production_worker as pw
from bayes_basket_target.production_worker import TargetProductionWorker

STAGES = inspect.getsource(TargetProductionWorker._run_stages)


def _gate(tmp_path, monkeypatch, *, flag: bool, enabled, env: str = ""):
    intent = tmp_path / "live_intent_v1.json"
    if enabled is not None:
        intent.write_text(json.dumps({"live_enabled": enabled}), encoding="utf-8")
    pause = tmp_path / "live_paused.flag"
    if flag:
        pause.write_text("", encoding="utf-8")
    monkeypatch.setattr(pw, "LIVE_INTENT_PATH", intent)
    monkeypatch.setattr(pw, "LIVE_PAUSE_FLAG_PATH", pause)
    monkeypatch.setenv("PM_STRATEGY_STAGES", env)
    return pw._live_strategy_on()


def test_strategy_runs_only_when_armed_and_not_paused(tmp_path, monkeypatch) -> None:
    assert _gate(tmp_path, monkeypatch, flag=False, enabled=True) is True
    assert _gate(tmp_path, monkeypatch, flag=True, enabled=True) is False
    assert _gate(tmp_path, monkeypatch, flag=False, enabled=False) is False
    # An unreadable intent is off, like every other operator gate here.
    assert _gate(tmp_path, monkeypatch, flag=False, enabled=None) is False
    assert _gate(tmp_path, monkeypatch, flag=True, enabled=False, env="always") is True


def _call_line(name: str) -> int:
    tree = ast.parse(textwrap.dedent(inspect.getsource(TargetProductionWorker._run_stages)))
    return min(
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    )


def test_order_housekeeping_runs_behind_the_probability_chain() -> None:
    assert _call_line("_order_housekeeping") > _call_line("_materialize_resident_probability_chain")
    # Not in front of the native P0 compile any more.
    assert "self._reconcile_unresolved_intents(" not in STAGES
    assert "self._flush_cash_phase_forward_observations()" in inspect.getsource(
        TargetProductionWorker._order_housekeeping
    )
    assert "if self._strategy_on:\n                housekeeping = self._order_housekeeping()" in STAGES


def test_live_off_skips_the_plans_and_ends_after_the_market_evidence() -> None:
    assert "if not self._strategy_on:\n                # Basket plans are strategy" in STAGES
    market = STAGES.index('self._mark("market_ingress")')
    live_off = STAGES.index('"state": "probability_only_live_off"')
    assert market < live_off < STAGES.index('self._mark("post_fill_recompute")')
    assert live_off < STAGES.index('self._mark("decision_loop")')
    receipts = pw._skipped_housekeeping("skipped_live_off")
    assert set(receipts) == {
        "account_equity", "intent_reconcile", "resting_reprice", "intent_retire",
        "live_cycle_settle", "cash_phase_forward_observations",
    }
    assert all(r["places_orders"] is False and r["state"] == "skipped_live_off" for r in receipts.values())
