"""Skip a test whose input is an audit output that is pruned by age.

``output/audits`` stopped being tracked in commit 10f91279 -- every
investigation and backfill script writes there, it had grown the repo
history past a gigabyte, and ``scripts/output_audits_retention.py`` now
prunes it by age.  The directory is ephemeral by design.

Several suites still read a specific artifact out of it and fail with
FileNotFoundError whenever the pruner has done its job, which is a red
test that says nothing about the code.  A test whose evidence has been
pruned has not failed; it has nothing to run against, and saying so is
the honest report.

This is deliberately a skip and not a softened assertion: when the
artifact is present the test runs exactly as before, and the assertion it
makes is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REASON = (
    "output/audits is untracked and age-pruned (commit 10f91279); "
    "regenerate the artifact with its own audit script to run this"
)


def requires_audit_artifacts(*paths: Path):
    """A marker that skips when any of these audit outputs is absent."""

    missing = [str(path) for path in paths if not Path(path).is_file()]
    return pytest.mark.skipif(
        bool(missing),
        reason=f"{REASON}: missing {', '.join(missing)}" if missing else REASON,
    )
