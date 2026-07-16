from pathlib import Path

import scan_weather_market as swm
from scripts import signal_to_order_safety_contract_audit as audit


def test_signal_to_order_safety_contract_passes():
    report = audit.build_audit(root=Path.cwd())

    assert report["ok"] is True
    assert report["read_only"] is True
    assert all(report["checks"].values())
    assert report["timing_contract"]["terminal_state"] == "stale"
    assert report["timing_contract"]["order_submitted_at"] == ""
    assert report["timing_contract"]["signal_to_order_slo_met"] is False
    assert report["checks"]["orderbook_missing_blocks"] is True
    assert report["checks"]["risk_cap_blocks"] is True
    assert report["checks"]["orderbook_refresh_segments_recorded"] is True
    assert report["checks"]["risk_guard_segments_recorded"] is True
    assert report["checks"]["pre_submit_refresh_and_cap_static_contract_present"] is True
    assert report["execution_segment_contract"]["orderbook_refreshed_at"] == "2026-07-03T09:05:38.250Z"
    assert report["execution_segment_contract"]["orderbook_checked_at"] == "2026-07-03T09:05:38.300Z"
    assert report["execution_segment_contract"]["risk_checked_at"] == "2026-07-03T09:05:38.900Z"
    assert report["execution_segment_contract"]["orderbook_to_risk_check_ms"] == 600.0
    assert report["guards"]["places_orders"] is False
    assert report["guards"]["fetches_orderbook"] is False


def test_signal_to_order_safety_contract_blocks_stale_classifier_regression(monkeypatch):
    original = swm._classify_signal_to_order_terminal_state

    def fake_classifier(reason_code, reason_text="", *, will_order=False):
        text = f"{reason_code} {reason_text}"
        if "posterior_expired" in text:
            return "blocked"
        return original(reason_code, reason_text, will_order=will_order)

    monkeypatch.setattr(audit.swm, "_classify_signal_to_order_terminal_state", fake_classifier)

    report = audit.build_audit(root=Path.cwd())

    assert report["ok"] is False
    assert "stale_posterior_does_not_classify_as_stale" in report["blockers"]
    assert report["checks"]["stale_posterior_blocks"] is False


def test_signal_to_order_safety_contract_blocks_duplicate_alias_regression(monkeypatch):
    monkeypatch.setattr(
        audit.swm,
        "ENTRY_HARD_REASON_ALIASES",
        {
            **dict(swm.ENTRY_HARD_REASON_ALIASES),
            "entry_submit_dedup_blocked": "not_duplicate",
        },
    )

    report = audit.build_audit(root=Path.cwd())

    assert report["ok"] is False
    assert "entry_submit_dedup_blocked_not_mapped_to_duplicate_open_order_hard_reason" in report["blockers"]
    assert report["checks"]["duplicate_order_blocks"] is False


def test_signal_to_order_safety_contract_blocks_missing_static_idempotency_contract(tmp_path: Path):
    root = tmp_path
    (root / "scripts").mkdir()
    (root / "scan_weather_market.py").write_text(
        """
def _classify_signal_to_order_terminal_state(reason_code, reason_text='', *, will_order=False):
    stale_markers = ('posterior_expired', 'decision_expired', 'orderbook_expired')
    blocked_markers = ('duplicate', 'open_order', 'same_outcome_open_order')
def _apply_signal_to_order_terminal():
    pass
def _clob_order_service_idempotency_key(payload):
    audit_keys = {'execution_domain'}
def _clob_order_service_direct_target_idempotency_key(payload):
    pass
def _run_node_order():
    order_cap_meta = {}
def _classify_mirror_order_failure():
    pass
def _posterior_target_primary_entry_fast_path():
    _run_node_order()
    def _is_effectively_binary_extreme():
        pass
def _apply_signal_to_order_execution_segments():
    orderbook_checked_at = ''
def _classify_signal_to_order_terminal_state(reason_code, reason_text='', *, will_order=False):
    pass
""",
        encoding="utf-8",
    )
    (root / "scripts" / "clob_order_service.mjs").write_text(
        "function stableKey(payload) { return payload; }\n",
        encoding="utf-8",
    )

    report = audit.build_audit(root=root)

    assert report["ok"] is False
    assert "static_contract_python_idempotency_key_excludes_audit_metadata_missing" in report["blockers"]
    assert "static_contract_clob_service_has_idempotent_replay_missing" in report["blockers"]
    assert "static_contract_clob_service_has_inflight_guard_missing" in report["blockers"]


def test_signal_to_order_safety_contract_blocks_orderbook_alias_regression(monkeypatch):
    monkeypatch.setattr(
        audit.swm,
        "ENTRY_HARD_REASON_ALIASES",
        {
            **dict(swm.ENTRY_HARD_REASON_ALIASES),
            "entry_orderbook_missing": "not_orderbook",
        },
    )

    report = audit.build_audit(root=Path.cwd())

    assert report["ok"] is False
    assert "entry_orderbook_missing_not_mapped_to_orderbook_missing_hard_reason" in report["blockers"]
    assert report["checks"]["orderbook_missing_blocks"] is False


def test_signal_to_order_safety_contract_blocks_cap_alias_regression(monkeypatch):
    monkeypatch.setattr(
        audit.swm,
        "ENTRY_HARD_REASON_ALIASES",
        {
            **dict(swm.ENTRY_HARD_REASON_ALIASES),
            "adjacent_bucket_exposure_cap": "not_bucket_cap",
        },
    )

    report = audit.build_audit(root=Path.cwd())

    assert report["ok"] is False
    assert "adjacent_bucket_exposure_cap_not_mapped_to_bucket_exposure_cap_hard_reason" in report["blockers"]
    assert report["checks"]["risk_cap_blocks"] is False


def test_signal_to_order_safety_contract_blocks_missing_pre_submit_static_contract(tmp_path: Path):
    root = tmp_path
    (root / "scripts").mkdir()
    (root / "scan_weather_market.py").write_text(
        """
def _clob_order_service_idempotency_key(payload):
    audit_keys = {
        'execution_domain',
        'order_execution_domain',
        'order_execution_source',
        'order_execution_context_version',
        'order_execution_reason',
        'order_intent_action',
        'manual_confirm_source',
    }
def _clob_order_service_direct_target_idempotency_key(payload):
    pass
def _run_node_order():
    order_cap_meta = {}
def _classify_mirror_order_failure():
    pass
def _posterior_target_primary_entry_fast_path():
    _run_node_order()
    def inner():
        pass
    def _is_effectively_binary_extreme():
        pass
def _apply_signal_to_order_execution_segments():
    orderbook_checked_at = ''
def _classify_signal_to_order_terminal_state(reason_code, reason_text='', *, will_order=False):
    stale_markers = ('posterior_expired', 'decision_expired', 'orderbook_expired')
    blocked_markers = ('duplicate', 'open_order', 'same_outcome_open_order')
def _apply_signal_to_order_terminal():
    pass
""",
        encoding="utf-8",
    )
    (root / "scripts" / "clob_order_service.mjs").write_text(
        "const a='idempotent_replay'; const b='idempotent_inflight'; app.post('/order-status', () => {});\n",
        encoding="utf-8",
    )

    report = audit.build_audit(root=root)

    assert report["ok"] is False
    assert "static_contract_execution_segments_preserve_orderbook_refresh_missing" in report["blockers"]
    assert "static_contract_primary_fast_path_checks_orderbook_before_submit_missing" in report["blockers"]
    assert "static_contract_primary_fast_path_runs_risk_guards_before_submit_missing" in report["blockers"]
    assert "static_contract_primary_fast_path_persists_decision_before_submit_missing" in report["blockers"]
    assert "static_contract_run_node_order_applies_buy_notional_cap_missing" in report["blockers"]
    assert report["checks"]["pre_submit_refresh_and_cap_static_contract_present"] is False
