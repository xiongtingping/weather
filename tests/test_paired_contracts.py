"""Contracts that live in two places, checked against each other in one.

Eight defects landed in a single day on 2026-08-18 and every one had the same
shape: a rule split across two sites, one site updated.  The affine bias form
was fitted, gated and folded into the cross weights while never being
persisted, so production served the constant form for months against a
certificate earned by another.  The spread cap was raised in training and left
at the old value in the serving validator, and 41 stations failed closed.  The
health check kept reading train RMSE after the fit moved to residual sigma and
reported 127 of 510 live scopes as broken.  The radiation coefficient was a
bare constant with nothing recording which value an artifact had been gated
at.  The centre/width evidence bound was placed on event date when the thing
it had to exclude was a decision timestamp.

The repository has over a thousand test files and none of them caught any of
it, because each site was individually correct and individually tested.  What
was missing was anywhere that named the pairs.

This file is that register.  Adding a contract here is cheap and the check is
uniform: state the two halves, state how to read each, and let one test assert
they agree.  A pair that cannot be expressed as two readable values does not
belong here -- it belongs in a test of its own, next to the code.
"""

from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _fit_parameters_keys() -> set[str]:
    from bayes_basket_target import native_ensemble_oos as oos

    cases = [
        {
            "event_date": f"2026-07-{day:02d}",
            "values": [20.0 + day * 0.1, 21.0 + day * 0.1],
            "actual_tmax_c": 21.5 + day * 0.1,
        }
        for day in range(1, 26)
    ]
    return set(oos._fit_parameters(cases))


def _persisted_parameter_keys() -> set[str]:
    """Every fitted key the artifact writer actually carries.

    Read from the writer's source rather than from a duplicated list, so the
    register cannot drift the way the thing it is guarding did.
    """

    source = _source("bayes_basket_target/native_ensemble_oos.py")
    start = source.index('"bias_parameters_json": _json({')
    end = source.index('"member_qc_contract_sha256"', start)
    return set(re.findall(r'parameters\[\s*"([a-z0-9_]+)"', source[start:end]))


# Each entry: what the contract is, and two callables whose values must agree.
# The description is the point -- it says what breaks when they disagree, in
# the terms of the incident that put the pair here.
PAIRED_CONTRACTS: tuple[tuple[str, Callable[[], Any], Callable[[], Any]], ...] = (
    (
        "Every fitted calibration parameter reaches an artifact column. "
        "A fitted key nobody persists is math the OOS gate scored and "
        "serving cannot see -- how the affine bias form ran for months "
        "against a certificate earned by a different fit.",
        _fit_parameters_keys,
        _persisted_parameter_keys,
    ),
    (
        "The training spread cap equals the serving spread ceiling. "
        "Raised in training and left in the validator, it failed 41 "
        "stations closed on 2026-08-15.",
        lambda: _clamp_ceiling(
            "bayes_basket_target/native_ensemble_oos.py",
            r"spread_factor = min\(\s*([0-9.]+)",
        ),
        lambda: _clamp_ceiling(
            "bayes_basket_target/native_ensemble_p0.py",
            r"spread\s*<=\s*([0-9.]+)",
        ),
    ),
    (
        "The health audit judges width by the scale the fit sizes it with. "
        "Reading RMSE after the fit moved to residual sigma demanded back "
        "exactly the width the bias correction had removed, and reported "
        "127 of 510 live scopes as too narrow.",
        lambda: _reads_key(
            "bayes_basket_target/native_ensemble_oos.py",
            "spread_factor = min",
            "residual_sigma",
        ),
        lambda: _reads_key(
            "scripts/audit_native_calibration_health.py",
            "def check_spread_bound",
            "train_residual_sigma_c",
        ),
    ),
    (
        "The Brier tolerance thresholds the audit reports are the ones the "
        "gate applies.  Duplicated to keep a cron audit out of the serving "
        "import graph, and duplicated constants that drift describe a gate "
        "nobody runs.",
        lambda: _gate_tolerances(),
        lambda: _audit_tolerances(),
    ),
    (
        "The radiation coefficient the gate scores is the one the artifact "
        "pins.  It was a bare constant, so moving it would have re-served "
        "every artifact already admitted at the old value.",
        lambda: _reads_key(
            "bayes_basket_target/native_ensemble_oos.py",
            "conditioning_by_day = _holdout_conditioning_signals",
            "radiation_shift_sigma=scope_radiation_sigma",
        ),
        lambda: _reads_key(
            "bayes_basket_target/native_ensemble_oos.py",
            '"::radiation_shift_sigma="',
            "candidate['radiation_shift_sigma']",
        ),
    ),
    (
        "Serving reads the radiation coefficient from the pin, not from the "
        "module constant, or an artifact is served a shift its gate never "
        "scored.",
        lambda: _reads_key(
            "bayes_basket_target/native_ensemble_p0.py",
            "forecast_radiation_shift_sigma(",
            "pinned_radiation_shift_sigma",
        ),
        lambda: True,
    ),
    (
        "The centre/width evidence bound is on the decision timestamp.  As "
        "an event-date bound it admitted the very decisions it existed to "
        "exclude: event date 2026-08-19 carries decisions from 2026-08-17.",
        lambda: _reads_key(
            "bayes_basket_target/center_width_store.py",
            "def serving_cells_by_day",
            "decision_at_utc>=?",
        ),
        lambda: _reads_key(
            "scripts/daily_calibration_training.sh",
            "CW_SINCE=",
            "T",  # a timestamp, not a bare date
        ),
    ),
    (
        "The evaluator and the server centre a distribution identically. "
        "An evaluator that centres differently hands out certificates it "
        "did not earn.",
        lambda: _reads_key(
            "bayes_basket_target/native_ensemble_oos.py",
            "def _corrected_center",
            'mean + float(parameters["bias_c"])',
        ),
        lambda: _reads_key(
            "bayes_basket_target/native_ensemble_p0.py",
            "_calibrated_system_probability",
            "bias_c",
        ),
    ),
)


def _clamp_ceiling(relative: str, pattern: str) -> float:
    match = re.search(pattern, _source(relative))
    if match is None:
        raise AssertionError(f"clamp not found in {relative}: {pattern}")
    return float(match.group(1))


def _reads_key(relative: str, anchor: str, needle: str) -> bool:
    """Whether ``needle`` appears in the region following ``anchor``.

    Deliberately a source read.  These pairs are about which name a call site
    uses, and a runtime probe would need the whole pipeline standing up to
    answer a question the text already answers.
    """

    source = _source(relative)
    if anchor not in source:
        raise AssertionError(f"anchor missing in {relative}: {anchor}")
    start = source.index(anchor)
    return needle in source[start:start + 4000]


def _gate_tolerances() -> tuple[float, float]:
    from bayes_basket_target import native_ensemble_oos as oos

    return (
        oos.BRIER_TOLERANCE_MAX_RELATIVE_LOSS,
        oos.BRIER_TOLERANCE_MIN_RELATIVE_GAIN,
    )


def _audit_tolerances() -> tuple[float, float]:
    import sys

    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import audit_native_calibration_health as audit

    return (
        audit.BRIER_TOLERANCE_MAX_LOSS,
        audit.BRIER_TOLERANCE_MIN_GAIN,
    )


class PairedContractTest(unittest.TestCase):
    def test_every_registered_pair_agrees(self) -> None:
        failures = []
        for description, left, right in PAIRED_CONTRACTS:
            try:
                left_value, right_value = left(), right()
            except Exception as error:  # noqa: BLE001 -- reported, not raised
                failures.append(f"{description}\n    unreadable: {error!r}")
                continue
            if left_value != right_value:
                failures.append(
                    f"{description}\n"
                    f"    one half: {left_value!r}\n"
                    f"    other half: {right_value!r}"
                )
        self.assertFalse(
            failures,
            "paired contracts disagree:\n\n" + "\n\n".join(failures),
        )

    def test_the_register_is_not_empty_and_每条都有说明(self) -> None:
        """A pair without a description is a pair nobody can act on.

        The failure message is the whole value of this file: whoever trips it
        is usually not whoever wrote the pair, and needs to know what breaks.
        """

        self.assertGreaterEqual(len(PAIRED_CONTRACTS), 8)
        for description, left, right in PAIRED_CONTRACTS:
            self.assertGreater(len(description), 60, description)
            self.assertTrue(callable(left) and callable(right))

    def test_a_broken_pair_is_reported_and_not_swallowed(self) -> None:
        """An unreadable half must fail loudly rather than pass vacuously.

        The likeliest way this register rots is a refactor moving an anchor,
        leaving a check that reads nothing and asserts nothing.
        """

        with self.assertRaises(AssertionError):
            _reads_key(
                "bayes_basket_target/native_ensemble_oos.py",
                "a-string-that-is-not-in-this-file",
                "anything",
            )


if __name__ == "__main__":
    unittest.main()
