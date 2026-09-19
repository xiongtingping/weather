"""The board's station view of the chain (user, 2026-09-19).

「前端的p0m、p1、metar、taf改为站的bayes更新视角，直观展示概率变化和信息增益、
数据变化等」「报文解析默认折叠」.  One card per station: P0 -> x TAF likelihood ->
P0-M -> x hard bound x joint METAR likelihood -> P1, bucket by bucket, with the
information each step added in bits.  These pin the arithmetic of the gains
and the page's structure.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

INTERVALS = json.dumps({"20": [19.5, 20.5], "21": [20.5, 21.5], "22": [21.5, 22.5]})
ORDINALS = json.dumps({"20": 0, "21": 1, "22": 2})


def test_kl_is_in_bits_and_the_zero_floor_is_not_information() -> None:
    assert C._kl_bits({"a": 1.0, "b": 0.0}, {"a": 0.5, "b": 0.5}) == pytest.approx(1.0)
    assert C._kl_bits({"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5}) == pytest.approx(0.0)
    # A posterior that holds the 0.1% floor where its prior held an exact
    # zero is a served lift, not an infinitely informative step.
    assert C._kl_bits({"a": 0.999, "b": 0.001}, {"a": 1.0, "b": 0.0}) == pytest.approx(0.0, abs=1e-6)


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "env.db")
    conn.execute(
        "CREATE TABLE basket_probability_envelope (checkpoint_id TEXT, stage TEXT,"
        " station_id TEXT, event_date TEXT, settlement_timezone TEXT,"
        " decision_at_utc TEXT, probabilities_json TEXT, bucket_ordinals_json TEXT,"
        " lineage_json TEXT, posterior_version TEXT, state TEXT)"
    )

    def put(cid, stage, at, probs, lineage, version="v"):
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cid, stage, "ZUUU", "2026-09-18", "Asia/Shanghai", at, json.dumps(probs),
             ORDINALS, json.dumps({"bucket_intervals_c_json": INTERVALS, **lineage}),
             version, "COMMITTED"),
        )

    put("p0", "P0", "2026-09-17T20:00:00Z", {"20": 0.3, "21": 0.4, "22": 0.3}, {})
    put("p0m", "P0-M", "2026-09-17T22:00:00Z", {"20": 0.3, "21": 0.4, "22": 0.3}, {
        "p0_root_checkpoint_id": "p0",
        "observed_hard_excluded_buckets": "NONE",
        "taf_observed_tx_c": "21.0",
        "p0m_taf_likelihood_json": json.dumps({"state": "IDENTITY_GATE_CLOSED"}),
    })
    receipt = {
        "state": "APPLIED", "alpha": 1.0, "rise_probability": 0.2,
        "running_high_c": 20.6, "running_high_bucket": "21",
        "evidence_by_bucket": {"20": 0.8, "21": 0.8, "22": 0.2},
        "features": {"remaining_hours": 5.0}, "fleet_mean_features": [],
    }
    for at, probs in (
        ("2026-09-18T05:00:00Z", {"20": 0.0, "21": 0.6, "22": 0.4}),
        ("2026-09-18T06:00:00Z", {"20": 0.0, "21": 0.8, "22": 0.2}),
    ):
        put(f"p1-{at}", "P1", at, probs, {
            "prediction_root_checkpoint_id": "p0m", "prediction_root_stage": "P0-M",
            "hard_excluded_buckets": "20",
            "metar_trajectory_running_high_c": "20.6",
            "p1_observation_likelihood_json": json.dumps(receipt),
        }, version="p1_joint_likelihood_v12")
    conn.commit()
    return conn


def test_the_card_splits_p1s_gain_into_the_bound_and_the_likelihood(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 18, 7, 0, tzinfo=C.timezone.utc))
    out = C.collect_station_bayes(_db(tmp_path))
    (card,) = out["stations"]
    gains = card["gain_bits"]
    # The hard bound kept 70% of the root: -log2(0.7).
    assert gains["fact"] == pytest.approx(-math.log2(0.7))
    assert gains["fact"] + gains["likelihood"] == pytest.approx(gains["p1_total"])
    assert gains["taf"] == pytest.approx(0.0, abs=1e-9), "a closed gate adds nothing"
    rows = {row["bucket"]: row for row in card["buckets"]}
    assert rows["20"]["excluded"] is True
    assert rows["22"]["g_alpha"] == pytest.approx(0.2)
    assert rows["21"]["p1"] == pytest.approx(0.8)
    # Two steps today; the second one's gain is measured against the first.
    assert [step["local"] for step in card["steps"]] == ["13:00", "14:00"]
    assert card["steps"][0]["gain_bits"] is None
    assert card["steps"][1]["gain_bits"] == pytest.approx(
        C._kl_bits({"21": 0.8, "22": 0.2}, {"21": 0.6, "22": 0.4})
    )
    assert card["shapes"]["p0"]["mean_c"] == pytest.approx(21.0)
    assert card["taf"]["tx_c"] == "21.0"


def test_the_page_is_one_station_view_with_the_reports_folded() -> None:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert '[stationBayesBoard,"站点贝叶斯更新"]' in source
    assert '"station_bayes": station_bayes,' in source
    # The two old report boards are gone; their reports live in the card.
    assert "function tafBoard()" not in source
    assert "function metarBoard()" not in source
    start = source.index("function stationBayesBoard(){")
    body = source[start:source.index("function p1JointBoard(){", start)]
    # Folded by default: the report section is a <details> with no ``open``.
    assert "<details><summary><b>报文解析</b>" in body
    assert '<details open><summary><b>报文解析</b>' not in body


def test_cards_cover_today_the_day_ahead_and_the_settled_days(tmp_path, monkeypatch) -> None:
    """User, 2026-09-19: 「增加20日（当天和提前一天的），历史数据自动下沉折叠」.

    Each station by its own local calendar: the day it observes, the day
    ahead (no P1 yet -- the card stops at P0-M, or P0), and settled days.
    """

    conn = _db(tmp_path)

    def put(cid, stage, station, day, tz, at, probs, lineage):
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cid, stage, station, day, tz, at, json.dumps(probs), ORDINALS,
             json.dumps({"bucket_intervals_c_json": INTERVALS, **lineage}), "v", "COMMITTED"),
        )

    flat = {"20": 0.2, "21": 0.5, "22": 0.3}
    # ZUUU's day ahead: P0 and P0-M in one cycle, stamped the same second.
    put("p0-19", "P0", "ZUUU", "2026-09-19", "Asia/Shanghai", "2026-09-18T06:00:00Z", flat, {})
    put("p0m-19", "P0-M", "ZUUU", "2026-09-19", "Asia/Shanghai", "2026-09-18T06:00:00Z", flat, {
        "p0_root_checkpoint_id": "p0-19", "observed_hard_excluded_buckets": "NONE",
        "p0m_taf_likelihood_json": json.dumps({"state": "IDENTITY_NO_TX"}),
    })
    # ZUUU's settled day before.
    put("p0-17", "P0", "ZUUU", "2026-09-17", "Asia/Shanghai", "2026-09-16T20:00:00Z", flat, {})
    put("p1-17", "P1", "ZUUU", "2026-09-17", "Asia/Shanghai", "2026-09-17T08:00:00Z",
        {"20": 0.0, "21": 1.0, "22": 0.0},
        {"prediction_root_checkpoint_id": "p0-17", "hard_excluded_buckets": "20"})
    # A station with only today's prior and nothing for tomorrow yet.
    put("nz-p0", "P0", "NZWN", "2026-09-18", "Pacific/Auckland", "2026-09-18T05:00:00Z", flat, {})
    conn.commit()
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 18, 7, 0, tzinfo=C.timezone.utc))
    out = C.collect_station_bayes(conn)
    order = [(e["role"], e["station_id"], e["event_date"], e["head_stage"]) for e in out["stations"]]
    assert order == [
        ("today", "NZWN", "2026-09-18", "P0"),
        ("today", "ZUUU", "2026-09-18", "P1"),
        ("ahead", "ZUUU", "2026-09-19", "P0-M"),
        ("history", "ZUUU", "2026-09-17", "P1"),
    ]
    assert out["missing_ahead"] == ["NZWN"]
    ahead = out["stations"][2]
    # No P1 yet: the P1 half is empty, not guessed.
    assert ahead["shapes"]["p1"] is None and ahead["metar"] is None and ahead["steps"] == []
    assert ahead["gain_bits"]["p1_total"] is None
    assert ahead["gain_bits"]["p0m_total"] == pytest.approx(0.0, abs=1e-9)
    assert all(row["p1"] is None for row in ahead["buckets"])
    assert out["stations"][0]["shapes"]["p0m"] is None
    assert out["stations"][3]["root_stage"] == "P0"
    # The fleet median reads today's P1s only.
    assert out["median_p1_gain_bits"] == pytest.approx(out["stations"][1]["gain_bits"]["p1_total"])


def test_the_settled_days_sink_to_the_bottom_folded() -> None:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    start = source.index("function stationBayesBoard(){")
    body = source[start:source.index("function p1JointBoard(){", start)]
    today, ahead = body.index('group("当天'), body.index('group("提前一天')
    history = body.index('<details class="by-hist">')
    assert today < ahead < history, "today, then the day ahead, then history last"
    assert '<details class="by-hist" open' not in body, "history is folded by default"
