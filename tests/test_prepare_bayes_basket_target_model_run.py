from __future__ import annotations

import json
from pathlib import Path

from scripts.prepare_bayes_basket_target_model_run import main


def test_cli_binds_registry_station_and_settlement_timezone(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setitem(
        main.__globals__,
        "load_market_registry_snapshot",
        lambda _path: {
            "snapshot_sha256": "a" * 64,
            "rows": [{
                "station_id": "RKPK",
                "event_date": "2026-07-27",
                "settlement_timezone": "Asia/Seoul",
            }],
        },
    )
    output = tmp_path / "contract.json"
    assert main([
        "--target-local-date",
        "2026-07-27",
        "--run-initialization-utc",
        "2026-07-25T00:00:00Z",
        "--market-registry-snapshot",
        str(tmp_path / "registry.json"),
        "--output",
        str(output),
    ]) == 0

    contract = json.loads(output.read_text(encoding="utf-8"))
    assert contract["station_ids"] == ["RKPK"]
    assert contract["target_timezone"] == "Asia/Seoul"
    assert contract["market_registry_snapshot_sha256"] == "a" * 64
    assert contract["boundary"]["execution_mode"] == "production_no_live"
    assert contract["boundary"]["formal_probability_input_eligible"] is True
    assert contract["boundary"]["live_submit_authorized"] is False
