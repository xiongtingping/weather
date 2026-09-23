"""Rotating native capture must accumulate coverage, not overwrite it.

`capture_official_native_ensemble_forward` writes an inbox containing only the
scope it just fetched.  The resident captures one scope per rotation step, so
writing that file straight to the shared path would leave exactly one station's
packet alive at any time.  The merge keeps every other packet, replaces the
matching scope, and prunes days that are over everywhere.
"""

import json
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.production_worker import (
    OFFICIAL_NATIVE_FORWARD_INBOX_SCHEMA,
    _merge_official_native_inbox,
)


def _packet(station: str, event_date: str, marker: str = "v1") -> dict:
    return {
        "schema": "official_native_ensemble_forward_packet_v1",
        "station_id": station,
        "event_date": event_date,
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "marker": marker,
    }


def _write_inbox(path: Path, packets: list[dict]) -> None:
    identity = {"schema": OFFICIAL_NATIVE_FORWARD_INBOX_SCHEMA, "packets": packets}
    path.write_text(
        json.dumps({**identity, "content_sha256": content_sha256(identity)}),
        encoding="utf-8",
    )


def _packets(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["packets"]


def _scopes(path: Path) -> set[tuple[str, str]]:
    return {(p["station_id"], p["event_date"]) for p in _packets(path)}


def test_other_stations_survive_a_capture(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [_packet("KMIA", "2026-08-07"), _packet("KATL", "2026-08-07")])
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [_packet("KLGA", "2026-08-07")])

    result = _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    assert result["packet_count"] == 3
    assert _scopes(inbox) == {
        ("KMIA", "2026-08-07"), ("KATL", "2026-08-07"), ("KLGA", "2026-08-07"),
    }


def test_same_scope_is_replaced_not_duplicated(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [_packet("KMIA", "2026-08-07", marker="old")])
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [_packet("KMIA", "2026-08-07", marker="new")])

    _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    packets = _packets(inbox)
    assert len(packets) == 1
    assert packets[0]["marker"] == "new"


def test_certainly_closed_days_are_pruned(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [
        _packet("KATL", "2026-08-01"),
        _packet("KATL", "2026-08-03"),
        _packet("KATL", "2026-08-06"),
    ])
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [_packet("KMIA", "2026-08-07")])

    result = _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    # 08-01 and 08-03 are over in every timezone; 08-06 is not.
    assert result["pruned"] == 2
    assert _scopes(inbox) == {("KATL", "2026-08-06"), ("KMIA", "2026-08-07")}


def test_a_fresh_capture_for_a_closed_day_is_still_kept(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [])
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [_packet("KMIA", "2026-08-01")])

    # Pruning applies to retained packets, never to what was just captured;
    # silently dropping a caller's own capture would be a confusing no-op.
    result = _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    assert result["pruned"] == 0
    assert _scopes(inbox) == {("KMIA", "2026-08-01")}


def test_content_hash_matches_written_packets(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [_packet("KATL", "2026-08-07")])
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [_packet("KMIA", "2026-08-07")])

    _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    written = json.loads(inbox.read_text(encoding="utf-8"))
    identity = {
        "schema": written["schema"],
        "packets": written["packets"],
    }
    assert written["content_sha256"] == content_sha256(identity)


def test_missing_inbox_is_created(tmp_path: Path) -> None:
    inbox = tmp_path / "absent.json"
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [_packet("KMIA", "2026-08-07")])

    result = _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    assert result["packet_count"] == 1
    assert inbox.is_file()


def test_empty_staging_fails_closed(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [_packet("KATL", "2026-08-07")])
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [])

    with pytest.raises(ContractError, match="official_native_staging_packets_missing"):
        _merge_official_native_inbox(
            inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
        )

    # A failed merge must leave the shared inbox untouched.
    assert _scopes(inbox) == {("KATL", "2026-08-07")}


def _lead_packet(
    station: str,
    event_date: str,
    *,
    lead_bucket: str,
    marker: str = "v1",
) -> dict:
    return {**_packet(station, event_date, marker), "lead_bucket": lead_bucket}


def test_a_fresher_lead_replaces_an_older_one(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [_lead_packet("KMIA", "2026-08-07", lead_bucket="d2")])
    staging = tmp_path / "staging.json"
    _write_inbox(
        staging,
        [_lead_packet("KMIA", "2026-08-07", lead_bucket="d1", marker="v2")],
    )

    result = _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    assert result["packet_count"] == 1
    assert _packets(inbox)[0]["lead_bucket"] == "d1"
    assert _packets(inbox)[0]["marker"] == "v2"


def test_a_late_older_lead_does_not_overwrite_a_fresher_packet(
    tmp_path: Path,
) -> None:
    # Captures for the three leads of one station-day run concurrently, so a
    # slow d2 fetch can land after the d1 fetch it was queued alongside.  The
    # inbox holds one packet per station-day; on arrival order alone the older
    # run would win and P0 would silently regress to a forecast made a day
    # earlier.
    inbox = tmp_path / "inbox.json"
    _write_inbox(
        inbox,
        [_lead_packet("KMIA", "2026-08-07", lead_bucket="d0", marker="fresh")],
    )
    staging = tmp_path / "staging.json"
    _write_inbox(
        staging,
        [_lead_packet("KMIA", "2026-08-07", lead_bucket="d2", marker="stale")],
    )

    result = _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    assert result["packet_count"] == 1
    assert _packets(inbox)[0]["marker"] == "fresh"


def test_recapture_at_the_same_lead_still_replaces(tmp_path: Path) -> None:
    # A re-read of the same lead is a refresh: the first capture may have hit
    # a run whose members had not all landed.
    inbox = tmp_path / "inbox.json"
    _write_inbox(
        inbox,
        [_lead_packet("KMIA", "2026-08-07", lead_bucket="d1", marker="old")],
    )
    staging = tmp_path / "staging.json"
    _write_inbox(
        staging,
        [_lead_packet("KMIA", "2026-08-07", lead_bucket="d1", marker="new")],
    )

    _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    assert _packets(inbox)[0]["marker"] == "new"


def test_other_scopes_are_untouched_by_the_lead_comparison(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(
        inbox,
        [
            _lead_packet("KMIA", "2026-08-07", lead_bucket="d0", marker="fresh"),
            _lead_packet("KATL", "2026-08-07", lead_bucket="d2"),
        ],
    )
    staging = tmp_path / "staging.json"
    _write_inbox(
        staging,
        [_lead_packet("KMIA", "2026-08-07", lead_bucket="d2", marker="stale")],
    )

    result = _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    assert result["packet_count"] == 2
    assert _scopes(inbox) == {("KMIA", "2026-08-07"), ("KATL", "2026-08-07")}


def test_a_fresher_run_with_no_fitted_weights_does_not_displace_a_servable_one(
    tmp_path,
) -> None:
    # The lead bucket is per station and derived from the run, so following a
    # new publication cycle can land a station in a training group its own
    # history never populated -- reading 06Z for the first time moved thirteen
    # American station-days into ``h12_24``, where nothing had ever been
    # fitted.  Serving the newer run there would serve no P0 at all.  The
    # archive still ingests every run, so the empty group fills and this
    # preference dissolves once weights exist.
    from bayes_basket_target.production_worker import (
        _merge_official_native_inbox,
    )

    def _packet(lead_bucket: str, run_init: str) -> dict:
        return {
            "schema": "official_native_ensemble_forward_packet_v1",
            "station_id": "CYYZ",
            "event_date": "2026-08-14",
            "lead_bucket": lead_bucket,
            "run_init_utc_by_system": {"ECMWF_IFS_ENS_OPEN_PF50": run_init},
        }

    inbox = tmp_path / "inbox.json"
    staging = tmp_path / "staging.json"
    fitted = {("CYYZ", "h24_48")}

    _write_inbox(inbox, [_packet("h24_48", "2026-08-13T00:00:00Z")])
    _write_inbox(staging, [_packet("h12_24", "2026-08-13T06:00:00Z")])
    _merge_official_native_inbox(
        inbox,
        staging_path=staging,
        as_of_utc="2026-08-13T16:00:00Z",
        fitted_lead_buckets=fitted,
    )
    held = json.loads(inbox.read_text(encoding="utf-8"))["packets"]
    assert [row["lead_bucket"] for row in held] == ["h24_48"]

    # Once the group has weights, the fresher run takes over on its own.
    _write_inbox(staging, [_packet("h12_24", "2026-08-13T06:00:00Z")])
    _merge_official_native_inbox(
        inbox,
        staging_path=staging,
        as_of_utc="2026-08-13T16:00:00Z",
        fitted_lead_buckets=fitted | {("CYYZ", "h12_24")},
    )
    held = json.loads(inbox.read_text(encoding="utf-8"))["packets"]
    assert [row["lead_bucket"] for row in held] == ["h12_24"]


# --------------------------------------------------------------------------
# Charter 193 (三) per system: no family in the served packet goes back to an
# older run through the merge (2026-09-23 d1 packets for 09-24).
# --------------------------------------------------------------------------
def _run_packet(station: str, runs: dict[str, str], marker: str) -> dict:
    return {
        **_packet(station, "2026-09-24", marker),
        "lead_bucket": "h24_48",
        "run_init_utc_by_system": runs,
    }


_IN_SERVICE = {
    "ECMWF_IFS_ENS_OPEN_PF50": "2026-09-22T18:00:00Z",
    "NOAA_GEFS_05": "2026-09-22T18:00:00Z",
    "DWD_ICON_GLOBAL_EPS": "2026-09-23T00:00:00Z",
    "ECCC_GEM_GLOBAL_ENS": "2026-09-22T12:00:00Z",
}


@pytest.mark.parametrize(
    "staged_runs, merged_marker, refused",
    [
        # every system at least as new: replaced, as before
        ({**_IN_SERVICE, "NOAA_GEFS_05": "2026-09-23T00:00:00Z"}, "staged", None),
        # the required pair level, ICON back to D-2 12Z: the old key (slower
        # required family) tied and let it in; now it stays out
        ({**_IN_SERVICE, "DWD_ICON_GLOBAL_EPS": "2026-09-22T12:00:00Z"},
         "service", ["DWD_ICON_GLOBAL_EPS"]),
        # GEFS ahead, ECMWF behind (the 05:00Z d1 packet): out
        ({**_IN_SERVICE, "NOAA_GEFS_05": "2026-09-23T00:00:00Z",
          "ECMWF_IFS_ENS_OPEN_PF50": "2026-09-22T12:00:00Z"}, "service", None),
        # a system the served packet does not carry cannot be "older"
        ({k: v for k, v in _IN_SERVICE.items() if k != "ECCC_GEM_GLOBAL_ENS"}
         | {"NOAA_GEFS_05": "2026-09-23T00:00:00Z"}, "staged", None),
    ],
)
def test_a_packet_older_on_any_system_is_not_merged(
    tmp_path: Path, staged_runs, merged_marker, refused
) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [_run_packet("ZUUU", _IN_SERVICE, "service")])
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [_run_packet("ZUUU", staged_runs, "staged")])

    result = _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-09-23T05:00:00Z"
    )

    (packet,) = _packets(inbox)
    assert packet["marker"] == merged_marker
    if refused is not None:
        assert result["refused_older_systems"] == {"ZUUU|2026-09-24": refused}
    if merged_marker == "staged":
        assert "refused_older_systems" not in result


def test_a_servable_packet_still_replaces_one_no_p0_can_use(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [_run_packet("ZUUU", _IN_SERVICE, "service")])
    staging = tmp_path / "staging.json"
    older_icon = {**_IN_SERVICE, "DWD_ICON_GLOBAL_EPS": "2026-09-22T12:00:00Z"}
    staged = {**_run_packet("ZUUU", older_icon, "staged"), "lead_bucket": "h12_24"}
    _write_inbox(staging, [staged])

    _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-09-23T05:00:00Z",
        fitted_lead_buckets={("ZUUU", "h12_24")},
    )

    assert _packets(inbox)[0]["marker"] == "staged"
