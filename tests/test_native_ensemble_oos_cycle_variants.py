"""A lead-hour group can be reached from more than one run cycle.

The hour bucket groups runs by how far ahead they were issued, so an 18Z run
two calendar days out and a 00Z run one day out both land in "24-48h ahead" for
some station-days.  A cross-ensemble artifact names one cycle per system, and
P0 matches the artifact's ``run_cycle_by_system`` against the cycles the
forward packet carries -- so every assignment a packet can present needs its
own artifact, and collapsing to a single "best" one makes P0 unmatchable.

Past the enumeration bound a group narrows to each system's best-evidenced
cycle, and since 2026-10-10 it also keeps the cycle maps the scope's recent
packets and its served artifact carry (``required_maps``): narrowed alone, the
packet's own map went unminted and P0 coasted on a thirty-day-old fit (ZUUU
h12_24 served a 09-28 fit into October, missing the newer layers).  Two maps
of one MIXED_NATIVE_CYCLES label take successive activation generations; the
build keys its one-candidate-per-identity guard on the map too.
"""

from __future__ import annotations

from bayes_basket_target.native_ensemble_oos import (
    MAX_CYCLE_ASSIGNMENTS_PER_GROUP,
    _cycle_variant_groups,
)


def _evaluation(*, holdout_n: int = 8, train_n: int = 25) -> dict[str, int]:
    return {"holdout_n": holdout_n, "train_n": train_n}


def test_each_cycle_assignment_becomes_its_own_group() -> None:
    groups = _cycle_variant_groups({
        ("JJA", "h24_48"): {
            "PF50": [("00Z", _evaluation()), ("18Z", _evaluation())],
            "GEFS": [("00Z", _evaluation()), ("18Z", _evaluation())],
        },
    })
    assignments = {
        tuple(
            (system_id, scopes[0][0])
            for system_id, scopes in sorted(system_rows.items())
        )
        for _scope_key, system_rows in groups
    }
    assert assignments == {
        (("GEFS", "00Z"), ("PF50", "00Z")),
        (("GEFS", "00Z"), ("PF50", "18Z")),
        (("GEFS", "18Z"), ("PF50", "00Z")),
        (("GEFS", "18Z"), ("PF50", "18Z")),
    }


def test_every_group_carries_exactly_one_cycle_per_system() -> None:
    """The loop body downstream reads ``scopes[0]`` and assumes it is alone."""

    groups = _cycle_variant_groups({
        ("JJA", "h24_48"): {
            "PF50": [("00Z", _evaluation()), ("18Z", _evaluation())],
            "GEFS": [("18Z", _evaluation())],
        },
    })
    assert groups
    for _scope_key, system_rows in groups:
        assert all(len(scopes) == 1 for scopes in system_rows.values())


def test_single_cycle_groups_are_unchanged() -> None:
    original = {
        ("JJA", "h12_24"): {
            "PF50": [("18Z", _evaluation())],
            "GEFS": [("18Z", _evaluation())],
        },
    }
    groups = _cycle_variant_groups(original)
    assert len(groups) == 1
    assert groups[0][0] == ("JJA", "h12_24")
    assert sorted(groups[0][1]) == ["GEFS", "PF50"]


def test_assignments_are_emitted_in_a_fixed_order() -> None:
    """Same evidence, same artifacts -- otherwise a rerun churns activations."""

    scope_groups = {
        ("JJA", "h24_48"): {
            "PF50": [("18Z", _evaluation()), ("00Z", _evaluation())],
            "GEFS": [("18Z", _evaluation()), ("00Z", _evaluation())],
        },
    }
    first = [
        [(sid, s[0][0]) for sid, s in sorted(rows.items())]
        for _key, rows in _cycle_variant_groups(scope_groups)
    ]
    assert first == [
        [("GEFS", "00Z"), ("PF50", "00Z")],
        [("GEFS", "00Z"), ("PF50", "18Z")],
        [("GEFS", "18Z"), ("PF50", "00Z")],
        [("GEFS", "18Z"), ("PF50", "18Z")],
    ]


def test_the_live_maximum_of_four_systems_two_cycles_enumerates_fully() -> None:
    """Four ready systems at two cycles each must not trigger the collapse.

    The collapse keeps one best-evidenced cycle per system, which is exactly
    the freeze the cohort grouper exists to prevent: the cycle the serving
    packet carries gets deduped away and its weight row ages while the
    correction rows advance (38 scopes measured on 2026-08-15).
    """

    scope_groups = {
        ("JJA", "h24_48"): {
            f"SYS{index}": [
                ("00Z", _evaluation(holdout_n=8, train_n=20)),
                ("18Z", _evaluation(holdout_n=8, train_n=25)),
            ]
            for index in range(4)
        },
    }
    groups = _cycle_variant_groups(scope_groups)
    assert 2 ** 4 == MAX_CYCLE_ASSIGNMENTS_PER_GROUP
    assert len(groups) == 16


def test_an_oversized_product_falls_back_to_one_cycle_per_system() -> None:
    """Past the enumeration bound the group narrows, it is not dropped.

    Losing the group would take the station out of training; the bound only
    exists so a runaway registry cannot explode the enumeration.
    """

    scope_groups = {
        ("JJA", "h24_48"): {
            f"SYS{index}": [
                ("00Z", _evaluation(holdout_n=8, train_n=20)),
                ("18Z", _evaluation(holdout_n=8, train_n=25)),
            ]
            for index in range(5)
        },
    }
    groups = _cycle_variant_groups(scope_groups)
    assert len(groups) == 1
    assert 2 ** 5 > MAX_CYCLE_ASSIGNMENTS_PER_GROUP
    # 18Z carries more training cases, so it is the retained variant.
    assert all(
        scopes[0][0] == "18Z" for scopes in groups[0][1].values()
    )


def _five_by_two() -> dict:
    return {
        ("JJA", "h24_48"): {
            f"SYS{index}": [
                ("00Z", _evaluation(holdout_n=8, train_n=20)),
                ("18Z", _evaluation(holdout_n=8, train_n=25)),
            ]
            for index in range(5)
        },
    }


def _maps(groups) -> list[dict[str, str]]:
    return [
        {system_id: scopes[0][0] for system_id, scopes in variant.items()}
        for _scope, variant in groups
    ]


def test_a_narrowed_group_still_mints_the_packets_cycle_map() -> None:
    """2026-10-10: the packet's map is minted beside the narrowed one."""

    packet = {f"SYS{index}": "00Z" for index in range(5)}
    groups = _cycle_variant_groups(
        _five_by_two(), {("JJA", "h24_48"): [packet]}
    )
    assert _maps(groups) == [
        {f"SYS{index}": "18Z" for index in range(5)},
        packet,
    ]


def test_a_system_the_map_does_not_reach_keeps_its_best_cycle() -> None:
    groups = _cycle_variant_groups(
        _five_by_two(),
        {("JJA", "h24_48"): [{"SYS0": "00Z", "SYS9": "00Z", "SYS1": "06Z"}]},
    )
    assert _maps(groups)[1] == {
        "SYS0": "00Z", "SYS1": "18Z", "SYS2": "18Z", "SYS3": "18Z", "SYS4": "18Z",
    }


def test_a_map_already_minted_is_not_repeated() -> None:
    narrowed = {f"SYS{index}": "18Z" for index in range(5)}
    packet = {f"SYS{index}": "00Z" for index in range(5)}
    groups = _cycle_variant_groups(
        _five_by_two(), {("JJA", "h24_48"): [narrowed, packet, packet]}
    )
    assert _maps(groups) == [narrowed, packet]


def test_a_group_under_the_bound_ignores_the_maps() -> None:
    """Fully enumerated already; nothing is added or reordered."""

    scope_groups = {
        ("JJA", "h24_48"): {
            f"SYS{index}": [
                ("00Z", _evaluation()), ("18Z", _evaluation()),
            ]
            for index in range(4)
        },
    }
    plain = _cycle_variant_groups(scope_groups)
    assert _cycle_variant_groups(
        scope_groups, {("JJA", "h24_48"): [{"SYS0": "00Z"}]}
    ) == plain
