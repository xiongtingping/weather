from __future__ import annotations

import json

from production_reported_x_weak_revision_writer import (
    materialize_reported_x_weak_revision_public_fact_row,
)
from tests.test_production_reported_x_public_ssot_writer import (
    _activation,
    _checkpoint,
)


def test_weak_revision_public_fact_replaces_integer_x_and_never_restores_latent() -> None:
    checkpoint = _checkpoint()
    previous = {
        "posterior_ssot_fact": {
            "schema": _activation()["public_ssot_schema"],
            "physical_high_distribution": {"31.25": 1.0},
            "reported_physical_x_distribution": {"31": 1.0},
            "probability_runtime_identity": {},
        }
    }
    result = materialize_reported_x_weak_revision_public_fact_row(
        activation=_activation(),
        previous_fact_json=json.dumps(previous),
        checkpoint_row=checkpoint,
        update_row={"weak_forecast_revision_update_id": "weakupd-1"},
        fact_snapshot_id="factsnap-weak-1",
        slug="chengdu-high-2026-07-15",
        event_date="2026-07-15",
        fact_as_of="2026-07-15T01:30:00Z",
    )
    ssot = json.loads(result["fact_json"])["posterior_ssot_fact"]
    assert ssot["reported_physical_x_distribution"] == {"31": 0.7, "32": 0.3}
    assert ssot["reported_x_lower_bound_c"] == 31
    assert ssot["last_weak_forecast_revision_update_id"] == "weakupd-1"
    assert ssot["latent_probability_public"] is False
    assert "physical_high_distribution" not in ssot

