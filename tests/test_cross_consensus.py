def test_the_conditional_tail_skew_is_deleted_not_dormant() -> None:
    """Charter 43 amendment (2026-09-22): the layer is gone, not switched off.

    New artifacts stopped carrying ``conditional_tail_skew_v1`` on 2026-08-18
    (d63426a7) and the last tagged artifact's window closed
    2026-09-17T09:11:35Z; of the 82,870 cross artifacts valid at the audit
    none carried the tag and none of 11,758 stable-pool P0 rows recorded a
    tail receipt.  Kept code there would read as a live rule to the next
    reviewer and kept the OOS holdout reading ten years of same-day
    climatology plus the station's whole truth record per call for nothing.
    """

    from bayes_basket_target import cross_consensus
    from bayes_basket_target import native_ensemble_oos
    from bayes_basket_target import native_ensemble_p0

    for name in (
        "apply_conditional_tail_skew",
        "CONDITIONAL_SKEW_METHOD_TAG",
        "CLIMATOLOGY_HEADROOM_BANDS",
        "TAF_DIRECTION_WARM",
    ):
        assert not hasattr(cross_consensus, name), name
    assert not hasattr(native_ensemble_p0, "_same_day_climatology_headroom")
    assert not hasattr(native_ensemble_p0, "apply_conditional_tail_skew")
    assert not hasattr(native_ensemble_oos, "apply_conditional_tail_skew")


def test_band_reliability_shrinks_the_shoulder_and_lifts_the_core() -> None:
    """Only the cells with sign-stable evidence act; the rest serve 1.0.

    Measured on 254 settled station-days: claims in [0.02, 0.12) realise
    0.69x of what they say (interval [0.55, 0.86]) and claims in
    [0.12, 0.20) realise 1.23x ([1.08, 1.39]); both keep their sign across a
    chronological half-split.  [0.20, 0.35) qualified through the OOS
    prediction archive and transfer validation.  The deep tail's 1.74x
    reverses sign between the serving and gate geometries, and fitting it
    anyway scored -0.97 NLL/day forward -- so it serves identity until its
    interval excludes one on the geometry it would be served on.
    """

    from bayes_basket_target.cross_consensus import apply_band_reliability

    raw = {
        "33": 0.010,   # deep tail: identity
        "34": 0.060,   # shoulder: shrinks
        "35": 0.150,   # core: lifts
        "36": 0.500,   # high core: identity
        "37": 0.280,   # 0.20-0.35: lifts
    }
    out, receipt = apply_band_reliability(raw)

    assert receipt["state"] == "APPLIED"
    assert receipt["applied_factors"] == {"34": 0.86, "35": 1.08, "37": 1.10}
    assert abs(sum(out.values()) - 1.0) < 1e-12
    # The shoulder bucket loses share and the lifted buckets gain share,
    # relative to the untouched buckets.
    assert out["34"] / raw["34"] < out["33"] / raw["33"]
    assert out["35"] / raw["35"] > out["33"] / raw["33"]
    assert out["37"] / raw["37"] > out["33"] / raw["33"]
    # Identity buckets all move by the same renormalisation alone.
    ratios = {key: out[key] / raw[key] for key in ("33", "36")}
    assert max(ratios.values()) - min(ratios.values()) < 1e-12


def test_band_reliability_is_identity_outside_the_qualified_bands() -> None:
    """A distribution with no mass in a qualified band passes unchanged."""

    from bayes_basket_target.cross_consensus import apply_band_reliability

    raw = {"35": 0.60, "36": 0.39, "37": 0.010}
    out, receipt = apply_band_reliability(raw)

    assert receipt["state"] == "NO_BAND_IN_SUPPORT"
    assert receipt["applied_factors"] == {}
    assert out == {key: float(value) for key, value in raw.items()}


def test_band_reliability_reads_the_claim_not_the_raw_input() -> None:
    """The band is the claim as it stands when the layer runs.

    The factors were measured on served distributions, so a bucket sitting
    at 0.119 must take the shoulder factor even though its post-shrink value
    leaves the band -- banding on the output would be solving a fixed point
    the measurement never saw.
    """

    from bayes_basket_target.cross_consensus import apply_band_reliability

    raw = {"35": 0.119, "36": 0.881}
    out, receipt = apply_band_reliability(raw)

    assert receipt["applied_factors"] == {"35": 0.86}
    assert out["35"] < raw["35"]
