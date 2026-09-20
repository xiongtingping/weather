"""Charter 162 (2026-09-20): TAF bulletins off NOAA's second door.

AWC's certificate expired at 2026-09-19T23:59:59Z and the TAF capture, which
verifies it like every other AWC transport, stopped with it.  TGFTP publishes
the same bulletins one file per clock hour, so one bounded request per cycle
carries every station's newest TAF.

What comes back is the bulletin text and nothing else -- TGFTP decodes
nothing -- so the rows this module builds carry the four fields the capture
resolves from the response itself (the station, the issue instant, when TGFTP
published it, and the validity window, all read off the bulletin) and no
``fcsts``.  The TX group is parsed from the text by the capture either way.
The decoded cloud, precipitation and wind fields are therefore absent, which
charter 158's tempering already has a meaning for: a TAF with no weather to
explain it is the SMALL group and pulls at full strength.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

TAF_CYCLE_ENDPOINT = (
    "https://tgftp.nws.noaa.gov/data/forecasts/taf/cycles/{cycle:02d}Z.TXT"
)
ORIGIN_SOURCE_ID = "global_noaa_tgftp_relay"
ORIGIN_AUTHORITY = "NOAA/NWS TGFTP worldwide TAF cycle relay"
PROVIDER = "NOAA/NWS TGFTP relay"
TRANSPORT = "official_noaa_tgftp_taf_cycle"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
USER_AGENT = "weather-bayes-basket-target/1.0"

_STAMP = re.compile(r"^(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2})\s*$")
_ISSUE = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z\b")
_VALIDITY = re.compile(r"\b(\d{2})(\d{2})/(\d{2})(\d{2})\b")


FALLBACK_ENV = "PM_TGFTP_FALLBACK"


def fallback_enabled() -> bool:
    """Is the second door open?  On everywhere but the test suite.

    The fallback reaches the network, and a test that makes AWC fail is
    testing what happens when AWC fails, not what TGFTP answers today.
    """

    return str(os.environ.get(FALLBACK_ENV, "on")).strip().lower() not in {
        "off", "0", "false", "no",
    }


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day_in_month(reference: datetime, day: int, hour: int, minute: int) -> datetime:
    """Resolve a DDHHMM group against the file's own date.

    A bulletin's group carries the day of the month only.  The cycle file
    stamps the calendar date it was published on, so a group whose day is
    ahead of it belongs to the month before (a 31st bulletin read from a 1st
    file), and one far behind it to the month after.
    """

    candidates = []
    for month_offset in (-1, 0, 1):
        anchor = (reference.replace(day=15) + timedelta(days=31 * month_offset))
        try:
            candidate = anchor.replace(
                day=day, hour=hour % 24, minute=minute, second=0, microsecond=0
            )
        except ValueError:
            continue
        candidate += timedelta(days=hour // 24)
        candidates.append(candidate)
    if not candidates:
        raise ValueError("tgftp_taf_day_group_invalid")
    return min(candidates, key=lambda value: abs((value - reference).total_seconds()))


def parse_cycle(text: str, *, stations: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """AWC-shaped rows from one cycle file.

    A bulletin starts at a ``TAF`` line and continues through the indented
    lines under it; the stamp above it is when TGFTP published it.
    """

    wanted = {str(value).strip().upper() for value in (stations or ())}
    rows: list[dict[str, Any]] = []
    stamp: datetime | None = None
    current: list[str] | None = None
    current_stamp: datetime | None = None

    def flush() -> None:
        nonlocal current, current_stamp
        if current is None or current_stamp is None:
            current = None
            return
        raw = " ".join(part.strip() for part in current if part.strip())
        current = None
        parts = raw.split()
        if len(parts) < 3 or parts[0].upper() != "TAF":
            return
        index = 2 if parts[1].upper() in {"AMD", "COR"} else 1
        station = parts[index].upper()
        if wanted and station not in wanted:
            return
        issue = _ISSUE.search(" ".join(parts[index + 1: index + 3]))
        validity = _VALIDITY.search(raw)
        if not issue or not validity:
            return
        try:
            issued_at = _day_in_month(
                current_stamp, int(issue.group(1)), int(issue.group(2)), int(issue.group(3))
            )
            valid_from = _day_in_month(
                current_stamp, int(validity.group(1)), int(validity.group(2)), 0
            )
            valid_to = _day_in_month(
                current_stamp, int(validity.group(3)), int(validity.group(4)), 0
            )
        except ValueError:
            return
        if valid_to <= valid_from:
            valid_to += timedelta(days=1)
        # TGFTP stamps when it published the bulletin.  A stamp ahead of the
        # issue instant is the honest availability; one behind it (the file
        # names the hour, the bulletin the minute) would claim the bulletin
        # was readable before it existed, so the issue instant stands.
        available_at = max(current_stamp, issued_at)
        rows.append({
            "icaoId": station,
            "rawTAF": raw,
            "issueTime": _iso(issued_at),
            "dbPopTime": _iso(available_at),
            "validTimeFrom": _iso(valid_from),
            "validTimeTo": _iso(valid_to),
            # No decoded segments exist on this door; the capture reads the
            # TX group from the text and serves without the weather fields.
            "fcsts": [],
            "tgftp_origin": True,
        })

    for line in text.splitlines():
        matched = _STAMP.match(line)
        if matched:
            flush()
            stamp = datetime(
                int(matched.group(1)), int(matched.group(2)), int(matched.group(3)),
                int(matched.group(4)), int(matched.group(5)), tzinfo=timezone.utc,
            )
            continue
        if not line.strip():
            flush()
            continue
        if line[:1].isspace():
            if current is not None:
                current.append(line)
            continue
        flush()
        if line.upper().startswith("TAF"):
            current = [line]
            current_stamp = stamp
    flush()
    return rows


# One cycle file is 1.3-1.6 MB and every station in a round wants the same
# three.  Asked per station that is 135 downloads a round, which is how the
# TAF ingress took 694 seconds on 2026-09-20; the file itself only changes
# once an hour, so a round shares one copy.
_CYCLE_CACHE: dict[str, tuple[float, bytes]] = {}
CYCLE_CACHE_TTL_SEC = 240.0


def _cached_get(url: str, *, timeout_sec: int) -> bytes:
    now = time.monotonic()
    hit = _CYCLE_CACHE.get(url)
    if hit is not None and now - hit[0] < CYCLE_CACHE_TTL_SEC:
        return hit[1]
    payload = _get(url, timeout_sec=timeout_sec)
    for key, (stamp, _value) in list(_CYCLE_CACHE.items()):
        if now - stamp >= CYCLE_CACHE_TTL_SEC:
            _CYCLE_CACHE.pop(key, None)
    _CYCLE_CACHE[url] = (now, payload)
    return payload


def _get(url: str, *, timeout_sec: int) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/plain"}
    )
    direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with direct_opener.open(request, timeout=max(2, int(timeout_sec))) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except Exception:
        command = [
            "/usr/bin/curl", "--fail-with-body", "--silent", "--show-error",
            "--location", "--proto", "=https", "--tlsv1.2",
            "--max-time", str(max(2, int(timeout_sec))),
            "--max-filesize", str(MAX_RESPONSE_BYTES),
            "--noproxy", "tgftp.nws.noaa.gov",
            "--header", f"User-Agent: {USER_AGENT}",
            "--header", "Accept: text/plain",
            url,
        ]
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=max(4, int(timeout_sec) + 2), check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tgftp_taf_curl_exit_{result.returncode}")
        payload = result.stdout
    if len(payload) > MAX_RESPONSE_BYTES:
        raise RuntimeError("tgftp_taf_response_too_large")
    return bytes(payload)


def fetch_taf_rows(
    station_id: str,
    *,
    timeout_sec: int = 12,
    cycles: int = 3,
    reference: datetime | None = None,
    getter=None,
) -> tuple[list[dict[str, Any]], str, str, int]:
    """Rows, the url read, the response hash and its size.

    Three cycles by default: a station whose newest bulletin is two hours old
    (ZUUU issues every six) is still answerable, and the capture picks the
    newest bulletin available at its own anchor out of whatever comes back.
    """

    station = str(station_id).strip().upper()
    if not station:
        raise ValueError("tgftp_taf_station_required")
    now = reference or datetime.now(timezone.utc)
    fetch = getter or (lambda url: _cached_get(url, timeout_sec=timeout_sec))
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    digest = hashlib.sha256()
    total = 0
    urls: list[str] = []
    errors: list[str] = []
    # TGFTP files TAFs by issuance cycle, and only the four exist (00/06/12/18Z);
    # the hours between them redirect.  Read the newest cycles backwards from
    # the one in force now.
    current = (now.hour // 6) * 6
    for offset in range(max(1, int(cycles)) - 1, -1, -1):
        moment = now - timedelta(hours=6 * offset)
        url = TAF_CYCLE_ENDPOINT.format(cycle=((current - 6 * offset) % 24))
        try:
            payload = fetch(url)
        except Exception as exc:  # a missing cycle is not a failed fetch
            errors.append(f"{moment.hour:02d}Z:{type(exc).__name__}")
            continue
        urls.append(url)
        digest.update(payload)
        total += len(payload)
        for row in parse_cycle(payload.decode("utf-8", "replace"), stations=[station]):
            rows[(row["issueTime"], row["rawTAF"])] = row
    if not rows:
        raise RuntimeError("tgftp_taf_no_rows:" + ",".join(errors or ["empty"]))
    return (
        sorted(rows.values(), key=lambda row: (row["issueTime"], row["rawTAF"])),
        ";".join(urls),
        digest.hexdigest(),
        total,
    )


def is_tgftp_row(row: Mapping[str, Any] | None) -> bool:
    return bool(row and row.get("tgftp_origin") is True)


__all__ = [
    "ORIGIN_AUTHORITY", "ORIGIN_SOURCE_ID", "PROVIDER", "TAF_CYCLE_ENDPOINT",
    "TRANSPORT", "FALLBACK_ENV", "fallback_enabled", "fetch_taf_rows",
    "is_tgftp_row", "parse_cycle",
]
