from __future__ import annotations

from contextlib import contextmanager

import production_amsc_archive_probability_ingress as runtime


class _Connection:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.in_transaction = False

    def execute(self, sql: str):
        assert sql == "BEGIN IMMEDIATE"
        self.in_transaction = True
        self.events.append("source_begin")
        return self

    def commit(self) -> None:
        self.in_transaction = False
        self.events.append("source_commit")

    def rollback(self) -> None:
        self.in_transaction = False
        self.events.append("source_rollback")


def test_source_observation_and_canonical_commit_before_probability_candidate(monkeypatch, tmp_path) -> None:
    events: list[str] = []
    connection_kwargs: list[dict] = []
    connection = _Connection(events)

    @contextmanager
    def connect(*_args, **kwargs):
        connection_kwargs.append(dict(kwargs))
        yield connection

    monkeypatch.setattr(runtime, "connect_archive_db_rw", connect)
    monkeypatch.setattr(
        runtime,
        "persist_source_observations_for_payload",
        lambda *_args, **_kwargs: events.append("source_persist") or {
            "schema": "source_observation_persistence_v1",
            "ok": True,
            "state": "available",
            "attempted": 1,
            "inserted": 1,
        },
    )
    monkeypatch.setattr(
        runtime,
        "apply_p2_canonical_amsc_forward_append",
        lambda *_args, **_kwargs: events.append("canonical_commit") or {"ok": True},
    )
    monkeypatch.setattr(runtime, "load_production_probability_activation", lambda *_: {"state": "production_enabled"})
    monkeypatch.setattr(
        runtime,
        "ensure_production_daily_p0",
        lambda *_args, **_kwargs: events.append("p0_ensure") or {"ok": True, "state": "already_initialized"},
    )
    monkeypatch.setattr(
        runtime,
        "drain_production_canonical_events",
        lambda *_args, **_kwargs: events.append("posterior_commit") or {"ok": True, "state": "drained"},
    )

    result = runtime.apply_production_amsc_archive_probability_ingress(
        db_path=tmp_path / "archive.db",
        raw_archive_dir=tmp_path / "raw",
        event_date="2026-07-14",
        decision_as_of="2026-07-14T08:00:00Z",
        source_observation_payload={"source_observations": [{"source_id": "zuuu_metar_raw_amsc"}]},
    )

    assert result["ok"] is True
    assert events == [
        "source_begin",
        "source_persist",
        "source_commit",
        "canonical_commit",
        "p0_ensure",
        "posterior_commit",
    ]
    assert result["boundary"]["legacy_fact_projection_precedes_probability"] is False
    assert result["boundary"]["single_writer_queue_claim_across_a0_a1_and_transaction_b"] is True
    assert len(connection_kwargs) == 2
    assert all(row["wal_autocheckpoint_pages"] == 0 for row in connection_kwargs)
    assert set(result["latency_stages_ms"]) == {
        "writer_queue_wait_ms",
        "source_observation_commit_ms",
        "canonical_commit_ms",
        "canonical_connection_total_ms",
        "canonical_connection_close_ms",
        "activation_load_ms",
        "daily_p0_ensure_ms",
        "probability_commit_ms",
        "sequential_update_ms",
        "probability_connection_close_ms",
        "total_ingress_ms",
    }


def test_source_schema_unavailable_rolls_back_before_canonical(monkeypatch, tmp_path) -> None:
    events: list[str] = []
    connection = _Connection(events)

    @contextmanager
    def connect(*_args, **_kwargs):
        yield connection

    monkeypatch.setattr(runtime, "connect_archive_db_rw", connect)
    monkeypatch.setattr(
        runtime,
        "persist_source_observations_for_payload",
        lambda *_args, **_kwargs: {"ok": True, "state": "table_missing"},
    )
    monkeypatch.setattr(
        runtime,
        "apply_p2_canonical_amsc_forward_append",
        lambda *_args, **_kwargs: events.append("canonical_commit") or {"ok": True},
    )

    try:
        runtime.apply_production_amsc_archive_probability_ingress(
            db_path=tmp_path / "archive.db",
            raw_archive_dir=tmp_path / "raw",
            event_date="2026-07-14",
            decision_as_of="2026-07-14T08:00:00Z",
            source_observation_payload={"source_observations": [{}]},
        )
    except ValueError as exc:
        assert str(exc) == "amsc_source_observation_schema_not_available"
    else:
        raise AssertionError("source schema failure must fail closed")

    assert events == ["source_begin", "source_rollback"]
