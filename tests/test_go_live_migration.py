"""The migration has to be provable before it is ever allowed to run.

It runs once, on the day money starts moving, and by then there is no time to
discover that the rebuild drops a trigger or that the live contract it minted
would not validate.  So everything here exercises the real code paths against
throwaway databases and temporary files: the rebuild is the shipped
``_rebuild``, the contracts are the shipped minters checked by the shipped
validators, and nothing touches the SSOT or the arming flag.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "scripts"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import go_live_migration as mig  # noqa: E402
from bayes_basket_target import activation, runtime_config  # noqa: E402
from bayes_basket_target.contracts import ContractError  # noqa: E402


PINNED_DDL = """
CREATE TABLE basket_target_order_intents (
  order_intent_id TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('YES','NO')),
  live_submit_authorized INTEGER NOT NULL CHECK(live_submit_authorized=0),
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
)
"""
INDEX_DDL = (
    "CREATE INDEX basket_target_order_intent_decision_idx"
    " ON basket_target_order_intents(decision_id,created_at)"
)
TRIGGER_DDL = (
    "CREATE TRIGGER basket_target_intents_no_no_side_insert"
    " BEFORE INSERT ON basket_target_order_intents"
    " WHEN upper(NEW.side) <> 'YES'"
    " BEGIN SELECT RAISE(ABORT,'no_order_side_retired'); END"
)


def _pinned_db(path: Path, rows: int = 25) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(PINNED_DDL)
    conn.execute(INDEX_DDL)
    conn.execute(TRIGGER_DDL)
    conn.executemany(
        "INSERT INTO basket_target_order_intents VALUES(?,?,?,?,?,?)",
        [(f"i{n}", f"d{n}", "YES", 0, f"k{n}", "2026-08-30T00:00:00Z")
         for n in range(rows)],
    )
    return conn


# --------------------------------------------------------------------------
# The rebuild
# --------------------------------------------------------------------------


def test_the_rebuild_carries_every_row_index_and_trigger(tmp_path) -> None:
    conn = _pinned_db(tmp_path / "a.db")
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents"
        ).fetchone()[0]
        outcome = mig._rebuild(conn)
        assert outcome["state"] == "widened"
        assert outcome["rows"] == before
        attachments = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE tbl_name='basket_target_order_intents'"
                " AND type IN ('index','trigger') AND sql IS NOT NULL"
            )
        }
        assert "basket_target_order_intent_decision_idx" in attachments
        assert "basket_target_intents_no_no_side_insert" in attachments
        # The trigger has to still bite, not merely exist.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO basket_target_order_intents"
                " VALUES('x','d','NO',0,'kx','2026-08-30T00:00:00Z')"
            )
    finally:
        conn.close()


def test_the_widened_table_accepts_what_the_pinned_one_refused(tmp_path) -> None:
    """The single behaviour the whole migration exists to produce."""

    conn = _pinned_db(tmp_path / "b.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO basket_target_order_intents"
                " VALUES('p','d','YES',1,'kp','2026-08-30T00:00:00Z')"
            )
        mig._rebuild(conn)
        conn.execute(
            "INSERT INTO basket_target_order_intents"
            " VALUES('p','d','YES',1,'kp','2026-08-30T00:00:00Z')"
        )
        assert conn.execute(
            "SELECT live_submit_authorized FROM basket_target_order_intents"
            " WHERE order_intent_id='p'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_history_may_not_gain_authority_in_the_copy(tmp_path) -> None:
    """Nothing recorded under the no-live contract may come out claiming live.

    Asserted by construction rather than by inspection: the rebuild counts
    the rows whose flag is non-zero after the copy and refuses if any is.
    """

    conn = _pinned_db(tmp_path / "c.db")
    try:
        mig._rebuild(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents"
            " WHERE live_submit_authorized<>0"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_a_second_run_is_a_no_op(tmp_path) -> None:
    conn = _pinned_db(tmp_path / "d.db")
    try:
        mig._rebuild(conn)
        again = mig._rebuild(conn)
        assert again["state"] == "already_widened"
    finally:
        conn.close()


def test_an_unrecognised_check_is_refused_rather_than_rewritten(tmp_path) -> None:
    """A table that does not carry the constraint we mean is not ours to edit."""

    conn = sqlite3.connect(tmp_path / "e.db", isolation_level=None)
    try:
        conn.execute(
            "CREATE TABLE basket_target_order_intents ("
            " order_intent_id TEXT PRIMARY KEY,"
            " live_submit_authorized INTEGER NOT NULL CHECK(live_submit_authorized=7))"
        )
        with pytest.raises(ContractError, match="check_not_recognised"):
            mig._rebuild(conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------


def test_it_refuses_while_the_operator_has_not_armed_it() -> None:
    """The arming flag is the operator's and the migration reads it, only."""

    failures = mig.preconditions(require_quiet=False)
    intent = json.loads(mig.INTENT_PATH.read_text(encoding="utf-8"))
    if intent.get("live_enabled") is True:
        pytest.skip("live intent is armed; this asserts the disarmed refusal")
    assert any("live_enabled is false" in item for item in failures)
    with pytest.raises(ContractError, match="preconditions"):
        mig.apply()


def test_a_barred_station_in_the_live_batch_stops_the_migration(monkeypatch) -> None:
    """Disjointness holds by construction now, and is still checked.

    In all_except_barred mode the batch cannot contain a barred station --
    which is a reason to keep the assertion rather than drop it, because a
    precondition that only ever passes is the one nobody notices breaking
    when somebody pins an explicit list.
    """

    barred = json.loads(mig.EXCLUSIONS_PATH.read_text(encoding="utf-8"))
    first = barred["excluded_stations"][0]["station"]
    monkeypatch.setattr(
        mig, "_live_scope",
        lambda: {"scope": "explicit", "stations": [first, "ZBAA"],
                 "barred": [first]},
    )
    failures = mig.preconditions(require_quiet=False)
    assert any(first in item and "barred" in item for item in failures)


def test_an_empty_live_batch_stops_the_migration(monkeypatch) -> None:
    """A derived batch that resolves to nothing must not read as ready."""

    monkeypatch.setattr(
        mig, "_live_scope",
        lambda: {"scope": "all_except_barred", "stations": [], "barred": []},
    )
    failures = mig.preconditions(require_quiet=False)
    assert any("no stations" in item for item in failures)


# --------------------------------------------------------------------------
# The minted contracts
# --------------------------------------------------------------------------


def test_the_minted_activation_validates_under_the_live_validator() -> None:
    """Minting and validating are written apart and must still agree.

    Run as a dry run, so this proves the contract without writing it.
    """

    row = mig.mint_live_activation(dry_run=True)
    assert row["written"] is False
    base = json.loads(mig.NO_LIVE_ACTIVATION.read_text(encoding="utf-8"))
    # Rebuild the payload the way the minter does, then validate it.
    payload = dict(base)
    payload.pop("activation_contract_sha256", None)
    minted = _mint_payload()
    checked = activation.validate_live_activation(minted)
    assert checked["execution_mode"] == "production_live"
    assert checked["live_submit_authorized"] is True
    assert checked["writes_production_db"] is False
    assert checked["activation_contract_sha256"] == row["sha256"]


def _mint_payload() -> dict:
    """The minter's payload without writing it, for the validator to check."""

    import copy
    saved = mig.LIVE_ACTIVATION
    try:
        captured: dict = {}
        original = mig.content_sha256

        def spy(row):
            captured.update(copy.deepcopy(row))
            return original(row)

        mig.content_sha256 = spy
        mig.mint_live_activation(dry_run=True)
    finally:
        mig.content_sha256 = original
        mig.LIVE_ACTIVATION = saved
    return {**captured, "activation_contract_sha256": original(captured)}


def test_a_tampered_live_contract_is_refused() -> None:
    """The seal is the point: flip a boundary by hand and it stops loading."""

    minted = _mint_payload()
    # Validates as minted.
    assert activation.validate_live_activation(minted)
    # Opening a boundary the live contract holds shut is refused on the
    # boundary itself, before the seal is even considered.
    with pytest.raises(ContractError, match="writes_production_db_must_be_false"):
        activation.validate_live_activation({**minted, "writes_production_db": True})
    # Closing one it needs open is refused too -- a half-live contract is not
    # a safer contract, it is an incoherent one.
    with pytest.raises(ContractError, match="calls_order_gateway_must_be_true"):
        activation.validate_live_activation({**minted, "calls_order_gateway": False})
    # And a body edited without resealing fails the hash, which is what makes
    # hand-editing the minted file detectable rather than effective.
    with pytest.raises(ContractError, match="contract_sha256_mismatch"):
        activation.validate_live_activation(
            {**minted, "minted_at_utc": "1999-01-01T00:00:00+00:00"}
        )


def test_the_no_live_contract_is_untouched_by_any_of_this() -> None:
    """The measurement lane must still load and still prove itself no-live."""

    loaded = activation.load_target_activation()
    assert loaded["execution_mode"] == "production_no_live"
    for key in ("live_submit_authorized", "places_orders", "calls_order_gateway"):
        assert loaded[key] is False


def test_the_dispatcher_refuses_a_file_that_names_neither_contract(tmp_path) -> None:
    """Guessing here would check a live contract against no-live rules."""

    path = tmp_path / "unknown.json"
    path.write_text(json.dumps({"schema": "something_else"}), encoding="utf-8")
    with pytest.raises(ContractError, match="activation_schema_unknown"):
        activation.load_activation(path)
    # The real no-live file still routes to the validator that pins it.
    routed = activation.load_activation(activation.DEFAULT_PATH)
    assert routed["execution_mode"] == "production_no_live"


def test_the_live_runtime_loader_refuses_a_zero_budget(tmp_path, monkeypatch) -> None:
    """A live lane at zero budget is dead, not safe, and says so."""

    payload = {
        "schema": runtime_config.LIVE_SCHEMA,
        "state": "ENABLED_FOR_PRODUCTION_LIVE",
        "execution_mode": "production_live",
        "target_only": True,
        "capital_scope": "target_owned_only",
        "target_remaining_budget_usdc": 0.0,
        "live_submit_authorized": True,
        "places_orders": True,
        "calls_order_gateway": True,
        "writes_production_db": False,
        "reads_shared_live_capital": False,
    }
    path = runtime_config.TARGET_CONFIG_ROOT / "live_runtime_test_tmp.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        with pytest.raises(
            runtime_config.TargetRuntimeConfigError, match="budget_must_be_positive"
        ):
            runtime_config.load_target_live_runtime_config(path)
        payload["target_remaining_budget_usdc"] = 250.0
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = runtime_config.load_target_live_runtime_config(path)
        assert loaded["live_submit_authorized"] is True
        assert loaded["target_remaining_budget_usdc"] == 250.0
    finally:
        path.unlink(missing_ok=True)


def test_the_no_live_runtime_loader_still_pins_its_five_boundaries() -> None:
    loaded = runtime_config.load_target_no_live_runtime_config()
    for key in (
        "live_submit_authorized", "places_orders", "writes_production_db",
        "calls_order_gateway", "reads_shared_live_capital",
    ):
        assert loaded[key] is False


def test_the_preselection_table_is_out_of_scope_and_stays_that_way() -> None:
    """It carries the same column and must not be swept up by a future edit.

    The five constraints on basket_p0_preselection_plan describe what a
    forward-day preselection is -- its own compiler says the path never
    creates an intent, reservation, order or gateway call.  Widening them
    would loosen 42k rows of a contract unrelated to trading.
    """

    source = (ROOT / "scripts" / "go_live_migration.py").read_text(encoding="utf-8")
    assert mig.TABLE == "basket_target_order_intents"
    assert "basket_p0_preselection_plan" in source
    assert "NOT touched" in source
    # And the migration must never name it as a table it rebuilds.
    assert "basket_p0_preselection_plan" not in mig.LEGACY
