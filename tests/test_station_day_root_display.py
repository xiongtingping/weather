"""The board must show the referenced root, even after a newer head arrives."""

import json
import sqlite3

import pytest

from bayes_basket_target.station_day_board import _p1_brief, _p1_root_center


def test_p1_centre_is_read_from_its_own_lineage():
    # P0-M and its brief are retired (charter 283); P1 reads its own model centre.
    head = {"lineage": {"model_center_c": "25.86"}}
    assert _p1_brief(head)["model_center_c"] == 25.86
    assert _p1_brief({"lineage": {}})["model_center_c"] is None


# A historical P0-M root stays readable (read-only history, charter 283).
@pytest.mark.parametrize("root_stage", ["P0", "P0-M"])
def test_root_lookup_does_not_use_a_newer_head(root_stage):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_probability_envelope ("
                 "checkpoint_id,content_sha256,station_id,event_date,stage,state,"
                 "decision_at_utc,lineage_json)")
    values = [
        ("root", "bound-hash", "ZUUU", "2026-10-08", root_stage, "COMMITTED",
         "2026-10-07T12:00:00Z", json.dumps({"model_center_c": "26.09"})),
        ("new-head", "new-hash", "ZUUU", "2026-10-08", root_stage, "COMMITTED",
         "2026-10-07T13:00:00Z", json.dumps({"model_center_c": "28.5"})),
    ]
    conn.executemany("INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?,?,?)", values)
    head = {"decided_at_utc": "2026-10-07T12:30:00Z", "lineage": {
        "prediction_root_stage": root_stage,
        "prediction_root_checkpoint_id": "root",
        "prediction_root_content_sha256": "bound-hash",
    }}
    assert _p1_root_center(conn, head, "ZUUU", "2026-10-08") == 26.09
    conn.execute("UPDATE basket_probability_envelope SET decision_at_utc='2026-10-07T12:00:00.844Z' WHERE checkpoint_id='root'")
    head["decided_at_utc"] = "2026-10-07T12:00:00.844006Z"
    assert _p1_root_center(conn, head, "ZUUU", "2026-10-08") == 26.09
    # A date/station/hash mismatch, unavailable lineage, future root, or
    # uncommitted root must not be disguised as the latest known centre.
    assert _p1_root_center(conn, head, "RKPK", "2026-10-08") is None
    assert _p1_root_center(conn, head, "ZUUU", "2026-10-09") is None
    head["lineage"]["prediction_root_content_sha256"] = "other-hash"
    assert _p1_root_center(conn, head, "ZUUU", "2026-10-08") is None
    head["lineage"]["prediction_root_content_sha256"] = "bound-hash"
    head["decided_at_utc"] = "2026-10-07T11:00:00Z"
    assert _p1_root_center(conn, head, "ZUUU", "2026-10-08") is None
    head["decided_at_utc"] = "2026-10-07T12:30:00Z"
    conn.execute("UPDATE basket_probability_envelope SET state='PENDING' WHERE checkpoint_id='root'")
    assert _p1_root_center(conn, head, "ZUUU", "2026-10-08") is None
    del head["lineage"]["prediction_root_checkpoint_id"]
    assert _p1_root_center(conn, head, "ZUUU", "2026-10-08") is None
    conn.close()
