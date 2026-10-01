"""Charter 241: P0-M's observed hard bound censors; P1 roots on the proportional shape.

A leaf module (contracts only) so the P1 side -- ``materialization``,
``metar_trajectory``, ``p1_compiler`` and the update's fit -- can all read the
root through one function without an import cycle.
"""

from __future__ import annotations

import json
import math
from typing import Mapping

from .contracts import ContractError, ProbabilityEnvelope

# Charter 241 (user ruling 2026-10-01 「修p0m」): where P0-M puts the mass the
# observed high has ruled out.  The day's maximum is max(running high, the
# rest of the day), so a scenario of the root that ended below the running
# high ends AT it -- in the running high's bucket -- not spread over the
# buckets above in proportion to what the root already said about them.  The
# proportional rule answered "Y >= H" and nothing else, and on the ladder that
# read as warm: P0-M cells under an observed bound sat 0.31 bucket warm, the
# KMIA 2026-09-29 day whose 16:25 running high was its final still served 55%
# above it.  Censoring is the stage's own arithmetic; P1 keeps rooting on the
# proportional shape (``p1_root_probabilities``), because P1's own update
# already reads what the reports say about the rest of the day and the
# censored mass would count that twice.
OBSERVED_FLOOR_CENSORING_RULE = "observed_floor_censoring_v1"
OBSERVED_FLOOR_CENSORING_LINEAGE_KEY = "observed_floor_censoring_json"
OBSERVED_FLOOR_CENSORING_RECEIPT_SCHEMA = "p0m_observed_floor_censoring_receipt_v1"


def p1_root_probabilities(root: ProbabilityEnvelope) -> dict[str, float]:
    """The vector P1 multiplies: the root's, or a censoring P0-M's proportional one.

    Charter 241: a P0-M whose observed bound censored carries the shape it
    would have served under the proportional rule, and P1 roots on that --
    P1's own update reads the reports about the rest of the day, so the
    censored mass would count the same evidence twice.  Serving
    (``materialize_p1``) and the update's fit (``fit_p1_observation_likelihood``)
    both read the root through this one function.  A malformed receipt fails
    closed rather than falling back to the censored vector.
    """

    raw = (
        root.lineage.get(OBSERVED_FLOOR_CENSORING_LINEAGE_KEY)
        if root.stage == "P0-M"
        else None
    )
    if raw in (None, "", "NONE"):
        return dict(root.probabilities)
    try:
        receipt = json.loads(str(raw))
        proportional = receipt["proportional_probabilities"]
        valid = (
            receipt.get("rule") == OBSERVED_FLOOR_CENSORING_RULE
            and isinstance(proportional, Mapping)
            and set(proportional) == set(root.probabilities)
        )
        values = (
            {bucket: float(proportional[bucket]) for bucket in root.probabilities}
            if valid else {}
        )
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ContractError("p1_root_observed_floor_receipt_malformed") from exc
    if (
        not values
        or any(not math.isfinite(v) or v < 0.0 for v in values.values())
        or abs(sum(values.values()) - 1.0) > 1e-9
    ):
        raise ContractError("p1_root_observed_floor_receipt_malformed")
    return values
