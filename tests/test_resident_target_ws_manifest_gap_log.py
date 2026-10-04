"""The publisher logs a manifest write gap, stamped, with what spanned it.

2026-10-04 15:42-15:43Z and 15:50-15:51Z the books stopped moving for over
50 s and 38 scopes died on production_quote_expired; market_ws.log could not
say why, because no line carried a time.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import resident_target_ws_publisher as pub  # noqa: E402


def _reset() -> None:
    pub._PUBLISH_TRACE.update(last_write=0.0, connections=0.0, slowest_scope_read=0.0)


def test_a_gap_past_the_threshold_is_logged_with_its_connections_and_reads(capsys) -> None:
    _reset()
    pub._note_manifest_write(100.0)
    pub._note_manifest_write(105.0)
    assert capsys.readouterr().out == ""
    pub._PUBLISH_TRACE["connections"] = 3
    pub._PUBLISH_TRACE["slowest_scope_read"] = 41.5
    pub._note_manifest_write(105.0 + pub.MANIFEST_GAP_REPORT_SECONDS + 30.0)
    line = capsys.readouterr().out.strip()
    stamp, message = line.split(" ", 1)
    assert stamp.endswith("Z") and "T" in stamp
    assert message == "[market_ws][target_exact] manifest_gap:50.0s connections:3 slowest_scope_read:41.5s"
    # The write that reports a gap starts the next count.
    assert pub._PUBLISH_TRACE["connections"] == 0.0
    assert pub._PUBLISH_TRACE["slowest_scope_read"] == 0.0


def test_the_first_write_reports_nothing(capsys) -> None:
    _reset()
    pub._note_manifest_write(10_000.0)
    assert capsys.readouterr().out == ""


def test_a_slow_catalog_read_is_logged_and_kept(monkeypatch, capsys) -> None:
    _reset()
    clock = iter([0.0, pub.SCOPE_PROBE_REPORT_SECONDS + 1.0])
    monkeypatch.setattr(pub.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(pub, "_scope", lambda **_: {"tokens": []})
    assert pub._timed_scope(db_path=Path("x"), as_of_utc="2026-10-04T00:00:00Z") == {"tokens": []}
    assert "slow_scope_read:6.0s" in capsys.readouterr().out
    assert pub._PUBLISH_TRACE["slowest_scope_read"] == pub.SCOPE_PROBE_REPORT_SECONDS + 1.0
