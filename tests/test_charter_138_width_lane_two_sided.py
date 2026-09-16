"""Charter 138: the width layer must be able to say "wider", and a pooled
table that stops earning its place must be retirable.

Two defects, one layer.  The grids stopped at 1.000, so the fit could say
"narrow" and "hold" and structurally could not say "widen" -- measured
2026-09-16, P0-M's window containment ran 0.969/0.966/0.942 (claiming the
mean+-1 window more often than it settled there) while every fit pinned at
the ceiling and reported "unchanged".  And ``fit_center_width`` had no
incumbent veto, so with the evidence clock restarted by every serving
arithmetic change the accumulating hold kept a pooled pair in service with
nobody looking -- the same hole the station lane closed on 2026-09-10.

Behavioural throughout: the assertions are about what the lane does with
cells and tables, not about what the constants read.
"""

from __future__ import annotations

import json
import math
import sqlite3

import pytest

from bayes_basket_target.center_width_calibration import (
    MAX_WIDTH_LAMBDA,
    MIN_WIDTH_LAMBDA,
    apply_center_width_calibration,
)
from bayes_basket_target.center_width_store import (
    CENTER_WIDTH_VETO_MIN_CELLS,
    CENTER_WIDTH_VETO_MIN_DAYS,
    LAMBDA_GRID,
    MIN_FORWARD_NLL_GAIN,
    MIN_QUALIFYING_DAYS,
    WIDTH_TIER_LAMBDA_GRID,
    _search,
    _search_tiers,
    _truth,
    ensure_center_width_schema,
    fit_center_width,
    load_center_width,
    load_center_width_cells,
    load_center_width_floor,
    persist_and_activate_center_width,
    refit_all_center_width,
    retire_center_width,
    serving_cells_by_day,
)
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.table_as_of import center_width_as_of

CENTRE = 27.0
INTERVALS = {
    "23°C or below": [None, 23.5],
    **{f"{v}°C": [v - 0.5, v + 0.5] for v in range(24, 31)},
    "31°C or higher": [30.5, None],
}
KEYS = list(INTERVALS)
CENTERS = [22.5, 24.0, 25.0, 26.0, 27.0, 28.0, 29.0, 30.0, 31.5]
# Deterministic, and normalised to unit standard deviation so that
# ``truth_sd`` below means what it says: the raw sequence's own sd is 0.795,
# and leaving it in would make a correctly served fleet look 20% too wide.
_RAW_NOISE = [0.9, -1.1, 0.4, -0.5, 1.2, -0.8, 0.2, -1.3, 0.7, -0.3,
              1.0, -0.6, 0.5, -1.0, 0.8, -0.2]
_NOISE_SD = math.sqrt(
    sum(v * v for v in _RAW_NOISE) / len(_RAW_NOISE)
    - (sum(_RAW_NOISE) / len(_RAW_NOISE)) ** 2
)
NOISE = [round(v / _NOISE_SD, 6) for v in _RAW_NOISE]


def _bucketed(sd: float) -> dict[str, float]:
    def cdf(x: float) -> float:
        return 0.5 * (1 + math.erf((x - CENTRE) / (sd * math.sqrt(2))))

    raw = {}
    for key, (lo, hi) in INTERVALS.items():
        a = cdf(lo) if lo is not None else 0.0
        b = cdf(hi) if hi is not None else 1.0
        raw[key] = b - a
    total = sum(raw.values())
    return {k: v / total for k, v in raw.items()}


def _seed(
    conn: sqlite3.Connection,
    *,
    served_sd: float,
    truth_sd: float,
    day_count: int,
    station_count: int = 20,
    stage: str = "P0",
    applied: tuple[float, float] | None = None,
    receipt: bool = True,
    receipt_drops: tuple[str, ...] = (),
) -> None:
    """Serve ``station_count`` stations a distribution a day, and settle each
    day's truth at CENTRE + noise * truth_sd.

    ``truth_sd`` wider than what is served is a fleet served TOO NARROW --
    the case the grid could not express and the case the veto exists to
    catch.  The served envelope is what the fit reads, so whatever pair was
    active when it was written is already inside it: these cells ARE the
    served distribution, not the model's input to it.

    ``applied`` runs the kernel the way production does -- an input of width
    ``served_sd / lambda`` through the pair, so the SERVED width is
    ``served_sd`` -- and stores the receipt the kernel returned, the row's own
    record of what it had applied.  ``receipt=False`` is a row written before
    that record existed, which is every historical row.
    """

    conn.executescript(
        "CREATE TABLE IF NOT EXISTS basket_probability_bridge_history ("
        " station_id TEXT, event_date TEXT, decision_at_utc TEXT,"
        " envelope_json TEXT, settlement_timezone TEXT, stage TEXT,"
        " checkpoint_id TEXT DEFAULT '',"
        " envelope_content_sha256 TEXT DEFAULT '');"
        "CREATE TABLE IF NOT EXISTS calibration_station_daily_high_truth ("
        " station_id TEXT, local_date TEXT, official_tmax_c REAL,"
        " revision_no INTEGER, qc_status TEXT);"
    )
    lineage_extra: dict[str, str] = {}
    if applied is None:
        probabilities = _bucketed(served_sd)
    else:
        delta, lam = applied
        probabilities, kernel_receipt = apply_center_width_calibration(
            _bucketed(served_sd / lam), delta_c=delta, width_lambda=lam,
            ordered_keys=KEYS, key_centers_c=CENTERS,
        )
        if receipt:
            kernel_receipt = {
                key: value for key, value in kernel_receipt.items()
                if key not in receipt_drops
            }
            lineage_extra["center_width_json"] = json.dumps(kernel_receipt)
    ordinals = {k: i for i, k in enumerate(KEYS)}
    for day_index in range(day_count):
        day = f"2026-07-{day_index + 1:02d}"
        for index in range(station_count):
            station = f"AAA{index}"
            envelope = {
                "probabilities": probabilities,
                "bucket_ordinals": ordinals,
                "lineage": {
                    "bucket_intervals_c_json": json.dumps(INTERVALS),
                    "hard_excluded_buckets": "NONE",
                    **lineage_extra,
                },
            }
            conn.execute(
                "INSERT INTO basket_probability_bridge_history"
                " VALUES (?,?,?,?,?,?,'','')",
                (station, day, f"{day}T00:00:00Z", json.dumps(envelope),
                 "UTC", stage),
            )
            offset = NOISE[(day_index * station_count + index) % len(NOISE)]
            conn.execute(
                "INSERT INTO calibration_station_daily_high_truth"
                " VALUES (?,?,?,1,'ACCEPTED')",
                (station, day, CENTRE + offset * truth_sd),
            )
    conn.commit()


def _cells(conn: sqlite3.Connection, *, stage: str = "P0") -> list[dict]:
    by_day = serving_cells_by_day(conn, _truth(conn), stage=stage)
    return [cell for day in sorted(by_day) for cell in by_day[day]]


# --- (一) the grid can say "wider" -------------------------------------


def test_the_search_can_ask_for_a_wider_distribution() -> None:
    """Truth twice as spread as the serving: the only correction that helps
    is widening, and the search must be able to return it.

    Under the old grid this returned exactly 1.000 -- the ceiling, reported
    as "unchanged" -- no matter how far the cells were from it.
    """

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed(conn, served_sd=1.0, truth_sd=2.0, day_count=4)
    _, lam, _ = _search(_cells(conn))
    assert lam > 1.0, lam
    assert lam <= MAX_WIDTH_LAMBDA


def test_the_tier_search_can_ask_for_a_wider_distribution() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed(conn, served_sd=1.0, truth_sd=2.0, day_count=4)
    _, lambdas, _ = _search_tiers(_cells(conn))
    assert max(lambdas) > 1.0, lambdas


def test_a_correctly_served_fleet_is_still_left_alone() -> None:
    """Widening is now sayable, which must not make it the default: cells
    whose serving already matches the truth fit near identity."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed(conn, served_sd=2.0, truth_sd=2.0, day_count=4)
    _, lam, _ = _search(_cells(conn))
    assert lam == pytest.approx(1.0, abs=0.15), lam


def test_every_grid_value_is_one_the_kernel_will_serve() -> None:
    """The grid is a proposal and the kernel is the authority.  1.5 is where
    the kernel stops -- past it a correction is describing something
    structural and must be reported, not served -- so a grid that reached it
    would be proposing pairs that fail closed at serve time."""

    probability = _bucketed(2.0)
    for grid in (LAMBDA_GRID, WIDTH_TIER_LAMBDA_GRID):
        assert MIN_WIDTH_LAMBDA <= min(grid)
        assert max(grid) < MAX_WIDTH_LAMBDA
        for lam in grid:
            served, _ = apply_center_width_calibration(
                probability, delta_c=0.0, width_lambda=lam,
                ordered_keys=KEYS, key_centers_c=CENTERS,
            )
            assert sum(served.values()) == pytest.approx(1.0)


# --- (二) the pooled incumbent can be retired ---------------------------


def _activate(conn: sqlite3.Connection, *, delta_c: float, lam: float) -> str:
    return persist_and_activate_center_width(
        conn, stage="P0", delta_c=delta_c, width_lambda=lam,
        evidence={"seeded": True}, serving_day_count=8,
        fitted_at_utc="2026-06-30T00:00:00Z",
    )["center_width_sha256"]


def test_an_incumbent_that_hurts_after_activation_is_vetoed() -> None:
    """The served cells are half as wide as the truth is spread.  Whatever
    narrowed this serving was wrong; undoing it wins on the cells served
    after the activation, and the lane retires the pair."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed(conn, served_sd=1.0, truth_sd=2.4, day_count=MIN_QUALIFYING_DAYS - 1,
          applied=(0.0, 0.5))
    _activate(conn, delta_c=0.0, lam=0.5)
    fitted = fit_center_width(conn, stage="P0")
    assert fitted["vetoed"]
    assert (fitted["delta_c"], fitted["width_lambda"]) == (0.0, 1.0)
    assert fitted["evidence"]["basis"] == "INCUMBENT_VETOED"
    veto = fitted["evidence"]["incumbent_veto"]
    assert veto["enough_evidence"]
    assert veto["undo_gain"] > MIN_FORWARD_NLL_GAIN
    assert veto["nll_undone"] < veto["nll_kept"]
    assert veto["activated_at_utc"] == "2026-06-30T00:00:00Z"


def test_the_veto_is_judged_before_the_accumulating_hold() -> None:
    """The crux.  The hold is exactly the window in which nothing else looks
    at the incumbent, so a veto that waited for the qualifying floor would
    never fire on the table it exists for."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    days = MIN_QUALIFYING_DAYS - 1
    assert CENTER_WIDTH_VETO_MIN_DAYS <= days < MIN_QUALIFYING_DAYS
    _seed(conn, served_sd=1.0, truth_sd=2.4, day_count=days,
          applied=(0.0, 0.5))
    _activate(conn, delta_c=0.0, lam=0.5)
    fitted = fit_center_width(conn, stage="P0")
    assert fitted["accumulating"], "the fit must still be short of the floor"
    assert fitted["vetoed"], "and the veto must fire anyway"
    assert fitted["evidence"]["incumbent_veto"]["cell_count"] >= (
        CENTER_WIDTH_VETO_MIN_CELLS
    )


def test_an_incumbent_that_earns_its_place_is_held_not_vetoed() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    # Served 2.0 wide against truth spread 1.5: the input was 2.67 and the
    # pair is what brought it near the truth, so leaving the pair off loses.
    _seed(conn, served_sd=2.0, truth_sd=1.5, day_count=MIN_QUALIFYING_DAYS - 1,
          applied=(0.0, 0.75))
    _activate(conn, delta_c=0.0, lam=0.75)
    fitted = fit_center_width(conn, stage="P0")
    assert not fitted["vetoed"]
    assert fitted["accumulating"]
    assert (fitted["delta_c"], fitted["width_lambda"]) == (0.0, 0.75)
    veto = fitted["evidence"]["incumbent_veto"]
    assert veto["undo_gain"] <= MIN_FORWARD_NLL_GAIN
    # lambda 0.75 is rank-deficient on this axis, so this verdict exists only
    # because the row recorded the input rather than leaving it to be solved.
    assert veto["counterfactual_basis"] == {"recorded_input": veto["cell_count"]}


def test_a_stage_with_no_table_records_no_veto() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed(conn, served_sd=1.0, truth_sd=2.4, day_count=MIN_QUALIFYING_DAYS - 1)
    fitted = fit_center_width(conn, stage="P0")
    assert not fitted["vetoed"]
    assert "incumbent_veto" not in fitted["evidence"]


def test_the_sweep_retires_a_vetoed_pair_and_journals_it() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed(conn, served_sd=1.0, truth_sd=2.4, day_count=MIN_QUALIFYING_DAYS - 1,
          applied=(0.0, 0.5))
    _activate(conn, delta_c=0.0, lam=0.5)
    entry = refit_all_center_width(conn, commit=True, stages=["P0"])["P0"]
    assert "error" not in entry, entry
    assert entry["vetoed"]
    assert entry["outcome"] == "INCUMBENT_VETOED"
    assert entry["commit"]["state"] == "RETIRED"
    assert load_center_width(conn, stage="P0") is None
    row = conn.execute(
        "SELECT outcome, evidence_json FROM"
        " calibration_center_width_fit_journal WHERE stage='P0'"
        " ORDER BY fitted_at_utc DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "INCUMBENT_VETOED"
    assert json.loads(row[1])["basis"] == "INCUMBENT_VETOED"


# --- (三) retiring has to reach the pinned readers ----------------------


def test_a_retired_pin_serves_identity_and_a_live_one_serves_its_pair() -> None:
    """Deactivating stops the unpinned readers; only the stamp stops the
    pinned ones.  Without it a vetoed pair keeps serving every artifact
    minted before the veto, for as long as the oldest one lives."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    sha = _activate(conn, delta_c=0.2, lam=0.6)
    assert load_center_width(conn, stage="P0", table_sha256=sha) == (
        0.2, 0.6, sha
    )
    retire_center_width(
        conn, stage="P0", retired_at_utc="2026-07-05T00:00:00Z"
    )
    assert load_center_width(conn, stage="P0") is None
    assert load_center_width(conn, stage="P0", table_sha256=sha) == (
        0.0, 1.0, sha
    )


def test_a_replay_before_the_retirement_still_reads_the_pair() -> None:
    """``as_of_utc`` is the replay's question: what was served then.  Serving
    passes nothing and stops at the veto."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    sha = _activate(conn, delta_c=0.2, lam=0.6)
    retire_center_width(
        conn, stage="P0", retired_at_utc="2026-07-05T00:00:00Z"
    )
    before = load_center_width(
        conn, stage="P0", table_sha256=sha,
        as_of_utc="2026-07-01T00:00:00Z",
    )
    after = load_center_width(
        conn, stage="P0", table_sha256=sha,
        as_of_utc="2026-07-09T00:00:00Z",
    )
    assert before == (0.2, 0.6, sha)
    assert after == (0.0, 1.0, sha)


def test_a_missing_pin_still_fails_closed() -> None:
    """Retirement is the one case a pin reads as identity.  A pin that names
    a version nobody has is still an artifact whose math cannot be
    reproduced, and must not be served an approximation of it."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    with pytest.raises(ContractError):
        load_center_width(conn, stage="P0", table_sha256="f" * 64)


def test_a_retired_pin_also_stops_the_floor_and_the_cells() -> None:
    """Retirement retires the row's whole arithmetic.  A pin reading identity
    for the pair while still serving that row's floor or TAF cells would be
    serving a composition nobody ever scored."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    sha = persist_and_activate_center_width(
        conn, stage="P0-M", delta_c=0.0, width_lambda=0.9,
        evidence={}, serving_day_count=8, sigma_floor_rungs=1.44,
        taf_cells={"far": [0.0, 0.3, 0.8]},
        fitted_at_utc="2026-06-30T00:00:00Z",
    )["center_width_sha256"]
    assert load_center_width_floor(
        conn, stage="P0-M", table_sha256=sha
    ) == pytest.approx(1.44)
    assert load_center_width_cells(conn, stage="P0-M", table_sha256=sha)
    retire_center_width(
        conn, stage="P0-M", retired_at_utc="2026-07-05T00:00:00Z"
    )
    assert load_center_width_floor(
        conn, stage="P0-M", table_sha256=sha
    ) == 0.0
    assert load_center_width_cells(
        conn, stage="P0-M", table_sha256=sha
    ) is None


def test_the_as_of_resolver_stops_replaying_a_retired_pair() -> None:
    """The replay resolver used to pass ``retirable=False``, which was true
    while the pooled lane had no veto and is a misread now: a replay after
    the retirement must score identity, and one before it must still score
    the pair that was actually served then.

    The activation event has to survive the retirement for that to be
    answerable at all -- retiring clears ``activated_at_utc`` on the row, so
    the journal's ACTIVATED entry is what the resolver reads back.
    """

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed(conn, served_sd=1.0, truth_sd=2.4, day_count=MIN_QUALIFYING_DAYS - 1,
          applied=(0.0, 0.5))
    sha = _activate(conn, delta_c=0.0, lam=0.5)
    conn.execute(
        "INSERT INTO calibration_center_width_fit_journal VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?)",
        ("seed", "P0", "2026-06-30T00:00:00Z", sha, 0.0, 0.5, 8, 200, 0.03,
         "ACTIVATED", "{}"),
    )
    conn.commit()
    before = center_width_as_of(
        conn, stage="P0", as_of_utc="2026-07-02T00:00:00Z"
    )
    assert before is not None
    assert (before["delta_c"], before["width_lambda"]) == (0.0, 0.5)

    entry = refit_all_center_width(conn, commit=True, stages=["P0"])["P0"]
    assert entry["outcome"] == "INCUMBENT_VETOED"
    retired_at = entry["commit"]["retired_at_utc"]

    still = center_width_as_of(
        conn, stage="P0", as_of_utc="2026-07-02T00:00:00Z"
    )
    assert still is not None, "a replay before the veto keeps its width"
    assert (still["delta_c"], still["width_lambda"]) == (0.0, 0.5)
    assert center_width_as_of(conn, stage="P0", as_of_utc=retired_at) is None


def test_an_old_pooled_journal_is_widened_for_the_veto_outcome() -> None:
    """The live journal predates the verdict; CREATE IF NOT EXISTS would
    leave its CHECK as it was and the insert would fail."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_center_width_fit_journal ("
        " fit_id TEXT PRIMARY KEY, stage TEXT NOT NULL,"
        " fitted_at_utc TEXT NOT NULL, center_width_sha256 TEXT NOT NULL,"
        " delta_c REAL NOT NULL, width_lambda REAL NOT NULL,"
        " serving_day_count INTEGER NOT NULL,"
        " serving_cell_count INTEGER NOT NULL, forward_nll_gain REAL NOT NULL,"
        " outcome TEXT NOT NULL CHECK(outcome IN"
        "  ('ACTIVATED','UNCHANGED','ACCUMULATING','REFUSED_FORWARD',"
        "   'NOT_WIRED')),"
        " evidence_json TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO calibration_center_width_fit_journal VALUES"
        " ('old','P0','2026-09-03T21:19:03Z',?,0.0,0.75,8,585,0.031,"
        " 'ACTIVATED','{}')",
        ("a" * 64,),
    )
    conn.commit()
    ensure_center_width_schema(conn)
    conn.execute(
        "INSERT INTO calibration_center_width_fit_journal VALUES"
        " ('new','P0','2026-09-16T00:00:00Z',?,0.0,1.0,3,585,-0.1,"
        " 'INCUMBENT_VETOED','{}')",
        ("b" * 64,),
    )
    rows = conn.execute(
        "SELECT fit_id, outcome FROM calibration_center_width_fit_journal"
        " ORDER BY fit_id"
    ).fetchall()
    assert rows == [("new", "INCUMBENT_VETOED"), ("old", "ACTIVATED")]
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name LIKE '%superseded'"
    ).fetchone()[0] == 0


def test_the_journal_migration_degrades_on_a_read_only_connection() -> None:
    """Every dry run, probe and dashboard opens this store read-only and
    calls the schema on the way in.  Adding an outcome makes the live CHECK
    look stale to all of them, and a rebuild that raises would take them out
    between the deploy and the next writable sweep -- for a migration none of
    them needs, since they read the journal rather than insert into it."""

    path = "file:charter138_ro?mode=memory&cache=shared"
    keep = sqlite3.connect(path, uri=True)
    # The live store's shape: every table already there, and only the
    # journal's CHECK behind.  ``CREATE TABLE IF NOT EXISTS`` on a table that
    # exists writes nothing, which is why the rest of the schema pass is
    # already safe read-only and only the rebuild was not.
    ensure_center_width_schema(keep)
    keep.execute("DROP TABLE calibration_center_width_fit_journal")
    keep.execute(
        "CREATE TABLE calibration_center_width_fit_journal ("
        " fit_id TEXT PRIMARY KEY, stage TEXT NOT NULL,"
        " fitted_at_utc TEXT NOT NULL, center_width_sha256 TEXT NOT NULL,"
        " delta_c REAL NOT NULL, width_lambda REAL NOT NULL,"
        " serving_day_count INTEGER NOT NULL,"
        " serving_cell_count INTEGER NOT NULL, forward_nll_gain REAL NOT NULL,"
        " outcome TEXT NOT NULL CHECK(outcome IN"
        "  ('ACTIVATED','UNCHANGED','ACCUMULATING','REFUSED_FORWARD',"
        "   'NOT_WIRED')),"
        " evidence_json TEXT NOT NULL)"
    )
    keep.commit()
    reader = sqlite3.connect(
        "file:charter138_ro?mode=memory&cache=shared&immutable=0",
        uri=True,
    )
    reader.execute("PRAGMA query_only=ON")
    ensure_center_width_schema(reader)
    # Still the old CHECK -- the reader could not migrate it, and did not
    # pretend otherwise by failing.
    live = keep.execute(
        "SELECT sql FROM sqlite_master WHERE"
        " name='calibration_center_width_fit_journal'"
    ).fetchone()[0]
    assert "INCUMBENT_VETOED" not in live
    assert keep.execute(
        "SELECT count(*) FROM sqlite_master WHERE name LIKE '%superseded'"
    ).fetchone()[0] == 0
    reader.close()
    keep.close()


def test_a_store_without_the_retirement_column_reads_as_never_retired() -> None:
    """Between a deploy and the next sweep the resident serves without having
    run the schema.  That is not a missing table, and every artifact pinning
    a version must keep reading its pair rather than failing closed."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    sha = _activate(conn, delta_c=0.2, lam=0.6)
    rows = conn.execute(
        "SELECT center_width_sha256, stage, delta_c, width_lambda,"
        " evidence_json, fitted_at_utc, serving_day_count, activated,"
        " activated_at_utc FROM calibration_center_width_table"
    ).fetchall()
    conn.execute("DROP TABLE calibration_center_width_table")
    conn.execute(
        "CREATE TABLE calibration_center_width_table ("
        " center_width_sha256 TEXT PRIMARY KEY, stage TEXT NOT NULL,"
        " delta_c REAL NOT NULL, width_lambda REAL NOT NULL,"
        " evidence_json TEXT NOT NULL, fitted_at_utc TEXT NOT NULL,"
        " serving_day_count INTEGER NOT NULL, activated INTEGER NOT NULL,"
        " activated_at_utc TEXT)"
    )
    conn.executemany(
        "INSERT INTO calibration_center_width_table VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    assert load_center_width(conn, stage="P0", table_sha256=sha) == (
        0.2, 0.6, sha
    )
    assert load_center_width_floor(
        conn, stage="P0", table_sha256=sha
    ) == 0.0


# --- (四) the counterfactual is recovered, not re-run -------------------


def _sigma(probability: dict[str, float]) -> float:
    total = sum(probability.values())
    mean = sum(
        c * probability[k] / total for k, c in zip(KEYS, CENTERS)
    )
    return math.sqrt(sum(
        probability[k] / total * (c - mean) ** 2
        for k, c in zip(KEYS, CENTERS)
    ))


def test_running_the_pair_backwards_is_not_its_inverse() -> None:
    """Why the counterfactual is recovered rather than re-run.

    The move is a linear map on the bucket vector, and a second pass through
    it is not an inverse: lambda and then 1/lambda smears mass across
    neighbours.  How far off that lands depends on how narrow the claim is
    relative to the rung -- on the live window of 2026-09-16, where the
    served spread runs near one rung, re-running came out 14% too wide on P0
    and 23% on P0-M, all of it pointing towards keeping the incumbent.

    Asserted as the claim itself rather than as a fixed bias: re-running does
    not return the input, and recording it does.
    """

    for width in (1.0, 1.6, 2.4):
        source = _bucketed(width)
        served, receipt = apply_center_width_calibration(
            source, delta_c=0.0, width_lambda=0.9,
            ordered_keys=KEYS, key_centers_c=CENTERS,
        )
        rerun, _ = apply_center_width_calibration(
            served, delta_c=0.0, width_lambda=1.0 / 0.9,
            ordered_keys=KEYS, key_centers_c=CENTERS,
        )
        recovered = {
            key: float(value)
            for key, value in receipt["input_probability"].items()
        }
        drift = sum(abs(rerun[k] - source[k]) for k in KEYS)
        exact = sum(abs(recovered[k] - source[k]) for k in KEYS)
        assert drift > 0.02, (width, drift)
        assert exact < 1e-6, (width, exact)

    # And in the regime production actually serves -- a claim about one rung
    # wide -- the drift is a widening, which is the direction that flatters
    # the incumbent.
    narrow = _bucketed(1.0)
    served, _ = apply_center_width_calibration(
        narrow, delta_c=0.0, width_lambda=0.9,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    rerun, _ = apply_center_width_calibration(
        served, delta_c=0.0, width_lambda=1.0 / 0.9,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    assert _sigma(rerun) > _sigma(narrow) * 1.05


def test_the_receipt_records_the_distribution_the_layer_was_applied_to() -> None:
    source = _bucketed(2.2)
    _, receipt = apply_center_width_calibration(
        source, delta_c=0.1, width_lambda=0.85,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    recorded = receipt["input_probability"]
    assert set(recorded) == set(source)
    assert sum(recorded.values()) == pytest.approx(1.0, abs=1e-6)
    for key, value in source.items():
        assert recorded[key] == pytest.approx(value, abs=1e-8)


def test_a_contraction_the_solve_cannot_invert_is_not_judged() -> None:
    """A historical row -- one written before the input was recorded -- under
    a contraction that is rank-deficient on its own axis.  Nothing short of
    the recorded input can answer it, and guessing would be a number with no
    evidence behind it, so the lane declines and holds."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed(conn, served_sd=1.0, truth_sd=2.4, day_count=MIN_QUALIFYING_DAYS - 1,
          applied=(0.0, 0.5), receipt_drops=("input_probability",))
    _activate(conn, delta_c=0.0, lam=0.5)
    fitted = fit_center_width(conn, stage="P0")
    veto = fitted["evidence"]["incumbent_veto"]
    assert not fitted["vetoed"]
    assert veto["cell_count"] == 0
    assert not veto["enough_evidence"]
    assert veto["skipped"] == {"not_recoverable": veto["skipped_count"]}
    assert veto["skipped_count"] > 0
    assert load_center_width(conn, stage="P0") is not None


def test_a_historical_row_a_full_rank_map_can_answer_is_judged() -> None:
    """The same row without a recorded input, but under a gentler pair the
    map keeps the rank of: the input is solved for, and the verdict stands on
    that rather than on the pair run backwards."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed(conn, served_sd=1.0, truth_sd=2.4, day_count=MIN_QUALIFYING_DAYS - 1,
          applied=(0.0, 0.9), receipt_drops=("input_probability",))
    _activate(conn, delta_c=0.0, lam=0.9)
    fitted = fit_center_width(conn, stage="P0")
    veto = fitted["evidence"]["incumbent_veto"]
    assert veto["counterfactual_basis"] == {"recovered": veto["cell_count"]}
    assert veto["cell_count"] > 0
    assert fitted["vetoed"]


def test_a_row_with_no_receipt_at_all_is_not_judged() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed(conn, served_sd=1.0, truth_sd=2.4, day_count=MIN_QUALIFYING_DAYS - 1,
          applied=(0.0, 0.9), receipt=False)
    _activate(conn, delta_c=0.0, lam=0.9)
    fitted = fit_center_width(conn, stage="P0")
    veto = fitted["evidence"]["incumbent_veto"]
    assert not fitted["vetoed"]
    assert veto["cell_count"] == 0
    assert veto["skipped"] == {"no_receipt": veto["skipped_count"]}


def test_the_verdict_reads_the_width_the_row_was_served_under() -> None:
    """Not the width the stage serves today.  A row served under a pair the
    stage has since replaced is evidence about that row's pair, and undoing
    the current one on it would be undoing something that was never there."""

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    _seed(conn, served_sd=1.0, truth_sd=2.4, day_count=MIN_QUALIFYING_DAYS - 1,
          applied=(0.0, 0.9))
    _activate(conn, delta_c=0.0, lam=0.9)
    from bayes_basket_target.center_width_store import _identity_counterfactual

    cell = _cells(conn)[0]
    assert cell["center_width_receipt"]["width_lambda"] == pytest.approx(0.9)
    counterfactual, basis = _identity_counterfactual(
        cell, delta_c=0.0, width_lambda=0.9
    )
    assert basis == "recorded_input"
    assert counterfactual is not None
    # Leaving off exactly what was applied returns the input, unchanged.
    assert _sigma(counterfactual) == pytest.approx(
        _sigma({k: float(v) for k, v in
                cell["center_width_receipt"]["input_probability"].items()}),
        rel=1e-9,
    )
