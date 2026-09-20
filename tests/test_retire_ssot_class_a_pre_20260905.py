"""The class-A retirement deletes only unguarded pre-cutoff rows and can be undone.

Checked against a store shaped like production's: an append-only table whose
BEFORE DELETE trigger must still be in force after the run, a row a training
fact names, a row of a station-day that had a live intent, and rows either
side of the cutoff.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import retire_ssot_class_a_pre_20260905 as retire  # noqa: E402


def _store(path: Path) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(
        """
        CREATE TABLE basket_probability_envelope (
            envelope_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,
            decision_at_utc TEXT, probabilities_json TEXT, payload BLOB);
        CREATE TRIGGER basket_probability_envelope_no_delete
        BEFORE DELETE ON basket_probability_envelope
        BEGIN SELECT RAISE(ABORT,'append_only'); END;
        CREATE TABLE basket_policy_training_fact (
            fact_id TEXT, probability_envelope_id TEXT, decision_id TEXT);
        CREATE TABLE auto_trade_decisions (
            decision_id TEXT, cycle_id TEXT, station_id TEXT, event_date TEXT,
            created_at TEXT);
        CREATE TABLE basket_target_order_intents (
            order_intent_id TEXT, decision_id TEXT, cycle_id TEXT,
            live_submit_authorized INTEGER, created_at TEXT);
        """
    )
    rows = [
        ("old_free", "KLGA", "2026-09-01", "2026-09-01T10:00:00Z"),
        ("old_named", "KLGA", "2026-09-02", "2026-09-02T10:00:00Z"),
        ("old_live_day", "EGLC", "2026-09-03", "2026-09-03T10:00:00Z"),
        ("new", "KLGA", "2026-09-06", "2026-09-06T10:00:00Z"),
    ]
    for env, station, day, at in rows:
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?)",
            (env, station, day, at, '{"25°C": 0.5}', b"\x00\x01"),
        )
    conn.execute("INSERT INTO basket_policy_training_fact VALUES ('f1','old_named','d0')")
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES "
        "('d_live','c_live','EGLC','2026-09-03','2026-09-03T10:00:00Z')"
    )
    conn.execute(
        "INSERT INTO basket_target_order_intents VALUES "
        "('i1','d_live','c_live',1,'2026-09-03T10:01:00Z')"
    )
    conn.close()


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    path = tmp_path / "ssot.db"
    _store(path)
    return path


# The mechanics are exercised on an envelope-shaped table; the envelope
# itself is not class A (see test_envelope_is_never_retired).
ENVELOPE_SHAPED_SPEC = (
    "basket_probability_envelope", "decision_at_utc",
    (("envelope_id", (
        ("basket_policy_training_fact", "probability_envelope_id"),
    )),),
    "live",
)


def _only_envelopes(monkeypatch) -> None:
    monkeypatch.setattr(retire, "TABLES", (ENVELOPE_SHAPED_SPEC,))


def test_envelope_is_never_retired() -> None:
    """Retired and restored on 2026-09-19: the daily refits read its history.

    fit_taf_likelihood reads P0/P0-M envelopes from 2026-08-16, the P1 sample
    builder a 45-day window of them, and calibration_refit replays every
    settled candidate window against its envelope.
    """
    assert "basket_probability_envelope" not in {spec[0] for spec in retire.TABLES}


def test_dry_run_changes_nothing(store: Path, tmp_path: Path, monkeypatch) -> None:
    _only_envelopes(monkeypatch)
    retire.main(["--db", str(store), "--export-dir", str(tmp_path / "x")])
    conn = sqlite3.connect(store)
    assert conn.execute("SELECT COUNT(*) FROM basket_probability_envelope").fetchone()[0] == 4
    assert not list((tmp_path / "x").glob("*.jsonl.gz"))


def _plenty_of_disk(monkeypatch) -> None:
    """The export's own guard reads the HOST's free disk (it refuses to start
    a batch under 5 GiB, so a retirement never fills the volume it is
    exporting to).  A test that inherits it passes or fails on whatever the
    machine happens to have free -- 2026-09-20 it had 3.2 GiB and these two
    went red on a machine issue, not a code one.  The guard has its own
    test; these measure the retirement."""

    monkeypatch.setattr(retire, "_free_disk", lambda _path: retire.MIN_FREE_DISK_BYTES * 2)


def test_apply_retires_only_unguarded_rows_and_keeps_the_trigger(
    store: Path, tmp_path: Path, monkeypatch
) -> None:
    _only_envelopes(monkeypatch)
    _plenty_of_disk(monkeypatch)
    retire.main(["--db", str(store), "--export-dir", str(tmp_path / "x"), "--apply"])
    conn = sqlite3.connect(store)
    left = {r[0] for r in conn.execute("SELECT envelope_id FROM basket_probability_envelope")}
    # Named by a training fact, a live station-day, and post-cutoff stay.
    assert left == {"old_named", "old_live_day", "new"}
    with pytest.raises(sqlite3.DatabaseError, match="append_only"):
        conn.execute("DELETE FROM basket_probability_envelope WHERE envelope_id='new'")
    manifest = (tmp_path / "x" / "manifest.jsonl").read_text().splitlines()
    assert len(manifest) == 1 and '"rows": 1' in manifest[0]


def test_restore_puts_rows_back_verbatim(store: Path, tmp_path: Path, monkeypatch) -> None:
    _only_envelopes(monkeypatch)
    _plenty_of_disk(monkeypatch)
    before = sqlite3.connect(store).execute(
        "SELECT rowid,* FROM basket_probability_envelope ORDER BY rowid"
    ).fetchall()
    retire.main(["--db", str(store), "--export-dir", str(tmp_path / "x"), "--apply"])
    retire.main(["--db", str(store), "--export-dir", str(tmp_path / "x"), "--restore"])
    after = sqlite3.connect(store).execute(
        "SELECT rowid,* FROM basket_probability_envelope ORDER BY rowid"
    ).fetchall()
    assert after == before
