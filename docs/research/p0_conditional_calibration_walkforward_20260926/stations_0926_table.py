"""Render stations_0926.json as the markdown table (stations_0926.md); marks whether the recorded seat cleared the price gate.

User: 「我要所有站点9月26日的k1、篮子和市场的均值、众数及选桶。」  Read-only.
"""
import json, re, sqlite3
r = json.load(open("/Users/xiong/weather/docs/research/p0_conditional_calibration_walkforward_20260926/stations_0926.json"))
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30); conn.execute("PRAGMA query_only=ON")
def bounds(label):
    n = [int(x) for x in re.findall(r"\d+", label)]
    if "below" in label: return (None, n[0])
    if "higher" in label: return (n[0], None)
    return (n[0], n[-1])
def b1(label):
    a, b = bounds(label)
    return f"≤{b}" if a is None else f"{a}+" if b is None else (f"{a}" if a == b else f"{a}-{b}")
def rng(ws):
    if not ws: return "—"
    if len(ws) == 1: return b1(ws[0])
    lo, hi = bounds(ws[0])[0], bounds(ws[-1])[1]
    los = "≤" + str(bounds(ws[0])[1]) if lo is None else str(lo)
    his = str(bounds(ws[-1])[0]) + "+" if hi is None else str(hi)
    return f"{los}–{his}"
def cell(d, delta=None):
    mm, mo = b1(d["mean_bucket"]), b1(d["mode_bucket"])
    c = mm if mm == mo else f"{mm}/{mo}"
    sk = {"对称": "", "偏暖": " ↑", "偏冷": " ↓"}[d["skew"]]
    dl = "" if delta is None else f"（{delta:+.2f}）"
    return f'{d["mean"]}{dl} · {c} · {rng(d["seat"])}{sk}'
lines = ["| 站 | 阶段 | 篮子 | K1（挪动 °C） | 市场 | 篮子实际入座 |", "|---|---|---|---|---|---|"]
for x in r["rows"]:
    if "error" in x:
        lines.append(f'| {x["station"]} | — | — | — | 盘口不完整 | — |'); continue
    b, k, m = x["basket"], x["k1"], x["market"]
    prod = b["production_seat"]
    ev = conn.execute(
        "SELECT seated, admissible FROM basket_candidate_window_evidence"
        " WHERE station_id=? AND event_date='2026-09-26' AND decision_at_utc=?",
        (x["station"], x["belief_at"])).fetchall()
    passed = any(a for s_, a in ev if s_)
    if prod is None:
        act = "无记录"
    elif not passed:
        # Charter 78: nothing cleared the price gate; the plan keeps the
        # probability core (charter 76) as an inadmissible placeholder.
        act = f"无窗口过闸（占位 {rng(prod)}）"
    else:
        act = ("同左" if prod == b["seat"] else rng(prod)) + "（过闸）"
    unit = "°F" if "°F" in (b["mean_bucket"] or "") else "°C"
    lines.append(f'| {x["station"]} {unit} | {x["stage"]} | {cell(b)} | {cell(k, x["k1_delta_c"])} | {cell(m)} | {act} |')
text = "\n".join(lines)
print(text)
open("/Users/xiong/weather/docs/research/p0_conditional_calibration_walkforward_20260926/stations_0926.md", "w").write(r["as_of_utc"] + "\n\n" + text + "\n")
