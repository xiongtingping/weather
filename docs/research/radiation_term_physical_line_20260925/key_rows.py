#!/usr/bin/env python3
"""The report's key rows (LogScore and Brier side by side) from a result file."""

from __future__ import annotations

import json
import sys

P0M_KEYS = (
    ("T+M+TAF vs T+M [all]", "TAF 云/雨/风 在「温度线＋模式物理」之上（主比较 1）"),
    ("T+M+revd vs T+M [all]", "修正形式：TAF−模式，按站去偏"),
    ("T+rev vs T [all]", "修正形式：TAF−模式（不带模式物理）"),
    ("T+TAF vs T [all]", "TAF 云/雨/风 在温度线之上"),
    ("T+M+TAF vs T+M [awc]", "同主比较 1，只看 AWC 近 29 天（45 站）"),
    ("T+M+TAF vs T+M [iem]", "同主比较 1，只看 IEM 美国 16 个月"),
    ("T+M+TAF vs T+M [with_tx]", "同主比较 1，只看带 TX 的 TAF"),
    ("T+M+oracle vs T+M [all]", "上限：事后实测升温窗云/雨（完美 TAF）"),
    ("T+M vs T [all]", "参照：模式自身物理重新加权"),
)
P1_KEYS = (
    ("T+S vs T", "METAR 此刻状态（生产已读，主比较 3）"),
    ("T+S+el+taf+rad vs T+S", "整条物理线在其之上（主比较 2）"),
    ("T+S+el vs T+S", "  其中：已过时段实况−预报"),
    ("T+S+eld vs T+S", "  其中：同上，按站去偏"),
    ("T+S+el+taf vs T+S+el", "  其中：继承的 TAF 余下时段修正"),
    ("T+S+el+taf+rad vs T+S+el+taf", "  其中：P0 辐射项在 P1 重新加权"),
    ("T+el vs T", "参照：只有已过时段实况−预报"),
)


def halves(block: dict | None) -> str:
    if not block or block.get("first_half_mean") is None:
        return "-"
    return f"{block['first_half_mean']:+.4f} / {block['second_half_mean']:+.4f}"


def cell(block: dict | None) -> str:
    if not block or not block.get("n"):
        return "-"
    low, high = block["ci95"]
    flag = "↑" if low > 0 else ("↓" if high < 0 else "")
    return f"{block['mean']:+.4f} [{low:+.4f}, {high:+.4f}]{flag}"


def main(path: str) -> None:
    data = json.load(open(path))
    p0 = data["p0_radiation_term"]
    print(f"P0 辐射项（0.07·Kt z·σ）对无辐射项：LogScore {cell(p0['radiation_term_logscore_gain'])}，"
          f"Brier {cell(p0['radiation_term_brier_gain'])}\n")
    for stage, title in (("p0m_morning", "P0-M 早间 TAF"), ("p0m_eve", "P0-M 前一晚 TAF")):
        block = data[stage]
        lit = block["literal_inheritance_0.07"]
        print(f"**{title}**（{block['cases']} 站日，带 TX {block['with_tx']}；来源 {block['sources']}）\n")
        print("| 比较 | LogScore（nat/站日） | Brier | 好的天数 | 前/后半 |")
        print("|---|---|---|---|---|")
        print(f"| 字面继承：TAF 云按 Kasten–Czeplak 改 Kt，0.07 系数不拟合（Δz 绝对值均值 {lit['mean_abs_dz']:.2f}） "
              f"| {cell(lit['logscore_gain'])} | {cell(lit['brier_gain'])} | "
              f"{lit['logscore_gain']['days_better']}/{lit['logscore_gain']['days']} | "
              f"{halves(lit['logscore_gain'])} |")
        for key, label in P0M_KEYS:
            log = block.get(f"{key} logscore")
            brier = block.get(f"{key} brier")
            if not log:
                continue
            print(f"| {label} | {cell(log)} | {cell(brier)} | {log['days_better']}/{log['days']} | "
                  f"{halves(log)} |")
        print()
    p1 = data["p1"]
    hours = sorted({key.split()[0] for key in p1 if key.startswith("h") and key.endswith("logscore")})
    print("**P1**（LogScore nat/决策；每格：均值 [95% 区间]，↑/↓＝区间不含 0）\n")
    print("| 比较 | " + " | ".join(f"当地 {h[1:]}:00" for h in hours) + " |")
    print("|---|" + "---|" * len(hours))
    for key, label in P1_KEYS:
        print(f"| {label} | " + " | ".join(cell(p1.get(f"{h} {key} logscore")) for h in hours) + " |")
    print("\nBrier（正＝更好）：\n")
    print("| 比较 | " + " | ".join(f"当地 {h[1:]}:00" for h in hours) + " |")
    print("|---|" + "---|" * len(hours))
    for key, label in P1_KEYS:
        print(f"| {label} | " + " | ".join(cell(p1.get(f"{h} {key} brier")) for h in hours) + " |")
    print("\n" + ", ".join(f"{h}: {p1.get(f'{h} rows')} 决策" for h in hours))


if __name__ == "__main__":
    main(sys.argv[1])
