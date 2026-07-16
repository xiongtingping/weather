from ui_snapshot_presenter import (
    UI_SNAPSHOT_SECTION_ORDER,
    summarize_ui_snapshot_presenter,
    ui_snapshot_presenter_contract,
)


def test_ui_snapshot_presenter_contract_declares_single_snapshot_boundaries():
    contract = ui_snapshot_presenter_contract()

    assert contract["schema"] == "ui_snapshot_presenter_contract_v1"
    assert contract["owner"] == "ui/server.py /api/ui_snapshot"
    assert contract["required_sections"] == list(UI_SNAPSHOT_SECTION_ORDER)
    assert contract["boundaries"]["frontend_recompute_allowed"] is False
    assert contract["boundaries"]["writes_archive_db"] is False


def test_ui_snapshot_presenter_summary_checks_required_sections():
    snapshot = {
        "snapshot_id": "uisnap_test",
        "snapshot_freshness_state": "fresh",
        "ui_sections": {key: {} for key in UI_SNAPSHOT_SECTION_ORDER},
    }

    assert summarize_ui_snapshot_presenter(snapshot)["ok"] is True
    incomplete = summarize_ui_snapshot_presenter({"ui_sections": {"facts": {}}})
    assert incomplete["ok"] is False
    assert "posterior" in incomplete["missing_sections"]
