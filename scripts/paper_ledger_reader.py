"""The production paper book, for the scripts that read it (2026-09-19, 「移出」).

The ledger's four ``paper_trade_*`` tables left the SSOT for
``data/paper_ledger.db``; a connection to the SSOT reads them after
``attach_paper_ledger(conn)``.  Imported by name from ``scripts/`` so a
script that never set up the package path can still reach the one helper.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bayes_basket_target.paper_ledger_store import attach_paper_ledger  # noqa: E402

__all__ = ["attach_paper_ledger"]
