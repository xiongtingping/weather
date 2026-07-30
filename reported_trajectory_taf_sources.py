"""Isolated official AMSC TAF acquisition for the weak forecast lane.

The hard-NO lane does not import this module.  Keeping the forecast client in
its own module makes that dependency wall auditable instead of relying on a
call-site flag inside a mixed source client.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from typing import Any, Mapping, Sequence

import reported_trajectory_sources as _metar_sources


def _amsc_taf_one(station_id: str, token: str, *, count: int, timeout: float) -> dict[str, Any]:
    station_ids = sorted({item.strip().upper() for item in station_id.split(",") if item.strip()})
    params = {
        "isCCCC": "cccc",
        "cccc": ",".join(station_ids),
        "count": str(count),
        "tt": "FC,FT,",
        "token": token,
    }
    request = urllib.request.Request(
        _metar_sources.AMSC_ENDPOINT + "?" + urllib.parse.urlencode(params),
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
            response_started_at = _metar_sources.utc_now()
            payload = json.loads(_metar_sources._read_bounded_response(response))
            status = int(response.status)
    except Exception as urllib_exc:
        command = [
            "curl", "-k", "-fsSL", "--compressed", "--connect-timeout", str(max(1, min(5, int(timeout)))),
            "--max-time", str(max(1, int(timeout))), "--max-filesize", str(_metar_sources.AMSC_MAX_RESPONSE_BYTES),
            "-G", _metar_sources.AMSC_ENDPOINT,
            "-H", f"sessionId: {token}", "-H", "app: AMS", "-H", "Content-Type: application/json",
        ]
        for key, value in params.items():
            command.extend(["--data-urlencode", f"{key}={value}"])
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 2)
        if result.returncode != 0:
            raise RuntimeError(
                f"amsc_taf_transport_failed:{type(urllib_exc).__name__}:curl_exit_{result.returncode}"
            ) from urllib_exc
        if len(result.stdout.encode("utf-8", "replace")) > _metar_sources.AMSC_MAX_RESPONSE_BYTES:
            raise RuntimeError("amsc_taf_response_too_large")
        response_started_at = ""
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("amsc_taf_response_json_invalid") from exc
        status = 200
    if payload.get("code") != 200:
        raise RuntimeError("amsc_taf_response_not_200")
    station_set = set(station_ids)
    reports: list[dict[str, str]] = []
    for row in payload.get("data") or []:
        if not isinstance(row, Mapping):
            continue
        raw = str(row.get("content") or "").strip().replace("\r", " ").replace("\n", " ")
        parts = raw.split()
        report_station = ""
        if len(parts) >= 2 and parts[0] == "TAF":
            report_station = parts[2] if len(parts) >= 3 and parts[1] in {"COR", "AMD"} else parts[1]
        if raw.startswith("TAF ") and report_station in station_set:
            reports.append({
                "station_id": report_station,
                "raw_text": raw,
                "source_report_time": str(row.get("oDateTime") or "").strip(),
                "report_type": "TAF",
                "taf_type": str(row.get("tt") or "FC").strip().upper(),
            })
    return {
        "station_ids": station_ids,
        "status": status,
        "response_started_at": response_started_at,
        "reports": reports,
    }


def fetch_amsc_taf_reports(
    station_ids: Sequence[str], *, count: int = 12, timeout: float = 8.0
) -> dict[str, Any]:
    stations = sorted({str(value).strip().upper() for value in station_ids if str(value).strip()})
    tokens = _metar_sources.amsc_token_candidates()
    if not tokens:
        raise RuntimeError("amsc_session_unavailable")
    last_errors: list[str] = []
    for token in tokens:
        try:
            row = _amsc_taf_one(",".join(stations), token, count=count, timeout=timeout)
        except Exception as exc:
            last_errors.append(f"batch:{type(exc).__name__}")
            continue
        return {
            "source_lane": "amsc_taf",
            "station_ids": stations,
            "response_started_at": str(row.get("response_started_at") or ""),
            "status": int(row.get("status") or 200),
            "reports": list(row.get("reports") or []),
            "station_errors": sorted(set(last_errors)),
            "taf_requested": True,
            "transport": "official_amsc_taf_batch_cccc",
        }
    _metar_sources._AMSC_TOKEN_CACHE = (0.0, [])
    raise RuntimeError("amsc_taf_all_tokens_failed:" + ",".join(sorted(set(last_errors))))


__all__ = ["fetch_amsc_taf_reports"]
