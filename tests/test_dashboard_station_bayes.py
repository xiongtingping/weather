"""The board's station-day view of the chain (user, 2026-09-19 / 2026-09-20).

「前端的p0m、p1、metar、taf改为站的bayes更新视角，直观展示概率变化和信息增益、
数据变化等」「报文解析默认折叠」, then 「把taf、区域模型、metar等站点贝叶斯更新重新
设计到一起，直观展示变化，信息不要分散，以日期、站点为视角，串联在一起」「表格形式」.

One ROW per station-day, the chain across its columns: P0 -> x TAF likelihood
-> P0-M -> x hard bound x joint METAR likelihood (the regional model a
covariate inside it) -> P1 -> the settled high.  These pin the arithmetic of
the gains, what a step is, and the page's structure.
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


def test_the_page_is_one_station_day_table_with_the_reports_folded() -> None:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert '[stationBayesBoard,"站点 × 日期"]' in source
    assert '"station_bayes": station_bayes,' in source
    # The two old report boards are gone; their reports live in the row.
    assert "function tafBoard()" not in source
    assert "function metarBoard()" not in source
    start = source.index("function stationBayesBoard(){")
    body = source[start:source.index("function p1JointBoard(){", start)]
    # Folded by default: the report section is a <details> with no ``open``.
    assert "<details><summary><b>报文解析</b>" in body
    assert '<details open><summary><b>报文解析</b>' not in body


def test_taf_the_regional_model_and_the_path_skill_are_joined_onto_the_row() -> None:
    """User, 2026-09-20: 「信息不要分散」.

    What used to be read in three other sections -- the TAF's own reports,
    the regional model's run age and bias, this station's hit rate over the
    last three weeks -- is joined onto the station-day it belongs to.  And
    each of those joins is by the day, not by the station alone: a TAF
    issued this morning reaches tomorrow as well, and yesterday's row must
    not wear today's reports.
    """

    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    start = source.index("function stationBayesBoard(){")
    body = source[start:source.index("function p1JointBoard(){", start)]
    for join in ("D.taf_board", "D.metar_board", "D.regional", "D.p1_path_skill"):
        assert join in body, f"{join} is not joined onto the row"
    assert "r.event_date === e.event_date" in body, "the TAF is filtered to the row's day"
    assert "metS.event_date === e.event_date" in body, "the METAR is filtered to the row's day"


def test_the_row_carries_the_chain_in_the_order_it_runs() -> None:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    start = source.index("function stationBayesBoard(){")
    body = source[start:source.index("function p1JointBoard(){", start)]
    head = body[body.index("<tr class=\"sb-head\">"):body.index("<tr class=\"sb-det\"")]
    order = [head.index(token) for token in (
        "sbDistStrip(e)", "shape(S.p0,", "${tafCell}", "shape(S.p0m,",
        "${liveCell}", "${regCell}", "shape(S.p1,", "${settleCell}")]
    assert order == sorted(order), "the columns run P0 -> TAF -> P0-M -> 实况/区域 -> P1 -> 结算"


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


def test_the_board_opens_on_today_and_the_other_days_are_one_chip_away() -> None:
    """User, 2026-09-19: 「历史数据自动下沉折叠」, kept in the table (09-20).

    180 station-days is a list nobody scans, so the chips filter the table
    and the board lands on today.  Each row is its own tbody -- the summary
    row and the detail row under it -- so filtering and reordering keep the
    two together, and the column sorter is opted out for the same reason.
    """

    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    start = source.index("function stationBayesBoard(){")
    body = source[start:source.index("function p1JointBoard(){", start)]
    chips = body[body.index("const chips = "):]
    chips = chips[:chips.index("return `<h2>")]
    assert chips.index('["today"') < chips.index('["ahead"') < chips.index('["history"') \
        < chips.index('["all"'), "the chips run today, tomorrow, settled, all"
    assert 'i === 0 ? ` data-active="1"` : ""' in chips, "the first chip -- today -- is the landing one"
    assert 'let day = "today";' in (ROOT / "scripts" / "calibration_dashboard.py").read_text(
        encoding="utf-8"), "and the filter starts there too"
    assert 'class="sbt" data-grouped="1"' in body, "the column sorter would split a row from its detail"
    assert '<tbody class="sb-row"' in body and '<tr class="sb-det" hidden>' in body
    # The detail is what the row opens into, so it must not start open.
    assert '<tr class="sb-det">' not in body


def test_the_details_are_hidden_until_a_row_is_opened() -> None:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert 'document.querySelectorAll("[data-sbt]")' in source, "the row opener is wired"
    assert 'ev.target.closest("tr.sb-head")' in source

def test_a_decision_seconds_replay_of_its_own_prefix_is_not_a_step(tmp_path, monkeypatch) -> None:
    """One instant holds the whole day walked again; only its last envelope
    was ever served.

    Production replays the day's prefix inside one decision second, so that
    second carries an envelope per earlier report -- the running high
    climbing 13 -> 14 -> 16 -> 18 with no time passing.  Drawn as steps
    those are vertical spikes back to the morning across the afternoon, and
    counted as steps they are information gains nobody was ever shown.  The
    rule is the one collect_p1_path_skill already applies to its heads: the
    last envelope of a second is the step, the rest are the replay.
    """

    conn = _db(tmp_path)
    receipt = {"state": "SILENT_PREFIX_NOT_CURRENT", "running_high_c": 20.6,
               "features": {"drawdown_from_high_c": 0.6, "regional_headroom_c": 1.4}}
    for suffix, probs in ((".100000", {"20": 0.5, "21": 0.5, "22": 0.0}),
                          (".200000", {"20": 0.2, "21": 0.7, "22": 0.1}),
                          (".300000", {"20": 0.0, "21": 0.9, "22": 0.1})):
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"replay{suffix}", "P1", "ZUUU", "2026-09-18", "Asia/Shanghai",
             f"2026-09-18T07:00:00{suffix}Z", json.dumps(probs), ORDINALS,
             json.dumps({"bucket_intervals_c_json": INTERVALS,
                         "prediction_root_checkpoint_id": "p0m",
                         "hard_excluded_buckets": "20",
                         "metar_trajectory_running_high_c": "20.6",
                         "p1_observation_likelihood_json": json.dumps(receipt)}),
             "p1_joint_likelihood_v12", "COMMITTED"),
        )
    conn.commit()
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 18, 7, 0, tzinfo=C.timezone.utc))
    (card,) = C.collect_station_bayes(conn)["stations"]
    # Three seconds, three steps: the three envelopes stamped 07:00:00 are
    # one step, and it is the last of them.
    assert [step["local"] for step in card["steps"]] == ["13:00", "14:00", "15:00"]
    assert card["replayed_steps"] == 2
    last = card["steps"][-1]
    assert last["mode"] == "21" and last["mode_p"] == pytest.approx(0.9)
    # The two covariates the likelihood read, put back on the °C axis.
    assert last["observed_c"] == pytest.approx(20.0)
    assert last["regional_high_c"] == pytest.approx(22.0)


def test_a_settled_day_carries_its_truth_and_the_rung_it_landed_on(tmp_path, monkeypatch) -> None:
    """The row ends on whether the chain was right, so the standing revision
    of the settled high travels with the card (charter 44: the highest
    revision wins)."""

    conn = _db(tmp_path)
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth (station_id TEXT,"
        " local_date TEXT, official_tmax_c REAL, revision_no INTEGER, qc_status TEXT)"
    )
    for revision, value in ((0, 20.2), (1, 21.4)):
        conn.execute(
            "INSERT INTO calibration_station_daily_high_truth VALUES (?,?,?,?,?)",
            ("ZUUU", "2026-09-18", value, revision, "ACCEPTED"),
        )
    conn.commit()
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 18, 7, 0, tzinfo=C.timezone.utc))
    (card,) = C.collect_station_bayes(conn)["stations"]
    assert card["settled"] == {"tmax_c": 21.4, "bucket": "21"}, "the later revision stands"
    # And the rung centres travel too: a Fahrenheit ladder's labels cannot
    # be turned back into °C by reading them.
    assert [row["centre_c"] for row in card["buckets"]] == [20.0, 21.0, 22.0]


def test_a_day_with_no_truth_yet_says_so_rather_than_guessing(tmp_path, monkeypatch) -> None:
    conn = _db(tmp_path)
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth (station_id TEXT,"
        " local_date TEXT, official_tmax_c REAL, revision_no INTEGER, qc_status TEXT)"
    )
    conn.commit()
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 18, 7, 0, tzinfo=C.timezone.utc))
    (card,) = C.collect_station_bayes(conn)["stations"]
    assert card["settled"] is None
