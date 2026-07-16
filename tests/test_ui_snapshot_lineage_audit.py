import json
import sqlite3
from pathlib import Path

from scripts import ui_snapshot_lineage_audit as audit


def _write_fact_db(tmp_path: Path, fact_json: dict) -> Path:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE event_fact_history (
                id INTEGER PRIMARY KEY,
                slug TEXT,
                event_date TEXT,
                fact_as_of TEXT,
                created_at TEXT,
                fact_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO event_fact_history
                (id, slug, event_date, fact_as_of, created_at, fact_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "highest-temperature-in-chengdu-on-july-4-2026",
                "2026-07-04",
                "2026-07-04T04:06:00+00:00",
                "2026-07-04T04:06:01+00:00",
                json.dumps(fact_json),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _posterior_ssot_blocks(*, fact_snapshot_id: str = "fact_abc", target_bucket: str = "30°C", target_probability: float = 0.7) -> dict:
    ssot = {
        "schema": "bayes_posterior_ssot_fact_v1",
        "state": "available",
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": "post_1",
        "lineage": {
            "source_event_ids": ["srcevt_1"],
            "source_observation_ids": ["srcobs_1"],
        },
        "target_bucket": target_bucket,
        "target_probability": target_probability,
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "phase": "D0",
        "beta_weak": 0.25,
        "beta_strong": 1.0,
        "phase_reason": "fixture_d0_observation_phase",
        "hard_mask_reason": "observed_today_max_c",
        "boundaries": {
            "frontend_recompute_allowed": False,
            "decision_recompute_allowed": False,
        },
    }
    selection = {
        "schema": "posterior_target_bucket_selection_v1",
        "ssot_source": "posterior_ssot_fact",
        "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "target_bucket": target_bucket,
        "target_probability": target_probability,
        "fact_snapshot_id": fact_snapshot_id,
    }
    return {
        "posterior_ssot_fact": ssot,
        "posterior_target_bucket_selection": selection,
        "target_bucket_selection": dict(selection),
    }


def _bayes_section_from_posterior_blocks(blocks: dict, *, fact_snapshot_id: str = "fact_abc") -> dict:
    ssot = blocks.get("posterior_ssot_fact") if isinstance(blocks, dict) else {}
    ssot = ssot if isinstance(ssot, dict) else {}
    state = str(ssot.get("state") or "available")
    section = {
        "schema": "ui_bayes_posterior_ssot_section_v1",
        "state": state,
        "read_only": True,
        "source": "facts.items[].posterior_ssot_fact",
        "fact_snapshot_id": str(ssot.get("fact_snapshot_id") or fact_snapshot_id),
        "posterior_run_id": str(ssot.get("posterior_run_id") or "post_1"),
        "posterior_as_of": "2026-07-04T04:06:00+00:00",
        "target_bucket": str(ssot.get("target_bucket") or ""),
        "target_bucket_label": str(ssot.get("target_bucket") or ""),
        "target_probability": ssot.get("target_probability"),
        "target_probability_field": str(ssot.get("target_probability_field") or "posterior_ssot_fact.target_probability"),
        "phase": str(ssot.get("phase") or "D0"),
        "beta_weak": ssot.get("beta_weak", 0.25),
        "beta_strong": ssot.get("beta_strong", 1.0),
        "phase_reason": str(ssot.get("phase_reason") or "fixture_d0_observation_phase"),
        "hard_mask_reason": str(ssot.get("hard_mask_reason") or "observed_today_max_c"),
        "frontend_recompute_allowed": False,
        "decision_recompute_allowed": False,
        "posterior_distribution_source": "posterior_ssot_fact.bucket_probabilities",
        "na_reason": str(ssot.get("state_reason") or "") if state != "available" else "",
    }
    for key in ("bucket_probabilities", "ranked_bucket_probabilities", "lineage", "boundaries"):
        if ssot.get(key) not in (None, "", [], {}):
            section[key] = ssot[key]
    return section


def _posterior_ssot_blocks_with_distribution() -> dict:
    blocks = _posterior_ssot_blocks(target_bucket="38°C", target_probability=0.4287)
    rows = [
        {"outcome": "37°C", "probability": 0.3236, "probability_pct": 32.36},
        {"outcome": "38°C", "probability": 0.4287, "probability_pct": 42.87},
        {"outcome": "39°C", "probability": 0.2038, "probability_pct": 20.38},
        {"outcome": "40°C", "probability": 0.0405, "probability_pct": 4.05},
        {"outcome": "41°C", "probability": 0.0033, "probability_pct": 0.33},
    ]
    ranked = [
        {"outcome": "38°C", "probability": 0.4287, "probability_pct": 42.87},
        {"outcome": "37°C", "probability": 0.3236, "probability_pct": 32.36},
        {"outcome": "39°C", "probability": 0.2038, "probability_pct": 20.38},
    ]
    blocks["posterior_ssot_fact"]["bucket_probabilities"] = list(rows)
    blocks["posterior_ssot_fact"]["ranked_bucket_probabilities"] = list(ranked)
    return blocks


def _ui_payload(*, fact_snapshot_id: str = "fact_abc", posterior_blocks: dict | None = None) -> dict:
    if posterior_blocks is None:
        posterior_blocks = _posterior_ssot_blocks(fact_snapshot_id=fact_snapshot_id)
    fact_item = {
        "slug": "highest-temperature-in-chengdu-on-july-4-2026",
        "event_date": "2026-07-04",
        "fact_id": 1,
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": "post_1",
    }
    if posterior_blocks:
        fact_item.update(posterior_blocks)
    fact_item["ui_sections"] = {
        "schema": "ui_snapshot_item_sections_v1",
        "read_only": True,
        "bayes_posterior_ssot": _bayes_section_from_posterior_blocks(
            posterior_blocks,
            fact_snapshot_id=fact_snapshot_id,
        ),
    }
    return {
        "ok": True,
        "snapshot_id": "uisnap_abc",
        "snapshot_etag": 'W/"uisnap_abc"',
        "run_date": "2026-07-04",
        "snapshot_freshness_state": "fresh",
        "snapshot_freshness_reason": "snapshot_ready",
        "source_fact_ids": [1],
        "fact_snapshot_ids": [fact_snapshot_id],
        "source_event_ids": ["srcevt_1"],
        "source_observation_ids": ["srcobs_1"],
        "panel_freshness": {
            "facts": {"state": "fresh", "source": "event_fact_latest", "stale_reason": "", "na_reason": ""},
            "posterior": {
                "state": "fresh",
                "source": "facts.items[].ui_sections.bayes_posterior_ssot",
                "stale_reason": "",
                "na_reason": "",
            },
            "decisions": {
                "state": "missing",
                "source": "auto_trade_decisions.path_probability_follow",
                "stale_reason": "",
                "na_reason": "auto_trade_decisions_missing_or_no_rows",
            },
        },
        "ui_sections": {
            "schema": "ui_snapshot_dashboard_sections_v1",
            "read_only": True,
            "source": "/api/ui_snapshot",
            "order": ["facts", "posterior", "decisions"],
            "facts": {
                "schema": "ui_snapshot_facts_section_v1",
                "state": "fresh",
                "source": "event_fact_latest",
                "item_count": 1,
                "latest_fact_as_of": "2026-07-04T04:06:00+00:00",
                "stale_reason": "",
                "na_reason": "",
            },
            "posterior": {
                "schema": "ui_snapshot_posterior_section_v1",
                "state": "fresh",
                "source": "facts.items[].ui_sections.bayes_posterior_ssot",
                "available_count": 1,
                "missing_count": 0,
                "stale_reason": "",
                "na_reason": "",
            },
            "decisions": {
                "schema": "ui_snapshot_decisions_section_v1",
                "state": "missing",
                "source": "auto_trade_decisions.path_probability_follow",
                "counts": {"total": 0, "blocked": 0, "will_order": 0},
                "latest_decision_at": "",
                "blocked_reasons": [],
                "latest": [],
                "stale_reason": "",
                "na_reason": "auto_trade_decisions_missing_or_no_rows",
            },
        },
        "lineage_state": {
            "schema": "ui_snapshot_lineage_state_v1",
            "placeholder": False,
            "fact_lineage_complete": True,
            "source_fact_ids": [1],
            "fact_snapshot_ids": [fact_snapshot_id],
            "source_event_ids": ["srcevt_1"],
            "source_observation_ids": ["srcobs_1"],
            "posterior_run_ids": ["post_1"],
            "items": [
                {
                    "slug": "highest-temperature-in-chengdu-on-july-4-2026",
                    "event_date": "2026-07-04",
                    "fact_id": 1,
                    "fact_snapshot_id": fact_snapshot_id,
                    "posterior_run_id": "post_1",
                    "source_event_ids": ["srcevt_1"],
                    "source_observation_ids": ["srcobs_1"],
                    "signal_id": "sig_1",
                }
            ],
        },
        "facts": {
            "items": [fact_item]
        },
    }


def test_ui_snapshot_lineage_audit_accepts_matching_fact_snapshot(tmp_path: Path) -> None:
    db_path = _write_fact_db(
        tmp_path,
        {
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "lineage_state": {
                "fact_snapshot_id": "fact_abc",
                "posterior_run_id": "post_1",
                "source_event_ids": ["srcevt_1"],
                "source_observation_ids": ["srcobs_1"],
            },
            "signal_to_order_timing": {
                "signal_id": "sig_1",
                "source_event_id": "srcevt_1",
                "source_observation_id": "srcobs_1",
            },
        },
    )

    result = audit.audit_payload(_ui_payload(), db_path=db_path, run_replay=False)

    assert result["ok"] is True
    assert result["blockers"] == []
    assert result["counts"]["checked_facts"] == 1
    assert result["fact_results"][0]["fact_snapshot_id"] == "fact_abc"


def test_ui_snapshot_lineage_audit_enforces_desktop_first_screen_contract(tmp_path: Path) -> None:
    db_path = _write_fact_db(
        tmp_path,
        {
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "lineage_state": {
                "fact_snapshot_id": "fact_abc",
                "posterior_run_id": "post_1",
                "source_event_ids": ["srcevt_1"],
                "source_observation_ids": ["srcobs_1"],
            },
            "source_observations": [{"source_event_id": "srcevt_1", "source_observation_id": "srcobs_1"}],
        },
    )

    result = audit.audit_payload(_ui_payload(), db_path=db_path, run_replay=False)

    assert result["ok"] is True
    contract = result["desktop_first_screen"]
    assert contract["schema"] == "ui_snapshot_desktop_first_screen_contract_v1"
    assert contract["ok"] is True
    assert contract["snapshot"]["snapshot_freshness_state"] == "fresh"
    assert contract["posterior"]["phase"] == "D0"
    assert contract["posterior"]["target_probability_field"] == "posterior_ssot_fact.target_probability"
    assert contract["posterior"]["hard_mask_reason"] == "observed_today_max_c"
    assert contract["decision_blocker"]["source"] == "auto_trade_decisions.path_probability_follow"

    broken = _ui_payload()
    broken["facts"]["items"][0]["ui_sections"]["bayes_posterior_ssot"].pop("hard_mask_reason", None)
    broken["facts"]["items"][0].pop("hard_mask_reason", None)
    broken["facts"]["items"][0]["posterior_ssot_fact"].pop("hard_mask_reason", None)
    broken_result = audit.audit_payload(broken, db_path=db_path, run_replay=False)

    assert broken_result["ok"] is False
    assert "desktop_first_screen_hard_mask_reason_missing" in broken_result["blockers"]


def test_ui_snapshot_lineage_audit_compares_replay_result_to_ui_payload(tmp_path: Path) -> None:
    db_path = _write_fact_db(
        tmp_path,
        {
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "lineage_state": {
                "fact_snapshot_id": "fact_abc",
                "posterior_run_id": "post_1",
                "source_event_ids": ["srcevt_1"],
                "source_observation_ids": ["srcobs_1"],
            },
            "source_observations": [
                {"source_event_id": "srcevt_1", "source_observation_id": "srcobs_1"}
            ],
        },
    )
    payload = _ui_payload()
    payload["lineage_state"]["items"][0].pop("signal_id", None)

    result = audit.audit_payload(payload, db_path=db_path, run_replay=True)

    assert result["ok"] is True
    consistency = result["ui_replay_consistency"]
    assert consistency["schema"] == "ui_replay_consistency_contract_v1"
    assert consistency["enabled"] is True
    assert consistency["ok"] is True
    assert consistency["checked_fact_count"] == 1
    item = consistency["items"][0]
    assert item["expected"]["fact_snapshot_ids"] == ["fact_abc"]
    assert item["resolved"]["fact_snapshot_ids"] == ["fact_abc"]
    assert item["expected"]["posterior_run_ids"] == ["post_1"]
    assert item["resolved"]["posterior_run_ids"] == ["post_1"]
    assert item["expected"]["source_observation_ids"] == ["srcobs_1"]
    assert item["resolved"]["source_observation_ids"] == ["srcobs_1"]
    assert consistency["guards"]["writes_archive_db"] is False


def test_ui_snapshot_lineage_audit_blocks_replay_fact_snapshot_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = _write_fact_db(
        tmp_path,
        {
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "lineage_state": {
                "fact_snapshot_id": "fact_abc",
                "posterior_run_id": "post_1",
                "source_event_ids": ["srcevt_1"],
                "source_observation_ids": ["srcobs_1"],
            },
            "source_observations": [
                {"source_event_id": "srcevt_1", "source_observation_id": "srcobs_1"}
            ],
        },
    )
    payload = _ui_payload()
    payload["lineage_state"]["items"][0].pop("signal_id", None)

    from scripts import ssot_chain_replay_audit

    def fake_replay(*args, **kwargs):
        return {
            "ok": True,
            "effective_slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "effective_event_date": "2026-07-04",
            "effective_source_event_id": "srcevt_1",
            "effective_source_observation_id": "srcobs_1",
            "signal_count": 0,
            "blockers": [],
            "warnings": [],
            "anchor_trace": {
                "resolved": {
                    "slug": "highest-temperature-in-chengdu-on-july-4-2026",
                    "event_date": "2026-07-04",
                    "fact_snapshot_id": "fact_other",
                    "posterior_run_id": "post_1",
                    "source_event_ids": ["srcevt_1"],
                    "source_observation_ids": ["srcobs_1"],
                }
            },
            "lineage_consistency": {
                "fact_snapshot_ids": ["fact_other"],
                "posterior_run_ids": ["post_1"],
            },
        }

    monkeypatch.setattr(ssot_chain_replay_audit, "audit", fake_replay)

    result = audit.audit_payload(payload, db_path=db_path, run_replay=True)

    assert result["ok"] is False
    assert "ui_replay_fact_snapshot_id_mismatch" in result["blockers"]
    assert result["ui_replay_consistency"]["ok"] is False
    assert result["ui_replay_consistency"]["items"][0]["blockers"] == [
        "ui_replay_fact_snapshot_id_mismatch"
    ]


def test_ui_snapshot_lineage_audit_blocks_missing_source_observation_ids(tmp_path: Path) -> None:
    db_path = _write_fact_db(
        tmp_path,
        {
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "lineage_state": {
                "fact_snapshot_id": "fact_abc",
                "posterior_run_id": "post_1",
                "source_event_ids": ["srcevt_1"],
                "source_observation_ids": ["srcobs_1"],
            },
            "source_observations": [
                {"source_event_id": "srcevt_1", "source_observation_id": "srcobs_1"}
            ],
        },
    )
    payload = _ui_payload()
    payload.pop("source_observation_ids", None)
    payload["lineage_state"].pop("source_observation_ids", None)
    payload["lineage_state"]["items"][0].pop("source_observation_ids", None)

    result = audit.audit_payload(payload, db_path=db_path, run_replay=False)

    assert result["ok"] is False
    assert "ui_snapshot_source_observation_ids_missing" in result["blockers"]


def test_ui_snapshot_lineage_audit_ignores_display_only_derived_observation_ids(tmp_path: Path) -> None:
    db_path = _write_fact_db(
        tmp_path,
        {
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "lineage_state": {
                "fact_snapshot_id": "fact_abc",
                "posterior_run_id": "post_1",
                "source_event_ids": ["srcevt_1"],
                "source_observation_ids": ["srcobs_1"],
            },
            "source_observations": [
                {"source_event_id": "srcevt_1", "source_observation_id": "srcobs_1"}
            ],
        },
    )
    payload = _ui_payload()
    display_row = {
        "source_event_id": "srcevt_display",
        "source_observation_id": "srcobs_display",
        "source_id": "zuuu_metar_raw_awc",
        "ui_display_derived_from_fact_features": True,
        "not_posterior_input": True,
        "payload": {
            "boundary": "display_only_from_existing_fact_fields_no_frontend_recompute_no_db_write",
        },
    }
    payload["source_event_ids"].append("srcevt_display")
    payload["source_observation_ids"].append("srcobs_display")
    payload["lineage_state"]["source_event_ids"].append("srcevt_display")
    payload["lineage_state"]["source_observation_ids"].append("srcobs_display")
    payload["lineage_state"]["items"][0]["source_event_ids"].append("srcevt_display")
    payload["lineage_state"]["items"][0]["source_observation_ids"].append("srcobs_display")
    payload["facts"]["items"][0]["source_observations"] = [display_row]
    payload["facts"]["items"][0]["source_acquisition_trace"] = {
        "schema": "ui_snapshot_derived_source_trace_v1",
        "display_only": True,
        "source_observations": [dict(display_row)],
        "source_event_ids": ["srcevt_display"],
        "source_observation_ids": ["srcobs_display"],
    }

    result = audit.audit_payload(payload, db_path=db_path, run_replay=False)

    assert result["ok"] is True
    assert "ui_snapshot_source_event_id_not_in_fact_json" not in result["blockers"]
    assert "ui_snapshot_source_observation_id_not_in_fact_json" not in result["blockers"]


def test_ui_snapshot_lineage_audit_blocks_fact_snapshot_mismatch(tmp_path: Path) -> None:
    db_path = _write_fact_db(
        tmp_path,
        {
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "lineage_state": {
                "fact_snapshot_id": "fact_db",
                "posterior_run_id": "post_1",
                "source_event_ids": ["srcevt_1"],
                "source_observation_ids": ["srcobs_1"],
            },
        },
    )

    result = audit.audit_payload(
        _ui_payload(fact_snapshot_id="fact_ui"),
        db_path=db_path,
        run_replay=False,
    )

    assert result["ok"] is False
    assert "ui_snapshot_fact_snapshot_id_mismatch" in result["blockers"]


def test_ui_snapshot_lineage_audit_accepts_matching_posterior_ssot_fact(tmp_path: Path) -> None:
    posterior_blocks = _posterior_ssot_blocks()
    db_path = _write_fact_db(
        tmp_path,
        {
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "fact_snapshot_id": "fact_abc",
            "posterior_run_id": "post_1",
            "lineage_state": {
                "fact_snapshot_id": "fact_abc",
                "posterior_run_id": "post_1",
                "source_event_ids": ["srcevt_1"],
                "source_observation_ids": ["srcobs_1"],
            },
            "source_observations": [{"source_event_id": "srcevt_1", "source_observation_id": "srcobs_1"}],
            **posterior_blocks,
        },
    )

    result = audit.audit_payload(
        _ui_payload(posterior_blocks=posterior_blocks),
        db_path=db_path,
        run_replay=False,
    )

    assert result["ok"] is True
    assert result["blockers"] == []


def test_ui_snapshot_lineage_audit_accepts_stale_invalidated_ssot_without_fallback_selection(tmp_path: Path) -> None:
    db_path = _write_fact_db(
        tmp_path,
        {
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "fact_snapshot_id": "fact_abc",
            "posterior_run_id": "post_1",
            "lineage_state": {
                "fact_snapshot_id": "fact_abc",
                "posterior_run_id": "post_1",
                "source_event_ids": ["srcevt_1"],
                "source_observation_ids": ["srcobs_1"],
            },
            "source_observations": [{"source_event_id": "srcevt_1", "source_observation_id": "srcobs_1"}],
            **_posterior_ssot_blocks(target_bucket="29°C or below", target_probability=0.22),
        },
    )
    stale_blocks = {
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "stale_invalidated",
            "state_reason": "posterior_target_bucket_hard_zero_by_observed_constraint",
            "fact_snapshot_id": "fact_abc",
            "posterior_run_id": "post_1",
            "invalidated_target_bucket": "29°C or below",
            "invalidated_target_probability": 0.22,
        },
        "target_bucket_source": "posterior_ssot_fact.unavailable",
        "target_bucket_na_reason": "posterior_target_bucket_hard_zero_by_observed_constraint",
    }

    result = audit.audit_payload(
        _ui_payload(posterior_blocks=stale_blocks),
        db_path=db_path,
        run_replay=False,
    )

    assert result["ok"] is True
    assert result["blockers"] == []


def test_ui_snapshot_lineage_audit_blocks_posterior_ssot_target_mismatch(tmp_path: Path) -> None:
    db_path = _write_fact_db(
        tmp_path,
        {
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "fact_snapshot_id": "fact_abc",
            "posterior_run_id": "post_1",
            "lineage_state": {
                "fact_snapshot_id": "fact_abc",
                "posterior_run_id": "post_1",
                "source_event_ids": ["srcevt_1"],
                "source_observation_ids": ["srcobs_1"],
            },
            "source_observations": [{"source_event_id": "srcevt_1", "source_observation_id": "srcobs_1"}],
            **_posterior_ssot_blocks(target_bucket="30°C", target_probability=0.7),
        },
    )

    result = audit.audit_payload(
        _ui_payload(posterior_blocks=_posterior_ssot_blocks(target_bucket="29°C", target_probability=0.6)),
        db_path=db_path,
        run_replay=False,
    )

    assert result["ok"] is False
    assert "ui_snapshot_posterior_ssot_fact_target_bucket_mismatch" in result["blockers"]
    assert "ui_snapshot_target_bucket_selection_target_probability_mismatch" in result["blockers"]


def test_ui_snapshot_lineage_audit_accepts_ranked_distribution_derived_from_ssot_rows(tmp_path: Path) -> None:
    posterior_blocks = _posterior_ssot_blocks_with_distribution()
    db_path = _write_fact_db(
        tmp_path,
        {
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "fact_snapshot_id": "fact_abc",
            "posterior_run_id": "post_1",
            "lineage_state": {
                "fact_snapshot_id": "fact_abc",
                "posterior_run_id": "post_1",
                "source_event_ids": ["srcevt_1"],
                "source_observation_ids": ["srcobs_1"],
            },
            "source_observations": [{"source_event_id": "srcevt_1", "source_observation_id": "srcobs_1"}],
            **posterior_blocks,
        },
    )

    result = audit.audit_payload(
        _ui_payload(posterior_blocks=posterior_blocks),
        db_path=db_path,
        run_replay=False,
    )

    assert result["ok"] is True
    assert result["blockers"] == []


def test_ui_snapshot_lineage_audit_blocks_ranked_distribution_probability_mismatch(tmp_path: Path) -> None:
    fact_blocks = _posterior_ssot_blocks_with_distribution()
    ui_blocks = json.loads(json.dumps(fact_blocks))
    ui_blocks["posterior_ssot_fact"]["ranked_bucket_probabilities"][1] = {
        "outcome": "41°C",
        "probability": 0.3313,
        "probability_pct": 33.13,
    }
    db_path = _write_fact_db(
        tmp_path,
        {
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "fact_snapshot_id": "fact_abc",
            "posterior_run_id": "post_1",
            "lineage_state": {
                "fact_snapshot_id": "fact_abc",
                "posterior_run_id": "post_1",
                "source_event_ids": ["srcevt_1"],
                "source_observation_ids": ["srcobs_1"],
            },
            "source_observations": [{"source_event_id": "srcevt_1", "source_observation_id": "srcobs_1"}],
            **fact_blocks,
        },
    )

    result = audit.audit_payload(
        _ui_payload(posterior_blocks=ui_blocks),
        db_path=db_path,
        run_replay=False,
    )

    assert result["ok"] is False
    assert "ui_snapshot_posterior_ssot_ranked_probability_mismatch" in result["blockers"]


def test_ui_snapshot_lineage_audit_blocks_ranked_distribution_skipping_higher_bucket(tmp_path: Path) -> None:
    fact_blocks = _posterior_ssot_blocks_with_distribution()
    ui_blocks = json.loads(json.dumps(fact_blocks))
    ui_blocks["posterior_ssot_fact"]["ranked_bucket_probabilities"][1] = {
        "outcome": "41°C",
        "probability": 0.0033,
        "probability_pct": 0.33,
    }
    db_path = _write_fact_db(
        tmp_path,
        {
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "fact_snapshot_id": "fact_abc",
            "posterior_run_id": "post_1",
            "lineage_state": {
                "fact_snapshot_id": "fact_abc",
                "posterior_run_id": "post_1",
                "source_event_ids": ["srcevt_1"],
                "source_observation_ids": ["srcobs_1"],
            },
            "source_observations": [{"source_event_id": "srcevt_1", "source_observation_id": "srcobs_1"}],
            **fact_blocks,
        },
    )

    result = audit.audit_payload(
        _ui_payload(posterior_blocks=ui_blocks),
        db_path=db_path,
        run_replay=False,
    )

    assert result["ok"] is False
    assert "ui_snapshot_posterior_ssot_ranked_skips_higher_probability_bucket" in result["blockers"]
