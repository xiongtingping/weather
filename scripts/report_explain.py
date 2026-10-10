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
from datetime import datetime, timedelta, timezone
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


# ---------------------------------------------------------------------------
# The station-day tables (user 2026-10-10「metar改为表格形式，拆分报文及对应
# 简要说明和趋势」「TAF 同理」): a report cut into its groups -- wind,
# visibility, weather, cloud, temperature, pressure, trend -- each group its
# raw code beside a few words, and the numbers a table compares from one
# report to the next.  A few words, not the sentences above.

COMPASS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")
CLOUD_SHORT = {"FEW": "少云", "SCT": "疏云", "BKN": "多云", "OVC": "阴天", "VV": "垂直能见度"}
CLEAR_SHORT = {"SKC": "晴空", "CLR": "晴空", "NSC": "无重要云", "NCD": "无云（自动站）"}
TAF_CHANGE_WORD = {"BECMG": "逐渐转为", "TEMPO": "短时", "FM": "起转为", "INTER": "间歇"}


def _group_wind(token: str) -> dict[str, Any] | None:
    match = re.fullmatch(r"(VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)", token)
    if not match:
        return None
    direction, speed, gust, unit = match.groups()
    scale = 0.514444 if unit == "KT" else 1.0
    mps, gust_mps = float(speed) * scale, float(gust) * scale if gust else None
    if direction != "VRB" and int(direction) == 0 and mps == 0:
        text = "静风"
    else:
        head = "风向不定" if direction == "VRB" else f"{COMPASS[round(int(direction) / 45) % 8]}风"
        text = f"{head} {mps:.0f} m/s" + (f"，阵风 {gust_mps:.0f} m/s" if gust_mps else "")
    return {"raw": token, "text": text, "dir_deg": None if direction == "VRB" else int(direction),
            "mps": round(mps, 1), "gust_mps": None if gust_mps is None else round(gust_mps, 1)}


def _group_visibility(token: str) -> dict[str, Any] | None:
    if token == "CAVOK":
        return {"raw": token, "text": "≥10 km，无云无天气", "m": 10000.0}
    if token == "9999":
        return {"raw": token, "text": "≥10 km", "m": 10000.0}
    if re.fullmatch(r"\d{4}", token):
        metres = float(token)
        return {"raw": token, "text": f"{metres / 1000:g} km" if metres >= 1000 else f"{metres:.0f} m", "m": metres}
    match = re.fullmatch(r"(P?)(\d{1,2}|\d/\d)SM", token)
    if match:
        top, value = match.groups()
        miles = float(value.split("/")[0]) / float(value.split("/")[1]) if "/" in value else float(value)
        km = miles * 1.609344
        return {"raw": token, "text": f"{'>' if top else ''}{km:.0f} km" if km >= 1 else f"{km * 1000:.0f} m",
                "m": round(km * 1000)}
    return None


def _group_weather(token: str) -> dict[str, Any] | None:
    if token == "NSW":
        return {"raw": token, "text": "天气转好", "precip": False}
    recent = token.startswith("RE") and len(token) > 3
    word = _weather(token[2:] if recent else token)
    if not word:
        return None
    precip = any(code in token for code in PRECIP_CODES)
    return {"raw": token, "text": ("近时" if recent else "") + word, "precip": precip and not recent}


def _group_cloud(token: str) -> dict[str, Any] | None:
    if token in CLEAR_SHORT:
        return {"raw": token, "text": CLEAR_SHORT[token], "cover": 0.0, "base_m": None}
    match = re.fullmatch(r"(FEW|SCT|BKN|OVC|VV)(\d{3}|///)(CB|TCU|///)?", token)
    if not match:
        return None
    kind, height, conv = match.groups()
    base = None if height == "///" else round(int(height) * 30.48, -1)
    text = CLOUD_SHORT[kind] + ("" if base is None else f" {base:.0f} m")
    text += {"CB": " · 积雨云", "TCU": " · 浓积云"}.get(conv or "", "")
    return {"raw": token, "text": text, "cover": CLOUD_WORD[kind][1], "base_m": base}


def _element(token: str) -> tuple[str, dict[str, Any]] | None:
    """One weather element a METAR body, a METAR trend or a TAF period can carry."""

    for kind, read in (("wind", _group_wind), ("vis", _group_visibility),
                       ("cloud", _group_cloud), ("wx", _group_weather)):
        group = read(token)
        if group:
            return kind, group
    return None


def _elements_text(elements: dict[str, Any]) -> str:
    parts = [g["text"] for k in ("wx", "cloud") for g in elements.get(k) or []]
    parts += [elements[k]["text"] for k in ("wind", "vis") if elements.get(k)]
    return "，".join(parts)


def metar_groups(raw: str) -> dict[str, Any]:
    tokens = raw.split()
    body = tokens[:tokens.index("RMK")] if "RMK" in tokens else tokens
    out: dict[str, Any] = {"wind": None, "vis": None, "wx": [], "cloud": [], "temp": None,
                           "qnh": None, "trend": None, "other": []}
    trend: list[str] = []
    for index, token in enumerate(body):
        if trend:
            trend.append(token)
            continue
        if token in ("METAR", "SPECI", "COR", "AUTO") or (index <= 2 and re.fullmatch(r"[A-Z]{4}", token)) \
                or re.fullmatch(r"\d{6}Z", token):
            continue
        if token in ("NOSIG", "BECMG", "TEMPO"):
            trend.append(token)
            continue
        if out["wind"] and re.fullmatch(r"\d{3}V\d{3}", token):
            out["wind"]["raw"] += " " + token
            out["wind"]["text"] += f"，摆动 {token[:3]}°–{token[4:]}°"
            continue
        temp = re.fullmatch(r"(M?)(\d{2})/(?:(M?)(\d{2}))?", token)
        if temp:
            t = (-1 if temp.group(1) else 1) * int(temp.group(2))
            d = None if temp.group(4) is None else (-1 if temp.group(3) else 1) * int(temp.group(4))
            out["temp"] = {"raw": token, "temp_c": t, "dew_c": d,
                           "text": f"{t}°C" + ("" if d is None else f"，露点 {d}°C")}
            continue
        qnh = re.fullmatch(r"([QA])(\d{4})", token)
        if qnh:
            hpa = float(qnh.group(2)) if qnh.group(1) == "Q" else round(float(qnh.group(2)) / 100 * 33.8639, 1)
            out["qnh"] = {"raw": token, "hpa": hpa, "text": f"{hpa:.0f} hPa"}
            continue
        element = _element(token)
        if element:
            kind, group = element
            if kind in ("wx", "cloud"):
                out[kind].append(group)
            elif out[kind] is None:
                out[kind] = group
            continue
        out["other"].append(token)
    if trend:
        if trend == ["NOSIG"]:
            out["trend"] = {"raw": "NOSIG", "text": "两小时内无变化"}
        else:
            parts: dict[str, Any] = {"wx": [], "cloud": []}
            for token in trend[1:]:
                element = _element(token)
                if element:
                    kind, group = element
                    if kind in ("wx", "cloud"):
                        parts[kind].append(group)
                    else:
                        parts[kind] = group
            words = {"BECMG": "逐渐转为", "TEMPO": "短时", "NOSIG": "无变化"}
            out["trend"] = {"raw": " ".join(trend), "text": f"{words.get(trend[0], trend[0])} {_elements_text(parts)}".strip()}
    return out


def _taf_time(code: str, issued: datetime) -> str | None:
    """``ddhh`` (or ``ddhhmm``) against the TAF's issue time, as UTC ISO."""

    if not re.fullmatch(r"\d{4}(\d{2})?", code):
        return None
    day, hour, minute = int(code[:2]), int(code[2:4]), int(code[4:6] or 0)
    year, month = issued.year, issued.month
    if day < issued.day - 15:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    elif day > issued.day + 15:
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    try:
        at = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(hours=hour, minutes=minute)
    except ValueError:
        return None
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def taf_groups(raw: str, issued_at_utc: str | None) -> dict[str, Any]:
    tokens = raw.split()
    try:
        issued = datetime.fromisoformat(str(issued_at_utc).replace("Z", "+00:00"))
    except ValueError:
        issued = None
    when = (lambda code: _taf_time(code, issued)) if issued else (lambda code: None)
    out: dict[str, Any] = {"amend": None, "valid_from_utc": None, "valid_to_utc": None,
                           "base": {"wind": None, "vis": None, "wx": [], "cloud": []},
                           "tx": None, "tn": None, "changes": []}
    current = out["base"]
    prob: str | None = None
    for index, token in enumerate(tokens):
        if token in ("TAF",) or (index <= 3 and re.fullmatch(r"[A-Z]{4}", token)) or re.fullmatch(r"\d{6}Z", token):
            continue
        if token in ("AMD", "COR", "RTD"):
            out["amend"] = token
            continue
        span = re.fullmatch(r"(\d{4})/(\d{4})", token)
        if span:
            if current is out["base"] and out["valid_from_utc"] is None:
                out["valid_from_utc"], out["valid_to_utc"] = when(span.group(1)), when(span.group(2))
            elif current is not out["base"]:
                current["from_utc"], current["to_utc"] = when(span.group(1)), when(span.group(2))
                current["raw"] += " " + token
            continue
        extreme = re.fullmatch(r"T([XN])(M?)(\d{2})/(\d{4})Z", token)
        if extreme:
            value = (-1 if extreme.group(2) else 1) * int(extreme.group(3))
            out["tx" if extreme.group(1) == "X" else "tn"] = {
                "raw": token, "c": value, "at_utc": when(extreme.group(4))}
            continue
        if re.fullmatch(r"PROB\d{2}", token):
            prob = token
            current = {"kind": token, "raw": token, "from_utc": None, "to_utc": None,
                       "wind": None, "vis": None, "wx": [], "cloud": []}
            out["changes"].append(current)
            continue
        fm = re.fullmatch(r"FM(\d{6})", token)
        if token in ("BECMG", "TEMPO", "INTER") or fm:
            kind = "FM" if fm else token
            if prob and current is not out["base"] and current["kind"] == prob and current["raw"] == prob:
                current["kind"], current["raw"] = f"{prob} {kind}", f"{prob} {token}"
            else:
                current = {"kind": kind, "raw": token, "from_utc": when(fm.group(1)) if fm else None,
                           "to_utc": None, "wind": None, "vis": None, "wx": [], "cloud": []}
                out["changes"].append(current)
            prob = None
            continue
        element = _element(token)
        if element:
            kind, group = element
            if kind in ("wx", "cloud"):
                current[kind].append(group)
            elif current[kind] is None:
                current[kind] = group
            if current is not out["base"]:
                current["raw"] += " " + token
    # An FM period runs to the next change of its own kind, else the end.
    fms = [c for c in out["changes"] if c["kind"] == "FM"]
    for this, nxt in zip(fms, fms[1:] + [None]):
        this["to_utc"] = nxt["from_utc"] if nxt else out["valid_to_utc"]
    for change in out["changes"]:
        word = next((TAF_CHANGE_WORD[k] for k in TAF_CHANGE_WORD if change["kind"].endswith(k)), "")
        chance = re.match(r"PROB(\d{2})", change["kind"])
        change["word"] = (f"{chance.group(1)}% 可能 " if chance else "") + word
        change["text"] = f"{change['word']} {_elements_text(change)}".strip()
    return out
