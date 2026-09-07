"""One definition of "which trajectory artifact would production have served".

Lifted out of the production worker on 2026-09-07, while building the
counterfactual the soft-likelihood question needs.  The worker held two copies
of the same two-step resolution -- promoted, else the candidate the 2026-08-18
ruling admits -- and a measurement that wanted to resolve what production would
have served had no way to do it except a third copy.  Three copies of a
fallback order is how a measurement quietly starts scoring a rule nobody runs.

The fallback order is the whole content of the function, so that is what is
pinned here.
"""
from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target import probability_evidence_store as S


def test_the_local_time_bucket_is_the_artifacts_own_axis() -> None:
    assert [S.local_time_bucket_for_hour(h) for h in (0, 5, 6, 11, 12, 17, 18, 23)] == [
        "NIGHT", "NIGHT", "MORNING", "MORNING",
        "AFTERNOON", "AFTERNOON", "EVENING", "EVENING",
    ]


def test_promoted_wins_and_the_candidate_is_the_fallback(monkeypatch) -> None:
    """The 2026-08-18 ruling: the gate is a label, not the switch on the layer.

    Refusing to resolve a candidate left 301 of 350 P1 station-days with no
    trajectory evidence at all, and P1 without it found the settled bucket
    45.7% of the time against 75.8% with it.  A resolver that dropped the
    fallback would silently reproduce that.
    """

    asked: list[str] = []

    def fake(conn, **kwargs):
        asked.append(str(kwargs["required_authority_scope"]))
        return {"artifact": {"id": "promoted"}} if (
            kwargs["required_authority_scope"] == "FORMAL_PROMOTED"
        ) else {"artifact": {"id": "candidate"}}

    monkeypatch.setattr(S, "resolve_active_probability_evidence_artifact", fake)
    row = S.resolve_metar_trajectory_artifact_row(
        sqlite3.connect(":memory:"), station_id="KLGA", source_lane="awc",
        season_bin="JJA", local_time_bucket="AFTERNOON", maturity_bin="D0",
        settlement_definition="official_daily_tmax_c",
        as_of_utc="2026-09-03T18:00:00Z",
    )
    assert row["artifact"]["id"] == "promoted"
    assert asked == ["FORMAL_PROMOTED"], "the candidate is not even asked for"


def test_the_candidate_serves_when_nothing_is_promoted(monkeypatch) -> None:
    asked: list[str] = []

    def fake(conn, **kwargs):
        asked.append(str(kwargs["required_authority_scope"]))
        return None if (
            kwargs["required_authority_scope"] == "FORMAL_PROMOTED"
        ) else {"artifact": {"id": "candidate"}}

    monkeypatch.setattr(S, "resolve_active_probability_evidence_artifact", fake)
    row = S.resolve_metar_trajectory_artifact_row(
        sqlite3.connect(":memory:"), station_id="KLGA", source_lane="awc",
        season_bin="JJA", local_time_bucket="AFTERNOON", maturity_bin="D0",
        settlement_definition="official_daily_tmax_c",
        as_of_utc="2026-09-03T18:00:00Z",
    )
    assert row["artifact"]["id"] == "candidate"
    assert asked == ["FORMAL_PROMOTED", "PRODUCTION_NO_LIVE_CANDIDATE"]


def test_neither_is_silence_rather_than_a_guess(monkeypatch) -> None:
    monkeypatch.setattr(
        S, "resolve_active_probability_evidence_artifact",
        lambda conn, **kwargs: None,
    )
    assert S.resolve_metar_trajectory_artifact_row(
        sqlite3.connect(":memory:"), station_id="KLGA", source_lane="awc",
        season_bin="JJA", local_time_bucket="AFTERNOON", maturity_bin="D0",
        settlement_definition="official_daily_tmax_c",
        as_of_utc="2026-09-03T18:00:00Z",
    ) is None


def test_the_scope_key_is_the_stores_own_and_not_rebuilt() -> None:
    """A resolver that spelled the key itself would drift from the store."""

    from pathlib import Path  # noqa: PLC0415

    source = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target" / "probability_evidence_store.py"
    ).read_text(encoding="utf-8")
    assert "scope_key = metar_trajectory_scope_key(" in source
    worker = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target" / "production_worker.py"
    ).read_text(encoding="utf-8")
    assert "resolve_metar_trajectory_artifact_row(" in worker
    assert worker.count("required_authority_scope=") == 0, (
        "the worker no longer spells the fallback order itself"
    )
