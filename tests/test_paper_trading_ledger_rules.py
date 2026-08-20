def test_the_ledger_carries_productions_structural_rules() -> None:
    """A measurement lane that drops a live rule measures a fiction.

    The ledger exists to reproduce what production seats.  Its first run
    recorded one- and two-bucket seats that charter 50 forbids, for two
    reasons that both look harmless in isolation: it called the window
    enumeration without a minimum, which defaults to one, and it passed a
    bare centre bucket as the evidence support -- and the incumbent is
    exempt from every floor, so that one bucket was seatable no matter what
    the floor said.

    A raw probability core can also be wider than the cap, and an over-wide
    incumbent is exempt from that too, which is how a five-bucket basket
    reached the book.
    """

    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "paper_trading_ledger.py"
    ).read_text(encoding="utf-8")
    assert "MINIMUM_SUPPORT_BUCKETS = 3" in source
    assert "minimum_support_buckets=MINIMUM_SUPPORT_BUCKETS" in source
    # The evidence support is the mass-selected run, not a bare centre.
    assert "desired=set(core) or {center}" in source
    assert "desired={center}," not in source
    # And it is trimmed to the cap before it can act as an incumbent.
    assert "if len(core) > MAXIMUM_SUPPORT_BUCKETS:" in source
    # The floor is per stage: charter 50a gives P1 two rungs while the
    # opening stages keep three.  Held to the opening floor, the ledger
    # reported P1 refusing seats production takes.
    assert "probability_stage=stage," in source


def test_recorded_seats_obey_the_ladder_bounds() -> None:
    """Whatever is in the book now must satisfy the rules now.

    On the *posted* window, not the filled one.  Entry is progressive, so a
    recorded position is whatever the book came down to and can legitimately
    be a single rung of a basket that was chosen with three.  The minimum
    support is a rule about what may be chosen, so that is where it is
    checked -- which is why the posted window is recorded separately at all.
    """

    import sqlite3
    from pathlib import Path

    db = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "bayes_basket_target_ssot.db"
    )
    if not db.is_file():
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = conn.execute(
            "SELECT stage,"
            " json_array_length("
            "  CASE WHEN posted_buckets_json IN ('', NULL)"
            "   THEN window_buckets_json ELSE posted_buckets_json END),"
            " COUNT(*) FROM paper_trade_position"
            " WHERE selection_rule_sha256 = ("
            "  SELECT selection_rule_sha256 FROM paper_trade_position"
            "  ORDER BY decision_at_utc DESC LIMIT 1)"
            " GROUP BY 1, 2"
        ).fetchall()
    except sqlite3.DatabaseError:
        return
    finally:
        conn.close()
    # P1 converges: the ladder walks down to two rungs as the day resolves,
    # and further still once the hard bound leaves one live outcome (user
    # ruling, 2026-08-16).  The opening stages keep the three-rung floor.
    # The cut is on the rule stamp, not on decision time.  It used to be a
    # timestamp because a rule change could not reach rows already recorded,
    # so 2026-08-17's book kept five two-bucket P0 seats taken under the old
    # floor and the assertion had to be narrowed around them.  Positions are
    # derived rows: they now carry the rule set that produced them and the
    # sweep re-derives any stamped with another, so every row under the
    # current stamp is answerable to the current rule.
    for stage, size, count in rows:
        low = 1 if str(stage) == "P1" else 3
        assert low <= int(size) <= 4, (
            f"{count} {stage} seats recorded at {size} buckets, "
            f"outside [{low}, 4]"
        )


def test_only_credible_fills_bankroll_the_book() -> None:
    """Phantom wins must not size real seats, and one book must stay one.

    A basket bought under ``DUST_COST_FLOOR`` is a one-sided tail quote, and
    the report has always split the book on that line.  Sizing did not: the
    daily compounding fed on the unsplit P&L, so 2026-08-13's dust -- +3713
    (P0) and +3733 (P1) on baskets costing a tenth of a cent -- lifted equity
    from about a thousand to 4615/4792 and the 2% per-scope cap sized
    2026-08-15's seats at 73 USDC apiece instead of 20.  Every one of those
    was a real fill losing real money at nearly four times its right size.

    The invariant that catches it without re-running the sweep: replay the
    compounding on credible settlements alone and no station-day may exceed
    the per-scope cap against the equity its day opened with.

    Replayed on the chain, not the stage row (user ruling, 2026-08-18).  The
    three stages are one position drawing on one bankroll, so what the cap
    binds is everything the station-day spent -- three stages each taking 2%
    is a 6% position in one outcome.
    """

    import sqlite3
    from collections import defaultdict
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # The three places the model has to bite: what compounds, what a chain is
    # allowed to deploy, and that a chain settles once.
    assert "credible = all_in_cost >= DUST_COST_FLOOR" in source
    assert "chain_credible = chain_cost >= DUST_COST_FLOOR" in source
    assert (
        "* STAGE_BUDGET_SHARE.get(stage, 1.0)\n"
        "                        - chain_stake" in source
    )
    assert "if chain_credible:\n                day_pnl += scored.pnl_usdc" in source

    db = root / "data" / "bayes_basket_target_ssot.db"
    if not db.is_file():
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = conn.execute(
            "SELECT p.event_date, p.station_id, SUM(p.stake_usdc),"
            " MAX(s.pnl_usdc), MAX(COALESCE(s.basket_all_in_cost,"
            "  p.all_in_cost))"
            " FROM paper_trade_position p"
            " LEFT JOIN paper_trade_settlement s"
            "  ON s.station_id=p.station_id AND s.event_date=p.event_date"
            " WHERE p.selection_rule_sha256 = ("
            "  SELECT selection_rule_sha256 FROM paper_trade_position"
            "  ORDER BY decision_at_utc DESC LIMIT 1)"
            " GROUP BY p.event_date, p.station_id"
            " ORDER BY p.event_date"
        ).fetchall()
    except sqlite3.DatabaseError:
        return
    finally:
        conn.close()
    if not rows:
        return

    initial, cap, floor = 1000.0, 0.02, 0.01
    equity = initial
    by_day: dict[str, list] = defaultdict(list)
    for event_date, station_id, stake, pnl, cost in rows:
        by_day[str(event_date)].append((str(station_id), stake, pnl, cost))
    for event_date in sorted(by_day):
        realised = 0.0
        for station_id, stake, pnl, cost in by_day[event_date]:
            assert float(stake) <= equity * cap + 1e-6, (
                f"{station_id} {event_date} staked {float(stake):.2f} across "
                f"its chain against an opening equity of {equity:.2f} -- over "
                f"the {cap:.0%} per-scope cap, which means either the "
                "compounding fed on a dust offer or a stage took an "
                "allowance of its own"
            )
            if pnl is not None and cost is not None and float(cost) >= floor:
                realised += float(pnl)
        equity = max(0.0, equity + realised)


def test_the_opening_cannot_spend_the_whole_chain_cap() -> None:
    """P0 may commit most of the station-day's cap; it may not commit all.

    One running total against one cap let the opening starve every later
    stage: ZUUU 2026-08-19 opened at 10.63 against a 10.6 cap and P1 was
    refused for having no room left, before its own numbers were reached
    (user ruling, 2026-08-19).  The chain total is unchanged -- the shares
    are cumulative and end at 1.0 -- so this redistributes room rather than
    granting more of it.
    """

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from paper_trading_ledger import (  # noqa: PLC0415
        MAX_STAKE_FRACTION_PER_SCOPE,
        STAGE_BUDGET_SHARE,
    )

    assert set(STAGE_BUDGET_SHARE) == {"P0", "P0-M", "P1"}
    assert STAGE_BUDGET_SHARE["P0"] < 1.0, (
        "the opening must leave room for the adjustments"
    )
    shares = [STAGE_BUDGET_SHARE[s] for s in ("P0", "P0-M", "P1")]
    assert shares == sorted(shares), "a later stage may never have less room"
    assert shares[-1] == 1.0, (
        "the last stage reaches the whole cap, so the chain total is "
        "unchanged"
    )
    assert 0 < MAX_STAKE_FRACTION_PER_SCOPE <= 0.05


def test_a_station_day_settles_once() -> None:
    """One position per station-day means one settlement against one truth.

    Read as three books the ledger scored each stage separately: of 152
    settled station-days, 40 were charged two and three times over for a
    single event, and the three bankrolls together staked 2220 USDC out of a
    nominal 1000.  The stage rows remain -- they are the top-ups -- but only
    the chain settles.
    """

    import sqlite3
    from pathlib import Path

    db = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "bayes_basket_target_ssot.db"
    )
    if not db.is_file():
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
    try:
        doubled = conn.execute(
            "SELECT station_id, event_date, COUNT(*) FROM ("
            "  SELECT s.station_id, s.event_date FROM paper_trade_settlement s"
            "  JOIN paper_trade_position p USING(position_id)"
            "  WHERE p.selection_rule_sha256 = ("
            "   SELECT selection_rule_sha256 FROM paper_trade_position"
            "   ORDER BY decision_at_utc DESC LIMIT 1))"
            " GROUP BY station_id, event_date HAVING COUNT(*) > 1"
        ).fetchall()
    except sqlite3.DatabaseError:
        return
    finally:
        conn.close()
    assert not doubled, (
        "a station-day settled more than once: "
        + ", ".join(f"{s} {d} x{n}" for s, d, n in doubled[:5])
    )


def test_an_incremental_window_opens_on_the_book_it_inherited() -> None:
    """A sweep sizes against the book that exists, not the one it started with.

    The equity curve compounds day by day across the days a pass walks, and
    an incremental pass walks only the last few.  Started at the initial
    capital every run, that made every seat a sweep created sized against a
    thousand USDC however the book had actually done -- and the sweep is the
    path that runs every fifteen minutes, so it is the path almost every
    recorded seat came through.  Measured before the fix: 24 positions over
    the 2% per-scope cap on 2026-08-16 through 08-18, sized off 1000 while
    the P1 book stood at 724.  The rebuild that walks everything was right
    all along, which is exactly why this went unseen.

    The dust filter has to travel with it.  A basket bought under a cent is
    a one-sided quote whose win returns a thousand times its stake; letting
    those into the opening equity is the same phantom bankroll by a
    different door.
    """

    import sqlite3
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from paper_trading_ledger import (  # noqa: PLC0415
        INITIAL_CAPITAL_USDC,
        _opening_equity,
    )

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE paper_trade_position ("
        " position_id TEXT PRIMARY KEY, all_in_cost REAL);"
        "CREATE TABLE paper_trade_settlement ("
        " position_id TEXT, stage TEXT, event_date TEXT, pnl_usdc REAL);"
    )
    conn.executemany(
        "INSERT INTO paper_trade_position VALUES (?,?)",
        [("credible", 0.30), ("dust", 0.001), ("inside", 0.30)],
    )
    conn.executemany(
        "INSERT INTO paper_trade_settlement VALUES (?,?,?,?)",
        [
            # Before the window: this is the book the sweep inherits.
            ("credible", "P1", "2026-08-10", -275.0),
            # Before the window but bought for a tenth of a cent -- a win
            # here is not money the next day can deploy.
            ("dust", "P1", "2026-08-11", 900.0),
            # Inside the window, so the day-by-day walk will add it; adding
            # it here as well would count it twice.
            ("inside", "P1", "2026-08-17", 50.0),
        ],
    )

    opened = _opening_equity(conn, earliest="2026-08-16")
    assert opened == INITIAL_CAPITAL_USDC - 275.0, (
        "an incremental window must open on the realised book before it, "
        f"got {opened}"
    )
    # A rebuild has no floor and walks every day itself, so it starts where
    # the book started.
    assert _opening_equity(conn, earliest="") == INITIAL_CAPITAL_USDC


def test_the_walk_reports_the_deepest_refusal_not_the_last_one() -> None:
    """The last cycle is the converged book and says nothing about the day.

    The sweep walks every cycle a stage was offered and used to overwrite
    the refusal each time, so what the page got told was whatever the 23:59
    book happened to be.  By then a resolved market quotes one bucket at
    0.999 and the rest at 0.001, no basket can be assembled from that, and
    the day was reported as "no window could be priced" -- including days
    that had been priced and declined on the objective hours earlier.

    Measured on 2026-08-20 across the 316 P0 station-days carrying
    ``no_priced_window``: median 2 quotable buckets at the last cycle
    against 5 at the first, 55% of last-cycle books already converged
    against 16% of first-cycle ones, and 136 of the 316 did produce a
    priceable window earlier in the day.  Depth is what carries the
    information, so depth is what the walk keeps.
    """

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from paper_trading_ledger import (  # noqa: PLC0415
        REFUSAL_DEPTH,
        _deeper_refusal,
    )

    early = ("no_positive_kelly", "概率 0.707 · 成本 1.060", 3, "T09:00Z")
    late = ("no_priced_window", "中心桶拒绝 2 · 桶数不足拒绝 3", 91, "T23:59Z")

    # The regression itself: a converged end-of-day book must not erase a
    # basket that was assembled and judged that morning.
    assert _deeper_refusal(early, late) == early

    # A genuinely deeper answer still replaces a shallower one, in either
    # arrival order -- otherwise the walk would freeze on "no book".
    start = ("no_book_at_decision", "", 0, "")
    assert _deeper_refusal(start, late) == late
    assert _deeper_refusal(late, early) == early

    # Ties keep the incumbent, which is the earliest book to reach that
    # depth and therefore the widest one that did.
    other = ("no_positive_kelly", "概率 0.500 · 成本 0.900", 40, "T15:00Z")
    assert _deeper_refusal(early, other) == early

    # Both "nothing could be assembled" codes sit on one rung: which of the
    # two is reported is decided by the floors that bit, not by depth.
    assert REFUSAL_DEPTH["leg_below_floor"] == REFUSAL_DEPTH["no_priced_window"]

    # Every reason the schema admits must be rankable, or an unlisted one
    # would silently sort as the shallowest and be unable to hold the slot.
    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    for reason in (
        "no_book_at_decision", "incomplete_book", "selection_failed",
        "no_priced_window", "leg_below_floor", "resting_unfilled",
        "no_positive_kelly", "no_depth", "no_top_up_needed",
        "chain_never_opened",
    ):
        assert reason in REFUSAL_DEPTH, reason

    # The cycle count is the whole walk, not the cycle the kept refusal came
    # from; it used to be carried in the tuple and re-read, which only held
    # while every cycle overwrote it.
    assert "cycles = refusal[2] + 1" not in source
    assert "refusal[1], cycles, refusal[3]," in source


def test_a_basket_dearer_than_its_payout_is_its_own_refusal() -> None:
    """Arithmetic and judgement are different owners.

    A basket asking more than the 1.00 it can ever pay loses at every
    probability, so the objective never gets a say -- but it was reported as
    ``no_positive_kelly``, which says the curve was scored and came out
    negative.  Measured on 2026-08-20 that was 82 of the 253 P0 refusals
    carrying the code, a third of them, at a median cost of 1.040 and a
    maximum of 2.997.  The fix for those is the book, not the model.
    """

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from paper_trading_ledger import (  # noqa: PLC0415
        REFUSAL_DEPTH,
        REFUSAL_REASONS,
        _REFUSAL_DDL,
        _kelly_stake,
    )

    assert "cost_exceeds_payout" in REFUSAL_REASONS
    # The CHECK is generated from the tuple, and ensure_schema rebuilds the
    # table when the live constraint is missing a name -- so the reason is
    # writable the first sweep after this lands.
    assert "'cost_exceeds_payout'" in _REFUSAL_DDL

    # Same rung as the code it splits from: both mean a basket was assembled
    # and priced and no stake followed.
    assert (
        REFUSAL_DEPTH["cost_exceeds_payout"]
        == REFUSAL_DEPTH["no_positive_kelly"]
    )
    # And deeper than "nothing could be assembled", or a converged late book
    # would bury it.
    assert (
        REFUSAL_DEPTH["cost_exceeds_payout"]
        > REFUSAL_DEPTH["no_priced_window"]
    )

    # The split is exactly where the stake rule stops consulting the model.
    for cost in (1.0, 1.04, 2.997):
        assert _kelly_stake(
            win_probability=0.999, cost=cost, equity=1000.0
        ) == (0.0, 0.0), cost
    # Just under it the probability still decides, so those stay where they
    # were: same 0.95 claim, one seatable and one not, on either side of the
    # payout line.
    assert _kelly_stake(
        win_probability=0.95, cost=0.90, equity=1000.0
    )[1] > 0.0
    assert _kelly_stake(
        win_probability=0.95, cost=1.00, equity=1000.0
    ) == (0.0, 0.0)

    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert 'if cost >= 1.0:' in source
    assert '"cost_exceeds_payout",' in source

    # The page must be able to name it, or the split only moves the confusion.
    dashboard = (root / "scripts" / "selection_market_dashboard.py").read_text(
        encoding="utf-8"
    )
    assert '"cost_exceeds_payout": (' in dashboard


def test_every_refusal_reason_has_page_copy_and_p0m_counts_taf_only() -> None:
    """The page must be able to name every code the ledger can write.

    Two reasons had no entry and rendered as raw identifiers, and they were
    not obscure: ``chain_never_opened`` is the largest single row at P1
    (67%) and ``no_top_up_needed`` sits in the P0-M table.  A reader saw an
    English constant where the explanation should be.

    P0-M is also counted only over stations whose TAF states a temperature.
    The stage confirms a TAF, so the 28 stations that never get one were
    sitting in its denominator as days it had no way to serve -- removing
    them moves the seat rate from 8.4% to 12.5% over the same seats.
    """

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from paper_trading_ledger import REFUSAL_REASONS  # noqa: PLC0415
    from scripts.selection_market_dashboard import (  # noqa: PLC0415
        REFUSAL_LABELS,
        STAGE_REQUIRING_TAF,
    )

    missing = [name for name in REFUSAL_REASONS if name not in REFUSAL_LABELS]
    assert not missing, missing
    for name, (label, note) in REFUSAL_LABELS.items():
        assert label.strip(), name
        assert note.strip(), name
        # A label that is just the identifier is the bug this pins.
        assert label != name, name

    assert STAGE_REQUIRING_TAF == "P0-M"

    # no_priced_window is two problems under one code and the copy has been
    # wrong in both directions: first it blamed price alone, then it blamed
    # width alone.  Measured on the recorded cycle of all 180 P0 days, 44%
    # could not reach three contiguous quoted buckets through the centre and
    # 56% assembled a legal basket whose best edge was negative (median
    # -0.247, 98% at or below zero).  The note has to carry both.
    note = REFUSAL_LABELS["no_priced_window"][1]
    assert "太窄" in note and "高过" in note, note


def test_both_copies_of_the_reason_table_cover_every_code() -> None:
    """Section 13 and the roster carry separate copies of the same codes.

    One is long-form, one is a short cell label with a tooltip, so they are
    not duplicates -- but a reason added to the ledger has to reach both or
    the page shows a raw identifier in one of them.  It had already drifted:
    the roster still explained no_priced_window as the market pricing above
    the model, which measurement reversed, and knew nothing about
    cost_exceeds_payout, chain_never_opened, no_top_up_needed or
    resting_unfilled.
    """

    import re
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from paper_trading_ledger import REFUSAL_REASONS  # noqa: PLC0415

    page = (root / "scripts" / "calibration_dashboard.py").read_text(
        encoding="utf-8"
    )
    block = re.search(r"const REASON=\{.*?\};", page, re.S)
    assert block, "the roster reason table moved or was renamed"
    roster = block.group(0)
    missing = [
        name for name in REFUSAL_REASONS if f"{name}:[" not in roster
    ]
    assert not missing, missing

    # Both single-cause readings of no_priced_window are wrong: it is 44%
    # width and 56% price.  The roster's short note has to name both, the
    # way section 13's does.
    short = re.search(r"no_priced_window:\[(.*?)\],\n", page, re.S)
    assert short, "the roster's no_priced_window entry moved"
    assert "太窄" in short.group(1) and "高过" in short.group(1), short.group(1)


def test_a_leg_nobody_offers_is_unbuyable_not_expensive() -> None:
    """The unfillable ceiling is a placeholder, not a price.

    _build_snapshot has to hand the kernel a representable book, and the
    snapshot schema requires ask_available to agree with a non-empty level
    list, so a bucket with no offer arrives carrying max(tick, 1-tick) on a
    single tick-sized level. Nothing fills against it -- which is the point --
    but summing it into a basket cost and reporting the result as "dearer
    than its own payout" describes a market that never quoted.

    That is where 2.997 and 3.996 came from: KLAX 2026-08-18 had nine of
    eleven buckets with ask_levels=[] and ask_available=False in the raw
    quote. Measured 2026-08-20, 13 of the 102 cost_exceeds_payout refusals
    carried such a leg, and every extreme value was one of them.
    """

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))

    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # Judged from the raw book, before the placeholder is substituted.
    assert 'if not (quotes.get(str(bucket)) or {}).get(' in source
    assert '"ask_levels"' in source
    # And it has to be decided before the cost test, or the placeholder wins.
    assert source.index("unoffered = [") < source.index("if cost >= 1.0:")
    assert "条腿无人挂卖" in source

    from scripts.selection_market_dashboard import (  # noqa: PLC0415
        REFUSAL_LABELS,
    )

    # The over-payout note must now claim only real quotes, and say where
    # the other kind went.
    over = REFUSAL_LABELS["cost_exceeds_payout"][1]
    assert "实价" in over and "没有一篮可买" in over
    # And the receiving row must admit the third case.
    assert "没人挂卖" in REFUSAL_LABELS["no_priced_window"][1]
