#!/usr/bin/env python3
"""Read-only cross-market anomaly monitor for Polymarket weather buckets.

This script joins four evidence lanes without writing to local state:

1. Gamma active events/markets.
2. CLOB books for YES tokens.
3. Data API recent public trades by condition id.
4. Local model/tick snapshots from data/archive.db when available.

The output is a single JSON snapshot that can be diffed or archived by the
caller. It never places orders and never mutates facts tables.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import math
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
DATA_API_TRADES_URL = "https://data-api.polymarket.com/trades"

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
MONTH_NAMES_BY_NUMBER = {value: key for key, value in MONTHS.items()}

MODEL_PRIORITY_FIELDS = (
    "q_bayes_posterior",
    "q_calibrated",
    "q_ensemble_qm_bucket",
    "q_raw",
)
MODEL_CONTEXT_HISTORY_LIMIT = 8
DEFAULT_ANOMALY_REPORT_DIR = ROOT / "data" / "reports" / "weather_anomaly"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return default


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def get_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    *,
    retries: int = 2,
    timeout: float = 20.0,
    sleep_s: float = 0.25,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # pragma: no cover - network guard
            last_error = exc
            if attempt < retries:
                time.sleep(sleep_s * (attempt + 1))
    raise RuntimeError(f"GET failed: {url} params={params}: {last_error}") from last_error


def context_hash(text: Any) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def split_context_sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return []
    parts = re.split(r"(?<=[.!?])\s+", normalized)
    return [part.strip() for part in parts if part.strip()]


def extract_temperature_mentions(text: str) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    pattern = re.compile(r"\b(\d{1,2})(?:\s*[–-]\s*(\d{1,2}))?\s*(?:°\s*C|Celsius)\b", re.I)
    for match in pattern.finditer(text or ""):
        low = int(match.group(1))
        high = int(match.group(2)) if match.group(2) else low
        mentions.append(
            {
                "text": match.group(0),
                "low_c": min(low, high),
                "high_c": max(low, high),
            }
        )
    return mentions


def plausible_peak_temperature_mentions(mentions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in mentions
        if (parse_float(item.get("low_c")) or 0.0) >= 10
        and (parse_float(item.get("high_c")) or 0.0) <= 50
    ]


def analyze_market_context_text(text: Any) -> dict[str, Any]:
    raw = str(text or "").strip()
    sentences = split_context_sentences(raw)
    temperature_mentions = extract_temperature_mentions(raw)
    source_patterns = [
        ("National Meteorological Center of China", r"National Meteorological Center of China"),
        ("China Meteorological Administration", r"China Meteorological Administration|CMA\b"),
        ("AccuWeather", r"AccuWeather"),
        ("timeanddate.com", r"timeanddate\.com"),
        ("GFS", r"\bGFS\b"),
        ("ECMWF", r"\bECMWF\b"),
        ("international agencies", r"international agencies"),
        ("global/regional models", r"global and regional models|regional models|forecast models|ensemble guidance|model consensus"),
        ("station observations", r"station data|observational trends|real-time observations|official readings|official.*measurements"),
    ]
    factor_patterns = [
        ("高压/稳定形势", r"high-pressure|stable atmospheric|stable .*conditions|subsidence|subtropical high"),
        ("低云量/有限云量", r"limited cloud|clear skies|mostly clear|reduced cloud"),
        ("云量/多云限制升温", r"mostly cloudy|overcast|widespread cloud"),
        ("轻风/通风弱", r"light winds|limited ventilation"),
        ("湿度/季风背景", r"humidity|humid|monsoon|East Asian monsoon|Southwest Monsoon"),
        ("对流/雷暴活动有限", r"limited convective|convective cooling|limited afternoon convection"),
        ("观测趋势支持", r"observational trends|early station data|real-time observations|official observations"),
        ("模型收敛", r"converged|consensus|limited spread|minimal deviation|tight clustering"),
    ]
    risk_patterns = [
        ("午后对流/雷暴", r"convective|thunderstorm|storm|shower"),
        ("云量突变/云隙增温", r"cloud breaks|unexpected cloud|clearing|additional heating|solar heating"),
        ("站点/传感器/校准差异", r"sensor|calibration|station|measurement variation|microclimate|official peak reading"),
        ("模型/实况更新", r"updated model runs|real-time observations|model shifts|observational updates"),
        ("读数修订/最终结算", r"revision|revisions|alter the official|final.*resolution|official.*determine"),
    ]

    sources = unique_preserve_order(
        label
        for label, pattern in source_patterns
        if re.search(pattern, raw, flags=re.I)
    )
    support_factors = unique_preserve_order(
        label
        for label, pattern in factor_patterns
        if re.search(pattern, raw, flags=re.I)
    )
    risk_factors = unique_preserve_order(
        label
        for label, pattern in risk_patterns
        if re.search(pattern, raw, flags=re.I)
    )

    target_sentences = [
        sentence
        for sentence in sentences
        if re.search(r"maximum|daily high|high of|outcome|market-implied|consensus|converged|near", sentence, re.I)
        and not re.search(r"typical|climatolog|range|outliers|could|if|unless", sentence, re.I)
    ]
    target_mentions: list[dict[str, Any]] = []
    for sentence in target_sentences:
        sentence_mentions = plausible_peak_temperature_mentions(extract_temperature_mentions(sentence))
        if sentence_mentions:
            target_mentions = sentence_mentions
            break
    if not target_mentions:
        target_mentions = plausible_peak_temperature_mentions(temperature_mentions)[:1]
    target_mentions = target_mentions[:3]
    target_range = None
    if target_mentions:
        target_range = {
            "low_c": min(item["low_c"] for item in target_mentions),
            "high_c": max(item["high_c"] for item in target_mentions),
        }
    target_text = ""
    if target_range:
        target_text = (
            f"{target_range['low_c']}°C"
            if target_range["low_c"] == target_range["high_c"]
            else f"{target_range['low_c']}-{target_range['high_c']}°C"
        )
    support_text = "、".join(support_factors[:3]) if support_factors else "未抽取到明确支撑因素"
    risk_text = "、".join(risk_factors[:3]) if risk_factors else "未抽取到明确剩余风险"
    reference_hint = (
        f"页面 Context 倾向 {target_text}；支撑：{support_text}；风险：{risk_text}。"
        if target_text
        else f"页面 Context 未给出明确目标温度；支撑：{support_text}；风险：{risk_text}。"
    )
    return {
        "available": bool(raw),
        "is_reference_only": True,
        "does_not_change_probability": True,
        "does_not_change_trade_decision": True,
        "target_temperature_range_c": target_range,
        "target_temperature_mentions": target_mentions,
        "all_temperature_mentions": temperature_mentions[:12],
        "source_mentions": sources[:8],
        "support_factors": support_factors[:8],
        "risk_factors": risk_factors[:8],
        "reference_hint": reference_hint,
        "risk_hint": risk_text,
    }


def parse_event_metadata(event: dict[str, Any]) -> dict[str, Any]:
    meta = event.get("eventMetadata") or {}
    if isinstance(meta, str):
        try:
            parsed = json.loads(meta)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return meta if isinstance(meta, dict) else {}


def gamma_market_context(event: dict[str, Any]) -> dict[str, Any]:
    meta = parse_event_metadata(event)
    text = str(meta.get("context_description") or "").strip()
    updated_at = str(meta.get("context_updated_at") or "").strip()
    return {
        "available": bool(text),
        "source": "gamma_event_metadata",
        "context_description": text,
        "context_updated_at": updated_at,
        "context_hash": context_hash(text),
        "analysis": analyze_market_context_text(text),
        "requires_regen": parse_bool(meta.get("context_requires_regen"), default=False),
    }


def fetch_page_market_context(
    session: requests.Session,
    slug: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    url = f"https://polymarket.com/event/{slug}"
    try:
        resp = session.get(
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 weather-market-anomaly-monitor/1.0"},
        )
        resp.raise_for_status()
    except Exception as exc:
        return {"available": False, "source": "polymarket_event_page", "error": str(exc)}
    candidates: list[dict[str, Any]] = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        resp.text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        raw = html_lib.unescape(match.group(1)).strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            if not isinstance(row, dict):
                continue
            headline = str(row.get("headline") or "")
            body = str(row.get("articleBody") or "").strip()
            if body and "Market Context" in headline:
                candidates.append(row)
    if not candidates:
        return {"available": False, "source": "polymarket_event_page", "error": "market_context_article_not_found"}
    candidates.sort(key=lambda row: str(row.get("dateModified") or row.get("datePublished") or ""), reverse=True)
    selected = candidates[0]
    text = str(selected.get("articleBody") or "").strip()
    return {
        "available": bool(text),
        "source": "polymarket_event_page_jsonld",
        "context_description": text,
        "context_updated_at": str(selected.get("dateModified") or selected.get("datePublished") or "").strip(),
        "context_hash": context_hash(text),
        "analysis": analyze_market_context_text(text),
        "headline": selected.get("headline"),
        "event_url": url,
    }


def latest_previous_page_contexts(report_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        paths = sorted(report_dir.glob("chengdu_weather_market_anomaly_*.json"), key=lambda p: p.stat().st_mtime)
    except Exception:
        return {}
    for path in reversed(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out: dict[str, dict[str, Any]] = {}
        for event in payload.get("events") or []:
            if not isinstance(event, dict):
                continue
            slug = str(event.get("slug") or "")
            ctx = event.get("page_market_context")
            if slug and isinstance(ctx, dict):
                out[slug] = ctx
        if out:
            return out
    return {}


def attach_previous_page_context(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    out = dict(current or {})
    prev = previous if isinstance(previous, dict) else {}
    current_hash = str(out.get("context_hash") or "")
    previous_hash = str(prev.get("context_hash") or "")
    out["previous_context_hash"] = previous_hash
    out["previous_context_updated_at"] = prev.get("context_updated_at")
    out["changed_vs_previous"] = bool(current_hash and previous_hash and current_hash != previous_hash)
    out["updated_at_changed_vs_previous"] = bool(
        out.get("context_updated_at")
        and prev.get("context_updated_at")
        and out.get("context_updated_at") != prev.get("context_updated_at")
    )
    return out


def slug_event_date(slug: str) -> str | None:
    m = re.search(r"-on-([a-z]+)-(\d{1,2})-(\d{4})$", slug or "")
    if not m:
        return None
    month = MONTHS.get(m.group(1).lower())
    if not month:
        return None
    return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"


def is_weather_temperature_event(event: dict[str, Any]) -> bool:
    slug = str(event.get("slug") or "").lower()
    title = str(event.get("title") or "").lower()
    desc = str(event.get("description") or "").lower()
    if slug.startswith("highest-temperature-in-"):
        return True
    if "highest temperature" in title and "wunderground" in desc:
        return True
    return False


def normalize_label(label: Any) -> str:
    text = str(label or "").lower()
    text = text.replace("\u00b0", "")
    text = text.replace("degrees", "")
    text = re.sub(r"\s+", "", text)
    return text


def bucket_sort_key(label: Any) -> tuple[float, str]:
    text = str(label or "")
    nums = [int(x) for x in re.findall(r"-?\d+", text)]
    if not nums:
        return (9999.0, text)
    base = float(nums[0]) if len(nums) == 1 else sum(nums) / len(nums)
    low = text.lower()
    if "or below" in low or "below" in low:
        base -= 0.25
    elif "or higher" in low or "higher" in low:
        base += 0.25
    return (base, text)


def field_max(row: dict[str, Any], fields: Iterable[str]) -> float:
    vals = [parse_float(row.get(field)) for field in fields]
    vals = [x for x in vals if x is not None]
    return max(vals) if vals else 0.0


def gamma_price(row: dict[str, Any]) -> float | None:
    prices = parse_json_list(row.get("outcomePrices"))
    if prices:
        return parse_float(prices[0])
    bid = parse_float(row.get("bestBid"))
    ask = parse_float(row.get("bestAsk"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return bid if bid is not None else ask


@dataclass
class MarketBucket:
    event_slug: str
    event_title: str
    event_end: datetime | None
    label: str
    condition_id: str
    yes_token: str | None
    no_token: str | None
    market_id: str | None
    gamma_yes: float | None
    gamma_bid: float | None
    gamma_ask: float | None
    liquidity: float
    volume: float
    volume24h: float
    active: bool
    closed: bool
    archived: bool


def extract_buckets(event: dict[str, Any]) -> list[MarketBucket]:
    out: list[MarketBucket] = []
    slug = str(event.get("slug") or "")
    title = str(event.get("title") or "")
    event_end = parse_dt(event.get("endDate") or event.get("eventDate"))
    for market in event.get("markets") or []:
        if not isinstance(market, dict):
            continue
        outcomes = parse_json_list(market.get("outcomes"))
        tokens = parse_json_list(market.get("clobTokenIds"))
        if len(outcomes) < 2 or str(outcomes[0]).lower() != "yes":
            continue
        condition_id = str(market.get("conditionId") or "")
        if not condition_id:
            continue
        label = str(market.get("groupItemTitle") or market.get("question") or "")
        out.append(
            MarketBucket(
                event_slug=slug,
                event_title=title,
                event_end=parse_dt(market.get("endDate")) or event_end,
                label=label,
                condition_id=condition_id,
                yes_token=str(tokens[0]) if len(tokens) >= 1 else None,
                no_token=str(tokens[1]) if len(tokens) >= 2 else None,
                market_id=str(market.get("id") or "") or None,
                gamma_yes=gamma_price(market),
                gamma_bid=parse_float(market.get("bestBid")),
                gamma_ask=parse_float(market.get("bestAsk")),
                liquidity=field_max(market, ("liquidityClob", "liquidityNum", "liquidity")),
                volume=field_max(market, ("volumeClob", "volumeNum", "volume")),
                volume24h=field_max(market, ("volume24hrClob", "volume24hr")),
                active=parse_bool(market.get("active"), default=True),
                closed=parse_bool(market.get("closed"), default=False),
                archived=parse_bool(market.get("archived"), default=False),
            )
        )
    return sorted(out, key=lambda b: bucket_sort_key(b.label))


def fetch_gamma_events(session: requests.Session, args: argparse.Namespace) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(args.gamma_max_pages):
        params = {
            "limit": args.gamma_limit,
            "offset": page * args.gamma_limit,
            "active": "true",
            "closed": "false",
            "order": "updatedAt",
            "ascending": "false",
        }
        try:
            batch = get_json(session, GAMMA_EVENTS_URL, params, sleep_s=args.request_sleep)
        except Exception:
            if args.city:
                break
            raise
        if not isinstance(batch, list) or not batch:
            break
        for event in batch:
            if not isinstance(event, dict) or not is_weather_temperature_event(event):
                continue
            slug = str(event.get("slug") or "")
            if args.slug_contains and args.slug_contains.lower() not in slug.lower():
                continue
            if args.city and f"-in-{args.city.lower()}-" not in slug.lower():
                continue
            if slug and slug not in seen:
                events.append(event)
                seen.add(slug)
        if args.max_events and len(events) >= args.max_events:
            return events[: args.max_events]
        time.sleep(args.request_sleep)
    if args.city:
        for event in fetch_direct_city_events(session, args):
            slug = str(event.get("slug") or "")
            if slug and slug not in seen:
                events.append(event)
                seen.add(slug)
            if args.max_events and len(events) >= args.max_events:
                return events[: args.max_events]
    return events


def fetch_direct_city_events(session: requests.Session, args: argparse.Namespace) -> list[dict[str, Any]]:
    city = args.city.strip().lower()
    if not city:
        return []
    now = utc_now()
    start_date = (now - timedelta(hours=args.allow_recently_ended_hours + 24)).date()
    end_date = (now + timedelta(hours=args.hours_to_end + 24)).date()
    days = (end_date - start_date).days + 1
    events: list[dict[str, Any]] = []
    for offset in range(max(days, 0)):
        date = start_date + timedelta(days=offset)
        month = MONTH_NAMES_BY_NUMBER[date.month]
        slug = f"highest-temperature-in-{city}-on-{month}-{date.day}-{date.year}"
        try:
            data = get_json(
                session,
                GAMMA_EVENTS_URL,
                {"slug": slug},
                retries=args.retries,
                sleep_s=args.request_sleep,
            )
        except Exception:
            continue
        if isinstance(data, list):
            for event in data:
                if isinstance(event, dict) and is_weather_temperature_event(event):
                    events.append(event)
        time.sleep(args.request_sleep)
    return events


def filter_event(event: dict[str, Any], buckets: list[MarketBucket], args: argparse.Namespace) -> bool:
    if not parse_bool(event.get("active"), default=True):
        return False
    if parse_bool(event.get("closed"), default=False) or parse_bool(event.get("archived"), default=False):
        return False
    end_dt = parse_dt(event.get("endDate") or event.get("eventDate"))
    if end_dt is None and buckets:
        end_dt = buckets[0].event_end
    if end_dt is not None:
        now = utc_now()
        if end_dt < now - timedelta(hours=args.allow_recently_ended_hours):
            return False
        if end_dt > now + timedelta(hours=args.hours_to_end):
            return False
    event_liq = field_max(event, ("liquidityClob", "liquidity"))
    event_vol = field_max(event, ("volumeClob", "volume"))
    if not event_liq:
        event_liq = sum(b.liquidity for b in buckets)
    if not event_vol:
        event_vol = sum(b.volume for b in buckets)
    return event_liq >= args.min_liquidity and event_vol >= args.min_volume


def top_n_depth(levels: list[dict[str, Any]], *, side: str, n: int = 5) -> tuple[float | None, float]:
    parsed: list[tuple[float, float]] = []
    for row in levels or []:
        price = parse_float(row.get("price"))
        size = parse_float(row.get("size"))
        if price is None or size is None:
            continue
        parsed.append((price, size))
    if not parsed:
        return None, 0.0
    reverse = side == "bid"
    parsed.sort(key=lambda x: x[0], reverse=reverse)
    best = parsed[0][0]
    depth = sum(size for _, size in parsed[:n])
    return best, depth


def fetch_book(session: requests.Session, token_id: str | None, args: argparse.Namespace) -> dict[str, Any]:
    if not token_id:
        return {"ok": False, "error": "missing_yes_token"}
    try:
        data = get_json(
            session,
            CLOB_BOOK_URL,
            {"token_id": token_id},
            retries=args.retries,
            sleep_s=args.request_sleep,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    bid, bid_depth = top_n_depth(bids, side="bid", n=5)
    ask, ask_depth = top_n_depth(asks, side="ask", n=5)
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    spread = ask - bid if bid is not None and ask is not None else None
    return {
        "ok": True,
        "timestamp": data.get("timestamp"),
        "best_bid": bid,
        "best_ask": ask,
        "mid": mid,
        "spread": spread,
        "bid_depth_top5": bid_depth,
        "ask_depth_top5": ask_depth,
        "bid_level_count": len(bids),
        "ask_level_count": len(asks),
    }


def normalize_trade(row: dict[str, Any], bucket: MarketBucket) -> dict[str, Any] | None:
    price = parse_float(row.get("price"))
    size = parse_float(row.get("size"))
    if price is None or size is None or price <= 0 or size <= 0:
        return None
    ts = parse_dt(row.get("timestamp"))
    if ts is None:
        return None
    side = str(row.get("side") or "").upper()
    outcome = str(row.get("outcome") or "")
    wallet = str(row.get("proxyWallet") or row.get("wallet") or row.get("funder") or "")
    if not wallet:
        return None
    return {
        "wallet": wallet.lower(),
        "side": side,
        "outcome": outcome,
        "price": price,
        "size": size,
        "notional": price * size,
        "timestamp": ts.isoformat(),
        "timestamp_epoch": int(ts.timestamp()),
        "transactionHash": row.get("transactionHash"),
        "asset": str(row.get("asset") or ""),
        "conditionId": str(row.get("conditionId") or bucket.condition_id),
        "bucket": bucket.label,
    }


def fetch_trades(
    session: requests.Session,
    bucket: MarketBucket,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    seen: set[tuple[Any, ...]] = set()
    while len(rows) < args.limit_trades:
        page_limit = min(args.data_api_page_limit, args.limit_trades - len(rows))
        params = {"market": bucket.condition_id, "limit": page_limit, "offset": offset}
        try:
            batch = get_json(
                session,
                DATA_API_TRADES_URL,
                params,
                retries=args.retries,
                sleep_s=args.request_sleep,
            )
        except Exception:
            break
        if not isinstance(batch, list) or not batch:
            break
        for raw in batch:
            if not isinstance(raw, dict):
                continue
            trade = normalize_trade(raw, bucket)
            if not trade:
                continue
            key = (
                trade.get("transactionHash"),
                trade["wallet"],
                trade["asset"],
                trade["timestamp_epoch"],
                trade["side"],
                trade["price"],
                trade["size"],
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(trade)
            if len(rows) >= args.limit_trades:
                break
        if len(batch) < page_limit:
            break
        offset += page_limit
        time.sleep(args.request_sleep)
    return rows


def wallet_share(wallet_notional: dict[str, float], top_n: int) -> float | None:
    total = sum(wallet_notional.values())
    if total <= 0:
        return None
    top = sorted(wallet_notional.values(), reverse=True)[:top_n]
    return sum(top) / total


def aggregate_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    wallet_notional: defaultdict[str, float] = defaultdict(float)
    by_wallet: defaultdict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    flows: defaultdict[str, float] = defaultdict(float)
    by_wallet_events: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        notional = float(trade["notional"])
        wallet = trade["wallet"]
        outcome = str(trade["outcome"]).lower()
        side = str(trade["side"]).lower()
        key = f"{outcome}_{side}"
        wallet_notional[wallet] += notional
        by_wallet[wallet][key] += notional
        flows[key] += notional
        by_wallet_events[wallet].append(trade)
    total = sum(wallet_notional.values())
    yes_support = flows["yes_buy"] + flows["no_sell"]
    yes_pressure = flows["yes_sell"] + flows["no_buy"]
    top_wallets = [
        {
            "wallet": wallet,
            "notional": value,
            "share": value / total if total > 0 else None,
            "flows": dict(by_wallet[wallet]),
        }
        for wallet, value in sorted(wallet_notional.items(), key=lambda item: item[1], reverse=True)[:5]
    ]
    wallet_flows = [
        {
            "wallet": wallet,
            "notional": value,
            "share": value / total if total > 0 else None,
            "flows": dict(by_wallet[wallet]),
        }
        for wallet, value in sorted(wallet_notional.items(), key=lambda item: item[1], reverse=True)
    ]
    flip_wallets: list[dict[str, Any]] = []
    for wallet, wallet_trades in by_wallet_events.items():
        ordered = sorted(wallet_trades, key=lambda t: t["timestamp_epoch"])
        no_buy_before = 0.0
        yes_buy_after = 0.0
        first_no_ts = None
        first_yes_after_ts = None
        for trade in ordered:
            if trade["outcome"].lower() == "no" and trade["side"].lower() == "buy":
                no_buy_before += trade["notional"]
                if first_no_ts is None:
                    first_no_ts = trade["timestamp"]
            elif (
                no_buy_before > 0
                and trade["outcome"].lower() == "yes"
                and trade["side"].lower() == "buy"
            ):
                yes_buy_after += trade["notional"]
                if first_yes_after_ts is None:
                    first_yes_after_ts = trade["timestamp"]
        if no_buy_before > 0 and yes_buy_after > 0:
            flip_wallets.append(
                {
                    "wallet": wallet,
                    "no_buy_before_yes_buy": no_buy_before,
                    "yes_buy_after": yes_buy_after,
                    "first_no_buy_at": first_no_ts,
                    "first_yes_buy_after_at": first_yes_after_ts,
                }
            )
    flip_wallets.sort(
        key=lambda row: min(row["no_buy_before_yes_buy"], row["yes_buy_after"]),
        reverse=True,
    )
    return {
        "trade_count": len(trades),
        "unique_wallets": len(wallet_notional),
        "total_notional": total,
        "flows": dict(flows),
        "yes_direction_net": yes_support - yes_pressure,
        "yes_support_notional": yes_support,
        "yes_pressure_notional": yes_pressure,
        "top1_wallet_share": wallet_share(wallet_notional, 1),
        "top3_wallet_share": wallet_share(wallet_notional, 3),
        "top5_wallet_share": wallet_share(wallet_notional, 5),
        "top_wallets": top_wallets,
        "wallet_flows": wallet_flows,
        "possible_no_pressure_then_yes_entry_wallets": flip_wallets[:5],
    }


def compact_trade_aggregate(agg: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_count": agg.get("trade_count", 0),
        "unique_wallets": agg.get("unique_wallets", 0),
        "total_notional": agg.get("total_notional", 0.0),
        "yes_direction_net": agg.get("yes_direction_net", 0.0),
        "yes_support_notional": agg.get("yes_support_notional", 0.0),
        "yes_pressure_notional": agg.get("yes_pressure_notional", 0.0),
        "top1_wallet_share": agg.get("top1_wallet_share"),
        "top3_wallet_share": agg.get("top3_wallet_share"),
        "top5_wallet_share": agg.get("top5_wallet_share"),
        "top_wallets": (agg.get("top_wallets") or [])[:5],
    }


def aggregate_recent_trade_windows(
    trades: list[dict[str, Any]],
    *,
    now: datetime,
    windows_minutes: Iterable[int] = (5, 30),
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for trade in trades:
        ts = parse_dt(trade.get("timestamp"))
        if ts is not None:
            parsed.append((ts, trade))
    for minutes in windows_minutes:
        cutoff = now - timedelta(minutes=minutes)
        window_trades = [trade for ts, trade in parsed if ts >= cutoff]
        out[f"{minutes}m"] = compact_trade_aggregate(aggregate_trades(window_trades))
    return out


def load_local_model(conn: sqlite3.Connection | None, slug: str) -> dict[str, Any]:
    if conn is None:
        return {"available": False, "reason": "db_unavailable", "rows": {}}
    try:
        history_rows = conn.execute(
            """
            SELECT payload_json, created_at
            FROM event_runs
            WHERE slug = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (slug, MODEL_CONTEXT_HISTORY_LIMIT),
        ).fetchall()
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"query_failed:{exc}", "rows": {}}
    if not history_rows:
        return {"available": False, "reason": "no_event_runs_row", "rows": {}}

    def extract_snapshot(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any] | None:
        try:
            payload = json.loads(row[0] or "{}")
        except json.JSONDecodeError:
            return None
        rows = payload.get("rows") or []
        model_rows_local: dict[str, dict[str, Any]] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            label = item.get("outcome")
            if not label:
                continue
            q_primary = None
            q_primary_field = None
            for field in MODEL_PRIORITY_FIELDS:
                q_val = parse_float(item.get(field))
                if q_val is not None:
                    q_primary = q_val
                    q_primary_field = field
                    break
            model_rows_local[normalize_label(label)] = {
                "outcome": label,
                "q_primary": q_primary,
                "q_primary_field": q_primary_field,
                "q_bayes_posterior": parse_float(item.get("q_bayes_posterior")),
                "q_calibrated": parse_float(item.get("q_calibrated")),
                "q_ensemble_qm_bucket": parse_float(item.get("q_ensemble_qm_bucket")),
                "q_physical_likelihood": parse_float(item.get("q_bayes_physical_likelihood_prob")),
                "q_entry": parse_float(item.get("q_entry")),
                "q_quote": parse_float(item.get("q_quote")),
                "weather_likelihood_feature_quality": item.get("weather_likelihood_feature_quality"),
                "weather_likelihood_feature_count": item.get("weather_likelihood_feature_count"),
            }
        return {
            "created_at": row[1],
            "payload": payload,
            "rows": model_rows_local,
            "weather_likelihood_refreshed_at": payload.get("weather_likelihood_refreshed_at"),
            "weather_likelihood_feature_quality": payload.get("weather_likelihood_feature_quality"),
            "weather_likelihood_feature_count": payload.get("weather_likelihood_feature_count"),
            "market_updated_at": payload.get("market_updated_at"),
            "model_cache_age_sec": payload.get("model_cache_age_sec"),
        }

    snapshots = [snap for row in history_rows if (snap := extract_snapshot(row)) is not None]
    if not snapshots:
        return {"available": False, "reason": "payload_json_decode_failed", "rows": {}}
    latest = snapshots[0]
    previous = snapshots[1] if len(snapshots) > 1 else None
    model_rows = latest["rows"]

    def compact_climo_regime_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
        features = payload.get("climo_regime_features") if isinstance(payload.get("climo_regime_features"), Mapping) else {}
        probs = payload.get("q_bayes_climo_regime_prob") if isinstance(payload.get("q_bayes_climo_regime_prob"), Mapping) else {}
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        row0 = next((row for row in rows if isinstance(row, Mapping)), {})
        evidence = parse_float(payload.get("q_bayes_climo_regime_evidence"))
        evidence_raw = parse_float(payload.get("q_bayes_climo_regime_evidence_raw"))
        pp_cap = parse_float(payload.get("q_bayes_climo_regime_pp_cap"))
        pp_max_delta = parse_float(payload.get("q_bayes_climo_regime_pp_max_delta"))
        pp_cap_stage = str(payload.get("q_bayes_climo_regime_pp_cap_stage") or "")
        primary_regime = str(payload.get("q_bayes_climo_regime_primary_regime") or "")
        high_temp_gate = payload.get("q_bayes_climo_regime_high_temp_gate")
        if evidence is None and isinstance(row0, Mapping):
            evidence = parse_float(row0.get("q_bayes_climo_regime_evidence"))
        if evidence_raw is None and isinstance(row0, Mapping):
            evidence_raw = parse_float(row0.get("q_bayes_climo_regime_evidence_raw"))
        if pp_cap is None and isinstance(row0, Mapping):
            pp_cap = parse_float(row0.get("q_bayes_climo_regime_pp_cap"))
        if pp_max_delta is None and isinstance(row0, Mapping):
            pp_max_delta = parse_float(row0.get("q_bayes_climo_regime_pp_max_delta"))
        if not pp_cap_stage and isinstance(row0, Mapping):
            pp_cap_stage = str(row0.get("q_bayes_climo_regime_pp_cap_stage") or "")
        if not primary_regime and isinstance(features, Mapping):
            primary_regime = str(features.get("primary_regime_label") or "")
        if not isinstance(high_temp_gate, Mapping) and isinstance(features, Mapping):
            high_temp_gate = features.get("high_temp_potential_gate")
        top_label = ""
        top_prob = None
        if isinstance(probs, Mapping) and probs:
            top_label = max(probs, key=lambda key: parse_float(probs.get(key)) or 0.0)
            top_prob = parse_float(probs.get(top_label))
        return {
            "available": bool(features),
            "display_label": "气候/环流弱参考，不覆盖实况温度轨迹",
            "potential_label": "支持升温潜力 ≠ 已兑现升温",
            "trajectory_priority_label": "实况温度轨迹优先",
            "source": features.get("source") if isinstance(features, Mapping) else "",
            "updated_at": features.get("updated_at") if isinstance(features, Mapping) else "",
            "status": features.get("status") if isinstance(features, Mapping) else "",
            "primary_regime_label": primary_regime,
            "regime_source": features.get("regime_source") if isinstance(features, Mapping) else "",
            "regime_updated_at": features.get("regime_updated_at") if isinstance(features, Mapping) else "",
            "regime_confidence": parse_float(features.get("regime_confidence")) if isinstance(features, Mapping) else None,
            "climo_tmax_mean_c": parse_float(features.get("climo_tmax_mean_c")) if isinstance(features, Mapping) else None,
            "climo_tmax_p50_c": parse_float(features.get("climo_tmax_p50_c")) if isinstance(features, Mapping) else None,
            "climo_tmax_p90_c": parse_float(features.get("climo_tmax_p90_c")) if isinstance(features, Mapping) else None,
            "climo_tmax_p95_c": parse_float(features.get("climo_tmax_p95_c")) if isinstance(features, Mapping) else None,
            "dewpoint_anomaly_c": parse_float(features.get("dewpoint_anomaly_c")) if isinstance(features, Mapping) else None,
            "monsoon_humidity_regime": features.get("monsoon_humidity_regime") if isinstance(features, Mapping) else "",
            "subtropical_high_regime": features.get("subtropical_high_regime") if isinstance(features, Mapping) else "",
            "basin_ventilation_index": parse_float(features.get("basin_ventilation_index")) if isinstance(features, Mapping) else None,
            "basin_ventilation_regime": features.get("basin_ventilation_regime") if isinstance(features, Mapping) else "",
            "evidence": evidence,
            "evidence_raw": evidence_raw,
            "pp_cap": pp_cap,
            "pp_max_delta": pp_max_delta,
            "pp_cap_stage": pp_cap_stage,
            "high_temp_potential_gate": dict(high_temp_gate) if isinstance(high_temp_gate, Mapping) else {},
            "temperature_trajectory_priority": True,
            "top_label": top_label,
            "top_prob": top_prob,
        }

    def snapshot_context_for_label(label_key: str, current_row: dict[str, Any]) -> dict[str, Any]:
        prev_row = previous["rows"].get(label_key) if previous else None
        q_now = parse_float(current_row.get("q_primary"))
        q_prev = parse_float((prev_row or {}).get("q_primary"))
        phys_now = parse_float(current_row.get("q_physical_likelihood"))
        phys_prev = parse_float((prev_row or {}).get("q_physical_likelihood"))
        latest_weather_at = str(latest.get("weather_likelihood_refreshed_at") or "")
        previous_weather_at = str((previous or {}).get("weather_likelihood_refreshed_at") or "")
        latest_quality = str(
            current_row.get("weather_likelihood_feature_quality")
            or latest.get("weather_likelihood_feature_quality")
            or ""
        )
        previous_quality = str(
            (prev_row or {}).get("weather_likelihood_feature_quality")
            or (previous or {}).get("weather_likelihood_feature_quality")
            or ""
        )
        latest_count = parse_float(
            current_row.get("weather_likelihood_feature_count")
            or latest.get("weather_likelihood_feature_count")
        )
        previous_count = parse_float(
            (prev_row or {}).get("weather_likelihood_feature_count")
            or (previous or {}).get("weather_likelihood_feature_count")
        )
        return {
            "latest_created_at": latest.get("created_at"),
            "previous_created_at": (previous or {}).get("created_at"),
            "q_primary": q_now,
            "previous_q_primary": q_prev,
            "q_primary_delta": q_now - q_prev if q_now is not None and q_prev is not None else None,
            "q_primary_field": current_row.get("q_primary_field"),
            "q_physical_likelihood": phys_now,
            "previous_q_physical_likelihood": phys_prev,
            "q_physical_likelihood_delta": (
                phys_now - phys_prev if phys_now is not None and phys_prev is not None else None
            ),
            "weather_likelihood_refreshed_at": latest_weather_at,
            "previous_weather_likelihood_refreshed_at": previous_weather_at,
            "weather_likelihood_refreshed_changed": bool(
                latest_weather_at and previous_weather_at and latest_weather_at != previous_weather_at
            ),
            "weather_likelihood_feature_quality": latest_quality,
            "previous_weather_likelihood_feature_quality": previous_quality,
            "weather_likelihood_feature_quality_changed": bool(
                latest_quality and previous_quality and latest_quality != previous_quality
            ),
            "weather_likelihood_feature_count": latest_count,
            "previous_weather_likelihood_feature_count": previous_count,
            "weather_likelihood_feature_count_delta": (
                latest_count - previous_count
                if latest_count is not None and previous_count is not None
                else None
            ),
            "model_cache_age_sec": latest.get("model_cache_age_sec"),
            "market_updated_at": latest.get("market_updated_at"),
        }
    context_by_label = {
        label_key: snapshot_context_for_label(label_key, row_data)
        for label_key, row_data in model_rows.items()
    }
    return {
        "available": True,
        "created_at": latest.get("created_at"),
        "rows": model_rows,
        "context_by_label": context_by_label,
        "history_count": len(snapshots),
        "city_most_likely_label": latest["payload"].get("city_most_likely_label"),
        "city_most_likely_q": latest["payload"].get("city_most_likely_q"),
        "climo_regime_summary": compact_climo_regime_summary(latest["payload"]),
    }


def load_recent_ticks(
    conn: sqlite3.Connection | None,
    slug: str,
    *,
    lookback_minutes: int = 45,
) -> dict[str, list[dict[str, Any]]]:
    if conn is None:
        return {}
    cutoff = (utc_now() - timedelta(minutes=lookback_minutes)).isoformat()
    try:
        rows = conn.execute(
            """
            SELECT outcome, mid, best_bid, best_ask, bid_depth_topN, ask_depth_topN, created_at
            FROM event_market_ticks
            WHERE slug = ?
              AND upper(side) = 'YES'
              AND created_at >= ?
            ORDER BY created_at ASC
            """,
            (slug, cutoff),
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[normalize_label(row[0])].append(
            {
                "outcome": row[0],
                "mid": parse_float(row[1]),
                "best_bid": parse_float(row[2]),
                "best_ask": parse_float(row[3]),
                "bid_depth_top5": parse_float(row[4]),
                "ask_depth_top5": parse_float(row[5]),
                "created_at": row[6],
            }
        )
    return dict(out)


def jump_from_ticks(ticks: list[dict[str, Any]], minutes: int) -> float | None:
    valid = [t for t in ticks if parse_float(t.get("mid")) is not None and parse_dt(t.get("created_at"))]
    if len(valid) < 2:
        return None
    latest = valid[-1]
    latest_dt = parse_dt(latest["created_at"])
    if latest_dt is None:
        return None
    cutoff = latest_dt - timedelta(minutes=minutes)
    base = None
    for row in valid:
        row_dt = parse_dt(row.get("created_at"))
        if row_dt is not None and row_dt >= cutoff:
            base = row
            break
    if base is None:
        base = valid[0]
    latest_mid = parse_float(latest.get("mid"))
    base_mid = parse_float(base.get("mid"))
    if latest_mid is None or base_mid is None:
        return None
    return latest_mid - base_mid


def current_change_from_ticks(
    ticks: list[dict[str, Any]],
    minutes: int,
    current_mid: float | None,
) -> float | None:
    if current_mid is None:
        return None
    valid = [t for t in ticks if parse_float(t.get("mid")) is not None and parse_dt(t.get("created_at"))]
    if not valid:
        return None
    valid.sort(key=lambda t: parse_dt(t["created_at"]) or datetime.min.replace(tzinfo=timezone.utc))
    cutoff = utc_now() - timedelta(minutes=minutes)
    base = None
    for row in valid:
        row_dt = parse_dt(row.get("created_at"))
        if row_dt is not None and row_dt <= cutoff:
            base = row
    if base is None:
        base = valid[0]
    base_mid = parse_float(base.get("mid"))
    if base_mid is None:
        return None
    return current_mid - base_mid


def core_bucket_price_probability_change(
    bucket_results: list[dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    changed = []
    for bucket in bucket_results:
        change_5m = parse_float(bucket.get("price_change_5m"))
        change_30m = parse_float(bucket.get("price_change_30m"))
        reasons = []
        if change_5m is not None and abs(change_5m) >= args.core_bucket_price_change_5m:
            reasons.append("core_bucket_yes_price_change_5m")
        if change_30m is not None and abs(change_30m) >= args.core_bucket_price_change_30m:
            reasons.append("core_bucket_yes_price_change_30m")
        if reasons:
            changed.append(
                {
                    "label": bucket.get("label"),
                    "market_mid": bucket.get("market_mid"),
                    "price_change_5m": change_5m,
                    "price_change_30m": change_30m,
                    "tick_count_45m": bucket.get("tick_count_45m"),
                    "selection_reasons": bucket.get("selection_reasons"),
                    "trigger_reasons": reasons,
                }
            )
    changed.sort(
        key=lambda row: max(
            abs(row.get("price_change_5m") or 0.0),
            abs(row.get("price_change_30m") or 0.0),
        ),
        reverse=True,
    )
    return {
        "triggered": bool(changed),
        "metric": "core_bucket_market_implied_probability_yes_price_change",
        "thresholds": {
            "price_change_5m": args.core_bucket_price_change_5m,
            "price_change_30m": args.core_bucket_price_change_30m,
        },
        "changed_bucket_count": len(changed),
        "changed_buckets": changed[:10],
    }

def select_core_bucket_indexes(
    buckets: list[MarketBucket],
    model_rows: dict[str, dict[str, Any]],
    *,
    adjacent: int,
) -> tuple[set[int], dict[int, list[str]]]:
    reasons: defaultdict[int, list[str]] = defaultdict(list)
    core: set[int] = set()
    if buckets:
        price_idx = max(
            range(len(buckets)),
            key=lambda i: buckets[i].gamma_yes if buckets[i].gamma_yes is not None else -1.0,
        )
        for idx in range(max(0, price_idx - adjacent), min(len(buckets), price_idx + adjacent + 1)):
            core.add(idx)
            reasons[idx].append("near_price_top")
        reasons[price_idx].append("price_top")
    model_candidates: list[tuple[int, float]] = []
    for idx, bucket in enumerate(buckets):
        row = model_rows.get(normalize_label(bucket.label))
        q = parse_float((row or {}).get("q_primary"))
        if q is not None:
            model_candidates.append((idx, q))
    if model_candidates:
        model_idx = max(model_candidates, key=lambda item: item[1])[0]
        for idx in range(max(0, model_idx - adjacent), min(len(buckets), model_idx + adjacent + 1)):
            core.add(idx)
            reasons[idx].append("near_model_top")
        reasons[model_idx].append("model_top")
    return core, dict(reasons)


def cross_bucket_wallet_paths(bucket_results: list[dict[str, Any]]) -> dict[str, Any]:
    wallet_bucket: defaultdict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    for bucket in bucket_results:
        label = bucket["label"]
        for wallet in bucket.get("trade_aggregate", {}).get("wallet_flows", []):
            wallet_bucket[wallet["wallet"]][label] = dict(wallet.get("flows") or {})
    paths = []
    max_opposite = 0.0
    for wallet, by_bucket in wallet_bucket.items():
        labels = list(by_bucket.keys())
        if len(labels) < 2:
            continue
        opposite = 0.0
        total = 0.0
        for flows in by_bucket.values():
            yes_buy = flows.get("yes_buy", 0.0) + flows.get("no_sell", 0.0)
            no_buy = flows.get("no_buy", 0.0) + flows.get("yes_sell", 0.0)
            total += yes_buy + no_buy
        for i, label_a in enumerate(labels):
            flows_a = by_bucket[label_a]
            yes_a = flows_a.get("yes_buy", 0.0) + flows_a.get("no_sell", 0.0)
            no_a = flows_a.get("no_buy", 0.0) + flows_a.get("yes_sell", 0.0)
            for label_b in labels[i + 1 :]:
                flows_b = by_bucket[label_b]
                yes_b = flows_b.get("yes_buy", 0.0) + flows_b.get("no_sell", 0.0)
                no_b = flows_b.get("no_buy", 0.0) + flows_b.get("yes_sell", 0.0)
                opposite = max(opposite, min(yes_a, no_b), min(no_a, yes_b))
        max_opposite = max(max_opposite, opposite)
        paths.append(
            {
                "wallet": wallet,
                "bucket_count": len(labels),
                "buckets": labels,
                "total_directional_notional": total,
                "opposite_cross_bucket_notional": opposite,
            }
        )
    paths.sort(key=lambda row: row["opposite_cross_bucket_notional"], reverse=True)
    return {
        "wallet_path_count": len(paths),
        "max_opposite_cross_bucket_notional": max_opposite,
        "top_paths": paths[:10],
    }


def timestamp_age_minutes(value: Any, *, now: datetime | None = None) -> float | None:
    dt = parse_dt(value)
    if dt is None:
        return None
    ref = now or utc_now()
    return max(0.0, (ref - dt).total_seconds() / 60.0)


def classify_bucket(
    result: dict[str, Any],
    *,
    gap_threshold: float,
    jump_threshold: float,
    wallet_share_threshold: float,
    thin_depth_threshold: float,
    turnover_threshold: float,
    cross_wallet_threshold: float,
    core_price_change_5m: float,
    book_inducement_5m_max_notional: float,
    book_inducement_30m_max_notional: float,
    wallet_flow_min_notional: float,
    market_context_model_prob_change: float = 0.02,
    market_context_physical_prob_change: float = 0.015,
    market_context_update_window_min: float = 45.0,
) -> dict[str, Any]:
    market_mid = parse_float(result.get("market_mid"))
    model_q = parse_float(result.get("model", {}).get("q_primary"))
    gap = market_mid - model_q if market_mid is not None and model_q is not None else None
    physical_q = parse_float(result.get("model", {}).get("q_physical_likelihood"))
    physical_gap = market_mid - physical_q if market_mid is not None and physical_q is not None else None
    agg = result.get("trade_aggregate") or {}
    book = result.get("book") or {}
    change_5m = parse_float(result.get("price_change_5m"))
    change_30m = parse_float(result.get("price_change_30m"))
    jump_30m = change_30m if change_30m is not None else parse_float(result.get("price_jump_30m"))
    top3_share = parse_float(agg.get("top3_wallet_share"))
    bid_depth = parse_float(book.get("bid_depth_top5"))
    ask_depth = parse_float(book.get("ask_depth_top5"))
    min_depth = None
    if bid_depth is not None and ask_depth is not None:
        min_depth = min(bid_depth, ask_depth)
    elif bid_depth is not None:
        min_depth = bid_depth
    elif ask_depth is not None:
        min_depth = ask_depth
    liquidity = parse_float(result.get("liquidity")) or 0.0
    turnover = (parse_float(agg.get("total_notional")) or 0.0) / liquidity if liquidity > 0 else None
    yes_direction_net = parse_float(agg.get("yes_direction_net")) or 0.0
    cross_opposite = parse_float(result.get("event_cross_bucket_max_opposite")) or 0.0
    no_then_yes = result.get("trade_aggregate", {}).get("possible_no_pressure_then_yes_entry_wallets") or []
    recent_windows = result.get("recent_trade_windows") or {}
    recent_5m = recent_windows.get("5m") or {}
    recent_30m = recent_windows.get("30m") or {}
    recent_5m_notional = parse_float(recent_5m.get("total_notional")) or 0.0
    recent_30m_notional = parse_float(recent_30m.get("total_notional")) or 0.0
    recent_30m_top3_share = parse_float(recent_30m.get("top3_wallet_share"))
    total_notional = parse_float(agg.get("total_notional")) or 0.0
    model_context = result.get("model_context") if isinstance(result.get("model_context"), dict) else {}
    page_market_context = (
        result.get("page_market_context")
        if isinstance(result.get("page_market_context"), dict)
        else {}
    )
    model_delta = parse_float(model_context.get("q_primary_delta"))
    physical_delta = parse_float(model_context.get("q_physical_likelihood_delta"))
    feature_count_delta = parse_float(model_context.get("weather_likelihood_feature_count_delta"))
    model_age_min = timestamp_age_minutes(model_context.get("latest_created_at"))
    weather_age_min = timestamp_age_minutes(model_context.get("weather_likelihood_refreshed_at"))
    model_recent = model_age_min is not None and model_age_min <= market_context_update_window_min
    weather_recent = weather_age_min is not None and weather_age_min <= market_context_update_window_min
    page_context_age_min = timestamp_age_minutes(page_market_context.get("context_updated_at"))
    page_context_recent = (
        page_context_age_min is not None
        and page_context_age_min <= market_context_update_window_min
    )
    market_price_jump = (
        (change_5m is not None and abs(change_5m) >= core_price_change_5m)
        or (jump_30m is not None and abs(jump_30m) >= jump_threshold)
    )

    triggers: dict[str, Any] = {}
    triggers["market_model_gap_ge_10pp"] = gap is not None and abs(gap) >= gap_threshold
    triggers["physical_gap_ge_10pp"] = physical_gap is not None and abs(physical_gap) >= gap_threshold
    triggers["core_bucket_price_change_5m_ge_3pp"] = (
        change_5m is not None and abs(change_5m) >= core_price_change_5m
    )
    triggers["price_jump_30m_ge_5pp"] = jump_30m is not None and abs(jump_30m) >= jump_threshold
    triggers["top3_wallet_share_ge_40pct"] = (
        top3_share is not None and top3_share >= wallet_share_threshold
    )
    triggers["recent_top3_wallet_share_ge_40pct"] = (
        recent_30m_top3_share is not None and recent_30m_top3_share >= wallet_share_threshold
    )
    triggers["recent_30m_trade_notional_ge_wallet_flow_min"] = (
        recent_30m_notional >= wallet_flow_min_notional
    )
    triggers["wallet_concentration_with_recent_flow"] = (
        triggers["recent_top3_wallet_share_ge_40pct"]
        and triggers["recent_30m_trade_notional_ge_wallet_flow_min"]
    )
    triggers["thin_top5_depth_lt_100"] = min_depth is not None and min_depth < thin_depth_threshold
    triggers["same_wallet_cross_bucket_opposite_ge_50"] = cross_opposite >= cross_wallet_threshold
    triggers["bucket_volume_liquidity_ge_30pct"] = (
        turnover is not None and turnover >= turnover_threshold
    )
    triggers["same_wallet_no_then_yes_entry"] = bool(no_then_yes)
    triggers["price_change_5m_with_low_recent_trade_flow"] = (
        triggers["core_bucket_price_change_5m_ge_3pp"]
        and recent_5m_notional <= book_inducement_5m_max_notional
    )
    triggers["price_jump_30m_with_low_recent_trade_flow"] = (
        triggers["price_jump_30m_ge_5pp"]
        and recent_30m_notional <= book_inducement_30m_max_notional
    )
    triggers["price_jump_without_recent_trade_flow"] = (
        triggers["price_change_5m_with_low_recent_trade_flow"]
        or triggers["price_jump_30m_with_low_recent_trade_flow"]
    )
    triggers["market_context_price_jump_watch"] = market_price_jump
    triggers["market_context_page_text_changed"] = bool(page_market_context.get("changed_vs_previous"))
    triggers["market_context_page_updated_at_changed"] = bool(
        page_market_context.get("updated_at_changed_vs_previous")
    )
    triggers["market_context_page_recently_updated"] = bool(page_context_recent)
    triggers["market_context_update_watch"] = bool(
        market_price_jump
        and (
            triggers["market_context_page_text_changed"]
            or triggers["market_context_page_updated_at_changed"]
            or triggers["market_context_page_recently_updated"]
        )
    )
    triggers["internal_context_model_snapshot_recent"] = bool(model_recent)
    triggers["internal_context_model_prob_shift_ge_threshold"] = (
        model_delta is not None and abs(model_delta) >= market_context_model_prob_change
    )
    triggers["internal_context_physical_likelihood_shift_ge_threshold"] = (
        physical_delta is not None and abs(physical_delta) >= market_context_physical_prob_change
    )
    triggers["internal_context_weather_likelihood_refreshed"] = bool(
        model_context.get("weather_likelihood_refreshed_changed") or weather_recent
    )
    triggers["internal_context_weather_feature_set_changed"] = bool(
        model_context.get("weather_likelihood_feature_quality_changed")
        or (feature_count_delta is not None and abs(feature_count_delta) >= 1)
    )
    internal_context_explainers = [
        key
        for key in (
            "internal_context_model_snapshot_recent",
            "internal_context_model_prob_shift_ge_threshold",
            "internal_context_physical_likelihood_shift_ge_threshold",
            "internal_context_weather_likelihood_refreshed",
            "internal_context_weather_feature_set_changed",
        )
        if triggers.get(key)
    ]
    triggers["internal_repricing_context_watch"] = bool(
        triggers["market_context_price_jump_watch"] and internal_context_explainers
    )

    classes: list[str] = []
    advice: list[str] = []
    if triggers["market_context_update_watch"]:
        classes.append("page_market_context_update_watch")
        advice.append("separate_market_context_from_flow_manipulation")
        advice.append("audit_polymarket_page_context_before_flow_interpretation")
    if triggers["internal_repricing_context_watch"]:
        classes.append("internal_repricing_context_watch")
        advice.append("separate_information_update_from_flow_manipulation")
        advice.append("audit_model_data_physics_update_before_flow_interpretation")
    if triggers["price_jump_without_recent_trade_flow"]:
        classes.append("thin_book_price_inducement")
        advice.append("do_not_infer_large_money_from_price_jump")
        advice.append("observe_until_book_rebuilds")
        advice.append("no_market_orders")
    if triggers["market_model_gap_ge_10pp"] and gap is not None:
        classes.append("severe_model_market_divergence")
        if gap > 0:
            if triggers["price_jump_without_recent_trade_flow"]:
                advice.append("fade_only_if_trade_flow_confirms")
            elif yes_direction_net < 0:
                classes.append("large_money_rational_reverse_trading")
                advice.append("fade_overpriced_bucket_passive_limits_only")
            else:
                classes.append("suspected_pump_or_inducement")
                advice.append("avoid_following_pumped_yes")
        else:
            if triggers["same_wallet_no_then_yes_entry"]:
                classes.append("suspected_pressure_accumulation")
                advice.append("watch_for_wallet_flip_before_following")
            else:
                advice.append("track_true_high_probability_bucket")
    if triggers["thin_top5_depth_lt_100"]:
        classes.append("thin_book_noise")
        advice.append("no_market_orders")
    if (triggers["core_bucket_price_change_5m_ge_3pp"] or triggers["price_jump_30m_ge_5pp"]) and not classes:
        classes.append("thin_book_noise")
        advice.append("observe_until_book_rebuilds")
    if triggers["wallet_concentration_with_recent_flow"]:
        classes.append("wallet_concentration_trade_flow")
        advice.append("inspect_top_wallet_continuation")
        advice.append("watch_wallet_flow_not_price")
    if (
        triggers["wallet_concentration_with_recent_flow"]
        and yes_direction_net > 0
        and gap is not None
        and gap > 0
    ):
        if "suspected_pump_or_inducement" not in classes:
            classes.append("suspected_pump_or_inducement")
        advice.append("do_not_chase_yes")
    if triggers["same_wallet_no_then_yes_entry"] and gap is not None and gap < 0:
        if "suspected_pressure_accumulation" not in classes:
            classes.append("suspected_pressure_accumulation")
        advice.append("passive_follow_only_after_retest")
    if not classes:
        classes.append("normal")
        advice.append("observe_or_use_normal_edge_rules")
    risk_score = 0.0
    if triggers["market_model_gap_ge_10pp"] and gap is not None:
        risk_score += min(30.0, abs(gap) / max(gap_threshold, 1e-9) * 12.0)
    if triggers["physical_gap_ge_10pp"]:
        risk_score += 10.0
    if triggers["core_bucket_price_change_5m_ge_3pp"]:
        risk_score += 12.0
    if triggers["price_jump_30m_ge_5pp"]:
        risk_score += 10.0
    if triggers["wallet_concentration_with_recent_flow"]:
        risk_score += 12.0
    if triggers["thin_top5_depth_lt_100"]:
        risk_score += 10.0
    if triggers["same_wallet_cross_bucket_opposite_ge_50"]:
        risk_score += 8.0
    if triggers["bucket_volume_liquidity_ge_30pct"]:
        risk_score += 8.0
    if triggers["same_wallet_no_then_yes_entry"]:
        risk_score += 8.0
    if triggers["price_jump_without_recent_trade_flow"]:
        risk_score += 14.0
    if triggers["market_context_update_watch"]:
        risk_score += 8.0
    if triggers["internal_repricing_context_watch"]:
        risk_score += 6.0

    return {
        "market_model_gap": gap,
        "physical_gap": physical_gap,
        "turnover_to_liquidity": turnover,
        "recent_trade_notional_5m": recent_5m_notional,
        "recent_trade_notional_30m": recent_30m_notional,
        "risk_score": min(100.0, risk_score),
        "triggers": triggers,
        "classification": sorted(set(classes)),
        "advice": sorted(set(advice)),
        "book_inducement": {
            "triggered": triggers["price_jump_without_recent_trade_flow"],
            "price_change_5m": change_5m,
            "price_change_30m": change_30m,
            "recent_trade_notional_5m": recent_5m_notional,
            "recent_trade_notional_30m": recent_30m_notional,
            "max_trade_notional_5m": book_inducement_5m_max_notional,
            "max_trade_notional_30m": book_inducement_30m_max_notional,
        },
        "wallet_concentration": {
            "triggered": triggers["wallet_concentration_with_recent_flow"],
            "recent_30m_top3_wallet_share": recent_30m_top3_share,
            "recent_trade_notional_30m": recent_30m_notional,
            "total_top3_wallet_share": top3_share,
            "total_trade_notional": total_notional,
            "min_trade_notional_30m": wallet_flow_min_notional,
        },
        "market_context": {
            "triggered": triggers["market_context_update_watch"],
            "is_warning_only": True,
            "does_not_change_probability": True,
            "does_not_change_trade_decision": True,
            "source": page_market_context.get("source"),
            "context_updated_at": page_market_context.get("context_updated_at"),
            "context_hash": page_market_context.get("context_hash"),
            "previous_context_hash": page_market_context.get("previous_context_hash"),
            "changed_vs_previous": page_market_context.get("changed_vs_previous"),
            "updated_at_changed_vs_previous": page_market_context.get("updated_at_changed_vs_previous"),
            "context_age_min": page_context_age_min,
            "context_excerpt": str(page_market_context.get("context_description") or "")[:500],
            "analysis": page_market_context.get("analysis"),
            "price_change_5m": change_5m,
            "price_change_30m": change_30m,
        },
        "internal_repricing_context": {
            "triggered": triggers["internal_repricing_context_watch"],
            "is_warning_only": True,
            "does_not_change_probability": True,
            "does_not_change_trade_decision": True,
            "price_change_5m": change_5m,
            "price_change_30m": change_30m,
            "model_snapshot_age_min": model_age_min,
            "weather_likelihood_age_min": weather_age_min,
            "q_primary_delta": model_delta,
            "q_physical_likelihood_delta": physical_delta,
            "weather_likelihood_refreshed_changed": bool(
                model_context.get("weather_likelihood_refreshed_changed")
            ),
            "weather_likelihood_feature_quality_changed": bool(
                model_context.get("weather_likelihood_feature_quality_changed")
            ),
            "weather_likelihood_feature_count_delta": feature_count_delta,
            "explainers": internal_context_explainers,
            "source": "event_runs_recent_snapshots_vs_market_ticks",
        },
    }


def model_probability_candidates(bucket_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for bucket in bucket_results:
        model = bucket.get("model") or {}
        q = parse_float(model.get("q_primary"))
        if q is None:
            continue
        market_mid = parse_float(bucket.get("market_mid"))
        gap = market_mid - q if market_mid is not None else None
        rows.append(
            {
                "label": bucket.get("label"),
                "q_primary": q,
                "q_primary_field": model.get("q_primary_field"),
                "market_mid": market_mid,
                "market_model_gap": gap,
                "q_bayes_posterior": model.get("q_bayes_posterior"),
                "q_calibrated": model.get("q_calibrated"),
                "q_physical_likelihood": model.get("q_physical_likelihood"),
                "selection_reasons": bucket.get("selection_reasons"),
            }
        )
    rows.sort(key=lambda row: row["q_primary"], reverse=True)
    return rows[:5]


def bucket_large_money_direction(bucket: dict[str, Any]) -> str:
    agg = bucket.get("trade_aggregate") or {}
    yes_net = parse_float(agg.get("yes_direction_net")) or 0.0
    if yes_net > 25:
        return "yes_support"
    if yes_net < -25:
        return "yes_pressure_or_no_buying"
    return "mixed_or_weak"


def event_high_risk_summary(event_result: dict[str, Any]) -> dict[str, Any]:
    high_buckets = []
    for bucket in event_result.get("buckets") or []:
        assessment = bucket.get("assessment") or {}
        risk_score = parse_float(assessment.get("risk_score")) or 0.0
        classes = assessment.get("classification") or []
        if classes == ["normal"] and risk_score < 40:
            continue
        high_buckets.append(
                {
                    "label": bucket.get("label"),
                    "event_date": slug_event_date(str(event_result.get("slug") or "")),
                    "alert_type": alert_evidence_type(
                    classes,
                    [
                        key
                        for key, enabled in (assessment.get("triggers") or {}).items()
                        if enabled
                    ],
                ),
                "market_mid": bucket.get("market_mid"),
                "model_q": (bucket.get("model") or {}).get("q_primary"),
                "market_model_gap": assessment.get("market_model_gap"),
                "risk_score": risk_score,
                "large_money_direction": (
                    "market_context_update_watch"
                    if (assessment.get("market_context") or {}).get("triggered")
                    else (
                        "internal_information_update_watch"
                        if (assessment.get("internal_repricing_context") or {}).get("triggered")
                        else (
                            "insufficient_recent_trade_flow"
                            if (assessment.get("book_inducement") or {}).get("triggered")
                            else bucket_large_money_direction(bucket)
                        )
                    )
                ),
                "book_inducement": assessment.get("book_inducement"),
                "wallet_concentration": assessment.get("wallet_concentration"),
                "market_context": assessment.get("market_context"),
                "internal_repricing_context": assessment.get("internal_repricing_context"),
                "classification": classes,
                "advice": assessment.get("advice"),
                "triggers": [
                    key
                    for key, enabled in (assessment.get("triggers") or {}).items()
                    if enabled
                ],
            }
        )
    high_buckets.sort(key=lambda row: row["risk_score"], reverse=True)
    return {
        "slug": event_result.get("slug"),
        "event_url": event_result.get("event_url"),
        "event_classification": event_result.get("event_classification"),
        "event_advice": event_result.get("event_advice"),
        "max_bucket_risk_score": high_buckets[0]["risk_score"] if high_buckets else 0.0,
        "large_money_direction": infer_event_large_money_direction(event_result),
        "true_high_probability_candidates": event_result.get("true_high_probability_candidates") or [],
        "high_risk_buckets": high_buckets[:8],
    }


def infer_event_large_money_direction(event_result: dict[str, Any]) -> str:
    yes_net_total = 0.0
    for bucket in event_result.get("buckets") or []:
        agg = bucket.get("trade_aggregate") or {}
        yes_net_total += parse_float(agg.get("yes_direction_net")) or 0.0
    if yes_net_total > 50:
        return "net_yes_support"
    if yes_net_total < -50:
        return "net_no_buying_or_yes_pressure"
    return "mixed_or_weak"


def canonical_execution_actions(advice: Iterable[str]) -> list[str]:
    actions: set[str] = set()
    for item in advice:
        if item in {
            "fade_overpriced_bucket_passive_limits_only",
            "avoid_following_pumped_yes",
        }:
            actions.update({"反向狙击", "只挂被动限价单"})
        elif item in {
            "no_market_orders",
            "no_market_orders_during_core_bucket_jump",
        }:
            actions.add("禁止市价单")
        elif item in {
            "track_true_high_probability_bucket",
            "run_full_wallet_book_trade_analysis",
            "inspect_top_wallet_continuation",
            "watch_wallet_flow_not_price",
        }:
            actions.add("重点跟踪真实高概率桶")
        elif item in {
            "observe_or_use_normal_edge_rules",
            "observe_until_book_rebuilds",
            "watch_for_wallet_flip_before_following",
            "do_not_infer_large_money_from_price_jump",
            "fade_only_if_trade_flow_confirms",
            "separate_market_context_from_flow_manipulation",
            "audit_polymarket_page_context_before_flow_interpretation",
            "separate_information_update_from_flow_manipulation",
            "audit_model_data_physics_update_before_flow_interpretation",
        }:
            actions.add("观望")
        elif item == "passive_follow_only_after_retest":
            actions.update({"跟随", "只挂被动限价单"})
    if not actions:
        actions.add("观望")
    order = ["跟随", "反向狙击", "只挂被动限价单", "禁止市价单", "观望", "重点跟踪真实高概率桶"]
    return [action for action in order if action in actions]


def alert_evidence_type(classes: Iterable[str], triggers: Iterable[str]) -> str:
    class_set = set(classes)
    trigger_set = set(triggers)
    if "page_market_context_update_watch" in class_set or "market_context_update_watch" in trigger_set:
        return "market_context"
    if "internal_repricing_context_watch" in class_set or "internal_repricing_context_watch" in trigger_set:
        return "internal_repricing_context"
    if "thin_book_price_inducement" in class_set or "price_jump_without_recent_trade_flow" in trigger_set:
        return "book_inducement"
    if "wallet_concentration_trade_flow" in class_set:
        return "wallet_concentration"
    if "suspected_pressure_accumulation" in class_set:
        return "pressure_accumulation"
    if "suspected_pump_or_inducement" in class_set:
        return "pump_or_inducement"
    if "large_money_rational_reverse_trading" in class_set:
        return "large_money_trade_flow"
    return "model_market_or_book_anomaly"


def alert_secondary_evidence_types(
    classes: Iterable[str],
    triggers: Iterable[str],
    primary: str,
) -> list[str]:
    candidates = []
    class_set = set(classes)
    trigger_set = set(triggers)
    if "page_market_context_update_watch" in class_set or "market_context_update_watch" in trigger_set:
        candidates.append("market_context")
    if "internal_repricing_context_watch" in class_set or "internal_repricing_context_watch" in trigger_set:
        candidates.append("internal_repricing_context")
    if "thin_book_price_inducement" in class_set or "price_jump_without_recent_trade_flow" in trigger_set:
        candidates.append("book_inducement")
    if "wallet_concentration_trade_flow" in class_set:
        candidates.append("wallet_concentration")
    if "suspected_pressure_accumulation" in class_set:
        candidates.append("pressure_accumulation")
    if "suspected_pump_or_inducement" in class_set:
        candidates.append("pump_or_inducement")
    if "large_money_rational_reverse_trading" in class_set:
        candidates.append("large_money_trade_flow")
    return [item for item in candidates if item != primary]


def build_alerts(event_results: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for event in event_results:
        event_trigger = (event.get("event_triggers") or {}).get(
            "core_bucket_price_probability_change"
        ) or {}
        changed_labels = {
            str(row.get("label"))
            for row in event_trigger.get("changed_buckets") or []
            if row.get("label")
        }
        for bucket in event.get("buckets") or []:
            assessment = bucket.get("assessment") or {}
            risk_score = parse_float(assessment.get("risk_score")) or 0.0
            classes = assessment.get("classification") or []
            trigger_keys = [
                key
                for key, enabled in (assessment.get("triggers") or {}).items()
                if enabled
            ]
            trigger_by_price_change = str(bucket.get("label")) in changed_labels
            if risk_score < args.alert_risk_threshold and not trigger_by_price_change:
                continue
            severity = "high" if risk_score >= 60 or trigger_by_price_change else "medium"
            agg = bucket.get("trade_aggregate") or {}
            model = bucket.get("model") or {}
            advice = assessment.get("advice") or []
            evidence_type = alert_evidence_type(classes, trigger_keys)
            if evidence_type == "book_inducement":
                large_money_direction = "insufficient_recent_trade_flow"
            elif evidence_type == "market_context":
                large_money_direction = "market_context_update_watch"
            elif evidence_type == "internal_repricing_context":
                large_money_direction = "internal_information_update_watch"
            else:
                large_money_direction = bucket_large_money_direction(bucket)
            alerts.append(
                {
                    "severity": severity,
                    "alert_type": evidence_type,
                    "secondary_evidence_types": alert_secondary_evidence_types(
                        classes,
                        trigger_keys,
                        evidence_type,
                    ),
                    "slug": event.get("slug"),
                    "event_date": slug_event_date(str(event.get("slug") or "")),
                    "event_url": event.get("event_url"),
                    "label": bucket.get("label"),
                    "risk_score": risk_score,
                    "market_mid": bucket.get("market_mid"),
                    "q_bayes_posterior": model.get("q_bayes_posterior"),
                    "q_calibrated": model.get("q_calibrated"),
                    "q_physical_likelihood": model.get("q_physical_likelihood"),
                    "market_model_gap": assessment.get("market_model_gap"),
                    "price_change_5m": bucket.get("price_change_5m"),
                    "price_change_30m": bucket.get("price_change_30m"),
                    "top3_wallet_share": agg.get("top3_wallet_share"),
                    "yes_direction_net": agg.get("yes_direction_net"),
                    "large_money_direction": large_money_direction,
                    "recent_trade_windows": bucket.get("recent_trade_windows"),
                    "book_inducement": assessment.get("book_inducement"),
                    "wallet_concentration": assessment.get("wallet_concentration"),
                    "market_context": assessment.get("market_context"),
                    "internal_repricing_context": assessment.get("internal_repricing_context"),
                    "classification": classes,
                    "triggered_metrics": trigger_keys,
                    "canonical_actions": canonical_execution_actions(advice),
                    "raw_advice": advice,
                    "top_wallets": (agg.get("top_wallets") or [])[:3],
                }
            )
    alerts.sort(key=lambda row: (row["severity"] != "high", -row["risk_score"]))
    return alerts


def analyze_event(
    session: requests.Session,
    conn: sqlite3.Connection | None,
    event: dict[str, Any],
    args: argparse.Namespace,
    previous_page_contexts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    buckets = extract_buckets(event)
    slug = str(event.get("slug") or "")
    model = load_local_model(conn, str(event.get("slug") or ""))
    model_rows = model.get("rows") or {}
    model_context_by_label = model.get("context_by_label") or {}
    page_context = fetch_page_market_context(session, slug, args)
    if not page_context.get("available"):
        page_context = gamma_market_context(event)
    page_context = attach_previous_page_context(
        page_context,
        (previous_page_contexts or {}).get(slug),
    )
    ticks = load_recent_ticks(conn, str(event.get("slug") or ""))
    analysis_now = utc_now()
    core_indexes, core_reasons = select_core_bucket_indexes(
        buckets,
        model_rows,
        adjacent=args.adjacent_buckets,
    )
    bucket_results: list[dict[str, Any]] = []
    for idx in sorted(core_indexes):
        bucket = buckets[idx]
        book = fetch_book(session, bucket.yes_token, args)
        trades = fetch_trades(session, bucket, args)
        agg = aggregate_trades(trades)
        recent_windows = aggregate_recent_trade_windows(trades, now=analysis_now)
        label_key = normalize_label(bucket.label)
        model_row = model_rows.get(label_key, {})
        market_mid = parse_float(book.get("mid"))
        if market_mid is None:
            market_mid = bucket.gamma_yes
        tick_rows = ticks.get(label_key) or []
        result = {
            "label": bucket.label,
            "bucket_index": idx,
            "selection_reasons": core_reasons.get(idx, []),
            "condition_id": bucket.condition_id,
            "yes_token": bucket.yes_token,
            "no_token": bucket.no_token,
            "market_id": bucket.market_id,
            "gamma_yes": bucket.gamma_yes,
            "gamma_bid": bucket.gamma_bid,
            "gamma_ask": bucket.gamma_ask,
            "market_mid": market_mid,
            "liquidity": bucket.liquidity,
            "volume": bucket.volume,
            "volume24h": bucket.volume24h,
            "book": book,
            "trade_aggregate": agg,
            "recent_trade_windows": recent_windows,
            "price_jump_5m": jump_from_ticks(tick_rows, 5),
            "price_jump_30m": jump_from_ticks(tick_rows, 30),
            "price_change_5m": current_change_from_ticks(tick_rows, 5, market_mid),
            "price_change_30m": current_change_from_ticks(tick_rows, 30, market_mid),
            "tick_count_45m": len(tick_rows),
            "model": model_row,
            "model_context": model_context_by_label.get(label_key, {}),
            "page_market_context": page_context,
        }
        bucket_results.append(result)
        time.sleep(args.request_sleep)
    cross_paths = cross_bucket_wallet_paths(bucket_results)
    classified_buckets = []
    for result in bucket_results:
        result["event_cross_bucket_max_opposite"] = cross_paths["max_opposite_cross_bucket_notional"]
        result["assessment"] = classify_bucket(
            result,
            gap_threshold=args.gap_threshold,
            jump_threshold=args.jump_threshold,
            wallet_share_threshold=args.wallet_share_threshold,
            thin_depth_threshold=args.thin_depth_threshold,
            turnover_threshold=args.turnover_threshold,
            cross_wallet_threshold=args.cross_wallet_threshold,
            core_price_change_5m=args.core_bucket_price_change_5m,
            book_inducement_5m_max_notional=args.book_inducement_5m_max_notional,
            book_inducement_30m_max_notional=args.book_inducement_30m_max_notional,
            wallet_flow_min_notional=args.wallet_flow_min_notional,
            market_context_model_prob_change=args.market_context_model_prob_change,
            market_context_physical_prob_change=args.market_context_physical_prob_change,
            market_context_update_window_min=args.market_context_update_window_min,
        )
        classified_buckets.append(result)
    core_price_change = core_bucket_price_probability_change(classified_buckets, args)

    event_classes: set[str] = set()
    event_advice: set[str] = set()
    abnormal_bucket_count = 0
    for result in classified_buckets:
        classes = set(result.get("assessment", {}).get("classification") or [])
        if classes != {"normal"}:
            abnormal_bucket_count += 1
        event_classes.update(classes)
        event_advice.update(result.get("assessment", {}).get("advice") or [])
    if "normal" in event_classes and len(event_classes) > 1:
        event_classes.remove("normal")
    if core_price_change.get("triggered"):
        event_classes.add("core_bucket_price_probability_change_anomaly")
        event_advice.add("run_full_wallet_book_trade_analysis")
        event_advice.add("no_market_orders_during_core_bucket_jump")
    if not event_classes:
        event_classes.add("normal")

    return {
        "slug": event.get("slug"),
        "title": event.get("title"),
        "endDate": event.get("endDate"),
        "updatedAt": event.get("updatedAt"),
        "page_market_context": page_context,
        "event_url": f"https://polymarket.com/event/{event.get('slug')}",
        "event_liquidity": field_max(event, ("liquidityClob", "liquidity")),
        "event_volume": field_max(event, ("volumeClob", "volume")),
        "bucket_count": len(buckets),
        "core_bucket_count": len(classified_buckets),
        "abnormal_bucket_count": abnormal_bucket_count,
        "model_available": bool(model.get("available")),
        "model_created_at": model.get("created_at"),
        "model_reason": model.get("reason"),
        "climo_regime_summary": model.get("climo_regime_summary"),
        "event_classification": sorted(event_classes),
        "event_advice": sorted(event_advice),
        "event_triggers": {
            "core_bucket_price_probability_change": core_price_change,
        },
        "cross_bucket_wallet_paths": cross_paths,
        "true_high_probability_candidates": model_probability_candidates(classified_buckets),
        "large_money_direction": infer_event_large_money_direction(
            {"buckets": classified_buckets}
        ),
        "buckets": classified_buckets,
    }


def compact_event_summary(event_result: dict[str, Any]) -> dict[str, Any]:
    abnormal = []
    for bucket in event_result.get("buckets") or []:
        assessment = bucket.get("assessment") or {}
        classes = assessment.get("classification") or []
        if classes == ["normal"]:
            continue
        abnormal.append(
            {
                "label": bucket.get("label"),
                "event_date": slug_event_date(str(event_result.get("slug") or "")),
                "alert_type": alert_evidence_type(
                    classes,
                    [
                        key
                        for key, enabled in (assessment.get("triggers") or {}).items()
                        if enabled
                    ],
                ),
                "mid": bucket.get("market_mid"),
                "q_primary": (bucket.get("model") or {}).get("q_primary"),
                "gap": assessment.get("market_model_gap"),
                "classification": classes,
                "advice": assessment.get("advice"),
                "book_inducement": assessment.get("book_inducement"),
                "wallet_concentration": assessment.get("wallet_concentration"),
                "market_context": assessment.get("market_context"),
                "internal_repricing_context": assessment.get("internal_repricing_context"),
                "triggers": [
                    key
                    for key, enabled in (assessment.get("triggers") or {}).items()
                    if enabled
                ],
                "top3_wallet_share": (bucket.get("trade_aggregate") or {}).get(
                    "top3_wallet_share"
                ),
                "yes_direction_net": (bucket.get("trade_aggregate") or {}).get(
                    "yes_direction_net"
                ),
            }
        )
    return {
        "slug": event_result.get("slug"),
        "event_url": event_result.get("event_url"),
        "classification": event_result.get("event_classification"),
        "advice": event_result.get("event_advice"),
        "event_triggers": event_result.get("event_triggers"),
        "climo_regime_summary": event_result.get("climo_regime_summary"),
        "abnormal_bucket_count": event_result.get("abnormal_bucket_count"),
        "abnormal_buckets": abnormal[:8],
    }


def climo_regime_summaries(event_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in event_results:
        summary = event.get("climo_regime_summary") if isinstance(event.get("climo_regime_summary"), dict) else {}
        if not summary:
            continue
        rows.append(
            {
                "slug": event.get("slug"),
                "event_date": slug_event_date(str(event.get("slug") or "")),
                "event_url": event.get("event_url"),
                **summary,
            }
        )
    rows.sort(key=lambda row: str(row.get("event_date") or ""))
    return rows


def market_context_summaries(event_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in event_results:
        ctx = event.get("page_market_context") if isinstance(event.get("page_market_context"), dict) else {}
        analysis = ctx.get("analysis") if isinstance(ctx.get("analysis"), dict) else {}
        rows.append(
            {
                "slug": event.get("slug"),
                "event_date": slug_event_date(str(event.get("slug") or "")),
                "event_url": event.get("event_url"),
                "source": ctx.get("source"),
                "context_updated_at": ctx.get("context_updated_at"),
                "changed_vs_previous": ctx.get("changed_vs_previous"),
                "reference_hint": analysis.get("reference_hint"),
                "target_temperature_range_c": analysis.get("target_temperature_range_c"),
                "support_factors": analysis.get("support_factors"),
                "risk_factors": analysis.get("risk_factors"),
                "is_reference_only": analysis.get("is_reference_only"),
                "does_not_change_probability": analysis.get("does_not_change_probability"),
                "does_not_change_trade_decision": analysis.get("does_not_change_trade_decision"),
            }
        )
    rows.sort(key=lambda row: str(row.get("event_date") or ""))
    return rows


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-json", default="")
    parser.add_argument("--output-alerts-json", default="")
    parser.add_argument("--max-events", type=int, default=0)
    parser.add_argument(
        "--city",
        default="chengdu",
        help="City slug filter. Defaults to chengdu for the current live workflow.",
    )
    parser.add_argument("--slug-contains", default="")
    parser.add_argument("--gamma-limit", type=int, default=100)
    parser.add_argument("--gamma-max-pages", type=int, default=20)
    parser.add_argument("--hours-to-end", type=float, default=72.0)
    parser.add_argument("--allow-recently-ended-hours", type=float, default=0.0)
    parser.add_argument("--min-liquidity", type=float, default=300.0)
    parser.add_argument("--min-volume", type=float, default=100.0)
    parser.add_argument("--adjacent-buckets", type=int, default=2)
    parser.add_argument("--limit-trades", type=int, default=150)
    parser.add_argument("--data-api-page-limit", type=int, default=100)
    parser.add_argument("--request-sleep", type=float, default=0.08)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--gap-threshold", type=float, default=0.10)
    parser.add_argument("--jump-threshold", type=float, default=0.05)
    parser.add_argument("--core-bucket-price-change-5m", type=float, default=0.03)
    parser.add_argument("--core-bucket-price-change-30m", type=float, default=0.05)
    parser.add_argument("--wallet-share-threshold", type=float, default=0.40)
    parser.add_argument("--thin-depth-threshold", type=float, default=100.0)
    parser.add_argument("--turnover-threshold", type=float, default=0.30)
    parser.add_argument("--cross-wallet-threshold", type=float, default=50.0)
    parser.add_argument("--book-inducement-5m-max-notional", type=float, default=25.0)
    parser.add_argument("--book-inducement-30m-max-notional", type=float, default=75.0)
    parser.add_argument("--wallet-flow-min-notional", type=float, default=100.0)
    parser.add_argument("--market-context-model-prob-change", type=float, default=0.02)
    parser.add_argument("--market-context-physical-prob-change", type=float, default=0.015)
    parser.add_argument("--market-context-update-window-min", type=float, default=45.0)
    parser.add_argument("--alert-risk-threshold", type=float, default=40.0)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    session = requests.Session()
    session.headers.update({"User-Agent": "weather-market-anomaly-monitor/1.0"})
    db_path = Path(args.db)
    conn: sqlite3.Connection | None = None
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
    started = utc_now()
    report_dir = (
        Path(args.output_json).parent
        if args.output_json
        else DEFAULT_ANOMALY_REPORT_DIR
    )
    previous_page_contexts = latest_previous_page_contexts(report_dir)
    gamma_events = fetch_gamma_events(session, args)
    selected_events: list[dict[str, Any]] = []
    filtered_out = 0
    for event in gamma_events:
        buckets = extract_buckets(event)
        if filter_event(event, buckets, args):
            selected_events.append(event)
        else:
            filtered_out += 1
    if not args.quiet:
        print(
            (
                "weather_market_anomaly_monitor: "
                f"gamma_weather_events_seen={len(gamma_events)} "
                f"selected_events={len(selected_events)} "
                f"filtered_out={filtered_out}"
            ),
            file=sys.stderr,
        )

    event_results: list[dict[str, Any]] = []
    for idx, event in enumerate(selected_events, start=1):
        if not args.quiet:
            print(
                (
                    "weather_market_anomaly_monitor: "
                    f"analyzing_event={idx}/{len(selected_events)} "
                    f"slug={event.get('slug')}"
                ),
                file=sys.stderr,
            )
        event_results.append(analyze_event(session, conn, event, args, previous_page_contexts))

    if conn is not None:
        conn.close()
    abnormal_events = [
        event
        for event in event_results
        if event.get("event_classification") and event.get("event_classification") != ["normal"]
    ]
    all_wallets = {
        wallet.get("wallet")
        for event in event_results
        for bucket in event.get("buckets") or []
        for wallet in (bucket.get("trade_aggregate") or {}).get("wallet_flows", [])
        if wallet.get("wallet")
    }
    total_trades = sum(
        (bucket.get("trade_aggregate") or {}).get("trade_count", 0)
        for event in event_results
        for bucket in event.get("buckets") or []
    )
    total_trade_notional = sum(
        (bucket.get("trade_aggregate") or {}).get("total_notional", 0.0)
        for event in event_results
        for bucket in event.get("buckets") or []
    )
    high_risk_events = [
        event_high_risk_summary(event)
        for event in abnormal_events
    ]
    high_risk_events.sort(
        key=lambda row: row.get("max_bucket_risk_score") or 0.0,
        reverse=True,
    )
    source_gaps = []
    if any(not event.get("model_available") for event in event_results):
        source_gaps.append("local_model_probability_missing_for_some_events")
    if any(
        not (bucket.get("book") or {}).get("ok")
        for event in event_results
        for bucket in event.get("buckets") or []
    ):
        source_gaps.append("clob_book_missing_for_some_core_buckets")
    if any(
        (bucket.get("trade_aggregate") or {}).get("trade_count", 0) == 0
        for event in event_results
        for bucket in event.get("buckets") or []
    ):
        source_gaps.append("data_api_trades_empty_for_some_core_buckets")
    source_gaps.extend(
        [
            "order_cancel_spoofing_evidence_unavailable_without_orderbook_stream_history",
            "wallet_identity_linkage_unavailable_without_external_clustering",
        ]
    )
    alerts = build_alerts(event_results, args)
    summary = {
        "generated_at": utc_now().isoformat(),
        "started_at": started.isoformat(),
        "source": {
            "gamma_events_url": GAMMA_EVENTS_URL,
            "clob_book_url": CLOB_BOOK_URL,
            "data_api_trades_url": DATA_API_TRADES_URL,
            "local_db": str(db_path),
        },
        "filters": {
            "active": True,
            "closed": False,
            "hours_to_end": args.hours_to_end,
            "allow_recently_ended_hours": args.allow_recently_ended_hours,
            "min_liquidity": args.min_liquidity,
            "min_volume": args.min_volume,
            "adjacent_buckets": args.adjacent_buckets,
            "city": args.city,
            "slug_contains": args.slug_contains,
        },
        "thresholds": {
            "market_model_gap": args.gap_threshold,
            "price_jump_30m": args.jump_threshold,
            "core_bucket_yes_price_change_5m": args.core_bucket_price_change_5m,
            "core_bucket_yes_price_change_30m": args.core_bucket_price_change_30m,
            "top3_wallet_share": args.wallet_share_threshold,
            "top5_depth": args.thin_depth_threshold,
            "cross_wallet_opposite": args.cross_wallet_threshold,
            "bucket_turnover_to_liquidity": args.turnover_threshold,
            "book_inducement_5m_max_notional": args.book_inducement_5m_max_notional,
            "book_inducement_30m_max_notional": args.book_inducement_30m_max_notional,
            "wallet_flow_min_notional": args.wallet_flow_min_notional,
            "market_context_model_prob_change": args.market_context_model_prob_change,
            "market_context_physical_prob_change": args.market_context_physical_prob_change,
            "market_context_update_window_min": args.market_context_update_window_min,
            "alert_risk_score": args.alert_risk_threshold,
        },
        "gamma_weather_events_seen": len(gamma_events),
        "events_filtered_out": filtered_out,
        "events_analyzed": len(event_results),
        "core_buckets_analyzed": sum(e.get("core_bucket_count", 0) for e in event_results),
        "trades_analyzed": total_trades,
        "trade_notional_analyzed": total_trade_notional,
        "unique_wallets_analyzed": len(all_wallets),
        "abnormal_events": len(abnormal_events),
        "abnormal_buckets": sum(e.get("abnormal_bucket_count", 0) for e in event_results),
        "events_with_local_model": sum(1 for e in event_results if e.get("model_available")),
        "market_context_summaries": market_context_summaries(event_results),
        "climo_regime_summaries": climo_regime_summaries(event_results),
        "compact_abnormal_events": [compact_event_summary(e) for e in abnormal_events[:30]],
        "high_risk_events": high_risk_events[:30],
        "alerts": alerts,
        "alert_count": len(alerts),
        "source_gaps": source_gaps,
    }
    report = {
        "summary": summary,
        "events": event_results,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    if args.output_alerts_json:
        alerts_path = Path(args.output_alerts_json)
        alerts_path.parent.mkdir(parents=True, exist_ok=True)
        alerts_path.write_text(
            json.dumps(
                {
                    "generated_at": summary["generated_at"],
                    "filters": summary["filters"],
                    "thresholds": summary["thresholds"],
                    "alert_count": len(alerts),
                    "alerts": alerts,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
