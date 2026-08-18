"""Which activation pointers does removing the affine form actually rebind?

A single-system artifact drifts when its own train_rmse_c changes.  A cross
selection drifts when ANY member system's train_rmse_c changes, because the
weights are normalised across the cohort -- so the cross blast radius is the
one that matters for P0 fail-closed, and it is larger than the per-system rate.
"""

from __future__ import annotations

import pickle
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/xiong/weather")
sys.path.insert(0, "/Users/xiong/weather/tmp")

from affine_study import as_cases, fit_constant

from bayes_basket_target import native_ensemble_oos as oos

CASES = "/Users/xiong/weather/tmp/affine_cases.pkl"
MIN_HOLDOUT = 5


def main() -> None:
    with open(CASES, "rb") as handle:
        data = pickle.load(handle)

    # NOTE: this reads the CURRENT (pre-change) fit, i.e. the code as shipped.
    # It is run against the stashed-in working tree, so import the branch that
    # still has the affine form only if you want the "before" picture; here we
    # recompute the choice explicitly so the answer does not depend on which
    # revision is checked out.
    from affine_study import fit_with_rule

    affine_systems: set[tuple] = set()
    cross_scopes: dict[tuple, list[str]] = defaultdict(list)
    for key, rows in sorted(data.items()):
        station, system, season, lead, cycle = key
        cases = as_cases(rows)
        if len(cases) < MIN_HOLDOUT + 12:
            continue
        train = cases[:-MIN_HOLDOUT]
        cross_scopes[(station, season, lead, cycle)].append(system)
        params = fit_with_rule(train, 6, 0.8)  # the rule as shipped
        if str(params.get("correction_form")) == "affine":
            affine_systems.add(key)

    total_systems = sum(len(v) for v in cross_scopes.values())
    dirty_cross = {
        scope
        for scope, systems in cross_scopes.items()
        if any(
            (scope[0], system, scope[1], scope[2], scope[3]) in affine_systems
            for system in systems
        )
    }
    dirty_stations = {scope[0] for scope in dirty_cross}

    print("Rebind blast radius of removing the affine form")
    print("=" * 60)
    print(f"  single-system artifacts that change train_rmse_c : "
          f"{len(affine_systems)}/{total_systems} "
          f"({100.0 * len(affine_systems) / total_systems:.0f}%)")
    print(f"  cross selections whose weights therefore shift   : "
          f"{len(dirty_cross)}/{len(cross_scopes)} "
          f"({100.0 * len(dirty_cross) / len(cross_scopes):.0f}%)")
    print(f"  stations touched                                 : "
          f"{len(dirty_stations)}/45")
    print()
    print("  clean stations (no scope drifts, pointers survive):")
    clean = sorted({scope[0] for scope in cross_scopes} - dirty_stations)
    print(f"    {', '.join(clean) if clean else '(none)'}")
    print()
    print("  per-station dirty cross-scope counts:")
    by_station: dict[str, int] = defaultdict(int)
    totals: dict[str, int] = defaultdict(int)
    for scope in cross_scopes:
        totals[scope[0]] += 1
    for scope in dirty_cross:
        by_station[scope[0]] += 1
    for station in sorted(by_station, key=lambda s: -by_station[s]):
        print(f"    {station}: {by_station[station]}/{totals[station]}")


if __name__ == "__main__":
    main()
