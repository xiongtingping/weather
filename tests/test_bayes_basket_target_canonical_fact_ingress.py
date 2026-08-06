from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.canonical_fact_ingress import (
    RETIRED_REASON,
    canonical_observation_fact_row,
    ingest_canonical_observation_evidence,
    stage_canonical_observation_evidence,
)
from bayes_basket_target.contracts import ContractError


@pytest.mark.parametrize(
    "call",
    (
        lambda conn: canonical_observation_fact_row({}),
        lambda conn: ingest_canonical_observation_evidence(conn, {}, commit=True),
        lambda conn: stage_canonical_observation_evidence(conn, {}),
    ),
)
def test_legacy_projection_ingress_is_retired_fail_closed(call) -> None:
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ContractError, match=RETIRED_REASON):
        call(conn)
    assert conn.in_transaction is False
