"""A failed account read must say WHICH failure, and must not carry a zero.

The account row is the one line on the board that is not counted off our
own tables: it is what the exchange says is resting right now.  On
2026-09-10 two consecutive renders carried
``{"available": false, "count": 0}`` and the card printed
"网关无响应：unknown" -- for a gateway that had answered.  The reader kept
only ``bool(ok)`` and dropped the reply's own ``error``, so the single
field that said what went wrong never left the function, and the two
distinct failures (nothing answered / the gateway answered and said no)
were indistinguishable on the page.

The account held one live order the whole time -- an order the ledger had
lost the handle to, resting since the previous day.

Two properties are pinned here:

* a read that did not succeed carries no ``count``, because a zero next to
  a failed read is how a reader learns to believe an unread account is
  empty; and
* the three failures are three different ``reason`` values, each carrying
  the evidence that distinguishes it.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


class _Body(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _answering(monkeypatch, payload: dict) -> None:
    def fake(_request, timeout=None):  # noqa: ARG001
        return _Body(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake)


def _raising(monkeypatch, exc: Exception) -> None:
    def fake(_request, timeout=None):  # noqa: ARG001
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", fake)


def test_a_complete_ok_enumeration_is_the_only_thing_that_counts(monkeypatch):
    _answering(monkeypatch, {
        "ok": True, "complete": True, "total_open_orders": 1,
        "open_orders": [{
            "id": "0xabc", "side": "BUY", "price": "0.16",
            "original_size": "7", "size_matched": "0", "outcome": "Yes",
            "created_at": 1788961099,
        }],
    })
    out = C._read_open_orders()
    assert out["available"] is True
    assert out["reason"] == "ok"
    assert out["count"] == 1
    assert out["orders"][0]["id"] == "0xabc"


def test_a_gateway_that_answered_no_is_not_reported_as_no_answer(monkeypatch):
    _answering(monkeypatch, {
        "ok": False, "status": "unknown", "error": "upstream_429_rate_limited",
    })
    out = C._read_open_orders()
    assert out["available"] is False
    assert out["reason"] == "refused"
    # The gateway's own account of the failure survives the frame.
    assert "upstream_429_rate_limited" in out["error"]
    assert "count" not in out


def test_ok_false_without_an_error_still_names_itself(monkeypatch):
    _answering(monkeypatch, {"ok": False})
    out = C._read_open_orders()
    assert out["reason"] == "refused"
    assert out["error"] == "ok_false_without_error"
    assert "count" not in out


def test_a_truncated_enumeration_is_not_a_count_of_the_account(monkeypatch):
    _answering(monkeypatch, {
        "ok": True, "complete": False, "pages_checked": 3,
        "total_open_orders": 12, "open_orders": [],
    })
    out = C._read_open_orders()
    assert out["available"] is False
    assert out["reason"] == "incomplete"
    # A floor, said out loud as a floor.
    assert "floor" in out["error"]
    assert "count" not in out


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("connection refused"),
    urllib.error.HTTPError("u", 500, "boom", {}, None),
])
def test_nothing_answering_carries_the_message_not_just_the_class(
    monkeypatch, exc,
):
    _raising(monkeypatch, exc)
    out = C._read_open_orders()
    assert out["available"] is False
    assert out["reason"] == "unreachable"
    # HTTPError alone cannot tell a vanished route from a handler that
    # threw; the status is the whole diagnosis and it must be in the text.
    assert type(exc).__name__ in out["error"]
    assert out["error"] != type(exc).__name__
    assert "count" not in out


def test_every_failure_is_stamped_with_when_we_asked(monkeypatch):
    _raising(monkeypatch, urllib.error.URLError("down"))
    assert C._read_open_orders()["asked_at"]
    _answering(monkeypatch, {"ok": False, "error": "nope"})
    assert C._read_open_orders()["asked_at"]


def test_the_card_has_a_sentence_for_every_reason_the_reader_emits():
    """A rename on one side leaves the other printing "读取失败"."""

    source = Path(C.__file__).read_text(encoding="utf-8")
    card = source.split("const O2WHY = {", 1)[1].split("}", 1)[0]
    for reason in ("unreachable", "refused", "incomplete"):
        assert f"{reason}:" in card, reason
