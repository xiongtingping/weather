"""The dashboard recomputes through the cold tier, and only there (「在外置硬盘」).

User ruling 2026-10-01: the truth-landing and H5 sections keep their closed
days cached; when a code change voids the cache, the days whose rows charter
238 moved are read from the external volume.  An unreadable volume is a
section that is not ready -- never a shorter history.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.offloaded_paths import OffloadedVolumeMissing
from scripts import calibration_dashboard as dash
from scripts import ssot_cold_offload as off


def test_an_unreadable_volume_is_a_section_not_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "ssot.db"
    sqlite3.connect(db).execute("CREATE TABLE t(x)")

    def gone(_conn, _tables, **_kwargs):
        raise OffloadedVolumeMissing("unplugged")

    monkeypatch.setattr(off, "attach_cold_history", gone)
    monkeypatch.setattr(dash, "COLD_TIER_STORE", db)
    conn = sqlite3.connect(db)
    with pytest.raises(dash.SectionNotReady, match="cold_tier_unavailable"):
        dash._history_conn(conn, ("basket_probability_envelope",))


def test_the_history_connection_reads_the_same_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "ssot.db"
    setup = sqlite3.connect(db)
    setup.execute("CREATE TABLE t(x)")
    setup.execute("INSERT INTO t VALUES(1)")
    setup.commit()
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(off, "attach_cold_history", lambda _c, tables, **_k: seen.append(tuple(tables)) or {})
    monkeypatch.setattr(dash, "COLD_TIER_STORE", db)
    history = dash._history_conn(sqlite3.connect(db), ("basket_official_ws_snapshot",))
    try:
        assert history.execute("SELECT x FROM t").fetchall() == [(1,)]
        with pytest.raises(sqlite3.OperationalError):
            history.execute("INSERT INTO t VALUES(2)")
    finally:
        history.close()
    assert seen == [("basket_official_ws_snapshot",)]


def test_any_other_store_is_read_as_it_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(off, "attach_cold_history", lambda *_a, **_k: pytest.fail("attached"))
    conn = sqlite3.connect(":memory:")
    assert dash._history_conn(conn, ("basket_probability_envelope",)) is conn
    other = sqlite3.connect(tmp_path / "copy.db")
    assert dash._history_conn(other, ("basket_probability_envelope",)) is other


def test_the_cold_book_floor_is_the_offload_cutoff() -> None:
    from datetime import datetime, timezone

    assert dash._cold_book_floor() == off.POLICY_BY_TABLE["basket_official_ws_snapshot"].cutoff_date(
        datetime.now(timezone.utc)
    )
