"""The band table can be varied on the book, not only in a notebook.

Charter 105's axis had a probability-layer reading and no money one, and the
standing rule is that a pooled-metric argument must clear a ledger replay --
the fleet-mean case is on record at -24% on the probability layer and -7.3
points on the book.  But the ledger cannot produce this arm on its own: the
band table is applied when the posterior is MATERIALIZED, so the ledger reads
a distribution the table has already shaped, and replaying it with a
different table means recompiling the envelope.

The recompile lane could not reach the table either.  Its ``--set-constant``
flag varies a module constant; the band table is loaded from the database by
the sha the artifact's combination method pins, so setting a constant misses
it and writing a different table misses it too -- the pin still names the old
one.  ``band_table_override`` is the narrow entry that closes it, shaped like
``artifact_parameter_override`` beside it: keyword-only, default None,
byte-identical when absent, and production never passes it.

The axis rides in WITH the factors.  A table handed over without one would be
applied through whichever lookup the pin happened to name, which is the exact
failure the triple exists to make impossible.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.native_ensemble_p0 import (  # noqa: E402
    compile_native_ensemble_p0,
)


def test_the_override_is_keyword_only_and_absent_by_default():
    parameter = inspect.signature(compile_native_ensemble_p0).parameters[
        "band_table_override"
    ]
    assert parameter.default is None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_override_carries_its_own_axis():
    """(factors, sha, axis).  Two of the three would let a distance-fitted
    counterfactual be applied through the claim lookup, which is the one way
    this entry could score math nobody would serve."""

    source = (
        ROOT / "bayes_basket_target" / "native_ensemble_p0.py"
    ).read_text(encoding="utf-8")
    body = source.split("if band_table_override is not None:", 1)[1]
    head = body.split("\n\n", 1)[0]
    assert "band_table_override[0]" in head
    assert "band_table_override[1]" in head
    assert "band_table_override[2]" in head
    # An axis it cannot key is refused, not defaulted -- the compile fails
    # closed like every other gate it passes.
    assert "band_table_override_axis_unknown" in body
    assert "FAIL_CLOSED_NO_P0" in body


def test_a_replayed_envelope_says_it_was_replayed():
    """An envelope compiled under a counterfactual table must never be
    mistaken for one that was served.  The receipt says so."""

    source = (
        ROOT / "bayes_basket_target" / "native_ensemble_p0.py"
    ).read_text(encoding="utf-8")
    assert '"band_table_counterfactual"' in source
    assert '"band_table_axis"' in source


def test_the_override_is_applied_after_the_activation_is_resolved():
    """Same contract as artifact_parameter_override: this may change the
    numbers, never whether a system was entitled to be seated.  The band
    layer is the outermost transform and runs long after the activation
    gates, so its position in the source is the guarantee."""

    source = (
        ROOT / "bayes_basket_target" / "native_ensemble_p0.py"
    ).read_text(encoding="utf-8")
    activation = source.index("native_ensemble_foundation_schema_status")
    override = source.index("if band_table_override is not None:")
    assert activation < override


def test_production_never_passes_it():
    """Grep, because the guarantee is that no production call site exists."""

    callers = []
    for path in (ROOT / "bayes_basket_target").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "compile_native_ensemble_p0(" in text and (
            "band_table_override=" in text
        ):
            callers.append(path.name)
    # Empty: the parameter is declared in one module and passed by none of
    # them.  The only caller that hands it in is the replay tool under
    # scripts/, which is not a production lane.
    assert callers == [], callers
    tool = (ROOT / "scripts" / "p0_recompile_replay.py").read_text(
        encoding="utf-8"
    )
    assert "band_table_override=band_table_override" in tool


def test_the_replay_tool_offers_the_arm_and_persists_nothing():
    tool = (ROOT / "scripts" / "p0_recompile_replay.py").read_text(
        encoding="utf-8"
    )
    assert '"--band-axis"' in tool
    assert 'choices=("claim", "centre_distance")' in tool
    assert "band_table_override=band_table_override" in tool
    # Fitted at run time against a read-only connection: activating a table
    # in order to score it is the thing this lane exists to avoid, and
    # query_only makes an accidental persist fail loudly.
    assert "fit_band_reliability_table(" in tool
    assert "PRAGMA query_only=ON" in tool
    assert "persist_and_activate_band_table" not in tool


def test_an_unknown_axis_is_refused_by_the_compiler_not_the_caller():
    """The caller's ``choices`` is a convenience; the guarantee is in the
    compile, because the entry has other callers than the CLI."""

    source = (
        ROOT / "bayes_basket_target" / "native_ensemble_p0.py"
    ).read_text(encoding="utf-8")
    guard = source.split("if band_table_override is not None:", 1)[1]
    assert "BAND_AXIS_CLAIM, BAND_AXIS_CENTRE_DISTANCE" in guard
    assert guard.index("raise ContractError") < guard.index(
        "band_reliability_receipt"
    ) if "band_reliability_receipt" in guard else True
