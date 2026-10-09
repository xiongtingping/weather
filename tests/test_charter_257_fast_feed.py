"""Charter 257 补记三 (user ruling 2026-10-09 「提高跟单线的权重！加一个快速读取通道」):
the cluster's taker fills are polled off the writer into memory every 15 s,
the lane reads them from there when the page is fresh and reaches back past
its cursor, and the production cycle runs the lane at its checkpoints.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target import copy_follow_feed as feed_module  # noqa: E402
from bayes_basket_target import copy_follow_lane as lane  # noqa: E402
from bayes_basket_target import production_worker  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from tests.test_charter_256_copy_follow_lane import USER, _seed, _ts  # noqa: E402
from tests.test_charter_257_copy_follow_v2 import (  # noqa: E402,F401 -- fixtures
    SETTINGS, W2, Cluster, _hermetic, _served, _taker,
)
from tests.test_tiebreak_lane import store  # noqa: E402,F401 -- ``store`` is a fixture


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _feed_fetch(pages, calls=None):
    def fetch(path, params):
        if calls is not None:
            calls.append((path, dict(params)))
        assert path == "/trades" and params["takerOnly"] == "true" and params["offset"] == 0
        page = pages[params["user"]]
        if isinstance(page, Exception):
            raise page
        return sorted(page, key=lambda r: -r["timestamp"])
    return fetch


def test_a_fresh_page_that_reaches_the_cursor_is_served_oldest_first() -> None:
    feed, clock = feed_module.ClusterTradeFeed(), Clock()
    rows = [_taker(at="2026-09-18T11:50:00Z", tx="0x1"), _taker(at="2026-09-18T12:00:00Z", tx="0x2")]
    receipt = feed.poll([USER, W2], fetch=_feed_fetch({USER: rows, W2: RuntimeError("boom")}), clock=clock)
    assert receipt["ok"] == 1 and W2 in receipt["failed"] and receipt["new_fills"] == 2 and feed.version == 1
    got = feed.rows_since(USER, _ts("2026-09-18T11:55:00Z"), clock=clock)
    assert [r["transactionHash"] for r in got] == ["0x2"]
    # A wallet whose read failed has no page: the lane reads the API itself.
    assert feed.rows_since(W2, 0, clock=clock) is None
    # The same page again moves nothing.
    feed.poll([USER], fetch=_feed_fetch({USER: rows}), clock=clock)
    assert feed.version == 1


def test_a_stale_page_or_one_that_may_not_reach_back_is_not_served(monkeypatch) -> None:
    feed, clock = feed_module.ClusterTradeFeed(), Clock()
    feed.poll([USER], fetch=_feed_fetch({USER: [_taker(at="2026-09-18T12:00:00Z")]}), clock=clock)
    clock.t += feed_module.FRESH_SECONDS + 1
    assert feed.rows_since(USER, 0, clock=clock) is None
    monkeypatch.setattr(feed_module, "PAGE_SIZE", 2)
    full = [_taker(at="2026-09-18T12:00:00Z", tx="0x1"), _taker(at="2026-09-18T12:01:00Z", tx="0x2")]
    feed.poll([USER], fetch=_feed_fetch({USER: full}), clock=clock)
    # A full page whose oldest fill is after the cursor may have dropped some.
    assert feed.rows_since(USER, _ts("2026-09-18T11:00:00Z"), clock=clock) is None
    assert len(feed.rows_since(USER, _ts("2026-09-18T12:00:30Z"), clock=clock)) == 1


def test_the_lane_takes_a_covered_wallet_from_the_feed_and_reads_the_rest(store) -> None:
    _seed(store)
    _served(store, {"27°C": 0.2, "28°C": 0.45, "29°C": 0.35})
    feed = feed_module.ClusterTradeFeed()
    feed.poll([USER], fetch=_feed_fetch({USER: [_taker(price=0.30)]}))
    api = Cluster([_taker(user=W2, price=0.30, tx="0xw2", at="2026-09-18T12:01:00Z", bucket="29°C")])
    lane.ensure_lane_schema(store)
    cfg = {**strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS, **SETTINGS}
    receipt = lane.ingest_cluster_taker_trades(store, cfg=cfg, now_utc="2026-09-18T12:02:30.000Z",
                                               fetch=api, feed=feed)
    assert receipt["users"][USER]["source"] == "feed" and receipt["users"][W2]["source"] == "api"
    assert {p["user"] for _, p in api.calls} == {W2}
    assert store.execute(f"SELECT COUNT(*) FROM {lane.SOURCE_TABLE}").fetchone()[0] == 2


def test_the_cycle_checkpoint_runs_the_lane_on_new_fills_and_rechecks_when_idle(monkeypatch) -> None:
    clock = [5000.0]
    monkeypatch.setattr(production_worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(production_worker, "PRODUCTION_EXECUTION_MODE", "production_live")
    monkeypatch.setattr("bayes_basket_target.strategy_lanes.lane_enabled", lambda name, path=None: True)
    feed = feed_module.FEED
    monkeypatch.setattr(feed, "version", 7)
    calls: list = []
    monkeypatch.setattr("bayes_basket_target.copy_follow_lane.execute_copy_follow",
                        lambda *a, **k: calls.append(k.get("feed")) or {"state": "ran"})

    class Conn:
        in_transaction = False

    class Worker:
        commit_conn = Conn()
        canonical_source_conn = None
        market_manifest_path = None

        def _add_span(self, *a, **k):
            pass

    worker = Worker()
    check = production_worker.TargetProductionWorker._copy_follow_if_due
    assert check(worker, "after_regime")["reason"] == "checkpoint:after_regime"
    clock[0] += production_worker.COPY_FOLLOW_CHECK_INTERVAL_SEC + 1
    assert check(worker, "after_market") is None            # nothing new in the feed
    monkeypatch.setattr(feed, "version", 8)
    assert check(worker, "after_market") is not None        # a new fill arrived
    clock[0] += production_worker.COPY_FOLLOW_IDLE_RECHECK_SEC + 1
    assert check(worker, "in_decision_loop") is not None   # idle recheck
    assert calls == [feed, feed, feed]
    worker.commit_conn.in_transaction = True
    clock[0] += 1000
    assert check(worker, "in_decision_loop") is None


def test_the_feed_polls_nothing_while_the_lane_is_off(monkeypatch) -> None:
    import weather_basket_yes_daemon as daemon

    monkeypatch.setattr(daemon, "PRODUCTION_EXECUTION_MODE", "production_live")
    monkeypatch.setattr("bayes_basket_target.strategy_lanes.lane_settings",
                        lambda name, path=None: {"enabled": False, "rule": lane.RULE_V2, "source_users": [USER]})
    assert daemon.copy_follow_feed_users() == []
    monkeypatch.setattr("bayes_basket_target.strategy_lanes.lane_settings",
                        lambda name, path=None: {"enabled": True, "rule": lane.RULE_V2, "source_users": [USER]})
    assert daemon.copy_follow_feed_users() == [USER]


def test_the_lane_runs_ahead_of_the_other_head_lanes_and_reads_the_feed() -> None:
    tree = ast.parse((ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8"))
    cycle = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "run_target_production_cycle")
    order = [n.func.id for n in ast.walk(cycle) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id in {"execute_copy_follow", "execute_tail_short", "_head_execute",
                               "_tail_head_execute", "_consensus_head_execute", "_proper_head_execute"}]
    calls = sorted((n.lineno, n.func.id) for n in ast.walk(cycle) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name) and n.func.id in set(order))
    names = [name for _, name in calls]
    first_copy = names.index("execute_copy_follow")
    assert names[:first_copy] == ["execute_tail_short"]
    for node in ast.walk(cycle):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "execute_copy_follow":
            assert any(k.arg == "feed" for k in node.keywords)
    worker_src = (ROOT / "bayes_basket_target" / "production_worker.py").read_text(encoding="utf-8")
    hook = ast.parse(worker_src)
    sites = [n for n in ast.walk(hook) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "_copy_follow_if_due"]
    assert len(sites) == 3


def test_the_charter_carries_the_ruling() -> None:
    clause = next(line for line in (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(
        encoding="utf-8").splitlines() if line.startswith("257. "))
    for phrase in ("补记三", "提高跟单线的权重", "copy_feed_executor", "_copy_follow_if_due", "不占写者槽"):
        assert phrase in clause
    assert "快读通道" in (ROOT / "AGENTS.md").read_text(encoding="utf-8")
