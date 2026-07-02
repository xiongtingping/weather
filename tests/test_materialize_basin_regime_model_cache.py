import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import materialize_basin_regime_model_cache as materializer


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            fact_json TEXT
        );
        CREATE TABLE event_fact_latest (
            slug TEXT,
            event_date TEXT,
            fact_id INTEGER,
            updated_at TEXT
        );
        CREATE TABLE model_member_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT,
            loc_key TEXT,
            event_date TEXT,
            market_var TEXT,
            market_unit TEXT,
            members_json TEXT,
            created_at TEXT,
            official_updated_source TEXT,
            basin_regime TEXT,
            basin_regime_features_json TEXT,
            basin_regime_source TEXT,
            basin_regime_updated_at TEXT,
            basin_regime_confidence REAL
        );
        """
    )
    fact = {
        "slug": "highest-temperature-in-chengdu-on-july-2-2026",
        "station_id": "ZUUU",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "climo_regime_features": {
            "primary_regime_label": "cloud_limited_proxy",
            "basin_ventilation_regime": "moderate_ventilation",
            "basin_heat_mechanism_state": "off",
            "regime_confidence": 0.8,
            "source": "unit_test_climo",
            "updated_at": "2026-07-01T00:00:00+00:00",
        },
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(id, slug, event_date, fact_as_of, fact_json)
        VALUES(1, ?, '2026-07-02', '2026-07-01T00:00:00+00:00', ?)
        """,
        (fact["slug"], json.dumps(fact)),
    )
    conn.execute(
        """
        INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES(?, '2026-07-02', 1, '2026-07-01T00:00:00+00:00')
        """,
        (fact["slug"],),
    )
    for source in ("observed", "open_meteo_historical_forecast"):
        conn.execute(
            """
            INSERT INTO model_member_cache(
                model, loc_key, event_date, market_var, market_unit, members_json,
                created_at, official_updated_source
            )
            VALUES('gfs_seamless', ?, '2026-07-02', 'temperature_2m_max', 'C',
                   '[30.0,31.0]', '2026-07-01T00:00:00+00:00', ?)
            """,
            (materializer.DEFAULT_LOC_KEY, source),
        )
    conn.commit()
    return conn


def test_materializer_dry_run_does_not_write(tmp_path):
    conn = _make_db(tmp_path / "archive.db")
    try:
        result = materializer.materialize(
            conn,
            start_date="2026-07-01",
            end_date="",
            loc_key=materializer.DEFAULT_LOC_KEY,
            station_id="ZUUU",
            market_var="temperature_2m_max",
            market_unit="C",
            apply=False,
        )
        rows = conn.execute(
            "SELECT basin_regime FROM model_member_cache ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert result["candidates"] == 1
    assert result["rows_changed"] == 0
    assert [row["basin_regime"] for row in rows] == [None, None]


def test_materializer_apply_merge_fills_observed_only(tmp_path):
    conn = _make_db(tmp_path / "archive.db")
    try:
        result = materializer.materialize(
            conn,
            start_date="2026-07-01",
            end_date="",
            loc_key=materializer.DEFAULT_LOC_KEY,
            station_id="ZUUU",
            market_var="temperature_2m_max",
            market_unit="C",
            apply=True,
        )
        rows = conn.execute(
            """
            SELECT official_updated_source, basin_regime, basin_regime_source
            FROM model_member_cache
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()

    assert result["rows_changed"] == 1
    assert rows[0]["official_updated_source"] == "observed"
    assert rows[0]["basin_regime"] == "cloud_limited_or_rain_cooled"
    assert rows[0]["basin_regime_source"] == "unit_test_climo"
    assert rows[1]["official_updated_source"] == "open_meteo_historical_forecast"
    assert rows[1]["basin_regime"] is None
