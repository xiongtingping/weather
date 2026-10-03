"""One date-admission rule for every evidence pass, and one group's refusal is its own.

Measured 2026-09-13: the resident fail-closed every cycle with
``official_ws_evidence_complete_scope_missing ... EHAM:no_registry_scope``
while the registry held eleven tradable buckets for each of the 44 listed
scopes of 2026-09-14.  LFPB|2026-09-14 held a position, so charter 85 led the
rotation with it; the main pass read its not-started group through the
started-day registry because it never passed ``listing_day_pass``; that group
was the cycle's required one, so its refusal failed the cycle before the
listing pass could run.  From 12:00Z -- NZWN's midnight, when 2026-09-14
became an open date -- the starvation guarantee admitted the listed scope as
well, and 2026-09-13 wrote no book at all.

These tests hold the rule (``_admitted_evidence_groups``), the ingress it
feeds against the real registry across a station's midnight, the per-group
confinement, and the rotation's return to the day under way.  Missing
registry, missing book and quote expiry still refuse under the listed view.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.market_ingress import (
    materialize_market_evidence,
    materialize_official_ws_evidence_cache,
)
from bayes_basket_target.production_worker import (
    EVIDENCE_GROUP_SCOPES_ATTR,
    EVENT_DAY_CAPTURE_BOUND_SEC,
    TargetProductionWorker,
    _admitted_evidence_groups,
    _compiled_listing_rows,
    _confine_blocked_scopes,
    _scope_manifest_station_day,
)
from bayes_basket_target.settlement_time import settlement_day_state
from bayes_basket_target.ssot import apply_production_ssot_schema
from tests.test_bayes_basket_target_market_ingress import (
    EXCHANGE_SIZING,
    _canonical,
    _freshness_args,
    _inputs,
    _manifest,
    _registry,
)

D13, D14 = "2026-09-13", "2026-09-14"


def _scope(station: str, day: str, state: str) -> dict:
    """The publisher's scope manifest shape: no ``station_id`` field."""

    return {
        "scope_key": f"{station}|{day}",
        "event_date": day,
        "target_local_date": day,
        "event_day_state": state,
        "station_scope": {"mode": "allowlist", "station_ids": [station]},
        "quotes_by_token": {f"{station}-{day}-t": {"book_observed_at": "x"}},
    }


def _shape(passes):
    return [
        (state, listing, sorted(_scope_manifest_station_day(m)[0] for m in group))
        for state, listing, group in passes
    ]


# ---------------------------------------------------------------- the rule


def test_the_live_manifest_shape_names_its_station() -> None:
    manifest = _scope("EHAM", D14, "not_started")
    assert "station_id" not in manifest
    assert _scope_manifest_station_day(manifest) == ("EHAM", D14)
    merged_single = {**manifest, "scope_key": f"SWEEP|{D14}"}
    assert _scope_manifest_station_day(merged_single) == ("EHAM", D14)


def test_a_listed_date_mixed_across_timezones_gives_each_group_its_own_view() -> None:
    passes, refused = _admitted_evidence_groups(
        [
            _scope("EHAM", D14, "not_started"),
            _scope("NZWN", D14, "open"),
            _scope("LFPB", D14, "not_started"),
        ],
        listing_scope_keys={f"EHAM|{D14}", f"LFPB|{D14}"},
    )
    assert _shape(passes) == [
        ("open", False, ["NZWN"]),
        ("not_started", True, ["EHAM", "LFPB"]),
    ]
    assert refused == []


def test_an_open_scope_never_borrows_the_listed_view_even_when_listed() -> None:
    # Authorization is necessary for the listed view, never sufficient: the
    # state the group is in decides.
    passes, _ = _admitted_evidence_groups(
        [_scope("NZWN", D14, "open")],
        listing_scope_keys={f"NZWN|{D14}"},
    )
    assert _shape(passes) == [("open", False, ["NZWN"])]


def test_a_not_started_scope_without_listing_authority_is_not_passed() -> None:
    passes, refused = _admitted_evidence_groups(
        [_scope("EHAM", D14, "not_started"), _scope("KORD", D14, "not_started")],
        listing_scope_keys={f"eham|{D14}"},
    )
    assert _shape(passes) == [("not_started", True, ["EHAM"])]
    assert refused == [f"KORD|{D14}"]


# ------------------------------------ the real registry across a midnight


def _registry_db(tmp_path: Path, *, empty_second_bucket: bool = False):
    """TEST|2026-07-23 settled in Amsterdam: local midnight is 22:00Z."""

    manifest = _manifest(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(conn)
    _registry(conn)
    if empty_second_bucket:
        shard = json.loads((tmp_path / "shards" / "yes.json").read_text())
        shard["token_id"] = "yes-39"
        shard["quote"].update({
            "token_id": "yes-39", "outcome": "39°C",
            "best_bid": None, "best_ask": None, "bids": [], "asks": [],
        })
        core = {k: v for k, v in shard.items() if k != "snapshot_sha256"}
        shard["snapshot_sha256"] = hashlib.sha256(_canonical(core)).hexdigest()
        (tmp_path / "shards" / "yes39.json").write_text(json.dumps(shard))
        root = json.loads(manifest.read_text())
        root["materialized_token_count"] = 2
        core = {k: v for k, v in root.items() if k != "snapshot_sha256"}
        root["snapshot_sha256"] = hashlib.sha256(_canonical(core)).hexdigest()
        manifest.write_text(json.dumps(root))
        conn.execute(
            "INSERT INTO reported_trajectory_market_registry VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "highest-temperature-in-test-on-july-23-2026", "2026-07-23", "TEST",
                "UTC", "39°C", "yes-39",
                json.dumps({"market_id": "m-39", "lower": 39.0, "upper": 39.0, **EXCHANGE_SIZING}),
                1, 1, 1,
            ),
        )
    conn.execute(
        "UPDATE reported_trajectory_market_registry SET settlement_timezone='Europe/Amsterdam'"
    )
    return conn, manifest


def _pass_at(conn, manifest, clock: str, *, listing_scope_keys=("TEST|2026-07-23",)):
    """Admit TEST's scope at ``clock`` and run the real evidence cache on it."""

    state = settlement_day_state(
        "2026-07-23", as_of_utc=clock, settlement_timezone="Europe/Amsterdam"
    )["state"]
    passes, _ = _admitted_evidence_groups(
        [_scope("TEST", "2026-07-23", state)],
        listing_scope_keys=set(listing_scope_keys),
    )
    (day_state, listing, _group), = passes
    receipt = materialize_official_ws_evidence_cache(
        conn,
        manifest_path=manifest,
        station_id="TEST",
        event_date="2026-07-23",
        as_of_utc=clock,
        include_listing_days=listing,
    )
    return day_state, listing, receipt


def test_the_listed_scope_materializes_either_side_of_its_midnight_and_back(tmp_path) -> None:
    conn, manifest = _registry_db(tmp_path)
    seen = []
    for clock in ("2026-07-22T21:59:00Z", "2026-07-22T22:01:00Z", "2026-07-22T21:30:00Z"):
        day_state, listing, receipt = _pass_at(conn, manifest, clock)
        assert receipt["selected_scope_count"] == 1
        seen.append((day_state, listing))
    assert seen == [("not_started", True), ("open", False), ("not_started", True)]

    # The started-day view is what the main pass used to read, and before the
    # midnight it finds the registry row and drops it: the 2026-09-13 refusal.
    with pytest.raises(ContractError, match=r"no_registry_scope\[2026-07-23 active=1 not_started=1\]"):
        materialize_official_ws_evidence_cache(
            conn, manifest_path=manifest, station_id="TEST", event_date="2026-07-23",
            as_of_utc="2026-07-22T21:59:00Z", include_listing_days=False,
        )


def test_the_listed_view_does_not_leak_its_empty_book_admission_into_an_open_day(tmp_path) -> None:
    """On a listed day a bucket nobody has quoted yet is a market state; on a
    started day it is a defect.  The listed view admits it, so handing that
    view to an open group is not harmless -- and after the midnight the rule
    stops handing it over."""

    conn, manifest = _registry_db(tmp_path, empty_second_bucket=True)
    _state, listing, receipt = _pass_at(conn, manifest, "2026-07-22T21:59:00Z")
    assert listing is True and receipt["selected_scope_count"] == 1
    with pytest.raises(ContractError, match="official_ws_selected_bucket_missing:yes-39"):
        _pass_at(conn, manifest, "2026-07-22T22:01:00Z")


def test_a_real_registry_gap_still_refuses_under_the_listed_view(tmp_path) -> None:
    conn, manifest = _registry_db(tmp_path)
    with pytest.raises(ContractError, match=r"no_registry_scope\[2026-07-23 active=0\]"):
        materialize_official_ws_evidence_cache(
            conn, manifest_path=manifest, station_id="EHAM", event_date="2026-07-23",
            as_of_utc="2026-07-22T21:59:00Z", include_listing_days=True,
        )


def test_a_missing_book_still_refuses_under_the_listed_view(tmp_path) -> None:
    conn, manifest = _registry_db(tmp_path)
    conn.execute(
        "INSERT INTO reported_trajectory_market_registry VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "highest-temperature-in-test-on-july-23-2026", "2026-07-23", "TEST",
            "Europe/Amsterdam", "39°C", "yes-39",
            json.dumps({"market_id": "m-39", "lower": 39.0, "upper": 39.0, **EXCHANGE_SIZING}),
            1, 1, 1,
        ),
    )
    with pytest.raises(ContractError, match="official_ws_selected_bucket_missing:yes-39"):
        _pass_at(conn, manifest, "2026-07-22T21:59:00Z")


def test_the_listed_view_keeps_the_quote_lease_and_its_expiry(tmp_path) -> None:
    inventory, order, cash = _inputs(tmp_path)
    manifest = _manifest(tmp_path)
    leases = []
    for listing in (False, True):
        with sqlite3.connect(":memory:") as conn:
            conn.row_factory = sqlite3.Row
            apply_production_ssot_schema(conn)
            _registry(conn)
            materialize_market_evidence(
                conn, manifest_path=manifest, inventory_path=inventory,
                order_state_path=order, cash_phase_path=cash,
                include_listing_days=listing, commit=True, **_freshness_args(),
            )
            leases.append([
                tuple(row) for row in conn.execute(
                    "SELECT quote_observed_at, quote_expires_at FROM basket_official_ws_snapshot"
                )
            ])
    # The same book, the same five-second lease, whichever view admitted it.
    assert leases == [[("2026-07-23T05:00:00Z", "2026-07-23T05:00:05Z")]] * 2

    lapsed = _freshness_args()
    lapsed["quote_freshness_policy"] = {
        **lapsed["quote_freshness_policy"], "valid_until_utc": "2026-07-23T04:00:00Z",
    }
    from bayes_basket_target import content_sha256

    lapsed["quote_freshness_policy"].pop("content_sha256")
    lapsed["quote_freshness_policy"]["content_sha256"] = content_sha256(
        lapsed["quote_freshness_policy"]
    )
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)
        with pytest.raises(ContractError, match="quote_freshness_policy_not_active"):
            materialize_market_evidence(
                conn, manifest_path=manifest, inventory_path=inventory,
                order_state_path=order, cash_phase_path=cash,
                include_listing_days=True, **lapsed,
            )


# -------------------------------------------- the cycle's passes, per group


class _Registry:
    """What the ingress would say, per group, without a database.

    A not-started group read through the started-day view finds nothing --
    exactly the production refusal -- and ``refuse`` names groups whose
    registry or books are genuinely missing."""

    def __init__(self, refuse=()):
        self.calls: list[tuple[str, str, tuple[str, ...], bool]] = []
        self.refuse = set(refuse)

    def __call__(self, activation, *, manifest_path, full_materialization, listing_day_pass):
        root = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        stations = tuple(sorted(root["station_scope"]["station_ids"]))
        day, state = root["event_date"], root["event_day_state"]
        self.calls.append((day, state, stations, listing_day_pass))
        if (day, state) in self.refuse:
            raise ContractError(
                "official_ws_evidence_complete_scope_missing:"
                f"{len(stations)} skipped:{stations[0]}|{day}:official_ws_selected_bucket_missing:tok"
            )
        if state == "not_started" and not listing_day_pass:
            raise ContractError(
                "official_ws_evidence_complete_scope_missing:"
                f"no_registry_scope[{day} active=11 not_started=11]"
            )
        return {"state": "committed", "station_allowlist": list(stations), "event_date": day}


@pytest.fixture
def cycle(monkeypatch):
    def build(*, listing_keys, turn, refuse=()):
        worker = object.__new__(TargetProductionWorker)
        registry = _Registry(refuse)
        blocked: list[dict] = []
        monkeypatch.setattr(worker, "_materialize_market_evidence_from_manifest", registry)
        monkeypatch.setattr(
            worker, "_listing_entry_scopes",
            lambda **_kw: sorted(tuple(key.split("|", 1)) for key in listing_keys),
        )
        monkeypatch.setattr(worker, "_take_listing_scope_turn", lambda **_kw: turn)

        def record_blocked(**kwargs):
            blocked.append(kwargs)
            return []

        monkeypatch.setattr(worker, "_commit_blocked_scopes", record_blocked)
        return worker, registry, blocked

    return build


def _selected(station, day, state):
    return {
        "station_id": station, "event_date": day, "event_day_state": state,
        "scope_key": f"{station}|{day}", "probability_evidence_fresh": True,
    }


def test_a_listed_scope_selected_as_the_lead_is_passed_and_compiled(cycle) -> None:
    worker, registry, blocked = cycle(
        listing_keys={f"LFPB|{D14}", f"EHAM|{D14}"}, turn=False,
    )
    result = worker._materialize_cycle_market_evidence(
        {},
        sweep_scopes=[
            _scope("LFPB", D14, "not_started"),
            _scope("EHAM", D14, "not_started"),
            _scope("NZWN", D14, "open"),
        ],
        selected_scope=_selected("LFPB", D14, "not_started"),
    )
    assert result["error"] is None
    assert registry.calls == [
        (D14, "open", ("NZWN",), False),
        (D14, "not_started", ("EHAM", "LFPB"), True),
    ]
    assert result["market_ingress"]["station_allowlist"] == ["EHAM", "LFPB"]
    # Not the listed days' turn, and still compiled: this pass is the cycle's.
    assert result["lead_listing_scopes"] == {("EHAM", D14), ("LFPB", D14)}
    assert result["listing_stations"] == {"EHAM", "LFPB"}
    assert blocked == []


def test_the_listed_lead_is_compiled_off_the_listed_days_turn_and_nothing_else_is() -> None:
    listing_rows = [("EHAM", D14), ("LFPB", D14), ("LFPB", "2026-09-15"), ("KORD", D14)]
    lead = {("EHAM", D14), ("lfpb", D14)}
    assert _compiled_listing_rows(listing_rows, serve_listing=False, lead_listing_scopes=lead) == [
        ("EHAM", D14), ("LFPB", D14),
    ]
    assert _compiled_listing_rows(listing_rows, serve_listing=False, lead_listing_scopes=set()) == []
    assert _compiled_listing_rows(listing_rows, serve_listing=True, lead_listing_scopes=set()) == listing_rows


def test_one_date_open_here_and_not_started_there_passes_each_group_once(cycle) -> None:
    worker, registry, _blocked = cycle(
        listing_keys={f"KLAX|{D13}", f"EHAM|{D14}"}, turn=True,
    )
    result = worker._materialize_cycle_market_evidence(
        {},
        sweep_scopes=[
            _scope("CYYZ", D13, "open"),
            _scope("KLAX", D13, "not_started"),
            _scope("EHAM", D14, "not_started"),
        ],
        selected_scope=_selected("CYYZ", D13, "open"),
    )
    assert result["error"] is None
    assert registry.calls == [
        (D13, "open", ("CYYZ",), False),
        (D13, "not_started", ("KLAX",), True),
        # The listed days' pass skips what the main pass already passed.
        (D14, "not_started", ("EHAM",), True),
    ]
    assert result["market_ingress"]["station_allowlist"] == ["CYYZ"]
    assert result["lead_listing_scopes"] == set()
    assert result["listing_stations"] == {"KLAX", "EHAM"}
    assert [item["event_date"] for item in result["market_ingress"]["listing_day_evidence"]] == [D14]


def test_a_refusing_listed_lead_does_not_take_the_open_group_with_it(cycle) -> None:
    worker, _registry, blocked = cycle(
        listing_keys={f"LFPB|{D14}"}, turn=False, refuse={(D14, "not_started")},
    )
    result = worker._materialize_cycle_market_evidence(
        {},
        sweep_scopes=[_scope("LFPB", D14, "not_started"), _scope("NZWN", D14, "open")],
        selected_scope=_selected("LFPB", D14, "not_started"),
    )
    assert result["error"] is None
    assert result["market_ingress"]["station_allowlist"] == ["NZWN"]
    groups = {g["day_state"]: g for g in result["market_ingress"]["evidence_groups"]}
    assert groups["not_started"]["state"] == "blocked_fail_closed"
    assert "official_ws_selected_bucket_missing" in groups["not_started"]["reason"]
    assert groups["open"]["state"] == "committed"
    # The ledger is open-day only; a listed group's refusal writes no decision.
    assert blocked == []


def test_a_refusing_open_lead_is_recorded_against_its_own_scopes_only(cycle) -> None:
    worker, _registry, blocked = cycle(
        listing_keys={f"KLAX|{D13}"}, turn=False, refuse={(D13, "open")},
    )
    result = worker._materialize_cycle_market_evidence(
        {},
        sweep_scopes=[
            _scope("CYYZ", D13, "open"),
            _scope("KORD", D13, "open"),
            _scope("KLAX", D13, "not_started"),
        ],
        selected_scope=_selected("CYYZ", D13, "open"),
    )
    assert result["error"] is None
    assert result["market_ingress"]["station_allowlist"] == ["KLAX"]
    assert [call["scopes"] for call in blocked] == [[("CYYZ", D13), ("KORD", D13)]]
    assert blocked[0]["reason_codes"] == [
        "official_ws_evidence_complete_scope_missing:2 skipped:CYYZ|2026-09-13:"
        "official_ws_selected_bucket_missing:tok"
    ]


def test_when_every_group_refuses_the_lead_error_is_raised_after_the_listed_days(cycle) -> None:
    worker, registry, _blocked = cycle(
        listing_keys={f"KLAX|{D13}", f"EHAM|{D14}"}, turn=True,
        refuse={(D13, "open"), (D13, "not_started"), (D14, "not_started")},
    )
    result = worker._materialize_cycle_market_evidence(
        {},
        sweep_scopes=[
            _scope("CYYZ", D13, "open"),
            _scope("KLAX", D13, "not_started"),
            _scope("EHAM", D14, "not_started"),
        ],
        selected_scope=_selected("CYYZ", D13, "open"),
    )
    error = result["error"]
    assert isinstance(error, ContractError)
    assert "CYYZ|2026-09-13" in str(error)
    # The listed day was still given its pass before the cycle failed.
    assert (D14, "not_started", ("EHAM",), True) in registry.calls
    # And the blocker belongs to the groups that raised it.
    assert getattr(error, EVIDENCE_GROUP_SCOPES_ATTR) == frozenset({("CYYZ", D13), ("KLAX", D13)})
    assert result["market_ingress"]["state"] == "blocked_fail_closed"
    assert result["market_ingress"]["reason"] == f"ContractError:{error}"


def test_a_listed_day_carries_the_cycle_when_the_current_day_refuses(cycle) -> None:
    worker, _registry, _blocked = cycle(
        listing_keys={f"EHAM|{D14}"}, turn=True, refuse={(D13, "open")},
    )
    result = worker._materialize_cycle_market_evidence(
        {},
        sweep_scopes=[_scope("CYYZ", D13, "open"), _scope("EHAM", D14, "not_started")],
        selected_scope=_selected("CYYZ", D13, "open"),
    )
    assert result["error"] is None
    assert result["market_ingress"]["station_allowlist"] == ["EHAM"]
    assert result["lead_listing_scopes"] == {("EHAM", D14)}


def test_a_fault_that_is_not_a_group_contract_still_fails_the_cycle(cycle, monkeypatch) -> None:
    worker, _registry, _blocked = cycle(listing_keys=set(), turn=False)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker, "_materialize_market_evidence_from_manifest", locked)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        worker._materialize_cycle_market_evidence(
            {},
            sweep_scopes=[_scope("CYYZ", D13, "open")],
            selected_scope=_selected("CYYZ", D13, "open"),
        )


def test_a_cycle_exception_is_recorded_only_against_the_scopes_it_names() -> None:
    scopes = [("CYYZ", D13), ("KLAX", D13), ("EGLC", D13)]
    confined = ContractError("x")
    setattr(confined, EVIDENCE_GROUP_SCOPES_ATTR, frozenset({("cyyz", D13), ("EHAM", D14)}))
    assert _confine_blocked_scopes(scopes, confined) == [("CYYZ", D13)]
    listed_only = ContractError("y")
    setattr(listed_only, EVIDENCE_GROUP_SCOPES_ATTR, frozenset({("EHAM", D14)}))
    assert _confine_blocked_scopes(scopes, listed_only) == []
    assert _confine_blocked_scopes(scopes, RuntimeError("z")) == scopes


# ------------------------------------------------------ the mid-loop refresh


def test_the_refresh_admits_per_station_day_and_confines_a_refusing_group(cycle, monkeypatch) -> None:
    worker, registry, _blocked = cycle(
        listing_keys={f"EHAM|{D14}"}, turn=False, refuse={(D14, "open")},
    )
    catalog = [
        {"event_date": D14, "manifest": _scope(station, D14, state), "probability_evidence_fresh": True}
        for station, state in (("NZWN", "open"), ("EHAM", "not_started"), ("KORD", "not_started"))
    ]
    monkeypatch.setattr(worker, "_resident_ws_scope_catalog", lambda: catalog)
    result = worker._refresh_scope_day_evidence({}, event_date=D14, as_of_utc="2026-09-13T14:14:00Z")
    assert registry.calls == [
        (D14, "open", ("NZWN",), False),
        # KORD is not a listing scope: no listed view, and no pass.
        (D14, "not_started", ("EHAM",), True),
    ]
    assert result["station_allowlist"] == ["EHAM"]
    assert [g["day_state"] for g in result["evidence_group_failures"]] == ["open"]

    registry.refuse.add((D14, "not_started"))
    with pytest.raises(ContractError, match="NZWN"):
        worker._refresh_scope_day_evidence({}, event_date=D14, as_of_utc="2026-09-13T14:14:00Z")


def test_the_refresh_restates_only_the_stations_it_is_asked_for(cycle, monkeypatch) -> None:
    """2026-10-03: a mid-loop refresh restates the decision loop's reach, not
    the whole day -- at four seconds a compile the whole day took longer to
    write than the lease it was renewing."""

    worker, registry, _blocked = cycle(listing_keys=set(), turn=False)
    catalog = [
        {"event_date": D14, "station_id": station, "manifest": _scope(station, D14, "open"),
         "probability_evidence_fresh": True}
        for station in ("CYYZ", "KLAX", "EGLC", "NZWN")
    ]
    monkeypatch.setattr(worker, "_resident_ws_scope_catalog", lambda: catalog)
    result = worker._refresh_scope_day_evidence(
        {}, event_date=D14, as_of_utc="2026-09-14T10:00:00Z", stations=["klax", "EGLC"],
    )
    assert registry.calls == [(D14, "open", ("EGLC", "KLAX"), False)]
    assert result["station_allowlist"] == ["EGLC", "KLAX"]
    assert result["day_scope_manifest_count"] == 4
    # A reach the catalog has nothing for says so, and says the day had some.
    none = worker._refresh_scope_day_evidence(
        {}, event_date=D14, as_of_utc="2026-09-14T10:00:00Z", stations=["RJTT"],
    )
    assert none == {"event_date": D14, "state": "no_scope_manifest", "day_scope_manifest_count": 4}
    assert len(registry.calls) == 1
    # None is the whole day, as before.
    worker._refresh_scope_day_evidence({}, event_date=D14, as_of_utc="2026-09-14T10:00:00Z")
    assert registry.calls[-1] == (D14, "open", ("CYYZ", "EGLC", "KLAX", "NZWN"), False)


def test_an_open_day_refresh_does_not_read_the_listing_scopes(cycle, monkeypatch) -> None:
    """They decide only a not-started group's view, and reading them re-runs
    the fleet's decision scopes: 2 s a refresh, 46 s of 105 on 2026-10-03."""

    worker, registry, _blocked = cycle(listing_keys={f"EHAM|{D14}"}, turn=False)
    asked: list[str] = []
    real = worker._listing_entry_scope_keys
    monkeypatch.setattr(
        worker, "_listing_entry_scope_keys",
        lambda **kw: asked.append(kw["as_of_utc"]) or real(**kw),
    )
    catalog = [
        {"event_date": D14, "station_id": station, "manifest": _scope(station, D14, state),
         "probability_evidence_fresh": True}
        for station, state in (("NZWN", "open"), ("CYYZ", "open"), ("EHAM", "not_started"))
    ]
    monkeypatch.setattr(worker, "_resident_ws_scope_catalog", lambda: catalog)
    worker._refresh_scope_day_evidence(
        {}, event_date=D14, as_of_utc="2026-09-14T10:00:00Z", stations=["NZWN", "CYYZ"],
    )
    assert asked == [] and registry.calls == [(D14, "open", ("CYYZ", "NZWN"), False)]
    # A reach holding a not-started scope still reads them, and still lends
    # the listed view only to the scopes they admit.
    worker._refresh_scope_day_evidence(
        {}, event_date=D14, as_of_utc="2026-09-14T10:00:00Z", stations=["NZWN", "EHAM"],
    )
    assert asked == ["2026-09-14T10:00:00Z"]
    assert registry.calls[-2:] == [(D14, "open", ("NZWN",), False), (D14, "not_started", ("EHAM",), True)]


# ------------------------------------------------ the rotation comes back


def _rotation_scope(station, tz, day, clock):
    state = settlement_day_state(day, as_of_utc=clock, settlement_timezone=tz)["state"]
    return {
        "scope_key": f"{station}|{day}",
        "station_id": station,
        "event_date": day,
        "event_day_state": state,
        "manifest": {
            "target_timezone": tz,
            "probability_authority": {"envelope_id": "env-1"},
            "quotes_by_token": {"t": {"book_observed_at": "2026-09-13T10:00:00Z"}},
        },
    }


def test_the_rotation_returns_to_the_day_under_way_across_a_listed_dates_first_midnight(monkeypatch) -> None:
    """2026-09-13 11:00-15:00Z, 7-minute cycles.  NZWN opens 2026-09-14 at
    12:00Z; LFPB|2026-09-14 is listed and holds a position throughout."""

    worker = object.__new__(TargetProductionWorker)
    monkeypatch.setattr(worker, "_ledger_held_scope_keys", lambda: frozenset({f"LFPB|{D14}"}))
    catalogue = [
        ("CYYZ", "America/Toronto", D13),
        ("EGLC", "Europe/London", D13),
        ("NZWN", "Pacific/Auckland", D14),
        ("LFPB", "Europe/Paris", D14),
    ]
    start = datetime(2026, 9, 13, 11, 0, tzinfo=timezone.utc)
    served_d13: list[datetime] = []
    picked: list[str] = []
    for step in range(int(4 * 60 / 7)):
        at = start + timedelta(minutes=7 * step)
        clock = at.strftime("%Y-%m-%dT%H:%M:%SZ")
        scopes = [
            scope for scope in (_rotation_scope(s, tz, day, clock) for s, tz, day in catalogue)
            if scope["event_day_state"] in {"open", "not_started"}
        ]
        chosen = worker._select_resident_open_ws_scope(scopes, as_of_utc=clock)
        picked.append(chosen["scope_key"])
        if chosen["event_date"] == D13:
            served_d13.append(at)

    # Charter 85 still leads with the held listed scope whenever nothing is
    # starved, and the listed date's own open station is still reached.
    assert f"LFPB|{D14}" in picked
    assert f"NZWN|{D14}" in picked
    # But the day under way is never left longer than the bound and two
    # cycles -- including after 12:00Z, which is where it used to stop.
    gaps = [
        (later - earlier).total_seconds()
        for earlier, later in zip([start, *served_d13], [*served_d13, start + timedelta(hours=4)])
    ]
    assert max(gaps) <= EVENT_DAY_CAPTURE_BOUND_SEC + 2 * 7 * 60, (picked, gaps)
    assert any(at >= datetime(2026, 9, 13, 12, 30, tzinfo=timezone.utc) for at in served_d13)
