"""The backend board-readings job (user rulings 2026-10-02 「只读是读后端算出
来的」 / 「新开一个后端读数任务」): it writes what the board shows, reads the
store read-only, and a reading that fails keeps its previous file."""

from __future__ import annotations

import json
import sqlite3

from bayes_basket_target import board_readings as BR


def _db(tmp_path):
    path = tmp_path / "ssot.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    return path


def test_a_reading_round_trips_and_a_foreign_file_is_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(BR, "READINGS_DIR", tmp_path)
    BR.write_reading("l4_lane", {"forward": {}}, code="c", build_sec=2.0)
    got = BR.load_reading("l4_lane")
    assert got["value"] == {"forward": {}} and got["code_sha256"] == "c"
    assert got["computed_at_utc"].endswith("Z")
    (tmp_path / "centre_shape.json").write_text(json.dumps({"schema": "other"}))
    assert BR.load_reading("centre_shape") is None
    assert BR.load_reading("seat_windows") is None


def test_the_store_is_opened_read_only_and_a_failure_keeps_the_last_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(BR, "READINGS_DIR", tmp_path / "readings")
    db = _db(tmp_path)

    def writes(conn):
        conn.execute("INSERT INTO t VALUES (1)")
        return {"never": True}

    def reads(conn):
        return {"rows": conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]}

    monkeypatch.setitem(BR._READINGS, "l4_lane", (reads, (reads,)))
    assert BR.run(db, ["l4_lane"])[0].startswith("l4_lane ")
    monkeypatch.setitem(BR._READINGS, "l4_lane", (writes, (writes,)))
    receipt = BR.run(db, ["l4_lane"])[0]
    assert receipt.startswith("l4_lane failed: OperationalError"), receipt
    assert BR.load_reading("l4_lane")["value"] == {"rows": 0}, "the last good file stays"
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_seat_windows_read_again_only_the_cells_that_can_still_change(tmp_path, monkeypatch) -> None:
    """2026-10-02: re-reading 45 days of cells every hour was the job's whole
    cost, inside production's cycle.  A run reads from SEAT_REREAD_HOURS
    before the last run began and keeps every older cell as written."""

    from datetime import datetime, timezone

    from bayes_basket_target import center_width_store as store

    monkeypatch.setattr(BR, "READINGS_DIR", tmp_path)
    now = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(BR, "_now", lambda: now)
    monkeypatch.setattr(store, "_truth", lambda _conn: {})
    asked: list[str] = []
    keys = ["20C", "21C", "22C"]

    def cells(_conn, _truth, *, stage, min_decision_at_utc):
        asked.append(min_decision_at_utc)
        if stage != "P0":
            return {}
        return {"2026-10-02": [{
            "station_id": "KAAA", "event_date": "2026-10-02",
            "decision_at_utc": "2026-10-02T13:00:00Z", "ordered_keys": keys,
            "probabilities": {"20C": 0.2, "21C": 0.6, "22C": 0.2},
            "excluded_buckets": [],
        }]}

    monkeypatch.setattr(store, "serving_cells_by_day", cells)
    old_key = "KAAA|2026-09-30|2026-09-29T06:00:00Z"
    BR.write_reading("seat_windows", {"seats": {old_key: {"state": "SYMMETRIC", "sign": 0,
                     "seat_keys": ["20C", "21C", "22C"]}}},
                     code=BR._code_sha(BR.seat_window), build_sec=0.0)
    previous = BR.load_reading("seat_windows")["computed_at_utc"]
    value = BR.seat_windows(None)
    assert set(asked) == {value["reread_from_utc"]}
    assert value["reread_from_utc"] > value["floor_utc"]
    assert value["reread_from_utc"] > "2026-10-02T07"
    assert old_key in value["seats"], "an older cell is kept as written"
    assert "KAAA|2026-10-02|2026-10-02T13:00:00Z" in value["seats"]
    assert value["computed"] == 1


def test_the_served_seat_is_the_one_the_newest_p0_names_not_the_highest_generation(monkeypatch) -> None:
    """2026-10-02: MAX(generation) per scope stood in for "the served seat";
    since charter 198 it was mostly a two-system re-mint (706 of 743 rows)
    while the served P0s carried six.  The seat is the one the station-day's
    newest P0 names in its lineage."""

    from datetime import datetime, timezone

    monkeypatch.setattr(BR, "_now", lambda: datetime(2026, 10, 2, 12, tzinfo=timezone.utc))
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE basket_probability_envelope (station_id, event_date, stage, state,
            decision_at_utc, lineage_json);
        CREATE TABLE calibration_cross_native_ensemble_artifact (cross_ensemble_artifact_id,
            season_bin, lead_bucket, run_cycle, combination_method, selection_evidence_sha256,
            content_sha256);
        CREATE TABLE calibration_native_ensemble_activation (available_system_set_hash_or_star,
            content_sha256);
        CREATE TABLE calibration_cross_native_ensemble_selection_evidence (content_sha256,
            selected_weights_json, cross_system_residual_correlation_json,
            run_cycle_by_system_json, family_weight_caps_json, train_end_date,
            candidate_system_ids_json, selected_system_ids_json, holdout_end_date,
            selected_system_count);
    """)
    for at, art in (("2026-10-02T06:00:00Z", "a-old"), ("2026-10-02T09:00:00Z", "a-six")):
        conn.execute("INSERT INTO basket_probability_envelope VALUES ('KAAA','2026-10-02','P0','COMMITTED',?,?)",
                     (at, json.dumps({"native_cross_ensemble_artifact_sha256": art,
                                      "native_cross_activation_sha256": "act-" + art})))
    for art, ev in (("a-old", "e-two"), ("a-six", "e-six")):
        conn.execute("INSERT INTO calibration_cross_native_ensemble_artifact VALUES (?,?,?,?,?,?,?)",
                     ("id-" + art, "PEAK", "h12_24", "00Z", "stable_pool", ev, art))
        conn.execute("INSERT INTO calibration_native_ensemble_activation VALUES ('h', ?)", ("act-" + art,))
    conn.execute("INSERT INTO calibration_cross_native_ensemble_selection_evidence VALUES"
                 " ('e-six','{}','{}','{}','{}','2026-09-30','[]','[]','2026-10-01',6)")
    conn.execute("INSERT INTO calibration_cross_native_ensemble_selection_evidence VALUES"
                 " ('e-two','{}','{}','{}','{}','2026-09-30','[]','[]','2026-10-01',2)")
    seats = BR.served_p0_seats(conn, days=1)
    assert [(s["artifact_id"], s["selected_system_count"]) for s in seats] == [("id-a-six", 6)]


def test_the_slow_readings_are_kept_while_current_and_recomputed_when_due(tmp_path, monkeypatch) -> None:
    """2026-10-04: l4_lane read 5-16 minutes of every hour; it and the
    centre/shape gap now keep their file until their age falls due."""

    from datetime import datetime, timedelta, timezone

    from bayes_basket_target import board_readings as BR

    monkeypatch.setattr(BR, "READINGS_DIR", tmp_path)
    calls: list[str] = []
    monkeypatch.setitem(BR._READINGS, "l4_lane", (lambda conn: calls.append("l4") or {"x": 1}, (BR.l4_lane,)))
    monkeypatch.setitem(BR._READINGS, "seat_windows", (lambda conn: calls.append("seat") or {"seats": {}}, (BR.seat_window,)))
    db = tmp_path / "x.db"
    import sqlite3
    sqlite3.connect(db).close()
    now = datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(BR, "_now", lambda: now)
    BR.run(db, ("l4_lane", "seat_windows"))
    assert calls == ["l4", "seat"]
    now = now + timedelta(hours=1)
    receipts = BR.run(db, ("l4_lane", "seat_windows"))
    assert calls == ["l4", "seat", "seat"] and receipts[0] == "l4_lane kept"
    now = now + timedelta(hours=5)
    BR.run(db, ("l4_lane",))
    assert calls[-1] == "l4", "six hours on, the reading is due again"
    BR.run(db, ("l4_lane",), force=True)
    assert calls[-1] == "l4" and calls.count("l4") == 3
