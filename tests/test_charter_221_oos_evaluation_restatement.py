"""Charter 221: an OOS evaluation that says again what its scope last said
is not written again.

Every scan re-evaluated every scope and appended a row; 52% of consecutive
same-scope rows differed from their predecessor only in ``evaluated_at_utc``
(about 1.1 GB/day at full speed).  Readers take the latest row at or before
their clock, so such a row changes no answer.  A row that differs in any
column is still written, and a key evaluated twice in one scan is written
whole so the readers' refusal of a clock tie is kept.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    _EVALUATION_READER_KEY,
    build_native_ensemble_oos,
    persist_native_ensemble_oos_evaluations,
)
from tests.test_stable_pool_serving import store  # noqa: E402,F401

TABLE = "calibration_cross_native_ensemble_oos_evaluation"


def _report(store):
    return build_native_ensemble_oos(
        store, station_id="TEST", as_of_utc="2026-07-01T00:00:00Z",
        membership_mode="stable_pool",
    )


def _resealed(report):
    body = {key: value for key, value in report.items() if key != "content_sha256"}
    return {**body, "content_sha256": content_sha256(body)}


def _count(store) -> int:
    return int(store.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])


def _latest(store, key_row, decision_at):
    """The P0 reader's own query (native_ensemble_p0)."""

    return store.execute(
        f"SELECT oos_state,selection_evidence_sha256,evaluated_at_utc FROM {TABLE} WHERE "
        + " AND ".join(f"{c}=?" for c in _EVALUATION_READER_KEY)
        + " AND evaluated_at_utc<=? ORDER BY evaluated_at_utc DESC,evaluation_id DESC LIMIT 2",
        (*key_row, decision_at),
    ).fetchall()


def test_an_unchanged_rescan_writes_nothing_and_readers_see_the_same_answer(store) -> None:
    report = _report(store)
    first = persist_native_ensemble_oos_evaluations(
        store, report, evaluated_at_utc="2026-07-01T00:00:00Z",
    )
    written = _count(store)
    assert first["oos_evaluations"] == written > 0
    keys = store.execute(
        f"SELECT {','.join(_EVALUATION_READER_KEY)} FROM {TABLE}"
    ).fetchall()
    before = {key: _latest(store, key, "2026-07-01T00:30:00Z")[0][:2] for key in keys}

    again = persist_native_ensemble_oos_evaluations(
        store, report, evaluated_at_utc="2026-07-01T00:20:00Z",
    )
    assert again["oos_evaluations"] == 0
    assert again["oos_evaluation_restatements_skipped"] == written
    assert _count(store) == written
    after = {key: _latest(store, key, "2026-07-01T00:30:00Z")[0][:2] for key in keys}
    assert after == before


def test_a_changed_evaluation_is_still_written(store) -> None:
    report = _report(store)
    persist_native_ensemble_oos_evaluations(
        store, report, evaluated_at_utc="2026-07-01T00:00:00Z",
    )
    written = _count(store)
    changed = copy.deepcopy(report)
    candidate = changed["cross_candidates"][0]
    candidate["oos_metrics"] = {
        **candidate["oos_metrics"],
        "brier": float(candidate["oos_metrics"]["brier"]) + 0.01,
    }
    receipt = persist_native_ensemble_oos_evaluations(
        store, _resealed(changed), evaluated_at_utc="2026-07-01T00:20:00Z",
    )
    assert receipt["oos_evaluations"] == 1
    assert receipt["oos_evaluation_restatements_skipped"] == written - 1
    assert _count(store) == written + 1


def test_a_backdated_ingest_is_judged_as_of_its_own_clock(store) -> None:
    report = _report(store)
    persist_native_ensemble_oos_evaluations(
        store, report, evaluated_at_utc="2026-07-01T00:00:00Z",
    )
    written = _count(store)
    # Nothing at or before this clock: every evaluation is new evidence then.
    receipt = persist_native_ensemble_oos_evaluations(
        store, report, evaluated_at_utc="2026-06-30T12:00:00Z",
    )
    assert receipt["oos_evaluations"] == written
    assert _count(store) == 2 * written


def test_a_key_evaluated_twice_in_one_scan_is_written_whole(store) -> None:
    report = _report(store)
    twin = copy.deepcopy(report["cross_candidates"][0])
    twin["threshold_profile"] = str(twin["threshold_profile"]) + "_TWIN"
    report = _resealed({**report, "cross_candidates": [*report["cross_candidates"], twin]})
    persist_native_ensemble_oos_evaluations(
        store, report, evaluated_at_utc="2026-07-01T00:00:00Z",
    )
    written = _count(store)
    receipt = persist_native_ensemble_oos_evaluations(
        store, report, evaluated_at_utc="2026-07-01T00:20:00Z",
    )
    # The twinned key is written again (both rows), so its clock tie stays a
    # tie for the readers; every other key is a restatement.
    assert receipt["oos_evaluations"] == 2
    assert receipt["oos_evaluation_restatements_skipped"] == written - 2


def test_the_reader_key_is_the_one_the_p0_reader_selects_by() -> None:
    source = Path(native_ensemble_oos.__file__).with_name("native_ensemble_p0.py").read_text()
    for column in _EVALUATION_READER_KEY:
        assert f"{column}=?" in source
