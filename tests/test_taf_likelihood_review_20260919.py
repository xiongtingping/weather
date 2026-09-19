"""The 2026-09-19 review of the TAF likelihood (charters 150/158).

1. The walk-forwards were not strictly as-of: they trained on every event day
   before the scored one, including days whose truth had not been committed
   when the scored P0-M was decided.  Training now reads only station-days
   whose truth was committed by the decision, at the revision standing then.
2. The fitter read ``c0`` off the P0-M row, whose ``model_center_c`` is the
   P0-M's own served anchor once the likelihood applies; serving subtracts
   the P0 root's centre.  The fitter now reads the root row.
3. Past a best-bucket exponent of ~690 every bucket underflowed and the stage
   returned the identity; the likelihood is now rescaled by its best bucket
   there, so a far TAF keeps pulling instead of vanishing.
"""
from __future__ import annotations

import importlib.util
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bayes_basket_target import ssot
from bayes_basket_target import taf_likelihood as TL
from bayes_basket_target.contracts import ProbabilityEnvelope
from tests.test_charter_150_p1_joint_likelihood import _p0m, _root_with_centre, _taf_artifact

ROOT = Path(__file__).resolve().parents[1]
CENTRES = {"37": 37.0, "38": 38.0, "39": 39.0}


def _fitter():
    spec = importlib.util.spec_from_file_location(
        "_fit_taf_review_20260919", ROOT / "scripts" / "fit_taf_likelihood.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _tight(tau: float = TL.MIN_TAU_C) -> dict:
    body = _taf_artifact()
    body["pool"] = {"a": 0.0, "b": 0.5, "tau": tau, "n": 100}
    return body


# -- 3. underflow ---------------------------------------------------------


def test_an_ordinary_likelihood_is_bit_for_bit_what_it_was() -> None:
    likelihood, receipt = TL.compile_taf_likelihood(
        _taf_artifact(), station_id="ZUUU", tx_c=39.0, root_center_c=38.0,
        bucket_centers_c=CENTRES, buckets=list(CENTRES),
    )
    assert receipt["state"] == "APPLIED"
    assert "likelihood_rescaled_by_exponent" not in receipt
    for bucket, centre in CENTRES.items():
        exponent = (1.0 - 0.5 * (centre - 38.0)) ** 2 / 2.0
        assert likelihood[bucket] == math.exp(-1.0 * exponent)


def test_a_far_taf_is_rescaled_not_silenced() -> None:
    # tau at its floor: x = 12 puts every bucket's exponent past 700, where
    # exp() underflowed and the old code returned the identity.
    likelihood, receipt = TL.compile_taf_likelihood(
        _tight(), station_id="ZUUU", tx_c=50.0, root_center_c=38.0,
        bucket_centers_c=CENTRES, buckets=list(CENTRES),
    )
    assert receipt["state"] == "APPLIED"
    assert receipt["likelihood_rescaled_by_exponent"] > 690.0
    assert max(likelihood.values()) == 1.0
    assert likelihood["39"] == 1.0 and likelihood["37"] < likelihood["38"] < 1.0


def test_the_posterior_moves_monotonically_through_the_old_underflow_edge() -> None:
    root = _root_with_centre()
    top = []
    for tx in [40.0 + 0.5 * i for i in range(30)]:       # x from 2 to 16.5
        envelope = _p0m(root, _tight(), tx=tx)
        assert envelope.lineage["taf_revision_state"] == "APPLIED"
        top.append(envelope.probabilities["39"])
    # Once a TAF is warm enough to favour the top bucket it only ever
    # favours it more -- it never snaps back to P0's 0.2.
    assert all(later >= earlier - 1e-12 for earlier, later in zip(top, top[1:]))
    assert top[-1] > 0.99


def test_a_non_finite_tx_is_silent() -> None:
    _, receipt = TL.compile_taf_likelihood(
        _taf_artifact(), station_id="ZUUU", tx_c=float("nan"), root_center_c=38.0,
        bucket_centers_c=CENTRES, buckets=list(CENTRES),
    )
    assert receipt["state"] == "IDENTITY_NON_FINITE"


# -- 2. the fitter's centre is the root's ---------------------------------


def _ddl(script: str, table: str) -> str:
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", script, flags=re.DOTALL
    )
    assert match is not None, table
    return match.group(0)


def _insert_envelope(conn: sqlite3.Connection, envelope: ProbabilityEnvelope) -> None:
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"env-{envelope.stage}-{envelope.checkpoint_id}", envelope.stage,
            envelope.checkpoint_id, envelope.station_id, envelope.event_date,
            envelope.settlement_timezone, envelope.decision_at_utc,
            envelope.fresh_until_utc, json.dumps(dict(envelope.bucket_ordinals)),
            json.dumps(dict(envelope.probabilities)), envelope.posterior_version,
            envelope.posterior_sha256, envelope.bridge_sha256,
            json.dumps(dict(envelope.lineage)), envelope.content_sha256,
            "COMMITTED", envelope.decision_at_utc,
        ),
    )


def _insert_truth(
    conn: sqlite3.Connection, station: str, day: str, tmax: float,
    revision: int, committed: str,
) -> None:
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"t-{station}-{day}-{revision}", station, day, "Asia/Shanghai", tmax,
            "AWC", "official_daily_max_v1", revision, committed, committed,
            committed, 1, "ACCEPTED", "settlement", "a" * 64, "[]",
            "FORMAL_NATIVE_ELIGIBLE", "c" * 64,
        ),
    )


def test_the_fitter_reads_the_root_s_centre_not_the_p0m_anchor(tmp_path) -> None:
    root = _root_with_centre()
    applied = _p0m(root, _taf_artifact(), tx=39.0)
    # The P0-M row's anchor is its own posterior's centre -- the review's case.
    assert float(applied.lineage["model_center_c"]) == pytest.approx(38.1843, abs=1e-4)
    assert applied.lineage["p0_root_checkpoint_id"] == root.checkpoint_id
    conn = sqlite3.connect(tmp_path / "ssot.db")
    conn.execute(_ddl(ssot.PRODUCTION_TARGET_DDL, "basket_probability_envelope"))
    conn.execute(_ddl(ssot.DDL, "calibration_station_daily_high_truth"))
    _insert_envelope(conn, root)
    _insert_envelope(conn, applied)
    _insert_truth(conn, root.station_id, root.event_date, 39.0, 0, "2026-07-23T17:00:00Z")
    conn.commit()
    fitter = _fitter()
    (case,) = fitter.load_cases(conn, since="2026-07-01", through="2026-07-31")
    assert case["c0"] == 38.0
    assert case["tx_c"] == 39.0 and case["truth_bucket"] == "39"
    assert case["decided_at_utc"] == applied.decision_at_utc
    assert case["truth_revisions"][0][:3] == (
        datetime(2026, 7, 23, 17, tzinfo=timezone.utc).timestamp(), 0, 39.0
    )


# -- 1. strict as-of --------------------------------------------------------


def _case(day: int, station: str, tx: float, truth: float, *, revised: float | None = None) -> dict:
    centres = {str(k): float(k) for k in range(20, 29)}
    probabilities = {b: math.exp(-((c - 24.0) ** 2) / 2.0) for b, c in centres.items()}
    total = sum(probabilities.values())
    midnight = datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(days=day)
    labels = [((midnight + timedelta(days=1, hours=2)).timestamp(), 0, truth, str(int(truth)))]
    if revised is not None:
        labels.append(((midnight + timedelta(days=4)).timestamp(), 1, revised, str(int(revised))))
    return {
        "station_id": station, "event_date": (midnight.date()).isoformat(),
        # A first P0-M a day and a half out.
        "decided_at": (midnight - timedelta(hours=36)).timestamp(),
        "tx_c": tx, "c0": 24.0, "centers": centres,
        "probabilities": {b: v / total for b, v in probabilities.items()},
        "root_mean_c": 24.0, "cloud_cover": 0.2, "precip_prob": 0.0,
        "group": TL.disagreement_group(tx - 24.0, 0.2, 0.0), "weight": 1.0,
        "truth_c": labels[-1][2], "truth_bucket": labels[-1][3],
        "truth_revisions": labels,
    }


def _cases() -> list[dict]:
    cases = []
    for day in range(20):
        for i, (tx, truth) in enumerate(((26.0, 25.0), (22.0, 23.0), (24.0, 24.0))):
            cases.append(_case(
                day, f"S{i}", tx, truth,
                revised=(truth + 1.0) if (day % 5 == 0 and i == 0) else None,
            ))
    return cases


def _record_training(monkeypatch, fitter) -> list[tuple[float, list[dict]]]:
    seen: list[tuple[float, list[dict]]] = []
    original = fitter.AsOf.training

    def training(self, at):
        rows = original(self, at)
        seen.append((at, rows))
        return rows

    monkeypatch.setattr(fitter.AsOf, "training", training)
    return seen


def _assert_as_of(seen: list[tuple[float, list[dict]]]) -> None:
    assert seen
    for at, rows in seen:
        for row in rows:
            assert row["decided_at"] <= at
            standing = [l for l in row["truth_revisions"] if l[0] <= at]
            assert standing, "a row trained before any of its truth was committed"
            label = max(standing, key=lambda l: l[1])
            assert (row["truth_c"], row["truth_bucket"]) == (label[2], label[3])


def test_the_line_walk_forward_trains_only_on_truth_committed_by_the_decision(monkeypatch) -> None:
    fitter = _fitter()
    cases = _cases()
    seen = _record_training(monkeypatch, fitter)
    gate = fitter.walk_forward(cases)
    _assert_as_of(seen)
    assert gate["training_cut"] == fitter.TRAINING_CUT
    # Decided 36h before its day, a P0-M has only the truth of days that
    # closed two or more days earlier: the event-date cut read the two
    # days before as well.
    for at, rows in seen:
        latest = max((r["event_date"] for r in rows), default=None)
        if latest is None:
            continue
        decided_for = datetime.fromtimestamp(at + 36 * 3600, timezone.utc).date()
        assert latest <= (decided_for - timedelta(days=3)).isoformat()
    # A revision committed later is not read before it existed.
    revised = [row for at, rows in seen for row in rows if len(row["truth_revisions"]) == 2]
    assert {row["truth_c"] for row in revised} == {25.0, 26.0}


def test_the_tempering_walk_forward_is_as_of_too(monkeypatch) -> None:
    fitter = _fitter()
    cases = _cases()
    seen = _record_training(monkeypatch, fitter)
    gate = fitter.walk_forward_tempering(cases, cases)
    _assert_as_of(seen)
    assert gate["training_cut"] == fitter.TRAINING_CUT


def test_a_case_decided_after_its_own_truth_is_not_scored() -> None:
    fitter = _fitter()
    cases = _cases()
    late = dict(cases[-1])
    late["decided_at"] = late["truth_revisions"][0][0] + 60.0
    gate = fitter.walk_forward(cases[:-1] + [late])
    assert gate["decided_after_own_truth_unscored"] == 1


def test_clock_strings_are_compared_as_instants() -> None:
    fitter = _fitter()
    assert fitter._epoch("2026-09-18T19:10:09.000001Z") > fitter._epoch("2026-09-18T19:10:09Z")
