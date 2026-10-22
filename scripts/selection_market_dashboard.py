#!/usr/bin/env python3
"""Station labels and the odds a mean ticket price implies, for the live board.

The page that used to live here -- every station's daily seat read against
the market off the paper ledger -- retired with that ledger (2026-09-25), and
the chosen-basket readings ``live_selection`` fed it went with that module
(2026-10-22, user 「删live_selection」).  What stays is what the live readers
import: the readable station names and ``_odds_from_mean_cost``.
"""

from __future__ import annotations

from collections.abc import Sequence

# The registry's city_label is empty for every station, so the readable name
# lives here.  The ICAO code stays the key everywhere -- it is the stable
# identifier -- and the label is only ever for the page.
STATION_LABELS = {
    "CYYZ": "多伦多", "EDDM": "慕尼黑", "EFHK": "赫尔辛基",
    "EGLC": "伦敦", "EHAM": "阿姆斯特丹", "EPWA": "华沙",
    "FACT": "开普敦", "KATL": "亚特兰大", "KAUS": "奥斯汀",
    "KBKF": "丹佛", "KDEN": "丹佛", "KDAL": "达拉斯", "KHOU": "休斯敦",
    "KLAX": "洛杉矶", "KLGA": "纽约", "KMIA": "迈阿密",
    "KORD": "芝加哥", "KSEA": "西雅图", "KSFO": "旧金山",
    "LEMD": "马德里", "LFPB": "巴黎", "LIMC": "米兰",
    "LTAC": "安卡拉", "MMMX": "墨西哥城", "MPMG": "巴拿马城",
    "NZWN": "惠灵顿", "OEJN": "吉达", "OPKC": "卡拉奇",
    "RCSS": "台北", "RJTT": "东京", "RKPK": "釜山",
    "RKSI": "首尔", "RPLL": "马尼拉", "SAEZ": "布宜诺斯艾利斯",
    "SBGR": "圣保罗", "VILK": "勒克瑙", "WMKK": "吉隆坡",
    "WSSS": "新加坡", "ZBAA": "北京", "ZGGG": "广州",
    "ZGSZ": "深圳", "ZHHH": "武汉", "ZSPD": "上海",
    "ZSQD": "青岛", "ZUCK": "重庆", "ZUUU": "成都",
}


def _odds_from_mean_cost(costs: Sequence[float | None]) -> float | None:
    """Net odds implied by an average ticket price, never the mean of odds.

    ``(1-c)/c`` is convex: the mean of it is set by whichever basket was
    cheapest, not by the money at stake.  Measured over this book on
    2026-09-10 the per-row mean was 14.7x while the money actually bought
    1.53x, and a single day's row printed 32.8x.  Average the price, then
    invert once.
    """

    priced = [float(c) for c in costs if c is not None and float(c) > 0]
    if not priced:
        return None
    mean = sum(priced) / len(priced)
    return (1.0 - mean) / mean if mean > 0 else None
