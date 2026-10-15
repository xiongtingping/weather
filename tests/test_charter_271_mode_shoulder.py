"""Charter 271: the width layer carries a mode-to-shoulder share per horizon bin
(user ruling 2026-10-16 「做」).

One transform, one resolver for serving, the gate and the fit; the share is
applied after the pair and the slice's tail mix and before the far-peak
ceiling; it rides on the width row's sha (a zero share keeps the old sha);
the nightly lane fits one share per bin on the stage's own cells, admits it
under the stage's ranked verdict and a fold majority, and vetoes an
incumbent that stopped earning.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import center_width_store as store
from bayes_basket_target.center_width_calibration import (
    MAX_MODE_SHOULDER_SHARE,
    MODE_SHOULDER_BINS,
    MODE_SHOULDER_SHARE_GRID,
    apply_center_width_calibration,
    apply_mode_shoulder,
    apply_peak_ceiling,
    apply_tail_mix,
    mode_shoulder_bin_for,
    mode_shoulder_share_for,
)
from bayes_basket_target.served_shape import (
    CenterWidthLayer,
    apply_served_table_layers,
    with_mode_shoulder,
    with_peak_ceiling,
)

KEYS = ["23C or below", "24C", "25C", "26C", "27C", "28C", "29C or higher"]
CENTERS = [22.5, 24.0, 25.0, 26.0, 27.0, 28.0, 29.5]
PEAKED = dict(zip(KEYS, [0.02, 0.05, 0.15, 0.50, 0.20, 0.06, 0.02]))
TABLE = {"bins": {"h24_48": {"share": 0.10}, "h48_plus": {"share": 0.15}}}


def test_the_constants_and_the_bins() -> None:
    assert MODE_SHOULDER_SHARE_GRID == (0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30)
    assert MAX_MODE_SHOULDER_SHARE == 0.30
    assert [name for name, _l, _u in MODE_SHOULDER_BINS] == ["h0_24", "h24_48", "h48_plus"]
    assert mode_shoulder_bin_for(3.0) == "h0_24"
    assert mode_shoulder_bin_for(24.0) == "h24_48"
    assert mode_shoulder_bin_for(47.9) == "h24_48"
    assert mode_shoulder_bin_for(60.0) == "h48_plus"
    assert mode_shoulder_bin_for(None) is None and mode_shoulder_bin_for(-1.0) is None
    assert mode_shoulder_share_for(TABLE, hours_to_settlement=30.0) == ("h24_48", 0.10)
    assert mode_shoulder_share_for(TABLE, hours_to_settlement=5.0) is None
    assert mode_shoulder_share_for(None, hours_to_settlement=30.0) is None
    with pytest.raises(ValueError):
        mode_shoulder_share_for({"bins": {"h24_48": {"share": 0.5}}}, hours_to_settlement=30.0)


def test_the_share_moves_mode_mass_to_the_shoulders_by_their_ratio() -> None:
    out, receipt = apply_mode_shoulder(PEAKED, share=0.10, ordered_keys=KEYS)
    assert receipt["state"] == "APPLIED" and receipt["mode_bucket"] == "26C"
    assert sum(out.values()) == pytest.approx(1.0)
    assert out["26C"] == pytest.approx(0.45)
    # 0.05 moved, split 0.15 : 0.20 between 25C and 27C.
    assert out["25C"] == pytest.approx(0.15 + 0.05 * 0.15 / 0.35)
    assert out["27C"] == pytest.approx(0.20 + 0.05 * 0.20 / 0.35)
    assert receipt["warm_share"] == pytest.approx(0.20 / 0.35)
    for key in ("23C or below", "24C", "28C", "29C or higher"):
        assert out[key] == pytest.approx(PEAKED[key])
    same, ident = apply_mode_shoulder(PEAKED, share=0.0, ordered_keys=KEYS)
    assert ident["state"] == "IDENTITY" and same == pytest.approx(PEAKED)


def test_edges_ruled_out_shoulders_and_refusals() -> None:
    edge = dict(zip(KEYS, [0.60, 0.20, 0.10, 0.05, 0.03, 0.01, 0.01]))
    out, receipt = apply_mode_shoulder(edge, share=0.20, ordered_keys=KEYS)
    assert out["23C or below"] == pytest.approx(0.48) and out["24C"] == pytest.approx(0.32)
    assert receipt["shoulder_buckets"] == ["24C"]
    out, receipt = apply_mode_shoulder(PEAKED, share=0.10, ordered_keys=KEYS, excluded_buckets=["25C"])
    assert out["25C"] == pytest.approx(0.15) and out["27C"] == pytest.approx(0.25)
    with pytest.raises(ValueError):
        apply_mode_shoulder(PEAKED, share=0.31, ordered_keys=KEYS)
    with pytest.raises(ValueError):
        apply_mode_shoulder(PEAKED, share=0.1, ordered_keys=KEYS[:-1])


def test_serving_order_pair_mix_share_ceiling_and_the_receipt() -> None:
    base = CenterWidthLayer(delta_c=0.0, width_lambda=0.75)
    layer = with_peak_ceiling(with_mode_shoulder(base, TABLE, hours_to_settlement=40.0),
                              hours_to_settlement=40.0, enabled=True)
    assert layer.mode_shoulder_bin == "h24_48" and layer.mode_shoulder_share == 0.10
    assert with_mode_shoulder(None, TABLE, hours_to_settlement=40.0) is None
    near = with_mode_shoulder(base, TABLE, hours_to_settlement=10.0)
    assert near is base and near.mode_shoulder_share is None
    # The table carried on the layer is read when no table is passed (the gate's path).
    carried = CenterWidthLayer(width_lambda=0.75, mode_shoulder_table=TABLE)
    assert with_mode_shoulder(carried, None, hours_to_settlement=40.0).mode_shoulder_share == 0.10
    served, receipt = apply_served_table_layers(PEAKED, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=layer)
    pair, _ = apply_center_width_calibration(PEAKED, delta_c=0.0, width_lambda=0.75, ordered_keys=KEYS, key_centers_c=CENTERS)
    shouldered, _ = apply_mode_shoulder(pair, share=0.10, ordered_keys=KEYS)
    expected, _ = apply_peak_ceiling(shouldered, ceiling=0.45, ordered_keys=KEYS)
    assert all(served[k] == pytest.approx(expected[k]) for k in KEYS)
    assert receipt["mode_shoulder_bin"] == "h24_48" and receipt["mode_shoulder_share"] == pytest.approx(0.10)
    assert receipt["mode_shoulder"]["state"] == "APPLIED"
    _, plain = apply_served_table_layers(PEAKED, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=base)
    assert plain["mode_shoulder_share"] is None and plain["mode_shoulder"] is None


def test_the_row_sha_carries_the_shares_and_round_trips(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "cw.db")
    plain = store.center_width_sha256(stage="P0", delta_c=0.0, width_lambda=0.75)
    zero = store.center_width_sha256(stage="P0", delta_c=0.0, width_lambda=0.75,
                                     mode_shoulder={"bins": {"h24_48": {"share": 0.0}}})
    shared = store.center_width_sha256(stage="P0", delta_c=0.0, width_lambda=0.75, mode_shoulder=TABLE)
    assert plain == zero and shared != plain
    committed = store.persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.0, width_lambda=0.75, evidence={"schema": "t"},
        serving_day_count=9, mode_shoulder=TABLE,
    )
    assert committed["center_width_sha256"] == shared
    loaded = store.load_center_width_mode_shoulder(conn, stage="P0")
    assert loaded["bins"] == {"h24_48": {"share": 0.1}, "h48_plus": {"share": 0.15}}
    assert store.load_center_width_mode_shoulder(conn, stage="P0", table_sha256=shared) == loaded
    store.retire_center_width(conn, stage="P0", retired_at_utc="2026-10-20T00:00:00Z")
    assert store.load_center_width_mode_shoulder(conn, stage="P0", table_sha256=shared) is None


def _cell(dist, settled, *, hours, station, day):
    return {
        "probabilities": dict(dist), "ordered_keys": KEYS, "key_centers_c": CENTERS,
        "settled_bucket": settled, "excluded_buckets": (), "event_date": day,
        "station_id": station, "decision_at_utc": f"{day}T00:00:00Z",
        "seated_system_count": 6, "hours_to_settlement": hours,
    }


def _days(n, pattern):
    """Day-before cells that claim the mode too hard: the truth sits on a
    shoulder three times in five.  Same-day cells ride along on the mode."""
    by_day = {}
    for index in range(n):
        day = f"2026-09-{index + 1:02d}"
        cells = []
        for station in range(10):
            cells.append(_cell(PEAKED, pattern[(index + station) % len(pattern)], hours=40.0, station=f"S{station:02d}", day=day))
            cells.append(_cell(PEAKED, "26C", hours=6.0, station=f"N{station:02d}", day=day))
        by_day[day] = cells
    return by_day


def test_the_lane_admits_a_share_where_the_mode_overclaims_and_not_where_it_does_not() -> None:
    by_day = _days(14, ["25C", "27C", "26C", "27C", "25C"])
    with store._round_memo():
        lane = store._mode_shoulder_lane(
            by_day=by_day, days=sorted(by_day), served_residual=(0.0, 1.0),
            sigma_floor_rungs=0.0, incumbent=None, activated_at_utc=None,
        )
    assert lane["bins"]["h24_48"]["admitted"] is True
    assert lane["served_table"]["bins"]["h24_48"]["share"] > 0.0
    assert lane["bins"]["h0_24"]["admitted"] is False
    assert "h0_24" not in (lane["served_table"]["bins"])


def test_an_incumbent_share_is_vetoed_when_its_undo_wins() -> None:
    by_day = _days(14, ["26C"])
    for cells in by_day.values():
        for cell in cells:
            cell["center_width_receipt"] = {"mode_shoulder_share": 0.25}
            cell["reserved_under_current_width"] = True
    with store._round_memo():
        lane = store._mode_shoulder_lane(
            by_day=by_day, days=sorted(by_day), served_residual=(0.0, 1.0),
            sigma_floor_rungs=0.0,
            incumbent={"bins": {"h24_48": {"share": 0.25}}},
            activated_at_utc="2026-08-01T00:00:00Z",
        )
    assert lane["bins"]["h24_48"]["veto"]["vetoed"] is True
    assert lane["served_table"] is None


def test_a_served_but_not_reserved_cell_is_left_out() -> None:
    cell = _cell(PEAKED, "26C", hours=40.0, station="S", day="2026-09-01")
    assert store._cell_shoulder_eligible(cell)
    cell["center_width_receipt"] = {"mode_shoulder_share": 0.1}
    assert not store._cell_shoulder_eligible(cell)
    cell["reserved_under_current_width"] = True
    assert store._cell_shoulder_eligible(cell)


def test_every_face_resolves_the_share_through_one_resolver() -> None:
    root = Path(__file__).resolve().parents[1]
    read = lambda rel: (root / rel).read_text(encoding="utf-8")
    p0 = read("bayes_basket_target/native_ensemble_p0.py")
    assert "mode_shoulder_table = load_center_width_mode_shoulder(" in p0
    assert p0.index("center_width_layer = with_mode_shoulder(") < p0.index("center_width_layer = with_peak_ceiling(")
    oos = read("bayes_basket_target/native_ensemble_oos.py")
    assert 'load_center_width_mode_shoulder(conn, stage="P0")' in oos
    assert "layer = with_mode_shoulder(layer, None, hours_to_settlement=hours)" in oos
    assert "mode_shoulder_table=center_width_mode_shoulder" in oos
    asof = read("bayes_basket_target/table_as_of.py")
    assert '"mode_shoulder": load_center_width_mode_shoulder(' in asof
    served = read("bayes_basket_target/served_shape.py")
    assert served.index("apply_mode_shoulder(") < served.index("apply_peak_ceiling(")
    charter = read("docs/constitution/SYSTEM_CHARTER.md")
    line = next(l for l in charter.splitlines() if l.startswith("271. "))
    for word in ("apply_mode_shoulder", "MODE_SHOULDER_SHARE_GRID", "mode_shoulder_json", "v29", "v32", "v40", "裁决人：熊先生"):
        assert word in line, word
