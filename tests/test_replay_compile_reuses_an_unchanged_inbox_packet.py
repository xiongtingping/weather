"""The cycle's replay recompiles a scope only when what it answers to moved.

Every active official packet was recompiled every cycle -- 34s at the median
and 22% of a cycle on 2026-09-15 -- and almost every compile reproduced the
packet already in the inbox: same forward snapshot, same activation chain.
A packet is reused while its snapshot and the station's newest activation
row are unchanged and it is younger than NATIVE_P0_REPLAY_RECOMPILE_SEC; a
new arrival is compiled at once by the forecast lane's drain, and any change
in what the compile reads is a fresh fingerprint.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3

import bayes_basket_target.production_worker as pw

FP = {"forward_snapshot_content_sha256": "a" * 64, "station_activation_rowid": 7}


def _row(*, fp=FP, decided="2026-09-15T08:00:00Z") -> dict:
    return {
        "source_class": "calibrated_native_cross_ensemble",
        "replay_fingerprint": dict(fp),
        "packet": {
            "station_id": "KMIA", "event_date": "2026-09-16",
            "native_probability_envelope": {"decision_at_utc": decided},
        },
    }


def test_a_fresh_row_with_the_same_fingerprint_is_reused() -> None:
    assert pw._replay_compile_reusable(_row(), FP, now_utc="2026-09-15T08:10:00Z")


def test_a_changed_snapshot_or_activation_is_compiled_again() -> None:
    other_sha = {**FP, "forward_snapshot_content_sha256": "b" * 64}
    other_act = {**FP, "station_activation_rowid": 8}
    assert not pw._replay_compile_reusable(_row(), other_sha, now_utc="2026-09-15T08:10:00Z")
    assert not pw._replay_compile_reusable(_row(), other_act, now_utc="2026-09-15T08:10:00Z")


def test_an_old_row_a_missing_fingerprint_or_no_row_is_compiled_again() -> None:
    late = "2026-09-15T08:00:00Z"
    assert not pw._replay_compile_reusable(
        _row(decided=late), FP,
        now_utc="2026-09-15T08:31:00Z",
    )
    assert not pw._replay_compile_reusable(
        _row(decided=late), FP, now_utc="2026-09-15T07:59:00Z",   # clock went back
    )
    unstamped = _row(); del unstamped["replay_fingerprint"]
    assert not pw._replay_compile_reusable(unstamped, FP, now_utc="2026-09-15T08:10:00Z")
    assert not pw._replay_compile_reusable(None, FP, now_utc="2026-09-15T08:10:00Z")
    assert not pw._replay_compile_reusable(_row(), None, now_utc="2026-09-15T08:10:00Z")


def test_the_fingerprint_is_the_snapshot_and_the_stations_newest_activation() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE calibration_native_ensemble_activation(station_id TEXT, artifact_id TEXT)")
    conn.executemany(
        "INSERT INTO calibration_native_ensemble_activation VALUES (?,?)",
        [("KMIA", "a1"), ("EGLC", "a2"), ("KMIA", "a3")],
    )
    packet = {"snapshot_content_sha256": "c" * 64}
    assert pw._replay_compile_fingerprint(conn, station_id="KMIA", forward_packet=packet) == {
        "forward_snapshot_content_sha256": "c" * 64, "station_activation_rowid": 3,
    }
    conn.execute("INSERT INTO calibration_native_ensemble_activation VALUES ('KMIA','a4')")
    assert pw._replay_compile_fingerprint(conn, station_id="KMIA", forward_packet=packet)["station_activation_rowid"] == 4
    assert pw._replay_compile_fingerprint(conn, station_id="KMIA", forward_packet={}) is None
    assert pw._replay_compile_fingerprint(None, station_id="KMIA", forward_packet=packet) is None


def test_the_inbox_carries_the_fingerprint_of_a_compiled_packet(tmp_path) -> None:
    path = tmp_path / "p0_inbox.json"
    packet = {
        "stage": "P0", "station_id": "KMIA", "event_date": "2026-09-16",
        "native_probability_envelope": {
            "decision_at_utc": "2026-09-15T08:00:00Z",
            "fresh_until_utc": "2026-09-16T08:00:00Z",
            "lineage": {"native_transport_provider": "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT"},
        },
    }
    compiled = {
        "schema": "calibrated_native_cross_ensemble_p0_packet_v1",
        "source_class": "calibrated_native_cross_ensemble",
        "packet_content_sha256": "d" * 64,
        "packet": packet,
        "replay_fingerprint": dict(FP),
    }
    pw._upsert_native_p0_inbox(path, compiled_packets=[compiled], generated_at_utc="2026-09-15T08:00:00Z")
    rows = pw._p0_inbox_rows_by_scope(path)
    assert rows[("KMIA", "2026-09-16")]["replay_fingerprint"] == FP
    # Unstamped compiles (the forecast lane's drain) carry no fingerprint and
    # are compiled once more by the cycle, which then stamps them.
    del compiled["replay_fingerprint"]
    compiled["packet"] = {**packet, "native_probability_envelope": {**packet["native_probability_envelope"], "decision_at_utc": "2026-09-15T08:05:00Z"}}
    pw._upsert_native_p0_inbox(path, compiled_packets=[compiled], generated_at_utc="2026-09-15T08:05:00Z")
    assert "replay_fingerprint" not in pw._p0_inbox_rows_by_scope(path)[("KMIA", "2026-09-16")]


def test_the_replay_asks_before_it_compiles_and_stamps_what_it_compiled() -> None:
    tree = ast.parse(inspect.getsource(pw))
    loop = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.For) and ast.unparse(node.target) == "replay_packet"
    )
    body = ast.unparse(loop)
    reuse_at = body.index("_replay_compile_reusable(")
    compile_at = body.index("_compile_native_p0_with_republisher_fallback(")
    stamp_at = body.index("compiled['replay_fingerprint'] = replay_fingerprint")
    assert reuse_at < compile_at < stamp_at
