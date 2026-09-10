"""A bound between fetches does not bound a fetch.

``capture_jma_msm_forward`` got a per-pass seconds budget on 2026-09-10, and
it did not stop the lane being ``timeout_killed``: the budget is checked
between station-days, and ONE station-day is many ranged requests, each
retrying four times against ``httpx.Timeout(60.0)``.

That sixty seconds is a BETWEEN-BYTES timeout, not a total.  A mirror that
trickles never trips it.  Measured 2026-09-10: a SUCCESSFUL pass took 16m59s
at 0% CPU -- 8,568 KB per station-day, about 36 KB/s -- while the same lane's
clock read ``timeout_killed`` at its 1,800-second subprocess budget, having
written nothing and held the exclusive SSOT writer slot for the whole half
hour.  A single range's worst case is four attempts plus backoff, roughly 252
seconds, which is enough on its own to outlast the subprocess.

So the deadline lives where the requests are, and is checked before each
request and before each retry backoff.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import jma_msm_archive as archive  # noqa: E402
from bayes_basket_target.jma_msm_archive import (  # noqa: E402
    JmaMsmPassDeadlineExceeded,
    pass_deadline,
    pass_deadline_seconds_left,
    set_pass_deadline,
)

URL = "https://example.invalid/msm.bin"


class _Client:
    def __init__(self, *, raises: int = 0, status: int = 206) -> None:
        self.calls = 0
        self.raises = raises
        self.status = status

    def get(self, url, headers=None):  # noqa: ARG002
        self.calls += 1
        if self.calls <= self.raises:
            raise httpx.ConnectError("dropped")
        return httpx.Response(self.status, content=b"0123456789")


@pytest.fixture(autouse=True)
def _no_real_client(monkeypatch):
    monkeypatch.setattr(archive, "close_client", lambda: None)
    yield
    set_pass_deadline(None)


def test_an_unarmed_pass_behaves_exactly_as_before(monkeypatch):
    client = _Client()
    monkeypatch.setattr(archive, "_client", lambda: client)

    assert archive._range(URL, 0, 10) == b"0123456789"
    assert client.calls == 1
    assert pass_deadline_seconds_left() is None


def test_a_spent_deadline_refuses_before_issuing_the_request(monkeypatch):
    """Not after.  The point is to not start work the pass cannot finish."""

    client = _Client()
    monkeypatch.setattr(archive, "_client", lambda: client)

    with pass_deadline(0.0):
        with pytest.raises(JmaMsmPassDeadlineExceeded):
            archive._range(URL, 0, 10)

    assert client.calls == 0, "a spent pass still dialled the mirror"


class _SlowClient:
    """Burns real clock inside the request, the way the mirror does.

    The clock must be alive at the top of the attempt and spent by the time
    the handler runs -- that is the branch under test, and patching
    ``time.sleep`` to reach it would also freeze the clock the deadline
    reads.
    """

    def __init__(self, *, burn: float, raises: bool = False, status: int = 206):
        self.calls = 0
        self.burn = burn
        self.raises = raises
        self.status = status

    def get(self, url, headers=None):  # noqa: ARG002
        self.calls += 1
        time.sleep(self.burn)
        if self.raises:
            raise httpx.ConnectError("dropped")
        return httpx.Response(self.status, content=b"0123456789")


def test_a_deadline_that_runs_out_mid_retry_stops_before_the_backoff(
    monkeypatch,
):
    """Four attempts and twelve seconds of backoff is the worst case this
    bound exists to cut short; discovering the clock is spent must not cost
    another sleep on top."""

    client = _SlowClient(burn=0.06, raises=True)
    monkeypatch.setattr(archive, "_client", lambda: client)

    started = time.monotonic()
    with pass_deadline(0.05):
        with pytest.raises(JmaMsmPassDeadlineExceeded):
            archive._range(URL, 0, 10)
    elapsed = time.monotonic() - started

    assert client.calls == 1, "kept retrying past the deadline"
    assert elapsed < archive.RANGE_BACKOFF_SECONDS, elapsed


def test_a_retryable_status_also_checks_the_clock_before_sleeping(monkeypatch):
    client = _SlowClient(burn=0.06, status=503)
    monkeypatch.setattr(archive, "_client", lambda: client)

    started = time.monotonic()
    with pass_deadline(0.05):
        with pytest.raises(JmaMsmPassDeadlineExceeded):
            archive._range(URL, 0, 10)
    elapsed = time.monotonic() - started

    assert client.calls == 1
    assert elapsed < archive.RANGE_BACKOFF_SECONDS, elapsed


def test_a_range_already_in_flight_is_allowed_to_finish(monkeypatch):
    """Checked before the request, never mid-response: a half-read range
    would be a half-written station-day."""

    client = _Client()
    monkeypatch.setattr(archive, "_client", lambda: client)

    with pass_deadline(60.0):
        body = archive._range(URL, 0, 10)

    assert body == b"0123456789"
    assert client.calls == 1


def test_the_deadline_is_restored_and_is_this_threads_own():
    import threading

    seen: list[float | None] = []
    with pass_deadline(30.0):
        outer = pass_deadline_seconds_left()
        with pass_deadline(5.0):
            assert pass_deadline_seconds_left() <= 5.0
        assert pass_deadline_seconds_left() > 5.0 <= outer

        thread = threading.Thread(
            target=lambda: seen.append(pass_deadline_seconds_left())
        )
        thread.start()
        thread.join()

    assert seen == [None], "another thread inherited this pass's clock"
    assert pass_deadline_seconds_left() is None
