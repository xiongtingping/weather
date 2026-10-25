"""Charter 151's ruling (2026-09-19, the TAF line served whatever its gate measured),
revoked 2026-10-25 (charter 281 补记二, user 「重裁宪章 151：TAF 似然不再强制服务，P0-M
只保留实况下界」): the file serves exactly as measured, so a closed gate is identity.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import taf_likelihood as TL  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402

CENTRES = {"23°C": 23.0, "24°C": 24.0, "25°C": 25.0, "26°C": 26.0}


def _artifact(state: str) -> dict:
    body = {
        "schema": TL.SCHEMA, "rule": TL.RULE,
        "gate": {"state": state, "ci_low": -0.03, "ci_high": 0.07},
        "pool": {"a": 0.0, "b": 0.3, "tau": 1.2, "n": 400},
        "stations": {},
    }
    body["content_sha256"] = content_sha256(body)
    return body


def _compile(loaded):
    return TL.compile_taf_likelihood(loaded, station_id="ZUUU", tx_c=26.0, root_center_c=24.0,
                                     bucket_centers_c=CENTRES, buckets=list(CENTRES))


def test_a_measured_closed_gate_serves_identity(tmp_path) -> None:
    path = tmp_path / "taf_likelihood_artifact.json"
    measured = _artifact("IDENTITY")
    path.write_text(json.dumps(measured))
    loaded = TL.load_artifact(path)
    assert loaded == measured
    likelihoods, receipt = _compile(loaded)
    assert receipt["state"] == "IDENTITY_GATE_CLOSED"
    assert set(likelihoods.values()) == {1.0}


def test_a_measured_open_gate_still_serves_the_line(tmp_path) -> None:
    path = tmp_path / "taf_likelihood_artifact.json"
    path.write_text(json.dumps(_artifact("SERVE")))
    likelihoods, receipt = _compile(TL.load_artifact(path))
    assert receipt["state"] == "APPLIED" and likelihoods["26°C"] > likelihoods["24°C"]


def test_the_ruling_is_gone() -> None:
    assert not hasattr(TL, "GATE_RULING") and not hasattr(TL, "apply_gate_ruling")
