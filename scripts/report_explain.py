"""TAF and METAR, taken apart token by token and read for temperature.

For the board's TAF and METAR tables (user, 2026-09-18: 只展示最新的报文拆解及
说明，以及对 p0m / p1 的实际影响；每一个报文要带总结分析，对于温度的影响是什么).
Pure functions over the raw text: no database, no clock.  The temperature
reading is a fixed rule, stated in words and never fitted here:

* a TAF's TX against the model's served mean for the same day;
* cloud, rain and wind as the physics the models get wrong in a direction --
  more cloud, rain or mixing and the day runs colder than the models say
  (``cross_consensus`` 2026-08-14: cloud -0.094, precipitation -0.128, wind
  -0.137 against the standardised residual);
* for a METAR, where the report sits against the day's running high and the
  forecast peak, and which way the last reports are moving.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Sequence

CLOUD_WORD = {
    "FEW": ("少云", 0.19), "SCT": ("疏云", 0.44), "BKN": ("多云", 0.75),
    "OVC": ("阴天", 1.0), "VV": ("天空不明（垂直能见度）", 1.0),
}
CLEAR_WORD = {
    "SKC": "晴空", "CLR": "晴空", "NSC": "无重要云", "NCD": "未探测到云",
    "CAVOK": "能见度好、无低云、无重要天气",
}
WX_WORD = {
    "RA": "雨", "SHRA": "阵雨", "TSRA": "雷雨", "TS": "雷暴", "DZ": "毛毛雨",
    "SN": "雪", "BR": "轻雾", "FG": "雾", "HZ": "霾", "FU": "烟", "DU": "浮尘",
    "SA": "沙", "VCSH": "附近阵雨", "VCTS": "附近雷暴", "SQ": "飑",
    "FZRA": "冻雨", "SHSN": "阵雪", "GR": "冰雹", "UP": "未知降水",
}
PRECIP_CODES = ("RA", "DZ", "SN", "TS", "SH", "GR", "UP")
CHANGE_WORD = {
    "BECMG": "逐渐转为", "TEMPO": "短时出现", "FM": "从此时起变为",
    "PROB30": "有 30% 可能", "PROB40": "有 40% 可能", "NOSIG": "未来两小时无明显变化",
    "INTER": "间歇出现",
}


def _wind(token: str) -> tuple[str, float | None] | None:
    match = re.fullmatch(r"(VRB|\d{3})(\d{2,3})(G(\d{2,3}))?(KT|MPS)", token)
    if not match:
        return None
    direction, speed, _g, gust, unit = match.groups()
    speed_v = float(speed)
    mps = speed_v * 0.514444 if unit == "KT" else speed_v
    unit_zh = "节" if unit == "KT" else "米/秒"
    head = "风向不定" if direction == "VRB" else f"风向 {int(direction)}°"
    text = f"{head}，风速 {int(speed_v)} {unit_zh}" + (f"，阵风 {int(gust)}" if gust else "")
    return text, mps


def _cloud(token: str) -> tuple[str, float] | None:
    match = re.fullmatch(r"(FEW|SCT|BKN|OVC|VV)(\d{3})(CB|TCU)?", token)
    if not match:
        return None
    kind, height, conv = match.groups()
    word, cover = CLOUD_WORD[kind]
    feet = int(height) * 100
    extra = {"CB": "（积雨云）", "TCU": "（浓积云）"}.get(conv or "", "")
    return f"{word}，云底 {feet} 英尺{extra}", cover


def _weather(token: str) -> str | None:
    match = re.fullmatch(r"([-+]|VC)?([A-Z]{2,6})", token)
    if not match:
        return None
    intensity, body = match.groups()
    if body not in WX_WORD and not any(body.startswith(k) for k in ("SH", "TS", "FZ")):
        return None
    word = WX_WORD.get(body) or WX_WORD.get(body[2:]) or body
    prefix = {"-": "小", "+": "强", "VC": "附近"}.get(intensity or "", "")
    return prefix + word


def _visibility(token: str) -> str | None:
    if token == "9999":
        return "能见度 ≥10 公里"
    if re.fullmatch(r"\d{4}", token):
        return f"能见度 {int(token)} 米"
    if re.fullmatch(r"P?\d{1,2}SM", token):
        return f"能见度 {token.replace('SM', '')} 英里"
    return None


def _explain_common(token: str) -> tuple[str, dict[str, Any]] | None:
    """A token either report can carry; returns its words and what it adds."""

    if token in CLEAR_WORD:
        return CLEAR_WORD[token], {"cover": 0.0}
    wind = _wind(token)
    if wind:
        return wind[0], {"wind_mps": wind[1]}
    cloud = _cloud(token)
    if cloud:
        return cloud[0], {"cover": cloud[1]}
    vis = _visibility(token)
    if vis:
        return vis, {}
    weather = _weather(token)
    if weather:
        precip = any(code in token for code in PRECIP_CODES)
        return weather, {"weather": weather, "precip": precip}
    if token == "NSW":
        return "无重要天气", {}
    return None


def explain_taf(raw: str) -> dict[str, Any]:
    """Tokens with their meaning, and what the base forecast says."""

    tokens = raw.split()
    out: list[dict[str, str]] = []
    kind = "TAF"
    tx = tn = None
    tx_at = tn_at = None
    tx_day_hour: tuple[int, int] | None = None
    cover = 0.0
    wind_mps = 0.0
    precip = False
    weathers: list[str] = []
    groups: list[str] = []
    group: str | None = None
    for index, token in enumerate(tokens):
        meaning = ""
        if token == "TAF" and index == 0:
            meaning = "机场天气预报"
        elif token in ("AMD", "COR", "RTD"):
            kind = f"TAF {token}"
            meaning = {"AMD": "修订报：预报员中途改了预报", "COR": "更正报：更正此前报文的错误",
                       "RTD": "延迟报"}[token]
        elif re.fullmatch(r"[A-Z]{4}", token) and index <= 2:
            meaning = "机场代码"
        elif re.fullmatch(r"\d{6}Z", token):
            meaning = f"{token[:2]} 日 {token[2:4]}:{token[4:6]}Z 发布"
        elif re.fullmatch(r"\d{4}/\d{4}", token):
            meaning = (f"有效期 {token[:2]} 日 {token[2:4]}Z 至 {token[5:7]} 日 {token[7:9]}Z"
                       if group is None else f"时段 {token[:2]} 日 {token[2:4]}Z–{token[5:7]} 日 {token[7:9]}Z")
        elif re.fullmatch(r"TXM?\d{2}/\d{4}Z", token):
            digits = re.search(r"TX(M?)(\d{2})/(\d{2})(\d{2})Z", token)
            value = -int(digits.group(2)) if digits.group(1) else int(digits.group(2))
            tx, tx_at = value, f"{digits.group(3)} 日 {digits.group(4)}Z"
            tx_day_hour = (int(digits.group(3)), int(digits.group(4)))
            meaning = f"最高气温 {value}°C，出现在 {tx_at}"
        elif re.fullmatch(r"TNM?\d{2}/\d{4}Z", token):
            digits = re.search(r"TN(M?)(\d{2})/(\d{2})(\d{2})Z", token)
            value = -int(digits.group(2)) if digits.group(1) else int(digits.group(2))
            tn, tn_at = value, f"{digits.group(3)} 日 {digits.group(4)}Z"
            meaning = f"最低气温 {value}°C，出现在 {tn_at}"
        elif token in CHANGE_WORD or re.fullmatch(r"FM\d{6}", token):
            group = token[:2] if token.startswith("FM") else token
            groups.append(group)
            meaning = (CHANGE_WORD["FM"] + f"（{token[2:4]} 日 {token[4:6]}:{token[6:8]}Z）"
                       if token.startswith("FM") else CHANGE_WORD[token])
        else:
            common = _explain_common(token)
            if common:
                meaning, adds = common
                if group is None:
                    cover = max(cover, adds.get("cover", 0.0))
                    wind_mps = max(wind_mps, adds.get("wind_mps", 0.0) or 0.0)
                if adds.get("precip"):
                    precip = True
                if adds.get("weather"):
                    weathers.append(adds["weather"] + ("" if group is None else f"（{CHANGE_WORD.get(group, group)}）"))
        out.append({"token": token, "meaning": meaning or "—"})
    return {
        "kind": kind, "tokens": out, "tx_c": tx, "tx_at": tx_at, "tx_day_hour": tx_day_hour, "tn_c": tn,
        "tn_at": tn_at, "base_cover": cover, "base_wind_mps": wind_mps,
        "precip": precip, "weathers": weathers, "change_groups": groups,
    }


def taf_temperature_reading(
    parsed: dict[str, Any], model_mean_c: float | None, *, model_label: str = "模型均值",
) -> list[str]:
    """What this TAF says about the day's high, in plain words.

    ``model_mean_c`` must be the served mean for the day this TAF's TX
    falls on -- a TAF for yesterday read against tomorrow's mean says
    nothing.  ``model_label`` names which one it is.
    """

    lines: list[str] = []
    tx = parsed.get("tx_c")
    if tx is not None and model_mean_c is not None:
        gap = float(tx) - float(model_mean_c)
        if abs(gap) < 0.5:
            lines.append(f"预报最高温 {tx}°C，和{model_label} {model_mean_c:.1f}°C 基本一致")
        else:
            lines.append(
                f"预报最高温 {tx}°C，比{model_label} {model_mean_c:.1f}°C "
                f"{'高' if gap > 0 else '低'} {abs(gap):.1f}°C → {'偏暖' if gap > 0 else '偏冷'}信号"
            )
    elif tx is not None:
        lines.append(f"预报最高温 {tx}°C")
    else:
        lines.append("这份报文没有给最高温（TX）")
    cover = float(parsed.get("base_cover") or 0.0)
    if cover >= 0.75:
        lines.append("主体预报多云到阴，白天升温受限 → 偏冷")
    elif cover <= 0.25:
        lines.append("主体预报云少，日照足，利于升温")
    if parsed.get("precip"):
        lines.append("有降水或雷雨（" + "、".join(parsed.get("weathers") or []) + "）→ 压低最高温，偏冷")
    wind = float(parsed.get("base_wind_mps") or 0.0)
    if wind >= 8.0:
        lines.append(f"风大（约 {wind:.0f} 米/秒），混合强 → 历史上这种天比模型说的更冷")
    if parsed.get("change_groups"):
        counts = Counter(parsed["change_groups"])
        lines.append("含变化组（" + "、".join(
            CHANGE_WORD.get(g, g) + (f" ×{n}" if n > 1 else "") for g, n in counts.items()
        ) + "），天气会中途变化")
    return lines


def explain_metar(raw: str) -> dict[str, Any]:
    tokens = raw.split()
    out: list[dict[str, str]] = []
    kind = "METAR"
    temp = dew = None
    cover = 0.0
    wind_mps = 0.0
    precip = False
    weathers: list[str] = []
    trend: str | None = None
    in_trend = False
    for index, token in enumerate(tokens):
        meaning = ""
        if token in ("METAR", "SPECI") and index == 0:
            kind = token
            meaning = "例行观测" if token == "METAR" else "特殊观测：天气突变时加发"
        elif token in ("COR", "AUTO"):
            if token == "COR":
                kind = kind + " COR"
            meaning = {"COR": "更正报", "AUTO": "自动站观测"}[token]
        elif re.fullmatch(r"[A-Z]{4}", token) and index <= 2:
            meaning = "机场代码"
        elif re.fullmatch(r"\d{6}Z", token):
            meaning = f"{token[:2]} 日 {token[2:4]}:{token[4:6]}Z 观测"
        elif re.fullmatch(r"M?\d{2}/(M?\d{2})?", token):
            left, right = token.split("/")
            temp = -int(left[1:]) if left.startswith("M") else int(left)
            if right:
                dew = -int(right[1:]) if right.startswith("M") else int(right)
            meaning = f"气温 {temp}°C" + (f"，露点 {dew}°C" if dew is not None else "")
        elif re.fullmatch(r"[QA]\d{4}", token):
            meaning = f"气压 {token[1:]} {'百帕' if token[0] == 'Q' else '（英寸汞柱×100）'}"
        elif token in CHANGE_WORD:
            trend = token
            in_trend = True
            meaning = CHANGE_WORD[token]
        elif token == "RMK":
            meaning = "以下为备注"
            break
        else:
            common = _explain_common(token)
            if common:
                meaning, adds = common
                if not in_trend:
                    cover = max(cover, adds.get("cover", 0.0))
                    wind_mps = max(wind_mps, adds.get("wind_mps", 0.0) or 0.0)
                    if adds.get("precip"):
                        precip = True
                    if adds.get("weather"):
                        weathers.append(adds["weather"])
        out.append({"token": token, "meaning": meaning or "—"})
    return {
        "kind": kind, "tokens": out, "temp_c": temp, "dew_c": dew,
        "cover": cover, "wind_mps": wind_mps, "precip": precip,
        "weathers": weathers, "trend": trend,
    }


def metar_temperature_reading(
    parsed: dict[str, Any],
    *,
    running_high_c: float | None,
    forecast_peak_c: float | None,
    previous_temps: Sequence[float],
    local_hour: float | None,
) -> list[str]:
    lines: list[str] = []
    temp = parsed.get("temp_c")
    if temp is None:
        return ["这份报文没有气温"]
    head = f"现在 {temp}°C"
    if running_high_c is not None:
        head += f"，今天已到最高 {running_high_c:g}°C"
    lines.append(head)
    if previous_temps:
        last = previous_temps[0]
        if temp > last:
            lines.append(f"比上一报升了 {temp - last:g}°C，还在升温")
        elif temp < last:
            lines.append(f"比上一报降了 {last - temp:g}°C，在回落")
        else:
            lines.append("和上一报持平")
    if forecast_peak_c is not None:
        room = float(forecast_peak_c) - float(running_high_c if running_high_c is not None else temp)
        if room > 0.5:
            lines.append(f"模型预计今天最高 {forecast_peak_c:.1f}°C，离已到最高还差 {room:.1f}°C")
        else:
            lines.append(f"模型预计今天最高 {forecast_peak_c:.1f}°C，已基本到顶")
    if local_hour is not None:
        if local_hour >= 16:
            lines.append(f"当地 {int(local_hour)} 点，已过午后峰值时段，再升空间小")
        elif local_hour < 9:
            lines.append(f"当地 {int(local_hour)} 点，白天升温还没开始")
    if parsed.get("dew_c") is not None and temp - parsed["dew_c"] <= 2:
        lines.append("气温接近露点，空气很湿，升温会慢")
    if float(parsed.get("cover") or 0.0) >= 0.75:
        lines.append("多云到阴，日照弱 → 压着升温")
    if parsed.get("precip"):
        lines.append("正在下雨/雷雨（" + "、".join(parsed.get("weathers") or []) + "）→ 偏冷")
    if parsed.get("kind", "").startswith("SPECI"):
        lines.append("特殊报：天气刚发生明显变化，优先看")
    return lines
