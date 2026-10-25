"""Charter 241: P0-M's observed hard bound censors; P1 roots on the proportional shape.

User ruling 2026-10-01 「修p0m」.  The day's maximum is max(running high, rest of
day), so P0-M puts the mass the observed high has ruled out on the running
high's bucket instead of renormalising it over the buckets above.  P1 keeps
multiplying the proportional shape (what v22 served), because its own update
reads the reports about the rest of the day.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from bayes_basket_target import observed_floor_censoring as OFC
from bayes_basket_target.contracts import ContractError, ProbabilityEnvelope
from bayes_basket_target.materialization import (
    floor_probabilities,
    materialize_p0m_observed,
)
from bayes_basket_target.p1_compiler import compile_station_p1_packets
from tests.test_bayes_basket_target_materialization import _envelope
from tests.test_charter_150_p1_joint_likelihood import _artifact, _observation, _registry
from tests.test_charter_161_p1_direct_update import _block, _with_block

ROOT = Path(__file__).resolve().parents[1]


def _root() -> ProbabilityEnvelope:
    base = _envelope()
    return ProbabilityEnvelope.from_mapping({
        **base.to_dict(),
        "lineage": {**base.lineage, "model_center_c": "38"},
    })


def _p0m(excluded=("37",), high=37.6) -> ProbabilityEnvelope:
    root = _root()
    return materialize_p0m_observed(
        root,
        p0_root=root,
        forecast_revision_id="taf-1",
        forecast_source_object_sha256="e" * 64,
        taf_tx_c=None,
        decision_at_utc="2026-07-23T05:00:00Z",
        fresh_until_utc="2026-07-23T06:00:00Z",
        bridge_sha256="b" * 64,
        observed_hard_excluded_buckets=excluded,
        observed_running_high_c=high,
    )


def _proportional_twin(p0m: ProbabilityEnvelope) -> ProbabilityEnvelope:
    """The same P0-M as v22 served it: proportional vector, no receipt."""

    receipt = json.loads(p0m.lineage[OFC.OBSERVED_FLOOR_CENSORING_LINEAGE_KEY])
    lineage = {
        k: v for k, v in p0m.lineage.items()
        if k != OFC.OBSERVED_FLOOR_CENSORING_LINEAGE_KEY
    }
    return ProbabilityEnvelope.from_mapping({
        **p0m.to_dict(),
        "probabilities": receipt["proportional_probabilities"],
        "lineage": lineage,
    })


def test_ruled_out_mass_lands_on_the_running_high_bucket() -> None:
    root = _root()
    envelope = _p0m()
    assert envelope.posterior_version == "p0m_taf_likelihood_v34"
    # root 37/38/39 = 0.2/0.6/0.2; "37" ruled out -> its 0.2 lands on "38".
    assert envelope.probabilities["37"] == 0.0
    assert envelope.probabilities["38"] == pytest.approx(0.8, abs=1e-12)
    assert envelope.probabilities["39"] == pytest.approx(0.2, abs=1e-12)
    receipt = json.loads(envelope.lineage[OFC.OBSERVED_FLOOR_CENSORING_LINEAGE_KEY])
    assert receipt["rule"] == OFC.OBSERVED_FLOOR_CENSORING_RULE
    assert receipt["schema"] == OFC.OBSERVED_FLOOR_CENSORING_RECEIPT_SCHEMA
    assert receipt["floor_bucket"] == "38"
    assert receipt["censored_mass"] == pytest.approx(root.probabilities["37"], abs=1e-12)
    # The proportional vector is exactly the pre-241 arithmetic.
    survivors = {"38": 0.6, "39": 0.2}
    total = sum(survivors.values())
    expected, _ = floor_probabilities(
        {"37": 0.0, **{k: v / total for k, v in survivors.items()}}, ("37",)
    )
    assert receipt["proportional_probabilities"] == pytest.approx(expected, abs=1e-15)


def test_no_exclusion_writes_no_receipt_and_serves_the_root() -> None:
    envelope = _p0m(excluded=(), high=None)
    assert OFC.OBSERVED_FLOOR_CENSORING_LINEAGE_KEY not in envelope.lineage
    assert dict(envelope.probabilities) == dict(_root().probabilities)
    assert OFC.p1_root_probabilities(envelope) == dict(envelope.probabilities)


def test_p1_roots_on_the_proportional_shape() -> None:
    p0m = _p0m()
    assert OFC.p1_root_probabilities(p0m) == pytest.approx(
        dict(_proportional_twin(p0m).probabilities), abs=0.0
    )


@pytest.mark.parametrize("artifact", [_artifact(), _with_block(_block())], ids=["g_alpha", "direct_update"])
def test_p1_on_a_censoring_p0m_equals_p1_on_its_proportional_twin(artifact) -> None:
    """The P1 a censoring P0-M yields is bit-for-bit the P1 v22 would have.

    The step reports 37.6, so P1's running-high bucket is "38" -- the very
    bucket the P0-M censored onto, the one case where the two roots differ.
    """

    censoring = _p0m()
    twin = _proportional_twin(censoring)
    served = []
    for root in (censoring, twin):
        packets = compile_station_p1_packets(
            predecessor=root,
            observations=[_observation(root, "a", "2026-07-23T15:00:00Z", 37.6)],
            registry_rows=_registry(root),
            compilation_at_utc="2026-07-23T15:00:00Z",
            observation_likelihood_artifact=artifact,
        )
        envelope = packets[-1]["expected_envelope"]
        assert "37" in packets[-1]["hard_excluded_buckets"]
        served.append((dict(envelope["probabilities"]), envelope["lineage"]))
    assert served[0][0] == served[1][0]
    # And the served P0-M itself is not what P1 multiplied.
    assert dict(censoring.probabilities) != served[0][0]


def test_a_malformed_receipt_fails_closed() -> None:
    p0m = _p0m()
    receipt = json.loads(p0m.lineage[OFC.OBSERVED_FLOOR_CENSORING_LINEAGE_KEY])
    for broken in (
        {**receipt, "rule": "proportional"},
        {**receipt, "proportional_probabilities": {"38": 1.0}},
        {**receipt, "proportional_probabilities": {"37": 0.0, "38": 0.9, "39": 0.2}},
        "not json",
    ):
        lineage = {
            **p0m.lineage,
            OFC.OBSERVED_FLOOR_CENSORING_LINEAGE_KEY: (
                broken if isinstance(broken, str) else json.dumps(broken)
            ),
        }
        bad = ProbabilityEnvelope.from_mapping({**p0m.to_dict(), "lineage": lineage})
        with pytest.raises(ContractError, match="p1_root_observed_floor_receipt_malformed"):
            OFC.p1_root_probabilities(bad)


def test_a_p0_root_or_unreceipted_p0m_is_its_own_vector() -> None:
    root = _root()
    assert OFC.p1_root_probabilities(root) == dict(root.probabilities)


def _calls(path: Path, owner: str) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == owner:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                    names.add(inner.func.id)
    return names


def test_the_update_fit_and_its_samples_read_the_root_through_the_one_function() -> None:
    # Serve/train split guard: the fit scores the prior P1 multiplies.
    assert "p1_root_probabilities" in _calls(
        ROOT / "scripts" / "fit_p1_observation_likelihood.py", "RootCache"
    )
    tree = ast.parse((ROOT / "scripts" / "build_metar_phase_hazard_samples.py").read_text())
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "p1_root_probabilities"
        for node in ast.walk(tree)
    )


def test_the_charter_states_the_rule() -> None:
    text = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text()
    clause = next(line for line in text.splitlines() if line.startswith("241. "))
    assert "observed_floor_censoring_v1" in clause
    assert "p1_root_probabilities" in clause
