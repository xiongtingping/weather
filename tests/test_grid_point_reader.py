"""The fast CONUS point reader may never return a value eccodes would not.

``GridPointReader`` replaces eccodes' per-(station, hour) nearest search --
1.24s a station on the NBM, about 600 seconds of every NBM P1 run on
2026-09-16 -- with one index per station per pass.  Three cheaper reads were
measured and refused first, and one of them, eccodes' own nearest ``indexes``
read back out of ``values``, returned the wrong cell at 5 of 12 NBM stations
by up to 10.6 K.  The NBM is ``alternativeRowScanning=1``: its odd rows run
the other way in ``values`` than in the latitudes/longitudes iterator.

So the reader derives its own index and maps it, and then does not trust the
mapping: the first message of each grid re-derives every station through
``nearest_point_kelvin`` and keeps an index only where the two agree exactly.

These tests hold that property on synthetic grids built in both scanning
orders, so they need neither the network nor a GRIB fixture.  The mapping was
proven bit-identical on real messages (44/44 HRRR, 72/72 NBM) on the day it
was written; what a test must hold forever is that a mismatch, a raise, or
an unfamiliar layout sends the point back to the slow read.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import noaa_conus_archive as A  # noqa: E402
from bayes_basket_target.contracts import ContractError  # noqa: E402

NX, NY = 5, 4


def _grid(alternate: bool):
    """Iterator-order lat/lon, and ``values`` stored the way GRIB stores them."""

    lats = np.repeat(np.linspace(30.0, 45.0, NY), NX)          # row-major
    lons = np.tile(np.linspace(-100.0, -80.0, NX), NY) % 360.0
    truth = 270.0 + np.arange(NX * NY, dtype=np.float64)       # distinct per cell
    stored = truth.copy()
    if alternate:
        for row in range(NY):
            if row % 2 == 1:
                stored[row * NX:(row + 1) * NX] = truth[row * NX:(row + 1) * NX][::-1]
    return lats, lons, truth, stored


def _install(monkeypatch, *, alternate: bool, layout=None, reference=None):
    lats, lons, truth, stored = _grid(alternate)
    base = {
        b"gridDefinitionTemplateNumber": 30, b"iScansNegatively": 0,
        b"jScansPositively": 1, b"bitmapPresent": 0,
        b"alternativeRowScanning": 1 if alternate else 0, b"Nx": NX,
    }
    base.update(layout or {})
    arrays = {b"latitudes": lats, b"longitudes": lons, b"values": stored}
    monkeypatch.setattr(A.GridPointReader, "_long", staticmethod(lambda lib, h, k: base[k]))
    monkeypatch.setattr(A.GridPointReader, "_array", staticmethod(lambda lib, h, k: arrays[k]))
    searched: list[tuple[float, float]] = []

    def eccodes_search(message, *, latitude, longitude):
        searched.append((latitude, longitude))
        cell = int(np.argmin((lats - latitude) ** 2 + ((lons - longitude % 360.0)) ** 2))
        value = float(truth[cell])
        return (reference(value) if reference else value), 1.0

    monkeypatch.setattr(A, "nearest_point_kelvin", eccodes_search)
    return lats, lons, truth, stored, searched


def _points_on_cells(lats, lons, cells):
    return [(float(lats[c]), float(lons[c]) - 360.0) for c in cells]


CELLS = [0, 3, 6, 9, 12, 17, 19]   # rows 0..3, both parities


@pytest.mark.parametrize("alternate", [False, True])
def test_derived_indexes_read_the_value_eccodes_would(monkeypatch, alternate):
    """In either scanning order, every kept index reads the eccodes value."""

    lats, lons, truth, stored, _ = _install(monkeypatch, alternate=alternate)
    reader = A.GridPointReader(_points_on_cells(lats, lons, CELLS))
    indexes = reader._derive(None, None, b"msg")
    assert None not in indexes, indexes
    assert [float(stored[i]) for i in indexes] == [float(truth[c]) for c in CELLS]


def test_the_row_flip_is_what_makes_alternate_scanning_agree(monkeypatch):
    """Without the flip, odd rows would read their mirror image.

    The mapping was chosen by measurement -- flipping none, odd or even rows
    on 72 real NBM values -- so the test pins the one that agreed.
    """

    assert [A._values_index(k, 4, True) for k in range(12)] == [
        0, 1, 2, 3, 7, 6, 5, 4, 8, 9, 10, 11,
    ]
    assert [A._values_index(k, 4, False) for k in range(12)] == list(range(12))


def test_a_value_that_disagrees_with_eccodes_is_read_slowly(monkeypatch):
    """The whole safety of the fast path: agreement is required, not assumed."""

    lats, lons, *_ = _install(
        monkeypatch, alternate=True, reference=lambda v: v + 0.05,
    )
    reader = A.GridPointReader(_points_on_cells(lats, lons, CELLS))
    assert reader._derive(None, None, b"msg") == [None] * len(CELLS)


def test_a_point_eccodes_refuses_stays_refused(monkeypatch):
    """Where the slow read raises today, the fast one must not invent a value."""

    lats, lons, *_ = _install(monkeypatch, alternate=False)
    refused = _points_on_cells(lats, lons, [6])[0]
    real = A.nearest_point_kelvin

    def sometimes_refuse(message, *, latitude, longitude):
        if (latitude, longitude) == refused:
            raise ContractError("noaa_conus_value_out_of_range")
        return real(message, latitude=latitude, longitude=longitude)

    monkeypatch.setattr(A, "nearest_point_kelvin", sometimes_refuse)
    points = _points_on_cells(lats, lons, [0, 6, 12])
    indexes = A.GridPointReader(points)._derive(None, None, b"msg")
    assert indexes[1] is None and indexes[0] is not None and indexes[2] is not None


@pytest.mark.parametrize("layout", [
    {b"gridDefinitionTemplateNumber": 0},     # regular lat/lon, not Lambert
    {b"bitmapPresent": 1},
    {b"iScansNegatively": 1},
    {b"jScansPositively": 0},
    {b"alternativeRowScanning": 2},
])
def test_an_unfamiliar_layout_never_takes_the_fast_path(monkeypatch, layout):
    """A layout nobody measured is not guessed at -- and costs no search."""

    lats, lons, _t, _s, searched = _install(monkeypatch, alternate=False, layout=layout)
    reader = A.GridPointReader(_points_on_cells(lats, lons, CELLS))
    assert reader._derive(None, None, b"msg") == [None] * len(CELLS)
    assert searched == []


def test_one_grid_is_proven_once_per_pass(monkeypatch):
    """The search is paid on the first message of a grid, not every hour."""

    lats, lons, *_ = _install(monkeypatch, alternate=True)
    derived: list[str] = []
    real_derive = A.GridPointReader._derive

    def counting_derive(self, library, handle, message):
        derived.append("x")
        return real_derive(self, library, handle, message)

    class _Lib:
        def codes_handle_new_from_message(self, *_a): return 1
        def codes_handle_delete(self, *_a): return 0

    monkeypatch.setattr(A, "_eccodes", lambda: _Lib())
    monkeypatch.setattr(A.GridPointReader, "_grid_identity", staticmethod(lambda lib, h: "grid-1"))
    monkeypatch.setattr(A.GridPointReader, "_derive", counting_derive)
    reader = A.GridPointReader(_points_on_cells(lats, lons, CELLS))
    for _hour in range(6):
        values = reader.kelvin(b"message")
        assert None not in values
    assert derived == ["x"]


def test_the_p1_fetch_falls_back_and_range_checks_what_it_takes(monkeypatch):
    """The lane consumes the reader the way it consumed decode_field.

    A point the reader could not prove goes to decode_field, unchanged; a
    fast value outside the plausible range raises the same contract error
    the slow read raises.
    """

    from bayes_basket_target import regional_point_lane as L

    targets = [
        {"station_id": "KAAA", "latitude": 35.0, "longitude": -90.0,
         "event_date": date(2026, 9, 16), "utc_offset_hours": -5},
        {"station_id": "KBBB", "latitude": 40.0, "longitude": -85.0,
         "event_date": date(2026, 9, 16), "utc_offset_hours": -5},
    ]
    monkeypatch.setattr(A, "product_hours", lambda product, run: [6])
    monkeypatch.setattr(A, "local_day_hours", lambda **_k: [6])
    monkeypatch.setattr(A, "_field_range", lambda url: (0, 1))
    monkeypatch.setattr(A, "_range_get", lambda url, s, e: b"grib")
    slow: list[str] = []

    def decode_field(message, *, latitude, longitude, run_init, forecast_hour, url):
        slow.append((latitude, longitude))
        return A.ConusField("x", forecast_hour, 12.5, 1.0, url, 4)

    monkeypatch.setattr(A, "decode_field", decode_field)

    class Reader:
        def __init__(self, points):
            pass

        def kelvin(self, message):
            return [290.0, None]           # fast for KAAA, slow for KBBB

    monkeypatch.setattr(A, "GridPointReader", Reader)
    run = datetime(2026, 9, 16, 0, tzinfo=timezone.utc)
    out = L.fetch_noaa_run_for_targets(product="HRRR", run_init=run, targets=targets)
    assert out[("KAAA", "2026-09-16")]["hourly_c"] == {6: pytest.approx(290.0 - 273.15)}
    assert out[("KBBB", "2026-09-16")]["hourly_c"] == {6: 12.5}
    assert slow == [(40.0, -85.0)]

    class OutOfRange(Reader):
        def kelvin(self, message):
            return [999.0, None]

    monkeypatch.setattr(A, "GridPointReader", OutOfRange)
    with pytest.raises(ContractError, match="noaa_conus_value_out_of_range"):
        L.fetch_noaa_run_for_targets(product="HRRR", run_init=run, targets=targets)
