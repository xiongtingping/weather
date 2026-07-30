"""Bounded proxy-consistency checks for the target research/production boundary.

This test deliberately inspects only a small, explicit set of files.  It does
not walk the repository, load the environment into the process, contact the
proxy, invoke launchctl, or restart any service.
"""

from __future__ import annotations

import json
import plistlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PROXY_URL = "http://127.0.0.1:7897"
EXPECTED_PROXY_HOST_PORT = "127.0.0.1:7897"
EXPECTED_PROVIDER = "Clash Verge"
MAX_ARTIFACT_BYTES = 512 * 1024

ENV_PATH = ROOT / ".env"
RESEARCH_CONFIG_PATH = ROOT / "config" / "trading" / "bayes_basket_target_research_network_v1.json"
LAUNCHD_PATH = ROOT / "launchd" / "com.weather.reported_trajectory_no.plist"
LAUNCHD_ENV_LOADER_PATH = ROOT / "scripts" / "lib" / "load_launchctl_env.sh"
CLOB_ORDER_SERVICE_PATH = ROOT / "scripts" / "clob_order_service.mjs"
SETTLEMENT_FLOOR_EXECUTE_PATH = ROOT / "scripts" / "settlement_floor_execute.py"

PROXY_URL_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "PM_MARKET_WS_PROXY",
    "PM_OFFICIAL_NWP_PROXY",
)
PROXY_PORT_KEYS = ("PM_PROXY_PORTS",)
CORE_ARTIFACT_PATHS = (
    ENV_PATH,
    RESEARCH_CONFIG_PATH,
    LAUNCHD_PATH,
    LAUNCHD_ENV_LOADER_PATH,
    CLOB_ORDER_SERVICE_PATH,
    SETTLEMENT_FLOOR_EXECUTE_PATH,
)
TARGET_CORE_SCRIPT_PATHS = (
    LAUNCHD_ENV_LOADER_PATH,
    CLOB_ORDER_SERVICE_PATH,
    SETTLEMENT_FLOOR_EXECUTE_PATH,
)
FORBIDDEN_RESIDUES = ("1082", "shadowrocket")


def _read_bounded(path: Path, *, limit: int = MAX_ARTIFACT_BYTES) -> str:
    """Read one known artifact, refusing unexpectedly large test inputs."""

    size = path.stat().st_size
    assert size <= limit, f"proxy artifact exceeds bounded test input: {path} ({size} bytes)"
    payload = path.read_bytes()
    assert len(payload) <= limit, f"proxy artifact read exceeded bound: {path}"
    return payload.decode("utf-8")


def _parse_dotenv(text: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values.setdefault(key, []).append(value)
    return values


def test_proxy_artifacts_are_explicit_and_bounded() -> None:
    """The consistency check must have a finite, small read set."""

    assert len(CORE_ARTIFACT_PATHS) == 6
    for path in CORE_ARTIFACT_PATHS:
        assert path.is_file(), f"missing explicit proxy artifact: {path}"
        _read_bounded(path)


def test_dotenv_proxy_values_are_unique_and_unified() -> None:
    values = _parse_dotenv(_read_bounded(ENV_PATH))

    for key in PROXY_URL_KEYS:
        assert values.get(key) == [EXPECTED_PROXY_URL], f"inconsistent .env proxy: {key}"
    for key in PROXY_PORT_KEYS:
        assert values.get(key) == ["7897"], f"inconsistent .env proxy port: {key}"


def test_research_config_uses_clash_verge_and_same_proxy() -> None:
    config = json.loads(_read_bounded(RESEARCH_CONFIG_PATH))

    assert config["provider"] == EXPECTED_PROVIDER
    assert config["proxy_url"] == EXPECTED_PROXY_URL
    assert config["fallback_proxy_url"] == ""
    assert config["proxy_candidates"] == [EXPECTED_PROXY_URL]
    assert config["boundary"]["service_restart_required"] is False
    assert config["boundary"]["order_gateway_called"] is False


def test_core_launchd_proxy_environment_is_unified() -> None:
    plist = plistlib.loads(_read_bounded(LAUNCHD_PATH).encode("utf-8"))
    environment = plist["EnvironmentVariables"]

    for key in PROXY_URL_KEYS[:6]:
        assert environment.get(key) == EXPECTED_PROXY_URL, f"inconsistent launchd proxy: {key}"
    assert environment["PM_TARGET_EXECUTION_MODE"] == "production_no_live"


def test_target_core_scripts_have_no_old_proxy_residue() -> None:
    for path in TARGET_CORE_SCRIPT_PATHS:
        text = _read_bounded(path).lower()
        for residue in FORBIDDEN_RESIDUES:
            assert residue not in text, f"old proxy residue {residue!r} remains in {path}"


def test_target_core_scripts_keep_7897_proxy_candidate() -> None:
    loader = _read_bounded(LAUNCHD_ENV_LOADER_PATH)
    clob = _read_bounded(CLOB_ORDER_SERVICE_PATH)
    settlement_floor = _read_bounded(SETTLEMENT_FLOOR_EXECUTE_PATH)

    assert "PM_PROXY_LOCAL_FALLBACK_PORTS:-7897" in loader
    assert 'env("PM_PROXY_LOCAL_FALLBACK_PORTS", "7897")' in clob
    assert 'os.environ.get("PM_PROXY_LOCAL_FALLBACK_PORTS", "7897")' in settlement_floor
    assert "127.0.0.1:$candidate" in loader
    assert "http://127.0.0.1:${port}" in clob
