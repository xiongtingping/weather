from __future__ import annotations

import asyncio
import json
import hashlib
from contextlib import nullcontext
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.market_ingress import load_official_ws_quotes
import scripts.capture_bayes_basket_target_official_ws_snapshot as capture_module
from scripts.capture_bayes_basket_target_official_ws_snapshot import (
    _build_manifest,
    _registry_snapshot_scope,
    _registry_snapshot_tokens,
    _quote_from_message,
    _settlement_day_state,
)


def _manifest_registry_scope(
    *,
    event_date: str,
    stations: dict[str, str],
    tokens: dict[str, str],
) -> dict[str, object]:
    return {
        "event_date": event_date,
        "station_allowlist": sorted(stations),
        "settlement_timezones_by_station": dict(sorted(stations.items())),
        "markets_by_token": {
            token: {
                "station_id": station_id,
                "event_date": event_date,
                "settlement_timezone": stations[station_id],
                "slug": f"market-{station_id.lower()}-{token}",
                "outcome": token,
            }
            for token, station_id in sorted(tokens.items())
        },
        "registry_snapshot_sha256": "a" * 64,
    }


def _registry_row(
    *,
    station_id: str,
    event_date: str = "2026-07-24",
    settlement_timezone: str = "America/New_York",
    outcome: str = "30",
    token: str | None = None,
    slug: str | None = None,
) -> dict[str, object]:
    token_value = token or f"yes-{station_id.lower()}-{outcome}"
    slug_value = slug or f"maximum-temperature-{station_id.lower()}-{outcome}"
    return {
        "slug": slug_value,
        "event_date": event_date,
        "outcome": outcome,
        "city_slug": station_id.lower(),
        "station_id": station_id,
        "settlement_timezone": settlement_timezone,
        "settlement_unit": "F",
        "source_lane": "awc",
        "source_eligible": 1,
        "source_block_reason": "",
        "resolution_url": "https://example.invalid",
        "rules_sha256": "a" * 64,
        "yes_token": token_value,
        "no_token": f"no-{station_id.lower()}-{outcome}",
        "market_active": 1,
        "market_accepting_orders": 1,
        "registry_json": {"lower": int(outcome), "upper": int(outcome)},
        "refreshed_at": "2026-07-24T11:59:00Z",
    }


def _write_registry_snapshot(
    path: Path,
    rows: list[dict[str, object]],
    *,
    forward: bool = False,
) -> Path:
    payload: dict[str, object] = {
        "schema": "bayes_basket_target_market_registry_snapshot_v1",
        "source_schema": "reported_trajectory_market_registry_v1",
        "source_scope": "shared_live_registry_export_only",
        "source_database_path": "/protected/read-only-source.db",
        "exported_at_utc": "2026-07-24T11:59:30Z",
        "source_row_count": len(rows),
        "row_count": len(rows),
        "filters": {
            "station_id": "",
            "event_date": "",
            "as_of_utc": "2026-07-24T12:00:00Z",
            "excluded_future_row_count": 0,
            "include_not_started_for_p0": forward,
        },
        "rows": rows,
        "writes_shared_live_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "forward_p0_catalog_only": forward,
    }
    payload["snapshot_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_book_message_becomes_complete_yes_quote() -> None:
    token, quote = _quote_from_message(
        {
            "event_type": "book",
            "asset_id": "yes-1",
            "bids": [{"price": "0.20", "size": "12"}],
            "asks": [{"price": "0.30", "size": "9"}],
        },
        observed_at="2026-07-24T23:00:00Z",
    )
    assert token == "yes-1"
    assert quote["side"] == "YES"
    assert quote["best_bid"] < quote["best_ask"]
    assert quote["book_side_state"] == "two_sided"


def test_one_sided_book_is_retained_as_official_evidence() -> None:
    token, quote = _quote_from_message(
        {
            "event_type": "book",
            "asset_id": "yes-ask-only",
            "bids": [],
            "asks": [{"price": "0.30", "size": "9"}],
        },
        observed_at="2026-07-24T23:00:00Z",
    )
    assert token == "yes-ask-only"
    assert quote["book_side_state"] == "ask_only"
    assert quote["best_bid"] is None
    assert quote["best_ask"] == 0.30


def test_ask_only_book_is_buy_materialized_but_sell_incomplete() -> None:
    registry_scope = _manifest_registry_scope(
        event_date="2026-07-24",
        stations={"TEST": "America/New_York"},
        tokens={"yes-ask-only": "TEST"},
    )
    parsed = _quote_from_message(
        {
            "event_type": "book",
            "asset_id": "yes-ask-only",
            "bids": [],
            "asks": [{"price": "0.30", "size": "9"}],
        },
        observed_at="2026-07-24T23:00:00Z",
    )
    assert parsed is not None
    token, quote = parsed
    manifest = _build_manifest(
        event_date="2026-07-24",
        target_timezone="America/New_York",
        as_of_utc="2026-07-24T23:00:00Z",
        tokens=[token],
        quotes={token: quote},
        shard_size=200,
        started_at_utc="2026-07-24T23:00:00Z",
        finished_at_utc="2026-07-24T23:00:01Z",
        errors=[],
        timeout_sec=45.0,
        station_scope=["TEST"],
        registry_scope=registry_scope,
    )
    assert manifest["materialized_token_count"] == 1
    assert manifest["unmaterialized_tokens"] == []
    assert manifest["sell_unmaterialized_tokens"] == [token]
    assert manifest["sell_incomplete_token_count"] == 1


def test_bid_only_book_is_retained_as_official_evidence() -> None:
    token, quote = _quote_from_message(
        {
            "event_type": "book",
            "asset_id": "yes-bid-only",
            "bids": [{"price": "0.20", "size": "12"}],
            "asks": [],
        },
        observed_at="2026-07-24T23:00:00Z",
    )
    assert token == "yes-bid-only"
    assert quote["book_side_state"] == "bid_only"
    assert quote["best_bid"] == 0.20
    assert quote["best_ask"] is None


def test_crossed_or_non_book_message_is_ignored() -> None:
    assert _quote_from_message(
        {"event_type": "price_change", "asset_id": "yes-1", "bids": [], "asks": []},
        observed_at="2026-07-24T23:00:00Z",
    ) is None
    assert _quote_from_message(
        {"event_type": "book", "asset_id": "yes-1", "bids": [{"price": 0.4, "size": 1}], "asks": [{"price": 0.3, "size": 1}]},
        observed_at="2026-07-24T23:00:00Z",
    ) is None


def test_capture_shard_returns_after_all_books_without_extra_recv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokens = [f"yes-{index}" for index in range(11)]
    messages = [
        {
            "event_type": "book",
            "asset_id": token,
            "bids": [{"price": "0.20", "size": "12"}],
            "asks": [{"price": "0.30", "size": "9"}],
        }
        for token in tokens
    ]
    state: dict[str, object] = {"recv_count": 0}

    class FakeWebSocket:
        async def send(self, _payload: str) -> None:
            return None

        async def recv(self) -> str:
            state["recv_count"] = int(state["recv_count"]) + 1
            if int(state["recv_count"]) > 1:
                raise AssertionError("collector waited for another frame after 11/11")
            return json.dumps(messages)

    class FakeConnection:
        async def __aenter__(self) -> FakeWebSocket:
            return FakeWebSocket()

        async def __aexit__(self, *_args: object) -> None:
            return None

    def fake_connect(*_args: object, **kwargs: object) -> FakeConnection:
        state["close_timeout"] = kwargs["close_timeout"]
        return FakeConnection()

    monkeypatch.setattr(capture_module.websockets, "connect", fake_connect)
    monkeypatch.setattr(capture_module, "assert_process_memory_budget", lambda **_kwargs: None)

    quotes, errors = asyncio.run(
        capture_module._capture_shard(
            tokens,
            timeout_sec=45.0,
            shard_index=0,
            proxy=None,
        )
    )

    assert set(quotes) == set(tokens)
    assert errors == []
    assert state["recv_count"] == 1
    assert state["close_timeout"] == capture_module.ONE_SHOT_CLOSE_TIMEOUT_SEC
    assert float(state["close_timeout"]) < 1.0


def test_capture_shard_missing_token_waits_for_timeout_and_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = [
        json.dumps(
            {
                "event_type": "book",
                "asset_id": "yes-1",
                "bids": [{"price": "0.20", "size": "12"}],
                "asks": [{"price": "0.30", "size": "9"}],
            }
        )
    ]

    class FakeWebSocket:
        async def send(self, _payload: str) -> None:
            return None

        async def recv(self) -> str:
            if frames:
                return frames.pop(0)
            await asyncio.Future()
            raise AssertionError("unreachable")

    class FakeConnection:
        async def __aenter__(self) -> FakeWebSocket:
            return FakeWebSocket()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        capture_module.websockets,
        "connect",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    monkeypatch.setattr(capture_module, "assert_process_memory_budget", lambda **_kwargs: None)

    quotes, errors = asyncio.run(
        capture_module._capture_shard(
            ["yes-1", "yes-2"],
            timeout_sec=1.0,
            shard_index=0,
            proxy=None,
        )
    )

    assert set(quotes) == {"yes-1"}
    assert "shard_0:missing_tokens=1" in errors


def test_duplicate_books_deterministically_keep_latest_arrival(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = [
        json.dumps(
            {
                "event_type": "book",
                "asset_id": "yes-1",
                "bids": [{"price": "0.20", "size": "12"}],
                "asks": [{"price": "0.40", "size": "9"}],
            }
        ),
        json.dumps(
            [
                {
                    "event_type": "book",
                    "asset_id": "yes-1",
                    "bids": [{"price": "0.21", "size": "12"}],
                    "asks": [{"price": "0.31", "size": "9"}],
                },
                {
                    "event_type": "book",
                    "asset_id": "yes-2",
                    "bids": [{"price": "0.22", "size": "12"}],
                    "asks": [{"price": "0.32", "size": "9"}],
                },
            ]
        ),
    ]
    observed = iter(
        [
            "2026-07-24T23:00:00.000Z",
            "2026-07-24T23:00:01.000Z",
        ]
    )

    class FakeWebSocket:
        async def send(self, _payload: str) -> None:
            return None

        async def recv(self) -> str:
            return frames.pop(0)

    class FakeConnection:
        async def __aenter__(self) -> FakeWebSocket:
            return FakeWebSocket()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        capture_module.websockets,
        "connect",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    monkeypatch.setattr(capture_module, "_now_utc", lambda: next(observed))
    monkeypatch.setattr(capture_module, "assert_process_memory_budget", lambda **_kwargs: None)

    quotes, errors = asyncio.run(
        capture_module._capture_shard(
            ["yes-1", "yes-2"],
            timeout_sec=45.0,
            shard_index=0,
            proxy=None,
        )
    )

    assert errors == []
    assert quotes["yes-1"]["best_ask"] == 0.31
    assert quotes["yes-1"]["book_observed_at"] == "2026-07-24T23:00:01.000Z"
    assert quotes["yes-2"]["best_ask"] == 0.32


def test_manifest_is_accepted_by_target_ingress_loader(tmp_path: Path) -> None:
    registry_scope = _manifest_registry_scope(
        event_date="2026-07-24",
        stations={"TEST": "America/New_York"},
        tokens={"yes-1": "TEST"},
    )
    manifest = _build_manifest(
        event_date="2026-07-24",
        target_timezone="America/New_York",
        as_of_utc="2026-07-24T23:00:00Z",
        tokens=["yes-1"],
        quotes={"yes-1": {"token_id": "yes-1", "side": "YES", "bids": [{"price": 0.2, "size": 2}], "asks": [{"price": 0.3, "size": 2}], "book_observed_at": "2026-07-24T23:00:00Z"}},
        shard_size=200,
        started_at_utc="2026-07-24T23:00:00Z",
        finished_at_utc="2026-07-24T23:00:01Z",
        errors=[],
        timeout_sec=45.0,
        station_scope=["TEST"],
        registry_scope=registry_scope,
    )
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    root, quotes = load_official_ws_quotes(path)
    assert root["state"] == "connected"
    assert set(quotes) == {"yes-1"}


def test_forward_p0_token_loader_accepts_verified_catalog_without_db_ingress(tmp_path: Path) -> None:
    payload = {
        "schema": "bayes_basket_target_market_registry_snapshot_v1",
        "source_schema": "reported_trajectory_market_registry_v1",
        "source_scope": "shared_live_registry_export_only",
        "source_database_path": "/protected/read-only-source.db",
        "exported_at_utc": "2026-07-24T22:59:00Z",
        "source_row_count": 1,
        "row_count": 1,
        "filters": {
            "station_id": "",
            "event_date": "2026-07-25",
            "as_of_utc": "2026-07-24T23:00:00Z",
            "excluded_future_row_count": 0,
            "include_not_started_for_p0": True,
        },
        "rows": [{
            "slug": "maximum-temperature-in-test-on-july-25",
            "event_date": "2026-07-25",
            "outcome": "38",
            "city_slug": "test",
            "station_id": "TEST",
            "settlement_timezone": "America/New_York",
            "settlement_unit": "F",
            "source_lane": "awc",
            "source_eligible": 1,
            "source_block_reason": "",
            "resolution_url": "https://example.invalid",
            "rules_sha256": "a" * 64,
            "yes_token": "yes-38",
            "no_token": "no-38",
            "market_active": 1,
            "market_accepting_orders": 1,
            "registry_json": {"lower": 38, "upper": 38},
            "refreshed_at": "2026-07-24T22:58:00Z",
        }],
        "writes_shared_live_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "forward_p0_catalog_only": True,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload["snapshot_sha256"] = hashlib.sha256(canonical).hexdigest()
    path = tmp_path / "forward.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert _registry_snapshot_tokens(
        registry_snapshot=path,
        event_date="2026-07-25",
        as_of_utc="2026-07-24T23:00:00Z",
        station_ids=["TEST"],
    ) == (["yes-38"], "America/New_York", "not_started")


def test_rkpk_settlement_day_opens_at_15z_not_et_boundary() -> None:
    assert _settlement_day_state(
        event_date="2026-07-27",
        target_timezone="Asia/Seoul",
        as_of_utc="2026-07-26T14:59:59Z",
    ) == "not_started"
    assert _settlement_day_state(
        event_date="2026-07-27",
        target_timezone="Asia/Seoul",
        as_of_utc="2026-07-26T15:00:00Z",
    ) == "open"
    assert _settlement_day_state(
        event_date="2026-07-27",
        target_timezone="Asia/Seoul",
        as_of_utc="2026-07-27T15:00:00Z",
    ) == "closed"


def test_run_writes_raw_consumer_manifest_separately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_scope = _manifest_registry_scope(
        event_date="2026-07-24",
        stations={"TEST": "America/New_York"},
        tokens={"yes-1": "TEST"},
    )
    manifest = _build_manifest(
        event_date="2026-07-24",
        target_timezone="America/New_York",
        as_of_utc="2026-07-24T23:00:00Z",
        tokens=["yes-1"],
        quotes={
            "yes-1": {
                "token_id": "yes-1",
                "side": "YES",
                "bids": [{"price": 0.2, "size": 2}],
                "asks": [{"price": 0.3, "size": 2}],
                "book_observed_at": "2026-07-24T23:00:00Z",
            }
        },
        shard_size=200,
        started_at_utc="2026-07-24T23:00:00Z",
        finished_at_utc="2026-07-24T23:00:01Z",
        errors=[],
        timeout_sec=45.0,
        station_scope=["TEST"],
        registry_scope=registry_scope,
    )

    async def fake_capture(**_kwargs: object) -> dict[str, object]:
        return {
            "schema": capture_module.SCHEMA,
            "state": "CAPTURED",
            "target_local_date": "2026-07-24",
            "as_of_utc": "2026-07-24T23:00:00Z",
            "manifest": manifest,
            "captured_token_count": 1,
            "subscribed_token_count": 1,
            "errors": [],
            "boundary": {"target_only": True, "official_ws_only": True},
        }

    monkeypatch.setattr(
        capture_module,
        "_registry_snapshot_scope",
        lambda **_kwargs: {
            **registry_scope,
            "tokens": ["yes-1"],
            "target_timezone": "America/New_York",
            "settlement_state": "open",
            "settlement_states_by_station": {"TEST": "open"},
        },
    )
    monkeypatch.setattr(capture_module, "_configured_proxy", lambda _explicit="": None)
    monkeypatch.setattr(capture_module, "capture", fake_capture)
    monkeypatch.setattr(capture_module, "_assert_output_disk_budget", lambda _path: None)

    audit_path = tmp_path / "capture-wrapper.json"
    manifest_path = tmp_path / "consumer-manifest.json"
    result = capture_module.run(
        event_date="2026-07-24",
        as_of_utc="2026-07-24T23:00:00Z",
        output=audit_path,
        manifest_output=manifest_path,
        timeout_sec=45.0,
        shard_size=200,
        market_registry_snapshot=tmp_path / "registry.json",
    )

    assert result["manifest_output"] == str(manifest_path.resolve())
    assert audit_path.is_file()
    assert manifest_path.is_file()
    persisted = json.loads(audit_path.read_text(encoding="utf-8"))
    assert persisted["manifest_written"] is True
    assert persisted.get("manifest_preserved_after_block") is not True
    root, quotes = load_official_ws_quotes(manifest_path)
    assert root["schema"] == "clob_ws_latest_quote_snapshot_v1"
    assert set(quotes) == {"yes-1"}


def test_blocked_capture_preserves_last_consumer_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit_path = tmp_path / "capture-wrapper.json"
    manifest_path = tmp_path / "consumer-manifest.json"
    previous = {"schema": "clob_ws_latest_quote_snapshot_v1", "state": "connected", "generation": 7}
    manifest_path.write_text(json.dumps(previous), encoding="utf-8")

    async def blocked_capture(**_kwargs: object) -> dict[str, object]:
        return {
            "schema": capture_module.SCHEMA,
            "state": "BLOCKED",
            "target_local_date": "2026-07-24",
            "as_of_utc": "2026-07-24T23:00:00Z",
            "manifest": {"schema": "clob_ws_latest_quote_snapshot_v1", "state": "blocked"},
            "captured_token_count": 0,
            "subscribed_token_count": 1,
            "errors": ["shard_0:connection_lost"],
            "boundary": {"target_only": True, "official_ws_only": True},
        }

    monkeypatch.setattr(
        capture_module,
        "_registry_snapshot_scope",
        lambda **_kwargs: {
            **_manifest_registry_scope(
                event_date="2026-07-24",
                stations={"TEST": "America/New_York"},
                tokens={"yes-1": "TEST"},
            ),
            "tokens": ["yes-1"],
            "target_timezone": "America/New_York",
            "settlement_state": "open",
            "settlement_states_by_station": {"TEST": "open"},
        },
    )
    monkeypatch.setattr(capture_module, "_configured_proxy", lambda _explicit="": None)
    monkeypatch.setattr(capture_module, "capture", blocked_capture)
    monkeypatch.setattr(capture_module, "_assert_output_disk_budget", lambda _path: None)

    result = capture_module.run(
        event_date="2026-07-24",
        as_of_utc="2026-07-24T23:00:00Z",
        output=audit_path,
        manifest_output=manifest_path,
        timeout_sec=45.0,
        shard_size=200,
        market_registry_snapshot=tmp_path / "registry.json",
    )

    assert result["state"] == "BLOCKED"
    assert result["manifest_written"] is False
    assert result["manifest_preserved_after_block"] is True
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == previous
    persisted = json.loads(audit_path.read_text(encoding="utf-8"))
    assert persisted["manifest_written"] is False
    assert persisted["manifest_preserved_after_block"] is True


def test_run_rejects_same_audit_and_manifest_output(tmp_path: Path) -> None:
    path = tmp_path / "same.json"
    with pytest.raises(ContractError, match="manifest_output_must_differ"):
        capture_module.run(
            event_date="2026-07-24",
            as_of_utc="2026-07-24T23:00:00Z",
            output=path,
            manifest_output=path,
            timeout_sec=45.0,
            shard_size=200,
            market_registry_snapshot=tmp_path / "registry.json",
        )


def test_proxy_configuration_is_not_retained_in_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_MARKET_WS_PROXY", "http://127.0.0.1:7890")
    assert capture_module._configured_proxy() == "http://127.0.0.1:7890"


def test_target_proxy_prefers_available_configured_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PM_MARKET_WS_PROXY", raising=False)
    def fake_connection(address, timeout):
        if address[1] != 1082:
            raise OSError("proxy_not_listening")
        return nullcontext()

    monkeypatch.setattr(capture_module.socket, "create_connection", fake_connection)
    assert capture_module._configured_proxy() == "http://127.0.0.1:7897"


def test_exact_45_station_registry_scope_materializes_nonempty_allowlist(
    tmp_path: Path,
) -> None:
    rows = [
        _registry_row(
            station_id=f"S{index:02d}",
            settlement_timezone=(
                "Asia/Seoul" if index % 2 else "America/New_York"
            ),
        )
        for index in range(45)
    ]
    path = _write_registry_snapshot(tmp_path / "registry-45.json", rows)
    scope = _registry_snapshot_scope(
        registry_snapshot=path,
        event_date="2026-07-24",
        as_of_utc="2026-07-24T12:00:00Z",
    )

    assert len(scope["station_allowlist"]) == 45
    assert len(scope["tokens"]) == 45
    assert scope["target_timezone"] is None
    assert set(scope["settlement_timezones_by_station"].values()) == {
        "America/New_York",
        "Asia/Seoul",
    }
    quotes = {
        token: {
            "token_id": token,
            "side": "YES",
            "bids": [{"price": 0.2, "size": 2}],
            "asks": [{"price": 0.3, "size": 2}],
            "book_observed_at": "2026-07-24T12:00:00Z",
        }
        for token in scope["tokens"]
    }
    manifest = _build_manifest(
        event_date="2026-07-24",
        target_timezone=None,
        as_of_utc="2026-07-24T12:00:00Z",
        tokens=scope["tokens"],
        quotes=quotes,
        shard_size=200,
        started_at_utc="2026-07-24T12:00:00Z",
        finished_at_utc="2026-07-24T12:00:01Z",
        errors=[],
        timeout_sec=45.0,
        station_scope=scope["station_allowlist"],
        registry_scope=scope,
    )
    assert manifest["station_scope"]["mode"] == "allowlist"
    assert len(manifest["station_scope"]["station_ids"]) == 45
    assert manifest["event_date"] == manifest["target_local_date"] == "2026-07-24"
    assert (
        manifest["settlement_timezone_authority"]
        == "market_registry.settlement_timezone"
    )
    assert manifest["market_registry_scope"]["settlement_timezones_by_station"][
        "S01"
    ] == "Asia/Seoul"


def test_exact_one_station_scope_never_uses_empty_allowlist_fallback(
    tmp_path: Path,
) -> None:
    path = _write_registry_snapshot(
        tmp_path / "registry-one.json",
        [_registry_row(station_id="RKPK", settlement_timezone="Asia/Seoul")],
    )
    scope = _registry_snapshot_scope(
        registry_snapshot=path,
        event_date="2026-07-24",
        as_of_utc="2026-07-24T12:00:00Z",
        station_ids=[],
    )
    assert scope["station_allowlist"] == ["RKPK"]
    assert scope["settlement_timezones_by_station"] == {"RKPK": "Asia/Seoul"}


def test_multi_day_registry_is_scoped_to_explicit_event_date(tmp_path: Path) -> None:
    path = _write_registry_snapshot(
        tmp_path / "registry-mixed-date.json",
        [
            _registry_row(station_id="KATL", event_date="2026-07-24"),
            _registry_row(station_id="RKPK", event_date="2026-07-25"),
        ],
    )
    scope = _registry_snapshot_scope(
        registry_snapshot=path,
        event_date="2026-07-24",
        as_of_utc="2026-07-24T12:00:00Z",
    )
    assert scope["event_date"] == "2026-07-24"
    assert scope["station_allowlist"] == ["KATL"]


def test_ineligible_row_without_station_does_not_pollute_scope(
    tmp_path: Path,
) -> None:
    unsupported = _registry_row(
        station_id="UNSUPPORTED",
        event_date="2026-07-24",
    )
    unsupported["station_id"] = ""
    unsupported["source_eligible"] = 0
    path = _write_registry_snapshot(
        tmp_path / "registry-with-ineligible.json",
        [
            _registry_row(station_id="KATL", event_date="2026-07-24"),
            unsupported,
        ],
    )
    scope = _registry_snapshot_scope(
        registry_snapshot=path,
        event_date="2026-07-24",
        as_of_utc="2026-07-24T12:00:00Z",
    )
    assert scope["station_allowlist"] == ["KATL"]


def test_empty_registry_scope_fails_closed(tmp_path: Path) -> None:
    path = _write_registry_snapshot(tmp_path / "registry-empty.json", [])
    with pytest.raises(
        ContractError,
        match="row_count_mismatch|registry_rows_missing",
    ):
        _registry_snapshot_scope(
            registry_snapshot=path,
            event_date="2026-07-24",
            as_of_utc="2026-07-24T12:00:00Z",
        )


def test_non_registry_station_fails_closed(tmp_path: Path) -> None:
    path = _write_registry_snapshot(
        tmp_path / "registry-one.json",
        [_registry_row(station_id="KATL")],
    )
    with pytest.raises(ContractError, match="station_scope_not_in_registry"):
        _registry_snapshot_scope(
            registry_snapshot=path,
            event_date="2026-07-24",
            as_of_utc="2026-07-24T12:00:00Z",
            station_ids=["NOTREG"],
        )


def test_46_station_scope_fails_closed(tmp_path: Path) -> None:
    path = _write_registry_snapshot(
        tmp_path / "registry-46.json",
        [_registry_row(station_id=f"S{index:02d}") for index in range(46)],
    )
    with pytest.raises(ContractError, match="station_scope_cap_exceeded"):
        _registry_snapshot_scope(
            registry_snapshot=path,
            event_date="2026-07-24",
            as_of_utc="2026-07-24T12:00:00Z",
        )


def test_duplicate_market_and_token_conflicts_fail_closed(tmp_path: Path) -> None:
    market_conflict = _write_registry_snapshot(
        tmp_path / "registry-market-conflict.json",
        [
            _registry_row(station_id="KATL", outcome="30", slug="slug-a"),
            _registry_row(
                station_id="KATL",
                outcome="30",
                slug="slug-b",
                token="yes-other",
            ),
        ],
    )
    with pytest.raises(ContractError, match="duplicate_market_conflict"):
        _registry_snapshot_scope(
            registry_snapshot=market_conflict,
            event_date="2026-07-24",
            as_of_utc="2026-07-24T12:00:00Z",
        )

    token_conflict = _write_registry_snapshot(
        tmp_path / "registry-token-conflict.json",
        [
            _registry_row(station_id="KATL", token="yes-shared"),
            _registry_row(station_id="RKPK", token="yes-shared"),
        ],
    )
    with pytest.raises(ContractError, match="duplicate_token_conflict"):
        _registry_snapshot_scope(
            registry_snapshot=token_conflict,
            event_date="2026-07-24",
            as_of_utc="2026-07-24T12:00:00Z",
        )


def test_token_resource_boundary_fails_closed(tmp_path: Path) -> None:
    rows = [
        _registry_row(
            station_id="KATL",
            outcome=str(1000 + index),
            token=f"yes-{index:03d}",
        )
        for index in range(capture_module.MAX_TOKENS + 1)
    ]
    path = _write_registry_snapshot(tmp_path / "registry-too-many-tokens.json", rows)
    with pytest.raises(ContractError, match="token_scope_cap_exceeded"):
        _registry_snapshot_scope(
            registry_snapshot=path,
            event_date="2026-07-24",
            as_of_utc="2026-07-24T12:00:00Z",
        )


def test_valid_invalid_valid_scope_replay_has_no_state_leak(tmp_path: Path) -> None:
    valid = _write_registry_snapshot(
        tmp_path / "registry-valid.json",
        [_registry_row(station_id="KATL")],
    )
    invalid = _write_registry_snapshot(
        tmp_path / "registry-invalid.json",
        [
            _registry_row(station_id="RKPK", event_date="2026-07-25"),
        ],
    )
    hashes: list[str] = []
    for path in (valid, invalid, valid, invalid, valid):
        if path == invalid:
            with pytest.raises(
                ContractError,
                match="active_scope_empty",
            ):
                _registry_snapshot_scope(
                    registry_snapshot=path,
                    event_date="2026-07-24",
                    as_of_utc="2026-07-24T12:00:00Z",
                )
        else:
            scope = _registry_snapshot_scope(
                registry_snapshot=path,
                event_date="2026-07-24",
                as_of_utc="2026-07-24T12:00:00Z",
            )
            hashes.append(
                hashlib.sha256(
                    json.dumps(
                        scope,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            )
    assert len(set(hashes)) == 1


def test_run_output_is_atomic_bounded_and_content_hash_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = _write_registry_snapshot(
        tmp_path / "registry.json",
        [_registry_row(station_id="KATL")],
    )

    async def fixed_capture(**kwargs: object) -> dict[str, object]:
        token = str(list(kwargs["tokens"])[0])
        manifest = _build_manifest(
            event_date=str(kwargs["event_date"]),
            target_timezone=kwargs["target_timezone"],
            as_of_utc=str(kwargs["as_of_utc"]),
            tokens=[token],
            quotes={
                token: {
                    "token_id": token,
                    "side": "YES",
                    "bids": [{"price": 0.2, "size": 2}],
                    "asks": [{"price": 0.3, "size": 2}],
                    "book_observed_at": "2026-07-24T12:00:00Z",
                }
            },
            shard_size=int(kwargs["shard_size"]),
            started_at_utc="2026-07-24T12:00:00Z",
            finished_at_utc="2026-07-24T12:00:01Z",
            errors=[],
            timeout_sec=float(kwargs["timeout_sec"]),
            station_scope=kwargs["station_scope"],
            registry_scope=kwargs["registry_scope"],
        )
        return {
            "schema": capture_module.SCHEMA,
            "state": "CAPTURED",
            "target_local_date": "2026-07-24",
            "as_of_utc": "2026-07-24T12:00:00Z",
            "manifest": manifest,
            "captured_token_count": 1,
            "subscribed_token_count": 1,
            "errors": [],
            "boundary": {
                "official_ws_only": True,
                "writes_event_market_ticks": False,
            },
        }

    monkeypatch.setattr(capture_module, "capture", fixed_capture)
    monkeypatch.setattr(capture_module, "_configured_proxy", lambda _value="": None)
    monkeypatch.setattr(capture_module, "_assert_output_disk_budget", lambda _path: None)
    audit_path = tmp_path / "audit.json"
    manifest_path = tmp_path / "manifest.json"
    hashes: list[tuple[str, str, str]] = []
    for _ in range(3):
        result = capture_module.run(
            event_date="2026-07-24",
            as_of_utc="2026-07-24T12:00:00Z",
            output=audit_path,
            manifest_output=manifest_path,
            timeout_sec=45.0,
            shard_size=200,
            market_registry_snapshot=registry_path,
        )
        hashes.append(
            (
                result["receipt_sha256"],
                hashlib.sha256(audit_path.read_bytes()).hexdigest(),
                hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            )
        )
    assert len(set(hashes)) == 1
    assert audit_path.stat().st_size < capture_module.MAX_JSON_OUTPUT_BYTES
    assert manifest_path.stat().st_size < capture_module.MAX_JSON_OUTPUT_BYTES
    assert {path.name for path in tmp_path.iterdir()} == {
        "registry.json",
        "audit.json",
        "manifest.json",
    }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["station_scope"] == {
        "mode": "allowlist",
        "station_ids": ["KATL"],
        "source_lane": "REGISTRY_EXACT_SCOPE",
    }
    assert manifest["event_date"] == manifest["target_local_date"]
    assert "event_market_ticks" not in json.dumps(manifest)
