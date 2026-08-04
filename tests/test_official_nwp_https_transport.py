import json
import ssl

import certifi
import pytest
from urllib.error import URLError
from urllib.request import Request

import official_nwp_https_transport as transport
from official_nwp_https_transport import official_https_context


def test_official_transport_uses_certifi_without_disabling_tls_checks():
    context = official_https_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert certifi.where()


def test_official_proxy_is_explicit_and_does_not_use_ambient_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.delenv("PM_OFFICIAL_NWP_PROXY", raising=False)
    monkeypatch.setattr(transport, "OFFICIAL_NWP_NETWORK_CONFIG", transport.Path("/missing/config.json"))
    assert transport.official_nwp_proxy_url() == ""
    assert transport.official_nwp_proxy_handler().proxies == {}


def test_official_proxy_handler_uses_explicit_proxy(monkeypatch):
    monkeypatch.setenv("PM_OFFICIAL_NWP_PROXY", "http://127.0.0.1:7890")
    handler = transport.official_nwp_proxy_handler()
    assert handler.proxies == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


def test_open_official_https_keeps_verified_context_with_proxy(monkeypatch):
    monkeypatch.setenv("PM_OFFICIAL_NWP_PROXY", "http://127.0.0.1:7890")
    captured = {}

    class _Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return object()

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return _Opener()

    monkeypatch.setattr(transport, "build_opener", fake_build_opener)
    result = transport.open_official_https(Request("https://example.com"), timeout_sec=4)
    assert result is not None
    assert captured["handlers"][0].proxies["https"] == "http://127.0.0.1:7890"
    context = captured["handlers"][1]._context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_refused_explicit_proxy_retries_once_with_verified_direct_tls(monkeypatch):
    monkeypatch.setenv("PM_OFFICIAL_NWP_PROXY", "http://127.0.0.1:7890")
    opened = []

    class _Opener:
        def __init__(self, handlers):
            self.handlers = handlers

        def open(self, request, timeout):
            opened.append((self.handlers, request, timeout))
            if len(opened) == 1:
                raise URLError(ConnectionRefusedError(61, "Connection refused"))
            return object()

    monkeypatch.setattr(
        transport,
        "build_opener",
        lambda *handlers: _Opener(handlers),
    )

    assert transport.open_official_https(
        Request("https://example.com"), timeout_sec=4
    ) is not None
    assert opened[0][0][0].proxies["https"] == "http://127.0.0.1:7890"
    assert opened[1][0][0].proxies == {}
    assert opened[1][0][1]._context.verify_mode == ssl.CERT_REQUIRED


def test_non_refused_proxy_failure_does_not_fallback(monkeypatch):
    monkeypatch.setenv("PM_OFFICIAL_NWP_PROXY", "http://127.0.0.1:7890")
    opens = 0

    class _Opener:
        def open(self, request, timeout):
            nonlocal opens
            opens += 1
            raise URLError(TimeoutError("timed out"))

    monkeypatch.setattr(transport, "build_opener", lambda *handlers: _Opener())
    with pytest.raises(URLError):
        transport.open_official_https(
            Request("https://example.com"), timeout_sec=4
        )
    assert opens == 1


def _write_network_config(path, *, enabled=True, proxy_url="http://127.0.0.1:1082"):
    path.write_text(
        json.dumps(
            {
                "schema": transport.OFFICIAL_NWP_NETWORK_SCHEMA,
                "enabled": enabled,
                "proxy_url": proxy_url,
            }
        ),
        encoding="utf-8",
    )


def test_network_config_supplies_proxy_when_env_is_absent(monkeypatch, tmp_path):
    config = tmp_path / "network.json"
    _write_network_config(config)
    monkeypatch.delenv("PM_OFFICIAL_NWP_PROXY", raising=False)
    monkeypatch.setattr(transport, "OFFICIAL_NWP_NETWORK_CONFIG", config)
    assert transport.official_nwp_proxy_url() == "http://127.0.0.1:1082"


def test_explicit_env_overrides_network_config(monkeypatch, tmp_path):
    config = tmp_path / "network.json"
    _write_network_config(config)
    monkeypatch.setattr(transport, "OFFICIAL_NWP_NETWORK_CONFIG", config)
    monkeypatch.setenv("PM_OFFICIAL_NWP_PROXY", "http://127.0.0.1:7891")
    assert transport.official_nwp_proxy_url() == "http://127.0.0.1:7891"


def test_disabled_network_config_deliberately_selects_direct_transport(monkeypatch, tmp_path):
    config = tmp_path / "network.json"
    _write_network_config(config, enabled=False, proxy_url="not-a-url")
    monkeypatch.delenv("PM_OFFICIAL_NWP_PROXY", raising=False)
    monkeypatch.setattr(transport, "OFFICIAL_NWP_NETWORK_CONFIG", config)
    assert transport.official_nwp_proxy_url() == ""


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"schema": "wrong", "enabled": True, "proxy_url": "http://127.0.0.1:1082"}, "schema_invalid"),
        (
            {
                "schema": transport.OFFICIAL_NWP_NETWORK_SCHEMA,
                "enabled": "yes",
                "proxy_url": "http://127.0.0.1:1082",
            },
            "enabled_boolean_required",
        ),
        (
            {
                "schema": transport.OFFICIAL_NWP_NETWORK_SCHEMA,
                "enabled": True,
                "proxy_url": "socks5://127.0.0.1:1082",
            },
            "must be an http",
        ),
    ],
)
def test_bad_network_config_fails_closed(monkeypatch, tmp_path, payload, reason):
    config = tmp_path / "network.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.delenv("PM_OFFICIAL_NWP_PROXY", raising=False)
    monkeypatch.setattr(transport, "OFFICIAL_NWP_NETWORK_CONFIG", config)
    with pytest.raises(ValueError, match=reason):
        transport.official_nwp_proxy_url()


def test_oversized_network_config_fails_closed(monkeypatch, tmp_path):
    config = tmp_path / "network.json"
    config.write_bytes(b"x" * (transport.MAX_OFFICIAL_NWP_NETWORK_CONFIG_BYTES + 1))
    monkeypatch.delenv("PM_OFFICIAL_NWP_PROXY", raising=False)
    monkeypatch.setattr(transport, "OFFICIAL_NWP_NETWORK_CONFIG", config)
    with pytest.raises(ValueError, match="too_large"):
        transport.official_nwp_proxy_url()
