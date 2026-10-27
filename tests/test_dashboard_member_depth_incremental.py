"""Member depth counted incrementally (user 2026-10-27 「把成员深度和真值落位改成增量读」).

The counts walked every family's slice of ``calibration_native_member_skill_idx``
(~10M entries) each run; on the swapping host one walk took 24 minutes, hit the
WAL yield line and never landed.  A paged run now keeps per-day rows and
stations with the highest rowid, adds the rows above it next run and counts
the days near the cold offload's line again.  Every step here is checked
against the old whole-slice COUNT.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import calibration_dashboard as C  # noqa: E402

NOW = datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)
FAMILIES = (("SYS_A", "A", "T_A", True), ("SYS_B", "B", "T_B", True))


def _old(conn, system, transport):
    return tuple(conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT station_id), COUNT(DISTINCT target_local_date), MAX(rowid)"
        " FROM calibration_native_ensemble_historical_member_run"
        " WHERE ensemble_system_id=? AND transport_provider_id=?",
        (system, transport),
    ).fetchone())


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)
    monkeypatch.setattr(C, "MEMBER_FAMILIES", FAMILIES)
    monkeypatch.setattr(C, "_now", lambda: NOW)
    monkeypatch.setattr(C, "_PAGED_RUN", {"paged": True})
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_native_ensemble_historical_member_run ("
        " ensemble_system_id, transport_provider_id, qc_status, target_local_date, station_id,"
        " native_run_init_utc, run_cycle, archive_lead_bucket, raw_tmax_c, archive_query_at_utc)"
    )
    conn.execute(
        "CREATE INDEX calibration_native_member_skill_idx ON calibration_native_ensemble_historical_member_run("
        " ensemble_system_id, transport_provider_id, qc_status, target_local_date, station_id,"
        " native_run_init_utc, run_cycle, archive_lead_bucket, raw_tmax_c)"
    )
    return conn


def _add(conn, system, transport, day, station, n=3, qc="ACCEPTED"):
    conn.executemany(
        "INSERT INTO calibration_native_ensemble_historical_member_run VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(system, transport, qc, day, station, "2026-10-01T00:00:00Z", "00Z", "h24_48", 20.0,
          "2026-10-01T05:00:00Z")] * n,
    )


def _day(ago):
    return (NOW.date() - timedelta(days=ago)).isoformat()


def _check(conn):
    got = C._member_depth_counts(conn)
    for system, _l, transport, _b in FAMILIES:
        assert got[system] == _old(conn, system, transport), system


def test_the_counts_match_the_whole_slice_through_growth_and_offload(board):
    conn = board
    for ago in (120, 100, 86, 40, 2):
        for station in ("ZUUU", "KLGA"):
            _add(conn, "SYS_A", "T_A", _day(ago), station)
    _add(conn, "SYS_B", "T_B", _day(3), "EGLC", qc="QUARANTINED")
    _add(conn, "SYS_A", "OTHER", _day(3), "EGLC")       # another transport: not the family's
    _check(conn)                                          # the first build
    _add(conn, "SYS_A", "T_A", _day(0), "NZWN")           # new captures
    _add(conn, "SYS_A", "T_A", _day(2), "ZUUU", n=2)
    _add(conn, "SYS_B", "T_B", _day(1), "EGLC", qc="ACCEPTED")
    _check(conn)
    _add(conn, "SYS_A", "T_A", _day(110), "RJTT")         # a backfill into an old day
    _check(conn)
    # The cold offload takes the old days (dropping the append-only rule for it).
    conn.execute(
        "DELETE FROM calibration_native_ensemble_historical_member_run"
        " WHERE target_local_date < ? AND station_id <> 'RJTT'", (_day(90),),
    )
    _check(conn)
    _check(conn)                                          # a run with nothing new


def test_a_build_cut_short_keeps_the_families_it_finished(board, monkeypatch):
    conn = board
    _add(conn, "SYS_A", "T_A", _day(5), "ZUUU")
    _add(conn, "SYS_B", "T_B", _day(5), "ZUUU")
    real = conn.execute
    calls = {"whole": 0}

    class Yielding:
        def execute(self, sql, *args):
            if sql.rstrip().endswith("GROUP BY 1,2,3"):
                calls["whole"] += 1
                if calls["whole"] == 2:
                    raise C.RenderYielded("wal past the line")
            return real(sql, *args)

    with pytest.raises(C.RenderYielded):
        C._member_depth_counts(Yielding())
    kept = C._load_closed_readings(C.RENDER_MEMBER_DEPTH_DAYS)["families"]
    assert set(kept) == {"SYS_A"}
    _check(conn)


def test_the_recount_line_sits_inside_the_offload_window():
    import ssot_cold_offload as O  # noqa: PLC0415

    assert C.MEMBER_DEPTH_RECOUNT_OLDER_THAN_DAYS == O.MEMBER_RUN_HOT_DAYS - 5


def test_without_a_paged_run_nothing_is_kept(board, monkeypatch):
    monkeypatch.setattr(C, "_PAGED_RUN", {})
    _add(board, "SYS_A", "T_A", _day(5), "ZUUU")
    _check(board)
    assert C._load_closed_readings(C.RENDER_MEMBER_DEPTH_DAYS) == {}
