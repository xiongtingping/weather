"""User ruling 2026-09-23 「以最新的为准」: a TAF revision's newest capture is the revision.

Revision ids hash the bulletin text, so two captures of one revision are the
same text with other metadata -- a later receipt, a sequence after the inbox
chain was renumbered, other decoded fields.  Committed first and then seen
again different, the conflict used to raise and hold every station's P0-M.
Now the newer capture is appended as a restatement (``<revision>~r001``...)
and every reader of the fact table takes a revision's newest restatement.
"""
from __future__ import annotations

import copy

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.native_ensemble_p0 import _taf_envelope_witness
from bayes_basket_target.native_ensemble_training_facts import (
    TAF_RESTATEMENT_CAP,
    ingest_forward_taf_inbox,
    newest_forward_taf_captures,
    taf_original_revision_id,
    taf_restatement_is_current,
)
from tests.test_forward_taf_ingest_cost import _capture, _conn, _inbox

DAY = "2026-08-20"
DECISION = "2026-08-20T00:00:00Z"


def _received(capture: dict, received_at: str, *, cloud: float | None = None) -> dict:
    out = copy.deepcopy(capture)
    out.pop("content_sha256")
    out["forecast_revision"]["received_at_utc"] = received_at
    if cloud is not None:
        out["taf_fields"]["cloud_cover_fraction"] = cloud
    return {**out, "content_sha256": content_sha256(out)}


def _facts(conn) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT revision_id, received_at_utc FROM calibration_forward_taf_capture"
        " WHERE station_id='ZUUU' ORDER BY revision_id"
    ).fetchall()


def test_restatement_ids_order_and_resolve() -> None:
    assert taf_original_revision_id("taf-a~r002") == "taf-a"
    assert taf_original_revision_id("taf-a") == "taf-a"
    ids = ["taf-a", "taf-a~r001", "taf-a~r002", "taf-b"]
    assert [i for i in ids if taf_restatement_is_current(i, ids)] == ["taf-a~r002", "taf-b"]


def test_a_changed_capture_is_restated_not_refused_and_the_witness_reads_it() -> None:
    conn = _conn()
    first = _capture("ZUUU", DAY, 1)
    assert ingest_forward_taf_inbox(conn, _inbox([first]))["wrote_count"] == 1

    later = _received(first, "2026-08-19T09:40:00Z", cloud=0.75)
    receipt = ingest_forward_taf_inbox(conn, _inbox([later]))
    assert receipt["wrote_count"] == 1 and receipt["rejected_count"] == 0
    assert _facts(conn) == [
        ("taf-zuuu-2026-08-20-1", "2026-08-19T09:05:00Z"),
        ("taf-zuuu-2026-08-20-1~r001", "2026-08-19T09:40:00Z"),
    ]
    witness = _taf_envelope_witness(
        conn, station_id="ZUUU", event_date=DAY, decision_at_utc=DECISION,
    )
    assert witness["forecast_revision_id"] == "taf-zuuu-2026-08-20-1~r001"
    assert witness["cloud_cover_fraction"] == 0.75
    # Before the restatement was received, the original is what was known.
    early = _taf_envelope_witness(
        conn, station_id="ZUUU", event_date=DAY, decision_at_utc="2026-08-19T09:10:00Z",
    )
    assert early["forecast_revision_id"] == "taf-zuuu-2026-08-20-1"

    # The same newest capture again writes nothing ...
    assert ingest_forward_taf_inbox(conn, _inbox([later]))["wrote_count"] == 0
    # ... and the inbox going back to the first content is newest again.
    assert ingest_forward_taf_inbox(conn, _inbox([first]))["wrote_count"] == 1
    assert _facts(conn)[-1][0] == "taf-zuuu-2026-08-20-1~r002"


def test_within_one_inbox_the_newer_capture_is_the_revision() -> None:
    conn = _conn()
    first = _capture("ZUUU", DAY, 1)
    later = _received(first, "2026-08-19T09:40:00Z", cloud=0.75)
    assert newest_forward_taf_captures([later, first]) == {0}
    receipt = ingest_forward_taf_inbox(conn, _inbox([later, first]))
    assert receipt["wrote_count"] == 1
    assert [r["reason"] for r in receipt["rejected_captures"]] == [
        "forward_taf_capture_superseded_in_inbox"
    ]
    assert _facts(conn) == [("taf-zuuu-2026-08-20-1", "2026-08-19T09:40:00Z")]


def test_a_flapping_revision_is_capped_and_refused_alone() -> None:
    conn = _conn()
    base = _capture("ZUUU", DAY, 1)
    other = _capture("ZSPD", DAY, 1)
    ingest_forward_taf_inbox(conn, _inbox([base, other]))
    for minute in range(TAF_RESTATEMENT_CAP):
        variant = _received(base, f"2026-08-19T10:{minute:02d}:00Z")
        assert ingest_forward_taf_inbox(conn, _inbox([variant, other]))["wrote_count"] == 1
    flapped = _received(base, "2026-08-19T11:00:00Z")
    receipt = ingest_forward_taf_inbox(conn, _inbox([flapped, other]))
    assert receipt["wrote_count"] == 0
    assert [r["reason"] for r in receipt["rejected_captures"]] == [
        "forward_taf_capture_restatement_cap"
    ]
