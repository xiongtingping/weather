from __future__ import annotations

import json

from observation_fast_lane_contract import build_observation_fast_lane_contract
from scripts import observation_fast_lane_latency_slo_audit as audit


def test_cli_accepts_packet_collection_and_remains_read_only(monkeypatch, capsys, tmp_path) -> None:
    topology = {
        "release_delay_ms": 0,
        "synchronous_forecast_refresh_on_hot_path": False,
        "generic_task_queue_on_hot_path": False,
        "resident_canonical_and_inference_worker": True,
        "transaction_b_atomic_before_intent": True,
        "posterior_commit_outbox_durable_before_intent": True,
        "decision_reads_committed_ssot": True,
        "signer_connection_pool_resident": True,
    }
    packet_path = tmp_path / "packets.json"
    packet_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "event_date": "2026-07-03",
                        "event_type": "SPECI",
                        "correlation_id": "corr_cli_001",
                        "fast_lane": build_observation_fast_lane_contract(
                            correlation_id="corr_cli_001",
                            timestamps={
                                "first_actionable_seen_at": "2026-07-03T06:30:00.000Z",
                                "canonicalized_at": "2026-07-03T06:30:00.050Z",
                                "transaction_b_committed_at": "2026-07-03T06:30:00.400Z",
                                "decision_intent_durable_at": "2026-07-03T06:30:00.700Z",
                            },
                            topology=topology,
                            mode="shadow",
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(audit.sys, "argv", ["observation_fast_lane_latency_slo_audit.py", "--input", str(packet_path)])

    assert audit.main() == 0

    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["overall"]["t1_to_t3"]["p99_ms"] == 400.0
    assert report["guards"]["writes_archive_db"] is False
    assert report["guards"]["places_orders"] is False
