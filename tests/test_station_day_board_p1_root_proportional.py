"""The P1 card's root is the vector P1 multiplies: P0-M's proportional one (charter 241).

User 2026-10-26 「p0m的下界挪 +0.39°C，和p1重复计算了吧」: the move was counted once in
the chain, but the card showed P0-M's censored vector as P1's root, hiding P1's own move.
"""

from __future__ import annotations

import json
import sqlite3

from bayes_basket_target import station_day_board as B
from bayes_basket_target.observed_floor_censoring import OBSERVED_FLOOR_CENSORING_LINEAGE_KEY

AXIS = [{"bucket": k, "ordinal": i, "centre_c": 19.0 + i} for i, k in enumerate(["19", "20", "21"])]


def test_p1_reads_the_proportional_root():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_probability_envelope (checkpoint_id, content_sha256, station_id, event_date,"
                 " stage, state, probabilities_json, lineage_json, decision_at_utc)")
    censored = {"19": 0.0, "20": 0.8, "21": 0.2}
    proportional = {"19": 0.0, "20": 0.6, "21": 0.4}
    lineage = {OBSERVED_FLOOR_CENSORING_LINEAGE_KEY: json.dumps({"proportional_probabilities": proportional})}
    conn.execute("INSERT INTO basket_probability_envelope VALUES ('m','a'*1,'ZUUU','2026-10-26','P0-M','COMMITTED',?,?,'t')"
                 .replace("'a'*1", "'a'"), (json.dumps(censored), json.dumps(lineage)))
    head = {"lineage": {"prediction_root_checkpoint_id": "m", "prediction_root_content_sha256": "a",
                        "prediction_root_stage": "P0-M"}, "vector": {"19": 0.0, "20": 0.7, "21": 0.3}}
    move = B._stage_move(conn, head, "ZUUU", "2026-10-26", AXIS, "P1")
    assert move["root_proportional"] is True
    assert move["pre"]["mean_c"] == 20.4 and move["delta_c"] == -0.1
