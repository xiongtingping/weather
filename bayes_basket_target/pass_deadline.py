"""One wall clock for a capture pass, checked where the requests are.

A capture lane's per-pass seconds budget is checked between units of work
-- between station-days, between runs -- and one unit is many ranged
requests, each retrying against an ``httpx`` timeout that is a
BETWEEN-BYTES bound, not a total.  A mirror that trickles never trips it.
Measured 2026-09-10 on the MSM lane: a SUCCESSFUL pass took 16m59s at 0%
CPU, and the same lane's clock read ``timeout_killed`` at its 1,800-second
subprocess budget having written nothing.  A bound between fetches does not
bound a fetch.

So the deadline lives here, armed once per pass, and the archive clients
check it before each request and before each retry -- never mid-response,
so a request that has started is allowed to finish and nothing is left
half read.  What is unreached is simply still missing, which the next pass
plans again.

Thread-local: a capture process arms it once and exits, and a test or a
caller in another thread is never bounded by somebody else's clock.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from .contracts import ContractError


class PassDeadlineExceeded(ContractError):
    """The pass ran out of wall clock inside a fetch, not between them.

    Not a fault.  A caller reports it as a spent budget, never beside
    network failures -- that would make a healthy lane read as a failing
    one -- and stops, because continuing hands the next unit a clock that is
    already gone.
    """


_DEADLINE_LOCAL = threading.local()


@contextmanager
def pass_deadline(seconds: float | None) -> Iterator[None]:
    """Bound every request this thread issues by one wall clock."""

    previous = getattr(_DEADLINE_LOCAL, "deadline", None)
    _DEADLINE_LOCAL.deadline = (
        None if seconds is None else time.monotonic() + float(seconds)
    )
    try:
        yield
    finally:
        _DEADLINE_LOCAL.deadline = previous


def set_pass_deadline(seconds: float | None) -> None:
    """Arm the deadline for this thread without wrapping the caller in a block.

    A ``with`` around a whole ``main`` would re-indent working code to bound
    it, and re-indenting working code is how a bounding change becomes a
    behaviour change.  A one-shot capture process arms this once and exits.
    """

    _DEADLINE_LOCAL.deadline = (
        None if seconds is None else time.monotonic() + float(seconds)
    )


def deadline_spent() -> bool:
    deadline = getattr(_DEADLINE_LOCAL, "deadline", None)
    return deadline is not None and time.monotonic() >= deadline


def seconds_left() -> float | None:
    """What the caller has left, so a receipt can say it stopped for time."""

    deadline = getattr(_DEADLINE_LOCAL, "deadline", None)
    return None if deadline is None else deadline - time.monotonic()


__all__ = [
    "PassDeadlineExceeded",
    "deadline_spent",
    "pass_deadline",
    "seconds_left",
    "set_pass_deadline",
]


# The writer lease.  The resident admits one SSOT writer at a time and hands
# the slot to the hosted sweeps between production cycles, so however long a
# sweep runs is how long production waits.  Each sweep used to size its own
# pass from its own constants (900s, 1200s, 1500s), each one patched after
# the incident it caused, and nothing at the scheduler said what a slot may
# cost.  The resident now says it, once, in this environment variable when it
# starts a sweep; a lane with a pass budget clamps to it here and asks for
# the next pass with ``hosted_sweep:continue`` when work is left.
WRITER_LEASE_ENV = "PM_WRITER_LEASE_SEC"
# The line a sliced sweep prints when it stopped inside its lease with work
# left; the resident brings it due again after a short delay instead of its
# whole interval.  The daemon and the paper ledger spell the same line.
HOSTED_SWEEP_CONTINUE_MARKER = "hosted_sweep:continue"


def writer_lease_seconds(default: float) -> float:
    """``default`` clamped to the writer lease the resident granted, if any.

    Unset, empty or unparsable means "no lease" -- a pass run by hand keeps
    its own budget -- and a lease below zero is refused the same way.
    """

    raw = os.environ.get(WRITER_LEASE_ENV, "").strip()
    if not raw:
        return float(default)
    try:
        lease = float(raw)
    except ValueError:
        return float(default)
    if lease <= 0.0:
        return float(default)
    return min(float(default), lease)
