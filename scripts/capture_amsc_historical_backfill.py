#!/usr/bin/env python3
"""Fetch one official AMSC station-day report window into the isolated H archive.

This command never writes canonical observations, facts, posterior state,
decisions, orders, or the forward AMSC raw archive.  Default mode fetches and
validates only; `--apply` additionally appends to `data/shadow` after an
explicit confirmation phrase.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_historical_backfill_archive import (  # noqa: E402
    historical_station_day_query_contract,
    persist_amsc_historical_backfill,
    prepare_amsc_historical_backfill,
)
from scripts.fetch_zuuu_metar_taf import ENDPOINT, request_headers, token_candidates  # noqa: E402


DEFAULT_ARCHIVE_DIR = ROOT / "data" / "shadow" / "amsc_historical_backfill"
APPLY_CONFIRMATION = "CAPTURE_AMSC_HISTORICAL_BACKFILL"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _amsc_datetime(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _query_params(*, query: dict, token: str) -> dict[str, str]:
    return {
        "isCCCC": "cccc",
        "cccc": str(query["station_id"]),
        "starDate": _amsc_datetime(str(query["utc_start"])),
        "endDate": _amsc_datetime(str(query["utc_end"])),
        "tt": ",".join(query["report_type_codes"]) + ",",
        "token": token,
    }


def _curl_tls_verified_payload(*, url: str, token: str, timeout: int) -> dict:
    """Use macOS system trust through curl without exposing a token in argv.

    This fallback is limited to Python's local CA-chain failure.  Curl's
    normal TLS verification remains enabled: the command and its stdin config
    explicitly contain no ``--insecure``/``insecure`` option.
    """

    def quoted(value: str) -> str:
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    config = "\n".join([
        f"url = {quoted(url)}",
        # AMSC is the formal China-station truth source and is reachable by
        # direct TLS from this host.  The shared NWP proxy intermittently
        # refuses this endpoint; routing historical backfill through it turns
        # an available official response into a false data gap.  Keep this
        # isolated historical transport direct so the resident/production
        # source client and its process boundary are untouched.
        'noproxy = "*"',
        f"header = {quoted(f'sessionId: {token}')}",
        'header = "app: AMS"',
        'header = "Content-Type: application/json"',
        'header = "User-Agent: Mozilla/5.0"',
        "location",
        "compressed",
        "fail",
        "silent",
        "show-error",
        "connect-timeout = 5",
        f"max-time = {max(1, int(timeout))}",
        "",
    ])
    result = subprocess.run(
        ["/usr/bin/curl", "--config", "-"],
        input=config,
        text=True,
        capture_output=True,
        check=False,
        timeout=max(2, int(timeout) + 5),
    )
    if result.returncode != 0:
        raise RuntimeError("amsc_curl_tls_verified_request_failed")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("amsc_curl_response_not_object")
    return payload


def fetch_historical_amsc_payload(*, query: dict, token: str, timeout: int) -> tuple[dict, str, dict[str, str]]:
    """Fetch content with credentials confined to the request, never disk.

    The fallback retains TLS verification and only addresses the known local
    Python trust-store mismatch; it is not a permissive transport fallback.
    """

    params = _query_params(query=query, token=token)
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=request_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=max(1, int(timeout))) as response:
            body = response.read().decode("utf-8", "replace")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("amsc_urllib_response_not_object")
        return payload, _utc_now(), {"mode": "urllib_default_tls", "tls_verification": "verified"}
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", "")).lower()
        if "certificate_verify_failed" not in reason:
            raise
    payload = _curl_tls_verified_payload(url=url, token=token, timeout=timeout)
    return payload, _utc_now(), {"mode": "curl_system_trust_tls", "tls_verification": "verified"}


def _compact(value: dict) -> dict:
    return {
        "schema": value.get("schema"),
        "ok": value.get("ok"),
        "state": value.get("state"),
        "block_reasons": value.get("block_reasons") or [],
        "query": value.get("query"),
        "query_id": value.get("query_id"),
        "response_sha256": value.get("response_sha256"),
        "returned_raw_count": value.get("returned_raw_count"),
        "outside_query_count": value.get("outside_query_count"),
        "malformed_report_count": value.get("malformed_report_count"),
        "boundary": value.get("boundary"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True, help="Asia/Shanghai physical date, YYYY-MM-DD")
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)

    query = historical_station_day_query_contract(event_date=args.event_date)
    if query.get("ok") is not True:
        print(json.dumps(_compact(query), ensure_ascii=False, sort_keys=True))
        return 2
    tokens = token_candidates()
    if not tokens:
        print(json.dumps({
            "schema": "amsc_historical_backfill_capture_v1",
            "ok": False,
            "state": "blocked",
            "block_reasons": ["amsc_session_unavailable"],
            "boundary": {"writes_archive_db": False, "writes_posterior": False, "places_orders": False},
        }, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        payload, received_at, transport = fetch_historical_amsc_payload(query=query, token=tokens[0], timeout=args.timeout)
        prepared = prepare_amsc_historical_backfill(
            payload=payload,
            query=query,
            query_received_at=received_at,
            query_transport=transport,
        )
    except Exception as exc:
        print(json.dumps({
            "schema": "amsc_historical_backfill_capture_v1",
            "ok": False,
            "state": "blocked",
            "block_reasons": [f"amsc_historical_fetch_failed:{exc.__class__.__name__}"],
            "boundary": {"writes_archive_db": False, "writes_posterior": False, "places_orders": False},
        }, ensure_ascii=False, sort_keys=True))
        return 1
    if not args.apply:
        print(json.dumps(_compact(prepared), ensure_ascii=False, sort_keys=True))
        return 0 if prepared.get("ok") is True else 1
    if args.confirm != APPLY_CONFIRMATION:
        print(json.dumps({
            "schema": "amsc_historical_backfill_capture_v1",
            "ok": False,
            "state": "blocked",
            "block_reasons": ["amsc_historical_backfill_apply_confirmation_missing_or_invalid"],
            "boundary": prepared.get("boundary"),
        }, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        result = persist_amsc_historical_backfill(archive_dir=args.archive_dir, prepared=prepared)
    except Exception as exc:
        print(json.dumps({
            "schema": "amsc_historical_backfill_capture_v1",
            "ok": False,
            "state": "blocked",
            "block_reasons": [f"amsc_historical_backfill_persist_failed:{exc.__class__.__name__}"],
            "boundary": prepared.get("boundary"),
        }, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
