from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_all_active_taf_oos_paths_use_canonical_v4_training_entry() -> None:
    paths = (
        ROOT / "bayes_basket_target" / "taf_nwp_crossfit.py",
        ROOT / "bayes_basket_target" / "taf_nwp_transport_oos.py",
        ROOT / "bayes_basket_target" / "taf_live_combination_oos.py",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "_fit_rolling_probability_ensemble" in source
        assert "_fit_probability_weights" not in source
        assert "rolling_origin_conditional_mean_stack_v1_min2" not in source


def test_crossfit_binds_formal_ensemble_model_mask() -> None:
    source = (
        ROOT / "bayes_basket_target" / "taf_nwp_crossfit.py"
    ).read_text(encoding="utf-8")
    assert "ModelEnsembleArtifact.from_mapping" in source
    assert "validate_formal_ensemble_artifact" in source
    assert "models = tuple(ensemble.candidate_models)" in source
    assert 'MODELS = ("ECMWF", "GFS")' not in source
