from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import bayes_basket_target.pr16_runtime_basis as runtime_basis
from bayes_basket_target.pr16_runtime_basis import (
    PR16RuntimeBasisError,
    SERVICES,
    build_pr16_runtime_artifacts,
)


AS_OF = "2026-08-05T12:00:00Z"


def _absent(*_args: object, **_kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(returncode=113, stdout="", stderr="not found")


def test_missing_current_pointer_emits_unique_runtime_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_basis.subprocess, "run", _absent)
    expected, terminals, receipt = build_pr16_runtime_artifacts(
        current_pointer_path=tmp_path / "missing-current.json",
        as_of_utc=AS_OF,
    )
    assert expected["expected_keys"] == []
    assert receipt["blockers"] == ["CURRENT_POINTER_MISSING"]
    assert receipt["current_pointer_state"] == "MISSING"
    assert receipt["current_pointer_content_sha256"] is None
    assert receipt["starts_services"] is False
    assert [row["service_label"] for row in terminals["terminal_scopes"]] == list(
        SERVICES
    )
    assert {
        tuple(row["blockers"]) for row in terminals["terminal_scopes"]
    } == {("CURRENT_POINTER_MISSING",)}
    assert all(row["service_present"] is False for row in terminals["terminal_scopes"])


def test_present_service_still_fails_closed_when_pointer_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime_basis.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="loaded", stderr=""),
    )
    with pytest.raises(PR16RuntimeBasisError, match="service_must_remain_absent"):
        build_pr16_runtime_artifacts(
            current_pointer_path=tmp_path / "missing-current.json",
            as_of_utc=AS_OF,
        )
