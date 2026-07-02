#!/usr/bin/env python3
"""Fast discovery for live Polymarket weather markets.

This is intentionally narrow: it only touches current/future temperature
markets, refreshes the weather-events cache, and caches outcome token ids so
the market WebSocket can subscribe on its next reload cycle.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PM_WEATHER_EVENTS_SKIP_PAGE", "1")
os.environ.setdefault(
    "PM_SCAN_GAMMA_MAX_PAGES",
    os.environ.get("PM_WEATHER_DISCOVERY_GAMMA_MAX_PAGES", "4"),
)
os.environ.setdefault(
    "PM_SCAN_GAMMA_TIMEOUT",
    os.environ.get("PM_WEATHER_DISCOVERY_GAMMA_TIMEOUT", "4"),
)
os.environ.setdefault(
    "PM_SCAN_GAMMA_RETRIES",
    os.environ.get("PM_WEATHER_DISCOVERY_GAMMA_RETRIES", "1"),
)
os.environ.setdefault(
    "PM_SCAN_GAMMA_BACKOFF",
    os.environ.get("PM_WEATHER_DISCOVERY_GAMMA_BACKOFF", "0.25"),
)

import scan_weather_market as swm  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover live weather markets and cache token ids.")
    parser.add_argument("--days-ahead", type=int, default=int(os.environ.get("PM_WEATHER_DISCOVERY_DAYS_AHEAD", "2")))
    parser.add_argument("--max-events", type=int, default=int(os.environ.get("PM_WEATHER_DISCOVERY_MAX_EVENTS", "80")))
    parser.add_argument("--max-details", type=int, default=int(os.environ.get("PM_WEATHER_DISCOVERY_MAX_DETAILS", "0")))
    parser.add_argument(
        "--refresh-existing-min",
        type=float,
        default=float(os.environ.get("PM_WEATHER_DISCOVERY_REFRESH_EXISTING_MIN", "20")),
        help="Skip detail refresh when token cache for a slug is newer than this many minutes.",
    )
    parser.add_argument("--tz", default=os.environ.get("PM_WEATHER_DISCOVERY_TZ", "America/New_York"))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _event_date(ev: Mapping[str, Any]) -> Optional[str]:
    return swm.event_date_from_slug_or_end_date(ev.get("slug"), ev.get("endDate"))


def _window_dates(tz_name: str, days_ahead: int) -> Tuple[date, date]:
    today_text = swm.today_date_str(tz_name)
    try:
        start = date.fromisoformat(today_text)
    except Exception:
        start = datetime.now(timezone.utc).date()
    end = start + timedelta(days=max(0, int(days_ahead)))
    return start, end


def _is_open_event(ev: Mapping[str, Any]) -> bool:
    if bool(ev.get("closed")) or bool(ev.get("archived")):
        return False
    active = ev.get("active")
    if active is not None and not bool(active):
        return False
    end_date = ev.get("endDate")
    if end_date and swm.is_end_date_passed(str(end_date), buffer_minutes=0):
        return False
    return True


def _merge_weather_cache(events: List[Dict[str, Any]]) -> int:
    existing = swm._load_weather_events_cache(allow_stale=True) or []
    good = swm._load_weather_events_good_cache() or []
    merged = swm._dedupe_weather_events(list(events) + list(existing) + list(good))
    if not merged:
        return 0
    swm._save_weather_events_cache(merged)
    if len(merged) >= swm._weather_events_good_min_count():
        swm._save_weather_events_good_cache(merged)
    return len(merged)


def _ensure_discovery_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_market_discovery (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            title TEXT,
            market_open_at TEXT,
            first_seen_at TEXT NOT NULL,
            first_tokenized_at TEXT,
            token_count INTEGER NOT NULL DEFAULT 0,
            status TEXT,
            details_json TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (slug, event_date)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_weather_market_discovery_updated
        ON weather_market_discovery(event_date, updated_at)
        """
    )


def _load_token_state() -> Dict[Tuple[str, str], Tuple[int, str]]:
    state: Dict[Tuple[str, str], Tuple[int, str]] = {}
    try:
        swm.init_db()
        with swm.open_db_ro_fast(timeout_sec=1.0, busy_timeout_ms=1000) as conn:
            rows = conn.execute(
                """
                SELECT slug, event_date, COUNT(*) AS token_count, MAX(updated_at) AS updated_at
                FROM market_tokens
                WHERE slug IS NOT NULL AND slug != ''
                  AND event_date IS NOT NULL AND event_date != ''
                GROUP BY slug, event_date
                """
            ).fetchall()
    except Exception:
        return state
    for row in rows or []:
        slug = str(row[0] or "").strip()
        event_date = str(row[1] or "").strip().split("T", 1)[0]
        count = int(swm.to_float(row[2]) or 0)
        updated_at = str(row[3] or "").strip()
        if slug and event_date:
            state[(slug, event_date)] = (count, updated_at)
    return state


def _age_minutes(iso_text: str) -> Optional[float]:
    dt = swm._parse_iso_dt(str(iso_text or "").strip())
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 60.0)


def _parse_tokens_from_event(ev: Mapping[str, Any]) -> List[Dict[str, Any]]:
    try:
        return swm.parse_outcomes_and_tokens_from_event(dict(ev))
    except Exception:
        return []


def _fetch_detail(slug: str) -> Optional[Dict[str, Any]]:
    timeout = swm.to_float(os.environ.get("PM_WEATHER_DISCOVERY_DETAIL_TIMEOUT"))
    if timeout is None or not math.isfinite(timeout) or timeout <= 0:
        timeout = min(float(swm.GAMMA_TIMEOUT), 6.0)
    retries_raw = swm.to_float(os.environ.get("PM_WEATHER_DISCOVERY_DETAIL_RETRIES"))
    retries = int(retries_raw) if retries_raw is not None and retries_raw > 0 else 1
    backoff = swm.to_float(os.environ.get("PM_WEATHER_DISCOVERY_DETAIL_BACKOFF"))
    if backoff is None or not math.isfinite(backoff) or backoff < 0:
        backoff = 0.25
    try:
        return swm.fetch_gamma_event_by_slug(
            slug,
            timeout=float(timeout),
            retries=max(1, int(retries)),
            backoff=float(backoff),
            bypass_proxy=False,
        )
    except Exception:
        return None


def _save_tokens_and_discovery(
    ev: Mapping[str, Any],
    event_date: str,
    tokens: List[Dict[str, Any]],
    *,
    dry_run: bool,
    status: str,
) -> bool:
    slug = str(ev.get("slug") or "").strip()
    if not slug or not event_date:
        return False
    now = swm.utc_now_iso()
    market_open_at = swm._extract_market_open_at(dict(ev))
    details = {
        "source": "gamma_fast_discovery",
        "closed": bool(ev.get("closed")),
        "active": ev.get("active"),
        "archived": bool(ev.get("archived")),
        "endDate": ev.get("endDate") or "",
    }
    if dry_run:
        return bool(tokens)
    swm.init_db()
    with swm.open_db() as conn:
        _ensure_discovery_table(conn)
        if tokens:
            swm._save_cached_market_tokens(conn, slug, event_date, tokens, now)
        conn.execute(
            """
            INSERT INTO weather_market_discovery (
                slug,
                event_date,
                title,
                market_open_at,
                first_seen_at,
                first_tokenized_at,
                token_count,
                status,
                details_json,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug, event_date) DO UPDATE SET
                title = COALESCE(NULLIF(excluded.title, ''), weather_market_discovery.title),
                market_open_at = COALESCE(excluded.market_open_at, weather_market_discovery.market_open_at),
                first_seen_at = COALESCE(weather_market_discovery.first_seen_at, excluded.first_seen_at),
                first_tokenized_at = COALESCE(
                    weather_market_discovery.first_tokenized_at,
                    excluded.first_tokenized_at
                ),
                token_count = CASE
                    WHEN excluded.token_count > COALESCE(weather_market_discovery.token_count, 0)
                    THEN excluded.token_count
                    ELSE weather_market_discovery.token_count
                END,
                status = excluded.status,
                details_json = excluded.details_json,
                updated_at = excluded.updated_at
            """,
            (
                slug,
                event_date,
                str(ev.get("title") or slug),
                market_open_at,
                now,
                now if tokens else None,
                len(tokens),
                status,
                json.dumps(details, ensure_ascii=True, sort_keys=True),
                now,
            ),
        )
        conn.commit()
    return bool(tokens)


def _iter_window_events(
    events: Iterable[Dict[str, Any]],
    *,
    start: date,
    end: date,
    max_events: int,
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        slug = str(ev.get("slug") or "").strip()
        if not slug or slug in seen:
            continue
        if not swm.is_temperature_event(ev):
            continue
        event_date_text = _event_date(ev)
        if not event_date_text:
            continue
        try:
            event_day = date.fromisoformat(event_date_text)
        except Exception:
            continue
        if event_day < start or event_day > end:
            continue
        if not _is_open_event(ev):
            continue
        seen.add(slug)
        selected.append(ev)
        if max_events > 0 and len(selected) >= max_events:
            break
    selected.sort(
        key=lambda item: (
            str(_event_date(item) or "9999-12-31"),
            str(item.get("createdAt") or item.get("creationDate") or item.get("startDate") or ""),
            str(item.get("slug") or ""),
        )
    )
    return selected


def main() -> None:
    args = _parse_args()
    start, end = _window_dates(args.tz, args.days_ahead)
    events, warnings = swm.fetch_weather_events_from_gamma()
    selected = _iter_window_events(
        events,
        start=start,
        end=end,
        max_events=max(0, int(args.max_events)),
    )
    cache_count = 0 if args.dry_run else _merge_weather_cache(events)
    token_state = _load_token_state()
    detail_count = 0
    tokenized = 0
    known_recent = 0
    failed = 0
    new_slugs: List[str] = []
    refreshed_slugs: List[str] = []

    for ev in selected:
        slug = str(ev.get("slug") or "").strip()
        event_date_text = _event_date(ev)
        if not slug or not event_date_text:
            continue
        key = (slug, event_date_text)
        existing_count, existing_updated_at = token_state.get(key, (0, ""))
        existing_age = _age_minutes(existing_updated_at)
        if existing_count > 0 and existing_age is not None and existing_age < float(args.refresh_existing_min):
            known_recent += 1
            _save_tokens_and_discovery(
                ev,
                event_date_text,
                [],
                dry_run=args.dry_run,
                status="known_recent",
            )
            continue

        tokens: List[Dict[str, Any]] = []
        detail = None
        if detail_count < max(0, int(args.max_details)):
            detail = _fetch_detail(slug)
            detail_count += 1
        if isinstance(detail, dict):
            ev_for_save = {**ev, **detail}
            tokens = _parse_tokens_from_event(ev_for_save)
        else:
            ev_for_save = ev
        if not tokens:
            tokens = _parse_tokens_from_event(ev)
        ok = _save_tokens_and_discovery(
            ev_for_save,
            event_date_text,
            tokens,
            dry_run=args.dry_run,
            status="tokenized" if tokens else "missing_tokens",
        )
        if ok:
            tokenized += 1
            if existing_count <= 0:
                new_slugs.append(slug)
            else:
                refreshed_slugs.append(slug)
        else:
            failed += 1

    payload = {
        "ok": True,
        "updated_at": swm.utc_now_iso(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "events_from_gamma": len(events),
        "events_selected": len(selected),
        "cache_count": cache_count,
        "known_recent": known_recent,
        "detail_fetches": detail_count,
        "tokenized": tokenized,
        "failed": failed,
        "new_slugs": new_slugs[:20],
        "refreshed_slugs": refreshed_slugs[:20],
        "warnings": warnings[:5],
        "dry_run": bool(args.dry_run),
    }
    if new_slugs and not args.dry_run:
        signal_path = Path(
            os.environ.get(
                "PM_WEATHER_DISCOVERY_SIGNAL_PATH",
                str(swm.DATA_DIR / "weather_discovery_new_market_signal.json"),
            )
        )
        try:
            signal_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = signal_path.with_suffix(signal_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True), encoding="utf-8")
            tmp.replace(signal_path)
        except Exception:
            pass
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
