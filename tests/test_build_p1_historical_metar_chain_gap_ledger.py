from __future__ import annotations

import json

from bayes_basket_target.contracts import content_sha256
from scripts.build_p1_historical_metar_chain_gap_ledger import _curve_state


def test_curve_state_requires_existing_content_addressed_direct_binding():
    curve_identity = {"schema": "root_future_temperature_curve_v1", "points": []}
    curve = {**curve_identity, "content_sha256": content_sha256(curve_identity)}
    root = {
        "stage": "P0",
        "checkpoint_id": "p0-checkpoint",
        "posterior_sha256": "probability-sha",
        "lineage_json": json.dumps({
            "conditional_future_temperature_curve_json": json.dumps(curve),
            "conditional_future_temperature_curve_sha256": curve["content_sha256"],
            "conditional_future_temperature_curve_root_stage": "P0",
            "conditional_future_temperature_curve_root_checkpoint_id": "p0-checkpoint",
            "conditional_future_temperature_curve_root_probability_sha256": "probability-sha",
        }),
    }

    assert _curve_state(root) == "ROOT_FUTURE_CURVE_BOUND"
    root["lineage_json"] = json.dumps({})
    assert _curve_state(root) == "ROOT_FUTURE_CURVE_MISSING"


def test_curve_state_rejects_a_curve_claimed_for_another_old_root():
    curve_identity = {"schema": "root_future_temperature_curve_v1", "points": []}
    curve = {**curve_identity, "content_sha256": content_sha256(curve_identity)}
    root = {
        "stage": "P0",
        "checkpoint_id": "new-root",
        "posterior_sha256": "new-probability",
        "lineage_json": json.dumps({
            "conditional_future_temperature_curve_json": json.dumps(curve),
            "conditional_future_temperature_curve_sha256": curve["content_sha256"],
            "conditional_future_temperature_curve_root_stage": "P0",
            "conditional_future_temperature_curve_root_checkpoint_id": "old-root",
            "conditional_future_temperature_curve_root_probability_sha256": "old-probability",
        }),
    }

    assert _curve_state(root) == "ROOT_FUTURE_CURVE_BINDING_INVALID"
