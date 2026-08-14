

def test_tail_reliability_lifts_deep_tails_and_preserves_order() -> None:
    """The measured 1.38x tail under-estimate, applied honestly.

    Deep tails lift by the full factor before renormalisation, buckets near
    the threshold taper to it so nothing re-ranks across the boundary, mass
    above the threshold only pays the renormalisation, and the result is a
    distribution again.
    """

    from bayes_basket_target.cross_consensus import (
        TAIL_RELIABILITY_FACTOR,
        TAIL_RELIABILITY_THRESHOLD,
        apply_tail_reliability,
    )

    raw = {
        "33": 0.010, "34": 0.080, "35": 0.200, "36": 0.300,
        "37": 0.250, "38": 0.100, "39": 0.040, "40": 0.020,
    }
    out, receipt = apply_tail_reliability(raw)
    assert abs(sum(out.values()) - 1.0) < 1e-9
    assert receipt["lifted_buckets"] == ["33", "39", "40"]
    # Deep tail: full factor pre-normalisation.
    assert out["33"] > raw["33"]
    pre_total = receipt["pre_normalisation_mass"]
    assert abs(out["33"] * pre_total - raw["33"] * TAIL_RELIABILITY_FACTOR) < 1e-9
    # Near-threshold: tapered to the threshold, never past it.
    assert abs(out["39"] * pre_total - TAIL_RELIABILITY_THRESHOLD) < 1e-9
    # Order preserved.
    ranked_in = sorted(raw, key=lambda k: raw[k])
    ranked_out = sorted(raw, key=lambda k: out[k])
    assert ranked_in == ranked_out
    # Above-threshold buckets untouched pre-normalisation.
    assert abs(out["36"] * pre_total - raw["36"]) < 1e-9
