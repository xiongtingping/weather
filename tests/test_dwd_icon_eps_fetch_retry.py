"""An ICON object cut mid-transfer is fetched again in place.

2026-09-18: DWD closed the connection 21.4 of 36.3 MB into one of the fifteen
objects a capture pulls, the whole ICON family failed, and every P0 for the
next day went out on ICON's run of the day before for more than seven hours.
"""
from __future__ import annotations

import httpx
import pytest

from bayes_basket_target import dwd_icon_eps_archive as icon
from bayes_basket_target.contracts import ContractError

URL = "https://opendata.dwd.de/weather/nwp/icon-eps/grib/00/t_2m/object.grib2.bz2"


def _client(responses: list) -> tuple[httpx.Client, list[int]]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        outcome = responses[min(len(calls), len(responses)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_a_cut_transfer_is_fetched_again_and_succeeds() -> None:
    client, calls = _client([
        httpx.RemoteProtocolError("peer closed connection without sending complete message body"),
        httpx.Response(200, content=b"grib-bytes"),
    ])
    slept: list[float] = []
    assert icon._fetch(client, URL, sleep=slept.append) == b"grib-bytes"
    assert len(calls) == 2
    assert slept == [icon.FETCH_TRANSPORT_BACKOFF_SEC[0]]


def test_an_unpublished_object_is_not_retried() -> None:
    client, calls = _client([httpx.Response(404)])
    with pytest.raises(ContractError, match="icon_eps_archive_object_unavailable"):
        icon._fetch(client, URL, sleep=lambda _s: None)
    assert len(calls) == 1


def test_a_transfer_that_keeps_failing_still_fails_after_the_bound() -> None:
    client, calls = _client([httpx.ReadTimeout("slow")] * 5)
    with pytest.raises(httpx.ReadTimeout):
        icon._fetch(client, URL, sleep=lambda _s: None)
    assert len(calls) == icon.FETCH_TRANSPORT_ATTEMPTS


def test_each_icon_grid_is_cached_by_its_own_point_count(tmp_path, monkeypatch) -> None:
    """2026-10-06: DWD moved ICON-EPS from 737,280 to 1,310,720 grid points
    with the 06Z run; one hard-coded count failed every run after that.  Each
    grid is read and cached under its own size, the first grid keeps its root
    files, the station map is cached, and an unknown size fails closed."""

    import pytest

    from bayes_basket_target import dwd_icon_eps_archive as A
    from bayes_basket_target.contracts import ContractError

    fetched: list[str] = []

    def fake_fetch(_client, url):
        fetched.append(url)
        return b"x"

    def fake_field(_library, _raw):
        n = 1310720
        field = "clat" if "clat" in fetched[-1] else "clon"
        # Every point far away except point 7, which sits on the station.
        base = 80.0 if field == "clat" else 170.0
        values = [base] * n
        values[7] = 30.578 if field == "clat" else 103.947  # ZUUU
        return values

    monkeypatch.setattr(A, "_fetch", fake_fetch)
    monkeypatch.setattr(A, "_single_field", fake_field)
    monkeypatch.setattr(A.bz2, "decompress", lambda raw: raw)
    got = A._grid_index(None, None, run_day="20261006", cycle="06",
                        station_ids=["ZUUU"], cache_dir=tmp_path, point_count=1310720)
    assert got == {"ZUUU": 7}
    assert (tmp_path / "1310720" / "clat.f64").stat().st_size == 1310720 * 8
    assert not (tmp_path / "clat.f64").exists()
    calls = len(fetched)
    # The station map is cached: no fetch and no search the second time.
    assert A._grid_index(None, None, run_day="20261007", cycle="00",
                         station_ids=["ZUUU"], cache_dir=tmp_path,
                         point_count=1310720) == {"ZUUU": 7}
    assert len(fetched) == calls
    with pytest.raises(ContractError, match="grid_point_count_invalid"):
        A._grid_index(None, None, run_day="20261006", cycle="06",
                      station_ids=["ZUUU"], cache_dir=tmp_path, point_count=999)
