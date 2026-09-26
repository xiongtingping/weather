"""The fitted tables the basket's compile reads, as of a moment (charter 230).

One mapping, keyed by lane: the L4 posterior rows (``POSTERIOR_TABLE_KEY``,
the basket's one belief) and the two shadow readings recorded beside it, the
beta lane (``BETA_TABLE_KEY``) and the pooled valuation
(``VALUATION_TABLE_KEY``).  Production reads it with ``as_of_utc`` None, the
paper ledger with the decision's own timestamp, so a historical decision is
priced on the rows that stood then.

Charter 68's effective-edge lambda tables used to travel in this mapping
(``effective_edge_store.load_effective_edge_tables``); charter 230 retired
that lane, and the store with it.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .basket_kernel.delta_v2 import BETA_TABLE_KEY
from .basket_kernel.posterior_v1 import POSTERIOR_TABLE_KEY
from .basket_kernel.valuation_v1 import VALUATION_TABLE_KEY


def load_served_tables(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    """Every lane's served rows as of ``as_of_utc`` (None: the active rows)."""

    from .beta_edge_store import load_beta_tables  # noqa: PLC0415
    from .posterior_store import load_posterior_tables  # noqa: PLC0415
    from .valuation_store import load_valuation_tables  # noqa: PLC0415

    return {
        POSTERIOR_TABLE_KEY: load_posterior_tables(conn, as_of_utc=as_of_utc),
        BETA_TABLE_KEY: load_beta_tables(conn, as_of_utc=as_of_utc),
        VALUATION_TABLE_KEY: load_valuation_tables(conn, as_of_utc=as_of_utc),
    }


__all__ = ["load_served_tables"]
