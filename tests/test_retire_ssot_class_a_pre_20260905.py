"""The class-A retirement deletes only unguarded pre-cutoff rows and can be undone.

Checked against a store shaped like production's: an append-only table whose
BEFORE DELETE trigger must still be in force after the run, a row a training
fact names, a row of a station-day that had a live intent, and rows either
side of the cutoff.
"""

from __future__ import annotations

import sqlite3
import sys
import gzip
import hashlib
from datetime import datetime, timedelta, timezone
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
    conn.rollback()
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


def test_explicit_older_cutoff_keeps_live_day_and_can_restore(
    store: Path, tmp_path: Path, monkeypatch
) -> None:
    _only_envelopes(monkeypatch)
    _plenty_of_disk(monkeypatch)
    export = tmp_path / "later"
    retire.main(["--db", str(store), "--export-dir", str(export),
                 "--cutoff", "2026-09-07", "--apply"])
    conn = sqlite3.connect(store)
    left = {r[0] for r in conn.execute("SELECT envelope_id FROM basket_probability_envelope")}
    assert left == {"old_named", "old_live_day"}
    retire.main(["--db", str(store), "--export-dir", str(export), "--restore"])
    assert conn.execute("SELECT COUNT(*) FROM basket_probability_envelope").fetchone()[0] == 4


def test_export_readback_and_same_device_guard(
    store: Path, tmp_path: Path, monkeypatch
) -> None:
    _only_envelopes(monkeypatch)
    payload = b"a\n" * 1000
    archive = tmp_path / "member.gz"
    with gzip.open(archive, "wb") as stream:
        stream.write(payload)
    digest = hashlib.sha256(payload).hexdigest()
    retire._verify_export_member(archive, 0, digest, len(payload))
    with pytest.raises(RuntimeError, match="verification_failed"):
        retire._verify_export_member(archive, 0, "0" * 64, len(payload))
    with pytest.raises(SystemExit, match="ssot_export_must_be_on_another_device"):
        retire.main(["--db", str(store), "--export-dir", str(tmp_path / "same"),
                     "--apply", "--require-external"])


def test_cutoff_refuses_days_inside_the_seven_day_boundary(
    store: Path, tmp_path: Path, monkeypatch
) -> None:
    _only_envelopes(monkeypatch)
    too_recent = (datetime.now(timezone.utc).date() - timedelta(days=6)).isoformat()
    with pytest.raises(SystemExit, match="cutoff_outside_retired_closed_day_window"):
        retire.main(["--db", str(store), "--export-dir", str(tmp_path / "recent"),
                     "--cutoff", too_recent, "--apply"])
    assert sqlite3.connect(store).execute(
        "SELECT COUNT(*) FROM basket_probability_envelope"
    ).fetchone()[0] == 4


def test_bounded_batches_resume_across_cutoff_and_preserve_guards(
    store: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    _only_envelopes(monkeypatch)
    _plenty_of_disk(monkeypatch)
    conn = sqlite3.connect(store)
    for i in range(3):
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?)",
            (f"extra_{i}", "KLGA", "2026-09-01", "2026-09-01T11:00:00Z", "{}", b"x"),
        )
    conn.commit()
    export = tmp_path / "bounded"
    args = ["--db", str(store), "--export-dir", str(export), "--cutoff",
            "2026-09-05", "--batch-bytes", "1", "--max-batches", "1", "--apply"]
    for expected in (1, 2, 3, 4):
        retire.main(args)
        assert '"state": "partial"' in capsys.readouterr().out
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope"
        ).fetchone()[0] == 7 - expected
    retire.main(args)
    assert '"state": "applied"' in capsys.readouterr().out
    assert {row[0] for row in conn.execute(
        "SELECT envelope_id FROM basket_probability_envelope"
    )} == {"old_named", "old_live_day", "new"}
    with pytest.raises(sqlite3.DatabaseError, match="append_only"):
        conn.execute("DELETE FROM basket_probability_envelope WHERE envelope_id='new'")
    assert len((export / "manifest.jsonl").read_text().splitlines()) == 4
    conn.rollback()
    retire.main(["--db", str(store), "--export-dir", str(export), "--restore"])
    assert conn.execute("SELECT COUNT(*) FROM basket_probability_envelope").fetchone()[0] == 7


def test_exact_plan_references_release_only_unread_plans(
    store: Path, tmp_path: Path, monkeypatch
) -> None:
    _plenty_of_disk(monkeypatch)
    monkeypatch.setattr(retire, "_require_external_export", lambda *_: None)
    monkeypatch.setattr(retire, "TABLES", ((
        "basket_p0_preselection_plan", "decision_at_utc", (), "live",
    ),))
    conn = sqlite3.connect(store)
    conn.execute("ALTER TABLE auto_trade_decisions ADD COLUMN details_json TEXT")
    conn.execute(
        "UPDATE auto_trade_decisions SET details_json=? WHERE decision_id='d_live'",
        ('{"production_plan_sha256":"sha_named"}',),
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions(decision_id,station_id,event_date,details_json)"
        " VALUES ('d_id','EGLC','2026-09-03',?)",
        ('{"preselection_id":"id_named"}',),
    )
    conn.execute(
        "CREATE TABLE basket_p0_preselection_plan (preselection_id TEXT,"
        " station_id TEXT,event_date TEXT,decision_at_utc TEXT,plan_json TEXT)"
    )
    conn.execute(
        "CREATE TRIGGER plan_no_delete BEFORE DELETE ON basket_p0_preselection_plan"
        " BEGIN SELECT RAISE(ABORT,'append_only'); END"
    )
    for plan_id, day, sha, body in (
        ("hash_named", "2026-09-03", "sha_named", None),
        ("id_named", "2026-09-03", "sha_id", None),
        ("free", "2026-09-03", "sha_free", None),
        ("unrecognized", "2026-09-03", None, "{}"),
        ("latest", "2026-09-06", "sha_latest", None),
    ):
        conn.execute(
            "INSERT INTO basket_p0_preselection_plan VALUES (?,?,?,?,?)",
            (plan_id, "EGLC", day, day + "T10:00:00Z",
             body or '{"compiled_plan":{"production_plan_sha256":"' + sha + '"}}'),
        )
    conn.execute(
        "INSERT INTO basket_p0_preselection_plan VALUES (?,?,?,?,?)",
        ("old_latest", "NZWN", "2026-09-01", "2026-09-01T10:00:00Z",
         '{"compiled_plan":{"production_plan_sha256":"sha_old_latest"}}'),
    )
    conn.commit()
    export = tmp_path / "exact"
    args = ["--db", str(store), "--export-dir", str(export), "--apply",
            "--exact-preselection-refs", "--cutoff", "2026-09-05"]
    retire.main(args)
    assert {r[0] for r in conn.execute(
        "SELECT preselection_id FROM basket_p0_preselection_plan"
    )} == {"hash_named", "id_named", "unrecognized", "latest", "old_latest"}
    retire.main(args)
    assert len((export / "manifest.jsonl").read_text().splitlines()) == 1
    with pytest.raises(sqlite3.DatabaseError, match="append_only"):
        conn.execute("DELETE FROM basket_p0_preselection_plan WHERE preselection_id='latest'")
    conn.rollback()
    retire.main(["--db", str(store), "--export-dir", str(export), "--restore"])
    assert conn.execute("SELECT COUNT(*) FROM basket_p0_preselection_plan").fetchone()[0] == 6


def test_exact_reference_audit_fails_closed_on_unreadable_decision(
    store: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(retire, "_require_external_export", lambda *_: None)
    monkeypatch.setattr(retire, "TABLES", ((
        "basket_p0_preselection_plan", "decision_at_utc", (), "live",
    ),))
    conn = sqlite3.connect(store)
    conn.execute("ALTER TABLE auto_trade_decisions ADD COLUMN details_json TEXT")
    conn.execute("UPDATE auto_trade_decisions SET details_json='not json'")
    conn.execute("CREATE TABLE basket_p0_preselection_plan ("
                 "preselection_id TEXT,station_id TEXT,event_date TEXT,"
                 "decision_at_utc TEXT,plan_json TEXT)")
    conn.commit()
    with pytest.raises(RuntimeError, match="preselection_decision_details_unreadable"):
        retire.main(["--db", str(store), "--export-dir", str(tmp_path / "invalid"),
                     "--exact-preselection-refs", "--apply"])
    assert conn.execute("SELECT COUNT(*) FROM basket_p0_preselection_plan").fetchone()[0] == 0


def test_new_decision_before_exact_delete_aborts_batch(
    store: Path, tmp_path: Path, monkeypatch
) -> None:
    _plenty_of_disk(monkeypatch)
    monkeypatch.setattr(retire, "_require_external_export", lambda *_: None)
    monkeypatch.setattr(retire, "TABLES", ((
        "basket_p0_preselection_plan", "decision_at_utc", (), "live",
    ),))
    conn = sqlite3.connect(store)
    conn.execute("ALTER TABLE auto_trade_decisions ADD COLUMN details_json TEXT")
    conn.execute("CREATE TABLE basket_p0_preselection_plan ("
                 "preselection_id TEXT,station_id TEXT,event_date TEXT,"
                 "decision_at_utc TEXT,plan_json TEXT)")
    for plan_id, day in (("old", "2026-09-01"), ("new", "2026-09-06")):
        conn.execute(
            "INSERT INTO basket_p0_preselection_plan VALUES (?,?,?,?,?)",
            (plan_id, "KLGA", day, day + "T10:00:00Z",
             '{"compiled_plan":{"production_plan_sha256":"sha_' + plan_id + '"}}'),
        )
    conn.commit()
    original = retire._preselection_decision_references

    def _concurrent_decision(reader):
        refs = original(reader)
        writer = sqlite3.connect(store)
        writer.execute(
            "INSERT INTO auto_trade_decisions(decision_id,station_id,event_date,details_json)"
            " VALUES ('late','KLGA','2026-09-01',?)",
            ('{"production_plan_sha256":"sha_old"}',),
        )
        writer.commit()
        writer.close()
        return refs

    monkeypatch.setattr(retire, "_preselection_decision_references", _concurrent_decision)
    with pytest.raises(RuntimeError, match="preselection_decisions_changed_before_delete"):
        retire.main(["--db", str(store), "--export-dir", str(tmp_path / "race"),
                     "--exact-preselection-refs", "--apply"])
    assert conn.execute("SELECT COUNT(*) FROM basket_p0_preselection_plan").fetchone()[0] == 2


def test_exact_reference_scan_limit_fails_closed(store: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(retire, "_require_external_export", lambda *_: None)
    monkeypatch.setattr(retire, "PRESELECTION_REF_MAX_ROWS", 0)
    monkeypatch.setattr(retire, "TABLES", ((
        "basket_p0_preselection_plan", "decision_at_utc", (), "live",
    ),))
    conn = sqlite3.connect(store)
    conn.execute("ALTER TABLE auto_trade_decisions ADD COLUMN details_json TEXT")
    conn.execute("CREATE TABLE basket_p0_preselection_plan ("
                 "preselection_id TEXT,station_id TEXT,event_date TEXT,"
                 "decision_at_utc TEXT,plan_json TEXT)")
    conn.commit()
    with pytest.raises(RuntimeError, match="preselection_reference_scan_resource_limit"):
        retire.main(["--db", str(store), "--export-dir", str(tmp_path / "bounded"),
                     "--exact-preselection-refs", "--apply"])


def test_exact_mode_requires_external_archive(store: Path, tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="ssot_export_must_be_on_another_device"):
        retire.main(["--db", str(store), "--export-dir", str(tmp_path / "same_device"),
                     "--exact-preselection-refs", "--apply"])
