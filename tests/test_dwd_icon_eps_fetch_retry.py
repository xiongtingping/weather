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
