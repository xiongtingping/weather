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
    """Phantom wins must not size real seats.

    A basket bought under ``DUST_COST_FLOOR`` is a one-sided tail quote, and
    the report has always split the book on that line.  Sizing did not: the
    daily compounding fed on the unsplit P&L, so 2026-08-13's dust -- +3713
    (P0) and +3733 (P1) on baskets costing a tenth of a cent -- lifted equity
    from about a thousand to 4615/4792 and the 2% per-scope cap sized
    2026-08-15's seats at 73 USDC apiece instead of 20.  Every one of those
    was a real fill losing real money at nearly four times its right size.

    The invariant that catches it without re-running the sweep: replay the
    compounding on credible settlements alone and no recorded stake may
    exceed the per-scope cap against the equity its day opened with.
    """

    import sqlite3
    from collections import defaultdict
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # The two places the split has to bite: what compounds, and what the day
    # is allowed to deploy.
    assert "credible = all_in_cost >= DUST_COST_FLOOR" in source
    assert "if credible:\n                        day_staked[stage] += stake" in source
    # The P&L now arrives from ``_score_settlement``, which the entry walk and
    # the truth-revision re-score share so the two cannot value the same seat
    # differently.  What this line guards is unchanged: only a credible fill
    # compounds.
    assert (
        "if credible:\n                        day_pnl[stage]"
        " += scored.pnl_usdc" in source
    )

    db = root / "data" / "bayes_basket_target_ssot.db"
    if not db.is_file():
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = conn.execute(
            "SELECT p.event_date, p.stage, p.stake_usdc, p.all_in_cost,"
            " s.pnl_usdc"
            " FROM paper_trade_position p"
            " LEFT JOIN paper_trade_settlement s USING(position_id)"
            " WHERE p.selection_rule_sha256 = ("
            "  SELECT selection_rule_sha256 FROM paper_trade_position"
            "  ORDER BY decision_at_utc DESC LIMIT 1)"
            " ORDER BY p.event_date"
        ).fetchall()
    except sqlite3.DatabaseError:
        return
    finally:
        conn.close()
    if not rows:
        return

    initial, cap, floor = 1000.0, 0.02, 0.01
    equity: dict[str, float] = defaultdict(lambda: initial)
    by_day: dict[str, list] = defaultdict(list)
    for event_date, stage, stake, cost, pnl in rows:
        by_day[str(event_date)].append((str(stage), stake, cost, pnl))
    for event_date in sorted(by_day):
        realised: dict[str, float] = defaultdict(float)
        for stage, stake, cost, pnl in by_day[event_date]:
            opening = equity[stage]
            assert float(stake) <= opening * cap + 1e-6, (
                f"{stage} {event_date} staked {float(stake):.2f} against an "
                f"opening equity of {opening:.2f} -- over the "
                f"{cap:.0%} per-scope cap, which means the compounding fed "
                f"on something the report calls a dust offer"
            )
            if pnl is not None and float(cost) >= floor:
                realised[stage] += float(pnl)
        for stage, amount in realised.items():
            equity[stage] = max(0.0, equity[stage] + amount)


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
    assert opened["P1"] == INITIAL_CAPITAL_USDC - 275.0, (
        "an incremental window must open on the realised book before it, "
        f"got {opened['P1']}"
    )
    assert opened["P0"] == INITIAL_CAPITAL_USDC, (
        "a stage with no history before the window opens at the initial "
        "capital"
    )
    # A rebuild has no floor and walks every day itself, so it starts where
    # the book started.
    assert _opening_equity(conn, earliest="") == {
        stage: INITIAL_CAPITAL_USDC for stage in ("P0", "P0-M", "P1")
    }
