from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import bind_bayes_basket_target_taf_live_compatibility as target


def test_rejects_blocked_transport_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "taf.json"
    report = tmp_path / "report.json"
    output = tmp_path / "bound.json"
    artifact.write_text("{}", encoding="utf-8")
    report.write_text(json.dumps({
        "schema": "bayes_basket_taf_nwp_transport_oos_report_v1",
        "state": "BLOCKED",
        "validated": False,
        "live_fingerprint": None,
        "evidence": None,
    }), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "bind",
            "--taf-artifact",
            str(artifact),
            "--transport-oos-report",
            str(report),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(
        RuntimeError, match="taf_transport_oos_report_not_validated"
    ):
        target.main()
    assert not output.exists()


def test_accepts_evaluator_pass_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "taf.json"
    report = tmp_path / "report.json"
    output = tmp_path / "bound.json"
    artifact.write_text("{}", encoding="utf-8")
    report.write_text(json.dumps({
        "schema": "bayes_basket_taf_nwp_transport_oos_report_v1",
        "state": "PASS",
        "validated": True,
        "live_fingerprint": {"fingerprint_sha256": "a" * 64},
        "evidence": {"content_sha256": "b" * 64},
    }), encoding="utf-8")
    bound = SimpleNamespace(
        artifact_id="taf-bound",
        content_sha256="c" * 64,
        nwp_live_compatibility_fingerprint_sha256="a" * 64,
        nwp_live_compatibility_evidence_sha256="b" * 64,
        nwp_live_compatibility_validated=True,
        to_dict=lambda: {"bound": True},
    )
    monkeypatch.setattr(
        target,
        "bind_taf_artifact_to_live_nwp_compatibility",
        lambda **_kwargs: bound,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "bind",
            "--taf-artifact",
            str(artifact),
            "--transport-oos-report",
            str(report),
            "--output",
            str(output),
        ],
    )
    assert target.main() == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "bound": True
    }
