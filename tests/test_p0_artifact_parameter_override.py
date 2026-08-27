"""The replay override may change numbers; it may not seat anything.

``artifact_parameter_override`` exists so an upstream constant can be scored
on the book: the trainer bakes bias, spread and the climatology blend into a
stored artifact, so a replay that varies such a constant has to hand in the
numbers it would have produced.  That makes it the one entry point into the
compiler that does not come from the database, which is exactly the kind of
door that has to stay narrow.

The property under test is where it sits.  It is applied *after* the system's
activation has been resolved and its run cycle checked, so every gate the
compile normally passes still runs and still fails closed.  An override
cannot admit a system, change which cycle is served, or turn a fail-closed
day into a served one -- it can only change what a system that was already
entitled to be seated says.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from bayes_basket_target.native_ensemble_p0 import (
    compile_native_ensemble_p0,
)

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "bayes_basket_target" / "native_ensemble_p0.py"
).read_text(encoding="utf-8")


def test_production_never_has_to_pass_it() -> None:
    parameter = inspect.signature(
        compile_native_ensemble_p0
    ).parameters["artifact_parameter_override"]
    assert parameter.default is None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_it_is_applied_after_the_activation_gate_not_before() -> None:
    # The activation lookup and its cycle check fail closed on their own.  An
    # override applied before them could seat a system the station is not
    # serving; applied after, it can only restate one that was.
    gate = SOURCE.index(
        "FAIL_CLOSED_NO_P0:native_system_artifact_cycle_mismatch"
    )
    override = SOURCE.index(
        "override = (artifact_parameter_override or {}).get(system_id)"
    )
    assert gate < override


def test_an_absent_override_leaves_every_parameter_untouched() -> None:
    # The substitution is guarded on the system having an entry, and each of
    # the three maps falls back to the artifact's own when the entry omits
    # it, so a partial override cannot blank the rest.
    block = SOURCE[
        SOURCE.index(
            "override = (artifact_parameter_override or {}).get(system_id)"
        ):
        SOURCE.index("reliability_parameters = json.loads(str(system_artifact[2]))")
    ]
    assert "if override is not None:" in block
    for name in ("bias_parameters", "spread_parameters",
                 "reliability_parameters"):
        assert f'override.get("{name}")' in block
        assert "or json.loads(str(system_artifact[" in block


def test_the_content_hash_and_cycle_are_carried_through_unchanged() -> None:
    # Positions 3 and 4 are the artifact's content hash and its run cycle.
    # Rewriting either would let a replay claim provenance it does not have,
    # or serve a cycle the gate above rejected.
    block = SOURCE[
        SOURCE.index(
            "override = (artifact_parameter_override or {}).get(system_id)"
        ):
        SOURCE.index("reliability_parameters = json.loads(str(system_artifact[2]))")
    ]
    assert "system_artifact[3]," in block
    assert "system_artifact[4]," in block
