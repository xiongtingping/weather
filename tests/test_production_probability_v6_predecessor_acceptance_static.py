"""What survives of the v6 predecessor acceptance check.

One assertion read scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh
to prove the cutover called the acceptance script.  That cutover finished
and its script was deleted on 2026-08-04, so the assertion could only
raise FileNotFoundError.  Retired 2026-09-07 with its subject.

The four assertions about the acceptance script itself are kept: they are
about what the running system refuses, not about how it was installed.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_predecessor_acceptance_refuses_stale_and_unconsumed_revisions() -> None:
    predecessor = (
        ROOT / "scripts/production_probability_v6_predecessor_acceptance.py"
    ).read_text(encoding="utf-8")

    assert '"active_v6_weak_source_snapshot_stale"' in predecessor
    assert '"active_v6_source_time_freshness_invalid"' in predecessor
    assert "pending_canonical_event_count" in predecessor
    assert "stale_newer_revision_not_consumed" in predecessor
