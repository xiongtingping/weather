from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts import recommended_pilot_gap_refresh_health as health


def _write_plan(path: Path, slug: str) -> None:
    path.write_text(
        json.dumps(
            {
                "queue_count": 1,
                "priority_slugs": [slug],
                "plan": [{"city": "ankara", "open_market_slugs": [slug]}],
            }
        ),
        encoding="utf-8",
    )


def _init_db(path: Path, slug: str, ts: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE event_market_ticks(slug TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE model_snapshots(slug TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE event_runs(slug TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE event_fact_history(slug TEXT, fact_as_of TEXT)")
    conn.execute("INSERT INTO event_market_ticks(slug, created_at) VALUES (?, ?)", (slug, ts))
    conn.execute("INSERT INTO model_snapshots(slug, created_at) VALUES (?, ?)", (slug, ts))
    conn.execute("INSERT INTO event_runs(slug, created_at) VALUES (?, ?)", (slug, ts))
    conn.execute("INSERT INTO event_fact_history(slug, fact_as_of) VALUES (?, ?)", (slug, ts))
    conn.commit()
    conn.close()


def test_gap_refresh_health_passes_when_priority_slug_is_fresh(tmp_path: Path) -> None:
    slug = "highest-temperature-in-ankara-on-june-1-2026"
    plan_path = tmp_path / "plan.json"
    db_path = tmp_path / "archive.db"
    now = datetime.now(timezone.utc).isoformat()
    _write_plan(plan_path, slug)
    _init_db(db_path, slug, now)

    report = health.build_report(
        db_path,
        plan_path=plan_path,
        max_tick_age_sec=900,
        max_model_age_sec=900,
        max_fact_age_sec=900,
    )

    assert report["ok"] is True
    assert report["priority_slug_count"] == 1
    assert report["rows"][0]["next_action"] == "priority_gap_slug_refresh_ok"


def test_gap_refresh_health_blocks_missing_tick(tmp_path: Path) -> None:
    slug = "highest-temperature-in-ankara-on-june-2-2026"
    plan_path = tmp_path / "plan.json"
    db_path = tmp_path / "archive.db"
    _write_plan(plan_path, slug)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE event_market_ticks(slug TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE model_snapshots(slug TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE event_runs(slug TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE event_fact_history(slug TEXT, fact_as_of TEXT)")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO model_snapshots(slug, created_at) VALUES (?, ?)", (slug, now))
    conn.execute("INSERT INTO event_fact_history(slug, fact_as_of) VALUES (?, ?)", (slug, now))
    conn.commit()
    conn.close()

    report = health.build_report(db_path, plan_path=plan_path)

    assert report["ok"] is False
    assert f"{slug}:tick_missing" in report["blockers"]
    assert report["rows"][0]["next_action"] == "refresh_market_ticks_for_priority_gap_slug"


def test_gap_refresh_health_blocks_empty_priority_slugs_when_queue_open(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    db_path = tmp_path / "archive.db"
    plan_path.write_text(json.dumps({"queue_count": 1, "priority_slugs": []}), encoding="utf-8")

    report = health.build_report(db_path, plan_path=plan_path)

    assert report["ok"] is False
    assert "recommended_pilot_gap_priority_slugs_missing" in report["blockers"]


def test_gap_refresh_health_checks_only_topn_priority_slugs(tmp_path: Path) -> None:
    fresh_slug = "highest-temperature-in-ankara-on-june-1-2026"
    stale_slug = "highest-temperature-in-atlanta-on-june-1-2026"
    plan_path = tmp_path / "plan.json"
    db_path = tmp_path / "archive.db"
    plan_path.write_text(
        json.dumps(
            {
                "queue_count": 2,
                "priority_slugs": [fresh_slug, stale_slug],
                "plan": [
                    {"city": "ankara", "open_market_slugs": [fresh_slug]},
                    {"city": "atlanta", "open_market_slugs": [stale_slug]},
                ],
            }
        ),
        encoding="utf-8",
    )
    _init_db(db_path, fresh_slug, datetime.now(timezone.utc).isoformat())

    report = health.build_report(db_path, plan_path=plan_path, topn=1)

    assert report["ok"] is True
    assert report["priority_slug_count"] == 1
    assert report["total_priority_slug_count"] == 2
    assert report["checked_topn"] == 1
    assert [row["slug"] for row in report["rows"]] == [fresh_slug]
