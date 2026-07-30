from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bayes_basket_target.contracts import (
    ContractError,
    assert_timestamp_not_future,
)


def test_evidence_clock_accepts_current_or_past_timestamp() -> None:
    now = datetime(2026, 7, 26, 20, 48, tzinfo=timezone.utc)

    assert (
        assert_timestamp_not_future(
            "2026-07-26T20:48:00Z",
            now_utc=now,
        )
        == "2026-07-26T20:48:00Z"
    )
    assert (
        assert_timestamp_not_future(
            "2026-07-26T20:47:59Z",
            now_utc=now,
        )
        == "2026-07-26T20:47:59Z"
    )


def test_evidence_clock_rejects_future_timestamp() -> None:
    now = datetime(2026, 7, 26, 20, 48, tzinfo=timezone.utc)

    with pytest.raises(ContractError, match="as_of_utc_in_future"):
        assert_timestamp_not_future(
            "2026-07-26T20:48:01Z",
            now_utc=now,
        )
