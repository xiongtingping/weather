"""v2.1 §1: there is one economic quantity, and three readers of it.

The admission gate, the Kelly stake and the execution layer's net-ROI
target must all read the same calibrated belief q = lambda(a) * p.  They
do today, by construction rather than by rule: the gate looks lambda up
once and records it; the solver multiplies the same lambda into the claim
factor and partitions the window with it; the scored row carries that
partition; the seated record copies it; and ``runtime.py`` hands it to
``basket_return_structure_v1`` as the claim.  A second lookup anywhere on
that chain, or a second belief substituted at one reader, is what v2.1
calls "argmax on the claimed edge" -- measured -18.85% against -4.95% when
tried as an objective.

These tests read the chain off the syntax tree, so a refactor that quietly
gives one reader its own belief fails here rather than in a ledger reading
weeks later.  Two research switches could break the invariant on purpose:
``SIZING_CLAIM_BANDS`` (a stake-only lift) and ``BETA_EDGE_MODE=on``
(charter 128, which by design keeps admission and sizing on one number by
turning lambda into the ratio that reproduces the beta belief).  Both are
pinned at their defaults.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ast.parse(
    (ROOT / "bayes_basket_target" / "delta_v2_runtime.py").read_text(
        encoding="utf-8"
    )
)
EXECUTION = ast.parse(
    (ROOT / "bayes_basket_target" / "runtime.py").read_text(encoding="utf-8")
)


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _strings(node: ast.AST) -> set[str]:
    return {
        n.value for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _calls(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id == name) or (
            isinstance(func, ast.Attribute) and func.attr == name
        ):
            yield node


def _keyword(call: ast.Call, key: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == key:
            return kw.value
    return None


def _assignments(tree: ast.AST, target: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == target for t in node.targets
        ):
            yield node.value


def _dict_values(tree: ast.AST, key: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == key:
                    yield v


def test_the_gate_reads_lambda_times_the_claim() -> None:
    """Reader one: admission.  effective_win = raw_win * claim_factor * lambda."""

    assert any(
        {"raw_win", "claim_factor", "gate_lambda"} <= _names(value)
        for value in _assignments(RUNTIME, "effective_win")
    )


def test_the_solver_multiplies_the_same_lambda_into_the_partition() -> None:
    """Reader two: sizing.  No second lookup -- the gate's lambda is reused."""

    assert any(
        isinstance(node, ast.AugAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "claim_factor"
        and isinstance(node.op, ast.Mult)
        and "gate_lambda" in _names(node.value)
        for node in ast.walk(RUNTIME)
    )
    # And the window's partition is taken with that factor.
    assert any(
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "deflate_window_partition_v1"
        and isinstance(_keyword(value, "claim_factor"), ast.Name)
        and _keyword(value, "claim_factor").id == "claim_factor"
        for value in _assignments(RUNTIME, "window_probabilities")
    )


def test_the_kelly_stake_is_fed_that_partition() -> None:
    # The solver scores on the partition, and the candidate generator it
    # is handed proposes against the same one -- the keyword lives on the
    # generator call nested inside the solver call, not on the solver.
    solve = list(_calls(RUNTIME, "solve_target_inventory_v2"))
    assert solve
    assert any(
        isinstance(_keyword(call, "probabilities"), ast.Name)
        and _keyword(call, "probabilities").id == "window_probabilities"
        for call in solve
    )
    generator = list(_calls(RUNTIME, "_target_candidates"))
    assert generator
    assert any(
        isinstance(_keyword(call, "probabilities_by_bucket"), ast.Name)
        and _keyword(call, "probabilities_by_bucket").id == "window_probabilities"
        for call in generator
    )
    # Inside the solver the Kelly candidate reads the sizing override only
    # when one is set, else the same partition.
    kelly = [
        call for call in _calls(RUNTIME, "_whole_basket_kelly_candidate")
        if _keyword(call, "probabilities_by_bucket") is not None
    ]
    assert kelly
    fed = _keyword(kelly[0], "probabilities_by_bucket")
    assert isinstance(fed, ast.IfExp)
    assert isinstance(fed.orelse, ast.Name)
    assert fed.orelse.id == "probabilities_by_bucket"
    assert "sizing_probabilities_by_bucket" in _names(fed.test)


def test_the_seated_record_carries_the_partition_it_was_judged_on() -> None:
    """The scored row copies ``window_probabilities``; the seat copies the row."""

    row_values = list(_dict_values(RUNTIME, "window_probabilities"))
    assert any(
        isinstance(v, ast.DictComp)
        and isinstance(v.generators[0].iter, ast.Call)
        and isinstance(v.generators[0].iter.func, ast.Attribute)
        and v.generators[0].iter.func.attr == "items"
        and isinstance(v.generators[0].iter.func.value, ast.Name)
        and v.generators[0].iter.func.value.id == "window_probabilities"
        for v in row_values
    )
    assert any(
        isinstance(v, ast.Call)
        and isinstance(v.func, ast.Name)
        and v.func.id == "dict"
        and v.args
        and isinstance(v.args[0], ast.Subscript)
        and isinstance(v.args[0].value, ast.Name)
        and v.args[0].value.id == "best"
        and "window_probabilities" in _strings(v.args[0])
        for v in row_values
    )


def test_the_roi_target_reads_the_seated_partition_and_nothing_else() -> None:
    """Reader three: execution authorization."""

    belief = list(_assignments(EXECUTION, "edge_belief"))
    assert belief
    assert {"basket_support_selection", "seated", "window_probabilities"} <= (
        _strings(belief[0])
    )
    structure = list(_calls(EXECUTION, "basket_return_structure_v1"))
    assert structure
    claim = _keyword(structure[0], "claim_by_bucket")
    assert claim is not None and "edge_belief" in _names(claim)


def test_no_second_belief_enters_by_default() -> None:
    """The two switches that could give a reader its own q are off."""

    from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: PLC0415
        BETA_EDGE_MODE,
        SIZING_CLAIM_BANDS,
    )

    assert SIZING_CLAIM_BANDS == ()
    assert BETA_EDGE_MODE == "shadow"


def test_charter_72_names_the_three_readers() -> None:
    """The clause and the code name the same three variables."""

    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(
        encoding="utf-8"
    )
    clause = next(
        line for line in charter.split("\n") if line.lstrip().startswith("72. ")
    )
    assert "2026-09-12 补记（v2.1 落地）" in clause
    for name in ("effective_win", "window_probabilities", "edge_belief"):
        assert f"`{name}`" in clause, name
