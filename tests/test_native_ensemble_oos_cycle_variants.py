"""A lead-hour group can be reached from more than one run cycle.

The hour bucket groups runs by how far ahead they were issued, so an 18Z run
two calendar days out and a 00Z run one day out both land in "24-48h ahead" for
some station-days.  A cross-ensemble artifact names one cycle per system, and
P0 matches the artifact's ``run_cycle_by_system`` against the cycles the
forward packet carries -- so every assignment a packet can present needs its
own artifact, and collapsing to a single "best" one makes P0 unmatchable.

The countervailing constraint is that activations are unique on
``(kind, station, system, season, lead, run_cycle, set_hash, generation)``
without the per-system mapping, so two assignments that both label as
MIXED_NATIVE_CYCLES collide and abort the station's whole transaction.
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


def test_an_oversized_product_falls_back_to_one_cycle_per_system() -> None:
    """Four systems at two cycles is sixteen artifacts for one scope.

    That is more calibration than the evidence behind it justifies, so the
    group narrows to each system's best-evidenced cycle rather than being
    dropped -- losing the group would take the station out of training.
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
    assert len(groups) == 1
    assert 2 ** 4 > MAX_CYCLE_ASSIGNMENTS_PER_GROUP
    # 18Z carries more training cases, so it is the retained variant.
    assert all(
        scopes[0][0] == "18Z" for scopes in groups[0][1].values()
    )
