from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.calibration_ingress import ingest_calibration_base_facts
from bayes_basket_target.contracts import ContractError


@pytest.mark.parametrize("commit", [False, True])
def test_legacy_calibration_writer_is_retired_and_zero_write(commit: bool) -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        before = conn.total_changes

        with pytest.raises(
            ContractError, match="calibration_legacy_ingress_retired"
        ):
            ingest_calibration_base_facts(
                conn,
                {"schema": "bayes_basket_target_calibration_evidence_v1"},
                commit=commit,
            )

        assert conn.total_changes == before
        assert conn.execute("SELECT COUNT(*) FROM sentinel").fetchone()[0] == 0


def test_legacy_calibration_writer_is_not_top_level_exported() -> None:
    import bayes_basket_target

    assert not hasattr(bayes_basket_target, "ingest_calibration_base_facts")
