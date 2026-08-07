"""A frozen WS handoff must not pin the NWP capture scope to a closed day.

`event_day_state` in the resident handoff is captured at write time.  When the
manifest stopped refreshing on 2026-08-03 it kept advertising that day as open,
so it became the NWP capture event-date allowlist, every live registry scope was
filtered out, and capture fail-closed with
`resident_nwp_exact_station_scope_missing`.  With no new model data there was no
new P0, no new probability envelope and therefore no new manifest: the handoff
could never refresh itself.  Dropping certainly-closed dates lets the scope fall
back to live registry discovery and breaks that deadlock.
"""

import json
from pathlib import Path

import pytest

from bayes_basket_target.production_worker import (
    MULTI_SCOPE_WS_MANIFEST_SCHEMA,
    _resident_open_event_dates,
)


def _manifest(tmp_path: Path, *, event_dates: list[str], state: str = "open") -> Path:
    path = tmp_path / "ws_manifest.json"
    path.write_text(
        json.dumps({
            "schema": MULTI_SCOPE_WS_MANIFEST_SCHEMA,
            "scopes": [
                {
                    "scope_key": f"KMIA|{event_date}",
                    "station_id": "KMIA",
                    "event_date": event_date,
                    "event_day_state": state,
                }
                for event_date in event_dates
            ],
        }),
        encoding="utf-8",
    )
    return path


def test_same_day_handoff_is_used(tmp_path: Path) -> None:
    path = _manifest(tmp_path, event_dates=["2026-08-03"])

    assert _resident_open_event_dates(
        path, as_of_utc="2026-08-03T18:00:00Z"
    ) == ("2026-08-03",)


def test_next_day_is_still_allowed_because_offsets_span_utc_minus12_to_plus14(
    tmp_path: Path,
) -> None:
    path = _manifest(tmp_path, event_dates=["2026-08-03"])

    # One day of slack is deliberate: an event day can still be open somewhere
    # on earth the following UTC day.  Only drop what is closed everywhere.
    assert _resident_open_event_dates(
        path, as_of_utc="2026-08-04T18:00:00Z"
    ) == ("2026-08-03",)


@pytest.mark.parametrize(
    "as_of", ["2026-08-05T00:00:00Z", "2026-08-06T14:00:00Z", "2026-09-01T00:00:00Z"]
)
def test_certainly_closed_day_is_dropped_however_open_the_snapshot_claims(
    tmp_path: Path, as_of: str
) -> None:
    path = _manifest(tmp_path, event_dates=["2026-08-03"])

    # The snapshot still says "open"; the clock says otherwise, and the clock
    # wins.  Returning () lets live registry discovery supply the scope.
    assert _resident_open_event_dates(path, as_of_utc=as_of) == ()


def test_current_open_day_survives(tmp_path: Path) -> None:
    path = _manifest(tmp_path, event_dates=["2026-08-06"])

    assert _resident_open_event_dates(
        path, as_of_utc="2026-08-06T14:00:00Z"
    ) == ("2026-08-06",)


def test_closed_state_is_still_ignored(tmp_path: Path) -> None:
    path = _manifest(tmp_path, event_dates=["2026-08-06"], state="closed")

    assert _resident_open_event_dates(path, as_of_utc="2026-08-06T14:00:00Z") == ()


def test_multiple_open_days_still_refuse_to_guess(tmp_path: Path) -> None:
    path = _manifest(tmp_path, event_dates=["2026-08-06", "2026-08-07"])

    # Mixing station-days would turn the 45-station cap into a station-day cap.
    assert _resident_open_event_dates(path, as_of_utc="2026-08-06T14:00:00Z") == ()


def test_stale_day_dropped_leaves_a_single_current_day(tmp_path: Path) -> None:
    path = _manifest(tmp_path, event_dates=["2026-08-03", "2026-08-06"])

    # Dropping the closed date resolves what would otherwise be an ambiguous
    # multi-day handoff.
    assert _resident_open_event_dates(
        path, as_of_utc="2026-08-06T14:00:00Z"
    ) == ("2026-08-06",)


def test_malformed_date_is_not_dropped_by_the_staleness_filter(tmp_path: Path) -> None:
    path = _manifest(tmp_path, event_dates=["not-a-date"])

    # The filter only discards dates it can prove are closed; malformed values
    # stay for the existing downstream gates to reject.
    assert _resident_open_event_dates(
        path, as_of_utc="2026-08-06T14:00:00Z"
    ) == ("not-a-date",)


def test_missing_manifest_yields_no_scope(tmp_path: Path) -> None:
    assert _resident_open_event_dates(tmp_path / "absent.json") == ()
