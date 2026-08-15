def test_conditional_skew_is_silent_without_a_signal() -> None:
    """No conditioning signal means no correction at all.

    The layer this replaces multiplied every tail bucket under 5% by 1.38 on
    every day alike.  That average was measured across all days pooled, so it
    invented mass on whichever side the day's own evidence argued against --
    and a penny bucket is exactly where the market is most confident, so the
    lift manufactured cheap seats on the days the market had the strongest
    reason to price them at a cent.  Absence of evidence about direction is
    not evidence for symmetry.
    """

    from bayes_basket_target.cross_consensus import apply_conditional_tail_skew

    raw = {
        "33": 0.010, "34": 0.080, "35": 0.200, "36": 0.300,
        "37": 0.250, "38": 0.100, "39": 0.040, "40": 0.020,
    }
    ordinals = {key: index for index, key in enumerate(sorted(raw))}
    out, receipt = apply_conditional_tail_skew(raw, ordinals=ordinals)

    assert receipt["state"] == "NO_CONDITIONING_SIGNAL"
    assert receipt["adjusted_buckets"] == []
    assert out == {key: float(value) for key, value in raw.items()}


def test_conditional_skew_widens_both_tails_at_the_same_day_record() -> None:
    """Width is conditioned on where the day sits in its own climatology."""

    from bayes_basket_target.cross_consensus import apply_conditional_tail_skew

    raw = {
        "33": 0.010, "34": 0.080, "35": 0.200, "36": 0.300,
        "37": 0.250, "38": 0.100, "39": 0.040, "40": 0.020,
    }
    ordinals = {key: index for index, key in enumerate(sorted(raw))}
    out, receipt = apply_conditional_tail_skew(
        raw,
        ordinals=ordinals,
        climatology_headroom_sigma=0.3,
    )

    assert receipt["state"] == "APPLIED"
    # A day sitting at its own same-day record is a volatile day in both
    # directions: measured 1.99x/2.15x realized-over-claimed against 1.41x/
    # 1.16x on an ordinary day.  With no forecaster disagreeing, the width
    # widens symmetrically -- the skew term is what breaks symmetry, and it
    # is absent here.
    ample, ample_receipt = apply_conditional_tail_skew(
        raw, ordinals=ordinals, climatology_headroom_sigma=3.0
    )
    assert receipt["base_tail_factor"] > ample_receipt["base_tail_factor"]
    assert out["40"] > ample["40"]
    assert abs(sum(out.values()) - 1.0) < 1e-9


def test_conditional_skew_follows_the_forecaster_when_taf_disagrees() -> None:
    """The airport forecaster reading the same day is independent evidence:
    TAF warmer than the models measured 8.2%/5.2% warm/cold exceedance,
    TAF colder measured 4.9%/12.8% (n=1,791, corr +0.167)."""

    from bayes_basket_target.cross_consensus import apply_conditional_tail_skew

    raw = {
        "33": 0.010, "34": 0.080, "35": 0.200, "36": 0.300,
        "37": 0.250, "38": 0.100, "39": 0.040, "40": 0.020,
    }
    ordinals = {key: index for index, key in enumerate(sorted(raw))}
    warm, warm_receipt = apply_conditional_tail_skew(
        raw, ordinals=ordinals, taf_gap_sigma=1.5
    )
    cold, cold_receipt = apply_conditional_tail_skew(
        raw, ordinals=ordinals, taf_gap_sigma=-1.5
    )

    assert warm_receipt["warm_tail_factor"] > 1.0
    assert warm_receipt["cold_tail_factor"] < 1.0
    assert cold_receipt["warm_tail_factor"] < 1.0
    assert cold_receipt["cold_tail_factor"] > 1.0
    # The two directions move the same bucket opposite ways -- the whole
    # point the unconditional lift could not express.
    assert warm["40"] > cold["40"]
    assert cold["33"] > warm["33"]
    for result in (warm, cold):
        assert abs(sum(result.values()) - 1.0) < 1e-9


def test_conditional_skew_never_reranks_across_the_threshold() -> None:
    """A tail bucket may not be lifted past the threshold that defines it."""

    from bayes_basket_target.cross_consensus import (
        CONDITIONAL_SKEW_THRESHOLD,
        apply_conditional_tail_skew,
    )

    raw = {
        "33": 0.010, "34": 0.080, "35": 0.200, "36": 0.300,
        "37": 0.250, "38": 0.100, "39": 0.045, "40": 0.020,
    }
    ordinals = {key: index for index, key in enumerate(sorted(raw))}
    out, receipt = apply_conditional_tail_skew(
        raw, ordinals=ordinals, taf_gap_sigma=1.5
    )
    pre_total = receipt["pre_normalisation_mass"]
    assert out["39"] * pre_total <= CONDITIONAL_SKEW_THRESHOLD + 1e-12


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
