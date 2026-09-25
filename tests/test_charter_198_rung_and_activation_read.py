"""Charter 198: one rung reading, and the activation read without its row cap.

Two serving defects found by the P0 downstream audit (2026-09-22), fixed in
one bump (P0 v9, P0-M v11, P1 v19; user ruling):

* The width kernel read a ladder's rung as its smallest key-centre step.
  Charter 190 put an open bucket's key centre 0.5 C past its bound, so on a
  Fahrenheit ladder the edge step is 0.5 + 5/9 = 1.056 C and the kernel read
  that, while the trading layer read 10/9 off the same ladder.  The rung
  scales every open-edge tail the kernel carries, so the served P0 moved on
  537 of 625 Fahrenheit packets (a bucket by up to 3.2e-3).
* The compile read the scope's valid activations ``ORDER BY generation DESC
  LIMIT 256``.  Generation counts re-mints of one pairing, not time, and a
  stable-pool scope holds up to 753 valid activations, so seats the rank
  would have chosen were never seen: 180 of 2,475 compiles since 09-20 sat
  on another seat.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import center_width_calibration as C  # noqa: E402
from bayes_basket_target import native_ensemble_p0 as P0  # noqa: E402
from bayes_basket_target import runtime  # noqa: E402

RUNG_F = 10.0 / 9.0


def _fahrenheit_ladder(first_f: int = 71, buckets: int = 11) -> dict:
    """A real market's shape: open ends, 2F closed buckets between them."""

    def c(value_f: float) -> float:
        return (value_f - 32.0) * 5.0 / 9.0

    intervals = {f"{first_f}F or below": [None, c(first_f + 0.5)]}
    low = first_f + 1
    for _ in range(buckets - 2):
        intervals[f"{low}-{low + 1}F"] = [c(low - 0.5), c(low + 1.5)]
        low += 2
    intervals[f"{low}F or higher"] = [c(low - 0.5), None]
    return intervals


def _celsius_ladder() -> dict:
    intervals = {"22C or below": [None, 22.5]}
    for degree in range(23, 32):
        intervals[f"{degree}C"] = [degree - 0.5, degree + 0.5]
    intervals["32C or higher"] = [31.5, None]
    return intervals


@pytest.mark.parametrize(
    "intervals, rung",
    [(_fahrenheit_ladder(), RUNG_F), (_fahrenheit_ladder(59, 5), RUNG_F),
     (_celsius_ladder(), 1.0)],
)
def test_the_kernel_and_the_trading_layer_read_one_rung(intervals, rung) -> None:
    keys = list(intervals)
    centers, bounds = C.served_bucket_axis(intervals, keys)
    assert bounds[0] is not None and bounds[1] is not None
    kernel = C.bucket_rung_width_c(centers)
    envelope = SimpleNamespace(
        lineage={"bucket_intervals_c_json": json.dumps(intervals)},
        bucket_ordinals={key: index for index, key in enumerate(keys)},
    )
    trading = runtime._bucket_rung_width_c(envelope)
    assert kernel == pytest.approx(rung, abs=1e-12)
    assert trading == pytest.approx(rung, abs=1e-12)
    assert kernel == trading
    # The defect: the smallest key-centre step is the open edge's on a
    # Fahrenheit ladder, half a degree plus half a rung.
    steps = [b - a for a, b in zip(centers, centers[1:])]
    if rung != 1.0:
        assert min(steps) == pytest.approx(0.5 + RUNG_F / 2.0)


def test_the_trading_layer_reads_through_the_shared_function(monkeypatch) -> None:
    intervals = _fahrenheit_ladder()
    envelope = SimpleNamespace(
        lineage={"bucket_intervals_c_json": json.dumps(intervals)},
        bucket_ordinals={key: index for index, key in enumerate(intervals)},
    )
    seen = []

    def spy(centers):
        seen.append(sorted(centers))
        return 42.0

    monkeypatch.setattr(runtime, "ladder_rung_width_c", spy)
    assert runtime._bucket_rung_width_c(envelope) == 42.0
    # Only the closed buckets reach it: the open ends have no width.
    assert len(seen) == 1 and len(seen[0]) == len(intervals) - 2


def test_an_open_fahrenheit_tail_is_scaled_by_the_fahrenheit_rung() -> None:
    intervals = _fahrenheit_ladder()
    keys = list(intervals)
    centers, bounds = C.served_bucket_axis(intervals, keys)
    probability = {key: 0.0 for key in keys}
    probability.update({keys[0]: 0.12, keys[1]: 0.18, keys[2]: 0.30,
                        keys[3]: 0.25, keys[4]: 0.15})
    served, receipt = C.apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=0.75, ordered_keys=keys,
        key_centers_c=centers, open_edge_bounds_c=bounds,
    )
    assert receipt["bucket_rung_c"] == pytest.approx(RUNG_F, abs=1e-12)
    scale = receipt["open_edge_tail_scale_c"]["lower"]
    assert scale == pytest.approx(0.12 * RUNG_F / 0.18, abs=1e-12)
    assert sum(served.values()) == pytest.approx(1.0, abs=1e-12)


def test_a_closed_integer_grid_still_reads_one_degree() -> None:
    # The OOS holdout's grid has no open ends; dropping its end buckets loses
    # nothing, and short axes read every step.
    assert C.bucket_rung_width_c([float(v) for v in range(20, 31)]) == 1.0
    assert C.bucket_rung_width_c([20.0, 21.0, 22.0]) == 1.0
    assert C.ladder_rung_width_c([25.0]) is None


# --- the activation read -----------------------------------------------------

def _probe_store() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_cross_native_ensemble_artifact("
        " cross_ensemble_artifact_id TEXT PRIMARY KEY,"
        " ensemble_system_ids_json TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE calibration_native_ensemble_activation("
        " activation_id TEXT PRIMARY KEY, artifact_kind TEXT, artifact_id TEXT,"
        " station_id TEXT, season_bin TEXT, lead_bucket TEXT,"
        " authority_scope TEXT, generation INTEGER, valid_from_utc TEXT,"
        " valid_until_utc TEXT, run_cycle_by_system_json TEXT)"
    )
    conn.execute(
        "CREATE INDEX calibration_native_activation_generation_idx ON "
        "calibration_native_ensemble_activation(station_id,artifact_kind,"
        "season_bin,lead_bucket,generation DESC,activation_id)"
    )
    return conn


def _activation(conn, activation_id, *, lead, generation, cycles) -> None:
    artifact_id = f"artifact-{activation_id}"
    conn.execute(
        "INSERT INTO calibration_cross_native_ensemble_artifact VALUES(?,?)",
        (artifact_id, json.dumps(sorted(cycles))),
    )
    conn.execute(
        "INSERT INTO calibration_native_ensemble_activation VALUES"
        "(?,?,?,?,?,?,?,?,?,?,?)",
        (activation_id, "CROSS_NATIVE_ENSEMBLE", artifact_id, "TEST", "PEAK",
         lead, "PRODUCTION_NO_LIVE_ONLY", generation,
         "2026-09-20T00:00:00Z", "2026-10-20T00:00:00Z", json.dumps(cycles)),
    )


def test_the_lead_probe_sees_a_fresh_seat_past_the_256th_row() -> None:
    """Charter 88's fresh-cycle preference must see every valid activation.

    h00_12 holds 300 re-minted seats of yesterday's cycle at high generations
    and one seat of today's run at generation 1; h12_24 holds a seat of
    today's run.  Under the 256-row cut the fresh h00_12 seat was invisible
    and the probe moved the packet to h12_24.
    """

    conn = _probe_store()
    stale = {"ECMWF": "00Z", "GEFS": "00Z"}
    fresh = {"ECMWF": "12Z", "GEFS": "00Z"}
    for index in range(300):
        _activation(conn, f"stale-{index:03d}", lead="h00_12",
                    generation=1000 + index, cycles=stale)
    _activation(conn, "fresh-h00", lead="h00_12", generation=1, cycles=fresh)
    _activation(conn, "fresh-h12", lead="h12_24", generation=1, cycles=fresh)
    chosen = P0._activated_lead_bucket(
        conn, station_id="TEST", season_bin="PEAK",
        decision_at_utc="2026-09-22T01:00:00Z",
        observed_system_ids=["ECMWF", "GEFS"],
        candidates=["h00_12", "h12_24"],
        packet_cycle_by_system={"ECMWF": "12Z", "GEFS": "00Z"},
        freshest_bucket_system_ids=["ECMWF"],
    )
    assert chosen == "h00_12"


def test_the_compile_seats_a_pool_seat_behind_300_newer_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seat the rank chooses is served however many rows sort above it.

    The fixture's pool is persisted and compiled once; then 300 copies of the
    served activation are added at generations 1000+, each naming a system
    set hash no packet can match, so every one is read and passed over.
    Under the 256-row cut they filled the read and the real seat was never
    seen.
    """

    from bayes_basket_target.contracts import content_sha256
    from bayes_basket_target.native_ensemble_oos import persist_native_ensemble_oos
    from tests.test_native_ensemble_p0 import _mixed_packet
    from bayes_basket_target import native_ensemble_oos
    from tests.test_native_ensemble_oos_p0_pipeline import _insert_fixture
    from tests.test_native_ensemble_p0 import (
        _insert_official_native_mixed_cycle_archive,
    )
    from tests.test_stable_pool_serving import (
        CYCLES, PROVIDERS, _build, _official,
    )

    store = sqlite3.connect(":memory:")
    _insert_fixture(store, cycle_by_system=CYCLES, lead_bucket="d2")
    _insert_official_native_mixed_cycle_archive(store, CYCLES)
    for system_id, cycle in CYCLES.items():
        monkeypatch.setitem(
            native_ensemble_oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS,
            system_id,
            {
                "transport_provider_id": PROVIDERS[system_id],
                "lead_bucket": "d2",
                "run_cycle": cycle,
            },
        )
    report = _build(store, "stable_pool")
    identity = {k: v for k, v in report.items() if k != "content_sha256"}
    identity["cross_candidates"] = _official(report)
    report = {**identity, "content_sha256": content_sha256(identity)}
    assert persist_native_ensemble_oos(
        store, report, activated_at_utc="2026-07-01T00:00:00Z"
    )["state"] == "COMMITTED"

    def _compile():
        compiled = P0.compile_native_ensemble_p0(
            store, forward_packet=_mixed_packet(),
            decision_at_utc="2026-07-01T13:00:00Z",
        )
        return compiled["activation_id"], compiled["cross_ensemble_artifact_id"]

    served_activation, served_artifact = _compile()
    columns = [
        row[1] for row in store.execute(
            "PRAGMA table_info(calibration_native_ensemble_activation)"
        )
    ]
    source = dict(zip(columns, store.execute(
        "SELECT * FROM calibration_native_ensemble_activation"
        " WHERE activation_id=?", (served_activation,),
    ).fetchone()))
    for index in range(300):
        row = dict(source)
        row["activation_id"] = f"decoy-{index:03d}"
        row["generation"] = 1000 + index
        row["available_system_set_hash_or_star"] = f"{index:064x}"
        row["content_sha256"] = f"{index + 1:064x}"
        store.execute(
            f"INSERT INTO calibration_native_ensemble_activation"
            f"({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            [row[name] for name in columns],
        )
    assert _compile() == (served_activation, served_artifact)


def test_the_bump_names_charter_198() -> None:
    from bayes_basket_target import serving_arithmetic as sa
    from bayes_basket_target.materialization import (
        materialize_p0m_observed,
        materialize_p1,
    )
    import inspect

    p0m = inspect.signature(materialize_p0m_observed).parameters[
        "posterior_version"
    ].default
    p1 = inspect.signature(materialize_p1).parameters[
        "posterior_version"
    ].default
    assert P0.POSTERIOR_VERSION == "calibrated_native_cross_ensemble_p0_v14"
    assert p0m == "p0m_taf_likelihood_v16"
    # Charter 200 moved P1 on to v20 (its own change); v19 keeps this
    # charter's moment.
    assert p1 == "p1_joint_likelihood_v25"
    # Charter 204 moved all three on again (P0 v10, P0-M v12, P1 v21); this
    # charter's versions keep their shared declaration.
    start = sa.version_effective_from("P0", "calibrated_native_cross_ensemble_p0_v9")
    assert start and start.endswith("Z") and start > "2026-09-22T00:00:00Z"
    assert sa.version_effective_from("P0-M", "p0m_taf_likelihood_v11") == start
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v19") == start
    assert sa.version_effective_from("P0", P0.POSTERIOR_VERSION) > start
    assert sa.version_effective_from("P0-M", p0m) > start
    assert sa.version_effective_from("P1", p1) > start
