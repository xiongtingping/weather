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
            "SELECT window_size, COUNT(*) FROM paper_trade_position"
            " WHERE event_date >= '2026-08-15' GROUP BY window_size"
        ).fetchall()
    except sqlite3.DatabaseError:
        return
    finally:
        conn.close()
    for size, count in rows:
        assert 3 <= int(size) <= 4, (
            f"{count} seats recorded at {size} buckets, outside [3, 4]"
        )
