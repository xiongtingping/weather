from __future__ import annotations

import json

from market_resolution_finality_parser import parse_market_resolution_finality


SLUG = "highest-temperature-in-chengdu-on-july-3-2026"
SOURCE = "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU"


def _raw(status: str = "resolved") -> bytes:
    return json.dumps(
        [
            {
                "slug": SLUG,
                "eventDate": "2026-07-03",
                "closed": True,
                "resolutionSource": SOURCE,
                "markets": [
                    {"groupItemTitle": "31°C", "umaResolutionStatus": status, "outcomes": ["Yes", "No"], "outcomePrices": [0.9, 0.1]},
                    {"groupItemTitle": "32°C", "umaResolutionStatus": status, "outcomes": ["Yes", "No"], "outcomePrices": [0.1, 0.9]},
                ],
            }
        ]
    ).encode("utf-8")


def test_finality_parser_uses_resolved_status_without_outcome_or_price() -> None:
    result = parse_market_resolution_finality(
        _raw(), public_slug=SLUG, resolution_local_date="2026-07-03", response_completed_at="2026-07-03T18:00:00Z"
    )
    assert result["ok"] is True
    assert result["market_status"] == "resolved"
    assert result["resolution_state_observed_at"] == "2026-07-03T18:00:00Z"
    assert result["boundary"]["does_not_read_market_outcome"] is True
    assert "outcomes" not in result
    assert "outcomePrices" not in result


def test_finality_parser_requires_every_bucket_market_to_be_resolved() -> None:
    result = parse_market_resolution_finality(
        _raw("pending"), public_slug=SLUG, resolution_local_date="2026-07-03", response_completed_at="2026-07-03T18:00:00Z"
    )
    assert result["ok"] is False
    assert result["block_reasons"] == ["market_finality_parser_not_all_markets_resolved"]
