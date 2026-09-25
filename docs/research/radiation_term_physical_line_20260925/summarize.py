#!/usr/bin/env python3
"""Render a ``measure.py`` result file as the tables the report quotes."""

from __future__ import annotations

import json
import sys


def line(label: str, block: dict | None) -> str:
    if not block or not block.get("n"):
        return f"| {label} | - | - | - | - |"
    low, high = block["ci95"]
    mark = " **>0**" if low > 0 else (" **<0**" if high < 0 else "")
    halves = ""
    if block.get("first_half_mean") is not None:
        halves = f" | {block['first_half_mean']:+.4f} / {block['second_half_mean']:+.4f}"
    return (
        f"| {label} | {block['mean']:+.4f}{mark} | [{low:+.4f}, {high:+.4f}] "
        f"| {block['days_better']}/{block['days']} | {block['n']}{halves} |"
    )


def main(path: str) -> None:
    data = json.load(open(path))
    print(f"inputs: {data['inputs']}\n")
    print("| comparison | by-day mean (nat, LogScore) | 95% CI (day bootstrap) | days better | decisions | first / second half |")
    print("|---|---|---|---|---|---|")
    p0 = data["p0_radiation_term"]
    print(line("P0: served radiation term (0.07 Kt z sigma) vs none", p0["radiation_term_logscore_gain"]))
    for stage in ("p0m_morning", "p0m_eve"):
        block = data[stage]
        print(line(f"{stage}: literal inheritance at 0.07 (TAF cloud, Kasten-Czeplak)", block["literal_inheritance_0.07"]["logscore_gain"]))
        for key in sorted(block):
            if key.endswith("logscore") and " vs " in key:
                print(line(f"{stage}: {key[:-9]}", block[key]))
    p1 = data["p1"]
    for key in sorted(p1):
        if key.endswith("logscore"):
            print(line(f"P1 {key[:-9]}", p1[key]))
    print("\nBrier (positive = better):\n")
    print("| comparison | by-day mean | 95% CI | days better | decisions | first / second half |")
    print("|---|---|---|---|---|---|")
    print(line("P0: served radiation term vs none", p0["radiation_term_brier_gain"]))
    for stage in ("p0m_morning", "p0m_eve"):
        block = data[stage]
        for key in sorted(block):
            if key.endswith("brier") and " vs " in key:
                print(line(f"{stage}: {key[:-6]}", block[key]))
    for key in sorted(p1):
        if key.endswith("brier"):
            print(line(f"P1 {key[:-6]}", p1[key]))


if __name__ == "__main__":
    main(sys.argv[1])
