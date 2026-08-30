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
    reached the book.  That trim is exercised directly rather than grepped
    for, because the property that matters is which bucket it keeps: trimmed
    toward the wrong anchor it drops the centre, and a core without its
    centre enumerates windows the gate would never see.
    """

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))

    from paper_trading_ledger import (  # noqa: PLC0415
        MAXIMUM_SUPPORT_BUCKETS,
        _entry_support_core,
    )

    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert "MINIMUM_SUPPORT_BUCKETS = 3" in source
    assert "minimum_support_buckets=MINIMUM_SUPPORT_BUCKETS" in source
    # The evidence support is the mass-selected run, not a bare centre.
    assert "desired=set(core) or {center}" in source
    assert "desired={center}," not in source
    # And it is trimmed to the cap before it can act as an incumbent.
    assert "core = _entry_support_core(" in source
    # The floor is per stage: charter 50a gives P1 two rungs while the
    # opening stages keep three.  Held to the opening floor, the ledger
    # reported P1 refusing seats production takes.
    assert "probability_stage=stage," in source

    # An over-wide core is capped, and the centre survives the cap even when
    # it sits at ordinal 0 -- the lowest rung of every ladder, and the case
    # that reads as false.  KSEA 2026-08-13 P1 is centred there.
    # Mass rising away from the centre, so the core grows to five and the
    # argmax lands well above it -- trimmed toward the argmax the centre is
    # the first bucket dropped.
    buckets = [
        "73°F or below", "74-75°F", "76-77°F", "78-79°F", "80-81°F",
        "82-83°F", "84-85°F",
    ]
    ordinals = {bucket: i for i, bucket in enumerate(buckets)}
    probabilities = {
        "73°F or below": 0.04, "74-75°F": 0.06, "76-77°F": 0.10,
        "78-79°F": 0.14, "80-81°F": 0.20, "82-83°F": 0.26, "84-85°F": 0.20,
    }
    from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: PLC0415
        probability_core,
    )

    assert len(
        probability_core(
            probabilities, ordinals=ordinals, center_bucket="73°F or below"
        )
    ) > MAXIMUM_SUPPORT_BUCKETS, "fixture no longer exercises the trim"
    core = _entry_support_core(
        probabilities=probabilities,
        ordinals=ordinals,
        center="73°F or below",
    )
    assert len(core) <= MAXIMUM_SUPPORT_BUCKETS, (
        f"core {sorted(core)} wider than the cap"
    )
    assert "73°F or below" in core, (
        f"core {sorted(core)} trimmed its own centre away"
    )


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
        "                        * tilt\n"
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

    # no_priced_window used to be two problems under one code and the copy
    # had been wrong in both directions: first it blamed price alone, then it
    # blamed width alone.  Measured on the recorded cycle of all 180 P0 days,
    # 44% could not reach three contiguous quoted buckets through the centre
    # and 56% assembled a legal basket whose best edge was negative (median
    # -0.247, 98% at or below zero).  The code was split on 2026-08-20, so
    # what the one note had to carry both of is now one measured share each
    # -- and both still have to be on the page, or the split has merely
    # hidden the half it stopped naming.
    assert "四成" in REFUSAL_LABELS["no_legal_window"][1]
    priced_out = REFUSAL_LABELS["negative_edge_window"][1]
    assert "六成" in priced_out and "−0.25" in priced_out, priced_out


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

    # Both single-cause readings of the old pooled code were wrong: it was
    # 44% width and 56% price.  Each share now sits on the code that owns it,
    # and the roster's short notes have to carry them the way section 13's
    # do -- a split that drops the number is a split that hid a half.
    for name, share in (("no_legal_window", "四成"),
                        ("negative_edge_window", "六成")):
        short = re.search(r"%s:\[(.*?)\],\n" % name, page, re.S)
        assert short, "the roster's %s entry moved" % name
        assert share in short.group(1), short.group(1)


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
    assert "实价" in over and "窗口定不出价" in over
    # And the receiving row must admit the third case.
    assert "没人挂卖" in REFUSAL_LABELS["no_priced_window"][1]


def test_seated_and_wholly_unfilled_are_disjoint_tallies() -> None:
    """They come from different tables and must not read as nested.

    ``seated_count`` counts paper_trade_position; ``nothing_filled_count``
    counts paper_trade_refusal rows carrying resting_unfilled -- a basket
    where no rung filled anywhere never becomes a position at all.  Printed
    one under the other in the same card, P0-M read "建仓 19" above "全篮落空
    20", which looks like 20 of 19.  It is 39 station-days: 19 seated, 20
    wholly unfilled.

    The fill-rate denominator is the seated side alone -- a day with nothing
    filled has no fill price to average -- so the two tallies cannot be
    folded together there either.
    """

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    import re

    board = (root / "scripts" / "selection_market_dashboard.py").read_text(
        encoding="utf-8"
    )
    # Different sources, and that is the whole point.  Read the SQL blocks
    # themselves rather than guessing at character distances.
    assert (
        "SELECT COUNT(*), stage FROM paper_trade_position GROUP BY stage"
        in board
    ), "seated_count no longer counts positions"
    assert "SELECT COUNT(*), stage FROM paper_trade_refusal" in board
    # Both posted-and-bought-nothing codes feed this tally.
    assert (
        "WHERE reason IN ('resting_unfilled','belief_withdrew_order')" in board
    )
    queries = re.findall(r"conn\.execute\(\s*(.*?)\)\s*(?:\.fetchall\(\))?",
                         board, re.S)
    ladder_q = [q for q in queries if "intended_legs_json" in q]
    # The share account is built off positions only: a wholly unfilled day
    # has no fill price to average, so it cannot enter the denominator.
    assert ladder_q and "paper_trade_position" in ladder_q[0]
    assert "paper_trade_refusal" not in ladder_q[0]

    page = (root / "scripts" / "calibration_dashboard.py").read_text(
        encoding="utf-8"
    )
    # The card states the total the two make up, and marks the nesting.
    assert "r.seated_count+r.nothing_filled_count" in page
    assert "├ 建仓篮子" in page
    assert "└ 全篮落空" in page
    # And the note names the case that misread.
    assert "并列的两堆，不是包含关系" in page


def test_withdrawing_and_never_being_reached_are_told_apart() -> None:
    """Both end with nothing bought and call for opposite actions.

    The belief gate re-reads our own probabilities every re-pricing round
    and cancels the whole order once its claimed expectation goes negative
    -- the market never got a say.  Reported as resting_unfilled that reads
    as the book failing to reach us, sending a reader to check execution
    when the cause was evidence.  The gate is measured to cost 6-20 points
    of fill for +2.3 points of ROI, so it is not a rare corner.
    """

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from paper_trading_ledger import (  # noqa: PLC0415
        REFUSAL_DEPTH,
        REFUSAL_REASONS,
        _REFUSAL_DDL,
    )

    assert "belief_withdrew_order" in REFUSAL_REASONS
    assert "'belief_withdrew_order'" in _REFUSAL_DDL
    # Same rung: a posted order that bought nothing, either way.
    assert (
        REFUSAL_DEPTH["belief_withdrew_order"]
        == REFUSAL_DEPTH["resting_unfilled"]
    )

    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # The flag has to come out of the walk, or the caller cannot tell.
    assert "return filled, missing, withdrawn" in source
    assert "got, missing, withdrawn = _resting_ladder_fill(" in source
    # And be tested before the market-never-came branch.  Anchor inside the
    # nothing-bought block: both codes also appear in REFUSAL_REASONS far
    # above, which is not the ordering under test.
    block = source[source.index("if not any(got.values()):"):]
    assert block.index("if withdrawn:") < block.index('"resting_unfilled",')

    from scripts.selection_market_dashboard import (  # noqa: PLC0415
        REFUSAL_LABELS,
    )

    assert "belief_withdrew_order" in REFUSAL_LABELS
    assert "自己撤" in REFUSAL_LABELS["belief_withdrew_order"][0]


def test_the_order_deadline_names_the_bound_that_bound_it() -> None:
    """"挂到结算" was wrong whenever a peak cutoff existed -- which is always.

    The copy keyed off MAKER_RESTING_HORIZON_MINUTES alone, one of three
    bounds and never the binding one while a station has a peak cutoff.
    Stations stop a little after their own peak: median 19:20 local,
    earliest 16:53.  So orders the page described as resting until
    settlement had in fact stopped mid-afternoon.
    """

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from paper_trading_ledger import (  # noqa: PLC0415
        _resting_deadline,
        _resting_deadline_reason,
    )

    common = {
        "placed_at_utc": "2026-08-19T22:00:00Z",
        "settlement_timezone": "Asia/Shanghai",
        "event_date": "2026-08-20",
    }
    # A station with a peak cutoff stops there, and says so with the hour.
    assert _resting_deadline_reason(
        cutoff_local_minutes=19 * 60, **common
    ) == "挂到达峰截止 19:00 本地"
    # The cutoff really is the earlier bound, not just the label.
    with_cut = _resting_deadline(cutoff_local_minutes=19 * 60, **common)
    without = _resting_deadline(cutoff_local_minutes=None, **common)
    assert with_cut < without, (with_cut, without)
    # No cutoff known: the day itself, and it must not claim settlement.
    assert _resting_deadline_reason(
        cutoff_local_minutes=None, **common
    ) == "挂到当日结束"

    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # The refusal text asks which bound bound it rather than hard-coding
    # one.  ("挂到结算" survives as the rule label's fallback for a build
    # with the cutoff disabled entirely, which is not this build.)
    block = source[source.index("if not any(got.values()):"):]
    # Two branches end in `continue`; the market-never-came one is second.
    first = block.index("continue") + len("continue")
    detail = block[:block.index("continue", first)]
    assert "_resting_deadline_reason(" in detail
    assert "挂到结算" not in detail


def test_the_budget_line_only_appears_when_the_budget_bound() -> None:
    """It claimed the allowance ran out on every chain that held anything.

    stake = min(kelly, max(0, stage_room)), and the refusal text appended
    "段额度已用尽" whenever ``held`` was non-empty -- a condition about the
    chain having a position, not about the allowance being spent.  So a
    stage refused because its Kelly was negative, or because the basket cost
    more than its own payout, was told the budget stopped it, which points
    at sizing when the cause was price or belief.

    Measured 2026-08-20 across all 78 refusals carrying the line: 63 were
    EV<=0, 15 were cost>=1.00, and none had run out of room -- the smallest
    remaining allowance was 5.45 against a chain that had spent 1.08.
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

    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # The Kelly stake is kept under its own name so the two causes stay
    # distinguishable after the clamp.
    assert "fraction, wanted_stake = _kelly_stake(" in source
    assert "stake = min(wanted_stake, max(0.0, stage_room))" in source
    # And the line is gated on the budget actually binding.
    assert "if wanted_stake > 0.0 and stage_room <= 0.0" in source
    assert "if not held\n" not in source

    # The staging still exists and still totals the per-scope cap at P1 --
    # this test is about the copy, not a licence to widen the cap.
    assert STAGE_BUDGET_SHARE["P1"] == 1.0
    assert STAGE_BUDGET_SHARE["P0"] < STAGE_BUDGET_SHARE["P0-M"] < 1.0
    assert MAX_STAKE_FRACTION_PER_SCOPE == 0.02


def test_posted_basket_hit_is_grouped_on_what_was_chosen() -> None:
    """window_size is the width that survived the market, not the choice.

    A three-rung basket the book cut to one is stored with window_size 1, so
    grouping hit rate on it files that day under one rung and empties the
    three-rung row of exactly its hardest cases.  The question "is a
    three-rung basket any good" is about the width we posted.

    Both readings are carried: measured 2026-08-20 over 137 settlements the
    posted basket covered the truth 37.2% of the time and the filled basket
    17.5%.  That gap is the market declining to sell the legs that went on
    to win, and it is the largest single term in the page's low hit rate --
    so showing only one of the two would hide it.
    """

    import sqlite3
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts.selection_market_dashboard import (  # noqa: PLC0415
        _posted_basket_hit_trend,
    )

    db = root / "data" / "bayes_basket_target_ssot.db"
    if not db.is_file():
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
    try:
        payload = _posted_basket_hit_trend(conn)
    except sqlite3.DatabaseError:
        return
    finally:
        conn.close()

    for row in payload["sizes"]:
        assert row["posted_size"] in (3, 4)
        # Every posted basket is at least as likely to cover as the part of
        # it we managed to buy -- the filled set is a subset of the posted.
        assert row["filled_hit_rate"] <= row["hit_rate"] + 1e-9, row
        for key in ("hit_rate", "filled_hit_rate", "claimed_mean"):
            assert 0.0 <= row[key] <= 1.0, (key, row)
        if row["delta"] is None:
            continue
        # A direction is only readable when it clears two combined standard
        # errors; the flag and the numbers must not disagree.
        assert row["delta_stderr"] is not None
        assert row["delta_is_readable"] == (
            abs(row["delta"]) >= 2.0 * row["delta_stderr"]
        ), row
        # The split is on settlements, not the middle date: neither half may
        # be starved.
        assert row["earlier"]["settled_count"] >= 8
        assert row["later"]["settled_count"] >= 8

    page = (root / "scripts" / "calibration_dashboard.py").read_text(
        encoding="utf-8"
    )
    # The strip must not print a direction it cannot support.
    assert "delta_is_readable" in page
    assert "与噪音不可分" in page


def test_every_seat_follows_the_d_rule_at_its_own_entry_cycle() -> None:
    """The execution layer must seat what the D rule chooses, every time.

    Replays each recorded position's entry cycle through the ledger's own
    reader and the production window enumeration, then holds every seat to
    the rule it claims in its own fingerprint:

      - the posted window is priced_windows[0] of that cycle (D-质量),
      - it contains the ladder centre (中心底仓, charter 49),
      - width within the stage's charter bounds (50/50a),
      - every quoted posted leg >= 0.02 (腿>=0.02),
      - EV>0 at entry for fully-filled rows (整篮EV) -- partially filled
        rows carry the post-fill pair, which can legitimately be negative:
        the gate ran on the posted basket, the market then sold only the
        legs that priced worse than they claimed.

    The support trim comes from the ledger itself
    (``_entry_support_core``).  Re-implemented here it read a correct row as
    a stale one: the copy fell back to the argmax bucket whenever the
    centre's ordinal was falsy, and ordinal 0 is the lowest bucket on every
    ladder.  KSEA 2026-08-13 P1 is centred on ``73°F or below`` at ordinal
    0, so the copy trimmed the core toward 80-81°F, dropped the centre, and
    enumerated a 74-81°F window costing 0.95 against a 0.46 claim -- which
    outranks the recorded seat on probability mass.  The row was never
    stale; it replays exactly on the ledger's own trim, and the allowlist
    that carried it is gone.  A failure here is a live divergence between
    the sweep and the rule, so it takes a fix, not a name on a list.
    """

    import json
    import sqlite3
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))

    db = root / "data" / "bayes_basket_target_ssot.db"
    if not db.is_file():
        return

    from bayes_basket_target.delta_v2_runtime import (  # noqa: PLC0415
        MINIMUM_LEG_ALL_IN_ASK,
        _support_windows,
    )
    from paper_trading_ledger import (  # noqa: PLC0415
        FEE_RATE,
        MAXIMUM_SUPPORT_BUCKETS,
        MINIMUM_SUPPORT_BUCKETS,
        SELECTION_RULE_SHA256,
        _build_snapshot,
        _entry_support_core,
        _stage_entry_candidates,
    )

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
    try:
        try:
            positions = conn.execute(
                "SELECT station_id, event_date, stage, decision_at_utc,"
                " posted_buckets_json, window_buckets_json,"
                " ladder_center_bucket, win_probability, all_in_cost,"
                " prices_json FROM paper_trade_position"
                " WHERE selection_rule_sha256=?",
                (SELECTION_RULE_SHA256,),
            ).fetchall()
        except sqlite3.DatabaseError:
            # A stub database (a worktree checkout) has the file and none
            # of the tables; the sibling tests skip the same way.
            return

        for (st, day, stage, at, posted_js, filled_js, centre, win, cost,
             prices_js) in positions:
            posted = json.loads(posted_js) if posted_js else json.loads(
                filled_js
            )
            filled = json.loads(filled_js)
            prices = json.loads(prices_js) if prices_js else {}
            scope = f"{st} {day} {stage}"

            assert str(centre) in {str(b) for b in posted}, (
                f"{scope}: centre {centre} not in posted window {posted}"
            )
            low = 2 if str(stage) == "P1" else 3
            assert low <= len(posted) <= 4, (
                f"{scope}: posted width {len(posted)} outside [{low}, 4]"
            )
            for bucket in posted:
                quote = prices.get(str(bucket))
                if quote is not None:
                    assert float(quote) >= MINIMUM_LEG_ALL_IN_ASK - 1e-9, (
                        f"{scope}: leg {bucket} quoted {quote} under floor"
                    )
            if len(filled) == len(posted):
                assert float(win) > float(cost), (
                    f"{scope}: fully-filled seat with EV<=0 "
                    f"({win} vs {cost}) -- the entry gate cannot have run"
                )

            replayed = None
            for payload in _stage_entry_candidates(
                conn, str(st), str(day), str(stage)
            ):
                if payload["decision_at_utc"] != str(at):
                    continue
                built = _build_snapshot(
                    station_id=str(st), event_date=str(day),
                    observed_at=payload["observed_at"],
                    buckets=payload["buckets"], quotes=payload["quotes"],
                )
                if built is None:
                    break
                snapshot, probabilities, _p, _d = built
                center = payload["center_bucket"] or max(
                    probabilities, key=lambda b: probabilities[b]
                )
                if center not in probabilities:
                    center = max(
                        probabilities, key=lambda b: probabilities[b]
                    )
                ordinals = {
                    r.bucket: int(r.ordinal) for r in snapshot.buckets
                }
                core = _entry_support_core(
                    probabilities=probabilities,
                    ordinals=ordinals,
                    center=center,
                )
                windows, _audit = _support_windows(
                    snapshot=snapshot, desired=set(core) or {center},
                    probabilities=probabilities, fee_rate=FEE_RATE,
                    maximum_support_buckets=MAXIMUM_SUPPORT_BUCKETS,
                    minimum_support_buckets=MINIMUM_SUPPORT_BUCKETS,
                    center_bucket=center, probability_stage=str(stage),
                )
                pw = [
                    r for r in windows
                    if r.get("all_in_price") is not None
                    and r.get("edge") is not None
                ]
                if pw:
                    replayed = {str(b) for b in pw[0]["buckets"]}
                break
            if replayed is not None:
                assert replayed == {str(b) for b in posted}, (
                    f"{scope}: posted {sorted(posted)} but the D rule "
                    f"chooses {sorted(replayed)} at that cycle"
                )
    finally:
        conn.close()


def test_consolidation_is_an_annotation_and_measures_what_it_says() -> None:
    """Three signs the market already priced the day, recorded, never used.

    Measured on 159 settled chains: the 45 entered with none of the three
    returned +77% (+42% with the best win removed) while every stratum
    with at least one returned -35% to -44%.  An inversion that strong,
    found in-sample, earns a forward validation lane -- not a gate.  So
    the count is written on the row and consulted by nothing.
    """

    import sqlite3
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from paper_trading_ledger import (  # noqa: PLC0415
        CONSOLIDATION_SHARP_TOP2,
        CONSOLIDATION_STABLE_LOOKBACK,
        _consolidation_signals,
    )

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        # ``state`` mirrors the real table, which pins it to COMMITTED by
        # CHECK.  The ledger's reads carry the predicate so SQLite may use
        # the covering index -- a stub without the column made those reads
        # fail here while working against the database they are written for.
        "CREATE TABLE basket_probability_bridge_history ("
        " history_id TEXT, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT,"
        " state TEXT NOT NULL DEFAULT 'COMMITTED');"
        "CREATE TABLE basket_probability_bridge_bucket_probability ("
        " history_id TEXT, bucket TEXT, ordinal INT, probability REAL);"
    )
    for i, centre_p in enumerate((0.5, 0.5, 0.5)):
        hid = f"h{i}"
        conn.execute(
            "INSERT INTO basket_probability_bridge_history"
            " (history_id, station_id, event_date, stage, decision_at_utc)"
            " VALUES (?,?,?,?,?)",
            (hid, "ZUUU", "2026-08-20", "P0", f"2026-08-19T0{i}:00:00Z"),
        )
        conn.executemany(
            "INSERT INTO basket_probability_bridge_bucket_probability"
            " VALUES (?,?,?,?)",
            [(hid, "32C", 0, centre_p), (hid, "33C", 1, 1 - centre_p)],
        )

    probabilities = {"31C": 0.15, "32C": 0.5, "33C": 0.35}
    ordinals = {"31C": 0, "32C": 1, "33C": 2}
    quotes = {
        "31C": {"best_ask": 0.10},
        "32C": {"best_ask": 0.45},
        "33C": {"ask_levels": [{"price": 0.30, "shares": 5}]},
    }
    got = _consolidation_signals(
        conn,
        station_id="ZUUU",
        event_date="2026-08-20",
        entry_decision_at_utc="2026-08-19T09:00:00Z",
        probabilities=probabilities,
        ordinals=ordinals,
        quotes=quotes,
    )
    # Centre 32C held over the three prior decisions and at entry: stable.
    # top2 = 0.85 >= 0.60: sharp.  All three ideal buckets quoted >= 0.02.
    assert got == {
        "stable": True, "sharp": True, "quoted": True, "count": 3
    }
    assert sum(
        (got["stable"], got["sharp"], got["quoted"])
    ) == got["count"]

    # Knock each signal out independently.
    conn.execute(
        "UPDATE basket_probability_bridge_bucket_probability"
        " SET probability=CASE bucket WHEN '32C' THEN 0.2 ELSE 0.8 END"
        " WHERE history_id='h1'"
    )
    assert _consolidation_signals(
        conn, station_id="ZUUU", event_date="2026-08-20",
        entry_decision_at_utc="2026-08-19T09:00:00Z",
        probabilities=probabilities, ordinals=ordinals, quotes=quotes,
    )["stable"] is False
    blunt = {"31C": 0.3, "32C": 0.25, "33C": 0.25, "34C": 0.2}
    assert _consolidation_signals(
        conn, station_id="ZUUU", event_date="2026-08-20",
        entry_decision_at_utc="2026-08-19T09:00:00Z",
        probabilities=blunt, ordinals={**ordinals, "34C": 3}, quotes=quotes,
    )["sharp"] is False
    assert _consolidation_signals(
        conn, station_id="ZUUU", event_date="2026-08-20",
        entry_decision_at_utc="2026-08-19T09:00:00Z",
        probabilities=probabilities, ordinals=ordinals,
        quotes={**quotes, "32C": {"best_ask": 0.001}},
    )["quoted"] is False

    assert CONSOLIDATION_STABLE_LOOKBACK == 3
    assert CONSOLIDATION_SHARP_TOP2 == 0.60

    # Promoted from annotation to stake scale (user ruling, 2026-08-21)
    # after the split survived every robustness cut -- but scale is the
    # whole licence.  The *count* may shrink a chain's allowance; it must
    # never pick which days trade, because a gate on the same split
    # changes the population the split was measured on.
    #
    # This used to be asserted by counting call sites, which stopped saying
    # anything once the gate and the scale began sharing one call
    # (2026-08-24).  The constraint is about which consumer reads what, so
    # assert that instead: the tilt is still taken under ``stage == "P0"``
    # and still keyed off ``count``, and nothing else consults ``count``.
    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert 'if stage == "P0":\n                        seat_consolidation' in source
    # Since 2026-08-28 (commit 10395c56) the tilt reads the named lights,
    # not the three-light count; ``count`` is written on the row and read
    # by nothing.  Zero consumers, not one, is now the annotation contract.
    assert source.count('["count"]') == 0, "count grew a consumer"
    assert 'if seat_consolidation.get(name) is True' in source
    assert "CONSOLIDATION_TILT_MIN_SIGNALS" in source
    assert "consolidation_json" in source

    # The certainty gate is a separate rule reading the same signs, and it
    # is deliberately NOT stage-scoped: production's gate carries no stage
    # condition, so a ledger that gated only the opener scored a narrower
    # strategy than the one that runs.  Aligned 2026-08-24.
    gate_at = source.index("if CERTAINTY_GATE and any(")
    opener_at = source.index('if stage == "P0":\n                        seat_consolidation')
    assert gate_at < opener_at, "the gate must be judged before the P0-only tilt"
    assert "certainty_gate_scope" in source


def test_the_selection_discount_shades_the_gate_and_only_the_gate() -> None:
    """E[realised | claimed, traded] belongs to the decision, not the record.

    The promise is honest unconditionally (1.03-1.22 at every decision age)
    and realises 0.60 of itself conditional on trading, so the gate judges
    the shaded claim while the row keeps the raw one -- shading the record
    would double-count the correction the next time the factor is measured.

    Flat deliberately: per consolidation stratum the factor runs
    0.38/0.64/0.56/0.70, non-monotone with five chains in the stratum that
    motivated stratifying, so a table has nothing to stand on yet.
    """

    import importlib
    import os
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import paper_trading_ledger as ledger  # noqa: PLC0415

    # Off by default: the consolidation forward lane owns the promotion bar.
    assert ledger.SELECTION_DISCOUNT == 0.0
    assert "选择折扣" not in ledger.SELECTION_RULE_LABEL

    os.environ["PAPER_LEDGER_SELECTION_DISCOUNT"] = "0.6"
    try:
        importlib.reload(ledger)
        assert ledger.SELECTION_DISCOUNT == 0.6
        assert "选择折扣λs0.6" in ledger.SELECTION_RULE_LABEL
    finally:
        del os.environ["PAPER_LEDGER_SELECTION_DISCOUNT"]
        importlib.reload(ledger)

    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # The shaded number feeds the Kelly stake and the refusal detail...
    assert "win_probability=gate_win," in source
    # ...and nowhere else: the recorded row keeps the raw claim.
    assert source.count("gate_win") == 3  # define, kelly, refusal detail


def test_the_consolidation_tilt_scales_stakes_and_never_selects() -> None:
    """Less money where the certainty premium eats the edge; same trades.

    The rule as re-ruled on 2026-08-28 (commits 4c8224be, 66ce4ac2): the
    tilt counts the two lights that discriminate -- ``stable`` and
    ``sharp`` -- fires at one of them, and cuts the allowance to a tenth.
    ``quoted`` left the tilt: it never separated winners from losers.

    Re-measured on the 2026-08-29 charter-66 book (301 settled chains),
    strata by lit count at the opener are monotone: 0 lights +5.6% (65%
    hit, 110 chains), 1 light -14.8% (167), 2 lights -47.8% (24).  The
    earlier ruling's boundary (fire at >= 2, cut to a quarter) pooled the
    +5.6% and -14.8% strata into one "healthy" band -- the split it was
    measured on was coarser than the phenomenon.  The permutation test on
    the 0-versus-rest split is p=0.14 on this sample, so the boundary is
    held by the ruling and its commit-time measurement, not by this book
    alone -- and this book's stakes are themselves shaped by the tilt, so
    its strata ROI are partly endogenous.  What this test pins is the
    RULE: reduce-only, opener-frozen, never selecting.
    """

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from paper_trading_ledger import (  # noqa: PLC0415
        CONSOLIDATION_TILT_FACTOR,
        CONSOLIDATION_TILT_MIN_SIGNALS,
        CONSOLIDATION_TILT_SIGNALS,
        SELECTION_RULE_LABEL,
    )

    # On by default, reduce-only, at the re-ruled boundary: one of the two
    # discriminating lights fires it, and it cuts to a twentieth (0.10 ->
    # 0.05, 2026-08-30 re-measure: monotone in depth down to the
    # minimum-clip plateau, +14.58 over 0.10 with drop-1/2/3, sign and
    # both temporal halves agreeing).
    assert CONSOLIDATION_TILT_MIN_SIGNALS == 1
    assert 0.0 < CONSOLIDATION_TILT_FACTOR < 1.0
    assert CONSOLIDATION_TILT_FACTOR == 0.05
    assert CONSOLIDATION_TILT_SIGNALS == ("stable", "sharp")
    # It reshapes the book, so it must reshape the fingerprint.
    assert "固化≥1减仓0.05" in SELECTION_RULE_LABEL
    assert "灯stable+sharp" in SELECTION_RULE_LABEL
    # The tilted stake floors at one minimum clip per leg, or the cut
    # becomes a gate the venue enforces (commit e4dac184).
    assert "钳最低单" in SELECTION_RULE_LABEL

    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # The factor can only ever shrink an allowance: clamped at parse...
    assert "CONSOLIDATION_TILT_FACTOR = min(1.0, float(" in source
    # ...applied to the stage allowance...
    assert "stage_room = stage_budget * tilt - chain_stake" in source
    # ...frozen for the chain by its opener, inherited by later stages.
    assert "chain_tilt = tilt" in source
    assert "tilt = chain_tilt" in source
    # The tilt reads the NAMED lights, and the recorded ``count`` has no
    # consumer at all any more -- it is annotation, full stop.  A second
    # reader of either is how the annotation became a selector last time.
    assert source.count('["count"]') == 0
    assert 'if seat_consolidation.get(name) is True' in source


def test_new_artifacts_name_the_seat_rule_that_actually_ran() -> None:
    """The method string is the artifact's only human-readable rule claim.

    It carried "train_rmse_inverse_family_capped_v1" long after e1c05256
    promoted the minimum-variance seat (282/282 live scopes in the
    seat-weight journal), and the stale literal kept being read as the
    serving rule -- including by this session.  New artifacts write the
    rule that actually divided the weight; the repair lane keeps the old
    literal on purpose, because it reproduces pre-bridge identities whose
    content sha covers that exact string.
    """

    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    builder = (root / "bayes_basket_target" / "native_ensemble_oos.py"
               ).read_text(encoding="utf-8")
    # The live builder composes the prefix from the recorded rule...
    assert '"train_rmse_inverse_family_capped_v1::"\n' not in builder
    assert '.get(\n                            "weight_rule"\n' in builder
    assert '"seat_weight_rule_unrecorded"' in builder
    # ...and nothing serves off the prefix: transforms key on contained
    # tags, so renaming cannot change any served math.
    assert "in artifact_combination_method" in (
        root / "bayes_basket_target" / "native_ensemble_p0.py"
    ).read_text(encoding="utf-8")
    # The repair lane must keep the historical literal byte-for-byte.
    repair = (root / "scripts" / "plan_native_ensemble_activation_repair.py"
              ).read_text(encoding="utf-8")
    assert '"train_rmse_inverse_family_capped_v1::"' in repair


def test_widening_the_reason_check_keeps_the_closed_history() -> None:
    """A vocabulary change must not erase the rows written under the old one.

    The old rebuild dropped the table and trusted the sweep to repopulate
    it, but the scheduled sweep walks only the recent window -- so every
    reason added since 2026-08-16 silently erased whatever closed history
    lay beyond four days, and the table never accumulated more than a few.
    The rebuild now migrates: surviving reasons cross intact, a reason
    that left the vocabulary is the only thing dropped (it cannot pass the
    new CHECK), and the caller is told so it can re-derive everything.
    """

    import re
    import sqlite3
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import paper_trading_ledger as ledger  # noqa: PLC0415

    conn = sqlite3.connect(":memory:")
    # Yesterday's vocabulary: everything current, minus one reason this
    # code can emit, plus one it no longer knows.
    narrowed = tuple(
        r for r in ledger.REFUSAL_REASONS if r != "chain_never_opened"
    ) + ("retired_reason",)
    old_ddl = re.sub(
        r"CHECK\(reason IN \([^)]*\)\)",
        "CHECK(reason IN (%s))"
        % ",".join("'%s'" % r for r in narrowed),
        ledger._REFUSAL_DDL,
    )
    assert "'retired_reason'" in old_ddl
    conn.execute(old_ddl)
    rows = [
        ("KMIA", "2026-08-01", "P0", "no_positive_kelly", "old and closed",
         3, "2026-08-01T12:00:00Z", "2026-08-01T23:00:00Z"),
        ("KLGA", "2026-08-02", "P1", "retired_reason", "vocabulary left it",
         2, "2026-08-02T12:00:00Z", "2026-08-02T23:00:00Z"),
    ]
    conn.executemany(
        "INSERT INTO paper_trade_refusal VALUES (?,?,?,?,?,?,?,?)", rows
    )
    conn.commit()

    assert ledger.ensure_schema(conn) is True
    kept = conn.execute(
        "SELECT station_id, reason FROM paper_trade_refusal ORDER BY 1"
    ).fetchall()
    # The closed-history row crossed; the retired-code row could not.
    assert kept == [("KMIA", "no_positive_kelly")]
    # No half-migrated leftovers.
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master"
        " WHERE name='paper_trade_refusal_old'"
    ).fetchone()[0] == 0
    # The new CHECK accepts the full current vocabulary.
    live = str(conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='paper_trade_refusal'"
    ).fetchone()[0])
    for reason in ledger.REFUSAL_REASONS:
        assert "'%s'" % reason in live
    # A second call is quiet: nothing to widen, nothing rebuilt.
    assert ledger.ensure_schema(conn) is False

    # And the backfill turns that signal into a full walk, or the migrated
    # rows would keep yesterday's vocabulary under today's labels.
    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert "refusal_rebuilt = ensure_schema(conn)" in source
    assert source.index("if refusal_rebuilt:") < source.index(
        "superseded = _drop_superseded_positions"
    )


def _ledger_path() -> None:
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))


def test_every_enumeration_gate_has_a_refusal_to_be_reported_as() -> None:
    """A gate the ledger cannot name is a refusal it will misattribute.

    Twice now the page has been handed counters that could not add up to the
    refusal they were offered as the explanation of: the leg floor on
    2026-08-17, and the edge/friction gate until 2026-08-20.  Both times the
    missing gate's refusals were silently absorbed by whichever gate was
    listed.  The mapping is keyed by the funnel the runtime publishes so a
    new gate fails here rather than there.
    """

    from bayes_basket_target.delta_v2_runtime import (  # noqa: PLC0415
        WINDOW_REFUSAL_FUNNEL,
    )

    _ledger_path()
    from paper_trading_ledger import (  # noqa: PLC0415
        GATE_LABEL,
        GATE_REFUSAL_REASON,
        REFUSAL_DEPTH,
        REFUSAL_REASONS,
    )

    assert set(GATE_REFUSAL_REASON) == set(WINDOW_REFUSAL_FUNNEL)
    assert set(GATE_LABEL) == set(WINDOW_REFUSAL_FUNNEL)
    for reason in GATE_REFUSAL_REASON.values():
        assert reason in REFUSAL_REASONS
        assert reason in REFUSAL_DEPTH


def test_the_refusal_names_the_gate_that_took_the_last_window() -> None:
    """Not the gate with the biggest count, and not price by default.

    The (2,3,1) shape below was the second commonest P0 refusal on the
    2026-08-20 book, 44 station-days of it.  The old test asked whether the
    leg floor outnumbered the two structural gates -- 1 > 2 + 3 is false --
    so the day was filed as "the market priced us out" when the market had
    quoted every rung and our own floor is what refused the last window.
    """

    _ledger_path()
    from paper_trading_ledger import (  # noqa: PLC0415
        GATE_LABEL,
        _refusal_reason_for_gate,
        _window_refusal_detail,
    )

    audit = {
        "enumerated_window_count": 6,
        "gapped_window_refused_count": 0,
        "center_refused_window_count": 2,
        "short_refused_window_count": 3,
        "leg_floor_refused_window_count": 1,
        "unpriced_refused_window_count": 0,
        "edge_refused_window_count": 0,
        "shortlisted_window_count": 0,
        "last_refusing_gate": "leg_floor",
    }
    assert _refusal_reason_for_gate(audit) == "leg_below_floor"
    detail = _window_refusal_detail(audit)
    # The line is an equation, and it balances.
    assert "枚举 6 =" in detail
    assert "计数不闭合" not in detail
    # Every gate appears, including the ones that did not fire, so a reader
    # can check the arithmetic rather than take the verdict on trust.
    for label in GATE_LABEL.values():
        assert label in detail

    # And the genuinely price-driven refusal keeps its own name.
    priced_out = dict(audit, **{
        "leg_floor_refused_window_count": 0,
        "edge_refused_window_count": 1,
        "last_refusing_gate": "edge",
    })
    assert _refusal_reason_for_gate(priced_out) == "negative_edge_window"
    # A structural sweep is neither of those.
    structural = dict(audit, **{
        "enumerated_window_count": 5,
        "leg_floor_refused_window_count": 0,
        "last_refusing_gate": "short",
    })
    assert _refusal_reason_for_gate(structural) == "no_legal_window"


def test_a_detail_line_that_does_not_balance_says_so() -> None:
    """The failure mode this whole shape exists to catch, made visible.

    If a future gate goes uncounted the numbers stop summing, and the page
    has to say that rather than present a short sum as a full explanation.
    """

    _ledger_path()
    from paper_trading_ledger import _window_refusal_detail  # noqa: PLC0415

    detail = _window_refusal_detail({
        "enumerated_window_count": 10,
        "center_refused_window_count": 2,
        "short_refused_window_count": 3,
        "shortlisted_window_count": 0,
        "last_refusing_gate": "short",
    })
    assert "计数不闭合" in detail


def test_which_gate_and_which_cycle_are_decided_separately() -> None:
    """The funnel picks the gate; depth picks the cycle.  Never one for both.

    Both halves of this were misattributions in their own right -- the gate
    by vote count (2026-08-17, 2026-08-20) and the cycle by recency (fixed
    2026-08-20 by ``_deeper_refusal``) -- and folding either into the other
    brings the corresponding bug back.  A window that was priced and lost on
    the number is further down the funnel than one that never reached a
    price, and further still is the objective actually judging it.
    """

    _ledger_path()
    from paper_trading_ledger import (  # noqa: PLC0415
        REFUSAL_DEPTH,
        _deeper_refusal,
    )

    # Structural, priced-but-thin and our own floor all sit on one rung:
    # none of them got a price the objective could be asked about.
    assert (
        REFUSAL_DEPTH["no_legal_window"]
        == REFUSAL_DEPTH["no_priced_window"]
        == REFUSAL_DEPTH["leg_below_floor"]
    )
    # Priced end to end and refused on the number is strictly deeper, and
    # the objective getting to judge is deeper still.
    assert (
        REFUSAL_DEPTH["negative_edge_window"]
        > REFUSAL_DEPTH["no_priced_window"]
    )
    assert (
        REFUSAL_DEPTH["no_positive_kelly"]
        > REFUSAL_DEPTH["negative_edge_window"]
    )

    # The walk keeps the deeper cycle regardless of arrival order: a morning
    # book that was priced and lost on edge is not unsaid by an evening book
    # whose structural floors took everything.
    morning = ("negative_edge_window", "枚举 6 = ...", 3, "T09:00Z")
    evening = ("no_legal_window", "枚举 6 = ...", 91, "T23:59Z")
    assert _deeper_refusal(morning, evening) == morning
    assert _deeper_refusal(evening, morning) == morning


def test_coverage_tier_follows_the_live_belief_not_the_seat() -> None:
    """The roles are live (user ruling, 2026-08-24): core is wherever the
    belief in force now puts the most mass, and the roles walk intraday."""

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))

    from paper_trading_ledger import _coverage_tier

    window = ["23°C", "24°C", "25°C", "26°C"]
    morning = {"23°C": 0.1, "24°C": 0.5, "25°C": 0.3, "26°C": 0.1}
    assert _coverage_tier("24°C", window=window, probabilities=morning) == "core"
    assert _coverage_tier("25°C", window=window, probabilities=morning) == "shoulder"
    assert _coverage_tier("26°C", window=window, probabilities=morning) == "tail"

    # The distribution walks a bucket to the right; every role walks with it.
    evening = {"23°C": 0.02, "24°C": 0.18, "25°C": 0.55, "26°C": 0.25}
    assert _coverage_tier("25°C", window=window, probabilities=evening) == "core"
    assert _coverage_tier("24°C", window=window, probabilities=evening) == "shoulder"
    assert _coverage_tier("26°C", window=window, probabilities=evening) == "shoulder"
    assert _coverage_tier("23°C", window=window, probabilities=evening) == "tail"

    # A bucket outside the posted window is a tail by construction.
    assert _coverage_tier("30°C", window=window, probabilities=evening) == "tail"


def test_coverage_premium_defaults_off_and_matches_production() -> None:
    """Off by measurement: 14 premium chains on the 08-15..23 book summed
    -48.0 ex-best and split 26 better / 67 worse.  The ledger must inherit
    production's number so the two contracts cannot drift apart."""

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))

    import paper_trading_ledger as ledger
    from bayes_basket_target.delta_v2_runtime import (
        MAKER_COVERAGE_RISK_BUDGET,
        MAKER_COVERAGE_VALUE_LAMBDA,
    )

    assert MAKER_COVERAGE_RISK_BUDGET < 0.0
    assert ledger.MAKER_RESTING_COVERAGE_BUDGET == MAKER_COVERAGE_RISK_BUDGET
    assert ledger.MAKER_RESTING_COVERAGE_LAMBDA == MAKER_COVERAGE_VALUE_LAMBDA
    # The knob must travel in the rule fingerprint, or flipping it would
    # leave rows scored under the other contract silently comparable.
    import inspect

    source = inspect.getsource(ledger)
    assert '"maker_completion_contract"' in source
    assert '"maker_coverage_risk_budget": MAKER_RESTING_COVERAGE_BUDGET' in source


def test_every_leg_carries_its_own_cash_and_the_legs_sum_to_the_stake(
) -> None:
    """Share counts do not price themselves (user ruling, 2026-08-29).

    A four-rung ladder buys each rung at a different price, so the widest
    leg is routinely not the dearest one -- OEJN 2026-08-30 put 25.7 shares
    on 38C for 4.11 and 20.1 shares on 39C for 5.42 -- and the basket total
    cannot be split by eye.  The cash is recorded where it is spent, leg by
    leg, and the legs add up to the stake the position is recorded at.
    """

    import sys
    import pytest
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from paper_trading_ledger import (  # noqa: PLC0415
        _fillable_size,
        _fillable_weighted,
    )

    # Two legs, each two levels deep, so a clip that outgrows the top level
    # pays the second one -- the walk the top-of-book price cannot see.
    legs = [
        [{"price": 0.10, "shares": 50.0}, {"price": 0.12, "shares": 500.0}],
        [{"price": 0.20, "shares": 40.0}, {"price": 0.25, "shares": 500.0}],
    ]
    equal = _fillable_size(
        legs=legs, win_probability=0.60, budget=30.0,
        fee_rate=0.0, capacity=200.0,
    )
    assert equal is not None
    size, cost, per_leg = equal
    assert len(per_leg) == len(legs)
    assert sum(per_leg) == pytest.approx(cost)
    # Equal share counts, unequal cash: the dear leg carries twice the money.
    assert per_leg[1] > per_leg[0]
    assert size > 0.0

    weighted = _fillable_weighted(
        legs=legs, weights=[0.3, 0.7], probabilities=[0.25, 0.35],
        budget=30.0, fee_rate=0.0,
    )
    assert weighted is not None
    shares, spend, per_leg = weighted
    assert len(per_leg) == len(shares)
    assert sum(per_leg) == pytest.approx(spend)

    # And the recorded row carries both: what each posted leg was priced to
    # cost and what each filled leg did cost.  Without the first, a leg that
    # bought nothing has no cash to be short of.
    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert "intended_leg_stake_json" in source
    assert "leg_stake_json" in source
    assert "json.dumps(intended_leg_stake, sort_keys=True)" in source
    assert "json.dumps(leg_stake, sort_keys=True)" in source


def test_the_page_prices_a_leg_and_says_when_it_only_derived_it() -> None:
    """Rows written before the cash was recorded are priced at top of book.

    That derivation is a lower bound, not the spend: it is exact when the
    clip never had to walk the offer and short by exactly the walk when it
    did (LFPB 2026-08-30, 0.22 of 11.32).  So it is marked, and the
    shortfall against the recorded stake is carried rather than hidden.
    """

    import sys
    import pytest
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from selection_market_dashboard import _entry_fill  # noqa: PLC0415

    common = {
        "posted_json": '["31C", "32C"]',
        "intended_json": '{"31C": 100.0, "32C": 50.0}',
        "kept_json": '{"31C": 100.0, "32C": 50.0}',
        "rungs_json": "",
        "prices": {"31C": 0.10, "32C": 0.30},
        "intended_stake": 26.0,
        "window": ["31C", "32C"],
    }
    measured = _entry_fill(
        intended_cash_json='{"31C": 10.5, "32C": 15.5}',
        kept_cash_json='{"31C": 10.5, "32C": 15.5}',
        stake=26.0, **common,
    )
    assert measured["leg_cash_derived"] is False
    assert measured["leg_cash_shortfall_usdc"] is None
    assert [leg["filled_stake_usdc"] for leg in measured["legs"]] == [
        10.5, 15.5
    ]
    # The thinner leg is the dearer one, which is the whole reason the cash
    # cannot be read off the share column.
    assert measured["legs"][1]["filled_stake_usdc"] > (
        measured["legs"][0]["filled_stake_usdc"]
    )

    derived = _entry_fill(
        intended_cash_json="", kept_cash_json="", stake=26.0, **common,
    )
    assert derived["leg_cash_derived"] is True
    # 100*0.10 + 50*0.30 = 25.0 against a 26.0 stake: the missing 1.0 is the
    # walk, and the page has to be able to say so.
    assert [leg["filled_stake_usdc"] for leg in derived["legs"]] == [
        10.0, 15.0
    ]
    assert derived["leg_cash_shortfall_usdc"] == pytest.approx(1.0)

    page = (root / "scripts" / "calibration_dashboard.py").read_text(
        encoding="utf-8"
    )
    # The column exists, and the derived rows are marked where they are read.
    assert "<th title=\"该腿的资金成本" in page
    assert "const approx=e.leg_cash_derived;" in page
    assert "leg_cash_shortfall_usdc" in page


def test_charter_66_a_topup_answers_to_the_chain_not_to_itself() -> None:
    """Charter 66 (user ruling, 2026-08-29): two chain-level gates.

    Stage-level positive EV assembled 38 chains whose blended cost crossed
    1.0 -- they hit 37 times out of 38 and still returned -31.9 USDC.
    EFHK 2026-08-24 is the canonical case: P0 held 19/20/21 for 11.05, P1
    then bought 18C at 0.997 against a 0.999 claim.  Under the claim the
    buy is rational insurance (clause B's growth is positive), and the
    chain it builds loses even when it wins (clause A refuses it) -- which
    is why both clauses exist and neither implies the other.

    The clauses are GATES on the tilt-delta size, not a sizer: the full
    log-optimal increment was tried as the sizer on the 2026-08-29 rebuild
    and bought the book from -3.3% to -18.1% ROI.
    """

    import math
    import sys
    import pytest
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from paper_trading_ledger import (  # noqa: PLC0415
        CHAIN_COST_PAR_BOUND,
        REFUSAL_REASONS,
        SELECTION_RULE_LABEL,
        _chain_cost_after_topup,
        _chain_topup_growth,
    )

    assert CHAIN_COST_PAR_BOUND == 1.0
    assert "chain_cost_at_par" in REFUSAL_REASONS
    assert "no_chain_growth" in REFUSAL_REASONS
    # The clauses ride in the label, so a book compiled without them can
    # never read as comparable to one compiled with them.
    assert "加仓链增益>0" in SELECTION_RULE_LABEL
    assert "链本<1" in SELECTION_RULE_LABEL

    held = {"19C": 14.64, "20C": 29.28, "21C": 20.08}
    claim = {"18C": 0.999, "19C": 0.0004, "20C": 0.0003, "21C": 0.0003}
    # Clause B, fed the claim, admits the insurance buy (tiny but positive).
    growth = _chain_topup_growth(
        held=held, bought={"18C": 7.39}, probabilities=claim,
        spend=7.37, bankroll=20.0,
    )
    assert growth > 0.0
    # Clause A prices the chain that buy leaves behind, and the number
    # matches what the ledger actually recorded for this chain (2.488).
    after = dict(held)
    after["18C"] = 7.39
    blended = _chain_cost_after_topup(
        held_after=after, probabilities=claim, total_stake=11.05 + 7.37,
    )
    assert blended is not None and blended >= CHAIN_COST_PAR_BOUND
    assert blended == pytest.approx(2.488, abs=0.05)

    # The discriminating case clause B exists for: a buy that concentrates
    # money on an outcome the chain already covers, at a price that eats
    # the gain, is refused by growth alone -- its blended cost stays under
    # par, so clause A never sees it.
    fat = {"20C": 0.50, "19C": 0.25, "21C": 0.20, "18C": 0.05}
    bad = _chain_topup_growth(
        held=held, bought={"20C": 10.0}, probabilities=fat,
        spend=9.99, bankroll=20.0,
    )
    assert not bad > 0.0
    # And a cheap, uncovered bucket that genuinely improves the curve
    # passes both clauses.
    good = _chain_topup_growth(
        held=held, bought={"18C": 24.7},
        probabilities={"18C": 0.55, "19C": 0.10, "20C": 0.25, "21C": 0.05},
        spend=7.4, bankroll=20.0,
    )
    assert good > 0.0
    good_after = dict(held)
    good_after["18C"] = 24.7
    good_cost = _chain_cost_after_topup(
        held_after=good_after,
        probabilities={"18C": 0.55, "19C": 0.10, "20C": 0.25, "21C": 0.05},
        total_stake=11.05 + 7.4,
    )
    assert good_cost is not None and good_cost < CHAIN_COST_PAR_BOUND

    # Spending the whole growth frame is minus infinity, not a crash.
    assert _chain_topup_growth(
        held=held, bought={"18C": 30.0}, probabilities=fat,
        spend=25.0, bankroll=20.0,
    ) == float("-inf")
    assert math.isfinite(growth)

    # A claim with no mass on any held bucket has no expected payout to
    # divide by, and refuses the same way par does.
    assert _chain_cost_after_topup(
        held_after={"30C": 5.0},
        probabilities={"31C": 1.0},
        total_stake=5.0,
    ) is None


def test_charter_66_binds_production_and_ledger_together() -> None:
    """Both lanes carry both clauses, from the same solver and same bound.

    The ledger is the measurement lane for the strategy production runs; a
    clause present in one and absent in the other measures a strategy that
    does not exist (memory: production-and-ledger-must-stay-aligned).
    """

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))

    ledger_source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    runtime_source = (
        root / "bayes_basket_target" / "delta_v2_runtime.py"
    ).read_text(encoding="utf-8")

    # Clause B: the ledger gates the tilt delta on chain growth; the
    # kernel's own baseline rule is production's spelling of the same
    # clause (a candidate must beat the no-new-action baseline to act).
    assert "_chain_topup_growth(" in ledger_source
    assert "complete_basket_target_not_activated" in (
        root / "bayes_basket_target" / "basket_kernel" / "delta_v2.py"
    ).read_text(encoding="utf-8")
    # Clause A: the same par bound on both sides, spelled the same way.
    assert "CHAIN_COST_PAR_BOUND = 1.0" in ledger_source
    assert "CHAIN_COST_PAR_BOUND = 1.0" in runtime_source
    assert "chain_cost_at_par" in ledger_source
    assert "chain_cost_at_par" in runtime_source
    # Production's gate must not exempt the incumbent: it sits in the
    # scored-window loop upstream of seat choice, where every window --
    # incumbent included -- passes through.
    assert "holds_anything and planned_total > inventory_cost" in (
        runtime_source
    )
    # The identity travels: rows record which sizing produced them.
    from paper_trading_ledger import SELECTION_RULE_SHA256  # noqa: PLC0415
    import paper_trading_ledger as ledger_module  # noqa: PLC0415

    assert hasattr(ledger_module, "CHAIN_TOPUP_BANKROLL_USDC")
    assert isinstance(SELECTION_RULE_SHA256, str) and len(
        SELECTION_RULE_SHA256
    ) == 64
    assert "chain_topup_sizing" in ledger_source


def test_charter_67_p1_waits_for_its_window() -> None:
    """P1's edge is intraday observation; its seat waits for the hours the
    daily maximum forms in (user ruling, 2026-08-30).

    The P1 lane compiles from local midnight and the entry walk took the
    first seatable cycle -- ZSQD 2026-08-29 spent P1's one top-up six
    minutes after local midnight on a single midnight observation.
    Measured over 117 settled multi-stage chains, net of the P0-only
    counterfactual: seats before local 11:00 contributed -49.5 USDC (54
    overnight -12.2, 26 morning -25.0); seats 11:00-16:00 contributed
    +99.8, still +38.7 with the best chain removed, permutation p=0.006.

    One kernel constant serves both lanes, so the strategy the ledger
    measures is the strategy production runs.
    """

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: PLC0415
        DEFAULT_P1_ENTRY_LOCAL_HOUR,
    )
    from paper_trading_ledger import (  # noqa: PLC0415
        P1_ENTRY_LOCAL_HOUR,
        REFUSAL_DEPTH,
        REFUSAL_REASONS,
        SELECTION_RULE_LABEL,
    )

    assert DEFAULT_P1_ENTRY_LOCAL_HOUR == 11.0
    assert P1_ENTRY_LOCAL_HOUR == DEFAULT_P1_ENTRY_LOCAL_HOUR
    assert "entry_window_not_open" in REFUSAL_REASONS
    # Our own clock, but NOT the certainty gate's layer.  The certainty
    # gate fires instead of judging on its own cycle; a pre-window cycle
    # never entered the funnel, and the afternoon cycles that follow DO get
    # judged -- so every judged refusal must outrank this one, or a day
    # priced and refused on edge all afternoon reads "窗未开" at 14:50
    # local (ZGGG 2026-08-30, the founding misreport).
    assert REFUSAL_DEPTH["entry_window_not_open"] < (
        REFUSAL_DEPTH["no_legal_window"]
    )
    assert REFUSAL_DEPTH["entry_window_not_open"] > (
        REFUSAL_DEPTH["incomplete_book"]
    )
    assert "P1≥本地11时" in SELECTION_RULE_LABEL

    ledger_source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    runtime_source = (root / "bayes_basket_target" / "runtime.py").read_text(
        encoding="utf-8"
    )
    # The ledger gates inside the entry walk, so a station with an afternoon
    # cycle still seats there -- the cycle is skipped, never the day.
    assert 'if stage == "P1" and P1_ENTRY_LOCAL_HOUR > 0:' in ledger_source
    # Production holds explicitly BEFORE the compile: the observation and
    # bound persistence upstream must keep flowing, so the gate must never
    # be an exception out of the compile itself.
    assert '"p1_entry_window_not_open"' in runtime_source
    assert "DEFAULT_P1_ENTRY_LOCAL_HOUR > 0" in runtime_source
