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
    "bayes_basket_target/canonical_activation_release.py:8e0316a05a83": "canonical_ssot_fact (208k rows), canonical_activation_release.py:939",
    "bayes_basket_target/canonical_typed_scope_builder.py:534dd71d6dc4": "basket_official_ws_snapshot (2208k rows), canonical_typed_scope_builder.py:909",
    "bayes_basket_target/execution_transition_runtime.py:2478edeee359": "basket_official_ws_snapshot (2208k rows), execution_transition_runtime.py:752",
    "bayes_basket_target/execution_transition_runtime.py:630aeb1f925a": "basket_official_ws_snapshot (2208k rows), execution_transition_runtime.py:757",
    "bayes_basket_target/execution_transition_runtime.py:c11bdf78d1eb": "basket_confirmed_inventory_snapshot (2154k rows), execution_transition_runtime.py:784",
    "bayes_basket_target/execution_transition_runtime.py:44fa649cc1e1": "basket_order_state_snapshot (2154k rows), execution_transition_runtime.py:788",
    "bayes_basket_target/execution_transition_runtime.py:cdd10a2ed977": "basket_confirmed_inventory_snapshot (2154k rows), execution_transition_runtime.py:793",
    "bayes_basket_target/execution_transition_runtime.py:429aab8a313d": "basket_order_state_snapshot (2154k rows), execution_transition_runtime.py:794",
    "bayes_basket_target/order_state_producer.py:f1c23011cc5b": "auto_trade_decisions (190k rows), order_state_producer.py:318",
    "bayes_basket_target/pair_maker_lane.py:a5512f297f95": "basket_target_market_registry (8474k rows), pair_maker_lane.py:299",
    "bayes_basket_target/pr11_taf_p0m_basis.py:dc041eb5827e": "canonical_ssot_fact (208k rows), pr11_taf_p0m_basis.py:377",
    "bayes_basket_target/pr12_awc_observation.py:6cd2e66f15fe": "basket_target_observation_facts (50k rows), pr12_awc_observation.py:194",
    "bayes_basket_target/pr14_strategy_basis.py:4c8fad5b02d7": "auto_trade_decisions (190k rows), pr14_strategy_basis.py:54",
    "bayes_basket_target/pr14_strategy_basis.py:dc041eb5827e": "canonical_ssot_fact (208k rows), pr14_strategy_basis.py:108",
    "bayes_basket_target/production_worker.py:f125cae4a4f4": "calibration_native_ensemble_system_artifact (161k rows), production_worker.py:2467",
    "bayes_basket_target/production_worker.py:0f6649a8d11e": "basket_confirmed_inventory_snapshot (2154k rows), production_worker.py:14283",
    "bayes_basket_target/production_worker.py:3ec06cd33c73": "basket_order_state_snapshot (2154k rows), production_worker.py:14288",
    "bayes_basket_target/realisation_store.py:e9d280f8aaef": "basket_policy_training_fact (126k rows), realisation_store.py:255",
    "bayes_basket_target/ssot.py:18d1b916c863": "calibration_native_ensemble_activation (902k rows), ssot.py:4395",
    "bayes_basket_target/tiebreak_cell_shadow.py:8c9a6b1ed268": "basket_policy_training_fact (126k rows), tiebreak_cell_shadow.py:414",
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
