"""A capture made during the cycle must be older than the decision instant.

`compile_native_ensemble_p0` rejects a packet whose `captured_at` is later than
`decision_at_utc` with `native_ensemble_p0_future_capture_forbidden`.  The
resident now captures the official-native packet inside the same cycle, and that
capture takes minutes -- so reusing the cycle's opening `as_of_utc` as the
decision instant would put the capture in the future and reject every packet the
resident fetched for itself.
"""

import inspect
import re

from bayes_basket_target import production_worker


SOURCE = inspect.getsource(production_worker.TargetProductionWorker.refresh_resident_nwp)


def test_decision_clock_is_rereread_after_an_in_cycle_capture() -> None:
    assert "native_decision_at_utc" in SOURCE
    # The refreshed clock must come from a new reading, not from as_of_utc.
    block = SOURCE[SOURCE.index("native_decision_at_utc = ") :][:400]
    assert "utc_now()" in block


def test_refreshed_clock_applies_only_to_a_completed_capture() -> None:
    block = SOURCE[SOURCE.index("native_decision_at_utc = ") :][:400]

    # No capture this cycle -> keep the cycle's as-of, so unrelated consumers
    # and retained packets are still judged against one consistent instant.
    assert '"CAPTURED"' in block
    assert "else as_of_utc" in block


def test_official_compile_uses_the_refreshed_clock() -> None:
    call = SOURCE[SOURCE.index("_compile_native_ensemble_p0_runtime(") :][:400]

    assert "decision_at_utc=native_decision_at_utc" in call


def test_capture_result_is_reported_in_cycle_status() -> None:
    full = inspect.getsource(production_worker)

    # Operators need to see whether a scope was captured, throttled or failed.
    assert '"official_native_capture": official_capture,' in full
