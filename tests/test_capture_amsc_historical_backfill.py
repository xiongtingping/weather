from __future__ import annotations

import json
import ssl
import subprocess
import urllib.error
from urllib.parse import parse_qs, urlparse

from amsc_historical_backfill_archive import historical_station_day_query_contract
from scripts import capture_amsc_historical_backfill as capture


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_historical_fetch_uses_physical_day_utc_window_and_keeps_token_in_request_only(monkeypatch):
    query = historical_station_day_query_contract(event_date="2025-07-05")
    seen: dict[str, str] = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["session_id"] = dict((key.lower(), value) for key, value in request.header_items()).get("sessionid")
        seen["timeout"] = str(timeout)
        return _Response({"code": 200, "data": []})

    monkeypatch.setattr(capture.urllib.request, "urlopen", fake_urlopen)
    payload, received_at, transport = capture.fetch_historical_amsc_payload(
        query=query,
        token="ephemeral-token",
        timeout=17,
    )

    params = parse_qs(urlparse(seen["url"]).query)
    assert payload == {"code": 200, "data": []}
    assert received_at.endswith("Z")
    assert transport == {"mode": "urllib_default_tls", "tls_verification": "verified"}
    assert params["starDate"] == ["2025-07-04 16:00:00"]
    assert params["endDate"] == ["2025-07-05 15:59:59"]
    assert params["tt"] == ["SA,SP,"]
    assert params["token"] == ["ephemeral-token"]
    assert seen["session_id"] == "ephemeral-token"
    assert seen["timeout"] == "17"


def test_historical_fetch_uses_system_trust_curl_only_for_python_certificate_failure(monkeypatch):
    query = historical_station_day_query_contract(event_date="2025-07-05")
    seen: dict[str, object] = {}

    def certificate_error(*_args, **_kwargs):
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate_verify_failed"))

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["config"] = kwargs["input"]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"code": 200, "data": []}), stderr="")

    monkeypatch.setattr(capture.urllib.request, "urlopen", certificate_error)
    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    payload, _received_at, transport = capture.fetch_historical_amsc_payload(
        query=query,
        token="ephemeral-token",
        timeout=17,
    )

    assert payload == {"code": 200, "data": []}
    assert transport == {"mode": "curl_system_trust_tls", "tls_verification": "verified"}
    assert seen["command"] == ["/usr/bin/curl", "--config", "-"]
    config = str(seen["config"])
    assert "ephemeral-token" in config
    assert "--insecure" not in config
    assert "insecure" not in config.lower()
