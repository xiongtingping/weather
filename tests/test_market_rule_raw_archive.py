from __future__ import annotations

import json

from market_rule_raw_archive import archive_market_rule_raw_snapshot, build_gamma_event_capture_url, load_market_rule_raw_snapshot


def test_market_rule_raw_archive_retains_raw_without_reading_market_semantics(tmp_path) -> None:
    raw = b'{"outcomes":["Yes","No"],"outcomePrices":[0.1,0.9],"rules":"daily high"}'
    result = archive_market_rule_raw_snapshot(
        archive_dir=tmp_path,
        target_identity="market_resolution_target_v1:sample",
        public_slug="sample",
        resolution_local_date="2026-07-03",
        source_url="https://gamma-api.polymarket.com/events/sample",
        raw_payload=raw,
        requested_at="2026-07-03T00:00:00Z",
        response_started_at="2026-07-03T00:00:01Z",
        response_completed_at="2026-07-03T00:00:02Z",
        response_status=200,
        response_headers={"Content-Type": "application/json", "Set-Cookie": "secret"},
    )
    loaded = load_market_rule_raw_snapshot(archive_dir=tmp_path, manifest_ref=result["manifest_ref"])
    manifest = json.loads((tmp_path / result["manifest_ref"]).read_text())
    assert loaded["raw_payload"] == raw
    assert result["guards"]["does_not_read_market_outcome"] is True
    assert "Set-Cookie" not in manifest["response_headers"]
    retry = archive_market_rule_raw_snapshot(
        archive_dir=tmp_path,
        target_identity="market_resolution_target_v1:sample",
        public_slug="sample",
        resolution_local_date="2026-07-03",
        source_url="https://gamma-api.polymarket.com/events/sample",
        raw_payload=raw,
        requested_at="2026-07-03T00:00:00Z",
        response_started_at="2026-07-03T00:00:01Z",
        response_completed_at="2026-07-03T00:00:02Z",
        response_status=200,
    )
    assert retry["raw_object_reused"] is True


def test_gamma_event_capture_url_encodes_the_public_slug() -> None:
    assert build_gamma_event_capture_url(public_slug="target with spaces") == "https://gamma-api.polymarket.com/events?slug=target+with+spaces"
