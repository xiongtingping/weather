"""The shadow band layer: geometry, re-set gates, and pooled fallback.

Every test here rests on the layer being SHADOW.  If any of these start
failing because a serving path grew a dependency on the module, the fix
is to remove the dependency, not to relax the test.
"""

import json
import sqlite3

import pytest

from bayes_basket_target import band_reliability_store
from bayes_basket_target import band_shadow_store
from bayes_basket_target.band_shadow_store import (
    BACKOFF_LEVELS,
    MIN_CELL_CLAIMED_MASS,
    MIN_CELL_DAYS,
    MIN_CELL_ROWS,
    POSITION_GROUPS,
    _benjamini_hochberg,
    _bootstrap_draws,
    _cell_statistics,
    _marginalise,
    _promoted_key,
    _shrunk_factor,
    collect_shadow_sample,
    ensure_shadow_schema,
    fit_band_shadow_table,
    persist_shadow_fit,
    position_group,
    refit_shadow_all_stages,
    shadow_sha256,
)
from bayes_basket_target.band_reliability_store import (
    BAND_AXIS_CENTRE_DISTANCE,
    persist_and_activate_band_table,
)


def test_position_groups_are_signed_and_cover_every_offset() -> None:
    """Cool and warm must not fold together.

    The 2026-08-28 probe measured P0's deep tail at 0.48 realised on the
    cool side against 1.47 on the warm side.  An unsigned |d| grouping
    averages those to roughly one and reports the tail as calibrated.
    """

    assert position_group(-7) == "cool_far"
    assert position_group(-2) == "cool_far"
    assert position_group(-1) == "cool_near"
    assert position_group(0) == "centre"
    assert position_group(1) == "warm_near"
    assert position_group(2) == "warm_far"
    assert position_group(9) == "warm_far"
    assert {position_group(d) for d in range(-9, 10)} == set(POSITION_GROUPS)


def _cell(by_day):
    return {
        "by_day": {day: (claimed, hits) for day, (claimed, hits) in by_day.items()},
        "rows": sum(1 for _ in by_day) * 10,
    }


def test_gate_a_mass_floor_blocks_a_cell_no_bootstrap_could_resolve() -> None:
    """A cell holding a fraction of an expected hit is not evidence.

    This is the gate the pooled rule has no analogue for: it gates the
    whole table on ten days and lets every cell inherit that, which is
    meaningless once cells are per-station.
    """

    days = [f"2026-08-{day:02d}" for day in range(1, 21)]
    draws = _bootstrap_draws(days)
    thin = _cell({day: (0.05, 0) for day in days})
    stats = _cell_statistics(thin, days=days, draws=draws)
    assert stats["claimed_mass"] < MIN_CELL_CLAIMED_MASS
    assert stats["ratio"] == 0.0
    # Twenty days and two hundred rows: the day and row floors alone would
    # have let this through.  Mass is what stops it.
    assert stats["day_count"] >= MIN_CELL_DAYS
    assert stats["row_count"] >= MIN_CELL_ROWS


def test_half_split_is_unevaluable_rather_than_free_when_halves_are_thin(
) -> None:
    """A cell too thin to disagree gets no credit for agreeing."""

    days = [f"2026-08-{day:02d}" for day in range(1, 21)]
    draws = _bootstrap_draws(days)
    # All the mass in the first half; the second half cannot take a side.
    lopsided = _cell(
        {day: ((3.0, 0) if index < 10 else (0.01, 0))[0:2]
         for index, day in enumerate(days)}
    )
    lopsided = {
        "by_day": {
            day: ((3.0, 0) if index < 10 else (0.01, 0))
            for index, day in enumerate(days)
        },
        "rows": 200,
    }
    stats = _cell_statistics(lopsided, days=days, draws=draws)
    assert stats["half_masses"][1] < band_shadow_store.MIN_HALF_MASS
    assert stats["halves_sign_stable"] is False


def test_benjamini_hochberg_controls_the_multiplicity_the_pooled_rule_lacks(
) -> None:
    """Three thousand cells at a bare 95% interval admit ~150 false ones.

    Reproduces the 2026-08-28 probe's own trap: a dozen stations showed a
    tail realising 0.00, which is arithmetic for 45 stations holding 1.7
    expected tail hits each -- not a dozen different stations.
    """

    # Forty-five nulls uniformly distributed, as a null family is.
    nulls = {f"null-{index}": (index + 0.5) / 45 for index in range(45)}
    thresholds = _benjamini_hochberg(nulls, q=0.10)
    assert all(value <= thresholds[key] for key, value in nulls.items()) is False
    passing = [key for key, value in nulls.items() if value <= thresholds[key]]
    assert len(passing) <= 5, passing

    # A genuine signal buried in the same family still comes through.
    with_signal = dict(nulls)
    with_signal["real"] = 0.0001
    thresholds = _benjamini_hochberg(with_signal, q=0.10)
    assert with_signal["real"] <= thresholds["real"]


def test_marginalise_preserves_mass_at_every_backoff_level() -> None:
    """Backoff must not invent or lose evidence, only re-key it."""

    band = (0.05, 0.10)
    finest = {
        ("ZBAA", "centre", band): _cell({"d1": (1.0, 1), "d2": (2.0, 0)}),
        ("ZBAA", "warm_far", band): _cell({"d1": (3.0, 2)}),
        ("KLGA", "centre", band): _cell({"d2": (4.0, 3)}),
    }
    total_claimed = 10.0
    total_hits = 6

    by_station = _marginalise(finest, level="station")
    assert set(by_station) == {("ZBAA", "*", band), ("KLGA", "*", band)}
    assert sum(
        claimed
        for cell in by_station.values()
        for claimed, _hits in cell["by_day"].values()
    ) == pytest.approx(total_claimed)

    by_position = _marginalise(finest, level="position")
    assert set(by_position) == {("*", "centre", band), ("*", "warm_far", band)}
    assert sum(
        hits
        for cell in by_position.values()
        for _claimed, hits in cell["by_day"].values()
    ) == total_hits
    # ZBAA's two days collapse into one keyed cell, not two rows.
    assert by_station[("ZBAA", "*", band)]["by_day"]["d1"][0] == pytest.approx(
        4.0
    )


def test_shared_bootstrap_draws_keep_cells_correlated() -> None:
    """One weather day moves many stations; the draws must reflect that.

    Independent per-cell draws would make two cells fed by the same day
    look independent, which is the failure the by-day unit exists to
    prevent in the first place.
    """

    days = [f"2026-08-{day:02d}" for day in range(1, 15)]
    first = _bootstrap_draws(days)
    second = _bootstrap_draws(days)
    assert (first == second).all()
    assert first.shape == (band_shadow_store.BOOTSTRAP_SAMPLES, len(days))
    # Every resample draws exactly as many day-slots as there are days.
    assert (first.sum(axis=1) == len(days)).all()


def test_promoted_key_maps_each_level_to_the_cell_that_earned_it() -> None:
    band = (0.10, 0.15)
    assert _promoted_key("ZBAA", "centre", band, "station_position") == (
        "ZBAA", "centre", band
    )
    assert _promoted_key("ZBAA", "centre", band, "station") == (
        "ZBAA", "*", band
    )
    assert _promoted_key("ZBAA", "centre", band, "position") == (
        "*", "centre", band
    )
    assert _promoted_key("ZBAA", "centre", band, "pooled") is None


def test_shrunk_factor_is_diagnostic_and_never_the_served_fallback() -> None:
    """Partial pooling is recorded, not served: the ruling said fallback."""

    days = ["d1"]
    draws = _bootstrap_draws(days)
    thin = _cell({"d1": (1.0, 2)})
    stats = _cell_statistics(thin, days=days, draws=draws)
    shrunk = _shrunk_factor(stats, pooled_factor=0.9)
    # Thin cell, so the shrink sits close to the pooled value...
    assert 0.9 <= shrunk < 1.4
    fat = _cell_statistics(
        _cell({"d1": (80.0, 160)}), days=days, draws=draws
    )
    # ...and a fat one is allowed to leave it, which is exactly why this
    # column is not what the cell serves.
    assert _shrunk_factor(fat, pooled_factor=0.9) > 1.5


BAND = (0.10, 0.15)


def _lane_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "CREATE TABLE calibration_station_daily_high_truth ("
        " station_id TEXT, local_date TEXT, official_tmax_c REAL,"
        " revision_no INTEGER DEFAULT 0, source_lane TEXT DEFAULT 'AWC',"
        " settlement_definition TEXT DEFAULT 'official_daily_tmax_c');"
        "CREATE TABLE basket_probability_bridge_history ("
        " station_id TEXT, event_date TEXT, history_id TEXT,"
        " envelope_json TEXT, decision_at_utc TEXT, stage TEXT,"
        " checkpoint_id TEXT DEFAULT '',"
        " envelope_content_sha256 TEXT DEFAULT '');"
        "CREATE TABLE basket_probability_bridge_bucket_probability ("
        " history_id TEXT, bucket TEXT, lower_c REAL, upper_c REAL,"
        " probability REAL);"
    )


def _write_station_day(
    conn: sqlite3.Connection,
    *,
    station: str,
    day: str,
    stage: str,
    claims: list[float],
    actual: float,
) -> None:
    """One snapshot: buckets are 1C wide starting at 20C, coldest first."""

    history_id = f"{station}-{day}-{stage}"
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth"
        "(station_id,local_date,official_tmax_c) VALUES (?,?,?)",
        (station, day, actual),
    )
    conn.execute(
        "INSERT INTO basket_probability_bridge_history"
        " (station_id, event_date, history_id, envelope_json,"
        "  decision_at_utc, stage) VALUES (?,?,?,?,?,?)",
        (station, day, history_id, json.dumps({"lineage": {}}),
         f"{day}T06:00:00Z", stage),
    )
    for index, claim in enumerate(claims):
        lower = 20.0 + index
        conn.execute(
            "INSERT INTO basket_probability_bridge_bucket_probability"
            " VALUES (?,?,?,?,?)",
            (history_id, f"b{index}", lower, lower + 1.0, claim),
        )


# Where truth lands, as an index into ``claims`` below.  Interleaved so
# the chronological halves see the same mix, and proportioned so the fit
# has both kinds of cell to decide about: the peak and the cool shoulder
# realise at exactly what they claim (those must fall back to the pool),
# while the warm shoulder realises at twice its claim (that one must earn
# its own factor).  Without a calibrated cell in the fixture a passing
# test only proves the gates promote, never that they refuse.
_OUTCOME_CYCLE = (
    3, 4, 2, 3, 5, 4, 1, 3, 6, 4, 0, 3, 2, 4, 5,
    3, 1, 4, 2, 3, 0, 4, 5, 3, 1,
)


def _synthetic_lane(
    conn: sqlite3.Connection, *, days: int = 24, stations: int = 6
) -> None:
    """Warm-shoulder buckets over-realise; the rest are honest.

    Every non-peak bucket claims the same 0.12, so a pooled band table
    cannot see the warm/cool split by construction -- only the position
    dimension can, which is the property the shadow exists to test.
    """

    _lane_tables(conn)
    claims = [0.12, 0.12, 0.12, 0.28, 0.12, 0.12, 0.12]
    for day_index in range(days):
        day = f"2026-08-{day_index + 1:02d}"
        for station_index in range(stations):
            station = f"ST{station_index:02d}"
            landing = _OUTCOME_CYCLE[
                (day_index * stations + station_index) % len(_OUTCOME_CYCLE)
            ]
            actual = 20.0 + landing + 0.5
            for stage in ("P0", "P0-M", "P1"):
                _write_station_day(
                    conn,
                    station=station,
                    day=day,
                    stage=stage,
                    claims=claims,
                    actual=actual,
                )
    # sqlite3's implicit transaction would still be open here, and the
    # store's ``BEGIN IMMEDIATE`` cannot nest inside it.
    conn.commit()


def test_thin_station_cells_fall_back_to_the_pooled_factor() -> None:
    """The ruling: a cell that cannot earn its own factor serves the pool.

    Not a shrunk factor, not identity -- the pooled one.  Identity would
    throw away the fleet-level correction the cell legitimately inherits.
    """

    conn = sqlite3.connect(":memory:")
    _synthetic_lane(conn, days=24, stations=6)
    pooled = tuple(
        (band, 0.75 if band == BAND else 1.0)
        for band in band_shadow_store.BAND_TABLE_CELLS
    )
    persist_and_activate_band_table(
        conn, stage="P0", factors=pooled, evidence={}, serving_day_count=24
    )
    fitted = fit_band_shadow_table(conn, stage="P0")
    assert fitted["cells"], "synthetic lane produced no cells"
    fallbacks = [
        cell for cell in fitted["cells"] if cell["served_by_level"] == "pooled"
    ]
    assert fallbacks, "nothing fell back; the gates are not binding"
    for cell in fallbacks:
        if cell["band"] == BAND:
            assert cell["factor"] == pytest.approx(0.75)
        else:
            assert cell["factor"] == pytest.approx(1.0)
        assert cell["basis"].startswith("pooled_fallback:")
        # A fallen-back cell must not advertise a serving measurement it
        # never made -- that conflation is what makes a table read as if
        # every station were measured individually.
        assert cell["serving_ratio"] is None


def test_station_dimension_is_starved_while_position_earns_its_keep() -> None:
    """Six stations x twenty-four days is still nothing per station cell.

    This is the whole finding in one assertion: the split the probe found
    is real and the position dimension carries it, while the station
    dimension cannot be measured at this sample no matter how real it is.
    """

    conn = sqlite3.connect(":memory:")
    _synthetic_lane(conn, days=24, stations=6)
    fitted = fit_band_shadow_table(conn, stage="P0")
    levels = fitted["evidence"]["levels"]
    assert levels["station_position"]["promoted"] == 0
    assert (
        levels["station_position"]["blocked"]["gate_a_insufficient_mass"]
        == levels["station_position"]["tested"]
    )
    assert levels["position"]["promoted"] > 0
    promoted = fitted["evidence"]["promoted"]
    assert any(key.startswith("['*', 'warm") for key in promoted), promoted
    # And the finer cells inherit it rather than measuring it themselves.
    inherited = [
        cell
        for cell in fitted["cells"]
        if cell["served_by_level"] == "position"
    ]
    assert inherited
    assert all(cell["serving_ratio"] is not None for cell in inherited)


def test_forward_window_gain_is_measured_against_the_backoff_not_identity(
) -> None:
    """A cell is credited with its increment, never the pool's correction.

    Scoring candidates against identity would hand every fine cell the
    pooled table's own gain and promote cells that add nothing.
    """

    conn = sqlite3.connect(":memory:")
    _synthetic_lane(conn, days=24, stations=6)
    strong_pool = tuple(
        (band, 1.35 if band == BAND else 1.0)
        for band in band_shadow_store.BAND_TABLE_CELLS
    )
    persist_and_activate_band_table(
        conn, stage="P0", factors=strong_pool, evidence={},
        serving_day_count=24,
    )
    with_pool = fit_band_shadow_table(conn, stage="P0")
    conn2 = sqlite3.connect(":memory:")
    _synthetic_lane(conn2, days=24, stations=6)
    without_pool = fit_band_shadow_table(conn2, stage="P0")
    # Same evidence, different base: the shadow's own gain is the
    # increment over its base, so a stronger pooled base cannot inflate it.
    assert with_pool["forward_nll_gain"] != without_pool["forward_nll_gain"]
    for fitted in (with_pool, without_pool):
        for cell in fitted["cells"]:
            gain = cell["forward_nll_gain"]
            if cell["served_by_level"] != "pooled":
                assert gain is not None and gain > 0


def test_persist_writes_cells_and_journal_without_activating_anything(
) -> None:
    """There is no activation flag to set, and that is the design."""

    conn = sqlite3.connect(":memory:")
    _synthetic_lane(conn, days=24, stations=6)
    fitted = fit_band_shadow_table(conn, stage="P0")
    outcome = persist_shadow_fit(
        conn,
        stage="P0",
        cells=fitted["cells"],
        evidence=fitted["evidence"],
        serving_day_count=fitted["serving_day_count"],
        serving_station_day_count=fitted["serving_station_day_count"],
        forward_nll_gain=fitted["forward_nll_gain"],
    )
    assert outcome["state"] == "WRITTEN"
    assert outcome["shadow_sha256"] == shadow_sha256(
        fitted["cells"], stage="P0"
    )
    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(calibration_band_shadow_cell)"
        )
    }
    assert "activated" not in columns
    assert "activated_at_utc" not in columns
    journal = conn.execute(
        "SELECT stage, cell_count, promoted_cell_count,"
        " pooled_fallback_cell_count FROM calibration_band_shadow_journal"
    ).fetchone()
    assert journal[0] == "P0"
    assert journal[1] == len(fitted["cells"])
    assert journal[2] + journal[3] == journal[1]

    # Re-persisting the same content records the fit without duplicating
    # its cells, the way the served store's UNCHANGED path does.
    again = persist_shadow_fit(
        conn,
        stage="P0",
        cells=fitted["cells"],
        evidence=fitted["evidence"],
        serving_day_count=fitted["serving_day_count"],
        serving_station_day_count=fitted["serving_station_day_count"],
        forward_nll_gain=fitted["forward_nll_gain"],
    )
    assert again["state"] == "RECORDED"
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_band_shadow_cell"
    ).fetchone()[0] == len(fitted["cells"])


def test_cells_are_pruned_to_the_retention_window_but_journal_is_kept(
    monkeypatch,
) -> None:
    """A diagnostic layer must not be what fills the SSOT.

    The store stopped the whole pipeline once by filling (2026-08-26);
    three thousand cells a day for a layer nothing serves is exactly the
    kind of tenant that should be evicted first.  The journal stays --
    it is small and it is what makes the accumulation readable.
    """

    monkeypatch.setattr(band_shadow_store, "CELL_RETENTION_FITS", 2)
    conn = sqlite3.connect(":memory:")
    _synthetic_lane(conn, days=24, stations=6)
    fitted = fit_band_shadow_table(conn, stage="P0")
    for index in range(4):
        # A different table each round, as accumulating evidence produces.
        cells = [dict(cell) for cell in fitted["cells"]]
        cells[0]["factor"] = 0.90 + index / 100.0
        persist_shadow_fit(
            conn,
            stage="P0",
            cells=cells,
            evidence={"round": index},
            serving_day_count=24,
            serving_station_day_count=144,
            forward_nll_gain=1.0,
        )
    vintages = conn.execute(
        "SELECT COUNT(DISTINCT shadow_sha256)"
        " FROM calibration_band_shadow_cell WHERE stage='P0'"
    ).fetchone()[0]
    assert vintages == 2
    journalled = conn.execute(
        "SELECT COUNT(*) FROM calibration_band_shadow_journal WHERE stage='P0'"
    ).fetchone()[0]
    assert journalled == 4


def test_pruning_one_stage_never_touches_another(monkeypatch) -> None:
    """Stages are isolated here the way they are in the served store."""

    monkeypatch.setattr(band_shadow_store, "CELL_RETENTION_FITS", 1)
    conn = sqlite3.connect(":memory:")
    _synthetic_lane(conn, days=24, stations=6)
    for stage in ("P0", "P1"):
        fitted = fit_band_shadow_table(conn, stage=stage)
        persist_shadow_fit(
            conn,
            stage=stage,
            cells=fitted["cells"],
            evidence={},
            serving_day_count=24,
            serving_station_day_count=144,
            forward_nll_gain=1.0,
        )
    # Refitting P0 twice more must not evict P1's only vintage.
    fitted = fit_band_shadow_table(conn, stage="P0")
    cells = [dict(cell) for cell in fitted["cells"]]
    cells[0]["factor"] = 0.5
    persist_shadow_fit(
        conn,
        stage="P0",
        cells=cells,
        evidence={},
        serving_day_count=24,
        serving_station_day_count=144,
        forward_nll_gain=1.0,
    )
    remaining = dict(
        conn.execute(
            "SELECT stage, COUNT(DISTINCT shadow_sha256)"
            " FROM calibration_band_shadow_cell GROUP BY stage"
        )
    )
    assert remaining["P0"] == 1
    assert remaining["P1"] == 1


def test_stage_isolation_survives_one_stage_throwing(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    _synthetic_lane(conn, days=24, stations=6)
    real = band_shadow_store.fit_band_shadow_table

    def flaky(inner, *, stage):
        if stage == "P0":
            raise sqlite3.OperationalError("database is locked")
        return real(inner, stage=stage)

    monkeypatch.setattr(band_shadow_store, "fit_band_shadow_table", flaky)
    report = refit_shadow_all_stages(conn, commit=True)
    assert "database is locked" in report["P0"]["error"]
    for stage in ("P0-M", "P1"):
        assert "error" not in report[stage], report[stage].get("error")
        assert report[stage]["commit"]["state"] == "WRITTEN"


def test_empty_lane_is_an_accumulating_state_not_an_error() -> None:
    conn = sqlite3.connect(":memory:")
    _lane_tables(conn)
    fitted = fit_band_shadow_table(conn, stage="P1")
    assert fitted["accumulating"] is True
    assert fitted["cells"] == []
    assert fitted["forward_nll_gain"] == 0.0


def test_a_distance_stage_fits_on_distance_bands_beside_a_claim_stage() -> None:
    """SUPERSEDES ``test_axis_block_is_reported_by_name_not_as_a_crash``.

    That test pinned the refusal: P0's pooled table moved to the
    centre-distance axis on 2026-09-07 and the lane stopped by name rather
    than misreading distances as claims.  Stopping was right while the lane
    could only key on claims; it is not right now that it can key on
    either, and it cost P0's candidate layer two days of evidence
    (2026-09-07..09).  The lane follows the pooled axis instead.

    Both stages are fitted in the same call on different axes, because the
    thing that would break silently is a module-level partition leaking
    from one stage to the next.
    """

    conn = sqlite3.connect(":memory:")
    _synthetic_lane(conn, days=24, stations=6)
    persist_and_activate_band_table(
        conn, stage="P0",
        factors=tuple(
            (band, 1.0)
            for band in band_reliability_store.BAND_DISTANCE_CELLS
        ),
        evidence={}, serving_day_count=24, axis=BAND_AXIS_CENTRE_DISTANCE,
    )
    persist_and_activate_band_table(
        conn, stage="P1",
        factors=tuple(
            (band, 1.0) for band in band_shadow_store.BAND_TABLE_CELLS
        ),
        evidence={}, serving_day_count=24,
    )
    fitted = fit_band_shadow_table(conn, stage="P0")
    assert fitted["evidence"].get("blocked_by") is None
    assert fitted["evidence"]["axis"] == BAND_AXIS_CENTRE_DISTANCE
    assert fitted["cells"], "a distance stage must produce cells"
    # Every band the fit emitted is a distance cell, never a claim cell.
    distance_bands = set(band_reliability_store.BAND_DISTANCE_CELLS)
    assert {tuple(cell["band"]) for cell in fitted["cells"]} <= distance_bands
    # The shape contract with the caller, key by key.
    for key in (
        "serving_day_count",
        "serving_station_day_count",
        "accumulating",
        "forward_nll_gain",
    ):
        assert key in fitted, key

    report = refit_shadow_all_stages(conn, commit=True)
    assert "error" not in report["P0"], report["P0"].get("error")
    assert report["P0"]["cell_count"] > 0
    assert report["P0"]["axis"] == BAND_AXIS_CENTRE_DISTANCE
    # The claim stage beside it is untouched, on its own partition.
    assert report["P1"].get("blocked_by") is None
    assert report["P1"]["cell_count"] > 0
    assert report["P1"]["axis"] == band_reliability_store.BAND_AXIS_CLAIM
    claim_cells = conn.execute(
        "SELECT DISTINCT band_lower FROM calibration_band_shadow_cell"
        " WHERE stage='P1'"
    ).fetchall()
    assert {row[0] for row in claim_cells} <= {
        cell[0] for cell in band_shadow_store.BAND_TABLE_CELLS
    }
    # The two fits are distinct tables, not one sha wearing two geometries:
    # (0.0, 1.0) is a legal band on both axes, so the axis has to be in the
    # identity or the retention prune would treat them as the same fit.
    assert band_shadow_store.shadow_sha256(
        fitted["cells"], stage="P0", axis=BAND_AXIS_CENTRE_DISTANCE
    ) != band_shadow_store.shadow_sha256(
        fitted["cells"], stage="P0",
    )


def test_shadow_is_not_reachable_from_any_serving_path() -> None:
    """The layer must stay unreachable from code that serves a claim.

    A shadow that a config flag can promote is not a shadow.  Reaching a
    served distribution has to be a code change somebody reviews, so the
    modules that compile and serve probabilities must not import this one.
    """

    import pathlib

    root = pathlib.Path(band_shadow_store.__file__).resolve().parent
    serving = (
        "cross_consensus.py",
        "native_ensemble_p0.py",
        "materialization.py",
        "p1_compiler.py",
        "canonical_ingress.py",
        "production_worker.py",
        "runtime.py",
        "order_submission.py",
        "policy.py",
    )
    for name in serving:
        path = root / name
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        assert "band_shadow" not in source, (
            f"{name} reached into the shadow layer; remove the dependency"
        )


def test_backoff_levels_end_at_pooled() -> None:
    """The last resort is the pool, by construction, not by convention."""

    assert BACKOFF_LEVELS[-1] == "pooled"
    assert BACKOFF_LEVELS[0] == "station_position"


def test_schema_is_idempotent_on_a_fresh_and_an_existing_database() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_shadow_schema(conn)
    ensure_shadow_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "calibration_band_shadow_cell" in tables
    assert "calibration_band_shadow_journal" in tables
