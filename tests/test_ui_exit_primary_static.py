from pathlib import Path


UI_INDEX = Path(__file__).resolve().parents[1] / "ui" / "index.html"


def _resolve_exit_primary_edge_block() -> str:
    html = UI_INDEX.read_text(encoding="utf-8")
    start = html.index("function resolveExitPrimaryEdge(obj)")
    end = html.index("function resolveSignalEdgeFromRow", start)
    return html[start:end]


def test_exit_primary_edge_does_not_fall_back_to_exec_edge_exit() -> None:
    block = _resolve_exit_primary_edge_block()

    assert "obj.hold_edge_realizable" in block
    assert "return toFiniteNumberOrNaN(obj.exec_edge_exit)" not in block
    assert "return Number.NaN" in block


def test_exit_primary_source_only_treats_hold_edge_as_main_source() -> None:
    block = _resolve_exit_primary_edge_block()

    assert "source === 'hold_edge_realizable'" in block
    assert "? 'hold_edge_realizable' : ''" in block
    assert "? 'hold_edge_realizable' : 'exec_edge_exit'" not in block


def test_exec_edge_exit_is_labeled_as_diagnostic_in_advantage_panel() -> None:
    html = UI_INDEX.read_text(encoding="utf-8")

    assert "exec_exit诊断" in html
    assert "exitPrimaryLabel = 'hold_exit'" in html
    assert "exitPrimaryLabel = exitPrimarySource === 'hold_edge_realizable' ? 'hold_exit' : 'exec_exit'" not in html


def test_exit_reason_family_helpers_are_available_in_both_ui_blocks() -> None:
    html = UI_INDEX.read_text(encoding="utf-8")

    assert html.count("function buildExitReasonFamilyParts(tradeInfo)") >= 2
    assert "function parseExitReasonTokens(raw)" in html


def test_resolve_trade_info_passes_exit_reason_family_fields() -> None:
    html = UI_INDEX.read_text(encoding="utf-8")

    assert html.count("function resolveTradeInfo(item, side, outcome)") == 2
    assert html.count("exit_reason_family_codes: String(trade && trade.exit_reason_family_codes || '')") == 2
    assert html.count("exit_reason_primary_family: String(trade && trade.exit_reason_primary_family || '')") == 2


def test_exit_reason_renderers_prepend_exit_reason_family() -> None:
    html = UI_INDEX.read_text(encoding="utf-8")

    assert "for (const reason of buildExitReasonFamilyParts(tradeInfo))" in html
    assert "reasons.push(...buildExitReasonFamilyParts(tradeInfo));" in html


def test_ui_server_does_not_synthesize_target_exit_from_exec_edge_exit() -> None:
    server_path = UI_INDEX.parent / "server.py"
    source = server_path.read_text(encoding="utf-8")

    assert "def _build_edge_realizable_target_exit_price_ui" not in source
    assert "exec_exit_price=exec_exit_price" not in source
    assert "exec_edge_exit=exec_edge_exit" not in source
    assert "fallback_exit = _coalesce_numeric(entry.get(\"exec_exit_price\")" not in source
    assert "edge_realizable_target_exit_price\"] = fallback_exit" not in source
    assert "v2_meta.get(\"target_exit_price\")" not in source


def test_tradeability_snapshot_control_is_not_rendered_after_strategy_config_panel_removal() -> None:
    html = UI_INDEX.read_text(encoding="utf-8")

    assert 'id="entryRequireTradeabilitySnapshot" data-config-key="entry_require_tradeability_snapshot"' not in html
    assert "entryRequireEdgeRealizable" not in html
