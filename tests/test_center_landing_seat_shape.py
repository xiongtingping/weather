"""The landing panel's seat window is the one production ruled, as stored.

Until 2026-09-12 ``collect_center_landing`` hard-coded the mean bucket +-1
as "the seat window"; from then until 2026-10-02 it ran charter 131's seat
rule itself on every served cell.  User ruling 2026-10-02 「只读是读后端算出
来的」: the panel reads the audit the decision's compiled plan kept
(``basket_support_selection.seat_shape``) for the envelopes production
decided on, and a cell nobody decided on has no seat.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import calibration_dashboard as C  # noqa: E402

KEYS = ["26C", "27C", "28C", "29C", "30C", "31C"]


def _store() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE auto_trade_decisions (decision_id TEXT, details_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_envelope (checkpoint_id TEXT,"
        " station_id TEXT, event_date TEXT, decision_at_utc TEXT,"
        " bucket_ordinals_json TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?)",
        ("cp-1", "KAAA", "2026-09-20", "2026-09-19T06:00:00Z",
         json.dumps({key: index for index, key in enumerate(KEYS)})),
    )
    details = {
        "checkpoint_id": "cp-1",
        "stage_zero_execution_anchor": {"compiled_plan": {"plan": {
            "basket_support_selection": {"seat_shape": {
                "allowed_ordinals": [[3, 4, 5], [3, 4]],
                "centre_ordinal": 3, "sign": 1, "state": "RIGHT_SKEWED",
                "rule": "seat_shape_mean_anchored_v1",
            }},
        }}},
    }
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES (?,?)", ("d-1", json.dumps(details))
    )
    return conn


def _cell(decided: str) -> dict:
    probabilities = dict(zip(KEYS, (0.02, 0.08, 0.2, 0.4, 0.2, 0.1)))
    return {
        "station_id": "KAAA", "event_date": "2026-09-20",
        "decision_at_utc": decided, "probabilities": probabilities,
        "ordered_keys": KEYS,
        "key_centers_c": [26.0 + index for index in range(len(KEYS))],
        "settled_bucket": "30C", "excluded_buckets": [],
    }


def test_the_seat_is_the_widest_window_the_decision_stored() -> None:
    seats = C._stored_seat_shapes(_store())
    assert seats == {("KAAA", "2026-09-20", "2026-09-19T06:00:00Z"): {
        "state": "RIGHT_SKEWED", "sign": 1, "seat_keys": ["29C", "30C", "31C"],
    }}


def test_a_cell_reads_the_seat_of_its_own_envelope_and_no_other() -> None:
    seats = C._stored_seat_shapes(_store())
    truth = {("KAAA", "2026-09-20"): 29.6}
    decided = C._center_landing_entry("P0", _cell("2026-09-19T06:00:00Z"), truth, seats)
    assert decided["state"] == "RIGHT_SKEWED" and decided["sign"] == 1
    assert abs(decided["seat_claim"] - 0.7) < 1e-12 and decided["seat_hit"] == 1
    # Another envelope of the same day was never decided on: no seat.
    other = C._center_landing_entry("P0", _cell("2026-09-19T09:00:00Z"), truth, seats)
    assert other["state"] is None and other["seat_claim"] is None
    assert other["mode_claim"] is not None, "the mode window is plain arithmetic and stays"


def test_group_row_carries_the_three_window_accounts():
    members = [
        {
            "error": 0.5, "event_date": "2026-09-10", "sign": 1,
            "seat_claim": 0.6, "seat_hit": 1, "same_window": False,
            "mode_claim": 0.7, "mode_hit": 0,
            "legacy_claim": 0.65, "legacy_hit": 0,
        },
        {
            "error": -0.5, "event_date": "2026-09-11", "sign": 0,
            "seat_claim": None, "seat_hit": None, "same_window": None,
            "mode_claim": 0.5, "mode_hit": 1,
            "legacy_claim": 0.5, "legacy_hit": 1,
        },
    ]
    row = C._center_landing_group(members, tier="mid", label="中")
    assert row["cells"] == 2
    # Skew is the seat audit's, so it is read among the decided cells only.
    assert row["skewed_share"] == 1.0
    # The unseated cell lands in the bias columns but not the seat account.
    assert row["seat_cells"] == 1
    assert row["seat_cover"] == 1.0
    assert row["mode_cells"] == 2 and row["mode_cover"] == 0.5
    assert row["legacy_cells"] == 2 and row["legacy_cover"] == 0.5
    assert row["same_window_share"] == 0.0


def test_cache_salt_names_the_rule_constants_and_the_reader():
    salt = C._center_landing_salt()
    rule = C._center_landing_rule()
    assert rule["seat_shape_rule"] in salt
    assert str(rule["skew_threshold_sigma"]) in salt
    assert "def _stored_seat_shapes" in salt
