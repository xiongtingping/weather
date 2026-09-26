"""The acceptance class the beta lane's readout was drawn on (charter 128).

The beta lane's board section left the page on 2026-09-26 (user: 「检查统一估值车道」
「全面检查旧的内容」): it was a shadow no rule read, drawn from payloads frozen
since 2026-09-12.  The class itself is production code the candidate-window
evidence still records, so its boundary stays pinned here.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_the_class_is_a_confident_model_against_a_cheap_market() -> None:
    from bayes_basket_target import candidate_window_evidence as ev  # noqa: PLC0415

    assert ev.in_acceptance_class(0.45, 0.29)
    assert not ev.in_acceptance_class(0.44, 0.29)
    assert not ev.in_acceptance_class(0.45, 0.30)
    assert not ev.in_acceptance_class(None, 0.1)
    assert not ev.in_acceptance_class(0.9, None)
    assert not ev.in_acceptance_class("x", 0.1)
