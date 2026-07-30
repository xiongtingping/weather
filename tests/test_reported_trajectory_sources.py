from __future__ import annotations

import json
import subprocess
import urllib.error

import pytest

import reported_trajectory_sources as sources


class _Response:
    def __init__(self, payload: dict, *, status: int = 200) -> None:
        self._body = json.dumps(payload).encode()
        self.headers = {"Content-Length": str(len(self._body))}
        self.status = status
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body) - self._offset
        result = self._body[self._offset:self._offset + size]
        self._offset += len(result)
        return result


def test_awc_uses_system_verified_curl_when_python_ca_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingOpener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.URLError("certificate verify failed")

    monkeypatch.setattr(
        sources.urllib.request,
        "build_opener",
        lambda *_args, **_kwargs: FailingOpener(),
    )
    urls: list[str] = []

    def curl_get(url, **_kwargs):
        urls.append(url)
        return b"METAR RKPK 261800Z 00000KT CAVOK 27/22 Q1008=\n"

    monkeypatch.setattr(sources, "_curl_official_get", curl_get)

    result = sources.fetch_awc_temperature_reports(
        ["RKPK"],
        hours=6,
        timeout=2,
    )

    assert result["transport"] == (
        "official_awc_raw_metar_batch_curl_system_ca"
    )
    assert result["reports"][0]["station_id"] == "RKPK"
    assert "hours=6" in urls[0]


def test_official_curl_transport_keeps_tls_verification_and_size_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(command, **_kwargs):
        captured.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(sources.subprocess, "run", fake_run)

    assert sources._curl_official_get(
        "https://example.invalid/object",
        timeout=2,
        max_response_bytes=1024,
        accept="text/plain",
    ) == b"ok"
    assert "-k" not in captured
    assert "--insecure" not in captured
    assert captured[captured.index("--proto") + 1] == "=https"
    assert captured[captured.index("--max-filesize") + 1] == "1024"


def test_awc_curl_fallback_bypasses_only_aviationweather_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(command, **_kwargs):
        captured.extend(command)
        return subprocess.CompletedProcess(
            command, 0, stdout=b"ok", stderr=b""
        )

    monkeypatch.setattr(sources.subprocess, "run", fake_run)

    sources._curl_official_get(
        "https://aviationweather.gov/api/data/metar",
        timeout=2,
        max_response_bytes=1024,
        accept="text/plain",
        bypass_proxy_host="aviationweather.gov",
    )

    assert captured[captured.index("--noproxy") + 1] == (
        "aviationweather.gov"
    )


def test_official_curl_transport_rejects_oversized_captured_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sources.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=b"x" * 5,
            stderr=b"",
        ),
    )
    with pytest.raises(RuntimeError, match="response_too_large"):
        sources._curl_official_get(
            "https://example.invalid/object",
            timeout=2,
            max_response_bytes=4,
            accept="text/plain",
        )


def test_amsc_curl_fallback_pins_intermediate_without_disabling_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sources.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError("incomplete chain")
        ),
    )
    captured: list[str] = []

    def fake_run(command, **_kwargs):
        captured.extend(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"code": 200, "data": []}),
            stderr="",
        )

    monkeypatch.setattr(sources.subprocess, "run", fake_run)
    result = sources._amsc_one("ZUUU", "redacted-token", count=2, timeout=2)

    assert result["status"] == 200
    assert captured[captured.index("--cacert") + 1] == str(
        sources.AMSC_INTERMEDIATE_CA
    )
    assert captured[captured.index("--proto") + 1] == "=https"
    assert "--tlsv1.2" in captured
    assert "-k" not in captured
    assert "--insecure" not in captured


def test_amsc_pinned_intermediate_integrity_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    tampered = tmp_path / "issuer.pem"
    tampered.write_text("tampered", encoding="utf-8")
    monkeypatch.setattr(sources, "AMSC_INTERMEDIATE_CA", tampered)
    with pytest.raises(
        RuntimeError, match="amsc_intermediate_ca_integrity_failed"
    ):
        sources._amsc_intermediate_ca_path()


def test_amsc_application_session_expiry_has_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sources.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response({
            "errCode": -12005,
            "errMsg": "sensitive upstream text must not escape",
        }),
    )
    with pytest.raises(RuntimeError, match=r"^amsc_session_expired$"):
        sources._amsc_one("ZUUU", "secret-token", count=2, timeout=2)


def test_amsc_expired_token_rescans_once_and_uses_only_new_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scans: list[bool] = []

    def candidates(*, force_refresh: bool = False):
        scans.append(force_refresh)
        return ["old-token", "new-token"] if force_refresh else ["old-token"]

    attempts: list[str] = []

    def fetch(_station, token, **_kwargs):
        attempts.append(token)
        if token == "old-token":
            raise RuntimeError("amsc_session_expired")
        return {
            "status": 200,
            "response_started_at": "2026-07-30T00:00:01Z",
            "reports": [],
        }

    monkeypatch.setattr(sources, "amsc_token_candidates", candidates)
    monkeypatch.setattr(sources, "_amsc_one", fetch)
    result = sources.fetch_amsc_temperature_reports(["ZUUU"])

    assert result["status"] == 200
    assert scans == [False, True]
    assert attempts == ["old-token", "new-token"]


def test_amsc_all_expired_candidates_stop_bounded_without_token_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scans: list[bool] = []
    attempts: list[str] = []

    def candidates(*, force_refresh: bool = False):
        scans.append(force_refresh)
        return ["secret-old", "secret-new"] if force_refresh else ["secret-old"]

    def fetch(_station, token, **_kwargs):
        attempts.append(token)
        raise RuntimeError("amsc_session_expired")

    monkeypatch.setattr(sources, "amsc_token_candidates", candidates)
    monkeypatch.setattr(sources, "_amsc_one", fetch)
    with pytest.raises(RuntimeError) as captured:
        sources.fetch_amsc_temperature_reports(["ZUUU"])

    assert str(captured.value) == "amsc_session_expired"
    assert scans == [False, True]
    assert attempts == ["secret-old", "secret-new"]
    assert "secret-" not in str(captured.value)
