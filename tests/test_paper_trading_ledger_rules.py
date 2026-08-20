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


def _ledger():
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    path = root / "scripts" / "paper_trading_ledger.py"
    spec = importlib.util.spec_from_file_location(
        "paper_trading_ledger", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("paper_trading_ledger", module)
    spec.loader.exec_module(module)
    return module


def test_every_enumeration_gate_has_a_refusal_to_be_reported_as() -> None:
    """A gate the ledger cannot name is a refusal it will misattribute.

    Twice now the page has been handed counters that could not add up to the
    refusal they were offered as the explanation of: the leg floor on
    2026-08-17, and the edge/friction gate until 2026-08-20.  Both times the
    missing gate's refusals were silently absorbed by whichever gate was
    listed.  The mapping is keyed by the funnel the runtime publishes so a
    new gate fails here rather than there.
    """

    from bayes_basket_target.delta_v2_runtime import WINDOW_REFUSAL_FUNNEL

    ledger = _ledger()
    assert set(ledger.GATE_REFUSAL_REASON) == set(WINDOW_REFUSAL_FUNNEL)
    assert set(ledger.GATE_LABEL) == set(WINDOW_REFUSAL_FUNNEL)
    for reason in ledger.GATE_REFUSAL_REASON.values():
        assert reason in ledger.REFUSAL_REASONS


def test_the_refusal_names_the_gate_that_took_the_last_window() -> None:
    """Not the gate with the biggest count, and not price by default.

    The (2,3,1) shape below was the second commonest P0 refusal on the
    2026-08-20 book, 44 station-days of it.  The old test asked whether the
    leg floor outnumbered the two structural gates -- 1 > 2 + 3 is false --
    so the day was filed as "the market priced us out" when the market had
    quoted every rung and our own floor is what refused the last window.
    """

    ledger = _ledger()
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
    assert ledger._refusal_reason_for_gate(audit) == "leg_below_floor"
    detail = ledger._window_refusal_detail(audit)
    # The line is an equation, and it balances.
    assert "枚举 6 =" in detail
    assert "计数不闭合" not in detail
    # Every gate appears, including the ones that did not fire, so a reader
    # can check the arithmetic rather than take the verdict on trust.
    for label in ledger.GATE_LABEL.values():
        assert label in detail

    # And the genuinely price-driven refusal keeps its own name.
    priced_out = dict(audit, **{
        "leg_floor_refused_window_count": 0,
        "edge_refused_window_count": 1,
        "last_refusing_gate": "edge",
    })
    assert (
        ledger._refusal_reason_for_gate(priced_out) == "negative_edge_window"
    )
    # A structural sweep is neither of those.
    structural = dict(audit, **{
        "enumerated_window_count": 5,
        "leg_floor_refused_window_count": 0,
        "last_refusing_gate": "short",
    })
    assert ledger._refusal_reason_for_gate(structural) == "no_legal_window"


def test_a_detail_line_that_does_not_balance_says_so() -> None:
    """The failure mode this whole shape exists to catch, made visible.

    If a future gate goes uncounted the numbers stop summing, and the page
    has to say that rather than present a short sum as a full explanation.
    """

    ledger = _ledger()
    detail = ledger._window_refusal_detail({
        "enumerated_window_count": 10,
        "center_refused_window_count": 2,
        "short_refused_window_count": 3,
        "shortlisted_window_count": 0,
        "last_refusing_gate": "short",
    })
    assert "计数不闭合" in detail
