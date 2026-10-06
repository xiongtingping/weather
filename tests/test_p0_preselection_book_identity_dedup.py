"""A book tick that changes no decision must not change ``plan_sha256``.

2026-10-06: the lifecycle projection carried the market snapshot's own hash,
so every cycle wrote a new 25 KB preselection row for an unchanged plan and
the SSOT reached its ceiling.
"""

from bayes_basket_target.p0_preselection import _decision_semantic_projection
from bayes_basket_target.contracts import content_sha256


def _compiled(snapshot_sha256: str, target_cost: float = 1.0) -> dict:
    return {
        "status": "hold",
        "reason_codes": ["hold"],
        "plan": {
            "basket_state": "HOLD",
            "basket_state_reason": "no_gap",
            "basket_state_transition": {
                "state": "HOLD",
                "reason": "no_gap",
                "desired_support": ["20"],
            },
            "candidate_support": ["20"],
            "final_support": ["20"],
            "snapshot_sha256": snapshot_sha256,
            "target_solution": {"target_cost_by_bucket": {"20": target_cost}},
        },
    }


def _plan_sha(compiled: dict) -> str:
    return content_sha256(_decision_semantic_projection(compiled))


def test_book_snapshot_hash_alone_does_not_move_plan_identity():
    assert _plan_sha(_compiled("a" * 64)) == _plan_sha(_compiled("b" * 64))


def test_economic_change_still_moves_plan_identity():
    assert _plan_sha(_compiled("a" * 64, 1.0)) != _plan_sha(_compiled("a" * 64, 1.5))


def test_lifecycle_still_projected():
    lifecycle = _decision_semantic_projection(_compiled("a" * 64))["plan"][
        "basket_lifecycle"
    ]
    assert lifecycle["state"] == "HOLD"
    assert "snapshot_sha256" not in lifecycle
    assert "support_version_sha256" not in lifecycle
