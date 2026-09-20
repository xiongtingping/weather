"""Temperature-only AMSC and AWC METAR/SPECI acquisition clients."""

from __future__ import annotations

import calendar
import csv
import gzip
import hashlib
import io
import json
import math
import os
import pwd
import re
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
AMSC_ENDPOINT = "https://www.amsc.net.cn/gateway/api/saas/rest/amc/ReportController/messageRetrievalSort"
AWC_ENDPOINT = "https://aviationweather.gov/api/data/metar"
AWC_CACHE_ENDPOINT = "https://aviationweather.gov/data/cache/metars.cache.csv.gz"
# Charter 162 (2026-09-20): the same NOAA reports off a second host, for the
# hours AWC itself is unreachable -- its TLS certificate expired at
# 2026-09-19T23:59:59Z and every AWC transport above failed on it at once,
# which froze the fleet's running highs and the truth lane with no second
# door.  TGFTP publishes one file per clock hour holding the latest report
# from every station, so one bounded request serves all forty-five.
NOAA_TGFTP_CYCLE_ENDPOINT = (
    "https://tgftp.nws.noaa.gov/data/observations/metar/cycles/{cycle:02d}Z.TXT"
)
NOAA_TGFTP_ORIGIN_SOURCE_ID = "global_noaa_tgftp_relay"
NOAA_TGFTP_ORIGIN_AUTHORITY = "NOAA/NWS TGFTP worldwide METAR cycle relay"
NOAA_TGFTP_ORIGIN_URL = "https://tgftp.nws.noaa.gov/data/observations/metar/"
NOAA_TGFTP_TRANSPORT = "official_noaa_tgftp_metar_cycle"
NOAA_TGFTP_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# The second door reaches the network, and a test that makes AWC fail is
# testing that failure, not what TGFTP answers today; the suite shuts it
# (tests/conftest.py) and production leaves it open.
TGFTP_FALLBACK_ENV = "PM_TGFTP_FALLBACK"


def tgftp_fallback_enabled() -> bool:
    return str(os.environ.get(TGFTP_FALLBACK_ENV, "on")).strip().lower() not in {
        "off", "0", "false", "no",
    }
AMSC_INTERMEDIATE_CA = (
    ROOT / "config" / "certs" / "globalsign-rsa-ov-ssl-ca-2018.pem"
)
AMSC_INTERMEDIATE_CA_SHA256 = (
    "20ab099881f1a46dd93b360ba3fd28fba95d37e36067e85e79db0ede29a5e963"
)
TOKEN_RE = re.compile(
    rb"\d{1,12}\$\$[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_AMSC_TOKEN_CACHE: tuple[float, list[str]] = (0.0, [])
AMSC_TOKEN_CACHE_TTL_SEC = 300.0
AMSC_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
AMSC_RESPONSE_CHUNK_BYTES = 64 * 1024
AWC_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# The global AWC cache is compressed.  Its wire-size cap alone cannot bound a
# highly-compressible response, so account for the expanded byte stream before
# CSV decoding.  This deliberately remains separate from the 4 MiB response
# cap above: both limits must pass.
# A full worldwide cache can legitimately exceed the former 64 MiB allowance.
# Keep the guard finite, streamed and fail-closed, but leave enough room for
# the official 45-station production lane without treating a normal cache as
# an oversized response.
AWC_MAX_DECOMPRESSED_CACHE_BYTES = 256 * 1024 * 1024
AWC_MAX_CACHE_LINE_BYTES = 1024 * 1024


def _amsc_intermediate_ca_path() -> Path:
    """Return the pinned issuer needed by AMSC's incomplete TLS chain."""

    try:
        payload = AMSC_INTERMEDIATE_CA.read_bytes()
    except OSError as exc:
        raise RuntimeError("amsc_intermediate_ca_unavailable") from exc
    if (
        len(payload) > 16 * 1024
        or hashlib.sha256(payload).hexdigest()
        != AMSC_INTERMEDIATE_CA_SHA256
    ):
        raise RuntimeError("amsc_intermediate_ca_integrity_failed")
    return AMSC_INTERMEDIATE_CA


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _read_bounded_bytes(
    response: Any,
    *,
    limit: int = AMSC_MAX_RESPONSE_BYTES,
) -> bytes:
    """Read one official response without allowing an unbounded body into RSS."""

    try:
        declared = int(str(response.headers.get("Content-Length") or "0"))
    except (AttributeError, TypeError, ValueError):
        declared = 0
    if declared > limit:
        raise RuntimeError("amsc_response_too_large")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(AMSC_RESPONSE_CHUNK_BYTES, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise RuntimeError("amsc_response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _read_bounded_response(
    response: Any,
    *,
    limit: int = AMSC_MAX_RESPONSE_BYTES,
) -> str:
    return _read_bounded_bytes(response, limit=limit).decode(
        "utf-8",
        "replace",
    )


def _iter_bounded_awc_cache_lines(compressed: bytes) -> Iterator[str]:
    """Expand the AWC gzip cache incrementally under strict byte limits.

    ``gzip.decompress`` materializes the complete expanded cache before the
    caller can inspect its size.  The cache fallback is a production input, so
    an expansion or a pathological CSV row beyond its bound is a fail-closed
    source error rather than a reason to continue to another transport.
    """

    total = 0
    pending = bytearray()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as stream:
            while True:
                chunk = stream.read(AMSC_RESPONSE_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > AWC_MAX_DECOMPRESSED_CACHE_BYTES:
                    raise RuntimeError("awc_cache_decompressed_too_large")
                pending.extend(chunk)
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        break
                    if newline + 1 > AWC_MAX_CACHE_LINE_BYTES:
                        raise RuntimeError("awc_cache_line_too_large")
                    yield bytes(pending[: newline + 1]).decode("utf-8", "replace")
                    del pending[: newline + 1]
                if len(pending) > AWC_MAX_CACHE_LINE_BYTES:
                    raise RuntimeError("awc_cache_line_too_large")
    except RuntimeError:
        raise
    except (EOFError, OSError) as exc:
        raise RuntimeError("awc_cache_gzip_invalid") from exc
    if pending:
        yield bytes(pending).decode("utf-8", "replace")


def _curl_official_get(
    url: str,
    *,
    timeout: float,
    max_response_bytes: int,
    accept: str,
    bypass_proxy_host: str = "",
) -> bytes:
    """Fetch one official HTTPS object through system-verified curl.

    Shadowrocket can install its local CA in the macOS trust store without
    adding it to Python's bundled CA file.  Curl uses the system trust store,
    keeps hostname/certificate verification enabled, and remains bounded by
    both wall-clock and response-size limits.  ``-k`` is deliberately absent.
    """

    seconds = max(1, int(math.ceil(float(timeout))))
    command = [
            "/usr/bin/curl",
            "-fsSL",
            "--compressed",
            "--proto",
            "=https",
            "--tlsv1.2",
            "--connect-timeout",
            str(min(5, seconds)),
            "--max-time",
            str(seconds),
            "--retry",
            "0",
            "--max-filesize",
            str(int(max_response_bytes)),
            "-H",
            f"Accept: {accept}",
            "-H",
            "User-Agent: weather-reported-trajectory-no/1.0",
    ]
    if bypass_proxy_host:
        command.extend(["--noproxy", bypass_proxy_host])
    command.append(url)
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=seconds + 2,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"official_curl_exit_{result.returncode}")
    if len(result.stdout) > int(max_response_bytes):
        raise RuntimeError("official_curl_response_too_large")
    return bytes(result.stdout)


def _chrome_roots() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("AMSC_CHROME_PROFILE_ROOT", "").strip()
    if configured:
        roots.append(Path(configured).expanduser())
    homes = [Path.home()]
    try:
        owner_home = Path(pwd.getpwuid(ROOT.stat().st_uid).pw_dir)
    except (KeyError, OSError):
        owner_home = None
    if owner_home is not None and owner_home not in homes:
        homes.append(owner_home)
    for home in homes:
        roots.extend(
            [
                home / "Library/Application Support/Google/Chrome",
                home / "Library/Application Support/Chromium",
            ]
        )
    return list(dict.fromkeys(roots))


def amsc_token_candidates(*, force_refresh: bool = False) -> list[str]:
    global _AMSC_TOKEN_CACHE
    now = datetime.now(timezone.utc).timestamp()
    cached_at, cached_tokens = _AMSC_TOKEN_CACHE
    if not force_refresh and cached_tokens and now - cached_at <= AMSC_TOKEN_CACHE_TTL_SEC:
        return list(cached_tokens)
    seen: set[str] = set()
    out: list[str] = []
    for name in ("AMSC_SESSION_ID", "AMSC_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    for root in _chrome_roots():
        if not root.exists():
            continue
        for leveldb in root.glob("*/Local Storage/leveldb"):
            try:
                paths = list(leveldb.iterdir())
            except OSError:
                continue
            for path in paths:
                if not path.is_file() or path.suffix not in {".ldb", ".log", ""}:
                    continue
                try:
                    blob = path.read_bytes()
                except OSError:
                    continue
                for match in TOKEN_RE.finditer(blob):
                    token = match.group(0).decode("ascii")
                    if token not in seen:
                        seen.add(token)
                        out.append(token)
    _AMSC_TOKEN_CACHE = (now, list(out))
    return out


def _amsc_one(station_id: str, token: str, *, count: int, timeout: float) -> dict[str, Any]:
    station_ids = sorted({item.strip().upper() for item in station_id.split(",") if item.strip()})
    params = {
        "isCCCC": "cccc",
        "cccc": ",".join(station_ids),
        "count": str(count),
        # Explicitly exclude FC/FT (TAF).  SA/SP are METAR/SPECI only.
        "tt": "SA,SP,",
        "token": token,
    }
    url = AMSC_ENDPOINT + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "sessionId": token,
            "app": "AMS",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_started_at = utc_now()
            payload = json.loads(_read_bounded_response(response))
            status = int(response.status)
    except Exception as urllib_exc:
        command = [
            "/usr/bin/curl", "-fsSL", "--compressed",
            "--proto", "=https", "--tlsv1.2",
            "--cacert", str(_amsc_intermediate_ca_path()),
            "--connect-timeout", str(max(1, min(3, int(timeout)))),
            "--max-time", str(max(1, int(timeout))), "--retry", "0", "--max-filesize", str(AMSC_MAX_RESPONSE_BYTES),
            "-G", AMSC_ENDPOINT,
            "-H", f"sessionId: {token}", "-H", "app: AMS",
            "-H", "Content-Type: application/json",
            "-H", "Accept: application/json",
            "-H", "User-Agent: Mozilla/5.0",
        ]
        for key, value in params.items():
            command.extend(["--data-urlencode", f"{key}={value}"])
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 2)
        if result.returncode != 0:
            raise RuntimeError(
                f"amsc_transport_failed:{type(urllib_exc).__name__}:curl_exit_{result.returncode}"
            ) from urllib_exc
        if len(result.stdout.encode("utf-8", "replace")) > AMSC_MAX_RESPONSE_BYTES:
            raise RuntimeError("amsc_response_too_large")
        response_started_at = ""
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("amsc_response_json_invalid") from exc
        status = 200
    try:
        response_code = int(payload.get("code"))
    except (TypeError, ValueError):
        response_code = None
    try:
        error_code = int(payload.get("errCode"))
    except (TypeError, ValueError):
        error_code = None
    if error_code == -12005:
        raise RuntimeError("amsc_session_expired")
    if response_code != 200:
        stable_code = (
            str(error_code)
            if error_code is not None
            else str(response_code)
            if response_code is not None
            else "unknown"
        )
        raise RuntimeError(f"amsc_application_error:{stable_code}")
    reports: list[dict[str, str]] = []
    station_set = set(station_ids)
    for row in payload.get("data") or []:
        if not isinstance(row, Mapping):
            continue
        raw = str(row.get("content") or "").strip()
        parts = raw.split()
        report_station = ""
        if len(parts) >= 2:
            report_station = parts[2] if parts[1] in {"COR", "AMD"} and len(parts) >= 3 else parts[1]
        if raw.startswith(("METAR ", "SPECI ")) and report_station in station_set:
            reports.append({"station_id": report_station, "raw_text": raw, "source_report_time": ""})
    return {
        "station_ids": station_ids,
        "status": status,
        "response_started_at": response_started_at,
        "reports": reports,
    }


def fetch_amsc_temperature_reports(
    station_ids: Sequence[str], *, count: int = 6, timeout: float = 5.0, max_workers: int = 9
) -> dict[str, Any]:
    stations = sorted({str(value).strip().upper() for value in station_ids if str(value).strip()})
    tokens = amsc_token_candidates()
    if not tokens:
        raise RuntimeError("amsc_session_unavailable")
    last_errors: list[str] = []
    attempted: set[str] = set()
    saw_expired_session = False

    def attempt(candidates: Sequence[str]) -> dict[str, Any] | None:
        nonlocal saw_expired_session
        for token in candidates:
            if token in attempted:
                continue
            attempted.add(token)
            try:
                # AMSC accepts a comma-separated CCCC set. One request avoids
                # nationwide serial waits and per-station request storms.
                row = _amsc_one(
                    ",".join(stations), token, count=count, timeout=timeout
                )
            except Exception as exc:
                reason = str(exc)
                if reason == "amsc_session_expired":
                    saw_expired_session = True
                # Never include a token or an upstream response body in
                # diagnostics. Error classes and stable local codes suffice.
                last_errors.append(
                    f"batch:{type(exc).__name__}:{reason[:120]}"
                )
                continue
            if row:
                return {
                    "source_lane": "amsc",
                    "station_ids": stations,
                    "response_started_at": str(
                        row.get("response_started_at") or ""
                    ),
                    "status": int(row.get("status") or 200),
                    "reports": list(row.get("reports") or []),
                    "station_errors": sorted(set(last_errors)),
                    "taf_requested": False,
                    "transport": "official_amsc_batch_cccc",
                }
        return None

    result = attempt(tokens)
    if result is not None:
        return result

    global _AMSC_TOKEN_CACHE
    if saw_expired_session:
        # One bounded rescan lets a freshly established browser login replace
        # an expired cached session. Already-attempted candidates are never
        # retried, so a stale LevelDB record cannot cause a loop.
        _AMSC_TOKEN_CACHE = (0.0, [])
        refreshed = attempt(amsc_token_candidates(force_refresh=True))
        if refreshed is not None:
            return refreshed
        _AMSC_TOKEN_CACHE = (0.0, [])
        if all(
            item.endswith(":amsc_session_expired")
            for item in last_errors
        ):
            raise RuntimeError("amsc_session_expired")

    _AMSC_TOKEN_CACHE = (0.0, [])
    raise RuntimeError(
        "amsc_all_tokens_failed:" + ",".join(sorted(set(last_errors)))
    )


def _awc_one(stations: Sequence[str], *, hours: float, timeout: float) -> dict[str, Any]:
    station_list = list(stations)
    params = {"ids": ",".join(station_list), "format": "json", "taf": "false", "hours": f"{hours:g}"}
    url = AWC_ENDPOINT + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "weather-reported-trajectory-no/1.0", "Accept": "application/json"},
    )
    # Per-station fallback must also bypass the machine-wide proxy.  The
    # proxy's CONNECT timeout was turning an otherwise usable official AWC
    # fallback into a multi-minute partial batch.
    direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with direct_opener.open(request, timeout=timeout) as response:
            response_started_at = utc_now()
            raw = _read_bounded_response(
                response,
                limit=AWC_MAX_RESPONSE_BYTES,
            )
            status = int(response.status)
    except Exception:
        raw = _curl_official_get(
            url,
            timeout=timeout,
            max_response_bytes=AWC_MAX_RESPONSE_BYTES,
            accept="application/json",
        ).decode("utf-8", "replace")
        response_started_at = utc_now()
        status = 200
    value = json.loads(raw) if raw.strip() else []
    rows = value if isinstance(value, list) else value.get("data", []) if isinstance(value, Mapping) else []
    station_set = set(station_list)
    reports: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("icaoId") or "").strip().upper()
        text = str(row.get("rawOb") or "").strip()
        if station in station_set and text.startswith(("METAR ", "SPECI ")):
            reports.append(
                {
                    "station_id": station,
                    "raw_text": text,
                    "source_report_time": str(row.get("reportTime") or row.get("obsTime") or "").strip(),
                }
            )
    return {
        "station_ids": station_list,
        "response_started_at": response_started_at,
        "status": status,
        "reports": reports,
    }


def _tgftp_cycle_reports(
    stations: "set[str]", *, cycle_hour: int, timeout: float
) -> list[dict[str, str]]:
    """One cycle file, filtered to the stations asked for.

    Each report is two lines: an issue stamp (``YYYY/MM/DD HH:MM``) and the
    bulletin body, which -- unlike AWC's raw API -- carries no ``METAR``
    keyword.  The keyword is what every reader downstream types the report
    by, so a body that lacks it is a routine METAR and is spelled as one.
    """

    url = NOAA_TGFTP_CYCLE_ENDPOINT.format(cycle=int(cycle_hour) % 24)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "weather-reported-trajectory-no/1.0",
            "Accept": "text/plain",
        },
    )
    direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with direct_opener.open(request, timeout=max(2.0, float(timeout))) as response:
            text = _read_bounded_response(
                response, limit=NOAA_TGFTP_MAX_RESPONSE_BYTES
            )
    except Exception:
        text = _curl_official_get(
            url,
            timeout=max(timeout, 8.0),
            max_response_bytes=NOAA_TGFTP_MAX_RESPONSE_BYTES,
            accept="text/plain",
            bypass_proxy_host="tgftp.nws.noaa.gov",
        ).decode("utf-8", "replace")
    reports: list[dict[str, str]] = []
    issued_at = ""
    for line in text.splitlines():
        body = line.strip()
        if not body:
            continue
        stamp = re.fullmatch(r"(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2})", body)
        if stamp:
            issued_at = (
                f"{stamp.group(1)}-{stamp.group(2)}-{stamp.group(3)}"
                f"T{stamp.group(4)}:{stamp.group(5)}:00Z"
            )
            continue
        parts = body.split()
        if not parts:
            continue
        keyword = parts[0].upper()
        if keyword in {"METAR", "SPECI"}:
            raw_text = body
            station = parts[2] if len(parts) > 2 and parts[1] in {"COR", "AMD"} else (
                parts[1] if len(parts) > 1 else ""
            )
        else:
            raw_text = "METAR " + body
            station = keyword
        if station not in stations:
            continue
        reports.append(
            {
                "station_id": station,
                "raw_text": raw_text,
                "source_report_time": issued_at,
                "origin_source_id": NOAA_TGFTP_ORIGIN_SOURCE_ID,
                "origin_authority": NOAA_TGFTP_ORIGIN_AUTHORITY,
                "origin_url": NOAA_TGFTP_ORIGIN_URL,
                "origin_transport": NOAA_TGFTP_TRANSPORT,
                "origin_fallback_used": True,
            }
        )
    return reports


def fetch_noaa_tgftp_temperature_reports(
    station_ids: Sequence[str],
    *,
    timeout: float = 8.0,
    cycles: int = 2,
    reference: datetime | None = None,
) -> dict[str, Any]:
    """The latest report per station from the newest ``cycles`` hour files.

    Two cycles by default: the hour just started holds only what has already
    landed in it, so the one before it is what covers a station that reports
    on the half hour.  Later cycles win on duplicates.
    """

    stations = sorted({str(value).strip().upper() for value in station_ids if str(value).strip()})
    if not stations:
        raise RuntimeError("noaa_tgftp_no_stations")
    now = reference or datetime.now(timezone.utc)
    station_set = set(stations)
    response_started_at = utc_now()
    latest: dict[tuple[str, str], dict[str, str]] = {}
    errors: list[str] = []
    ordered = [now - timedelta(hours=offset) for offset in range(max(1, int(cycles)) - 1, -1, -1)]
    for moment in ordered:
        try:
            for report in _tgftp_cycle_reports(
                station_set, cycle_hour=moment.hour, timeout=timeout
            ):
                latest[(report["station_id"], report["raw_text"])] = report
        except Exception as exc:  # one missing cycle is not a failed poll
            errors.append(f"tgftp_cycle_{moment.hour:02d}:{type(exc).__name__}")
    if not latest:
        raise RuntimeError("noaa_tgftp_no_reports:" + ",".join(errors))
    return {
        "source_lane": "awc",
        "station_ids": stations,
        "response_started_at": response_started_at,
        "status": 200 if not errors else 206,
        "reports": sorted(latest.values(), key=lambda row: (row["station_id"], row["raw_text"])),
        "station_errors": sorted(set(errors)),
        "taf_requested": False,
        "transport": NOAA_TGFTP_TRANSPORT,
    }


def fetch_awc_temperature_reports(
    station_ids: Sequence[str], *, hours: float, timeout: float = 6.0, batch_size: int = 10, max_workers: int = 6,
    date_anchor: str | None = None,
) -> dict[str, Any]:
    """``date_anchor`` reads the window ending at that instant, not at now.

    Without it, AWC looks back from the present and the 400-report response
    cap decides how far back that reaches -- for a half-hourly station about
    eight days, silently.  An anchored request is how a day deeper inside
    AWC's 30-day retention stays reachable at all.
    """

    stations = sorted({str(value).strip().upper() for value in station_ids if str(value).strip()})
    raw_api_error = ""
    try:
        params = {
            "ids": ",".join(stations),
            "format": "raw",
            "taf": "false",
            "hours": f"{float(hours):g}",
        }
        if date_anchor:
            params["date"] = str(date_anchor)
        url = AWC_ENDPOINT + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "weather-reported-trajectory-no/1.0", "Accept": "text/plain"},
        )
        # The machine-wide proxy adds 1-6 seconds of CONNECT/TLS latency to
        # this public official endpoint.  AWC is reachable directly, so the
        # production hot path explicitly bypasses proxy discovery.  Fallbacks
        # below remain official AWC transports and are used only on failure.
        direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with direct_opener.open(
                request,
                timeout=max(1.0, float(timeout)),
            ) as response:
                response_started_at = utc_now()
                text = _read_bounded_response(
                    response,
                    limit=AWC_MAX_RESPONSE_BYTES,
                )
                status = int(response.status)
            transport = "official_awc_raw_metar_batch_direct"
        except Exception:
            text = _curl_official_get(
                url,
                timeout=timeout,
                max_response_bytes=AWC_MAX_RESPONSE_BYTES,
                accept="text/plain",
                bypass_proxy_host="aviationweather.gov",
            ).decode("utf-8", "replace")
            response_started_at = utc_now()
            status = 200
            transport = (
                "official_awc_raw_metar_batch_curl_system_ca"
            )
        station_set = set(stations)
        reports: list[dict[str, str]] = []
        for line in text.splitlines():
            raw_text = line.strip()
            parts = raw_text.split()
            report_station = parts[2] if len(parts) > 2 and parts[1] in {"COR", "AMD"} else parts[1] if len(parts) > 1 else ""
            if raw_text.startswith(("METAR ", "SPECI ")) and report_station in station_set:
                reports.append({"station_id": report_station, "raw_text": raw_text, "source_report_time": ""})
        return {
            "source_lane": "awc",
            "station_ids": stations,
            "response_started_at": response_started_at,
            "status": status,
            "reports": reports,
            "station_errors": [],
            "taf_requested": False,
            "transport": transport,
        }
    except Exception as exc:
        raw_api_error = f"awc_raw_api:{type(exc).__name__}"
    # The official global cache is one bounded request for all stations and is
    # materially more stable than dozens of per-station API calls.  Only the
    # raw report, station and observation time columns are consumed.
    cache_error = ""
    try:
        request = urllib.request.Request(
            AWC_CACHE_ENDPOINT,
            headers={"User-Agent": "weather-reported-trajectory-no/1.0", "Accept": "application/gzip"},
        )
        # Keep the official cache fallback on the same direct transport as
        # the primary AWC endpoint.  Proxy CONNECT/TLS failures were causing
        # the fallback to return old/partial batches during a live poll.
        direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with direct_opener.open(
                request,
                timeout=max(timeout, 8.0),
            ) as response:
                response_started_at = utc_now()
                compressed = _read_bounded_bytes(
                    response,
                    limit=AWC_MAX_RESPONSE_BYTES,
                )
                status = int(response.status)
            transport = "official_awc_global_metar_cache"
        except Exception:
            compressed = _curl_official_get(
                AWC_CACHE_ENDPOINT,
                timeout=max(timeout, 8.0),
                max_response_bytes=AWC_MAX_RESPONSE_BYTES,
                accept="application/gzip",
                bypass_proxy_host="aviationweather.gov",
            )
            response_started_at = utc_now()
            status = 200
            transport = (
                "official_awc_global_metar_cache_curl_system_ca"
            )
        station_set = set(stations)
        reports: list[dict[str, str]] = []
        reader = csv.DictReader(_iter_bounded_awc_cache_lines(compressed))
        for row in reader:
            station = str(row.get("station_id") or "").strip().upper()
            raw_text = str(row.get("raw_text") or "").strip()
            if station in station_set and raw_text.startswith(("METAR ", "SPECI ")):
                reports.append(
                    {
                        "station_id": station,
                        "raw_text": raw_text,
                        "source_report_time": str(row.get("observation_time") or "").strip(),
                    }
                )
        return {
            "source_lane": "awc",
            "station_ids": stations,
            "response_started_at": response_started_at,
            "status": status,
            "reports": reports,
            "station_errors": [],
            "taf_requested": False,
            "transport": transport,
        }
    except RuntimeError as exc:
        # Resource-limit violations are not transport failures.  Falling back
        # after an oversized expansion would defeat the required fail-closed
        # boundary for the same source poll.
        if str(exc) in {
            "awc_cache_decompressed_too_large",
            "awc_cache_line_too_large",
        }:
            raise
        cache_error = f"awc_cache:{type(exc).__name__}"
    except Exception as exc:
        cache_error = f"awc_cache:{type(exc).__name__}"
    batches = [stations[index : index + max(1, int(batch_size))] for index in range(0, len(stations), max(1, int(batch_size)))]
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), len(batches) or 1))) as executor:
        futures = {executor.submit(_awc_one, batch, hours=hours, timeout=timeout): batch for batch in batches}
        for future in as_completed(futures):
            batch = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                errors.append(f"{','.join(batch)}:{type(exc).__name__}")
    if not rows:
        # Charter 162: every AWC door is shut at once only when AWC itself is
        # down (its certificate expired on 2026-09-19).  The same NOAA reports
        # are on TGFTP; the reports carry their true origin so nothing has to
        # pretend they came from AWC.
        reason = "awc_all_transports_failed:" + raw_api_error + "," + cache_error + "," + ",".join(errors)
        if not tgftp_fallback_enabled():
            raise RuntimeError(reason)
        try:
            fallback = fetch_noaa_tgftp_temperature_reports(stations, timeout=timeout)
        except Exception as tgftp_exc:
            raise RuntimeError(reason + ",tgftp:" + type(tgftp_exc).__name__) from tgftp_exc
        for report in fallback["reports"]:
            report["origin_fallback_reason"] = reason[:200]
        fallback["station_errors"] = sorted({*fallback.get("station_errors", []), reason[:200]})
        return fallback
    return {
        "source_lane": "awc",
        "station_ids": stations,
        "response_started_at": min((str(row.get("response_started_at") or "") for row in rows if row.get("response_started_at")), default=""),
        "status": 200 if not errors else 206,
        "reports": [report for row in rows for report in row.get("reports") or []],
        "station_errors": sorted(set([raw_api_error, cache_error, *errors])),
        "taf_requested": False,
        "transport": "official_awc_api_batch_fallback",
    }


def infer_ddhhmm_time(*, day: int, hour: int, minute: int, reference: datetime) -> datetime:
    reference = reference.astimezone(timezone.utc)
    center = reference.replace(day=15, hour=0, minute=0, second=0, microsecond=0)
    anchors = [center - timedelta(days=32), center, center + timedelta(days=32)]
    candidates: list[datetime] = []
    for anchor in anchors:
        if 1 <= day <= calendar.monthrange(anchor.year, anchor.month)[1]:
            candidates.append(datetime(anchor.year, anchor.month, day, hour, minute, tzinfo=timezone.utc))
    if not candidates:
        raise ValueError("metar_ddhhmm_time_invalid")
    return min(candidates, key=lambda value: abs((value - reference).total_seconds()))


__all__ = [
    "AMSC_ENDPOINT", "AWC_CACHE_ENDPOINT", "AWC_ENDPOINT", "amsc_token_candidates", "fetch_amsc_temperature_reports",
    "fetch_awc_temperature_reports", "infer_ddhhmm_time", "utc_now",
]
