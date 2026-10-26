"""Charter 282: P1 moved by the latest TAF TX revision, in a narrow window only.

User ruling 2026-10-26 「按方案上线（默认影子、每日拟合过 240 门才服务），升版重启」.
A P1 decided within three hours of a TX revision's issue, between 10:00 and 16:00
local and after the bulletin arrived, is moved by kappa x step; serving needs the
daily fit's walk-forward to pass charter 240, otherwise the receipt records the
move and the vector is untouched.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from bayes_basket_target import tx_revision_shift as TXR  # noqa: E402
from bayes_basket_target.p1_compiler import compile_station_p1_packets  # noqa: E402
from bayes_basket_target.probability_packet import build_probability_envelope  # noqa: E402
from bayes_basket_target.p1_compiler import materialization_packet  # noqa: E402
from tests.test_charter_150_p1_joint_likelihood import (  # noqa: E402
    _envelope, _observation, _p1_root, _registry,
)

TZ = "America/New_York"
DAY = "2026-07-23"
BULLETINS = [
    {"issued_at_utc": "2026-07-23T05:00:00Z", "received_at_utc": "2026-07-23T05:50:00Z", "tx_c": 37.0,
     "revision_sequence": 1, "revision_id": "taf-revision-a"},
    {"issued_at_utc": "2026-07-23T13:00:00Z", "received_at_utc": "2026-07-23T13:50:00Z", "tx_c": 39.0,
     "revision_sequence": 2, "revision_id": "taf-revision-b"},
]


def _fit(verdict="PASS", kappa=0.2):
    return {"kappa": kappa, "verdict": verdict, "fitted_at_utc": "2026-07-23T00:00:00Z", "content_sha256": "0" * 64}


def _bundle(at, fit=None, bulletins=BULLETINS):
    return TXR.step_bundle(bulletins, decision_at_utc=at, event_date=DAY, settlement_timezone=TZ,
                           fit=_fit() if fit is None else fit)


def test_the_revision_is_known_only_once_received():
    assert TXR.latest_revision(BULLETINS, decision_at_utc="2026-07-23T13:30:00Z", event_date=DAY,
                               settlement_timezone=TZ) is None
    rev = TXR.latest_revision(BULLETINS, decision_at_utc="2026-07-23T14:00:00Z", event_date=DAY,
                              settlement_timezone=TZ)
    assert rev["step_c"] == 2.0 and rev["previous_tx_c"] == 37.0 and rev["hours_since_issue"] == 1.0


def test_a_restated_bulletin_is_replaced_by_its_restatement():
    restated = BULLETINS + [{"issued_at_utc": "2026-07-23T13:20:00Z", "received_at_utc": "2026-07-23T13:55:00Z",
                             "tx_c": 37.0, "revision_sequence": 3, "revision_id": "taf-revision-b~r001"}]
    assert TXR.latest_revision(restated, decision_at_utc="2026-07-23T14:00:00Z", event_date=DAY,
                               settlement_timezone=TZ) is None


@pytest.mark.parametrize("at, reason", [
    ("2026-07-23T17:30:00Z", "OUTSIDE_WINDOW"),   # 4.5 h after issue
    ("2026-07-23T20:30:00Z", "OUTSIDE_WINDOW"),   # 16:30 local
    ("2026-07-23T13:30:00Z", "NO_REVISION"),      # issued, not yet received
])
def test_outside_the_window_the_vector_is_untouched(at, reason):
    probs = {"37": 0.2, "38": 0.6, "39": 0.2}
    out, receipt = TXR.apply(probs, bucket_ordinals={"37": 0, "38": 1, "39": 2},
                             intervals_json='{"37":[36.5,37.5],"38":[37.5,38.5],"39":[38.5,39.5]}',
                             bundle=_bundle(at), mode="serve")
    assert out == probs and receipt["state"] == "IDLE" and receipt["reason"] == reason


def test_the_move_serves_only_when_the_fit_passes():
    probs = {"37": 0.2, "38": 0.6, "39": 0.2}
    args = dict(bucket_ordinals={"37": 0, "38": 1, "39": 2},
                intervals_json='{"37":[36.5,37.5],"38":[37.5,38.5],"39":[38.5,39.5]}')
    moved, served = TXR.apply(probs, bundle=_bundle("2026-07-23T15:00:00Z"), mode="serve", **args)
    assert served["state"] == "SERVED" and served["delta_c"] == pytest.approx(0.4)
    assert moved["39"] > probs["39"] and moved["37"] < probs["37"]
    assert sum(moved.values()) == pytest.approx(1.0)
    same, shadow = TXR.apply(probs, bundle=_bundle("2026-07-23T15:00:00Z", fit=_fit("REFUSED")), mode="serve", **args)
    assert same == probs and shadow["state"] == "SHADOW" and shadow["reason"] == "GATE_NOT_PASSED"
    assert shadow["shifted_probabilities"] == moved
    same, forced = TXR.apply(probs, bundle=_bundle("2026-07-23T15:00:00Z"), mode="shadow", **args)
    assert same == probs and forced["reason"] == "MODE_SHADOW"


def test_a_ruled_out_bucket_stays_at_zero():
    geom = TXR.geometry(["37", "38", "39"], {"37": [36.5, 37.5], "38": [37.5, 38.5], "39": [38.5, 39.5]})
    out = TXR.shift_vector([0.0, 0.7, 0.3], geom, -0.6)
    assert out[0] == 0.0 and sum(out) == pytest.approx(1.0)


def _compile(at, **kw):
    root = _p1_root(_envelope())
    packets = compile_station_p1_packets(
        predecessor=root, observations=[_observation(root, "a", at, 36.8)],
        registry_rows=_registry(root), compilation_at_utc=at, **kw,
    )
    return root, packets


def test_the_p1_compile_moves_after_the_bound_and_rides_the_packet():
    root, packets = _compile("2026-07-23T15:00:00Z", tx_bulletins=BULLETINS, tx_revision_fit=_fit())
    envelope = packets[-1]["expected_envelope"]
    receipt = json.loads(envelope["lineage"][TXR.LINEAGE_KEY])
    assert receipt["state"] == "SERVED"
    assert envelope["probabilities"]["39"] > receipt["pre_shift_probabilities"]["39"]
    assert envelope["posterior_version"] == "p1_joint_likelihood_v49"
    # The strict materializer rebuilds the same envelope from the packet alone.
    packet = {**packets[-1], "expected_envelope": envelope}
    rebuilt = build_probability_envelope(materialization_packet(packet))
    assert rebuilt.probabilities == envelope["probabilities"]
    assert rebuilt.to_dict() == envelope


def test_without_bulletins_the_p1_is_what_it_was():
    _, with_none = _compile("2026-07-23T15:00:00Z")
    envelope = with_none[-1]["expected_envelope"]
    assert TXR.LINEAGE_KEY not in envelope["lineage"]
    assert with_none[-1]["tx_revision_shift"] is None
    _, idle = _compile("2026-07-23T15:00:00Z", tx_bulletins=[], tx_revision_fit=_fit())
    assert idle[-1]["expected_envelope"]["probabilities"] == envelope["probabilities"]


def test_bulletins_are_read_by_arrival_and_a_failed_read_is_none():
    conn = sqlite3.connect(":memory:")
    assert TXR.load_bulletins(conn, station_id="ZUUU", event_date=DAY, as_of_utc="2026-07-23T15:00:00Z") == []
    conn.execute("CREATE TABLE calibration_forward_taf_capture (station_id, event_date, issued_at_utc,"
                 " received_at_utc, tx_c, revision_sequence, revision_id)")
    for b in BULLETINS:
        conn.execute("INSERT INTO calibration_forward_taf_capture VALUES (?,?,?,?,?,?,?)",
                     ("ZUUU", DAY, b["issued_at_utc"], b["received_at_utc"], b["tx_c"], b["revision_sequence"],
                      b["revision_id"]))
    got = TXR.load_bulletins(conn, station_id="ZUUU", event_date=DAY, as_of_utc="2026-07-23T13:30:00Z")
    assert [b["tx_c"] for b in got] == [37.0]
    conn.execute("DROP TABLE calibration_forward_taf_capture")
    conn.execute("CREATE TABLE calibration_forward_taf_capture (station_id)")
    assert TXR.load_bulletins(conn, station_id="ZUUU", event_date=DAY, as_of_utc="2026-07-23T15:00:00Z") is None


def test_the_fit_scores_what_serving_serves():
    import fit_tx_revision_shift as FIT  # noqa: PLC0415

    record = {"vector": [0.2, 0.6, 0.2], "ordered": ["37", "38", "39"], "step_c": 2.0, "winner": 2,
              "geom": TXR.geometry(["37", "38", "39"], {"37": [36.5, 37.5], "38": [37.5, 38.5], "39": [38.5, 39.5]})}
    probs = {"37": 0.2, "38": 0.6, "39": 0.2}
    moved, _ = TXR.apply(probs, bucket_ordinals={"37": 0, "38": 1, "39": 2},
                         intervals_json='{"37":[36.5,37.5],"38":[37.5,38.5],"39":[38.5,39.5]}',
                         bundle=_bundle("2026-07-23T15:00:00Z"), mode="serve")
    import math  # noqa: PLC0415
    assert FIT.score(record, 0.2)["logscore"] == pytest.approx(-math.log(moved["39"]))


def test_the_daily_fit_is_a_managed_read_only_sweep():
    daemon = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    assert re.search(r'"fit_tx_revision_shift",\s*"scripts/fit_tx_revision_shift_sweep\.sh",\s*86400\.0', daemon)
    assert '"fit_tx_revision_shift": "none"' in daemon
    sweep = (ROOT / "scripts" / "fit_tx_revision_shift_sweep.sh").read_text(encoding="utf-8")
    assert "single_instance fit_tx_revision_shift" in sweep and "scripts/fit_tx_revision_shift.py" in sweep
    source = (ROOT / "scripts" / "fit_tx_revision_shift.py").read_text(encoding="utf-8")
    assert "mode=ro" in source
    assert not re.search(r"INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM", source, re.I)
