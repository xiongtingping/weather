"""Charter 151 (user ruling 2026-09-19): the TAF likelihood serves by ruling.

The refit keeps writing the gate it measured; the loader stamps the ruling over
it, keeps the measurement beside it, and re-hashes what serves.
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


def _artifact(state: str = "IDENTITY") -> dict:
    body = {
        "schema": TL.SCHEMA, "rule": TL.RULE,
        "gate": {"state": state, "ci_low": -0.03, "ci_high": 0.07},
        "pool": {"a": 0.0, "b": 0.3, "tau": 1.2, "n": 400},
        "stations": {},
    }
    body["content_sha256"] = content_sha256(body)
    return body


def test_the_ruling_opens_a_measured_closed_gate_and_keeps_the_measurement() -> None:
    measured = _artifact("IDENTITY")
    served = TL.apply_gate_ruling(measured, TL.GATE_SERVE)
    assert served["gate"]["state"] == TL.GATE_SERVE
    assert served["gate"]["measured_state"] == "IDENTITY"
    assert served["gate"]["ruling"] == TL.GATE_RULING_ID
    assert served["measured_content_sha256"] == measured["content_sha256"]
    rest = {k: v for k, v in served.items() if k != "content_sha256"}
    assert served["content_sha256"] == content_sha256(rest) != measured["content_sha256"]
    assert measured["gate"]["state"] == "IDENTITY"          # input untouched


def test_no_ruling_or_an_open_gate_leaves_the_artifact_as_measured() -> None:
    measured = _artifact("IDENTITY")
    assert TL.apply_gate_ruling(measured, "") == measured
    opened = _artifact("SERVE")
    assert TL.apply_gate_ruling(opened, TL.GATE_SERVE) == opened


def test_the_loader_serves_the_ruling_and_the_likelihood_speaks(tmp_path) -> None:
    path = tmp_path / "taf_likelihood_artifact.json"
    path.write_text(json.dumps(_artifact("IDENTITY")))
    loaded = TL.load_artifact(path)
    assert loaded["gate"]["state"] == TL.GATE_RULING == TL.GATE_SERVE
    centres = {"23°C": 23.0, "24°C": 24.0, "25°C": 25.0, "26°C": 26.0}
    likelihoods, receipt = TL.compile_taf_likelihood(
        loaded, station_id="ZUUU", tx_c=26.0, root_center_c=24.0,
        bucket_centers_c=centres, buckets=list(centres),
    )
    assert receipt["state"] == "APPLIED"
    assert receipt["artifact_sha256"] == loaded["content_sha256"]
    assert likelihoods["26°C"] > likelihoods["24°C"]
