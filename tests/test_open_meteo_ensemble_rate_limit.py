"""A rate-limited fetch must wait and retry, not fail the scope outright.

Each request here asks for every registered ensemble model at once, so it costs
the provider many call units rather than one.  Widening the rotation catalogue
multiplied that, and 429 became a normal operating condition -- while being the
one failure the retry loop could not survive: ``raise_for_status`` raises an
``HTTPStatusError``, and only transport and timeout errors were caught.
"""

from __future__ import annotations

import types

import pytest

import bayes_basket_target.open_meteo_ensemble as open_meteo_ensemble
from bayes_basket_target.contracts import ContractError


class _Response:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b'{"ok": 1}',
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise open_meteo_ensemble.httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=self
            )

    def iter_bytes(self):
        yield self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class _Client:
    responses: list[_Response] = []
    calls = 0

    def __init__(self, **_kwargs) -> None:
        pass

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def stream(self, _method: str, _url: str) -> _Response:
        response = _Client.responses[
            min(_Client.calls, len(_Client.responses) - 1)
        ]
        _Client.calls += 1
        return response


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch):
    """Drive the retry loop with scripted responses and a recorded clock."""

    slept: list[float] = []
    monkeypatch.setattr(open_meteo_ensemble.httpx, "Client", _Client)
    monkeypatch.setattr(
        open_meteo_ensemble,
        "time",
        types.SimpleNamespace(sleep=lambda seconds: slept.append(round(seconds, 2))),
    )

    def _run(responses: list[_Response]):
        _Client.responses = responses
        _Client.calls = 0
        slept.clear()
        return slept

    return _run


def test_rate_limited_request_is_retried_until_it_succeeds(transport) -> None:
    slept = transport([_Response(429), _Response(429), _Response(200)])

    payload, sha256 = open_meteo_ensemble._bounded_get_json(
        "https://example.invalid/ensemble", timeout_seconds=1.0
    )

    assert payload == {"ok": 1}
    assert len(sha256) == 64
    assert _Client.calls == 3
    # Waited between attempts rather than hammering: retrying a rate limit with
    # no delay spends the quota that is refusing us.
    assert slept == [2.0, 4.0]


def test_retry_after_header_is_preferred_over_guessing(transport) -> None:
    # The header is the provider saying exactly when its window reopens.
    slept = transport(
        [_Response(429, headers={"Retry-After": "3"}), _Response(200)]
    )

    open_meteo_ensemble._bounded_get_json(
        "https://example.invalid/ensemble", timeout_seconds=1.0
    )

    assert slept == [3.0]


def test_retry_after_is_capped_so_one_scope_cannot_hold_the_rotation(
    transport,
) -> None:
    slept = transport(
        [_Response(429, headers={"Retry-After": "3600"}), _Response(200)]
    )

    open_meteo_ensemble._bounded_get_json(
        "https://example.invalid/ensemble", timeout_seconds=1.0
    )

    assert slept == [open_meteo_ensemble.MAX_RETRY_BACKOFF_SECONDS]


def test_persistent_rate_limit_reports_itself_as_one(transport) -> None:
    # It used to surface as a bare HTTPStatusError, which reads as a broken
    # request rather than a quota that will reopen.
    slept = transport([_Response(429)])

    with pytest.raises(ContractError, match="open_meteo_ensemble_rate_limited:429"):
        open_meteo_ensemble._bounded_get_json(
            "https://example.invalid/ensemble", timeout_seconds=1.0
        )

    assert _Client.calls == open_meteo_ensemble.MAX_TRANSPORT_ATTEMPTS
    # No wait after the final attempt: the scope is given up either way, and
    # sleeping first only holds a rotation slot open for a retry that will not
    # happen.
    assert len(slept) == open_meteo_ensemble.MAX_TRANSPORT_ATTEMPTS - 1


def test_a_client_error_is_not_retried(transport) -> None:
    # A 400 will be a 400 next time; only the statuses that mean "later" are
    # worth another unit of quota.
    slept = transport([_Response(400)])

    with pytest.raises(open_meteo_ensemble.httpx.HTTPStatusError):
        open_meteo_ensemble._bounded_get_json(
            "https://example.invalid/ensemble", timeout_seconds=1.0
        )

    assert _Client.calls == 1
    assert slept == []


def test_server_errors_share_the_rate_limit_treatment(transport) -> None:
    slept = transport([_Response(503), _Response(200)])

    open_meteo_ensemble._bounded_get_json(
        "https://example.invalid/ensemble", timeout_seconds=1.0
    )

    assert _Client.calls == 2
    assert slept == [2.0]
    assert 503 in open_meteo_ensemble.RETRYABLE_HTTP_STATUS_CODES


def test_only_global_models_are_requested() -> None:
    # The provider prices a request by (variables x models x days), so asking
    # for all eighteen registered systems costs roughly eighteen units per
    # station-day.  Seven are regional and returned nothing outside their own
    # domain -- billed, then discarded into unavailable_products.
    assert set(open_meteo_ensemble.GLOBAL_ENSEMBLE_SYSTEMS).issubset(
        open_meteo_ensemble.ENSEMBLE_SYSTEMS
    )
    assert len(open_meteo_ensemble.GLOBAL_ENSEMBLE_SYSTEMS) == 11
    assert all(
        config.get("model_scope") == "global"
        for config in open_meteo_ensemble.GLOBAL_ENSEMBLE_SYSTEMS.values()
    )
    # The registry stays complete: retained packets and the history/previous-run
    # callers look systems up by id, including the regional ones.
    regional = set(open_meteo_ensemble.ENSEMBLE_SYSTEMS) - set(
        open_meteo_ensemble.GLOBAL_ENSEMBLE_SYSTEMS
    )
    assert "DWD_ICON_D2_EPS" in regional
    assert "UKMO_UK_ENS" in regional


def test_every_registered_system_states_its_scope() -> None:
    # An unstated scope used to default to "global", which both billed the
    # regional models everywhere and labelled them global in the packet.
    assert all(
        config.get("model_scope") in {"global", "regional"}
        for config in open_meteo_ensemble.ENSEMBLE_SYSTEMS.values()
    )


def test_rate_limit_diagnostics_name_the_window_that_was_hit() -> None:
    # A 429 that reports only its status code cannot be acted on: cutting
    # models, slowing the sweep and raising the quota are three different
    # fixes, and which applies depends on whether the minutely, hourly or
    # daily window was exceeded.  The provider says so in the body.
    response = _Response(
        429,
        headers={
            "Retry-After": "12",
            "X-RateLimit-Limit-Minute": "600",
            "Content-Type": "application/json",
        },
        body=b'{"error": true, "reason": "Minutely API request limit exceeded."}',
    )
    response.content = response._body

    diagnostics = open_meteo_ensemble.rate_limit_diagnostics(response)

    assert diagnostics["reason"] == "Minutely API request limit exceeded."
    assert diagnostics["retry-after"] == "12"
    assert diagnostics["x-ratelimit-limit-minute"] == "600"
    # Unrelated headers stay out of the receipt.
    assert "content-type" not in diagnostics


def test_rate_limit_diagnostics_never_raise_on_a_hostile_response() -> None:
    # This runs on a failure path; a diagnostic that throws would replace a
    # useful rate-limit error with a confusing one.
    class _Hostile:
        @property
        def headers(self):
            raise RuntimeError("no headers")

        @property
        def content(self):
            raise RuntimeError("no body")

    assert open_meteo_ensemble.rate_limit_diagnostics(_Hostile()) == {}


def test_persistent_rate_limit_carries_the_reason_into_the_error(
    transport,
) -> None:
    body = b'{"error": true, "reason": "Daily API request limit exceeded."}'
    response = _Response(429, body=body)
    response.content = body
    transport([response])

    with pytest.raises(ContractError, match="Daily API request limit exceeded"):
        open_meteo_ensemble._bounded_get_json(
            "https://example.invalid/ensemble", timeout_seconds=1.0
        )
