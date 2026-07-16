from __future__ import annotations

from scripts.evaluate_physical_state_bayes_v6_artifact_candidates import _scenario


def test_oos_control_defaults_are_explicitly_unavailable() -> None:
    control = _scenario()
    assert control.forcing_availability == 0.0
    assert control.precipitation_feature_available == 0.0
    assert control.cloud_feature_available == 0.0
    assert control.cb_feature_available == 0.0
