"""The fitted tables the basket's compile reads, as of a moment (charter 230).

One mapping, keyed by lane: the L4 posterior rows (``POSTERIOR_TABLE_KEY``).
Production reads it with ``as_of_utc`` None, the paper ledger with the
decision's own timestamp, so a historical decision is priced on the rows that
stood then.  The beta lane's and the pooled valuation's rows travelled here as
shadow readings until charter 233 retired both lanes.

Charter 68's effective-edge lambda tables used to travel in this mapping
(``effective_edge_store.load_effective_edge_tables``); charter 230 retired
that lane, and the store with it.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .basket_kernel.posterior_v1 import POSTERIOR_TABLE_KEY


def load_served_tables(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    """Every lane's served rows as of ``as_of_utc`` (None: the active rows)."""

    from .posterior_store import load_posterior_tables  # noqa: PLC0415

    return {
        POSTERIOR_TABLE_KEY: load_posterior_tables(conn, as_of_utc=as_of_utc),
    }


__all__ = ["load_served_tables"]
