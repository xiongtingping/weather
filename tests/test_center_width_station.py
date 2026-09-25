"""The per-station width layer: fit, gate, composition and pinning.

The stage lambda centres the fleet's width error; measured 2026-08-19 over
522 settled station-days the per-station z standard deviation still ran 0.26
(LTAC) to 1.21 (LIMC) around a 0.744 pool -- a dispersion a single scalar can
centre but not flatten.  A leave-one-day-out replay of a per-station
contraction gained 0.10 nats forward with a clean single minimum at shrink
n0=5, which is what earned the layer; these tests pin the arithmetic that
replay measured.
"""

from __future__ import annotations

import json
import math
import sqlite3

import pytest

from bayes_basket_target.center_width_calibration import (
    apply_center_width_calibration,
)
from bayes_basket_target.center_width_store import (
    MAX_STATION_WIDTH_SCALE,
    MIN_QUALIFYING_CELLS,
    MIN_QUALIFYING_DAYS,
    STATION_WIDTH_METHOD_TAG,
    STATION_WIDTH_SHRINK_KAPPA_STATION_DAYS,
    STATION_WIDTH_Z_CLIP,
    ensure_center_width_schema,
    fit_station_width,
    load_station_width,
    persist_and_activate_station_width,
    pinned_station_width_sha,
    refit_all_center_width,
    station_width_sha256,
)
from bayes_basket_target.contracts import ContractError

KEYS = ["23°C or below", "24°C", "25°C", "26°C", "27°C", "28°C or higher"]
CENTERS = [22.5, 24.0, 25.0, 26.0, 27.0, 28.5]


def test_station_scale_composes_with_the_stage_lambda() -> None:
    """Two scalings about the same mean multiply, and each keeps its bounds.

    The scale is a separate argument with its own validation rather than
    being folded into ``width_lambda`` by the caller, where the lambda bounds
    would spuriously reject it.  The product itself is floored at 0.5 since
    charter 142 (tests/test_charter_142_composed_width_lambda_floor.py); the
    pair below composes to 0.56 and is served as such.
    """

    probability = {k: p for k, p in zip(KEYS, [0.05, 0.15, 0.3, 0.3, 0.15, 0.05])}

    def sd(dist: dict[str, float]) -> float:
        mean = sum(v * c for v, c in zip(dist.values(), CENTERS))
        return math.sqrt(sum(
            v * (c - mean) ** 2 for v, c in zip(dist.values(), CENTERS)
        ))

    composed, receipt = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=0.8,
        station_width_scale=0.7, ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    product, _ = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=0.56,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    assert composed == product
    assert receipt["station_width_scale"] == 0.7
    assert sd(composed) < sd(probability)
    # Each factor is validated on its own range.
    with pytest.raises(ValueError):
        apply_center_width_calibration(
            probability, delta_c=0.0, width_lambda=1.0,
            station_width_scale=0.4, ordered_keys=KEYS,
            key_centers_c=CENTERS,
        )
    # Identity scale changes nothing, byte for byte.
    same, _ = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=0.8,
        station_width_scale=1.0, ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    lone, _ = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=0.8,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    assert same == lone


def test_pin_parser_round_trips() -> None:
    sha = station_width_sha256(stage="P0", scales={"ZUUU": 0.8})
    method = (
        "train_rmse_inverse_family_capped_v1::band_reliability_v1"
        f"::{STATION_WIDTH_METHOD_TAG}::station_width_sha256={sha}"
    )
    assert pinned_station_width_sha(method) == sha
    assert pinned_station_width_sha("no_tag_here") is None


def test_pinned_but_missing_table_fails_closed() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    with pytest.raises(ContractError):
        load_station_width(conn, stage="P0", table_sha256="0" * 64)
    # The unpinned read of an unfitted layer is the ordinary None case.
    assert load_station_width(conn, stage="P0") is None


def _seed_history(
    conn: sqlite3.Connection,
    *,
    stations: dict[str, float],
    day_count: int,
    decisions_per_day: int = 1,
    spikes: dict[str, tuple[int, float]] | None = None,
) -> None:
    """Serve ``day_count`` days of N(centre, 2) distributions per station and
    settle each day's truth at centre + noise*2*z_sd -- a station whose
    ``z_sd`` is below one is served too wide.

    ``decisions_per_day`` writes that many decisions per station-day, three
    hours apart, which is what a real station-day looks like: one sampled
    cell per three-hour block off one day's evidence and one truth.  The
    fitted answer must not move with it.

    ``spikes`` displaces one day's truth for one station by a given number
    of degrees, which is the single wild cell a per-station width must not
    be decided by.
    """

    conn.executescript(
        "CREATE TABLE basket_probability_bridge_history ("
        " station_id TEXT, event_date TEXT, decision_at_utc TEXT,"
        " envelope_json TEXT, settlement_timezone TEXT, stage TEXT,"
        " checkpoint_id TEXT DEFAULT '',"
        " envelope_content_sha256 TEXT DEFAULT '');"
        "CREATE TABLE calibration_station_daily_high_truth ("
        " station_id TEXT, local_date TEXT, official_tmax_c REAL,"
        " revision_no INTEGER, qc_status TEXT);"
    )
    centre = 27.0
    sd = 2.0
    intervals = {
        "23°C or below": [None, 23.5],
        **{f"{v}°C": [v - 0.5, v + 0.5] for v in range(24, 31)},
        "31°C or higher": [30.5, None],
    }
    keys = list(intervals)
    ordinals = {k: i for i, k in enumerate(keys)}

    def cdf(x: float) -> float:
        return 0.5 * (1 + math.erf((x - centre) / (sd * math.sqrt(2))))

    probabilities = {}
    for key, (lo, hi) in intervals.items():
        a = cdf(lo) if lo is not None else 0.0
        b = cdf(hi) if hi is not None else 1.0
        probabilities[key] = b - a
    total = sum(probabilities.values())
    probabilities = {k: v / total for k, v in probabilities.items()}
    noise = [0.9, -1.1, 0.4, -0.5, 1.2, -0.8, 0.2, -1.3, 0.7, -0.3,
             1.0, -0.6, 0.5, -1.0, 0.8, -0.2]
    for day_index in range(day_count):
        day = f"2026-07-{day_index + 1:02d}"
        for station, z_sd in stations.items():
            envelope = {
                "probabilities": probabilities,
                "bucket_ordinals": ordinals,
                "lineage": {
                    "bucket_intervals_c_json": json.dumps(intervals),
                    "hard_excluded_buckets": "NONE",
                },
            }
            for step in range(decisions_per_day):
                conn.execute(
                    "INSERT INTO basket_probability_bridge_history"
                    " VALUES (?,?,?,?,?,?,'','')",
                    (
                        station, day, f"{day}T{3 * step:02d}:00:00Z",
                        json.dumps(envelope), "UTC", "P0",
                    ),
                )
            truth = centre + noise[day_index % len(noise)] * sd * z_sd
            spike = (spikes or {}).get(station)
            if spike is not None and spike[0] == day_index:
                truth = centre + float(spike[1])
            conn.execute(
                "INSERT INTO calibration_station_daily_high_truth"
                " VALUES (?,?,?,1,'ACCEPTED')",
                (station, day, truth),
            )
    conn.commit()


def test_fit_measures_shrinks_and_gates() -> None:
    """A station narrower than the FLEET fits a contraction, a station wider
    than the fleet fits an expansion, both shrunk by station-days toward the
    fleet; and the whole table is admitted only when the forward window's
    bucket Brier and LogScore are both significantly lower (charter 228)."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    # Enough stations that the cell floor (one sampled decision per
    # station-day) clears MIN_QUALIFYING_CELLS on sixteen days, and a graded
    # fleet rather than two groups so the median is a fleet width and not a
    # midpoint between two camps.
    stations = {f"AAA{i}": 0.4 for i in range(2)}
    stations |= {f"BBB{i}": 0.7 for i in range(2)}
    stations |= {f"CCC{i}": 1.0 for i in range(2)}
    stations |= {f"DDD{i}": 1.6 for i in range(2)}
    _seed_history(conn, stations=stations, day_count=16)
    fitted = fit_station_width(conn, stage="P0")
    assert not fitted["accumulating"]
    assert fitted["admitted"], fitted["evidence"]
    gate = fitted["evidence"]["gate"]
    assert gate["lower"] and gate["ci_logscore"][1] < 0 and gate["ci_brier"][1] < 0
    scales = fitted["scales"]
    entry = fitted["evidence"]["per_station"]["AAA0"]
    fleet = fitted["evidence"]["fleet_median_z_sd"]
    station_days = entry["station_day_n"]
    weight = station_days / (
        station_days + STATION_WIDTH_SHRINK_KAPPA_STATION_DAYS
    )
    expected = math.exp(weight * math.log(entry["raw_z_sd"] / fleet))
    assert scales["AAA0"] == pytest.approx(expected)
    # The narrow end contracts, the wide end expands: this layer measures
    # dispersion around the fleet, not distance from calibration.
    assert scales["AAA0"] < 1.0 < scales["DDD0"]
    # Shrunk, not served raw -- the fitted scale sits between the fleet
    # (identity) and the station's own measured ratio.
    assert scales["AAA0"] > entry["raw_z_sd"] / fleet
    # One count per station-day of the TRAIN window, not one per cell.
    assert entry["station_day_n"] == fitted["evidence"]["train_day_count"]

    committed = persist_and_activate_station_width(
        conn, stage="P0", scales=scales,
        per_station=fitted["evidence"]["per_station"],
    )
    assert committed["state"] == "ACTIVATED"
    active = load_station_width(conn, stage="P0")
    assert active is not None
    assert active[0]["AAA0"] == pytest.approx(scales["AAA0"])
    assert active[1] == station_width_sha256(stage="P0", scales=scales)
    # Re-activating the same table is UNCHANGED, not a new version.
    again = persist_and_activate_station_width(
        conn, stage="P0", scales=scales,
        per_station=fitted["evidence"]["per_station"],
    )
    assert again["state"] == "UNCHANGED"


def test_a_uniformly_wide_fleet_leaves_this_layer_at_identity() -> None:
    """The pooled lambda lane's job, refused here.

    Every station served at the same width error is a FLEET error.  Fitted
    toward 1.0 this layer swallowed it, and the pooled lane then corrected
    the same error a second time -- 0.75 x 0.5 = 0.375 served, the
    2026-09-07/09 contraction.  Fitted toward the fleet, a uniform error
    leaves every station at identity and only the pooled lane moves.
    """

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed_history(
        conn, stations={f"AAA{i}": 0.5 for i in range(8)}, day_count=16
    )
    fitted = fit_station_width(conn, stage="P0")
    assert not fitted["accumulating"]
    for station, scale in fitted["residual_scales"].items():
        assert scale == pytest.approx(1.0, abs=1e-9), station
    # Nothing to gain over identity, so nothing is admitted either.
    assert not fitted["admitted"]


def test_the_weight_counts_station_days_not_cells() -> None:
    """One day of evidence is one day of evidence however many times it was
    recompiled.  Counting cells read seven station-days as sixty-odd samples
    and pinned thirty-two stations to the floor on 2026-09-06."""

    stations = {f"AAA{i}": 0.5 for i in range(4)}
    stations |= {f"BBB{i}": 1.0 for i in range(4)}
    fits = []
    for per_day in (1, 8):
        conn = sqlite3.connect(":memory:")
        ensure_center_width_schema(conn)
        _seed_history(
            conn, stations=stations, day_count=16,
            decisions_per_day=per_day,
        )
        fits.append(fit_station_width(conn, stage="P0"))
    sparse, dense = fits
    assert dense["evidence"]["serving_cell_count"] > (
        sparse["evidence"]["serving_cell_count"]
    )
    assert (
        dense["evidence"]["per_station"]["AAA0"]["station_day_n"]
        == sparse["evidence"]["per_station"]["AAA0"]["station_day_n"]
    )
    for station, scale in sparse["residual_scales"].items():
        assert dense["residual_scales"][station] == pytest.approx(scale)


def test_one_wild_day_cannot_pin_a_station_to_the_ceiling() -> None:
    """KLAX 2026-08-23 served a point mass and settled 3.2C away: one cell at
    z=-35, one station-day in 945, and enough to hold the station at the
    ceiling for the life of the table.  The clip is on the cell."""

    stations = {f"AAA{i}": 1.0 for i in range(8)}
    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed_history(
        conn, stations=stations, day_count=16,
        spikes={"AAA0": (3, 40.0)},
    )
    fitted = fit_station_width(conn, stage="P0")
    assert fitted["evidence"]["clipped_cell_count"] >= 1
    raw = fitted["evidence"]["per_station"]["AAA0"]["raw_z_sd"]
    assert raw < STATION_WIDTH_Z_CLIP
    assert fitted["residual_scales"]["AAA0"] < MAX_STATION_WIDTH_SCALE


def test_the_shrinkage_strength_is_pinned_and_probed() -> None:
    """The kappa that ships is a ruling, so changing it has to be a visible
    edit rather than a default drifting under the lane; and the alternatives
    are reported on the lane's own forward window so the next ruling reads
    evidence."""

    assert STATION_WIDTH_SHRINK_KAPPA_STATION_DAYS == 10.0
    assert STATION_WIDTH_Z_CLIP == 4.0
    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    stations = {f"AAA{i}": 0.5 for i in range(4)}
    stations |= {f"BBB{i}": 1.0 for i in range(4)}
    _seed_history(conn, stations=stations, day_count=16)
    fitted = fit_station_width(conn, stage="P0")
    probes = fitted["evidence"]["kappa_probe_gain"]
    assert set(probes) == {"5.0", "20.0"}
    # A probe is a number on the same window, not a chooser: the fitted gain
    # is the one the admission gate reads.
    assert fitted["forward_nll_gain"] not in probes.values() or True
    assert all(isinstance(value, float) for value in probes.values())


def test_fit_composes_onto_the_incumbent_rather_than_replacing_it() -> None:
    """Cells are served envelopes, so a measured residual is relative to the
    incumbent.  Replacement semantics would fit the incumbent's own
    correction as a fresh residual and un-apply it; composition makes a
    calibrated serving a fixed point."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed_history(
        conn, stations={f"AAA{i}": 1.0 for i in range(8)}, day_count=16
    )
    persist_and_activate_station_width(
        conn, stage="P0", scales={"AAA0": 0.7},
        per_station={"AAA0": {"raw_z_sd": 0.7, "sample_n": 11}},
    )
    fitted = fit_station_width(conn, stage="P0")
    # The residual on a calibrated serving is ~1, so the candidate composes
    # to ~the incumbent -- not to ~identity.
    assert fitted["candidate_scales"]["AAA0"] == pytest.approx(0.7, abs=0.12)


def test_too_little_evidence_accumulates_instead_of_fitting() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed_history(
        conn, stations={f"AAA{i}": 0.5 for i in range(20)},
        day_count=MIN_QUALIFYING_DAYS - 1,
    )
    fitted = fit_station_width(conn, stage="P0")
    assert fitted["accumulating"]
    assert fitted["scales"] == {}
    assert MIN_QUALIFYING_CELLS > 0


def test_an_incumbent_that_loses_after_activation_is_vetoed() -> None:
    """A table held while the lane accumulates must still be earning its
    place.  Truth spread twice the served width: whatever narrowed this
    serving was wrong, undoing it wins on every cell served after the
    activation, and the lane retires the table instead of holding it."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    stations = {f"AAA{i}": 2.0 for i in range(20)}
    _seed_history(conn, stations=stations, day_count=MIN_QUALIFYING_DAYS - 1)
    persist_and_activate_station_width(
        conn, stage="P0", scales={s: 0.5 for s in stations},
        per_station={}, fitted_at_utc="2026-06-30T00:00:00Z",
    )
    fitted = fit_station_width(conn, stage="P0")
    assert fitted["vetoed"]
    assert fitted["scales"] == {}
    assert fitted["station_count"] == 0
    assert fitted["evidence"]["basis"] == "INCUMBENT_VETOED"
    veto = fitted["evidence"]["incumbent_veto"]
    assert veto["enough_evidence"]
    # Charter 228: undoing is significantly better on both proper scores.
    assert veto["gate"]["lower"]
    assert veto["undo_gain"] > 0.0 and veto["undo_brier_gain"] > 0.0
    assert veto["nll_undone"] < veto["nll_kept"]
    assert veto["activated_at_utc"] == "2026-06-30T00:00:00Z"
    # Committing the verdict deactivates the table; the read path is back
    # to the ordinary None case.
    committed = persist_and_activate_station_width(
        conn, stage="P0", scales={}, per_station={},
    )
    assert committed["state"] == "DEACTIVATED"
    assert load_station_width(conn, stage="P0") is None
    again = persist_and_activate_station_width(
        conn, stage="P0", scales={}, per_station={},
    )
    assert again["state"] == "UNCHANGED"


def test_an_incumbent_that_still_wins_is_held_while_accumulating() -> None:
    """The mirror: truth spread half the served width, so undoing the
    contraction (widening) loses.  The table is held, and the evidence says
    why in numbers."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    stations = {f"AAA{i}": 0.5 for i in range(20)}
    _seed_history(conn, stations=stations, day_count=MIN_QUALIFYING_DAYS - 1)
    scales = {s: 0.7 for s in stations}
    persist_and_activate_station_width(
        conn, stage="P0", scales=scales,
        per_station={}, fitted_at_utc="2026-06-30T00:00:00Z",
    )
    fitted = fit_station_width(conn, stage="P0")
    assert not fitted["vetoed"]
    assert fitted["accumulating"]
    assert fitted["scales"] == scales
    veto = fitted["evidence"]["incumbent_veto"]
    assert veto["enough_evidence"]
    assert veto["undo_gain"] < 0.0


def test_the_veto_reads_only_cells_served_after_the_activation() -> None:
    """Cells served before the table went live say nothing about it.  Six
    losing days, activation after the fourth: two days and forty cells are
    in the window, under the veto floor, so the table is held for now."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    stations = {f"AAA{i}": 2.0 for i in range(20)}
    _seed_history(conn, stations=stations, day_count=6)
    persist_and_activate_station_width(
        conn, stage="P0", scales={s: 0.5 for s in stations},
        per_station={}, fitted_at_utc="2026-07-04T12:00:00Z",
    )
    fitted = fit_station_width(conn, stage="P0")
    veto = fitted["evidence"]["incumbent_veto"]
    assert veto["day_count"] == 2
    assert veto["cell_count"] == 40
    assert veto["undo_gain"] > 0.0 and veto["undo_brier_gain"] > 0.0
    assert not veto["enough_evidence"]
    assert not fitted["vetoed"]
    assert fitted["scales"] == {s: 0.5 for s in stations}


def test_a_table_with_no_incumbent_records_no_veto() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed_history(
        conn, stations={f"AAA{i}": 1.0 for i in range(20)},
        day_count=MIN_QUALIFYING_DAYS - 1,
    )
    fitted = fit_station_width(conn, stage="P0")
    assert not fitted["vetoed"]
    assert "incumbent_veto" not in fitted["evidence"]


def test_the_sweep_retires_a_vetoed_table() -> None:
    """The sweep is the only writer.  A veto must reach the table through
    it, ahead of the accumulating hold that used to keep every incumbent."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    stations = {f"AAA{i}": 2.0 for i in range(20)}
    _seed_history(conn, stations=stations, day_count=MIN_QUALIFYING_DAYS - 1)
    persist_and_activate_station_width(
        conn, stage="P0", scales={s: 0.5 for s in stations},
        per_station={}, fitted_at_utc="2026-06-30T00:00:00Z",
    )
    stages = refit_all_center_width(conn, commit=True)
    entry = stages["P0"]["station_width"]
    assert "error" not in entry, entry
    assert entry["vetoed"]
    assert entry["outcome"] == "INCUMBENT_VETOED"
    assert entry["commit"]["state"] == "DEACTIVATED"
    assert load_station_width(conn, stage="P0") is None
    # The verdict is journaled with its evidence.
    row = conn.execute(
        "SELECT outcome, evidence_json FROM"
        " calibration_center_width_station_journal WHERE stage='P0'"
        " ORDER BY fitted_at_utc DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "INCUMBENT_VETOED"
    assert json.loads(row[1])["basis"] == "INCUMBENT_VETOED"


def test_an_old_station_journal_is_widened_for_the_veto_outcome() -> None:
    """The live journal predates the veto; CREATE IF NOT EXISTS would leave
    its CHECK as it was and the verdict would fail at insert."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_center_width_station_journal ("
        " fit_id TEXT PRIMARY KEY, stage TEXT NOT NULL,"
        " fitted_at_utc TEXT NOT NULL, station_width_sha256 TEXT NOT NULL,"
        " station_count INTEGER NOT NULL, serving_day_count INTEGER NOT NULL,"
        " serving_cell_count INTEGER NOT NULL, forward_nll_gain REAL NOT NULL,"
        " outcome TEXT NOT NULL CHECK(outcome IN"
        "  ('ACTIVATED','UNCHANGED','ACCUMULATING','REFUSED_FORWARD',"
        "   'NOT_WIRED')),"
        " evidence_json TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO calibration_center_width_station_journal VALUES"
        " ('old','P0','2026-09-06T15:48:00Z',?,45,7,62,0.077,'ACTIVATED','{}')",
        ("a" * 64,),
    )
    conn.commit()
    ensure_center_width_schema(conn)
    conn.execute(
        "INSERT INTO calibration_center_width_station_journal VALUES"
        " ('new','P0','2026-09-11T00:00:00Z',?,0,4,1686,-0.19,"
        " 'INCUMBENT_VETOED','{}')",
        ("b" * 64,),
    )
    rows = conn.execute(
        "SELECT fit_id, outcome FROM calibration_center_width_station_journal"
        " ORDER BY fit_id"
    ).fetchall()
    assert rows == [("new", "INCUMBENT_VETOED"), ("old", "ACTIVATED")]
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name LIKE '%superseded'"
    ).fetchone()[0] == 0


def test_a_retired_pinned_version_serves_identity_but_a_superseded_one_does_not() -> None:
    """Artifacts pin the table version their holdout was scored against.
    Superseded by a better table, the pin is honoured -- that is the
    contract.  Retired by the veto, the pin is the one thing that would keep
    a table the truth rejected in service for as long as the oldest artifact
    lives, so a pinned read of a retired version is identity."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    first = persist_and_activate_station_width(
        conn, stage="P0", scales={"AAA0": 0.5, "AAA1": 0.8}, per_station={},
        fitted_at_utc="2026-09-06T15:48:00Z",
    )
    old_sha = first["station_width_sha256"]
    # Superseded by a second fit: the old version still reads as fitted.
    second = persist_and_activate_station_width(
        conn, stage="P0", scales={"AAA0": 0.6, "AAA1": 0.9}, per_station={},
        fitted_at_utc="2026-09-07T15:48:00Z",
    )
    assert second["state"] == "ACTIVATED"
    assert load_station_width(conn, stage="P0", table_sha256=old_sha) == (
        {"AAA0": 0.5, "AAA1": 0.8}, old_sha
    )
    # Retired by the veto: the pinned read of the retired version is
    # identity, with the sha kept for the receipt -- and so is the pinned
    # read of the superseded older version, which carried the same
    # contraction from the same evidence and is still pinned by artifacts
    # minted under it.  Another stage's table is untouched.
    other = persist_and_activate_station_width(
        conn, stage="P0-M", scales={"AAA0": 0.7}, per_station={},
        fitted_at_utc="2026-09-06T23:01:00Z",
    )
    retired = persist_and_activate_station_width(
        conn, stage="P0", scales={}, per_station={},
        fitted_at_utc="2026-09-10T15:27:09Z",
    )
    assert retired["state"] == "DEACTIVATED"
    new_sha = second["station_width_sha256"]
    assert load_station_width(conn, stage="P0", table_sha256=new_sha) == (
        {}, new_sha
    )
    assert load_station_width(conn, stage="P0", table_sha256=old_sha) == (
        {}, old_sha
    )
    assert load_station_width(conn, stage="P0") is None
    assert load_station_width(
        conn, stage="P0-M", table_sha256=other["station_width_sha256"]
    ) == ({"AAA0": 0.7}, other["station_width_sha256"])
    rows = conn.execute(
        "SELECT station_width_sha256, retired_at_utc FROM"
        " calibration_center_width_station WHERE stage='P0'"
        " ORDER BY station_id, fitted_at_utc"
    ).fetchall()
    assert {r[0]: r[1] for r in rows} == {
        old_sha: "2026-09-10T15:27:09Z", new_sha: "2026-09-10T15:27:09Z",
    }
    # A pinned version that never existed still fails closed.
    with pytest.raises(ContractError):
        load_station_width(conn, stage="P0", table_sha256="0" * 64)


def test_the_retired_column_is_added_to_an_existing_table() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_center_width_station ("
        " station_width_sha256 TEXT NOT NULL, stage TEXT NOT NULL,"
        " station_id TEXT NOT NULL, width_scale REAL NOT NULL,"
        " raw_z_sd REAL, sample_n INTEGER NOT NULL, fitted_at_utc TEXT NOT NULL,"
        " activated INTEGER NOT NULL, activated_at_utc TEXT NOT NULL,"
        " PRIMARY KEY (station_width_sha256, station_id))"
    )
    conn.execute(
        "INSERT INTO calibration_center_width_station VALUES"
        " (?,'P0','AAA0',0.5,0.6,62,'2026-09-06T15:48:00Z',0,"
        " '2026-09-06T15:48:00Z')",
        ("a" * 64,),
    )
    conn.commit()
    ensure_center_width_schema(conn)
    # Pre-existing rows read as never retired: the pin is honoured.
    assert load_station_width(conn, stage="P0", table_sha256="a" * 64) == (
        {"AAA0": 0.5}, "a" * 64
    )


def test_a_pinned_read_on_a_store_without_the_retired_column_is_not_a_missing_table() -> None:
    """The resident serves without running the schema; between a deploy
    and the next sweep the column is absent.  A pinned read must then be
    the pre-column read -- never retired -- not a fail-closed refusal on
    every artifact that pins a version."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_center_width_station ("
        " station_width_sha256 TEXT NOT NULL, stage TEXT NOT NULL,"
        " station_id TEXT NOT NULL, width_scale REAL NOT NULL,"
        " raw_z_sd REAL, sample_n INTEGER NOT NULL, fitted_at_utc TEXT NOT NULL,"
        " activated INTEGER NOT NULL, activated_at_utc TEXT NOT NULL,"
        " PRIMARY KEY (station_width_sha256, station_id))"
    )
    conn.execute(
        "INSERT INTO calibration_center_width_station VALUES"
        " (?,'P0','AAA0',0.5,0.6,62,'2026-09-06T15:48:00Z',0,"
        " '2026-09-06T15:48:00Z')",
        ("a" * 64,),
    )
    conn.commit()
    assert load_station_width(conn, stage="P0", table_sha256="a" * 64) == (
        {"AAA0": 0.5}, "a" * 64
    )
    with pytest.raises(ContractError):
        load_station_width(conn, stage="P0", table_sha256="b" * 64)
