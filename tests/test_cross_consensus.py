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


def test_the_band_reliability_layer_is_deleted_not_dormant() -> None:
    """Charter 232 (2026-09-26): the band layer retired with its lane."""

    from bayes_basket_target import cross_consensus
    from bayes_basket_target import native_ensemble_p0
    from bayes_basket_target import served_shape

    for name in (
        "apply_band_reliability",
        "BAND_RELIABILITY_METHOD_TAG",
        "BAND_RELIABILITY_METHOD_TAG_V2",
        "BAND_RELIABILITY_FACTORS",
    ):
        assert not hasattr(cross_consensus, name), name
    assert not hasattr(native_ensemble_p0, "resolve_pinned_band_layer")
    assert not hasattr(served_shape, "BandLayer")
    assert served_shape.SERVED_TABLE_LAYER_ORDER == ("center_width",)


def test_charter_246_bridge_ramp_starts_at_one_degree_and_is_full_by_one_and_a_half() -> None:
    from bayes_basket_target import cross_consensus as cc

    assert (cc.CONSENSUS_BRIDGE_GAP_START_C, cc.CONSENSUS_BRIDGE_GAP_FULL_C, cc.CONSENSUS_BRIDGE_LAMBDA_MAX) == (1.0, 1.5, 0.4)
    assert cc.consensus_bridge_lambda(1.0) == 0.0
    assert abs(cc.consensus_bridge_lambda(1.25) - 0.2) < 1e-12
    assert cc.consensus_bridge_lambda(1.5) == 0.4 and cc.consensus_bridge_lambda(3.0) == 0.4
