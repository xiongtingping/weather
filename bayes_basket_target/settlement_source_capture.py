"""Charter 225: capture a day's settlement evidence, bounded, for a repair.

* Wunderground daily history -- the page is server-rendered; one GET of the
  market's ``resolutionSource`` + ``/date/Y-M-D`` in the market's unit (the
  ``wu_units`` cookie) returns the observation table.  The raw bytes are kept
  (gzip) as ``wu_daily_history.html.gz``.
* NOAA's time series is served through a page token that only weather.gov's
  own origin may use, so no script reads it.  A NOAA day is captured as the
  market's official resolution instead (``market_resolution.json``: the
  event's markets with their settled prices), which determines a one-degree
  bucket exactly.  The first batch's NOAA pages (2026-09-25) were read in a
  browser, on the page itself, and sit in the same tree as
  ``noaa_summary.json``.

Every response is size-bounded and read in one bounded pass; any failure is a
``ContractError`` and the day is left for the next pass.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError
from .settlement_source_truth import (
    AWC_FAMILY,
    NOAA_FAMILY,
    PAGE_KIND,
    RESOLUTION_KIND,
    WU_FAMILY,
)

WU_OBJECT = "wu_daily_history.html.gz"
NOAA_OBJECT = "noaa_summary.json"
# Charter 276: the market's own AWC raw-METAR URL, kept as fetched.
AWC_OBJECT = "awc_metar_raw.txt.gz"
AWC_MAX_BYTES = 512 * 1024
RESOLUTION_OBJECT = "market_resolution.json"
WU_MAX_BYTES = 2 * 1024 * 1024
GAMMA_EVENT_URL = "https://gamma-api.polymarket.com/events?slug="
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def wu_capture_url(resolution_source: str, event_date: str) -> str:
    year, month, day = str(event_date).split("-")
    return str(resolution_source).rstrip("/") + f"/date/{int(year)}-{int(month)}-{int(day)}"


def _bounded_get(client: Any, url: str, *, headers: Mapping[str, str], limit: int) -> bytes:
    with client.stream("GET", url, headers=dict(headers)) as response:
        if response.status_code != 200:
            raise ContractError(f"settlement_source_http_{response.status_code}")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > limit:
                raise ContractError("settlement_source_response_too_large")
            chunks.append(chunk)
    return b"".join(chunks)


def _keep(folder: Path, name: str, body: bytes, meta: dict[str, Any]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_bytes(gzip.compress(body) if name.endswith(".gz") else body)
    meta = {
        **meta,
        "captured_at_utc": _now(),
        "raw_sha256": hashlib.sha256(body).hexdigest(),
        "raw_bytes": len(body),
    }
    (folder / (name + ".meta")).write_text(json.dumps(meta, sort_keys=True))
    return target


def capture(
    client: Any,
    *,
    market: Mapping[str, Any],
    station_id: str,
    event_date: str,
    captures: Path,
) -> Path:
    """Fetch and keep one day's Wunderground settlement page."""

    if market["family"] == NOAA_FAMILY:
        raise ContractError("settlement_source_noaa_page_data_origin_locked")
    if market["family"] == AWC_FAMILY:
        # The rules name the exact query (station, end of window, hours); it is
        # fetched as written so the evidence is the page the market settles on.
        url = str(market["resolution_source"])
        if not url.startswith("https://aviationweather.gov/api/data/metar?"):
            raise ContractError("settlement_source_awc_url_invalid")
        body = _bounded_get(client, url, headers={"User-Agent": USER_AGENT}, limit=AWC_MAX_BYTES)
        return _keep(captures / station_id / event_date, AWC_OBJECT, body,
                     {"capture_url": url, "kind": PAGE_KIND})
    if market["family"] != WU_FAMILY:
        raise ContractError("settlement_source_family_unsupported")
    url = wu_capture_url(str(market["resolution_source"]), event_date)
    cookie = "wu_units=" + ("e" if market["unit"] == "F" else "m")
    body = _bounded_get(
        client, url, headers={"User-Agent": USER_AGENT, "Cookie": cookie}, limit=WU_MAX_BYTES,
    )
    return _keep(captures / station_id / event_date, WU_OBJECT, body,
                 {"capture_url": url, "cookie": cookie, "kind": PAGE_KIND})


def capture_resolution(
    event: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
    captures: Path,
) -> Path:
    """Keep the market's official resolution: its markets and settled prices."""

    slug = str(event.get("slug") or "")
    body = json.dumps({
        "slug": slug,
        "closed": event.get("closed") is True,
        "closedTime": event.get("closedTime"),
        "resolutionSource": event.get("resolutionSource"),
        "description": event.get("description"),
        "markets": [
            {
                "groupItemTitle": market.get("groupItemTitle"),
                "outcomePrices": market.get("outcomePrices"),
                "umaResolutionStatus": market.get("umaResolutionStatus"),
            }
            for market in event.get("markets") or []
            if isinstance(market, Mapping)
        ],
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _keep(captures / station_id / event_date, RESOLUTION_OBJECT, body,
                 # A slug is [a-z0-9-] only; nothing to escape.
                 {"capture_url": GAMMA_EVENT_URL + slug, "kind": RESOLUTION_KIND})


def load_case(
    *,
    market: Mapping[str, Any],
    station_id: str,
    event_date: str,
    captures: Path,
    settlement_timezone: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """The repair case of one captured day, for ``settlement_source_truth``.

    The settlement page when one was captured (Wunderground's, or a NOAA page
    read in a browser); otherwise the market's resolution.
    """

    folder = captures / station_id / event_date
    page = (WU_OBJECT if market["family"] == WU_FAMILY
            else AWC_OBJECT if market["family"] == AWC_FAMILY else NOAA_OBJECT)
    name, kind = (page, PAGE_KIND) if (folder / page).exists() else (RESOLUTION_OBJECT, RESOLUTION_KIND)
    path = folder / name
    meta = json.loads((folder / (name + ".meta")).read_text())
    raw = gzip.decompress(path.read_bytes()) if name.endswith(".gz") else path.read_bytes()
    object_ref = str(path)
    if root is not None and path.is_relative_to(root):
        object_ref = str(path.relative_to(root))
    return {
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "market": {
            "slug": market["slug"],
            "family": market["family"],
            "unit": market["unit"],
            "resolution_source": market["resolution_source"],
            "winning_bucket": market["winning_bucket"],
            "closed": market["closed"],
        },
        "source": {
            "family": market["family"],
            "kind": kind,
            "capture_url": meta["capture_url"],
            "object_ref": object_ref,
            "raw_bytes": raw,
            "raw_sha256": meta["raw_sha256"],
            "captured_at_utc": meta["captured_at_utc"],
        },
    }


__all__ = [
    "AWC_OBJECT",
    "NOAA_OBJECT",
    "RESOLUTION_OBJECT",
    "WU_OBJECT",
    "capture",
    "capture_resolution",
    "load_case",
    "wu_capture_url",
]
