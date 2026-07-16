from __future__ import annotations

import os

import pytest

from bot.main import LIVE_CONFIRMATION_PHRASE, _prepare_execution_mode


def test_cli_forces_dry_run_over_live_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "0")

    downstream = _prepare_execution_mode(["--once"])

    assert downstream == ["--once"]
    assert os.environ["PM_AUTO_TRADE_DRY_RUN"] == "1"
    assert os.environ["PM_BOT_CLI_LIVE_CONFIRMED"] == "0"


def test_cli_live_requires_exact_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")

    with pytest.raises(SystemExit, match="Live mode blocked"):
        _prepare_execution_mode(["--live"])
    with pytest.raises(SystemExit, match="Live mode blocked"):
        _prepare_execution_mode(["--live", "--confirm-live", "yes"])

    assert os.environ["PM_AUTO_TRADE_DRY_RUN"] == "1"


def test_cli_explicit_live_is_removed_from_downstream_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")

    downstream = _prepare_execution_mode(
        ["--once", "--live", "--confirm-live", LIVE_CONFIRMATION_PHRASE]
    )

    assert downstream == ["--once"]
    assert os.environ["PM_AUTO_TRADE_DRY_RUN"] == "0"
    assert os.environ["PM_BOT_CLI_LIVE_CONFIRMED"] == "1"


def test_cli_rejects_confirmation_without_live() -> None:
    with pytest.raises(SystemExit, match="only valid together"):
        _prepare_execution_mode(["--confirm-live=" + LIVE_CONFIRMATION_PHRASE])
