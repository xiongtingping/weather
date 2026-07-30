from __future__ import annotations

import json

from ui import server


def test_probability_chain_health_endpoint_is_read_only_and_passes_ui_lineage(monkeypatch):
    calls = []

    def fake_probe(**kwargs):
        calls.append(kwargs)
        return {
            "schema": "probability_chain_freshness_probe_v1",
            "state": "fresh",
            "latest_checkpoint": "checkpoint-1",
            "latest_fact": "fact-1",
            "lag_seconds": 2,
            "broken_at": None,
            "blockers": [],
            "ui": {"hash_match": True, "blocker": None},
        }

    with server.PROBABILITY_CHAIN_HEALTH_CACHE_LOCK:
        server.PROBABILITY_CHAIN_HEALTH_CACHE.clear()
    monkeypatch.setattr(server, "probe_chain", fake_probe)

    payload = server.probability_chain_health(
        prefer_run_date="2026-07-21",
        slug="highest-temperature-in-chengdu-on-july-21-2026",
        ui_identity={
            "fact_snapshot_id": "fact-1",
            "posterior_checkpoint_id": "checkpoint-1",
        },
    )

    assert payload["state"] == "fresh"
    assert payload["endpoint"] == "/api/probability-chain-health"
    assert payload["ui_checked"] is True
    assert calls == [
        {
            "db_path": server.DB_PATH,
            "event_date": "2026-07-21",
            "slug": "highest-temperature-in-chengdu-on-july-21-2026",
            "source_age_sec": 60.0,
            "include_ui": False,
            "ui_identity_override": None,
        }
    ]


def test_probability_chain_health_ui_mount_and_poll_are_present():
    html = server.load_html()
    assert 'id="probabilityChainFreshnessMount"' in html
    assert html.count('id="probabilityChainFreshnessMount"') == 1
    assert html.index('id="probabilityChainFreshnessMount"') < html.index('id="autoTradeMonitor"')
    assert "/api/probability-chain-health" in html
    assert "PROBABILITY_CHAIN_HEALTH_REFRESH_MS = 30000" in html
    assert "ui_snapshot" not in html[html.index("/api/probability-chain-health") : html.index("/api/probability-chain-health") + 250]


def test_cached_manifest_drift_is_kept_until_live_runtime_is_repaired(monkeypatch, tmp_path):
    activation_dir = tmp_path / "config" / "production_probability_runtime_activation.json"
    activation_dir.parent.mkdir(parents=True)
    activation_dir.write_text(json.dumps({"runtime_manifest_sha256": "runtime-2"}), encoding="utf-8")
    monkeypatch.setattr(server, "ROOT_DIR", tmp_path)

    import production_probability_v6_runtime_manifest as runtime_manifest

    monkeypatch.setattr(runtime_manifest, "v6_runtime_manifest_sha256", lambda root: "runtime-1")
    cached = {"manifest": {"blocker": "manifest_drift"}}
    assert server._probability_chain_cached_manifest_drift(cached) is False

    monkeypatch.setattr(runtime_manifest, "v6_runtime_manifest_sha256", lambda root: "runtime-2")
    assert server._probability_chain_cached_manifest_drift(cached) is True
