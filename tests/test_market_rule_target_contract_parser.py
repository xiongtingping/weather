from __future__ import annotations

import json
from pathlib import Path

from market_rule_target_contract_parser import parse_market_rule_target_contract_extraction
from resolution_target_contract_runtime import build_market_resolution_target_contract_proposal


SLUG = "highest-temperature-in-chengdu-on-july-3-2026"
SOURCE = "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU"
ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = json.loads((ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_text())


def _raw() -> bytes:
    return json.dumps(
        {
            "slug": SLUG,
            "eventDate": "2026-07-03",
            "resolutionSource": SOURCE,
            "markets": [
                {"groupItemTitle": "30°C or below", "outcomes": ["Yes", "No"], "outcomePrices": [0.2, 0.8]},
                {"groupItemTitle": "31°C", "outcomes": ["Yes", "No"], "outcomePrices": [0.5, 0.5]},
                {"groupItemTitle": "32°C or higher", "outcomes": ["Yes", "No"], "outcomePrices": [0.3, 0.7]},
            ],
        }
    ).encode("utf-8")


def test_rule_parser_extracts_only_source_and_bucket_contract_not_outcome_or_price() -> None:
    result = parse_market_rule_target_contract_extraction(
        _raw(), public_slug=SLUG, resolution_local_date="2026-07-03"
    )
    assert result["ok"] is True
    extraction = result["extraction"]
    assert extraction["station_id"] == "ZUUU"
    assert extraction["bucket_contract"]["30°C or below"] == {"lower_inclusive": None, "upper_exclusive": 30.5}
    assert extraction["bucket_contract"]["32°C or higher"] == {"lower_inclusive": 31.5, "upper_exclusive": None}
    assert "outcomes" not in extraction
    assert "outcomePrices" not in extraction
    assert "outcome" not in " ".join(extraction["extraction_evidence_fragments"]).lower()
    assert "price" not in " ".join(extraction["extraction_evidence_fragments"]).lower()


def test_rule_parser_rejects_bucket_without_explicit_celsius_contract() -> None:
    raw = json.loads(_raw())
    raw["markets"][1]["groupItemTitle"] = "31°F"
    result = parse_market_rule_target_contract_extraction(
        json.dumps(raw).encode("utf-8"), public_slug=SLUG, resolution_local_date="2026-07-03"
    )
    assert result["ok"] is False
    assert result["block_reasons"] == ["market_rule_parser_bucket_label_invalid_or_duplicate"]


def test_raw_rule_parser_can_feed_target_contract_with_open_end_buckets() -> None:
    raw = _raw()
    parsed = parse_market_rule_target_contract_extraction(raw, public_slug=SLUG, resolution_local_date="2026-07-03")
    proposal = build_market_resolution_target_contract_proposal(
        authority_contract=AUTHORITY,
        target_identity=f"market_resolution_target_v1:{SLUG}",
        public_slug=SLUG,
        resolution_local_date="2026-07-03",
        station_id="ZUUU",
        resolution_source=SOURCE,
        market_rule_raw_payload=raw,
        market_rule_raw_ref="market-rules/2026-07-03/raw.json",
        market_rule_extraction=parsed["extraction"],
    )
    assert proposal["ok"] is True
    assert proposal["bucket_contract"]["30°C or below"]["lower_inclusive"] is None
    assert proposal["bucket_contract"]["32°C or higher"]["upper_exclusive"] is None


def test_rule_parser_accepts_raw_gamma_event_list_shape() -> None:
    raw = json.dumps([json.loads(_raw())]).encode("utf-8")
    result = parse_market_rule_target_contract_extraction(raw, public_slug=SLUG, resolution_local_date="2026-07-03")
    assert result["ok"] is True
