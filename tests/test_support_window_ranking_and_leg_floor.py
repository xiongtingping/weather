"""Ranking by belief, and the per-leg price floor.

Both were measured on the settled book over 2026-07-28..08-15, and both
replace something that looked principled and lost money.
"""

from bayes_basket_target.basket_kernel.contracts import TargetBasketSnapshot


def _bid(ask: float) -> float:
    """One tick under the ask, or nothing at all on a tick-priced leg.

    A leg offered at the minimum tick has no room for a bid beneath it, and
    in the live book it does not have one -- the penny quote is one-sided,
    which is the whole reason it states no probability.
    """

    return 0.0 if ask <= 0.002 else round(ask - 0.001, 4)


def _snapshot(rows: tuple[tuple[str, float, float], ...]) -> object:
    """(bucket, probability, ask) -> a snapshot with one ask level each."""

    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6",
        "snapshot_id": "ranking-fixture",
        "sequence": 1,
        "observed_at": "2026-08-16T03:00:00Z",
        "market_id": "ranking-market",
        "station_id": "ZUUU",
        "event_date": "2026-08-16",
        "quote_version": "quote-v1",
        "quote_observed_at": "2026-08-16T03:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inventory-v1",
        "order_state_version": "order-v1",
        "evidence": {
            "probability_stage": "P0",
            "posterior_version": "p0-v1",
            "posterior_sha256": "a" * 64,
        },
        "buckets": [
            {
                "bucket": bucket,
                "ordinal": index,
                "probability": probability,
                "bid_price": _bid(ask),
                "bid_levels": (
                    [{"price": _bid(ask), "shares": 50.0}]
                    if _bid(ask) > 0.0 else []
                ),
                "ask_levels": [{"price": ask, "shares": 500.0}],
                "price_tick_size": 0.001,
                "maker_queue_ahead_shares": 0.0,
                "maker_queue_known": True,
            }
            for index, (bucket, probability, ask) in enumerate(rows)
        ],
        "checkpoint_changed": True,
        "quote_changed": True,
        "fill_changed": True,
        "time_gate_changed": True,
        "evidence_fresh": True,
        "contract_matches": True,
        "order_state_known": True,
        "risk_inputs_complete": True,
        "time_left_minutes": 1440.0,
    })


def _windows(rows, *, desired, center, minimum=3, maximum=4):
    from bayes_basket_target.delta_v2_runtime import _support_windows

    snapshot = _snapshot(rows)
    return _support_windows(
        snapshot=snapshot,
        desired=set(desired),
        probabilities={
            row.bucket: float(row.probability) for row in snapshot.buckets
        },
        fee_rate=0.0,
        maximum_support_buckets=maximum,
        minimum_support_buckets=minimum,
        center_bucket=center,
    )


def test_the_ladder_extends_toward_belief_not_toward_the_cheap_side() -> None:
    """The flank with more model mass outranks the flank with more edge.

    The old key (KL, and raw edge before it) is a divergence between our
    belief and the market's, so it peaks exactly where the two disagree --
    measured to be where this model is least reliable.  Over 158 settled
    seats the flank it chose was cheaper than its mirror 96% of the time and
    the truth landed on the mirror 83%; of 123 misses, 101 were on the side
    the window declined to extend toward.

    Here the centre is 33C.  Below it sits real mass that the market prices
    fairly; above it sits thin mass the market has nearly ruled out.  Edge
    and KL both prefer the cheap upper flank; belief prefers the lower one.
    """

    rows = (
        ("30C", 0.06, 0.070),
        ("31C", 0.15, 0.150),
        ("32C", 0.25, 0.250),
        ("33C", 0.34, 0.300),
        ("34C", 0.11, 0.020),
        ("35C", 0.06, 0.011),
        ("36C", 0.03, 0.011),
    )
    windows, audit = _windows(
        rows, desired={"33C"}, center="33C", minimum=3, maximum=3
    )
    assert audit["ladder_center_bucket"] == "33C"
    top = tuple(windows[0]["buckets"])
    assert "33C" in top
    lower = {"32C", "31C"} & set(top)
    upper = {"34C", "35C"} & set(top)
    assert lower and not upper, (
        f"ranked window {top} extended toward the cheap flank; "
        "the ranking must follow probability mass"
    )
    # And the cheap-flank window is still *present* -- this is a ranking
    # change, not an exclusion.  The curve score still gets to see it.
    assert any(
        "34C" in window["buckets"] for window in windows
    ), "the cheap flank must remain a candidate, only ranked below"


def test_ranking_is_monotone_in_probability_mass() -> None:
    rows = (
        ("30C", 0.05, 0.10),
        ("31C", 0.10, 0.10),
        ("32C", 0.20, 0.10),
        ("33C", 0.30, 0.10),
        ("34C", 0.20, 0.10),
        ("35C", 0.10, 0.10),
        ("36C", 0.05, 0.10),
    )
    windows, _ = _windows(rows, desired={"33C"}, center="33C")
    priced = [w for w in windows if w["all_in_price"] is not None]
    masses = [float(w["win_probability"]) for w in priced]
    assert masses == sorted(masses, reverse=True)


def test_a_penny_leg_is_excluded_not_the_window() -> None:
    """A sub-floor rung leaves the domain; the rungs beside it do not.

    Measured on the settled book: every window whose cheapest leg was under
    a cent returned +487% while all four dearer strata returned between -12%
    and -100%.  The entire apparent edge lived on one-sided tail offers.

    But voiding the whole window for one of them threw away the rungs beside
    it that were priced perfectly well (user ruling, 2026-08-17).  The floor
    now applies to the candidate domain, so the cheap rung is simply not a
    candidate and the enumeration grows contiguous runs from what remains --
    which is why this cannot leave a hole in a window.
    """

    from bayes_basket_target.delta_v2_runtime import MINIMUM_LEG_ALL_IN_ASK

    rows_index = {
        "30C": 0, "31C": 1, "32C": 2, "33C": 3, "34C": 4, "35C": 5,
    }
    rows = (
        ("30C", 0.05, 0.060),
        ("31C", 0.10, 0.080),
        ("32C", 0.25, 0.200),
        ("33C", 0.30, 0.250),
        ("34C", 0.20, 0.001),   # penny leg
        ("35C", 0.10, 0.150),
    )
    windows, audit = _windows(rows, desired={"33C"}, center="33C")
    assert MINIMUM_LEG_ALL_IN_ASK == 0.02
    assert "34C" in audit["leg_floor_excluded_buckets"]
    for window in windows:
        assert "34C" not in window["buckets"], (
            f"{window['buckets']} still contains an excluded rung"
        )
    # The rungs beside it are still seatable -- that is the whole change.
    assert any(
        window["all_in_price"] is not None and len(window["buckets"]) >= 3
        for window in windows
    ), "excluding one rung must not cost the window its neighbours"
    # And nothing spans the hole it left.
    for window in windows:
        ordinals = [rows_index[b] for b in window["buckets"]]
        assert max(ordinals) - min(ordinals) == len(ordinals) - 1, (
            f"{window['buckets']} spans the excluded rung"
        )


def test_the_floor_is_per_leg_not_per_basket() -> None:
    """A basket average hides the case the floor exists for.

    Three fair legs plus one penny leg prices like a fair basket -- the
    whole-basket cost clears any sane floor while the payout geometry is
    entirely the penny leg's.
    """

    rows = (
        ("30C", 0.05, 0.100),
        ("31C", 0.10, 0.150),
        ("32C", 0.25, 0.300),
        ("33C", 0.30, 0.350),
        ("34C", 0.20, 0.002),
        ("35C", 0.10, 0.200),
    )
    windows, _ = _windows(rows, desired={"33C"}, center="33C")
    offending = [
        w for w in windows if "34C" in w["buckets"]
    ]
    assert not offending, (
        "a window averaging well above the floor still got a penny leg"
    )


def test_an_inherited_support_gets_no_exemption_from_the_leg_floor() -> None:
    """The incumbent reaches the list without passing the loop's floors.

    It must not thereby keep a lottery leg the enumerated windows are
    refused -- the floor is about what the quote means, not about which
    window earned its place.
    """

    rows = (
        ("30C", 0.05, 0.100),
        ("31C", 0.10, 0.150),
        ("32C", 0.25, 0.300),
        ("33C", 0.30, 0.350),
        ("34C", 0.20, 0.001),
        ("35C", 0.10, 0.200),
    )
    windows, _ = _windows(
        rows, desired={"33C", "34C"}, center="33C", minimum=2
    )
    incumbent = [w for w in windows if w["is_incumbent"]]
    assert incumbent, "the incumbent must still be listed"
    assert incumbent[0]["all_in_price"] is None, (
        "an incumbent resting on a penny leg must not be offered a price"
    )


# Every gate that can refuse an enumerated window keeps a counter, and the
# counters close against the enumeration.  This is the invariant that stops a
# third occurrence of a bug that has now happened twice: the ledger explains a
# refusal to the page purely in terms of these numbers, so a gate without one
# gets its refusals blamed on whichever gate is listed.  The leg floor was
# unlisted on 2026-08-17 (112 P0 refusals attributed to a centre-bucket count
# that could not explain them) and the edge/friction gate was unlisted until
# 2026-08-20 (308 station-days filed under "the market priced us out" when the
# structural floors had taken the whole enumeration).


def test_the_refusal_counters_close_against_the_enumeration() -> None:
    """Refusals plus shortlist equals every window enumerated.

    Checked across shapes that exercise different gates, because a counter
    that closes on one shape and not another is the same defect with a
    narrower blast radius.
    """

    from bayes_basket_target.delta_v2_runtime import _window_audit_closes

    shapes = (
        # A day the structural floors take whole.
        (
            (("30C", 0.20, 0.250), ("31C", 0.50, 0.550), ("32C", 0.30, 0.330)),
            {"31C"}, "31C", 3, 4,
        ),
        # A day with a penny leg inside the only legal window.
        (
            (("30C", 0.20, 0.250), ("31C", 0.50, 0.550), ("32C", 0.30, 0.001)),
            {"31C"}, "31C", 3, 4,
        ),
        # A wide day where several windows reach the price test.
        (
            (
                ("30C", 0.06, 0.070), ("31C", 0.15, 0.150),
                ("32C", 0.25, 0.250), ("33C", 0.34, 0.300),
                ("34C", 0.11, 0.020), ("35C", 0.06, 0.011),
                ("36C", 0.03, 0.011),
            ),
            {"33C"}, "33C", 3, 4,
        ),
    )
    for rows, desired, center, minimum, maximum in shapes:
        _, audit = _windows(
            rows, desired=desired, center=center,
            minimum=minimum, maximum=maximum,
        )
        assert _window_audit_closes(audit), (
            "counters do not account for the enumeration on %s: %r"
            % (center, audit)
        )


def test_the_edge_friction_gate_is_counted() -> None:
    """A window refused on price alone must say so.

    Every leg here carries a real quote well above the floor, the window
    holds the centre and clears the length floor -- and the model's edge over
    the all-in price does not clear tick-rounding across the legs.  Before
    this counter existed the branch refused silently, and the ledger printed
    three structural counts as the explanation of a refusal none of them had
    caused.
    """

    rows = (
        ("30C", 0.32, 0.330),
        ("31C", 0.36, 0.370),
        ("32C", 0.32, 0.330),
    )
    windows, audit = _windows(
        rows, desired={"31C"}, center="31C", minimum=3, maximum=3
    )
    assert not [
        row for row in windows if row.get("all_in_price") is not None
    ], "the fixture must leave nothing priced on the shortlist"
    assert audit["edge_refused_window_count"] > 0, (
        "the price gate refused the only legal window and did not count it"
    )
    assert audit["last_refusing_gate"] == "edge"


def test_the_last_gate_names_the_refusal_not_the_loudest_counter() -> None:
    """The leg floor kills the last window while being outvoted.

    This is the shape the old ``leg_floor > centre + short`` test could never
    win.  The centre rung and the length floor refuse the windows that were
    never candidates -- there are always more of those -- and the one window
    that could still have been seated dies on the leg floor.  Counting votes
    hands the verdict to whichever gate sits earliest in the funnel, every
    time.
    """

    # The penny leg has to arrive through the inherited support: a bucket
    # quoted under the floor is dropped from the domain outright, so the
    # in-loop floor only ever sees one the resolver put back.
    rows = (
        ("30C", 0.20, 0.250),
        ("31C", 0.50, 0.550),
        ("32C", 0.30, 0.001),
    )
    _, audit = _windows(
        rows, desired={"31C", "32C"}, center="31C", minimum=3, maximum=3
    )
    centre_out = audit["center_refused_window_count"]
    short_out = audit["short_refused_window_count"]
    leg_out = audit["leg_floor_refused_window_count"]
    assert leg_out > 0
    assert leg_out <= centre_out + short_out, (
        "fixture no longer reproduces the outvoted-leg-floor shape"
    )
    assert audit["last_refusing_gate"] == "leg_floor"
