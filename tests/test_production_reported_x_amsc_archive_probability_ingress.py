from __future__ import annotations

from contextlib import contextmanager

import production_reported_x_amsc_archive_probability_ingress as runtime


class _Cursor:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class _Connection:
    def __init__(self):
        self.in_transaction = False

    def execute(self, sql, params=()):
        if sql == "BEGIN IMMEDIATE":
            self.in_transaction = True
            return _Cursor()
        if "FROM canonical_observation_event_sources" in sql:
            assert set(params) == {"src-old", "src-new"}
            return _Cursor([("src-old",)])
        raise AssertionError(f"unexpected SQL: {sql}")

    def commit(self):
        self.in_transaction = False

    def rollback(self):
        self.in_transaction = False


def test_fast_lane_persists_only_uncanonicalized_primary_rows(monkeypatch, tmp_path) -> None:
    conn = _Connection()
    persisted_rows = []
    canonical_ids = []

    @contextmanager
    def gate(*args, **kwargs):
        yield

    @contextmanager
    def connect(*args, **kwargs):
        yield conn

    def persist(_conn, payload, *, created_at):
        persisted_rows.extend(payload.get("source_observations") or [])
        return {"ok": True, "state": "available", "attempted": len(persisted_rows), "inserted": 1}

    def canonical(_conn, **kwargs):
        canonical_ids.extend(kwargs.get("source_observation_ids") or [])
        return {"ok": True, "state": "applied"}

    monkeypatch.setattr(runtime, "archive_db_write_gate", gate)
    monkeypatch.setattr(runtime, "connect_archive_db_rw", connect)
    monkeypatch.setattr(runtime, "persist_source_observations_for_payload", persist)
    monkeypatch.setattr(runtime, "apply_p2_canonical_amsc_forward_append", canonical)
    monkeypatch.setattr(runtime, "load_successor_activation", lambda path: {"activation_contract_sha256": "sha"})
    monkeypatch.setattr(runtime, "ensure_reported_x_daily_p0", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(runtime, "drain_reported_x_canonical_events", lambda *args, **kwargs: {"ok": True})

    payload = {
        "source_observations": [
            {
                "source_id": "zuuu_metar_raw_amsc",
                "source_observation_id": "src-old",
                "event_date": "2026-07-15",
            },
            {
                "source_id": "zuuu_metar_raw_amsc",
                "source_observation_id": "src-new",
                "event_date": "2026-07-15",
            },
            {
                "source_id": "zuuu_taf_forecast_amsc",
                "source_observation_id": "src-taf",
                "event_date": "2026-07-15",
            },
        ]
    }
    result = runtime.apply_reported_x_amsc_archive_probability_ingress(
        db_path=tmp_path / "archive.db",
        raw_archive_dir=tmp_path / "raw",
        event_date="2026-07-15",
        decision_as_of="2026-07-15T11:00:51Z",
        source_observation_payload=payload,
        source_observation_created_at="2026-07-15T11:00:51Z",
    )

    assert result["ok"] is True
    assert [row["source_observation_id"] for row in persisted_rows] == ["src-new"]
    assert canonical_ids == ["src-new"]
    source = result["source_observations"]
    assert source["provider_window_primary_candidate_count"] == 2
    assert source["incremental_primary_candidate_count"] == 1
    assert source["provider_window_non_primary_rows_deferred_after_t3"] == 1
    assert {
        "source_observation_append_ms",
        "canonical_append_ms",
        "daily_p0_ensure_ms",
        "canonical_probability_drain_ms",
        "total_ingress_ms",
    }.issubset(result["latency_stages_ms"])


def test_fast_lane_rejects_primary_row_without_immutable_identity(monkeypatch, tmp_path) -> None:
    conn = _Connection()

    @contextmanager
    def gate(*args, **kwargs):
        yield

    @contextmanager
    def connect(*args, **kwargs):
        yield conn

    monkeypatch.setattr(runtime, "archive_db_write_gate", gate)
    monkeypatch.setattr(runtime, "connect_archive_db_rw", connect)

    try:
        runtime.apply_reported_x_amsc_archive_probability_ingress(
            db_path=tmp_path / "archive.db",
            raw_archive_dir=tmp_path / "raw",
            event_date="2026-07-15",
            decision_as_of="2026-07-15T11:00:51Z",
            source_observation_payload={
                "source_observations": [
                    {"source_id": "zuuu_metar_raw_amsc", "event_date": "2026-07-15"}
                ]
            },
        )
    except ValueError as exc:
        assert str(exc) == "reported_x_primary_source_observation_identity_missing"
    else:
        raise AssertionError("missing primary identity must fail closed")
