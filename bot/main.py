from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_env_file(path: Path, *, override_credentials: bool = False) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    credential_keys = {
        "POLY_API_KEY",
        "POLY_API_SECRET",
        "POLY_API_PASSPHRASE",
        "POLY_PRIVATE_KEY",
        "PRIVATE_KEY",
    }
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and value[0] not in {"'", '"'} and " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not value:
            continue
        if override_credentials and key in credential_keys:
            os.environ[key] = value
        elif not os.environ.get(key):
            os.environ[key] = value


def _hydrate_runtime_env() -> None:
    _load_env_file(ROOT / ".env")
    shared_raw = os.environ.get("PM_SHARED_POLY_ENV_PATH") or str(Path.home() / "poly" / ".env")
    _load_env_file(Path(shared_raw).expanduser(), override_credentials=True)
    if not os.environ.get("PRIVATE_KEY") and os.environ.get("POLY_PRIVATE_KEY"):
        os.environ["PRIVATE_KEY"] = str(os.environ.get("POLY_PRIVATE_KEY") or "")
    if not os.environ.get("PM_FUNDER_ADDRESS") and os.environ.get("POLY_FUNDER_ADDRESS"):
        os.environ["PM_FUNDER_ADDRESS"] = str(os.environ.get("POLY_FUNDER_ADDRESS") or "")


def main() -> None:
    """Run the existing weather auto-trade worker through the bot CLI.

    This module is intentionally thin: scan_weather_market remains the SSOT for
    candidate generation, decision writing, and dry-run order behavior.
    """

    _hydrate_runtime_env()
    import scan_weather_market

    os.environ.setdefault("PM_AUTO_TRADE_DRY_RUN", "1")
    argv = sys.argv[1:]
    if "--once" in argv and "--auto-trade-job-worker" not in argv:
        argv = ["--auto-trade-job-worker", *argv]
    old_argv = sys.argv
    try:
        sys.argv = ["scan_weather_market.py", *argv]
        scan_weather_market.main()
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    main()
