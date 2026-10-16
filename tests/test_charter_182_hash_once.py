"""Charter 182: what cannot have changed is not hashed or sorted again.

The Python sampling ledger after charter 181 (2026-09-21 11:30Z) put
``content_sha256`` first: the probability envelope hashed its whole body,
lineage documents and all, on every access, and the loader re-verified every
committed row of the station-day on every load (428-883 loads a cycle).  The
settlement lane sorted each closed station-day's ~880 eligible registry rows
every cycle to keep the newest snapshot's (45 calls, 7.6s).
"""

from __future__ import annotations

import dataclasses
import hashlib
import sqlite3

import pytest

from bayes_basket_target import contracts
from bayes_basket_target import production_io as pio
from bayes_basket_target import settlement_truth as truth
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.market_registry import apply_target_market_registry_schema
from bayes_basket_target.materialization import (
    forget_lineage_documents,
    insert_probability_envelope,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from tests.test_bayes_basket_target_materialization import _envelope


@pytest.fixture(autouse=True)
def _fresh():
    pio.forget_verified_envelopes()
    forget_lineage_documents()
    truth._REGISTRY_SCOPE_MEMO.clear()
    yield
    pio.forget_verified_envelopes()
    forget_lineage_documents()
    truth._REGISTRY_SCOPE_MEMO.clear()


def _count_hashes(monkeypatch) -> list[int]:
    calls = [0]
    real = contracts.content_sha256

    def counting(value):
        calls[0] += 1
        return real(value)

    monkeypatch.setattr(contracts, "content_sha256", counting)
    return calls


# --- the envelope's own hash -------------------------------------------------------

def test_an_envelope_hashes_its_body_once(monkeypatch) -> None:
    envelope = _envelope()
    calls = _count_hashes(monkeypatch)
    first = envelope.content_sha256
    assert first == hashlib.sha256(
        contracts.canonical_json(envelope.to_dict()).encode("utf-8")
    ).hexdigest()
    assert envelope.content_sha256 == first
    assert calls[0] == 1
    changed = dataclasses.replace(envelope, fresh_until_utc="2099-01-01T00:00:00Z")
    assert changed.content_sha256 != first
    assert envelope == dataclasses.replace(envelope)  # eq still reads fields only


# --- the loader ------------------------------------------------------------------------

def _store(path) -> tuple[sqlite3.Connection, object]:
    envelope = _envelope()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(conn)
    insert_probability_envelope(conn, envelope, created_at_utc="2026-07-23T04:01:00Z")
    conn.commit()
    return conn, envelope


def _load(conn, envelope):
    return pio.load_materialized_probability_envelopes(
        conn,
        station_id=envelope.station_id,
        event_date=envelope.event_date,
        evaluation_at_utc="2099-01-01T00:00:00Z",
        required_probability_pathway=None,
    )


def test_a_verified_row_is_not_hashed_again_and_carries_its_hash(tmp_path, monkeypatch) -> None:
    conn, envelope = _store(tmp_path / "ssot.db")
    first = _load(conn, envelope)
    assert [e.content_sha256 for e in first] == [envelope.content_sha256]
    calls = _count_hashes(monkeypatch)
    second = _load(conn, envelope)
    assert second == first
    assert second[0].content_sha256 == envelope.content_sha256
    assert calls[0] == 0


def test_a_row_whose_body_does_not_match_its_hash_still_fails(tmp_path) -> None:
    conn, envelope = _store(tmp_path / "ssot.db")
    for name in ("basket_probability_envelope_no_update",):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute(
        "UPDATE basket_probability_envelope SET content_sha256=? WHERE envelope_id=?",
        ("f" * 64, envelope.checkpoint_id),
    )
    conn.commit()
    with pytest.raises(ContractError, match="content_sha_mismatch"):
        _load(conn, envelope)
    with pytest.raises(ContractError, match="content_sha_mismatch"):
        _load(conn, envelope)


def test_an_in_memory_store_is_not_remembered(monkeypatch) -> None:
    conn, envelope = _store(":memory:")
    _load(conn, envelope)
    assert not pio._VERIFIED_ENVELOPES


# --- the settlement registry scope -----------------------------------------------------

def _registry(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    apply_target_market_registry_schema(conn)
    return conn


def _snapshot(conn, sha: str, refreshed: str, *, zone="Asia/Shanghai", lane="awc") -> None:
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot VALUES(?,?,?,?,?,?,?,?)",
        (sha, "s", "shared_live_registry_export_only", refreshed, "p", 2, refreshed, "c" * 64),
    )
    for outcome in ("28°C", "29°C"):
        conn.execute(
            "INSERT INTO basket_target_market_registry(snapshot_sha256,slug,event_date,outcome,"
            "city_slug,station_id,settlement_timezone,settlement_unit,source_lane,source_eligible,"
            "source_block_reason,resolution_url,rules_sha256,yes_token,no_token,market_active,"
            "market_accepting_orders,registry_json,refreshed_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sha, f"slug-{outcome}", "2026-09-20", outcome, "chengdu", "ZUUU", zone, "C",
             lane, 1, None, None, "r" * 64, "y", "n", 1, 1, "{}", refreshed),
        )
    conn.commit()


def _reference(rows_conn, as_of):
    """The unremembered read, on a copy the memo cannot key (in-memory)."""
    copy = sqlite3.connect(":memory:")
    rows_conn.backup(copy)
    return truth._registry_scope(copy, station_id="ZUUU", event_date="2026-09-20", as_of_utc=as_of)


def _trace(conn):
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    return statements


def test_the_scope_is_remembered_per_fingerprint_and_agrees_with_the_read(tmp_path) -> None:
    conn = _registry(tmp_path / "ssot.db")
    _snapshot(conn, "a" * 64, "2026-09-20T10:00:00Z")
    _snapshot(conn, "b" * 64, "2026-09-20T12:00:00Z")
    ask = lambda as_of: truth._registry_scope(  # noqa: E731
        conn, station_id="ZUUU", event_date="2026-09-20", as_of_utc=as_of,
    )
    assert ask("2026-09-21T00:00:00Z") == _reference(conn, "2026-09-21T00:00:00Z")
    assert ask("2026-09-21T00:00:00Z")[2] == "b" * 64
    statements = _trace(conn)
    assert ask("2026-09-21T06:00:00Z")[2] == "b" * 64
    assert not [s for s in statements if "source_eligible=1" in s]
    conn.set_trace_callback(None)
    # a clock before the newest row: the as-of read, as before
    assert ask("2026-09-20T11:00:00Z") == _reference(conn, "2026-09-20T11:00:00Z")
    assert ask("2026-09-20T11:00:00Z")[2] == "a" * 64
    # a new snapshot moves the fingerprint
    _snapshot(conn, "c" * 64, "2026-09-20T14:00:00Z")
    assert ask("2026-09-21T06:00:00Z")[2] == "c" * 64


def test_a_remembered_refusal_is_raised_again(tmp_path) -> None:
    conn = _registry(tmp_path / "ssot.db")
    _snapshot(conn, "a" * 64, "2026-09-20T10:00:00Z", lane="amsc")
    for _ in range(2):
        with pytest.raises(ContractError, match="formal_source_lane_must_be_awc"):
            truth._registry_scope(
                conn, station_id="ZUUU", event_date="2026-09-20",
                as_of_utc="2026-09-21T00:00:00Z",
            )


def test_the_memo_key_is_one_statement(tmp_path) -> None:
    from bayes_basket_target import market_registry as mr

    conn = _registry(tmp_path / "ssot.db")
    _snapshot(conn, "a" * 64, "2026-09-20T10:00:00Z")
    mr.registry_fingerprint(conn)  # the database name is asked once
    statements = _trace(conn)
    key = mr.registry_fingerprint(conn)
    assert key is not None and key[1][0] == 1
    assert len([s for s in statements if "rowid" in s]) == 1


def test_ingests_step_the_scope_and_every_answer_agrees_with_the_whole_read(tmp_path) -> None:
    """2026-10-16: every ingest re-read every copy of the scope (EGLC 10-14:
    3,322 rows in 302 snapshots, ~0.4 s, 31-48 scopes a pass).  An ingest now
    reads only the new snapshots' rows; retention reads the scope whole."""

    import random

    rng = random.Random(272)
    conn = _registry(tmp_path / "ssot.db")
    stamps = ["2026-09-20T10:00:00Z", "2026-09-20T12:00:00Z", "2026-09-20T12:00:00Z"]
    ask = lambda: truth._registry_scope(  # noqa: E731
        conn, station_id="ZUUU", event_date="2026-09-20", as_of_utc="2026-09-22T00:00:00Z",
    )

    def answer(fn):
        try:
            return ("ok", fn())
        except ContractError as exc:
            return ("refused", str(exc))

    shas: list[str] = []
    for step in range(40):
        if shas and step % 9 == 8:
            oldest = shas.pop(0)
            conn.execute(
                "DELETE FROM basket_target_market_registry WHERE snapshot_sha256=?", (oldest,)
            )
            conn.commit()
        else:
            sha = f"{rng.getrandbits(256):064x}"
            stamp = rng.choice(stamps) if step % 5 else f"2026-09-20T1{step % 10}:30:00Z"
            if step % 7 == 3:  # a snapshot without this scope's rows
                conn.execute(
                    "INSERT INTO basket_target_market_registry_snapshot VALUES(?,?,?,?,?,?,?,?)",
                    (sha, "s", "shared_live_registry_export_only", stamp, "p", 0, stamp, "c" * 64),
                )
                conn.execute(
                    "INSERT INTO basket_target_market_registry(snapshot_sha256,slug,event_date,"
                    "outcome,city_slug,station_id,settlement_timezone,settlement_unit,source_lane,"
                    "source_eligible,source_block_reason,resolution_url,rules_sha256,yes_token,"
                    "no_token,market_active,market_accepting_orders,registry_json,refreshed_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sha, "other", "2026-09-20", "1", "x", "RKSI", "Asia/Seoul", "C", "awc",
                     1, None, None, "r" * 64, "y", "n", 1, 1, "{}", stamp),
                )
                conn.commit()
            else:
                _snapshot(conn, sha, stamp, lane="amsc" if step % 11 == 6 else "awc")
            shas.append(sha)
        statements = _trace(conn)
        got = answer(ask)
        conn.set_trace_callback(None)
        assert got == answer(lambda: _reference(conn, "2026-09-22T00:00:00Z")), step
        whole = [s for s in statements
                 if "WHERE station_id=? AND event_date=? AND source_eligible=1" in s
                 or ("station_id='ZUUU' AND event_date='2026-09-20' AND source_eligible=1" in s
                     and "snapshot_sha256=" not in s)]
        if step % 9 == 8 and step:
            assert whole, step  # retention: the scope is read whole
        elif step and len(shas) > 1:
            assert not whole, (step, whole)
