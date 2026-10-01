"""Every hop of the compile chain accepts what the hop above it passes.

The chain is five functions deep and each one forwards its keywords to the
next by name.  Adding an input therefore means editing five signatures and
four forwards, and nothing checked that they agreed -- so a change could
land its two ends and leave the middle out, which is exactly what happened.

Measured 2026-09-09: ``off_centre_claim_bands`` was added to both callers
(``production_io`` and ``p0_preselection``) and to the deepest consumer
(``compile_delta_v2_runtime_plan``) at 08:00:39Z, and to neither of the two
functions between them.  The resident restarted at 08:42Z onto the pair and
every scope reaching the decision path raised

    TypeError: compile_production_plan() got an unexpected keyword argument
               'off_centre_claim_bands'

Ten of forty-five scopes died on it in the 09:23Z cycle, and it surfaced on
the board only because a render finally completed at 10:26Z -- the renders
before it were being killed at their timeout, so the first page to carry the
failure was written two and a half hours after it started.

Pinned by signature, not by calling anything: the failure is that a name one
function passes is a name the next does not take, and that is answerable
without a database, a fixture or a clock.
"""

from __future__ import annotations

import inspect

from bayes_basket_target.delta_v2_runtime import compile_delta_v2_runtime_plan
from bayes_basket_target.production import compile_production_plan
from bayes_basket_target.runtime import (
    DeltaBasketScoreV2Optimizer,
    _compile_target_plan,
    compile_target_plan,
)
import pytest


# Top to bottom, the way a keyword travels.
CHAIN = (
    ("production.compile_production_plan", compile_production_plan),
    (
        "runtime.DeltaBasketScoreV2Optimizer.compile",
        DeltaBasketScoreV2Optimizer.compile,
    ),
    ("runtime.compile_target_plan", compile_target_plan),
    ("runtime._compile_target_plan", _compile_target_plan),
    (
        "delta_v2_runtime.compile_delta_v2_runtime_plan",
        compile_delta_v2_runtime_plan,
    ),
)

# The inputs the callers thread through the whole chain.  Each one is a table
# or artefact the optimizer reads, loaded once per scope by the caller and
# passed down rather than re-read at every level.
THREADED_KEYWORDS = (
    "served_tables",
    "off_centre_claim_bands",
    "chain_consolidation_tilt",
    "last_acted_probability_identity",
    "last_decision_signature",
)


def _parameters(fn) -> set[str]:
    return set(inspect.signature(fn).parameters)


@pytest.mark.parametrize("keyword", THREADED_KEYWORDS)
def test_every_hop_of_the_compile_chain_accepts_the_threaded_keyword(
    keyword: str,
) -> None:
    missing = [name for name, fn in CHAIN if keyword not in _parameters(fn)]
    assert not missing, (
        f"{keyword} is passed down the chain but not accepted by: "
        + ", ".join(missing)
    )


def test_no_hop_relies_on_kwargs_to_swallow_what_it_does_not_declare() -> None:
    """A ``**kwargs`` anywhere would make the test above unfalsifiable.

    It would also move the failure from the call to wherever the unknown
    name is finally read, which is the harder half of the bug: the
    TypeError at least named the argument and the function.
    """

    for name, fn in CHAIN:
        kinds = [p.kind for p in inspect.signature(fn).parameters.values()]
        assert inspect.Parameter.VAR_KEYWORD not in kinds, (
            f"{name} accepts **kwargs; a missing hop would stop failing loudly"
        )
