"""Mid-cycle fast-lane checkpoints (user, 2026-09-19: 「极限压低信息到来到bayes概率更新的延迟」).

The head pass waits for the next cycle (~200s).  Three checkpoints inside the
cycle run the same passes again when a TAF or a report landed since the last
pass covered it: after the P0 replay compile, after the forward P0 loop, and
right before the decision loop.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import bayes_basket_target.production_worker as pw


def _worker(tmp_path, *, taf=b"{}", report=(10, 0, "2026-09-19")):
    worker = object.__new__(pw.TargetProductionWorker)
    worker._stage_timings_ms = {}
    worker.taf_inbox_path = tmp_path / "taf_inbox.json"
    worker.taf_inbox_path.write_bytes(taf)
    worker.canonical_source_conn = object()
    worker._report = report
    return worker


@pytest.fixture(autouse=True)
def _report_mark(monkeypatch):
    monkeypatch.setattr(pw, "_live_observation_watermark", lambda conn: _report_mark.value)
    _report_mark.value = (10, 0, "2026-09-19")
    monkeypatch.setattr(pw, "utc_now", lambda: "2026-09-19T03:00:00Z")
    monkeypatch.setattr(pw.TargetProductionWorker, "_mark", lambda self, name: self._stage_timings_ms.__setitem__(name, 1.0), raising=False)


def test_nothing_new_costs_nothing(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    worker._p0m_covered_taf_watermark = pw._taf_inbox_watermark(worker.taf_inbox_path)
    worker._p1_covered_watermark = (10, 0, "2026-09-19")
    monkeypatch.setattr(worker, "_materialize_early_p0m", lambda **k: pytest.fail("no P0-M"))
    monkeypatch.setattr(worker, "_materialize_early_p1", lambda **k: pytest.fail("no P1"))
    assert worker._fast_lane_checkpoint("after_p0_replay") is None
    assert worker._stage_timings_ms == {}


def test_a_new_taf_runs_p0m_and_forces_p1(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    worker._p0m_covered_taf_watermark = ("old",)
    worker._p1_covered_watermark = (10, 0, "2026-09-19")
    calls = []
    monkeypatch.setattr(worker, "_materialize_early_p0m", lambda **k: calls.append(("P0-M", k)) or {"ran": True, "new_envelopes": True})
    monkeypatch.setattr(worker, "_materialize_early_p1", lambda **k: calls.append(("P1", k)) or {"ran": True, "p1": {"state": "COMMITTED"}})
    out = worker._fast_lane_checkpoint("after_forward_p0")
    assert [c[0] for c in calls] == ["P0-M", "P1"] and calls[1][1]["force"] is True
    assert out["label"] == "after_forward_p0"
    assert "fast_lane_after_forward_p0" in worker._stage_timings_ms


def test_a_new_report_runs_p1_and_moves_the_covered_mark_forward(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    worker._p0m_covered_taf_watermark = pw._taf_inbox_watermark(worker.taf_inbox_path)
    worker._p1_covered_watermark = (9, 0, "2026-09-19")
    worker._probability_chain_ran = False
    monkeypatch.setattr(worker, "_materialize_early_p0m", lambda **k: pytest.fail("TAF did not move"))
    head = {"ran": True, "p1": {"state": "COMMITTED"}}
    monkeypatch.setattr(worker, "_materialize_early_p1", lambda **k: head)
    worker._fast_lane_checkpoint("after_p0_replay")
    assert worker._p1_covered_watermark == (10, 0, "2026-09-19")
    # Before the chain, it becomes the cycle's head P1.
    assert worker._early_p1_this_cycle is head


def test_after_the_chain_a_checkpoint_does_not_replace_the_head(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    worker._p0m_covered_taf_watermark = pw._taf_inbox_watermark(worker.taf_inbox_path)
    worker._p1_covered_watermark = (9, 0, "2026-09-19")
    worker._probability_chain_ran = True
    worker._early_p1_this_cycle = {"ran": False}
    monkeypatch.setattr(worker, "_materialize_early_p1", lambda **k: {"ran": True, "p1": {"state": "COMMITTED"}})
    worker._fast_lane_checkpoint("before_decisions")
    assert worker._early_p1_this_cycle == {"ran": False}


def test_a_blocked_p1_covers_nothing(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    worker._p0m_covered_taf_watermark = pw._taf_inbox_watermark(worker.taf_inbox_path)
    worker._p1_covered_watermark = (9, 0, "2026-09-19")
    monkeypatch.setattr(worker, "_materialize_early_p1", lambda **k: {"ran": True, "p1": {"state": "BLOCKED_FAIL_CLOSED"}})
    worker._fast_lane_checkpoint("before_decisions")
    assert worker._p1_covered_watermark == (9, 0, "2026-09-19")


def test_the_checkpoints_are_off_on_the_switch(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pw, "FAST_LANE_CHECKPOINTS_ENABLED", False)
    assert _worker(tmp_path)._fast_lane_checkpoint("after_p0_replay") is None


def test_a_covered_mark_never_moves_back() -> None:
    assert pw._later_report_watermark((12, 3, "d"), (10, 9, "d")) == (12, 3, "d")
    assert pw._later_report_watermark((10, 0, "d"), (12, 0, "d")) == (12, 0, "d")
    assert pw._later_report_watermark(None, (1, 0, "d")) == (1, 0, "d")
    assert pw._later_report_watermark((1, 0, "d"), None) == (1, 0, "d")


def test_every_checkpoint_label_has_a_tier_and_sits_where_it_should() -> None:
    source = Path(pw.__file__).read_text()
    tree = ast.parse(source)
    labels = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_fast_lane_checkpoint" and node.args
                and isinstance(node.args[0], ast.Constant)):
            labels.append((node.lineno, node.args[0].value))
    labels.sort()
    assert [label for _line, label in labels] == ["after_p0_replay", "after_forward_p0", "before_decisions"]
    for _line, label in labels:
        assert pw.STAGE_TIER[f"fast_lane_{label}"] == pw.STAGE_TIER_R0_DECISION
