"""Charter 130: one economic hurdle, four evaluation points (ruled "5%").

The same expectation used to be asked three times with three pass marks --
zero at admission, five percent at execution, zero on a completion's
increment -- and the seats that cleared the first and failed the second
blocked their station-day for cycles (147 refusals over 48 station-days in
three days).  Now there is one constant, one kernel inequality, and four
places it is read: the window at admission, the lifted bundle at execution,
the incumbent each cycle, and a completion's increment.  The station quota
reads the same belief the gates read.

These tests hold the constant, the arithmetic, the four sites (off the
syntax tree), the retired knobs, the ledger's fingerprint, the allocator's
belief, and the clause -- and one behavioural pair: a +6% window seats, a
+4% window does not, and at ``PM_ECONOMIC_HURDLE=0`` it does again, so the
pre-130 book stays rebuildable.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target import delta_v2_runtime as R  # noqa: E402
from bayes_basket_target.basket_kernel import delta_v2 as K  # noqa: E402

KERNEL_SRC = (ROOT / "bayes_basket_target" / "basket_kernel" / "delta_v2.py").read_text(encoding="utf-8")
RUNTIME_SRC = (ROOT / "bayes_basket_target" / "delta_v2_runtime.py").read_text(encoding="utf-8")
EXECUTION_SRC = (ROOT / "bayes_basket_target" / "runtime.py").read_text(encoding="utf-8")
WORKER_SRC = (ROOT / "bayes_basket_target" / "production_worker.py").read_text(encoding="utf-8")
LEDGER_SRC = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(encoding="utf-8")


def _calls(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == name) or (
                isinstance(f, ast.Attribute) and f.attr == name
            ):
                yield node


def _strings(node: ast.AST) -> set[str]:
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def test_the_constant_and_the_inequality() -> None:
    assert K.ECONOMIC_HURDLE_V1 == 0.05
    assert K.BASKET_TARGET_NET_ROI is K.ECONOMIC_HURDLE_V1
    assert K.ECONOMIC_HURDLE_RULE_VERSION == "one_hurdle_four_evaluation_points_v1"
    c = K.clears_economic_hurdle_v1
    assert c(expected_payout=1.05, cost=1.0)
    assert not c(expected_payout=1.049, cost=1.0)
    assert not c(expected_payout=1.0, cost=0.0)
    assert c(expected_payout=1.0, cost=1.0, hurdle=0.0)
    assert K.economic_hurdle_usdc(6.30) == pytest.approx(0.315)


def test_the_execution_reading_is_the_same_inequality() -> None:
    """``meets_target`` is the predicate on the structure's own numbers."""

    structure = K.basket_return_structure_v1(
        shares_by_bucket={"a": 5.0, "b": 5.0},
        price_by_bucket={"a": 0.30, "b": 0.33},
        claim_by_bucket={"a": 0.35, "b": 0.30},
        support=("a", "b"),
    )
    expected_payout = 0.35 * 5.0 + 0.30 * 5.0
    cost = 5.0 * 0.30 + 5.0 * 0.33
    assert structure["meets_target"] == K.clears_economic_hurdle_v1(
        expected_payout=expected_payout, cost=cost,
    )
    assert structure["equal_basket"]["meets_target"] == K.clears_economic_hurdle_v1(
        expected_payout=0.65, cost=0.63,
    )
    # And the execution layer hands the structure no other target.
    tree = ast.parse(EXECUTION_SRC)
    calls = list(_calls(tree, "basket_return_structure_v1"))
    assert calls
    assert all(all(kw.arg != "target_net_roi" for kw in call.keywords) for call in calls)


def test_the_four_evaluation_points_read_the_predicate() -> None:
    tree = ast.parse(RUNTIME_SRC)
    predicate_calls = list(_calls(tree, "clears_economic_hurdle_v1"))
    # Gate, gate's lambda counter, incumbent, incumbent's lambda counter,
    # completion increment, and the convergence shadow's two questions.
    assert len(predicate_calls) >= 7
    # Every friction-in-dollars variable is h * C.
    for target in ("friction", "incumbent_friction", "increment_friction"):
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == target for t in node.targets
            ):
                assert list(_calls(node.value, "economic_hurdle_usdc")), target
                found = True
        assert found, target
    for name in ("SUPPORT_EDGE_FRICTION_TICKS_PER_LEG", "SUPPORT_EDGE_MARGIN"):
        assert name not in RUNTIME_SRC
        assert name not in LEDGER_SRC


def test_the_retired_knobs_are_refused_not_ignored() -> None:
    for knob in ("PM_SUPPORT_EDGE_FRICTION_TICKS_PER_LEG", "PM_SUPPORT_EDGE_MARGIN"):
        env = {**os.environ, knob: "0.01"}
        proc = subprocess.run(
            [sys.executable, "-c", "import bayes_basket_target.basket_kernel.delta_v2"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        assert proc.returncode != 0, knob
        assert "retired by charter 130" in proc.stderr, knob


def test_the_allocator_reads_the_seated_belief() -> None:
    tree = ast.parse(WORKER_SRC)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_station_edge_evidence"
    )
    assigned = [
        n.value for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "probabilities" for t in n.targets)
    ]
    assert assigned
    assert {"basket_support_selection", "seated", "window_probabilities"} <= _strings(assigned[0])
    assert "probability_envelope" not in _strings(fn)


def test_the_ledger_hashes_the_hurdle_and_zero_rebuilds_the_old_gate() -> None:
    import paper_trading_ledger as L  # noqa: PLC0415

    assert L._SELECTION_RULE_IDENTITY["economic_hurdle"] == 0.05
    assert L._SELECTION_RULE_IDENTITY["economic_hurdle_rule"] == K.ECONOMIC_HURDLE_RULE_VERSION
    served = L.SELECTION_RULE_SHA256
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'scripts'); import paper_trading_ledger as L; "
         "print(L.SELECTION_RULE_SHA256)"],
        cwd=str(ROOT), env={**os.environ, "PM_ECONOMIC_HURDLE": "0"},
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    assert proc.stdout.strip() != served
    bad = subprocess.run(
        [sys.executable, "-c", "import bayes_basket_target.basket_kernel.delta_v2"],
        cwd=str(ROOT), env={**os.environ, "PM_ECONOMIC_HURDLE": "0.9"},
        capture_output=True, text=True,
    )
    assert bad.returncode != 0 and "outside [0, 0.5]" in bad.stderr


def _snapshot(asks):
    from bayes_basket_target.basket_kernel.contracts import TargetBasketSnapshot  # noqa: PLC0415

    labels = ["29°C", "30°C", "31°C", "32°C", "33°C", "34°C"]
    probs = [0.04, 0.14, 0.32, 0.28, 0.14, 0.08]
    rows = [
        {
            "bucket": b, "ordinal": i + 2, "probability": p,
            "bid_price": max(0.0, a - 0.01),
            "bid_levels": [{"price": max(0.001, a - 0.01), "shares": 50.0}],
            "ask_levels": [{"price": a, "shares": 100.0}],
            "price_tick_size": 0.01,
            "maker_queue_ahead_shares": 0.0, "maker_queue_known": True,
        }
        for i, (b, p, a) in enumerate(zip(labels, probs, asks))
    ]
    snapshot = TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6", "snapshot_id": "c130", "sequence": 1,
        "observed_at": "2026-09-12T00:00:00Z", "market_id": "m", "station_id": "X",
        "event_date": "2026-09-13", "quote_version": "q",
        "quote_observed_at": "2026-09-12T00:00:00Z", "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "i", "order_state_version": "o",
        "evidence": {"probability_stage": "P0", "posterior_version": "p", "posterior_sha256": "a" * 64},
        "buckets": rows, "checkpoint_changed": True, "quote_changed": True,
        "fill_changed": True, "time_gate_changed": True, "evidence_fresh": True,
        "contract_matches": True, "order_state_known": True, "risk_inputs_complete": True,
        "time_left_minutes": 600.0,
    })
    return snapshot, dict(zip(labels, probs))


def _seat(asks, *, tables):
    snapshot, probabilities = _snapshot(asks)
    windows, audit = R._support_windows(
        snapshot=snapshot, desired={"31°C", "32°C", "33°C"},
        probabilities=probabilities, fee_rate=0.0,
        maximum_support_buckets=3, minimum_support_buckets=2,
        center_bucket="31°C", probability_stage="P0",
        effective_edge_tables=tables,
    )
    # Charter 131: this fixture reads RIGHT_SKEWED (mean 4.58 against mode
    # 31°C, 0.58 buckets over a 0.43 gate), so the mean bucket is 32°C and
    # the three-rung seat is {32, 33, 34}; {31, 32, 33} is not a shape.
    seat = {tuple(w["buckets"]): w for w in windows}.get(("32°C", "33°C", "34°C"))
    return seat, audit


def test_six_percent_seats_four_percent_does_not_and_zero_rebuilds_it(monkeypatch) -> None:
    """The pass mark itself, on a window whose lambda is one."""

    tables = {"P0": ((float("inf"), 1.0),)}
    # 32/33/34 claims 0.50.  On a one-cent tick: at 0.45 all-in it returns
    # +11%; at 0.48, +4.2%.  The two-rung {32, 33} at 0.39 clears either way
    # (0.42 against 0.41), so exactly one window is refused at 4.2%.
    six = [0.03, 0.12, 0.30, 0.26, 0.13, 0.06]
    four = [0.03, 0.12, 0.30, 0.26, 0.13, 0.09]
    seat, audit = _seat(six, tables=tables)
    assert seat is not None and seat["admissible"] is True, audit
    assert audit["edge_refused_window_count"] == 0
    seat, audit = _seat(four, tables=tables)
    assert seat is None or seat["admissible"] is False
    assert audit["edge_refused_window_count"] == 1
    refused = audit["edge_best_refused_window"]
    assert refused["buckets"] == ["32°C", "33°C", "34°C"]
    assert refused["edge"] > 0  # positive, and still refused
    # The dollars the edge had to clear: h times the all-in cost.
    assert refused["friction"] == pytest.approx(0.05 * 0.48)
    monkeypatch.setattr(K, "ECONOMIC_HURDLE_V1", 0.0)
    seat, _ = _seat(four, tables=tables)
    assert seat is not None and seat["admissible"] is True


def test_the_clause_names_what_the_code_does() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = next(l for l in charter.split("\n") if l.lstrip().startswith("130. "))
    for needle in (
        "ECONOMIC_HURDLE_V1", "0.05", "clears_economic_hurdle_v1", "PM_ECONOMIC_HURDLE",
        "四个评估点", "_station_edge_evidence", "PM_SUPPORT_EDGE_MARGIN", "第 97 条", "第 116 条",
    ):
        assert needle in clause, needle
    for n in (64, 84):
        old = next(l for l in charter.split("\n") if l.lstrip().startswith(f"{n}. "))
        assert "第 130 条改写" in old, n


def test_the_head_is_chosen_on_the_effective_belief(monkeypatch) -> None:
    """Charter 64 as written ("折后"), code caught up 2026-09-12.

    A wider window can claim more and, deflated by its own agreement band,
    believe less.  The seat goes to what the gate, the stake and the ROI
    target believe -- the same lambda(a) * p -- not to the raw claim.
    """

    # Agreement below 0.70 is trusted in full; at or above it, 0.80 arrives.
    tables = {"P0": ((0.70, 1.0), (float("inf"), 0.80))}
    # Charter 131 shapes on this fixture: {32, 33} claims 0.42 at 0.29
    # (a=0.69 -> lambda 1.0 -> 0.42); {32, 33, 34} claims 0.50 at 0.36
    # (a=0.72 -> lambda 0.80 -> 0.40).  Both clear the 5% hurdle.
    asks = [0.03, 0.12, 0.30, 0.20, 0.09, 0.07]
    snapshot, probabilities = _snapshot(asks)
    windows, audit = R._support_windows(
        snapshot=snapshot, desired={"31°C", "32°C", "33°C"},
        probabilities=probabilities, fee_rate=0.0,
        maximum_support_buckets=3, minimum_support_buckets=2,
        center_bucket="31°C", probability_stage="P0",
        effective_edge_tables=tables,
    )
    admissible = [w for w in windows if w["admissible"] and w["all_in_price"] is not None]
    listing = [
        (w["buckets"], round(w["win_probability"], 3), round(w["effective_win_probability"], 3), w["all_in_price"])
        for w in windows
    ]
    assert len(admissible) >= 2, listing
    by_claim = max(admissible, key=lambda w: float(w["win_probability"]))
    by_effective = max(admissible, key=lambda w: float(w["effective_win_probability"]))
    assert tuple(by_claim["buckets"]) == ("32°C", "33°C", "34°C"), listing
    assert tuple(by_effective["buckets"]) == ("32°C", "33°C"), listing
    assert tuple(windows[0]["buckets"]) == ("32°C", "33°C"), listing
    # And the ledger hashes the ordering it replays.
    import paper_trading_ledger as L  # noqa: PLC0415

    assert L._SELECTION_RULE_IDENTITY["window_ranking"] == "effective_probability_mass"

