"""A poll's receipt is computed once per distinct poll row (2026-09-19).

5,590 reports against 2,829 polls in a three-day identity window: the same
provider payload was canonically hashed once per report it carried.  The memo
must return exactly what the uncached function returns, and a changed row is a
new key.
"""
from __future__ import annotations

import json

import bayes_basket_target.canonical_observation_export as E


def _raw(**over):
    raw = {
        "source_poll_id": "poll-1", "poll_source_lane": "awc",
        "poll_request_started_at": "2026-09-19T03:00:00Z",
        "poll_response_started_at": "2026-09-19T03:00:00.500Z",
        "poll_response_completed_at": "2026-09-19T03:00:01Z",
        "poll_response_status": 200, "poll_duration_ms": 1000.0,
        "poll_report_count": 2, "poll_new_report_count": 1, "poll_error_code": None,
        "poll_station_ids_json": json.dumps(["ZUUU", "KLAX"]),
        "poll_json": json.dumps({"reports": [{"id": 1}]}),
    }
    raw.update(over)
    return raw


def test_the_memo_returns_exactly_the_uncached_receipt() -> None:
    E._POLL_RECEIPT_MEMO.clear()
    raw = _raw()
    first = E._source_poll_receipt(raw, expected_lane="awc")
    assert first == E._source_poll_receipt_uncached(raw, expected_lane="awc")
    assert E._source_poll_receipt(dict(raw), expected_lane="awc") == first
    assert len(E._POLL_RECEIPT_MEMO) == 1


def test_a_changed_row_is_a_new_key() -> None:
    E._POLL_RECEIPT_MEMO.clear()
    a = E._source_poll_receipt(_raw(), expected_lane="awc")
    b = E._source_poll_receipt(_raw(poll_json=json.dumps({"reports": [{"id": 2}]})), expected_lane="awc")
    c = E._source_poll_receipt(_raw(poll_new_report_count=2), expected_lane="awc")
    assert len({a, b, c}) == 3 and len(E._POLL_RECEIPT_MEMO) == 3
