"""The middle of a colon chain is where the subject lives.

The fail-closed line used to print the first segment and the last, on the
reasoning that these reasons are chains whose informative end is last.  True
of the chain it was written for -- ``target_ssot_read_only_open_failed:
ResourceBoundError:target_database_wal_checkpoint_busy`` -- and false of a
chain whose outer and inner conditions are the SAME phrase.

Measured 2026-09-10:

    生产回执 fail-closed：blocked_fail_closed·
    official_ws_evidence_complete_scope_missing…no_registry_scope

off a reason that ran ``…scope_missing:1 skipped:<station>:ContractError:
…scope_missing:no_registry_scope``.  Both slots spent on one phrase, and the
station -- the only datum saying what to fix -- dropped between them.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


def _note(reason: str) -> str:
    return C._production_health({"production": {
        "ok": False, "state": "blocked_fail_closed", "reason": reason,
        "stage_timings_ms": {"cycle_total": 0.1},
    }})["production_note"]


def test_the_station_in_the_middle_survives():
    note = _note(
        "official_ws_evidence_complete_scope_missing:1 skipped:KLGA:"
        "ContractError:official_ws_evidence_complete_scope_missing:"
        "no_registry_scope"
    )

    assert "KLGA" in note
    assert "no_registry_scope" in note
    assert "1 skipped" in note
    # The phrase said once, not twice.
    assert note.count("official_ws_evidence_complete_scope_missing") == 1
    # A bare exception class between two conditions is the noise.
    assert "ContractError" not in note


def test_the_chain_this_was_written_for_still_names_what_to_fix():
    note = _note(
        "target_ssot_read_only_open_failed:ResourceBoundError:"
        "target_database_wal_checkpoint_busy"
    )

    assert "target_ssot_read_only_open_failed" in note
    assert "target_database_wal_checkpoint_busy" in note


def test_a_chain_of_nothing_but_exception_classes_still_prints_something():
    """Dropping every segment would leave the line saying only its state."""

    note = _note("ContractError:RuntimeError")

    assert "ContractError" in note or "RuntimeError" in note


def test_a_single_segment_reason_is_printed_whole():
    assert "plain_single_segment_reason" in _note("plain_single_segment_reason")


def test_a_chain_too_long_to_print_keeps_its_head_and_its_last_two():
    long_reason = ":".join(["seg%02d_%s" % (i, "x" * 12) for i in range(20)])
    note = _note(long_reason)

    assert "…" in note
    assert "seg00" in note
    assert "seg19" in note
    assert "seg18" in note
    assert len(note) < 220
