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
    """Whatever is in the book now must satisfy the rules now."""

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
            "SELECT stage, window_size, COUNT(*) FROM paper_trade_position"
            " WHERE selection_rule_sha256 = ("
            "  SELECT selection_rule_sha256 FROM paper_trade_position"
            "  ORDER BY decision_at_utc DESC LIMIT 1)"
            " GROUP BY stage, window_size"
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
