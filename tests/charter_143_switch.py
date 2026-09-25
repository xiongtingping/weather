"""The charter-143 switch, set where every reader of it looks.

The kernel and the runtime each bind ``CHARTER_143_COMPLETION`` at import.
A test that means the pre-143 completion rule (charters 112/116/130/136,
66(A), 71) has to set both, or it judges one half of the pipeline under one
rule and the other half under the other.
"""

from __future__ import annotations

import pytest

from bayes_basket_target import delta_v2_runtime
from bayes_basket_target.basket_kernel import delta_v2 as kernel


def set_charter_143(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    assert mode in {"on", "off"}
    monkeypatch.setattr(kernel, "CHARTER_143_COMPLETION", mode)
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_143_COMPLETION", mode)
