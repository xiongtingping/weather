from pathlib import Path

from scripts.probability_ssot_mainline_audit import ROOT, run_audit


def test_audit_detects_retired_absolute_strong_file(tmp_path: Path) -> None:
    for relative in (
        "initial_posterior_checkpoint_shadow.py",
        "sequential_bayes_filter_shadow_runtime.py",
        "sequential_bayes_shadow_pipeline.py",
        "sequential_bayes_public_ssot_shadow_writer.py",
        "strong_observation_lr_evaluator.py",
    ):
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    launcher = tmp_path / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("# clean launcher\n", encoding="utf-8")
    (tmp_path / "physical_x_shadow_posterior_runtime.py").write_text("# retired\n", encoding="utf-8")

    result = run_audit(root=tmp_path)

    assert result["ok"] is False
    assert "retired_absolute_strong_probability_path_present:physical_x_shadow_posterior_runtime.py" in result["violations"]
