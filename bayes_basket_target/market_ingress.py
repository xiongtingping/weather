"""Controlled ingress from the official CLOB WS evidence manifest.

The resident market listener owns the WebSocket connection and publishes an
atomic, content-addressed manifest.  This module materializes either the
fully joined target input bundle or a bounded evidence-only cache.  It
deliberately does not fetch positions, orders, prices, or weather data:
inventory, order-state, and cash-phase inputs must be supplied as explicit,
versioned artifacts for the executable snapshot path.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .market_registry import (
    load_active_registry_rows,
    load_started_registry_rows,
)
from .market_materialization import insert_market_evidence_bundle
from .quote_freshness import QuoteFreshnessPolicy
from .resource_bounds import (
    MAX_INPUT_FILES_PER_BATCH,
    MAX_WS_EVIDENCE_CACHE_ROW_BYTES,
    MAX_WS_EVIDENCE_CACHE_SCOPES,
    WS_EVIDENCE_CACHE_RETAIN_DAYS,
    assert_connection_target_capacity,
    assert_target_db_capacity,
    load_bounded_json,
)


MANIFEST_SCHEMA = "clob_ws_latest_quote_snapshot_v1"
TOKEN_SCHEMA = "clob_ws_token_quote_snapshot_v1"
INGRESS_SCHEMA = "bayes_basket_market_evidence_ingress_v1"


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify_hash(schema: str, raw: Mapping[str, Any], field: str = "snapshot_sha256") -> None:
    supplied = _text(field, raw.get(field))
    core = {key: value for key, value in raw.items() if key != field}
    if hashlib.sha256(_canonical(core)).hexdigest() != supplied:
        raise ContractError(f"{schema}_content_sha_mismatch")


def _read_json(path: Path, name: str) -> dict[str, Any]:
    try:
        value = load_bounded_json(path)
    except Exception as exc:
        raise ContractError(f"{name}_json_invalid") from exc
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_mapping_required")
    return dict(value)


def _input_payload(
    path: Path | None,
    payload: Mapping[str, Any] | None,
    name: str,
) -> dict[str, Any]:
    if payload is not None:
        if not isinstance(payload, Mapping):
            raise ContractError(f"{name}_mapping_required")
        return dict(payload)
    if path is None:
        raise ContractError(f"{name}_input_missing")
    return _read_json(Path(path), name)


def _quote_is_complete(quote: Any) -> bool:
    if not isinstance(quote, Mapping):
        return False
    asks = quote.get("asks")
    bids = quote.get("bids")
    if not isinstance(asks, list) or not isinstance(bids, list):
        return False
    # A one-sided official book is a materialized liquidity state.  Preserve
    # it and carry the unavailable side explicitly downstream.
    #
    # An empty book on both sides is that same kind of state, not a capture
    # gap: the market listed the bucket and nobody has posted an order at any
    # price.  What distinguishes the two is whether a book was ever observed,
    # which the timestamp below already answers -- a token that never
    # returned one has no timestamp and stays incomplete.
    #
    # Refusing both-sides-empty cost whole stations.  Measured 2026-08-16:
    # NZWN, RKPK, ZSPD and ZSQD each had a live scope with 4 to 8 priced
    # buckets and the rest empty, and the ingress admits a scope only when
    # every one of its buckets is complete -- so seven good books were
    # discarded to avoid recording four empty ones, and those stations had no
    # market evidence at all.  The kernel already carries an unpriceable
    # bucket (ask_available false), so downstream needs nothing new.
    return bool(str(quote.get("book_observed_at") or quote.get("created_at") or "").strip())


def _verify_token_shard(
    path: Path,
    *,
    active_generations: Mapping[int, int],
    active_connection_times: Mapping[int, str],
) -> tuple[str, dict[str, Any]] | None:
    try:
        payload = load_bounded_json(path)
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    try:
        _verify_hash(TOKEN_SCHEMA, payload)
        if payload.get("schema") != TOKEN_SCHEMA:
            return None
        if payload.get("source") != "official_polymarket_clob_websocket":
            return None
        if payload.get("evidence_only") is not True or payload.get("authorizes_order") is not False:
            return None
        if payload.get("shard_active") is not True:
            return None
        token_id = _text("token_id", payload.get("token_id"))
        shard_index = int(payload.get("shard_index"))
        generation = int(payload.get("shard_generation"))
        if active_generations.get(shard_index) != generation:
            return None
        quote = payload.get("quote")
        if not _quote_is_complete(quote):
            return None
        quote_value = dict(quote)
        quote_token = str(quote_value.get("token_id") or token_id).strip()
        if quote_token != token_id:
            return None
        quote_side = str(quote_value.get("side") or "YES").upper()
        if quote_side != "YES":
            return None
        quote_value["_shard_generation"] = generation
        quote_value["_shard_index"] = shard_index
        stream_confirmed_at = str(
            active_connection_times.get(shard_index) or ""
        ).strip()
        if stream_confirmed_at:
            _timestamp(
                "official_ws_stream_confirmed_at",
                stream_confirmed_at,
            )
            quote_value["_stream_confirmed_at"] = stream_confirmed_at
        return token_id, quote_value
    except (ContractError, TypeError, ValueError):
        return None


def load_official_ws_quotes_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Verify one already-bounded official WS scope manifest.

    The scope may store quotes inline or in generation-fenced token shards.
    Partial/unmaterialized tokens are retained as a diagnostic in the root;
    callers must still check that every selected basket token is present.
    """

    if not isinstance(payload, Mapping):
        raise ContractError("official_ws_manifest_mapping_required")
    root = dict(payload)
    _verify_hash(MANIFEST_SCHEMA, root)
    if root.get("schema") != MANIFEST_SCHEMA:
        raise ContractError("official_ws_manifest_schema_invalid")
    if root.get("source") != "official_polymarket_clob_websocket":
        raise ContractError("official_ws_manifest_source_invalid")
    if root.get("evidence_only") is not True or root.get("authorizes_order") is not False:
        raise ContractError("official_ws_manifest_authorization_invalid")
    if root.get("state") != "connected":
        raise ContractError("official_ws_manifest_not_connected")
    active_shards = {int(value) for value in root.get("active_shards") or []}
    expected_shards = int(root.get("expected_shards") or 0)
    if expected_shards <= 0 or active_shards != set(range(expected_shards)):
        raise ContractError("official_ws_manifest_shard_set_incomplete")

    quotes: dict[str, dict[str, Any]] = {}
    storage = str(root.get("quote_storage") or "")
    if storage == "inline_v1":
        raw_quotes = root.get("quotes_by_token")
        if not isinstance(raw_quotes, Mapping):
            raise ContractError("official_ws_manifest_quotes_missing")
        for token_id, raw_quote in raw_quotes.items():
            token = _text("token_id", token_id)
            if _quote_is_complete(raw_quote):
                quote_value = dict(raw_quote)
                if str(quote_value.get("token_id") or token).strip() != token:
                    continue
                if str(quote_value.get("side") or "YES").upper() != "YES":
                    continue
                quotes[token] = quote_value
    elif storage == "sha256_token_shards_v1":
        shard_dir = Path(_text("quote_shard_dir", root.get("quote_shard_dir")))
        active_generations = {
            int(key): int(value)
            for key, value in (root.get("active_shard_generations") or {}).items()
        }
        active_connection_times = {
            int(key): str(value)
            for key, value in (
                root.get("active_shard_connection_observed_at") or {}
            ).items()
            if str(value or "").strip()
        }
        shard_paths = sorted(shard_dir.glob("*.json"))
        if len(shard_paths) > MAX_INPUT_FILES_PER_BATCH:
            raise ContractError("official_ws_manifest_shard_file_count_exceeded")
        for shard_path in shard_paths:
            value = _verify_token_shard(
                shard_path,
                active_generations=active_generations,
                active_connection_times=active_connection_times,
            )
            if value is not None:
                token_id, quote = value
                quotes[token_id] = quote
    else:
        raise ContractError("official_ws_manifest_storage_invalid")
    blocked_tokens = {
        str(value)
        for value in root.get("unmaterialized_tokens") or []
        if str(value)
    }
    # Older publishers used this list as a buy-side gate and included valid
    # bid-only books.  Retain a structurally materialized one-sided quote;
    # only absent or empty payloads are actually unmaterialized.
    for token_id in blocked_tokens:
        if not _quote_is_complete(quotes.get(token_id)):
            quotes.pop(token_id, None)
    if not quotes:
        raise ContractError("official_ws_manifest_complete_quotes_missing")
    return root, quotes


def load_official_ws_quotes(
    path: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load and verify one official WS scope manifest from a bounded file."""

    return load_official_ws_quotes_payload(
        _read_json(Path(path), "official_ws_manifest")
    )


def _scope_key(station_id: str, event_date: str) -> str:
    return f"{station_id}|{event_date}"


def _input_rows(payload: Mapping[str, Any], name: str) -> dict[str, dict[str, Any]]:
    if str(payload.get("schema") or "") != f"bayes_basket_{name}_ingress_v1":
        raise ContractError(f"{name}_ingress_schema_invalid")
    rows = payload.get("markets")
    if not isinstance(rows, list) or not rows:
        raise ContractError(f"{name}_ingress_markets_missing")
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ContractError(f"{name}_ingress_market_invalid")
        station = _text(f"{name}_station_id", raw.get("station_id"))
        event_date = _text(f"{name}_event_date", raw.get("event_date"))
        key = _scope_key(station, event_date)
        if key in result:
            raise ContractError(f"{name}_ingress_scope_duplicate")
        result[key] = dict(raw)
    return result


def _load_registry(
    conn: sqlite3.Connection,
    *,
    station_id: str = "",
    event_date: str = "",
    as_of_utc: str | None = None,
    include_listing_days: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Registry rows for the requested scope.

    ``include_listing_days`` admits a listed-but-not-started settlement day.
    Charter 48 (2026-08-14) makes a listing tradable at listing, so the
    caller that compiles a listing scope needs its registry rows; every
    other caller keeps the started-day view it has always had, because a
    formal inventory, order-state or readiness read still belongs to a day
    that has begun.  The switch is explicit for exactly that reason -- the
    two beliefs are both live, and which one applies is the caller's.
    """

    rows = (
        load_active_registry_rows(
            conn,
            station_id=station_id,
            event_date=event_date,
        )
        if include_listing_days
        else load_started_registry_rows(
            conn,
            as_of_utc=as_of_utc,
            station_id=station_id,
            event_date=event_date,
        )
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = {
            "slug": str(row["slug"]),
            "event_date": str(row["event_date"]),
            "station_id": str(row["station_id"] or ""),
            "settlement_timezone": str(row["settlement_timezone"] or ""),
            "outcome": str(row["outcome"] or ""),
            "yes_token": str(row["yes_token"] or ""),
            "market_id": str(row.get("market_id") or ""),
            "lower": row.get("lower"),
            "upper": row.get("upper"),
            "order_min_size": row.get("order_min_size"),
        }
        if not item["station_id"] or not item["yes_token"] or not item["outcome"]:
            raise ContractError("market_registry_target_identity_missing")
        grouped.setdefault(_scope_key(item["station_id"], item["event_date"]), []).append(item)
    return grouped


def _exchange_min_lot(row: Mapping[str, Any]) -> float:
    """The market's minimum lot, in shares, as the exchange states it.

    Fail closed.  A market that does not state its lot is already blocked
    upstream -- the collector adds ``official_market_order_sizing_rule_missing``
    to ``source_block_reasons`` and ``load_active_registry_rows`` only reads
    ``source_eligible=1`` -- so an eligible row arriving here without one can
    only come from a registry snapshot taken before 2026-08-30.  Sizing a leg
    against that snapshot would mint the same undersized intents this rule
    exists to stop, and the last good snapshot still carries the value, so
    refusing the new snapshot is the safe direction.
    """
    raw = row.get("order_min_size")
    if isinstance(raw, bool) or raw is None:
        raise ContractError("market_registry_order_min_size_missing")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ContractError("market_registry_order_min_size_invalid") from exc
    if not math.isfinite(value) or value <= 0:
        raise ContractError("market_registry_order_min_size_invalid")
    return value


def _bucket_sort_key(row: Mapping[str, Any]) -> tuple[float, float, str]:
    lower = row.get("lower")
    upper = row.get("upper")
    try:
        low = float(lower) if lower is not None else -math.inf
    except (TypeError, ValueError):
        low = -math.inf
    try:
        high = float(upper) if upper is not None else math.inf
    except (TypeError, ValueError):
        high = math.inf
    return low, high, str(row.get("outcome") or "")


def _ask_levels(
    quote: Mapping[str, Any],
    *,
    required: bool = True,
) -> list[dict[str, float]]:
    result: list[dict[str, float]] = []
    previous = -1.0
    for raw in quote.get("asks") or []:
        if not isinstance(raw, Mapping):
            raise ContractError("official_ws_ask_level_invalid")
        try:
            price, shares = float(raw.get("price")), float(raw.get("size"))
        except (TypeError, ValueError) as exc:
            raise ContractError("official_ws_ask_level_invalid") from exc
        if not math.isfinite(price) or not math.isfinite(shares) or not 0.0 < price <= 1.0 or shares <= 0.0 or price <= previous:
            raise ContractError("official_ws_ask_levels_not_strictly_increasing")
        result.append({"price": price, "shares": shares})
        previous = price
    if not result and required:
        raise ContractError("official_ws_ask_depth_missing")
    return result


def _bid_levels(
    quote: Mapping[str, Any],
    *,
    required: bool = True,
) -> list[dict[str, float]]:
    result: list[dict[str, float]] = []
    previous = 2.0
    for raw in quote.get("bids") or []:
        if not isinstance(raw, Mapping):
            raise ContractError("official_ws_bid_level_invalid")
        try:
            price, shares = float(raw.get("price")), float(raw.get("size"))
        except (TypeError, ValueError) as exc:
            raise ContractError("official_ws_bid_level_invalid") from exc
        if not math.isfinite(price) or not math.isfinite(shares) or not 0.0 < price <= 1.0 or shares <= 0.0 or price >= previous:
            raise ContractError("official_ws_bid_levels_not_strictly_decreasing")
        result.append({"price": price, "shares": shares})
        previous = price
    if not result and required:
        raise ContractError("official_ws_bid_depth_missing")
    return result


def derive_official_ws_price_tick_size(
    quote: Mapping[str, Any],
    *,
    asks: list[dict[str, float]],
    bids: list[dict[str, float]],
    scope_prices: Sequence[float] = (),
) -> tuple[float, str]:
    # The tick belongs to the market, not to one outcome.  Inferring it from a
    # single book reads whatever that book happens to be quoting: a ZUUU
    # outcome sitting on whole cents infers 0.01 while its neighbour quoting
    # 0.004 proves the market is on 0.001, and the coarse answer then costs
    # that outcome a tick of Maker competitiveness on every order.  Widening
    # the evidence to every book in the same scope keeps the rule intact --
    # still the coarsest grid *proved* by official levels -- while letting one
    # sibling's finer quote speak for the market they share.
    prices = [
        float(level["price"])
        for level in [*asks, *bids]
    ]
    evidence = [*prices, *(float(value) for value in scope_prices)]

    def validate_grid(tick: float, *, source: str) -> tuple[float, str]:
        if any(
            abs(price / tick - round(price / tick)) > 1e-7
            for price in prices
        ):
            raise ContractError("official_ws_price_tick_mismatch")
        return tick, source

    raw = quote.get("tick_size")
    if raw not in (None, ""):
        try:
            tick = float(raw)
        except (TypeError, ValueError) as exc:
            raise ContractError("official_ws_tick_size_invalid") from exc
        if not math.isfinite(tick) or not 0.0 < tick < 1.0:
            raise ContractError("official_ws_tick_size_invalid")
        return validate_grid(tick, source="OFFICIAL_WS_TICK_SIZE")
    # Older committed WS shards predate tick_size_change retention.  Infer
    # only the coarsest exchange grid proved by every official book level;
    # never select a finer, more permissive tick.
    for tick in (0.1, 0.01, 0.001, 0.0001):
        if all(
            abs(price / tick - round(price / tick)) <= 1e-7
            for price in evidence
        ):
            return validate_grid(
                tick,
                source=(
                    "OFFICIAL_WS_SCOPE_GRID_DERIVED"
                    if scope_prices
                    else "OFFICIAL_WS_BOOK_GRID_DERIVED"
                ),
            )
    raise ContractError("official_ws_tick_size_missing")


def _quote_time(quote: Mapping[str, Any]) -> str:
    """Return the timestamp of the authoritative economic book state.

    ``_stream_confirmed_at`` is only a bounded connection-liveness lease.
    PING/PONG or an unrelated message on the shard does not change this
    token's price/depth and must not manufacture a new quote, snapshot,
    decision, or training fact.
    """

    return _timestamp(
        "quote_observed_at",
        quote.get("book_observed_at") or quote.get("created_at"),
    )


def _market_quote(
    *,
    root: Mapping[str, Any],
    quotes: Mapping[str, Mapping[str, Any]],
    registry_rows: Iterable[Mapping[str, Any]],
    inventory: Mapping[str, Any],
    order_state: Mapping[str, Any],
    cash_phase_input: Mapping[str, Any],
    remaining_budget_usdc: float,
    sequence: int,
    quote_freshness_policy: QuoteFreshnessPolicy,
    policy_authority_scope: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    rows = sorted(list(registry_rows), key=_bucket_sort_key)
    if not rows:
        raise ContractError("market_registry_target_scope_missing")
    station_id = _text("station_id", rows[0].get("station_id"))
    event_date = _text("event_date", rows[0].get("event_date"))
    if any(row.get("station_id") != station_id or row.get("event_date") != event_date for row in rows):
        raise ContractError("market_registry_scope_mixed")
    settlement_timezones = {
        _text("settlement_timezone", row.get("settlement_timezone"))
        for row in rows
    }
    if len(settlement_timezones) != 1:
        raise ContractError("market_registry_settlement_timezone_not_unique")
    settlement_timezone = next(iter(settlement_timezones))
    bucket_values, quote_observed_at = _scope_quote_values(rows, quotes)
    quote_expires_at = quote_freshness_policy.derive_expiry(
        quote_observed_at=quote_observed_at,
        required_authority_scope=policy_authority_scope,
    )
    if not isinstance(cash_phase_input, Mapping):
        raise ContractError("cash_phase_input_mapping_required")
    if str(cash_phase_input.get("station_id") or "") != station_id or str(cash_phase_input.get("event_date_local") or "") != event_date:
        raise ContractError("cash_phase_input_scope_mismatch")
    quote_version = content_sha256({
        "schema": "official_ws_quote_version_v1",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "buckets": bucket_values,
    })
    snapshot_id = "ws_" + content_sha256({
        "schema": "basket_ws_snapshot_id_v1",
        "quote_version": quote_version,
        "inventory_version": _text("inventory_version", inventory.get("inventory_version")),
        "order_state_version": _text("order_state_version", order_state.get("order_state_version")),
        "cash_phase_input": dict(cash_phase_input),
    })[:40]
    aggregate_market_id = "basket_" + content_sha256({"schema": "basket_market_scope_v1", "station_id": station_id, "event_date": event_date, "slugs": sorted({str(row['slug']) for row in rows})})[:32]
    quote = {
        "snapshot_id": snapshot_id,
        "market_id": aggregate_market_id,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "sequence": int(sequence),
        "observed_at": quote_observed_at,
        "quote_version": quote_version,
        "quote_observed_at": quote_observed_at,
        "quote_expires_at": quote_expires_at,
        "quote_freshness_policy_sha256": (
            quote_freshness_policy.content_sha256
        ),
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": _text("inventory_version", inventory.get("inventory_version")),
        "order_state_version": _text("order_state_version", order_state.get("order_state_version")),
        "remaining_budget_usdc": float(remaining_budget_usdc),
        "buckets": bucket_values,
        "cash_phase_input": dict(cash_phase_input),
    }
    if str(inventory.get("station_id") or "") != station_id or str(inventory.get("event_date") or "") != event_date:
        raise ContractError("inventory_scope_mismatch")
    if str(order_state.get("station_id") or "") != station_id or str(order_state.get("event_date") or "") != event_date:
        raise ContractError("order_state_scope_mismatch")
    inventory_row = dict(inventory)
    order_state_row = dict(order_state)
    for name, row in (
        ("inventory", inventory_row),
        ("order_state", order_state_row),
    ):
        supplied_timezone = str(
            row.get("settlement_timezone") or ""
        ).strip()
        if supplied_timezone and supplied_timezone != settlement_timezone:
            raise ContractError(
                f"{name}_settlement_timezone_mismatch"
            )
        row["settlement_timezone"] = settlement_timezone
    return quote, inventory_row, order_state_row


def _scope_quote_values(
    rows: Iterable[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]],
    manifest_root: Mapping[str, Any] | None = None,
    *,
    listing_day: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Validate one complete registry scope using only official WS quotes."""

    ordered_rows = sorted(list(rows), key=_bucket_sort_key)
    if not ordered_rows:
        raise ContractError("market_registry_target_scope_missing")
    station_id = _text("station_id", ordered_rows[0].get("station_id"))
    event_date = _text("event_date", ordered_rows[0].get("event_date"))
    if any(row.get("station_id") != station_id or row.get("event_date") != event_date for row in ordered_rows):
        raise ContractError("market_registry_scope_mixed")
    bucket_values: list[dict[str, Any]] = []
    observed_times: list[str] = []
    missing_tokens: list[str] = []
    # A listing-day book nobody has quoted yet.  The publisher subscribes at
    # listing (charter 48) and names such tokens in the manifest root; a
    # named bookless bucket is a legal market state -- unbuyable this cycle,
    # never a scope blocker -- while an unnamed missing token stays a defect,
    # because then the scope claims a bucket the stream is not carrying.
    #
    # The preselection lane has admitted exactly this shape since 2026-08-14
    # (p0_preselection, "p0_preselection_official_ws_token_missing").  The
    # evidence lane never got the same treatment, so the first cycle that
    # stored a listing day's evidence refused all 41 scopes on
    # official_ws_selected_bucket_missing.
    declared_unmaterialized = {
        str(value)
        for value in (manifest_root or {}).get("unmaterialized_tokens") or []
        if str(value)
    }

    def _named_bookless(token: str, quote: Mapping[str, Any] | None) -> bool:
        """Whether this token is a listing book nobody has quoted yet.

        Two shapes name one, and which appears depends on the publisher's
        manifest schema.  The sharded single-scope manifest lists the token
        under ``unmaterialized_tokens``.  The resident exact-scope catalog
        has no such key: it names the token by carrying it at all, with an
        empty book, and an empty book on a listed day is precisely the state
        charter 48 admits.

        Only on a listing day.  On a started day an empty book is the
        existing official_ws_book_empty defect and stays one -- the market
        has been open and something should have quoted it.
        """

        if token in declared_unmaterialized:
            return True
        if not listing_day:
            return False
        if quote is None:
            # Absence is NOT emptiness, and treating it as such was a lie
            # this lane told for one cycle: the scope manifests carry a full
            # book for every listing token -- CYYZ 09-01 measured eleven
            # registry tokens, eleven quotes, eleven books, identical sets --
            # so a token missing here means something between the manifest
            # and this function dropped it, and reporting that as "nobody has
            # quoted it yet" hides the defect behind a market state.
            # official_ws_selected_bucket_missing names it instead.
            return False
        return not _ask_levels(quote, required=False) and not _bid_levels(
            quote, required=False
        )
    unmaterialized_buckets: list[dict[str, Any]] = []
    for ordinal, row in enumerate(ordered_rows):
        token_id = _text("yes_token", row.get("yes_token"))
        quote = quotes.get(token_id)
        if _named_bookless(token_id, quote):
            unmaterialized_buckets.append({
                "bucket": str(row["outcome"]),
                "ordinal": ordinal,
                "token_id": token_id,
                "unmaterialized_book": True,
                "ask_levels": [],
                "ask_available": False,
                "bid_levels": [],
                "bid_available": False,
                "bid_price": 0.0,
                "best_ask": None,
                "price_tick_size": 0.0,
                "tick_size_source": "UNMATERIALIZED_BOOK",
                "min_order_size": _exchange_min_lot(row),
                "maker_queue_ahead_shares": 0.0,
                "maker_queue_known": False,
                "quote_source": "OFFICIAL_CLOB_WS",
            })
            continue
        if quote is None or not _quote_is_complete(quote):
            missing_tokens.append(token_id)
            continue
        observed_at = _quote_time(quote)
        observed_times.append(observed_at)
        ask_levels = _ask_levels(quote, required=False)
        bid_levels = _bid_levels(quote, required=False)
        if not ask_levels and not bid_levels:
            missing_tokens.append(token_id)
            continue
        price_tick_size, tick_size_source = (
            derive_official_ws_price_tick_size(
            quote,
            asks=ask_levels,
            bids=bid_levels,
            )
        )
        if (
            ask_levels
            and bid_levels
            and bid_levels[0]["price"] >= ask_levels[0]["price"]
        ):
            raise ContractError("official_ws_crossed_book")
        raw_best_bid = quote.get("best_bid")
        if bid_levels:
            try:
                best_bid = (
                    float(raw_best_bid)
                    if raw_best_bid not in (None, "")
                    else bid_levels[0]["price"]
                )
            except (TypeError, ValueError) as exc:
                raise ContractError("official_ws_best_bid_invalid") from exc
            if (
                not math.isfinite(best_bid)
                or not 0.0 < best_bid <= 1.0
                or (
                    ask_levels
                    and best_bid >= ask_levels[0]["price"]
                )
            ):
                raise ContractError("official_ws_best_bid_invalid")
        else:
            if raw_best_bid not in (None, ""):
                try:
                    empty_bid = float(raw_best_bid)
                except (TypeError, ValueError) as exc:
                    raise ContractError(
                        "official_ws_best_bid_without_depth"
                    ) from exc
                # The resident CLOB listener serializes an authoritative
                # empty bid side as numeric zero.  Zero is the absence of an
                # executable bid, not a price level; any positive/non-finite
                # value without matching depth remains invalid.
                if not math.isfinite(empty_bid) or empty_bid != 0.0:
                    raise ContractError(
                        "official_ws_best_bid_without_depth"
                    )
            best_bid = 0.0
        raw_best_ask = quote.get("best_ask")
        if ask_levels:
            try:
                best_ask = (
                    float(raw_best_ask)
                    if raw_best_ask not in (None, "")
                    else ask_levels[0]["price"]
                )
            except (TypeError, ValueError) as exc:
                raise ContractError("official_ws_best_ask_invalid") from exc
            if (
                not math.isfinite(best_ask)
                or not 0.0 < best_ask <= 1.0
                or abs(best_ask - ask_levels[0]["price"]) > 1e-9
            ):
                raise ContractError("official_ws_best_ask_invalid")
        else:
            if raw_best_ask not in (None, ""):
                raise ContractError("official_ws_best_ask_without_depth")
            best_ask = None
        bucket_values.append({
            "bucket": str(row["outcome"]),
            "ordinal": ordinal,
            "token_id": token_id,
            "ask_levels": ask_levels,
            "ask_available": bool(ask_levels),
            "bid_levels": bid_levels,
            "price_tick_size": price_tick_size,
            "tick_size_source": tick_size_source,
            # Shares, not cash: the exchange refuses a smaller lot outright,
            # so the sizing layer needs it to avoid minting a leg that cannot
            # be filled at any price.
            "min_order_size": _exchange_min_lot(row),
            "bid_price": best_bid,
            "bid_available": bool(bid_levels),
            # Official WS depth is the queue authority.  The basket kernel
            # resolves the exact queue at its eventual candidate price.
            "maker_queue_ahead_shares": (
                float(bid_levels[0]["shares"]) if bid_levels else 0.0
            ),
            "maker_queue_known": True,
            "best_ask": best_ask,
            "book_observed_at": _timestamp(
                "book_observed_at",
                quote.get("book_observed_at") or quote.get("created_at"),
            ),
            "quote_source": "OFFICIAL_CLOB_WS",
            **(
                {"shard_generation": int(quote["_shard_generation"])}
                if quote.get("_shard_generation") is not None
                else {}
            ),
            **(
                {"shard_index": int(quote["_shard_index"])}
                if quote.get("_shard_index") is not None
                else {}
            ),
        })
    if missing_tokens:
        raise ContractError("official_ws_selected_bucket_missing:" + ",".join(missing_tokens[:5]))
    bucket_values.extend(unmaterialized_buckets)
    bucket_values.sort(key=lambda item: int(item["ordinal"]))
    if not observed_times:
        # Every bucket in this scope is a named bookless listing bucket.
        # That is a real market state on a listing day and the scope is
        # recorded as such, rather than refused for lacking a quote nobody
        # has published yet.
        if unmaterialized_buckets:
            raise ContractError("official_ws_scope_bookless")
        raise ContractError("official_ws_selected_quote_missing")
    # Every selected leg must be fresh.  Taking the newest token would let
    # one active bucket mask stale evidence in another basket leg.
    return bucket_values, min(observed_times, key=_timestamp_order)


def _evidence_cache_row(
    *,
    root: Mapping[str, Any],
    registry_rows: Iterable[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]],
    listing_day: bool = False,
) -> dict[str, Any]:
    rows = sorted(list(registry_rows), key=_bucket_sort_key)
    bucket_values, observed_at = _scope_quote_values(
        rows, quotes, root, listing_day=listing_day
    )
    station_id = _text("station_id", rows[0].get("station_id"))
    event_date = _text("event_date", rows[0].get("event_date"))
    settlement_timezones = {
        _text("settlement_timezone", item.get("settlement_timezone"))
        for item in rows
    }
    if len(settlement_timezones) != 1:
        raise ContractError("market_registry_settlement_timezone_not_unique")
    settlement_timezone = next(iter(settlement_timezones))
    quote_version = content_sha256({
        "schema": "official_ws_quote_version_v1",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "buckets": bucket_values,
    })
    market_id = "basket_" + content_sha256({
        "schema": "basket_market_scope_v1",
        "station_id": station_id,
        "event_date": event_date,
        "slugs": sorted({str(row["slug"]) for row in rows}),
    })[:32]
    row = {
        "scope_key": _scope_key(station_id, event_date),
        "evidence_id": "ws_evidence_" + content_sha256({
            "station_id": station_id,
            "event_date": event_date,
            "quote_version": quote_version,
        })[:40],
        "market_id": market_id,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "observed_at": observed_at,
        "quote_version": quote_version,
        "manifest_sha256": _text("manifest_sha256", root.get("snapshot_sha256")),
        "buckets_json": bucket_values,
        "quote_source": "OFFICIAL_CLOB_WS",
        "evidence_only": 1,
        "authorizes_order": 0,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    row["content_sha256"] = content_sha256({
        "schema": "basket_official_ws_evidence_cache_v1",
        **{key: value for key, value in row.items() if key != "content_sha256"},
    })
    return row


def _timestamp_order(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("official_ws_evidence_observed_at_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("official_ws_evidence_observed_at_timezone_required")
    return parsed.astimezone(timezone.utc)


def _upsert_evidence_cache_row(conn: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT evidence_id,observed_at,quote_version,content_sha256 "
        "FROM basket_official_ws_evidence_cache WHERE scope_key=?",
        (row["scope_key"],),
    ).fetchone()
    if existing is not None:
        if str(existing[2]) == str(row["quote_version"]):
            return {"scope_key": row["scope_key"], "state": "ALREADY_CURRENT", "wrote": False, "evidence_id": existing[0]}
        if _timestamp_order(str(row["observed_at"])) < _timestamp_order(str(existing[1])):
            raise ContractError("official_ws_evidence_time_rewind")
        conn.execute(
            "UPDATE basket_official_ws_evidence_cache SET evidence_id=?,market_id=?,settlement_timezone=?,observed_at=?,"
            "quote_version=?,manifest_sha256=?,buckets_json=?,quote_source=?,evidence_only=?,"
            "authorizes_order=?,content_sha256=?,updated_at=? WHERE scope_key=?",
            (
                row["evidence_id"], row["market_id"], row["settlement_timezone"],
                row["observed_at"], row["quote_version"],
                row["manifest_sha256"], json.dumps(row["buckets_json"], ensure_ascii=False, sort_keys=True),
                row["quote_source"], row["evidence_only"], row["authorizes_order"],
                row["content_sha256"], row["updated_at"], row["scope_key"],
            ),
        )
        return {"scope_key": row["scope_key"], "state": "UPDATED_CURRENT_ONLY", "wrote": True, "evidence_id": row["evidence_id"]}
    conn.execute(
        "INSERT INTO basket_official_ws_evidence_cache(" 
        "scope_key,evidence_id,market_id,station_id,event_date,settlement_timezone,observed_at,quote_version,"
        "manifest_sha256,buckets_json,quote_source,evidence_only,authorizes_order,content_sha256,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["scope_key"], row["evidence_id"], row["market_id"], row["station_id"],
            row["event_date"], row["settlement_timezone"],
            row["observed_at"], row["quote_version"], row["manifest_sha256"],
            json.dumps(row["buckets_json"], ensure_ascii=False, sort_keys=True), row["quote_source"],
            row["evidence_only"], row["authorizes_order"], row["content_sha256"], row["updated_at"],
        ),
    )
    return {"scope_key": row["scope_key"], "state": "COMMITTED", "wrote": True, "evidence_id": row["evidence_id"]}


def materialize_official_ws_evidence_cache(
    conn: sqlite3.Connection,
    *,
    manifest_path: Path,
    station_id: str = "",
    event_date: str = "",
    scope_keys: Iterable[tuple[str, str]] | None = None,
    as_of_utc: str | None = None,
    commit: bool = False,
    include_listing_days: bool = False,
) -> dict[str, Any]:
    """Keep only latest complete official-WS evidence, with no execution inputs.

    This cache is intentionally separate from ``basket_official_ws_snapshot``.
    It cannot satisfy readiness, cannot be consumed by the compiler, and does
    not require q=0 inventory, order-state, or a promoted cash policy.
    """

    root, quotes = load_official_ws_quotes(Path(manifest_path))
    # The cache is evidence-only, but it is still target-owned formal input
    # storage.  It holds no quote for a day that has not started unless the
    # caller asks for one: charter 48 makes a listed day tradable at
    # listing, and a scope this book may open on needs its evidence stored
    # like any other.  The sentence that stood here -- "do not persist a
    # quote for an ET day that has not started" -- predates that ruling and
    # was the gate that kept listing entry at zero.
    # Accept an explicit clock so replays and audits cannot depend on the
    # workstation wall clock.
    groups = _load_registry(
        conn,
        station_id=station_id,
        event_date=event_date,
        as_of_utc=as_of_utc,
        include_listing_days=include_listing_days,
    )
    selected = sorted(groups)
    if scope_keys is not None:
        requested = {
            (_text("scope_station_id", station), _text("scope_event_date", day))
            for station, day in scope_keys
        }
        selected = [key for key in selected if tuple(key.split("|", 1)) in requested]
    rows: list[dict[str, Any]] = []
    skipped: dict[str, list[str]] = {}
    for key in selected:
        try:
            rows.append(_evidence_cache_row(
                root=root,
                registry_rows=groups[key],
                quotes=quotes,
                listing_day=include_listing_days,
            ))
        except ContractError as exc:
            skipped[key] = [str(exc)]
    if not rows:
        # Every scope was skipped, and the per-scope reasons are the only
        # thing that says why -- the success path has always reported them
        # in ``skipped_scopes`` while this branch dropped them, leaving the
        # top-level production blocker with no station, no scope and no
        # remedy in it.  Bounded to a handful so one bad manifest cannot
        # inflate the status payload.
        detail = "; ".join(
            f"{key}:{reasons[0]}"
            for key, reasons in sorted(skipped.items())[:5]
        )
        raise ContractError(
            "official_ws_evidence_complete_scope_missing"
            + (f":{len(skipped)} skipped:{detail}" if skipped else ":no_registry_scope")
        )
    for row in rows:
        encoded = json.dumps(row["buckets_json"], ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(encoded) > MAX_WS_EVIDENCE_CACHE_ROW_BYTES:
            raise ContractError("official_ws_evidence_cache_row_bytes_exceeded")
    # Age the working set out before measuring it.  Without this the cache
    # only ever grows: it reached the 512-scope ceiling holding 20 event days,
    # and a ceiling reached is an ingress that writes nothing at all rather
    # than one that writes a little less.  A scope outside the subscription
    # window has no reader -- the scope sweep reads this table to find live
    # station-days, and the market history itself is in
    # ``basket_official_ws_snapshot``.
    floor_day = (
        datetime.now(timezone.utc)
        - timedelta(days=WS_EVIDENCE_CACHE_RETAIN_DAYS)
    ).date().isoformat()
    conn.execute(
        "DELETE FROM basket_official_ws_evidence_cache WHERE event_date < ?",
        (floor_day,),
    )
    existing_count = int(conn.execute(
        "SELECT COUNT(*) FROM basket_official_ws_evidence_cache"
    ).fetchone()[0])
    existing_keys = {
        str(value[0])
        for value in conn.execute(
            "SELECT scope_key FROM basket_official_ws_evidence_cache"
        ).fetchall()
    }
    new_scope_count = sum(1 for row in rows if str(row["scope_key"]) not in existing_keys)
    if existing_count + new_scope_count > MAX_WS_EVIDENCE_CACHE_SCOPES:
        raise ContractError("official_ws_evidence_cache_scope_limit_exceeded")
    db_path = assert_connection_target_capacity(conn)
    if db_path is not None:
        projected_bytes = sum(
            len(json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            for row in rows
        )
        assert_target_db_capacity(db_path, projected_bytes=projected_bytes)
    savepoint = "basket_ws_evidence_cache"
    conn.execute(f"SAVEPOINT {savepoint}")
    receipts: list[dict[str, Any]] = []
    try:
        for row in rows:
            receipts.append(_upsert_evidence_cache_row(conn, row))
        if commit:
            conn.execute(f"RELEASE {savepoint}")
            conn.commit()
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    return {
        "schema": "bayes_basket_official_ws_evidence_cache_ingress_v1",
        "state": "COMMITTED" if commit else "VALIDATED_NO_WRITE",
        "manifest_sha256": str(root["snapshot_sha256"]),
        "selected_scope_count": len(rows),
        "skipped_scope_count": len(skipped),
        "skipped_scopes": skipped,
        "receipts": receipts,
        "evidence_only": True,
        "authorizes_order": False,
        "places_orders": False,
        "calls_order_gateway": False,
    }

def _next_sequence(conn: sqlite3.Connection, station_id: str, event_date: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence),-1)+1 FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?",
        (station_id, event_date),
    ).fetchone()
    return max(0, int(row[0] or 0))


def _reuse_sequence_for_replay(conn: sqlite3.Connection, quote: Mapping[str, Any]) -> int | None:
    row = conn.execute(
        "SELECT sequence FROM basket_official_ws_snapshot WHERE snapshot_id=?",
        (str(quote.get("snapshot_id") or ""),),
    ).fetchone()
    return None if row is None else int(row[0])


def materialize_market_evidence(
    conn: sqlite3.Connection,
    *,
    manifest_path: Path,
    inventory_path: Path | None = None,
    order_state_path: Path | None = None,
    cash_phase_path: Path | None = None,
    inventory_payload: Mapping[str, Any] | None = None,
    order_state_payload: Mapping[str, Any] | None = None,
    cash_phase_payload: Mapping[str, Any] | None = None,
    station_id: str = "",
    event_date: str = "",
    scope_keys: Iterable[tuple[str, str]] | None = None,
    quote_freshness_policy: QuoteFreshnessPolicy | Mapping[str, Any],
    policy_authority_scope: str,
    include_listing_days: bool = False,
    commit: bool = False,
) -> dict[str, Any]:
    """Validate and optionally append all complete market evidence bundles.

    A single transaction is used when ``commit`` is true.  Any selected
    market that lacks one of the three explicit auxiliary inputs aborts the
    whole batch; no partial target generation is published.
    """

    root, quotes = load_official_ws_quotes(Path(manifest_path))
    freshness_policy = (
        quote_freshness_policy
        if isinstance(quote_freshness_policy, QuoteFreshnessPolicy)
        else QuoteFreshnessPolicy.from_mapping(quote_freshness_policy)
    )
    inventory_rows = _input_rows(
        _input_payload(inventory_path, inventory_payload, "inventory"),
        "confirmed_inventory",
    )
    order_rows = _input_rows(
        _input_payload(order_state_path, order_state_payload, "order_state"),
        "order_state",
    )
    cash_rows = _input_rows(
        _input_payload(cash_phase_path, cash_phase_payload, "cash_phase"),
        "cash_phase",
    )
    # Charter 48: the listing pass materializes a day that has not started;
    # the started-day view is the default for every other caller.
    groups = _load_registry(
        conn,
        station_id=station_id,
        event_date=event_date,
        include_listing_days=include_listing_days,
    )
    selected = sorted(groups)
    if scope_keys is not None:
        requested = {
            (_text("scope_station_id", station), _text("scope_event_date", day))
            for station, day in scope_keys
        }
        selected = [key for key in selected if tuple(key.split("|", 1)) in requested]
    if station_id or event_date:
        selected = [key for key in selected if (not station_id or key.startswith(station_id + "|")) and (not event_date or key.endswith("|" + event_date))]
    if not selected:
        raise ContractError("market_registry_target_scope_empty")
    bundles: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for key in selected:
        if key not in inventory_rows or key not in order_rows or key not in cash_rows:
            raise ContractError(f"market_auxiliary_input_missing:{key}")
        inventory = inventory_rows[key]
        order_state = order_rows[key]
        cash = cash_rows[key]
        if inventory.get("complete") not in (True, 1):
            raise ContractError(f"confirmed_inventory_incomplete:{key}")
        if order_state.get("known") not in (True, 1):
            raise ContractError(f"order_state_unknown:{key}")
        try:
            remaining_budget = float(inventory.get("remaining_budget_usdc"))
        except (TypeError, ValueError) as exc:
            raise ContractError(f"remaining_budget_missing:{key}") from exc
        if not math.isfinite(remaining_budget) or remaining_budget < 0:
            raise ContractError(f"remaining_budget_invalid:{key}")
        bundle = _market_quote(
            root=root,
            quotes=quotes,
            registry_rows=groups[key],
            inventory=inventory,
            order_state=order_state,
            cash_phase_input=cash.get("cash_phase_input"),
            remaining_budget_usdc=remaining_budget,
            sequence=_next_sequence(conn, *key.split("|", 1)),
            quote_freshness_policy=freshness_policy,
            policy_authority_scope=policy_authority_scope,
        )
        previous_sequence = _reuse_sequence_for_replay(conn, bundle[0])
        if previous_sequence is not None:
            bundle[0]["sequence"] = previous_sequence
        bundles.append(bundle)
    savepoint = "basket_market_ingress_batch"
    # A savepoint works both on a clean connection and when the caller has an
    # open transaction (for example after installing an isolated test
    # registry).  It keeps the batch atomic without committing unrelated
    # caller-owned work until the explicit commit path below.
    conn.execute(f"SAVEPOINT {savepoint}")
    receipts: list[dict[str, Any]] = []
    try:
        for quote, inventory, order_state in bundles:
            receipts.append(insert_market_evidence_bundle(conn, quote, inventory, order_state))
        if commit:
            conn.execute(f"RELEASE {savepoint}")
            conn.commit()
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    return {
        "schema": INGRESS_SCHEMA,
        "state": "COMMITTED" if commit else "VALIDATED_NO_WRITE",
        "manifest_sha256": str(root["snapshot_sha256"]),
        "selected_market_count": len(bundles),
        "receipts": receipts,
        "places_orders": False,
        "calls_order_gateway": False,
    }




# The exchange's minimum lot for a registry row, for producers outside this
# module (the forward P0 snapshot builder) that carry the same field.
def exchange_min_lot(row: Mapping[str, Any]) -> float:
    return _exchange_min_lot(row)

__all__ = [
    "exchange_min_lot",
    "INGRESS_SCHEMA",
    "derive_official_ws_price_tick_size",
    "load_official_ws_quotes",
    "materialize_market_evidence",
    "materialize_official_ws_evidence_cache",
]
