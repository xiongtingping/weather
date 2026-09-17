import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_delta_basket_score_v2_no_live.sh"


def test_cutover_uses_v2_manifest_without_subminimum_clip() -> None:
    manifest = json.loads(
        (
            ROOT
            / "config/trading/"
            "delta_basket_score_v2_rkpk_no_live_bootstrap_v2.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["strategy_version"] == "target-v2"
    assert manifest["cash_clip_grid"] == [1.0, 2.0]
    assert min(manifest["price_grid"]) == 0.01
    assert 0.20 in manifest["price_grid"]
    assert max(manifest["price_grid"]) == 0.98
    assert manifest["live_submit_authorized"] is False
    assert manifest["calls_order_gateway"] is False
