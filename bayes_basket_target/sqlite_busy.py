"""Tell SQLite's two "database is locked" faults apart, once, for everybody.

``sqlite3.OperationalError: database is locked`` is the same sentence for two
unrelated faults, and telling them apart decides whether waiting longer can
ever help:

  * ``SQLITE_BUSY`` (5) -- another connection holds the file's write lock and
    this one waited out its whole ``busy_timeout``.  Retrying is the right
    answer: the holder is making progress and will commit.
  * ``SQLITE_BUSY_SNAPSHOT`` (517) -- this connection already holds a read
    snapshot that someone else has since written past, so the read cannot be
    promoted to a write.  SQLite returns this *immediately* and never invokes
    the busy handler, because no amount of waiting fixes a stale snapshot.
    Only ending the read does, and the read belongs to a caller the failing
    statement cannot reach.

Measured: with a pinned cursor and a competing committer, BEGIN IMMEDIATE
fails in 0.000s with 517 while busy_timeout is 60000, and neither
``rollback()``, ``commit()``, nor a raw ROLLBACK releases it -- only closing
or draining the cursor does.

Lives here rather than inside the production worker because the capture lanes
need the same distinction and a second copy of it would drift: on 2026-09-15
``capture_jma_msm_forward`` and ``capture_cma_grapes_forward`` each died on a
contended INSERT, and the lane could not say whether it had been starved of a
lock (retry) or had poisoned its own snapshot (do not).  The caller supplies
the noun -- production names its faults ``target_writer_*`` -- so what is
shared is the classification, not the vocabulary.
"""

from __future__ import annotations

import contextlib
import sqlite3

SQLITE_BUSY_PRIMARY = 5
SQLITE_BUSY_SNAPSHOT = 517

#: The kind returned when this connection's own read snapshot is the blocker.
#: Deterministic until the caller's read ends, so retrying only burns clock.
STALE_READ_SNAPSHOT = "stale_read_snapshot"
#: The kind returned when another connection holds the write lock and this
#: one waited out its ``busy_timeout``.  A retry can win.
LOCK_STARVED = "lock_starved"
#: The kind returned when the message says "locked" or "busy" but the
#: interpreter carried no extended code, so the kind was inferred.
LOCK_UNAVAILABLE = "lock_unavailable"


def busy_kind(exc: BaseException) -> tuple[str, int] | None:
    """Name a lock failure by its extended result code, or None if unrelated.

    The code is returned beside the name so a reader can tell a measured
    classification from an inferred one: older interpreters do not carry
    ``sqlite_errorcode``, and those answers come back with code 0.
    """

    if not isinstance(exc, sqlite3.OperationalError):
        return None
    code = int(getattr(exc, "sqlite_errorcode", 0) or 0)
    if code == SQLITE_BUSY_SNAPSHOT:
        return (STALE_READ_SNAPSHOT, code)
    if code == SQLITE_BUSY_PRIMARY:
        return (LOCK_STARVED, code)
    # Older interpreters do not carry the extended code.  Fall back to the
    # message so the blocker is still named rather than opaque, and mark the
    # code 0 so a reader knows the kind was inferred, not measured.
    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
        return (LOCK_UNAVAILABLE, 0)
    return None


class WriteBlocked(RuntimeError):
    """This pass could not take the SSOT write lock, so it stopped.

    Not a fault of the lane: another writer -- a production cycle, a paper
    ledger round re-deriving the whole book -- held the file's write lock
    for longer than the connection's ``busy_timeout``.  It is raised instead
    of letting ``sqlite3.OperationalError`` travel because the two need
    opposite handling.  A capture that cannot store a run has nothing to
    gain from fetching the next one, and a great deal to lose from dying:
    on 2026-09-15 ``capture_jma_msm_forward`` spent 955 seconds of ranged
    reads and ``capture_cma_grapes_forward`` 115, and both ended on this
    exception with their pass aborted, their un-committed runs discarded,
    and their lane's clock reading ``failed`` for the next three and six
    hours.  A missed cycle is permanent for the products whose transport
    serves only the run it currently holds.
    """

    def __init__(self, kind: str, code: int) -> None:
        super().__init__(f"{kind}:sqlite_errorcode={code}")
        self.kind = str(kind)
        self.code = int(code)


def begin_immediate_if_idle(conn: sqlite3.Connection) -> bool:
    """Open the connection's transaction by taking the write lock first.

    A ``SAVEPOINT`` on a connection with no transaction opens a DEFERRED one:
    it reads on a snapshot and asks for the write lock only at its first
    write.  A batch that looks a row up before deciding to write it --
    content-addressed replays, "already current" quotes -- can read for a
    long stretch before that first write, and if any other connection
    commits in the stretch the write fails at once with
    ``SQLITE_BUSY_SNAPSHOT`` (517, "database is locked"); ``busy_timeout``
    is never consulted.  Measured on the SSOT 2026-09-26: three production
    cycles of two hundred ended that way inside the market-evidence and
    forward-TAF batches (07:30, 10:00, 12:40Z), each before its decision
    loop ran.  ``BEGIN IMMEDIATE`` takes the lock up front -- waiting out
    ``busy_timeout`` like any writer -- so the reads and the writes share one
    snapshot and no promotion can fail.

    Returns whether it opened the transaction; the caller then owns its
    ``COMMIT``/``ROLLBACK``.  A connection already inside a transaction is
    left as it is: the caller's transaction and its lock are the caller's.
    """

    if conn.in_transaction:
        return False
    conn.execute("BEGIN IMMEDIATE")
    return True


@contextlib.contextmanager
def blocked_when_busy():
    """Re-raise a contended-lock fault inside as ``WriteBlocked``.

    Anything else -- a CHECK constraint, a UNIQUE, a missing column -- is
    left alone: those are the lane's own faults and must keep travelling as
    themselves.  Only "somebody else has the file" is converted, because
    only that one means "stop this pass, not this lane".
    """

    try:
        yield
    except sqlite3.OperationalError as exc:
        kind = busy_kind(exc)
        if kind is None:
            raise
        raise WriteBlocked(*kind) from exc


def write_row(
    conn: sqlite3.Connection, sql: str, params: "tuple[object, ...]"
) -> sqlite3.Cursor:
    """Run one write and commit it; ``WriteBlocked`` if the lock was not free.

    The capture lanes all have the same shape -- fetch a run, write its row,
    commit, fetch the next -- and it is what lets the resident release
    production from waiting on them.  It also means every one of them has a
    write that can meet a contended file, and three of them had spelled that
    write out three times with no guard at all.
    """

    try:
        with blocked_when_busy():
            cursor = conn.execute(sql, params)
            conn.commit()
    except WriteBlocked:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    return cursor


__all__ = [
    "LOCK_STARVED",
    "LOCK_UNAVAILABLE",
    "SQLITE_BUSY_PRIMARY",
    "SQLITE_BUSY_SNAPSHOT",
    "STALE_READ_SNAPSHOT",
    "WriteBlocked",
    "begin_immediate_if_idle",
    "blocked_when_busy",
    "busy_kind",
    "write_row",
]
