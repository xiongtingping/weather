"""真值落位, as computed and as rendered (2026-10-03 audit, user 「改」).

Four rules the audit set (docs/research/truth_landing_audit_20261003):
truth is the settled bucket; the market's number is the normalised entry mid
and the ask is shown only as the cost of buying; both rulers read the same
buckets; the current chain and its history are two populations, every
verdict reading a 95% event-day interval.

Asserted on computed tables and rendered output: a sentence that says
市场更准 over numbers whose interval crosses zero is the failure this pins.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

SOURCE = C.HTML_TEMPLATE


def _function(name: str) -> str:
    start = SOURCE.index(f"function {name}(")
    depth = 0
    for index in range(SOURCE.index("{", start), len(SOURCE)):
        if SOURCE[index] == "{":
            depth += 1
        elif SOURCE[index] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[start:index + 1]
    raise AssertionError(name)


def _render(payload: dict) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    script = (
        "const D = " + json.dumps({"truth_landing": payload}, ensure_ascii=False) + ";\n"
        "const h = (s) => String(s == null ? '' : s);\n"
        + _function("truthLandingDetail") + "\nconsole.log(truthLandingDetail());\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


# --------------------------------------------------------------- computation
LADDER = ["20", "21", "22", "23", "24"]


def _row(day, truth, claims, market, cost=None):
    return {"event_date": day, "truth": truth, "claims": dict(zip(LADDER, claims)),
            "market": dict(zip(LADDER, market)),
            "cost": dict(zip(LADDER, cost or [m + 0.03 for m in market]))}


def test_both_rulers_read_the_same_buckets_and_count_them_once():
    rows = [_row(f"2026-09-{d:02d}", "22", [0.05, 0.2, 0.5, 0.2, 0.05], [0.05, 0.25, 0.4, 0.25, 0.05])
            for d in range(1, 11)]
    tables = C._truth_landing_tables(rows)

    assert tables["station_days"] == 10 and tables["event_days"] == 10
    assert sum(b["bucket_count"] for b in tables["by_price"]) == 50
    assert sum(b["bucket_count"] for b in tables["by_claim"]) == 50
    assert sum(b["truth_count"] for b in tables["by_price"]) == 10
    assert sum(b["truth_count"] for b in tables["by_claim"]) == 10


def test_a_group_whose_interval_crosses_zero_is_not_called():
    # Model and market sit either side of the hit rate, by the same margin on
    # alternate days: nothing to choose between them.
    rows = []
    for d in range(1, 21):
        truth = "22" if d % 2 else "21"
        rows.append(_row(f"2026-09-{d:02d}", truth, [0.02, 0.36, 0.40, 0.2, 0.02], [0.02, 0.16, 0.60, 0.2, 0.02]))
    groups = {g["key"]: g for g in C._truth_landing_tables(rows)["disagreement"]["groups"]}
    much = groups["market_much"]
    assert much["buckets"] == 20 and much["market_nearer_interval"] is not None
    lo, hi = much["market_nearer_interval"]
    assert lo <= much["market_nearer"] <= hi


def test_the_settled_bucket_is_the_truth_and_unpriced_days_leave_both_sides(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "TRUTH_LANDING_SINCE", "2026-09-01")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE calibration_station_market_timezone_binding (station_id TEXT, settlement_timezone TEXT)")
    conn.execute("INSERT INTO calibration_station_market_timezone_binding VALUES ('KLGA','America/New_York')")
    conn.execute("CREATE TABLE basket_probability_envelope (station_id TEXT, event_date TEXT, stage TEXT,"
                 " decision_at_utc TEXT, probabilities_json TEXT, posterior_version TEXT)")
    conn.execute("CREATE VIEW calibration_station_registry AS SELECT DISTINCT station_id"
                 " FROM basket_probability_envelope")
    conn.execute("CREATE TABLE basket_official_ws_snapshot (station_id TEXT, event_date TEXT, observed_at TEXT,"
                 " quote_expires_at TEXT, buckets_json TEXT)")
    conn.execute("CREATE TABLE basket_station_day_settlement_label (station_id TEXT, event_date TEXT,"
                 " truth_revision_no INTEGER, settled_bucket TEXT)")
    for day, priced in (("2026-09-02", True), ("2026-09-03", False), ("2026-09-04", True)):
        conn.execute("INSERT INTO basket_probability_envelope VALUES ('KLGA',?,'P0',?,?,'v')",
                     (day, f"{day}T01:00:00Z", json.dumps({"70": 0.6, "71": 0.4})))
        for hour in (2, 3):
            books = [{"bucket": "70", "ask_levels": [{"price": 0.66}], "bid_price": 0.54}]
            if priced:
                books.append({"bucket": "71", "ask_levels": [{"price": 0.46}], "bid_price": 0.34})
            conn.execute("INSERT INTO basket_official_ws_snapshot VALUES ('KLGA',?,?,?,?)",
                         (day, f"{day}T{hour:02d}:00:00Z", "x", json.dumps(books)))
    # Revision 1 corrects revision 0; the last day is never settled.
    conn.execute("INSERT INTO basket_station_day_settlement_label VALUES ('KLGA','2026-09-02',0,'71')")
    conn.execute("INSERT INTO basket_station_day_settlement_label VALUES ('KLGA','2026-09-02',1,'70')")
    conn.execute("INSERT INTO basket_station_day_settlement_label VALUES ('KLGA','2026-09-03',0,'70')")
    saved = dict(C._PAGED_RUN)
    C._PAGED_RUN.clear()
    try:
        out = C.collect_truth_landing(conn)
    finally:
        C._PAGED_RUN.update(saved)

    assert out["closed_station_days"] == 3
    assert out["settled_station_days"] == 2 and out["unsettled_station_days"] == 1
    assert out["unpriced_station_days"] == 1          # 09-03 has no quote on 71
    segments = {s["key"]: s for s in out["segments"]}
    rows = segments["current"]["station_days"] + segments["history"]["station_days"]
    assert rows == 1                                   # only 09-02 is settled and fully priced
    seg = segments["current"] if segments["current"]["station_days"] else segments["history"]
    by_price = {tuple(b["band"]): b for b in seg["by_price"]}
    # mids 0.60 and 0.40 (ask + bid halved), already summing to one; the ask is the cost
    top = by_price[(0.5, 1.01)]
    assert top["value_mean"] == pytest.approx(0.60) and top["cost_mean"] == pytest.approx(0.66)
    assert top["truth_count"] == 1                     # the corrected settled bucket, 70


# --------------------------------------------------------------- rendering
def _group(key, buckets, claim, price, hit, interval, cost=None):
    label = {name: label for name, label, _, _ in C.TRUTH_LANDING_GAP_GROUPS}[key]
    return {"key": key, "label": label, "buckets": buckets, "hits": round(hit * buckets),
            "claim_mean": claim, "price_mean": price, "cost_mean": cost if cost is not None else price + 0.03,
            "hit_rate": hit, "hit_interval": [hit - 0.02, hit + 0.02],
            "market_nearer": abs(hit - claim) - abs(hit - price), "market_nearer_interval": interval}


def _band(low, high, truths, buckets, hit, mean, gap_interval, cost=None):
    return {"band": [low, high], "truth_count": truths, "bucket_count": buckets,
            "truth_share": 0.1, "hit_rate": hit, "value_mean": mean, "cost_mean": cost if cost is not None else mean + 0.02,
            "hit_interval": [hit - 0.03, hit + 0.03], "gap_interval": gap_interval}


GROUPS = [
    _group("model_much", 900, 0.32, 0.14, 0.17, [0.08, 0.13]),
    _group("model_bit", 900, 0.16, 0.10, 0.11, [-0.01, 0.05]),
    _group("agree", 900, 0.13, 0.13, 0.14, [-0.001, 0.001]),
    _group("market_bit", 900, 0.08, 0.14, 0.12, [0.006, 0.047]),
    _group("market_much", 900, 0.12, 0.29, 0.31, [-0.20, -0.10]),
]
BANDS_PRICE = [_band(0.0, 0.05, 50, 9000, 0.005, 0.017, [-0.014, -0.010]),
               _band(0.4, 0.45, 100, 200, 0.43, 0.42, [-0.04, 0.06])]
BANDS_CLAIM = [_band(0.0, 0.05, 160, 11000, 0.015, 0.011, [0.002, 0.006]),
               _band(0.4, 0.45, 110, 280, 0.39, 0.42, [-0.09, 0.03])]


def _payload(groups=GROUPS, by_price=BANDS_PRICE, by_claim=BANDS_CLAIM, history=None):
    current = {"key": "current", "label": "现役：P0 证据纪元 2026-09-14 起的首个决策（宪章 202）",
               "station_days": 700, "event_days": 17, "by_price": by_price, "by_claim": by_claim,
               "disagreement": {"unlikely_below": 0.05, "unlikely_buckets": 50, "unlikely_hits": 1, "groups": groups,
                                "acceptance_baseline": {"claim_floor": 0.45, "market_ceiling": 0.3, "buckets": 51, "hits": 3,
                                                        "hit_rate": 3 / 51, "claim_mean": 0.51, "price_mean": 0.18, "cost_mean": 0.21}}}
    segments = [current] + ([history] if history else [])
    return {"since": "2026-08-15", "closed_station_days": 2115, "settled_station_days": 2114,
            "unsettled_station_days": 1, "truth_off_ladder_days": 0, "unpriced_station_days": 190,
            "epoch_utc": "2026-09-14T05:52:13Z", "segments": segments}


def _verdicts(html: str) -> list[str]:
    return re.findall(r'<div class="tl-verdict"><span class="pill \w*">([^<]+)</span>', html)


def test_each_group_reads_its_interval():
    html = _render(_payload())
    assert _verdicts(html) == ["市场更准", "分不出", "分不出", "市场更准", "模型更准"]
    assert "2 组市场更准</b>、1 组模型更准、1 组分不出" in html


def test_a_thin_group_is_not_judged():
    groups = [_group("model_much", 12, 0.30, 0.10, 0.28, [0.1, 0.2])] + GROUPS[1:]
    assert _verdicts(_render(_payload(groups)))[0] == "样本少"


def test_the_market_is_the_mid_and_the_ask_is_a_cost():
    html = _render(_payload())
    assert "市场（归一中间价）" in html and "卖一均价" in html
    assert "真值＝官方结算桶" in html
    expected = 100 * (900 * (0.17 - 0.17) + 900 * (0.11 - 0.13)) / 1800
    assert f"{abs(expected):.1f}¢/股" in html


def test_a_band_reads_whether_each_ruler_is_right_in_it():
    html = _render(_payload())
    rows = re.findall(r'<td class="mono">(\d\.\d\d–\d\.\d\d)</td>.*?<span class="pill \w*">([^<]+)</span></td></tr>', html, re.S)
    assert rows == [("0.00–0.05", "都不准"), ("0.40–0.45", "都准")]


def test_the_current_chain_comes_first_and_open_and_the_history_folds():
    history = dict(_payload()["segments"][0], key="history", label="历史：2026-09-14 之前", station_days=1200, event_days=30)
    html = _render(_payload(history=history))
    first = html.index("现役：P0 证据纪元"); second = html.index("历史：2026-09-14 之前")
    assert first < second
    assert html[:first].rstrip().endswith("<summary><b>") or "<details open" in html[:first]
    assert html.count("<details") == 2 and html.count("<details open") == 1


def test_an_empty_segment_draws_nothing():
    payload = _payload()
    payload["segments"][0]["station_days"] = 0
    assert _render(payload).strip() == ""


def test_the_summary_leads_with_who_is_nearer_and_folds_the_tables():
    """2026-10-17 「边际板块……按同样版式极简化」: the section opens on the tally of
    disagreeing groups, the cents bought over the market, and one dot chart;
    the former tables sit under 「明细」."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    helpers = SOURCE[SOURCE.index("const edgeBig="):SOURCE.index("function truthLanding(")]
    script = (
        "const D = " + json.dumps({"truth_landing": _payload()}, ensure_ascii=False) + ";\n"
        "const h = (s) => String(s == null ? '' : s);\n" + helpers
        + _function("truthLandingDetail") + "\n" + _function("truthLanding") + "\nconsole.log(truthLanding());\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    html = result.stdout
    summary = html[:html.index("<summary>明细</summary>")]
    assert "真值落位" in summary and "分歧时市场更准" in summary and "2 / 4" in summary
    assert "<svg viewBox" in summary and "实际开出" in summary
    assert "2 组市场更准</b>" in html[html.index("<summary>明细</summary>"):], "the old verdict line is folded"
