"""The TAF likelihood fit reads a frozen day's envelope rows from its case cache.

User ruling 2026-10-01 「改」: the fit needs a handful of numbers per settled
station-day and used to re-derive every one of them each night from the
P0/P0-M envelopes since 2026-08-16, which kept those envelopes in the SSOT.
A day FREEZE_LAG_DAYS past is copied once; the case selection is unchanged.
Measured on the live store the same day: 672 cases and 3,057 tempering cases
identical either way, and the written artifact byte-for-byte the same.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bayes_basket_target import ssot
from tests.test_charter_150_p1_joint_likelihood import _p0m, _root_with_centre, _taf_artifact
from tests.test_taf_likelihood_review_20260919 import _ddl, _fitter, _insert_envelope, _insert_truth

NOW = "2026-10-01T00:00:00Z"


def _store(tmp_path: Path):
    root = _root_with_centre()
    applied = _p0m(root, _taf_artifact(), tx=39.0)
    conn = sqlite3.connect(tmp_path / "ssot.db")
    conn.execute(_ddl(ssot.PRODUCTION_TARGET_DDL, "basket_probability_envelope"))
    conn.execute(_ddl(ssot.DDL, "calibration_station_daily_high_truth"))
    _insert_envelope(conn, root)
    _insert_envelope(conn, applied)
    _insert_truth(conn, root.station_id, root.event_date, 39.0, 0, "2026-07-23T17:00:00Z")
    conn.commit()
    return conn, root, applied


def test_a_frozen_day_reads_the_same_from_the_cache(tmp_path: Path) -> None:
    fitter = _fitter()
    conn, root, _applied = _store(tmp_path)
    day = root.event_date
    cache = fitter.open_case_cache(tmp_path / "cases.db")
    receipt = fitter.refresh_case_cache(conn, cache, since=day, frozen_through=day, now_utc=NOW)
    assert receipt == {"frozen": 1, "refrozen": 0, "kept": 0}
    ssot_source, cached = fitter.SsotSource(conn), fitter.CachedSource(conn, cache)
    assert cached.p0m_rows(day, day) == ssot_source.p0m_rows(day, day)
    assert cached.p0_roots(root.station_id, day) == ssot_source.p0_roots(root.station_id, day)
    assert cached.root(root.checkpoint_id) == ssot_source.root(root.checkpoint_id)
    assert fitter.load_cases(conn, since=day, through=day, source=cached) == fitter.load_cases(
        conn, since=day, through=day,
    )


def test_a_frozen_day_survives_its_envelopes_leaving_the_ssot(tmp_path: Path) -> None:
    fitter = _fitter()
    conn, root, _applied = _store(tmp_path)
    day = root.event_date
    before = fitter.load_cases(conn, since=day, through=day)
    cache = fitter.open_case_cache(tmp_path / "cases.db")
    fitter.refresh_case_cache(conn, cache, since=day, frozen_through=day, now_utc=NOW)
    conn.execute("DELETE FROM basket_probability_envelope")   # the cold tier took them
    conn.commit()
    assert fitter.load_cases(conn, since=day, through=day) == []
    assert fitter.refresh_case_cache(conn, cache, since=day, frozen_through=day, now_utc=NOW)["kept"] == 1
    assert fitter.load_cases(conn, since=day, through=day, source=fitter.CachedSource(conn, cache)) == before


def test_a_day_the_ssot_added_to_after_its_freeze_is_frozen_again(tmp_path: Path) -> None:
    fitter = _fitter()
    conn, root, _applied = _store(tmp_path)
    day = root.event_date
    cache = fitter.open_case_cache(tmp_path / "cases.db")
    fitter.refresh_case_cache(conn, cache, since=day, frozen_through=day, now_utc=NOW)
    # A P0-M the freeze never saw, under another checkpoint.
    conn.execute(
        "INSERT INTO basket_probability_envelope SELECT 'env-late', stage, 'late-cp', station_id,"
        " event_date, settlement_timezone, decision_at_utc, fresh_until_utc, bucket_ordinals_json,"
        " probabilities_json, posterior_version, posterior_sha256, bridge_sha256, lineage_json,"
        " content_sha256, state, created_at FROM basket_probability_envelope WHERE stage='P0-M'"
    )
    conn.commit()
    receipt = fitter.refresh_case_cache(conn, cache, since=day, frozen_through=day, now_utc=NOW)
    assert receipt["refrozen"] == 1
    cached = fitter.CachedSource(conn, cache)
    assert sorted(cached.p0m_rows(day, day)) == sorted(fitter.SsotSource(conn).p0m_rows(day, day))


def test_days_inside_the_lag_are_read_off_the_ssot(tmp_path: Path) -> None:
    fitter = _fitter()
    conn, root, _applied = _store(tmp_path)
    day = root.event_date
    cache = fitter.open_case_cache(tmp_path / "cases.db")
    receipt = fitter.refresh_case_cache(conn, cache, since=day, frozen_through="2026-07-01", now_utc=NOW)
    assert receipt == {"frozen": 0, "refrozen": 0, "kept": 0}
    cached = fitter.CachedSource(conn, cache)
    assert day not in cached.frozen
    assert cached.p0m_rows(day, day) == fitter.SsotSource(conn).p0m_rows(day, day)


def test_a_root_the_ssot_did_not_have_stays_absent(tmp_path: Path) -> None:
    fitter = _fitter()
    conn, root, _applied = _store(tmp_path)
    day = root.event_date
    conn.execute(
        "UPDATE basket_probability_envelope SET lineage_json=json_set(lineage_json,"
        " '$.p0_root_checkpoint_id', 'missing-root') WHERE stage='P0-M'"
    )
    conn.commit()
    cache = fitter.open_case_cache(tmp_path / "cases.db")
    fitter.refresh_case_cache(conn, cache, since=day, frozen_through=day, now_utc=NOW)
    assert fitter.CachedSource(conn, cache).root("missing-root") is None
    assert fitter.load_cases(conn, since=day, through=day, source=fitter.CachedSource(conn, cache)) == []


def test_the_nightly_sweep_runs_the_fit_with_its_cache() -> None:
    root = Path(__file__).resolve().parents[1]
    sweep = (root / "scripts" / "p1_likelihood_refit_sweep.sh").read_text(encoding="utf-8")
    assert '"$PY" scripts/fit_taf_likelihood.py --through "$THROUGH"' in sweep
    assert "--case-cache" not in sweep    # the default path: the cache is on
    fitter = _fitter()
    assert fitter.CASE_CACHE_PATH.parent.name == "bayes_basket_target_nwp"
