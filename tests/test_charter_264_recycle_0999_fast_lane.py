"""Charter 264 (user rulings 2026-10-09 「把我的卖出车道0.998也改为0.999」 and
「把其他策略移除快车道」): the recycle lane sells at 0.999 and retires its
0.998 offers first; the cycle's mid-cycle fast lane runs the copy-follow lane
alone, reading the fast feed only.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import certain_win_recycle as recycle  # noqa: E402
from bayes_basket_target import copy_follow_lane as copy_lane  # noqa: E402
from bayes_basket_target import exit_orders  # noqa: E402
from bayes_basket_target import production_worker  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from tests.test_certain_win_recycle import ED, NOW, ST, TZ, _armed, _plans, _store  # noqa: E402
from tests.test_charter_256_copy_follow_lane import _seed  # noqa: E402
from tests.test_charter_257_copy_follow_v2 import SETTINGS, Cluster, _hermetic, _taker  # noqa: E402,F401
from tests.test_tiebreak_lane import store  # noqa: E402,F401 -- ``store`` is a fixture


def test_the_recycle_price_is_0999_on_a_fine_tick() -> None:
    assert recycle.RECYCLE_PRICE == 0.999 and recycle.recycle_price(0.001) == 0.999
    assert recycle.RULE_VERSION == "certain_win_recycle_resting_0999_v6"
    with pytest.raises(ValueError):
        recycle.recycle_price(0.01)


def test_a_standing_0998_offer_is_retired_before_its_shares_are_reoffered(monkeypatch) -> None:
    conn, canonical = _store(holdings={"25°C": 10.0})
    _armed(monkeypatch)
    exit_orders.send_exit_sell(
        conn, lane="recycle998", intent_id="recycle998_0998_test",
        station_id=ST, event_date=ED, settlement_timezone=TZ,
        bucket="25°C", token_id="t25", price=0.998, shares=10.0,
        order_type="GTC", post_only=False, tick_size=0.001,
        intent_body={"rule": "certain_win_recycle_resting_0998_v5"},
        transport=lambda _path, _payload: {"ok": True, "orderID": "0x998", "status": "live"},
        live_submit_authorized=True,
    )
    assert _plans(conn, canonical) == []      # still standing: its shares are reserved

    class Client:
        def cancel_order(self, payload):
            assert payload["idempotency_key"].endswith(":cancel:recycle_0999_v6")
            return {"ok": True}

        def lookup_exchange_order(self, _payload):
            return {"ok": True, "status": "canceled", "response": {"id": "0x998", "status": "canceled",
                                                                   "size_matched": 0}}

    receipt = recycle.retire_old_price_orders(conn, now_utc=NOW, client=Client())
    assert receipt["closed"] == 1
    plans = _plans(conn, canonical)
    assert [(p["bucket"], p["limit_price"], p["shares"]) for p in plans if p["bucket"] == "25°C"] == [
        ("25°C", 0.999, 10.0)]


def test_the_copy_follow_lane_is_the_fast_lanes_one_strategy() -> None:
    source = Path(production_worker.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    lane_entries = set()
    for module in list((ROOT / "bayes_basket_target").glob("*_lane.py")) + [ROOT / "bayes_basket_target" / "certain_win_recycle.py"]:
        lane_entries |= {n.name for n in ast.parse(module.read_text(encoding="utf-8")).body
                         if isinstance(n, ast.FunctionDef) and n.name.startswith("execute_")}
    assert {"execute_tail_short", "execute_p1_tail_entries", "execute_copy_follow"} <= lane_entries
    called = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} & lane_entries
    assert called == {"execute_copy_follow"}
    checkpoint = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "_fast_lane_checkpoint_if_due")
    hooks = [n.func.attr for n in ast.walk(checkpoint)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr.endswith("_if_due")]
    assert hooks == ["_copy_follow_if_due"]
    hook = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_copy_follow_if_due")
    kw = {k.arg: k.value for n in ast.walk(hook) if isinstance(n, ast.Call)
          and getattr(n.func, "id", None) == "execute_copy_follow" for k in n.keywords}
    assert isinstance(kw["api_fallback"], ast.Constant) and kw["api_fallback"].value is False


def test_mid_cycle_reads_the_feed_only(store) -> None:
    _seed(store)
    copy_lane.ensure_lane_schema(store)
    api = Cluster([_taker(price=0.30)])
    cfg = {**strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS, **SETTINGS}
    receipt = copy_lane.ingest_cluster_taker_trades(store, cfg=cfg, now_utc="2026-09-18T12:02:30.000Z",
                                                    fetch=api, feed=None, api_fallback=False)
    assert api.calls == []
    assert {u: r["state"] for u, r in receipt["users"].items()} == {u: "feed_not_covering" for u in cfg["source_users"]}


def test_the_charter_carries_both_rulings() -> None:
    clause = next(line for line in (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(
        encoding="utf-8").splitlines() if line.startswith("264. "))
    for phrase in ("把我的卖出车道0.998也改为0.999", "把其他策略移除快车道", "RECYCLE_PRICE", "api_fallback"):
        assert phrase in clause
