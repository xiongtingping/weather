from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_THIS_PLACES_REAL_ORDERS"


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


def _prepare_execution_mode(argv: Sequence[str]) -> list[str]:
    """Resolve the interactive CLI execution mode before importing the engine.

    The repository .env may intentionally keep the launchd worker in Live mode.
    An operator invoking ``python -m bot.main`` must never inherit that setting
    implicitly.  Live therefore requires both an explicit flag and an exact
    confirmation phrase; every other invocation force-overrides dry-run.
    """

    args = list(argv)
    live_requested = False
    confirmation = ""
    downstream: list[str] = []
    index = 0
    while index < len(args):
        arg = str(args[index])
        if arg == "--live":
            live_requested = True
            index += 1
            continue
        if arg == "--confirm-live":
            if index + 1 >= len(args):
                raise SystemExit("--confirm-live requires the exact Live confirmation phrase")
            confirmation = str(args[index + 1])
            index += 2
            continue
        if arg.startswith("--confirm-live="):
            confirmation = arg.split("=", 1)[1]
            index += 1
            continue
        downstream.append(arg)
        index += 1

    if live_requested:
        if confirmation != LIVE_CONFIRMATION_PHRASE:
            raise SystemExit(
                "Live mode blocked: use --live --confirm-live "
                f"{LIVE_CONFIRMATION_PHRASE}"
            )
        os.environ["PM_AUTO_TRADE_DRY_RUN"] = "0"
        os.environ["PM_BOT_CLI_LIVE_CONFIRMED"] = "1"
    else:
        if confirmation:
            raise SystemExit("--confirm-live is only valid together with --live")
        os.environ["PM_AUTO_TRADE_DRY_RUN"] = "1"
        os.environ["PM_BOT_CLI_LIVE_CONFIRMED"] = "0"
    return downstream


def main() -> None:
    """Run the existing weather auto-trade worker through the bot CLI.

    This module is intentionally thin: scan_weather_market remains the SSOT for
    candidate generation, decision writing, and dry-run order behavior.
    """

    _hydrate_runtime_env()
    argv = _prepare_execution_mode(sys.argv[1:])
    import scan_weather_market

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
