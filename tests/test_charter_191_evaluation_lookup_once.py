"""Charter 191: one compile asks each OOS evaluation question once.

The generations of one cross pairing share one evaluation key (station,
season, lead, fitted cycle map, members, set hash, instant), and the compile's
activation walk asked it once per generation: 22,249 lookups for the 132
packets of one replay pass, 20,246 of them repeats of a question already
answered at the same instant (2026-09-21).  The answer to a repeat is the one
the first ask got.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from bayes_basket_target import native_ensemble_p0 as p0
from bayes_basket_target.native_ensemble_p0 import compile_native_ensemble_p0
from tests.test_native_ensemble_p0 import (
    _install_mixed_cycle_calibrations,
    _mixed_packet,
)

DECISION_AT_UTC = "2026-07-01T13:00:00Z"
LOOKUP = "oos_state,selection_evidence_sha256,evaluated_at_utc"


@pytest.fixture(autouse=True)
def _fixture_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    from bayes_basket_target import native_ensemble_oos
    from bayes_basket_target import station_season

    monkeypatch.setattr(native_ensemble_oos, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setattr(p0, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setitem(station_season._SEASON_ANCHOR_MONTH, "TEST", 7)


def _regenerate(conn: sqlite3.Connection, copies: int) -> None:
    """Re-activate the fixture's cross pairing ``copies`` more times."""

    columns = [row[1] for row in conn.execute(
        "PRAGMA table_info(calibration_native_ensemble_activation)"
    )]
    (row,) = conn.execute(
        "SELECT * FROM calibration_native_ensemble_activation "
        "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE'"
    ).fetchall()
    base = dict(zip(columns, row))
    for k in range(1, copies + 1):
        copy = dict(base)
        copy["activation_id"] = f"{base['activation_id']}-g{k}"
        copy["generation"] = int(base["generation"]) + k
        copy["content_sha256"] = hashlib.sha256(
            json.dumps([base["content_sha256"], k]).encode()
        ).hexdigest()
        conn.execute(
            "INSERT INTO calibration_native_ensemble_activation("
            + ",".join(columns) + ") VALUES(" + ",".join("?" * len(columns)) + ")",
            [copy[c] for c in columns],
        )


def _compile(copies: int) -> tuple[dict, list[str]]:
    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    _regenerate(conn, copies)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    compiled = compile_native_ensemble_p0(
        conn, forward_packet=_mixed_packet(), decision_at_utc=DECISION_AT_UTC,
    )
    conn.set_trace_callback(None)
    return compiled, [s for s in statements if LOOKUP in s]


def test_many_generations_of_one_pairing_ask_once() -> None:
    compiled, lookups = _compile(copies=12)
    # One in the activation walk for the shared key, one in the selected
    # activation's own re-check (a separate gate, left as it was).
    assert len(lookups) == 2
    assert len(set(lookups)) == 1
    # The newest generation is the one served, as before.
    assert compiled["activation_id"].endswith("-g12")


def test_the_served_packet_does_not_depend_on_the_repeats() -> None:
    one, _ = _compile(copies=0)
    many, _ = _compile(copies=12)
    envelope_one = one["packet"]["native_probability_envelope"]
    envelope_many = many["packet"]["native_probability_envelope"]
    assert envelope_one["probabilities"] == envelope_many["probabilities"]
