"""P0 stable qualified pool: who sits, and how the weight is divided.

Charter 134 (user ruling 2026-09-14, "批准稳定池").  The subset contest seated
whichever combination scored best on three settled days and re-ran as new
days landed, so the seat changed with the luck of those three days.  Measured
over 1,148 contests at three non-overlapping replay origins, picking on the
three days before instead changed the winner 79.8% of the time, and seating
the whole cohort scored within -1.0% [-3.6%, +2.1%] CRPS of the honest pick;
over 3,705 served P0 updates on 2026-09-10..12, a membership swap moved the
centre toward the settled high as often as away (51.6%), while a new run of
the same members made it measurably closer.

So membership is a slow variable and weather is a fast one:

* every qualified native system with a run in hand sits; one without a run
  this decision sits out and the rest renormalise (the missing-member rule);
* the mixture keeps every member's spread and the disagreement between their
  centres -- nothing here narrows a distribution by dropping a dissenter;
* skill still divides the weight, shrunk toward a family-balanced baseline
  so a few days of evidence cannot swing a system between full weight and
  none.

Which skill weights feed these rules, and how strongly to shrink, was chosen
by the nested replay (scripts/replay_p0_stable_pool.py, gate in
scripts/aggregate_p0_stable_pool_replay.py) before any serving path read
them.
"""
from __future__ import annotations

import math
import os
from collections import defaultdict
from itertools import combinations
from collections.abc import Mapping, Sequence

from .contracts import ContractError

STABLE_POOL_WEIGHT_RULE = "stable_pool_shrunk_skill_v1"
# Which rule seats P0.  ``stable_pool`` is charter 134's; ``subset_contest``
# is the rule it replaced, kept one switch away for a rollback.
P0_MEMBERSHIP_MODE_STABLE_POOL = "stable_pool"
P0_MEMBERSHIP_MODE_SUBSET_CONTEST = "subset_contest"
P0_MEMBERSHIP_MODES = (
    P0_MEMBERSHIP_MODE_STABLE_POOL,
    P0_MEMBERSHIP_MODE_SUBSET_CONTEST,
)
P0_MEMBERSHIP_MODE = os.environ.get(
    "P0_MEMBERSHIP_MODE", P0_MEMBERSHIP_MODE_STABLE_POOL
).strip() or P0_MEMBERSHIP_MODE_STABLE_POOL
if P0_MEMBERSHIP_MODE not in P0_MEMBERSHIP_MODES:
    raise RuntimeError(f"unknown P0_MEMBERSHIP_MODE: {P0_MEMBERSHIP_MODE}")
# The served shrinkage.  The nested replay chose kappa on earlier days for
# each update; across 7,714 updates it landed on 0.25 (40%), 0.5 (45%), 0.75
# (12%) and 1.0 (3%), and 0.5 alone scores -4.86% CRPS against the contest.
STABLE_POOL_KAPPA = 0.5
# Candidate shrinkage strengths the replay chooses among.  0 serves the skill
# weights untouched; 1 serves the family-balanced baseline.
STABLE_POOL_KAPPA_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)
MIN_POOL_FAMILIES = 2


def _families(
    system_ids: Sequence[str], family_by_system: Mapping[str, str]
) -> dict[str, list[str]]:
    families: dict[str, list[str]] = defaultdict(list)
    for system_id in sorted(set(str(value) for value in system_ids)):
        if system_id not in family_by_system:
            raise ContractError(f"stable_pool_family_unknown:{system_id}")
        families[str(family_by_system[system_id])].append(system_id)
    return families


def family_balanced_weights(
    system_ids: Sequence[str],
    family_by_system: Mapping[str, str],
) -> dict[str, float]:
    """Equal weight per independence family, split equally inside it.

    A family with two products is one piece of independent information, so it
    gets one family's share, not two.
    """

    families = _families(system_ids, family_by_system)
    if len(families) < MIN_POOL_FAMILIES:
        raise ContractError("stable_pool_fewer_than_two_families")
    share = 1.0 / len(families)
    return {
        system_id: share / len(members)
        for members in families.values()
        for system_id in members
    }


def shrink_toward_baseline(
    skill_weights: Mapping[str, float],
    baseline_weights: Mapping[str, float],
    *,
    kappa: float,
) -> dict[str, float]:
    """``(1 - kappa) * skill + kappa * baseline``, over the same systems."""

    kappa = float(kappa)
    if not math.isfinite(kappa) or not 0.0 <= kappa <= 1.0:
        raise ContractError("stable_pool_kappa_out_of_range")
    if set(skill_weights) != set(baseline_weights):
        raise ContractError("stable_pool_weight_keys_mismatch")
    for weights in (skill_weights, baseline_weights):
        total = sum(float(value) for value in weights.values())
        if any(
            not math.isfinite(float(value)) or float(value) < 0.0
            for value in weights.values()
        ) or not math.isclose(total, 1.0, abs_tol=1e-9):
            raise ContractError("stable_pool_weights_not_a_distribution")
    blended = {
        system_id: (1.0 - kappa) * float(skill_weights[system_id])
        + kappa * float(baseline_weights[system_id])
        for system_id in sorted(skill_weights)
    }
    total = sum(blended.values())
    return {system_id: value / total for system_id, value in blended.items()}


def available_pool_weights(
    weights: Mapping[str, float],
    available_system_ids: Sequence[str],
    family_by_system: Mapping[str, str],
) -> dict[str, float] | None:
    """The missing-member rule: drop systems without a run, renormalise.

    ``None`` when what is left spans fewer than two families or carries no
    weight, which is a P0 that must not be served -- never a single-family
    distribution wearing a pool's name.
    """

    available = {str(value) for value in available_system_ids}
    kept = {
        system_id: float(value)
        for system_id, value in weights.items()
        if system_id in available and float(value) > 0.0
    }
    if len(_families(list(kept), family_by_system)) < MIN_POOL_FAMILIES:
        return None
    total = sum(kept.values())
    if total <= 0.0:
        return None
    return {system_id: value / total for system_id, value in sorted(kept.items())}


def stable_pool_weights(
    skill_weights: Mapping[str, float],
    family_by_system: Mapping[str, str],
    *,
    kappa: float,
) -> dict[str, float]:
    """Skill weights for the whole pool, shrunk toward the family baseline."""

    baseline = family_balanced_weights(list(skill_weights), family_by_system)
    return shrink_toward_baseline(skill_weights, baseline, kappa=kappa)


def pool_subsets(
    system_ids: Sequence[str],
    family_by_system: Mapping[str, str],
) -> list[tuple[str, ...]]:
    """The seat sets a cohort mints: the whole pool and every missing-member view.

    The compile can only serve an artifact whose systems all have a usable
    run in the packet, so every view a packet can leave it with has to be
    certified in advance.  Every subset of two or more systems spanning at
    least two families is minted, whole pool first, then by size.

    Until 2026-09-14T06Z a four-system pool minted only itself and its
    one-missing views.  A packet with two members short -- KLGA h24_48 at
    05:31Z, where only IFS and GEFS had usable receipts -- then found no pool
    seat and was served a contest pair (IFS 0.28 / GEFS 0.72) where the pool
    rule gives 0.39 / 0.61; about 9% of the 05:22Z inbox went that way, and
    those contest rows expire in October with nothing to replace them.  User
    ruling the same morning: "铸对子".
    """

    ordered = tuple(sorted(set(str(value) for value in system_ids)))
    out: list[tuple[str, ...]] = []
    for size in range(len(ordered), 1, -1):
        for subset in combinations(ordered, size):
            if len({family_by_system[member] for member in subset}) < MIN_POOL_FAMILIES:
                continue
            out.append(subset)
    return out
