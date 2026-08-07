"""`settlement_timezone` must not count as reported-high risk lineage.

`build_preselection_plan` rejects a non-P1 envelope that carries *any* key from
the reported-high risk lineage set, and requires a P1 envelope to carry them
all.  `settlement_timezone` was in that set, but every envelope carries it at
every stage -- so the forbidden-key branch fired on every P0 envelope ever
built and `p0_reported_high_risk_lineage_forbidden` structurally blocked P0
preselection.  The P1 branch never reads it, so removing it costs nothing.
"""

import pytest

from bayes_basket_target import runtime


SOURCE = __import__("inspect").getsource(runtime)


def test_settlement_timezone_is_not_a_risk_lineage_key() -> None:
    start = SOURCE.index("risk_lineage_keys = {")
    end = SOURCE.index("}", start)
    block = SOURCE[start:end]

    assert '"settlement_timezone"' not in block


def test_the_four_reported_high_keys_remain() -> None:
    start = SOURCE.index("risk_lineage_keys = {")
    end = SOURCE.index("}", start)
    block = SOURCE[start:end]

    for key in (
        "reported_high_bucket",
        "reported_high_ordinal",
        "canonical_observation_event_id",
        "hard_bound_id",
    ):
        assert f'"{key}"' in block, f"{key} must still gate reported-high lineage"


def test_p1_branch_never_reads_settlement_timezone_from_lineage() -> None:
    # If the P1 branch had consumed it, dropping it from the key set would
    # weaken a real check rather than fix a misclassification.
    start = SOURCE.index('if envelope.stage == "P1":')
    end = SOURCE.index("elif present_risk_keys:", start)
    p1_branch = SOURCE[start:end]

    assert 'lineage["settlement_timezone"]' not in p1_branch
    assert 'lineage.get("settlement_timezone")' not in p1_branch
