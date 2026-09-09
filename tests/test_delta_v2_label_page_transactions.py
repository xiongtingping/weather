"""Paging bounds the work only if the transaction is inside the loop.

Measured on the live resident 2026-09-09: the ``delta_v2_settlement_labels``
savepoint held the writer lock for 561.6 seconds in a single call.  Not a slow
statement -- eight pages, each fine on its own, wrapped in one transaction, so
the lock was held for their sum.  It was 59% of every millisecond the writer
lock was held that cycle, inside a cycle whose target is 300 seconds and which
ran 3,400.

The rule this file pins is the one the incident taught: a loop that pages in
order to bound its work must commit each page, or the bound applies to the
work and not to the lock -- which is the thing anybody else is waiting on.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import production_worker  # noqa: E402


def _label_pass_source() -> str:
    """The body of the label pass, located by its savepoint name."""

    source = Path(
        production_worker.__file__
    ).read_text(encoding="utf-8")
    anchor = source.index('savepoint="delta_v2_settlement_labels"')
    start = source.rindex("page_results: list[dict[str, Any]] = []", 0, anchor)
    end = source.index("except Exception as exc:", anchor)
    return source[start:end]


def test_the_page_transaction_opens_inside_the_page_loop():
    body = _label_pass_source()
    loop = body.index("for _page_number in range(")
    txn = body.index("with _writer_transaction(")
    assert loop < txn, (
        "the writer transaction opens before the page loop, so eight pages "
        "are held under one lock -- the shape that cost 561.6s on 2026-09-09"
    )


def test_the_cursor_advances_per_committed_page():
    """Not after the loop.

    Advancing only after all eight pages meant a raise on page eight
    discarded the seven that had committed, and the next pass redid them.
    """

    body = _label_pass_source()
    loop = body.index("for _page_number in range(")
    cursor = body.index("self._delta_v2_label_fact_cursor = committed_cursor")
    assert loop < cursor, "the cursor is advanced outside the page loop"
    # And it is advanced after the transaction closes, never inside it: a
    # cursor moved before its page is durable would skip that page on the
    # next pass.
    txn = body.index("with _writer_transaction(")
    materialize = body.index("materialize_delta_v2_settled_training_labels")
    assert txn < materialize < cursor


def test_the_writer_transaction_takes_a_real_lock_when_it_is_outermost():
    """Why moving it inside the loop actually releases anything.

    ``_writer_transaction`` degrades to SAVEPOINT when a transaction is
    already open -- a savepoint releases nothing -- so this only helps
    because the label pass is the outermost writer.  Pinned so a future
    caller that wraps the pass in its own transaction has to notice that it
    puts the 561-second hold straight back.
    """

    source = inspect.getsource(production_worker._writer_transaction)
    assert "own_transaction = not conn.in_transaction" in source
    assert re.search(r"if own_transaction:\s*\n\s*_begin_writer_lock", source)
    assert 'conn.execute(f"SAVEPOINT {savepoint}")' in source


def test_every_page_is_bounded_before_it_is_taken():
    """The memory budget check stays per page, not per pass."""

    body = _label_pass_source()
    loop = body.index("for _page_number in range(")
    budget = body.index("assert_process_memory_budget()")
    assert loop < budget, "the per-page budget assertion left the loop"
