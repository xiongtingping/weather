import sqlite3
from pathlib import Path

from scripts import backfill_chengdu_model_history_cache as hist


def test_candidate_rows_skip_existing_without_force() -> None:
    rows, skipped = hist._candidate_rows(
        loc_key="30.5583,103.9460",
        values_by_model={
            "gfs_seamless": {
                "2026-05-30": 26.5,
                "2026-05-31": 27.5,
            }
        },
        existing={("gfs_seamless", "2026-05-31")},
        force=False,
    )

    assert [row["event_date"] for row in rows] == ["2026-05-30"]
    assert rows[0]["official_updated_source"] == "open_meteo_historical_forecast"
    assert rows[0]["members_json"] == "[26.5]"
    assert skipped == [
        {"model": "gfs_seamless", "event_date": "2026-05-31", "reason": "already_cached"}
    ]


def test_resolve_window_uses_available_wu_history(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE event_daily_temps (
                event_date TEXT,
                station_id TEXT,
                market_var TEXT,
                market_unit TEXT,
                settlement_actual REAL,
                resolution_source TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO event_daily_temps(
                event_date, station_id, market_var, market_unit, settlement_actual, resolution_source
            ) VALUES (?, 'ZUUU', 'temperature_2m_max', 'C', ?, ?)
            """,
            [
                ("2026-05-01", 20.0, "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU"),
                ("2026-05-03", 22.0, "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU"),
            ],
        )
        conn.commit()
        args = hist.parse_args([])
        args.station_id = "ZUUU"
        args.start_date = ""
        args.end_date = ""
        args.days = 0

        start_date, end_date, policy = hist._resolve_window(conn, args)
    finally:
        conn.close()

    assert (start_date, end_date, policy) == (
        "2026-05-01",
        "2026-05-03",
        "available_wu_history",
    )


def test_runner_applies_model_history_by_default() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "backfill_chengdu_wu_settlement_history_runner.sh"
    )
    text = script.read_text(encoding="utf-8")

    assert 'PM_CHENGDU_MODEL_HISTORY_APPLY:-1' in text
    assert "APPLY_CHENGDU_MODEL_HISTORY" in text
    assert 'CHENGDU_WU_CITY="chengdu"' in text
    assert 'CHENGDU_WU_STATION_ID="ZUUU"' in text
    assert 'CHENGDU_MODEL_HISTORY_LOC_KEY="30.5583,103.9460"' in text
    assert 'export PM_ARCHIVE_WRITE_SCOPE="chengdu_zuuu"' in text
    assert '"--days" "${PM_CHENGDU_WU_BACKFILL_DAYS:-0}"' in text
    assert '"--city" "${CHENGDU_WU_CITY}"' in text
    assert '"--station-id" "${CHENGDU_WU_STATION_ID}"' in text
    assert '"--loc-key" "${CHENGDU_MODEL_HISTORY_LOC_KEY}"' in text
