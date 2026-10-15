"""No SQL statement may scan a big table row by row (user ruling 2026-10-15).

「关于索引、关于读库，我记得这个问题已经修复很多次了吧？为什么同样的问题一直
犯？？？如何长期解决？」  Each slow render and slow cycle since August was one
statement that read every row of a large table because no index could seek
it, found after it hurt and fixed alone -- and the next one written the same
way passed every test.  This test plans every SELECT in the production
package and the board's modules against the live schema snapshot
(``tests/fixtures/ssot_schema_snapshot.json``, refreshed by
``scripts/dump_ssot_schema_snapshot.py``) and fails on a SCAN of a table of
50k rows or more that no covering index answers.

``KNOWN_DEBT`` is what was there on 2026-10-15, keyed by statement text so a
line move does not reopen it; each entry is a scan to fix, never a template.
A fixed statement drops out of the scan list and must leave this list too.
"""

from __future__ import annotations

from tests import sql_plan_guard as guard

KNOWN_DEBT = {
    # Operator tools, not the resident: release materialisation, the expected
    # scope manifest and the cold-offload audit.
    "bayes_basket_target/canonical_activation_release.py:8e0316a05a83":
        "canonical_ssot_fact 209k; load_db_audit_snapshot, release tool only",
    "bayes_basket_target/canonical_typed_scope_builder.py:534dd71d6dc4":
        "basket_official_ws_snapshot 2.2M; _market_records, manifest/offload tools only",
    # Latent: the receipt ingress (production_io.apply_no_live_execution_
    # receipt) has no caller, and nothing writes post-fill recompute requests
    # (table empty 2026-10-15).  If either path wakes, each lookup by
    # content_sha256 is 35-214 s: index content_sha256 or delete the chain.
    "bayes_basket_target/execution_transition_runtime.py:2478edeee359":
        "basket_official_ws_snapshot 2.2M by content_sha256 (214 s); latent, no caller",
    "bayes_basket_target/execution_transition_runtime.py:c11bdf78d1eb":
        "basket_confirmed_inventory_snapshot 2.2M by content_sha256 (48 s); latent, no caller",
    "bayes_basket_target/execution_transition_runtime.py:44fa649cc1e1":
        "basket_order_state_snapshot 2.2M by content_sha256 (35 s); latent, no caller",
    "bayes_basket_target/production_worker.py:0f6649a8d11e":
        "basket_confirmed_inventory_snapshot 2.2M by content_sha256; post-fill requests table empty",
    "bayes_basket_target/production_worker.py:3ec06cd33c73":
        "basket_order_state_snapshot 2.2M by content_sha256; post-fill requests table empty",
    # Cheap in fact: narrow rows, 0.24 s for the whole table (will_order is
    # always 0 in production_no_live).
    "bayes_basket_target/order_state_producer.py:f1c23011cc5b":
        "auto_trade_decisions 191k; OR-scoped quiescence check, 0.24 s measured",
    # Called only through apply_native_ensemble_foundation_schema, which no
    # production path calls (tests do).
    "bayes_basket_target/ssot.py:18d1b916c863":
        "calibration_native_ensemble_activation 903k; run-cycle mapping migration, tests only",
}


def test_no_new_statement_scans_a_big_table() -> None:
    conn, sizes = guard.snapshot_connection()
    found = guard.full_scans(conn, sizes)
    new = [f for f in found if f["key"] not in KNOWN_DEBT]
    assert not new, "\n".join(
        f'{f["path"]}:{f["line"]} scans {f["table"]} ({f["rows"]:,} rows): '
        f'{f["detail"]} -- give it a predicate an index can seek, key {f["key"]}'
        for f in new
    )


def test_known_debt_lists_only_scans_still_there() -> None:
    conn, sizes = guard.snapshot_connection()
    still = {f["key"] for f in guard.full_scans(conn, sizes)}
    assert not set(KNOWN_DEBT) - still, sorted(set(KNOWN_DEBT) - still)


def test_the_guard_sees_a_scan_and_a_seek() -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (station_id TEXT NOT NULL, event_date TEXT, payload TEXT)")
    conn.execute("CREATE INDEX t_idx ON t(station_id, event_date)")
    sizes = {"t": guard.BIG_TABLE_ROWS}
    plan = lambda sql: " ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql))  # noqa: E731
    assert "SCAN t" in plan("SELECT payload FROM t WHERE event_date>='x'")
    assert "SEARCH t USING INDEX t_idx" in plan(
        "SELECT payload FROM t WHERE station_id IN (SELECT 'A') AND event_date>='x'"
    )
    assert sizes["t"] >= guard.BIG_TABLE_ROWS


def test_the_snapshot_is_the_live_shape() -> None:
    conn, sizes = guard.snapshot_connection()
    assert len(sizes) > 150
    assert sizes["calibration_native_ensemble_historical_member_run"] > 1_000_000
