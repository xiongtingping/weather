#!/usr/bin/env python3
"""Mint the live trading contract, once the operator has armed the intent.

What actually stands between this book and a live order is one stored CHECK
and three contract files.  Everything else people call a blocker is either
downstream of those or is not ours: the preflight
(``scripts/go_live_migration_preflight.py``) enumerates all of it.

The stored CHECK is on ``basket_target_order_intents``:
``live_submit_authorized INTEGER NOT NULL CHECK(live_submit_authorized=0)``.
Every one of the 1406 intents the book has prepared carries a 0 there, not
because the objective declined but because the column cannot hold anything
else.  SQLite cannot alter a CHECK in place, so the table is rebuilt -- and
that rebuild is the reason this step is a migration rather than a config edit.

``basket_p0_preselection_plan`` carries the same column and is deliberately
NOT touched.  Its five no-live constraints (authority scope, PRELIMINARY_NO_LIVE
state, and three zeroed execution counters) describe what a forward-day
preselection *is* -- ``compile_forward_preselection`` states it plainly: this
path "never creates an intent, reservation, order, or gateway call", and its
final authority is P1.  Widening it would loosen a contract that has nothing
to do with going live, on 42k rows, for no gain.

The three contracts are minted rather than edited, alongside the no-live ones
rather than over them.  Both lanes must keep working: the no-live activation
is what production runs today and what every replay is scored against, and a
migration that reached into ``validate_target_activation`` to relax it would
take the measurement lane down with it.

Refuses unless ``live_enabled`` is true in config/trading/live_intent_v1.json.
That is the arming flag's stated purpose and it is the operator's alone --
this script never sets it, and it places no orders of its own.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402

if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
from live_control_server import live_stations as _live_scope  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
TABLE = "basket_target_order_intents"
LEGACY = f"{TABLE}_pre_live"
SERVICE_LABEL = f"gui/{__import__('os').getuid()}/com.weather.weather_basket_yes"

INTENT_PATH = ROOT / "config/trading/live_intent_v1.json"
EXCLUSIONS_PATH = ROOT / "config/trading/live_station_exclusions_v1.json"
NO_LIVE_ACTIVATION = (
    ROOT / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
)
LIVE_ACTIVATION = (
    ROOT / "config/trading/bayes_basket_target_production_live_activation_v1.json"
)
NO_LIVE_RUNTIME = ROOT / "config/bayes_basket_target/no_live_runtime_v1.json"
LIVE_RUNTIME = ROOT / "config/bayes_basket_target/live_runtime_v1.json"
CAPITAL_POLICY = ROOT / "config/trading/portfolio_capital_policy_v1.json"

PINNED_CHECK = "CHECK(live_submit_authorized=0)"
WIDENED_CHECK = "CHECK(live_submit_authorized IN (0,1))"

LIVE_ACTIVATION_SCHEMA = "bayes_basket_target_production_live_activation_v1"
LIVE_RUNTIME_SCHEMA = "bayes_basket_target_live_runtime_v1"
# Not a new scope: the live contract keeps the one the artifacts carry.
LIVE_AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_CANDIDATE"


# --------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _service_running() -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", SERVICE_LABEL],
        text=True, capture_output=True, check=False,
    )
    return result.returncode == 0 and "state = running" in result.stdout


def preconditions(*, require_quiet: bool) -> list[str]:
    """Everything that must hold before a single byte is written.

    Returned rather than raised so ``--check`` can print the whole list; a
    caller that finds one failure and stops teaches the operator one blocker
    per run, which is how a migration takes six attempts.
    """

    failures: list[str] = []
    intent = _json(INTENT_PATH)
    if intent.get("live_enabled") is not True:
        failures.append(
            "live_intent_v1.live_enabled is false -- the operator arms this,"
            " not the migration"
        )
    # The live batch is derived, not enumerated -- read it the way the system
    # reads it rather than off the stored field, which is empty by design in
    # all_except_barred mode.  Disjointness then holds by construction, and is
    # still asserted: a precondition that can only pass is worth nothing, but
    # one that cannot fail silently is worth keeping.
    scope = _live_scope()
    allowlist = scope["stations"]
    if not allowlist:
        failures.append(
            f"the live batch resolves to no stations (scope={scope['scope']});"
            " a live open needs something to trade"
        )
    overlap = sorted(set(allowlist) & set(scope["barred"]))
    if overlap:
        failures.append(f"live batch carries barred stations: {','.join(overlap)}")
    if not NO_LIVE_ACTIVATION.is_file():
        failures.append(f"missing {NO_LIVE_ACTIVATION.name} to mint the live one from")
    if not DB.is_file():
        failures.append(f"missing {DB}")
    if require_quiet and _service_running():
        failures.append(
            "the resident writer is running; stop it for the rebuild"
            f" (launchctl bootout {SERVICE_LABEL})"
        )
    return failures


# --------------------------------------------------------------------------
# The rebuild
# --------------------------------------------------------------------------


def _table_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return str(row[0]) if row else ""


def _rebuild(conn: sqlite3.Connection) -> dict[str, Any]:
    """Widen the one CHECK, carrying every row and every attachment across.

    Three stages rather than one transaction, because ``executescript`` and
    bare DDL commit implicitly: the original survives under ``_pre_live``
    until the copy has been counted, so an interrupted run is recoverable by
    renaming it back rather than by having lost the table.
    """

    current = _table_sql(conn, TABLE)
    if not current:
        raise ContractError("go_live_migration_table_missing")
    if WIDENED_CHECK in current:
        return {"state": "already_widened", "rows": None}
    if PINNED_CHECK not in current:
        raise ContractError("go_live_migration_check_not_recognised")

    before = int(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
    columns = [
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({TABLE})")
    ]
    column_list = ",".join(columns)
    triggers = [
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
            " AND tbl_name=? AND sql IS NOT NULL", (TABLE,),
        )
    ]
    indexes = [
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index'"
            " AND tbl_name=? AND sql IS NOT NULL", (TABLE,),
        )
    ]
    # The new DDL is the old one with exactly one substring replaced.  Building
    # it from the stored SQL rather than from a checked-in template is what
    # keeps a column somebody added since (target_execution_hard_deadline_at
    # arrived this way) from being silently dropped by the rebuild.
    widened = current.replace(PINNED_CHECK, WIDENED_CHECK, 1)
    if widened == current or WIDENED_CHECK not in widened:
        raise ContractError("go_live_migration_ddl_rewrite_failed")

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    for name, _sql in triggers:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name, _sql in indexes:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute(f"ALTER TABLE {TABLE} RENAME TO {LEGACY}")
    conn.execute("COMMIT")

    conn.execute(widened)
    for _name, sql in indexes:
        conn.execute(sql)
    for _name, sql in triggers:
        conn.execute(sql)

    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        f"INSERT INTO {TABLE} ({column_list})"
        f" SELECT {column_list} FROM {LEGACY}"
    )
    after = int(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
    if after != before:
        raise ContractError("go_live_migration_row_count_changed")
    still_pinned = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE live_submit_authorized<>0"
        ).fetchone()[0]
    )
    if still_pinned:
        # Nothing recorded before the migration may claim live authority; a
        # row that does would mean the copy invented one.
        raise ContractError("go_live_migration_history_gained_authority")
    conn.execute(f"DROP TABLE {LEGACY}")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise ContractError("go_live_migration_foreign_key_broken")
    conn.execute("COMMIT")
    conn.execute("PRAGMA foreign_keys=ON")

    final = _table_sql(conn, TABLE)
    if WIDENED_CHECK not in final:
        raise ContractError("go_live_migration_check_not_widened")
    kept = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name=?"
            " AND type IN ('index','trigger') AND sql IS NOT NULL", (TABLE,),
        )
    }
    # Parenthesised deliberately: set difference binds tighter than union in
    # Python, so the unbracketed form reads as triggers | (indexes - kept) and
    # reports every trigger as lost no matter what survived.  The rehearsal
    # caught this, which is the whole reason it exists.
    missing = sorted(
        ({name for name, _ in triggers} | {name for name, _ in indexes}) - kept
    )
    if missing:
        raise ContractError(
            "go_live_migration_attachments_lost:" + ",".join(missing)
        )
    return {"state": "widened", "rows": after}


# --------------------------------------------------------------------------
# The contracts
# --------------------------------------------------------------------------


def mint_live_activation(*, dry_run: bool) -> dict[str, Any]:
    """Derive the live contract from the no-live one it stands in for.

    Derived rather than authored so the two differ only where going live
    requires: the required-input list, the scope keys and the freshness policy
    are carried across unchanged, and a reader diffing them sees exactly the
    boundaries that moved.
    """

    base = _json(NO_LIVE_ACTIVATION)
    row = {k: v for k, v in base.items() if k != "activation_contract_sha256"}
    row["schema"] = LIVE_ACTIVATION_SCHEMA
    row["state"] = "production_live"
    row["execution_mode"] = "production_live"
    # policy_authority_scope is carried across UNCHANGED, and the name is
    # why this needs saying: it reads like an execution authority and is not
    # one.  It selects which station-policy artifacts the lane may resolve,
    # and every stored artifact carries PRODUCTION_NO_LIVE_CANDIDATE.  Moving
    # it on go-live day would re-resolve every station's policy -- or resolve
    # none -- at the exact moment nothing else should be changing.  Execution
    # authority lives in the seven booleans below; which policy is
    # authoritative is a separate decision that has to be made separately.
    # The boundaries that move, and only these.  network_access and
    # calls_order_gateway open because an order has to reach the exchange;
    # writes_production_db stays false because the target lane still owns its
    # own database and borrowing the resident's was never part of going live.
    for key in (
        "authorizes_order", "live_decision_write_authorized",
        "live_order_authorized", "live_submit_authorized",
        "places_orders", "calls_order_gateway", "network_access",
    ):
        row[key] = True
    row["production_mutation"] = False
    row["writes_production_db"] = False
    row["derived_from"] = {
        "schema": base.get("schema"),
        "activation_contract_sha256": base.get("activation_contract_sha256"),
    }
    row["minted_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    payload = {**row, "activation_contract_sha256": content_sha256(row)}
    if not dry_run:
        LIVE_ACTIVATION.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return {
        "path": str(LIVE_ACTIVATION),
        "sha256": payload["activation_contract_sha256"],
        "written": not dry_run,
    }


def mint_live_runtime(*, dry_run: bool) -> dict[str, Any]:
    base = _json(NO_LIVE_RUNTIME)
    payload = {
        "schema": LIVE_RUNTIME_SCHEMA,
        "state": "ENABLED_FOR_PRODUCTION_LIVE",
        "execution_mode": "production_live",
        "target_only": True,
        "capital_scope": "target_owned_only",
        # Carried, not reset: the operator has been sizing this number on the
        # control panel and a migration that quietly restored a default would
        # open live at a budget nobody chose.
        "target_remaining_budget_usdc": base.get("target_remaining_budget_usdc"),
        "live_submit_authorized": True,
        "places_orders": True,
        "calls_order_gateway": True,
        "writes_production_db": False,
        "reads_shared_live_capital": False,
        "derived_from": base.get("schema"),
        "minted_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if not dry_run:
        LIVE_RUNTIME.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return {"path": str(LIVE_RUNTIME), "written": not dry_run,
            "budget_usdc": payload["target_remaining_budget_usdc"]}


def raise_capital_policy_scope(*, dry_run: bool) -> dict[str, Any]:
    policy = _json(CAPITAL_POLICY)
    before = policy.get("authority_scope")
    if before == LIVE_AUTHORITY_SCOPE:
        return {"path": str(CAPITAL_POLICY), "state": "already_live"}
    policy["authority_scope"] = LIVE_AUTHORITY_SCOPE
    for key in ("calls_order_gateway", "places_orders", "live_submit_authorized"):
        policy[key] = True
    policy["scope_raised_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    if not dry_run:
        CAPITAL_POLICY.write_text(
            json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return {"path": str(CAPITAL_POLICY), "before": before,
            "after": LIVE_AUTHORITY_SCOPE, "written": not dry_run}


# --------------------------------------------------------------------------
# Rehearsal
# --------------------------------------------------------------------------


def rehearse() -> dict[str, Any]:
    """Run the real rebuild against a copy of only the table it touches.

    Copying the whole SSOT to find out would be gigabytes; the table is 1406
    rows, so the copy is seconds and exactly as conclusive.  Touches nothing
    outside its temporary file, so it runs with the writer up and needs no
    arming flag -- which is what makes it the thing to run first.
    """

    with tempfile.TemporaryDirectory(prefix="go-live-rehearsal-") as work:
        copy = Path(work) / "rehearsal.db"
        src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30.0)
        try:
            src.execute("PRAGMA busy_timeout=60000")
            ddl = _table_sql(src, TABLE)
            attachments = [
                str(row[0]) for row in src.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name=?"
                    " AND type IN ('index','trigger') AND sql IS NOT NULL", (TABLE,),
                )
            ]
            columns = [str(r[1]) for r in src.execute(f"PRAGMA table_info({TABLE})")]
            rows = src.execute(f"SELECT {','.join(columns)} FROM {TABLE}").fetchall()
        finally:
            src.close()
        dst = sqlite3.connect(copy, timeout=30.0, isolation_level=None)
        try:
            dst.execute(ddl)
            for sql in attachments:
                dst.execute(sql)
            dst.executemany(
                f"INSERT INTO {TABLE} ({','.join(columns)})"
                f" VALUES({','.join('?' * len(columns))})",
                rows,
            )
            outcome = _rebuild(dst)
            # The point of the rehearsal: prove the widened table accepts what
            # the pinned one refused, then prove nothing else changed.
            dst.execute("BEGIN IMMEDIATE")
            probe = dict(zip(columns, rows[0])) if rows else None
            accepted = None
            if probe is not None:
                probe["order_intent_id"] = "rehearsal-probe"
                probe["idempotency_key"] = "rehearsal-probe-key"
                probe["live_submit_authorized"] = 1
                dst.execute(
                    f"INSERT INTO {TABLE} ({','.join(columns)})"
                    f" VALUES({','.join('?' * len(columns))})",
                    [probe[c] for c in columns],
                )
                accepted = True
            dst.execute("ROLLBACK")
            integrity = str(
                dst.execute("PRAGMA integrity_check").fetchone()[0]
            )
        finally:
            dst.close()
    return {
        "rows_copied": len(rows),
        "rebuild": outcome,
        "live_flag_accepted_after": accepted,
        "integrity": integrity,
    }


# --------------------------------------------------------------------------


def apply(*, backup: bool = True) -> dict[str, Any]:
    failures = preconditions(require_quiet=True)
    if failures:
        raise ContractError("go_live_migration_preconditions:" + "; ".join(failures))
    receipt: dict[str, Any] = {
        "schema": "go_live_migration_receipt_v1",
        "started_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if backup:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = DB.with_name(f"{DB.name}.pre_go_live_{stamp}")
        shutil.copy2(DB, target)
        receipt["backup"] = str(target)
    conn = sqlite3.connect(DB, timeout=120.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=120000")
        receipt["rebuild"] = _rebuild(conn)
    finally:
        conn.close()
    receipt["live_activation"] = mint_live_activation(dry_run=False)
    receipt["live_runtime"] = mint_live_runtime(dry_run=False)
    receipt["capital_policy"] = raise_capital_policy_scope(dry_run=False)
    receipt["finished_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    receipt["remaining_code_edits"] = [
        "bayes_basket_target/activation.py: load the live schema (additive)",
        "bayes_basket_target/runtime_config.py: live loader (additive)",
        "PRODUCTION_EXECUTION_MODE in production_worker.py and production.py",
        "scripts/clob_order_service.sh: its gate is inverted and refuses live",
    ]
    out = ROOT / "data" / "go_live_migration_receipt.json"
    out.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return receipt


def rollback(*, backup_path: str) -> dict[str, Any]:
    """Put the pinned CHECK back by restoring the pre-migration file.

    Restoring the backup rather than re-narrowing the CHECK in place: a
    re-narrow would fail the moment one live-authorised intent exists, which
    is exactly when somebody would be reaching for this.
    """

    source = Path(backup_path).expanduser().resolve(strict=True)
    if _service_running():
        raise ContractError("go_live_rollback_writer_running")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    aside = DB.with_name(f"{DB.name}.rolled_back_{stamp}")
    shutil.move(str(DB), str(aside))
    shutil.copy2(source, DB)
    for path in (LIVE_ACTIVATION, LIVE_RUNTIME):
        if path.is_file():
            path.unlink()
    return {"restored_from": str(source), "previous_db_kept_at": str(aside)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", default="check",
        choices=("check", "rehearse", "apply", "rollback"),
        help="check prints the preconditions and the plan; rehearse runs the"
             " real rebuild on a throwaway copy; apply is the migration;"
             " rollback restores a pre-migration backup",
    )
    parser.add_argument("--backup", help="rollback: the .pre_go_live_ file")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.mode == "rollback":
        if not args.backup:
            print("rollback needs --backup", file=sys.stderr)
            return 2
        print(json.dumps(rollback(backup_path=args.backup), ensure_ascii=False, indent=2))
        return 0

    if args.mode == "rehearse":
        result = rehearse()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.mode == "apply":
        print(json.dumps(apply(), ensure_ascii=False, indent=2))
        return 0

    failures = preconditions(require_quiet=False)
    plan = {
        "preconditions_failed": failures,
        "would_rebuild": {
            "table": TABLE,
            "from": PINNED_CHECK,
            "to": WIDENED_CHECK,
        },
        "would_mint": [
            mint_live_activation(dry_run=True),
            mint_live_runtime(dry_run=True),
            raise_capital_policy_scope(dry_run=True),
        ],
        "not_touched": {
            "basket_p0_preselection_plan":
                "preliminary by design; never creates an intent or order",
        },
    }
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print("go-live 迁移 — 计划\n")
    if failures:
        print("前置条件未满足，apply 会拒绝：")
        for item in failures:
            print("  ✗", item)
    else:
        print("前置条件全部满足。")
    print(f"\n重建：{TABLE}")
    print(f"  {PINNED_CHECK}  →  {WIDENED_CHECK}")
    print("\n铸造：")
    for row in plan["would_mint"]:
        print("  ", row.get("path"))
    print("\n不动：basket_p0_preselection_plan（前瞻预选按设计从不建 intent 或下单）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
