"""Persistent multi-scope official WS publisher inside ``market_ws``.

This coroutine is an internal task of the existing system-domain
``market_ws`` service.  It opens bounded official WS subscriptions for the
canonical registry catalog and atomically maintains one deterministic,
scope-addressable target-owned manifest.  A subscription is never larger than
the official per-connection token limit; all shards are one generation and a
failed shard invalidates that generation.
It never writes ticks, SQLite, the protected shared manifest, or any order
surface.  A disconnect stops freshness advancement, so the unchanged target
manifest expires through the existing five-second policy.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import ssl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import certifi
import websockets

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import (
    PROBABILITY_MAX_STALE_SECONDS,
    STAGES_SERVABLE_PAST_CADENCE,
    ContractError,
)
from bayes_basket_target.database import (
    DEFAULT_TARGET_DB,
    DEFAULT_TARGET_WS_MANIFEST,
)
from bayes_basket_target.market_registry import (
    LISTED_DAYS_PER_STATION,
    load_active_registry_rows,
    settlement_day_state,
)
from bayes_basket_target.resource_bounds import (
    MAX_JSON_OUTPUT_BYTES,
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    write_bounded_json,
)
from bayes_basket_target.station_registry import (
    coordinate_registry_manifest,
)
from scripts.capture_bayes_basket_target_official_ws_snapshot import (
    ONE_SHOT_CLOSE_TIMEOUT_SEC,
    WS_URL,
    _build_manifest,
    _configured_proxy,
    _iter_messages,
    _now_utc,
    _quote_from_message,
)


# Deliberately the same bound the compiler enforces.  This gate decides which
# scopes get published and the compiler decides which it will act on; when the
# two carried their own copies of "expired", scopes cleared this one and died
# at the next, which is how ZUUU sat in the candidate set all day and never
# compiled.
P1_PROBABILITY_MAX_STALE_SECONDS = PROBABILITY_MAX_STALE_SECONDS

SCHEMA = "bayes_basket_target_resident_multi_scope_ws_publisher_v1"
MANIFEST_SCHEMA = "bayes_basket_target_resident_multi_scope_ws_manifest_v1"
# The catalog is intentionally dynamic, but bounded to the canonical rollout
# domain.  A station may contribute exactly one current open scope and one
# fresh not-started scope; it cannot silently rotate through additional days.
MAX_TARGET_STATIONS = expected_station_count()
# Three concurrent dates per station once listing-day subscription landed:
# the day being settled, tomorrow, and the day after (markets list at D-2).
MAX_TARGET_SCOPES = MAX_TARGET_STATIONS * 3
MAX_TARGET_TOKENS = 2048
MAX_TOKENS_PER_CONNECTION = 400
CANONICAL_TARGET_STATIONS = frozenset(
    coordinate_registry_manifest()["station_ids"]
)
MAX_FRAME_BYTES = 8 * 1024 * 1024
TEXT_PING_SECONDS = 1.0
RECEIVE_TIMEOUT_SECONDS = 2.0
SCOPE_REFRESH_SECONDS = 5.0
# The target manifest is a complete, inline 45-scope snapshot.  Rebuilding it
# for every price-change frame makes the resident task spend its entire event
# loop encoding the same 2–3 MB JSON document.  Keep the official socket and
# in-memory books live, but publish at a bounded cadence well inside the
# 60-second quote freshness policy.  A separate liveness cadence lets a quiet
# socket advance transport evidence without manufacturing economic quote time.
MANIFEST_WRITE_INTERVAL_SECONDS = 2.0
MANIFEST_LIVENESS_WRITE_INTERVAL_SECONDS = 5.0
# A quiet token does not emit price_change frames.  Recycle the complete
# official subscription before the 60s economic quote lease can expire, so a
# fresh initial book is obtained for every leg without treating transport
# liveness as a new economic observation.
# Sets the worst-case age of a quiet book, which the consumer's 60s lease
# has to absorb along with the pipeline behind it.  Measured 2026-08-21: the
# evidence cache is written ~22s before the compile evaluates it, so a book
# sampled late in a 45s cycle arrived already 48-61s old and 41 of 45 scopes
# expired together -- they share one manifest read, so they share its phase.
# Thirty leaves the margin that arithmetic needs.  Affordable now in a way it
# was not before: the scope probe each refresh performs used to cost 36-40s
# and now costs 0.24-1.4s.
CONNECTION_REFRESH_SECONDS = 30.0
# The manifest's write gap, made visible.  The consumer's lease is 60 s and a
# planned refresh lands every ~30 s, so a gap past 20 s is already eating the
# lease.  2026-10-04 15:42-15:43Z and 15:50-15:51Z the books stopped moving
# for over 50 s and 38 scopes died on production_quote_expired, and this log
# could not say why: its lines carried no time and nothing recorded what the
# loop was doing in between.  Every line is now stamped, and a gap is logged
# on the write that ends it, with how many connections it spanned and the
# slowest synchronous catalog read inside it (the event loop stops while
# one runs).
MANIFEST_GAP_REPORT_SECONDS = 20.0
SCOPE_PROBE_REPORT_SECONDS = 5.0
_PUBLISH_TRACE: dict[str, float] = {
    "last_write": 0.0,
    "connections": 0.0,
    "slowest_scope_read": 0.0,
}


def _log(message: str) -> None:
    print(f"{_now_utc()} {message}", flush=True)


def _timed_scope(*, db_path: Path, as_of_utc: str) -> dict[str, Any]:
    """``_scope``, with a slow read logged and kept for the gap report."""

    started = time.monotonic()
    try:
        return _scope(db_path=db_path, as_of_utc=as_of_utc)
    finally:
        elapsed = time.monotonic() - started
        _PUBLISH_TRACE["slowest_scope_read"] = max(
            _PUBLISH_TRACE["slowest_scope_read"], elapsed
        )
        if elapsed > SCOPE_PROBE_REPORT_SECONDS:
            _log(f"[market_ws][target_exact] slow_scope_read:{elapsed:.1f}s")


def _note_manifest_write(now: float) -> None:
    last = _PUBLISH_TRACE["last_write"]
    gap = now - last if last > 0.0 else 0.0
    if gap > MANIFEST_GAP_REPORT_SECONDS:
        _log(
            f"[market_ws][target_exact] manifest_gap:{gap:.1f}s"
            f" connections:{int(_PUBLISH_TRACE['connections'])}"
            f" slowest_scope_read:{_PUBLISH_TRACE['slowest_scope_read']:.1f}s"
        )
    _PUBLISH_TRACE["last_write"] = now
    _PUBLISH_TRACE["connections"] = 0.0
    _PUBLISH_TRACE["slowest_scope_read"] = 0.0
RETRY_MAX_SECONDS = 10.0
_STAGE_PRIORITY = {"P0": 0, "P0-M": 1, "P1": 2}


class ScopeCatalogChanged(RuntimeError):
    """Reconnect the entire shard generation for an exact catalog."""


class ConnectionRefreshRequired(RuntimeError):
    """Planned full resubscription before a quiet book becomes stale."""


def _apply_no_live_boundary(manifest: dict[str, Any]) -> None:
    manifest["resident_exact_scope_publisher"] = {
        "schema": SCHEMA,
        "persistent_connection": True,
        "exact_scope": True,
        "multi_scope": True,
        "writes_historical_ticks": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }
    manifest["evidence_only"] = True
    manifest["authorizes_order"] = False
    manifest["calls_order_gateway"] = False
    manifest["live_submit_authorized"] = False
    manifest["orders_submitted"] = 0


def _open_target_read_only(path: Path = DEFAULT_TARGET_DB) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    conn = sqlite3.connect(
        f"file:{resolved}?mode=ro",
        uri=True,
        timeout=5.0,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("resident_target_ws_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("resident_target_ws_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _subscription_sha256(scopes: list[Mapping[str, Any]]) -> str:
    """Identity of what we must be subscribed to, and nothing else.

    A websocket subscription is defined by its token set.  ``catalog_sha256``
    additionally covers the probability metadata the manifest publishes for
    consumers -- envelope id, stage, freshness -- and comparing *that* to
    decide whether to reconnect tore the whole socket generation down every
    time any station produced a new checkpoint.  Across 45 stations that is
    continuous: P0 recompiles dozens of times a station-day and P1 follows
    every observation, so the books were being resubscribed for reasons that
    had nothing to do with which books we need.  Measured 2026-08-21: the
    effective re-observation period ran 75-190s against a 60s lease, with 328
    self-inflicted connection teardowns in one log.

    Probability metadata changing is a manifest rewrite, not a reconnect.
    """

    core = [
        {
            "scope_key": str(scope["scope_key"]),
            "station_id": str(scope["station_id"]),
            "event_date": str(scope["event_date"]),
            "target_timezone": str(scope["target_timezone"]),
            "tokens": list(scope["tokens"]),
            "registry_snapshot_sha256": str(
                scope["registry_scope"]["registry_snapshot_sha256"]
            ),
        }
        for scope in scopes
    ]
    return hashlib.sha256(
        json.dumps(
            core,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _catalog_sha256(scopes: list[Mapping[str, Any]]) -> str:
    core = [
        {
            "scope_key": str(scope["scope_key"]),
            "station_id": str(scope["station_id"]),
            "event_date": str(scope["event_date"]),
            "target_timezone": str(scope["target_timezone"]),
            "tokens": list(scope["tokens"]),
            "probability_stage": str(scope["probability_stage"]),
            "probability_envelope_id": str(
                scope["probability_envelope_id"]
            ),
            "probability_fresh_until_utc": str(
                scope["probability_fresh_until_utc"]
            ),
            "probability_evidence_fresh": bool(
                scope["probability_evidence_fresh"]
            ),
            "registry_snapshot_sha256": str(
                scope["registry_scope"]["registry_snapshot_sha256"]
            ),
        }
        for scope in scopes
    ]
    return hashlib.sha256(
        json.dumps(
            core,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _connection_shards(tokens: Sequence[str]) -> list[dict[str, Any]]:
    """Partition the canonical token set deterministically for official WS.

    This is a transport partition only: scopes remain the authority boundary
    and all shards must be complete before their combined manifest is written.
    """

    normalized = sorted({str(token).strip() for token in tokens if str(token).strip()})
    if not normalized or len(normalized) > MAX_TARGET_TOKENS:
        raise ContractError("resident_target_ws_token_catalog_invalid")
    return [
        {
            "shard_id": index,
            "tokens": normalized[offset : offset + MAX_TOKENS_PER_CONNECTION],
        }
        for index, offset in enumerate(
            range(0, len(normalized), MAX_TOKENS_PER_CONNECTION)
        )
    ]


def _future_probability_expired(
    catalog: Mapping[str, Any],
    *,
    observed_at_utc: str,
) -> bool:
    """Only a future preselection scope requires fresh probability.

    An open scope remains subscribed when its latest P1 expires so the next
    official observation can immediately rebuild P1 against a current book.
    Probability consumers still fail closed on the stale P1 itself.
    """

    observed = _parse_utc(observed_at_utc)
    return any(
        str(scope.get("event_day_state") or "") == "not_started"
        # A listing-day scope carries no probability claim at all; an empty
        # fresh-until is absence, not expiry, and must not churn the
        # connection.
        and str(scope.get("probability_fresh_until_utc") or "").strip()
        # Fires on the transition, not on the state: a scope the catalog
        # already carries as stale (user ruling, 2026-09-06: a not-started
        # day keeps its book) has nothing left to refresh, and re-raising
        # on every frame for it is the 2026-08-14 freeze.
        and bool(scope.get("probability_evidence_fresh", True))
        and observed >= _parse_utc(
            str(scope["probability_fresh_until_utc"])
        )
        for scope in catalog.get("scopes", ())
    )


def _scope(
    *,
    db_path: Path = DEFAULT_TARGET_DB,
    as_of_utc: str,
) -> dict[str, Any]:
    """Return a bounded catalog of open and pre-event probability scopes.

    This is deliberately separate from ``load_started_registry_rows``.
    Truth, P1 production, and hard-NO consumers keep their started-day gate;
    only this evidence-only WS subscriber may include a not-started day, and
    then only when its current SSOT is a fresh P0/P0-M artifact.
    """

    as_of = _parse_utc(as_of_utc)
    # sqlite3.Connection.__exit__ only commits/rolls back; it does not close
    # the connection.  This resident coroutine rebuilds the catalog across
    # reconnects, so an ordinary ``with conn`` leaks read snapshots and can
    # indefinitely pin the target WAL.  ``closing`` makes the ownership
    # boundary explicit.
    with closing(_open_target_read_only(db_path)) as conn:
        # Closed event days are discarded below, so reading them costs a
        # scan and buys nothing.  Two days back clears every settlement
        # offset in the fleet (UTC-8 to UTC+13) with a full day to spare,
        # and the table keeps ~24 days of append-only history: measured
        # 2026-08-21, the bound drops 80% of the 148,578 committed rows.
        oldest_open_event_date = (
            _parse_utc(as_of_utc).date() - timedelta(days=2)
        ).isoformat()
        probability_rows = conn.execute(
            "SELECT envelope_id,stage,checkpoint_id,station_id,event_date,"
            "settlement_timezone,decision_at_utc,fresh_until_utc "
            "FROM basket_probability_envelope "
            "WHERE state='COMMITTED' "
            "AND event_date>=? "
            "AND stage IN ('P0','P0-M','P1') "
            "ORDER BY station_id,event_date,decision_at_utc,stage,envelope_id",
            (oldest_open_event_date,),
        ).fetchall()
        # Match ``select_current_stage`` exactly without loading the full
        # probability vectors: first select the latest submitted checkpoint
        # within each stage, then select the highest submitted stage.  A
        # newer raw P0/P0-M must never replace an already-visible P1.
        current_by_stage: dict[
            tuple[str, str], dict[str, sqlite3.Row]
        ] = {}
        for row in probability_rows:
            decision_at = _parse_utc(str(row["decision_at_utc"]))
            if decision_at > as_of:
                continue
            station_id = str(row["station_id"] or "").strip().upper()
            event_date = str(row["event_date"] or "")
            if not station_id or not event_date:
                raise ContractError("resident_target_ws_probability_scope_invalid")
            if station_id not in CANONICAL_TARGET_STATIONS:
                continue
            key = (station_id, event_date)
            stage = str(row["stage"] or "").strip().upper()
            if stage not in _STAGE_PRIORITY:
                raise ContractError("resident_target_ws_probability_stage_invalid")
            candidates = current_by_stage.setdefault(key, {})
            previous = candidates.get(stage)
            if previous is None:
                candidates[stage] = row
                continue
            previous_decision_at = _parse_utc(
                str(previous["decision_at_utc"])
            )
            if decision_at > previous_decision_at:
                candidates[stage] = row
            elif decision_at == previous_decision_at:
                # The materialized stage chain has no deterministic basis for
                # resolving two checkpoints in the same stage at the same
                # decision clock.  Mirror ``select_current_stage`` and stop
                # the publisher rather than selecting by envelope ID.
                raise ContractError(
                    "resident_target_ws_probability_stage_checkpoint_ambiguous"
                )

        current_probability = {
            key: rows[max(rows, key=lambda stage: _STAGE_PRIORITY[stage])]
            for key, rows in current_by_stage.items()
        }

        active_rows = load_active_registry_rows(conn)
        rows_by_scope: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in active_rows:
            station_id = str(row.get("station_id") or "").strip().upper()
            event_date = str(row.get("event_date") or "")
            if not station_id or not event_date:
                raise ContractError("resident_target_ws_registry_scope_invalid")
            if station_id not in CANONICAL_TARGET_STATIONS:
                continue
            rows_by_scope.setdefault((station_id, event_date), []).append(row)

        scopes: list[dict[str, Any]] = []
        # Listing-day scopes (registry-only, no envelope) were unioned in
        # here on 2026-08-14 and reverted the same hour: with ~300 bookless
        # tokens subscribed the manifest write gate never completed and the
        # whole publisher output froze, starving every consumer of current
        # quotes.  The listing-first subscription is still the right goal --
        # it returns behind a write path that publishes unmaterialized
        # tokens without waiting for them.
        # The catalog is the union of what we believe (envelope scopes) and
        # what the exchange lists (registry scopes).  Rootless listing-day
        # scopes subscribe with no probability claims; stale-rooted future
        # scopes are still skipped by the admission below, which is what
        # keeps the expired-probability gate a no-op in steady state.
        catalog_keys = sorted(
            set(current_probability) | set(rows_by_scope)
        )
        for station_id, event_date in catalog_keys:
            probability = current_probability.get(
                (station_id, event_date)
            )
            rows = rows_by_scope.get((station_id, event_date)) or []
            if not rows:
                continue
            timezones = {
                str(row.get("settlement_timezone") or "").strip()
                for row in rows
            }
            if len(timezones) != 1 or "" in timezones:
                raise ContractError(
                    "resident_target_ws_settlement_timezone_invalid"
                )
            timezone_name = next(iter(timezones))
            if probability is not None:
                probability_timezone = str(
                    probability["settlement_timezone"] or ""
                ).strip()
                if probability_timezone != timezone_name:
                    raise ContractError(
                        "resident_target_ws_probability_registry_timezone_mismatch"
                    )
            state = str(settlement_day_state(
                event_date,
                as_of_utc=as_of_utc,
                settlement_timezone=timezone_name,
            )["state"])
            if probability is None:
                # Listed but not yet believed: subscribe to the book, carry
                # no probability claims, and let the preselection's own
                # evidence gate keep it un-actionable until a root lands.
                stage = ""
                probability_age_seconds = 0.0
                within_cadence = False
                probability_evidence_fresh = False
                probability_past_cadence = False
            else:
                stage = str(probability["stage"])
            fresh_until = (
                _parse_utc(str(probability["fresh_until_utc"]))
                if probability is not None
                else None
            )
            # The envelope selected above is already the latest committed one
            # for this scope; ``fresh_until`` is the cadence P1 re-rolls on,
            # not an expiry of what it says.  Past that instant the latest
            # reading is still the only reading -- there is nothing newer to
            # prefer -- so treating it as absent stood a station down for
            # holding slightly old information rather than none.  P1 rolls
            # hourly off arriving observations, so every scope spends part of
            # every hour past its window; 26 of 48 sat out one recovery this
            # way.
            #
            # Usability is therefore bounded by age, not by the cadence.
            # Beyond the staleness limit a probability is not slightly old,
            # it describes a different state of the day, and refusing is
            # right.
            #
            # This governs the *probability* only.  P1's hard bound comes
            # from the observed high, which only ever rises, so an older
            # bound is a weaker one and would readmit buckets the day has
            # already ruled out.  The bound is re-derived from current
            # observations downstream and is never carried along with the
            # envelope; nothing here relaxes it.
            if probability is not None:
                probability_age_seconds = max(
                    0.0, (as_of - fresh_until).total_seconds()
                )
                within_cadence = as_of < fresh_until
                probability_evidence_fresh = within_cadence or (
                    stage in STAGES_SERVABLE_PAST_CADENCE
                    and probability_age_seconds
                    <= P1_PROBABILITY_MAX_STALE_SECONDS
                )
                probability_past_cadence = (
                    probability_evidence_fresh and not within_cadence
                )
            if state == "closed":
                continue
            if state == "not_started" and stage not in {"P0", "P0-M", ""}:
                continue
            # A not-started day keeps its book while its P0 is stale (user
            # ruling, 2026-09-06).  Until then a stale future scope was
            # dropped here, so at any instant only the stations whose P0
            # had refreshed inside its six-hour window were subscribed --
            # 20 of 45 for 2026-09-06 on the evening of 09-05 -- and when
            # the rest came back the forward lane had no book to compile
            # against.  The scope stays, flagged ``probability_evidence_
            # fresh=False``; the compile still refuses a stale probability.
            # The 2026-08-14 freeze this line once guarded against (an
            # expired overnight scope raising ScopeCatalogChanged on every
            # frame) is closed in ``_future_probability_expired`` itself,
            # which now fires once per expiry rather than while expired.
            if state not in {"not_started", "open"}:
                continue
            tokens = sorted({
                str(row.get("yes_token") or "").strip()
                for row in rows
                if str(row.get("yes_token") or "").strip()
            })
            if not tokens or len(tokens) != len(rows):
                raise ContractError(
                    "resident_target_ws_token_scope_invalid"
                )
            snapshot_row = conn.execute(
                "SELECT s.snapshot_sha256 FROM "
                "basket_target_market_registry_snapshot s "
                "WHERE EXISTS (SELECT 1 FROM "
                "basket_target_market_registry r "
                "WHERE r.snapshot_sha256=s.snapshot_sha256 "
                "AND r.station_id=? AND r.event_date=?) "
                "ORDER BY s.ingested_at_utc DESC,"
                "s.snapshot_sha256 DESC LIMIT 1",
                (station_id, event_date),
            ).fetchone()
            if snapshot_row is None:
                raise ContractError(
                    "resident_target_ws_registry_snapshot_missing"
                )
            scopes.append({
                "scope_key": f"{station_id}|{event_date}",
                "station_id": station_id,
                "station_ids": [station_id],
                "event_date": event_date,
                "target_timezone": timezone_name,
                "event_day_state": state,
                "probability_stage": stage,
                "probability_envelope_id": (
                    str(probability["envelope_id"])
                    if probability is not None else ""
                ),
                "probability_fresh_until_utc": (
                    str(probability["fresh_until_utc"])
                    if probability is not None else ""
                ),
                "probability_evidence_fresh": probability_evidence_fresh,
                # Visible so a consumer, and an audit, can tell a reading
                # taken inside the roll cadence from the latest one carried
                # past it.  Both are usable; they are not the same claim.
                "probability_past_cadence": probability_past_cadence,
                "probability_age_seconds": round(
                    probability_age_seconds, 3
                ),
                "tokens": tokens,
                "registry_scope": {
                    "event_date": event_date,
                    "station_allowlist": [station_id],
                    "settlement_timezones_by_station": {
                        station_id: timezone_name,
                    },
                    "markets_by_token": {
                        str(row["yes_token"]): {
                            "event_date": event_date,
                            "outcome": str(row["outcome"]),
                            "settlement_timezone": timezone_name,
                            "slug": str(row["slug"]),
                            "station_id": station_id,
                        }
                        for row in rows
                    },
                    "registry_snapshot_sha256": str(snapshot_row[0] or ""),
                },
            })

    if not scopes:
        raise ContractError("resident_target_ws_scope_missing")
    scopes.sort(key=lambda item: str(item["scope_key"]))
    # One open day per station, and up to LISTED_DAYS_PER_STATION listed
    # (not-started) days -- the D-2 listing window charter 48(e) admits into
    # the catalog.  Until 2026-09-02 this guard allowed one scope per state,
    # which held only while the registry export dropped the second listed
    # day; the moment that export carried it, every frame died here
    # (``station_state_ambiguous`` 15:06Z onward), the manifest froze, and
    # seventy scopes failed on production_quote_expired in one cycle.
    # Scopes are keyed by (station, event_date), so the listed days are
    # distinct by construction; what is ambiguous is two *open* days for
    # one station, or more listed days than the window can hold.
    station_states: dict[str, dict[str, int]] = {}
    for scope in scopes:
        station_id = str(scope["station_id"])
        state = str(scope["event_day_state"])
        counts = station_states.setdefault(station_id, {})
        counts[state] = counts.get(state, 0) + 1
        limit = (
            LISTED_DAYS_PER_STATION if state == "not_started" else 1
        )
        if counts[state] > limit:
            raise ContractError("resident_target_ws_station_state_ambiguous")
    if len(station_states) > MAX_TARGET_STATIONS:
        raise ContractError("resident_target_ws_station_cap_exceeded")
    if len(scopes) > MAX_TARGET_SCOPES:
        raise ContractError("resident_target_ws_scope_cap_exceeded")
    all_tokens = [
        token
        for scope in scopes
        for token in scope["tokens"]
    ]
    if (
        len(all_tokens) != len(set(all_tokens))
        or len(all_tokens) > MAX_TARGET_TOKENS
    ):
        raise ContractError("resident_target_ws_token_catalog_invalid")
    tokens = sorted(all_tokens)
    return {
        "scopes": scopes,
        "tokens": tokens,
        "connection_shards": _connection_shards(tokens),
        "catalog_sha256": _catalog_sha256(scopes),
        "subscription_sha256": _subscription_sha256(scopes),
    }


def _manifest_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _build_multi_scope_manifest(
    catalog: Mapping[str, Any],
    *,
    quotes: Mapping[str, Mapping[str, Any]],
    observed_at: str,
    started_at: str,
) -> dict[str, Any]:
    shards = list(catalog.get("connection_shards") or _connection_shards(
        list(catalog["tokens"])
    ))
    scope_manifests: dict[str, dict[str, Any]] = {}
    scope_summaries: list[dict[str, Any]] = []
    for scope in catalog["scopes"]:
        scope_key = str(scope["scope_key"])
        scope_tokens = list(scope["tokens"])
        scope_quotes = {
            token: dict(quotes[token])
            for token in scope_tokens
            if token in quotes
        }
        manifest = _build_manifest(
            event_date=str(scope["event_date"]),
            as_of_utc=observed_at,
            tokens=scope_tokens,
            quotes=scope_quotes,
            shard_size=MAX_TOKENS_PER_CONNECTION,
            started_at_utc=started_at,
            finished_at_utc=observed_at,
            errors=[],
            timeout_sec=RECEIVE_TIMEOUT_SECONDS,
            target_timezone=str(scope["target_timezone"]),
            station_scope=scope["station_ids"],
            registry_scope=scope["registry_scope"],
        )
        _apply_no_live_boundary(manifest)
        manifest["scope_key"] = scope_key
        manifest["event_day_state"] = str(scope["event_day_state"])
        manifest["probability_authority"] = {
            "stage": str(scope["probability_stage"]),
            "envelope_id": str(scope["probability_envelope_id"]),
            "fresh_until_utc": str(
                scope["probability_fresh_until_utc"]
            ),
            "evidence_fresh": bool(
                scope["probability_evidence_fresh"]
            ),
        }
        manifest["snapshot_sha256"] = _manifest_sha256({
            key: value
            for key, value in manifest.items()
            if key != "snapshot_sha256"
        })
        scope_manifests[scope_key] = manifest
        scope_summaries.append({
            "scope_key": scope_key,
            "station_id": str(scope["station_id"]),
            "event_date": str(scope["event_date"]),
            "event_day_state": str(scope["event_day_state"]),
            "probability_stage": str(scope["probability_stage"]),
            "probability_fresh_until_utc": str(
                scope["probability_fresh_until_utc"]
            ),
            "probability_evidence_fresh": bool(
                scope["probability_evidence_fresh"]
            ),
            # Carried through rather than recomputed: the summary is what the
            # worker reads, so a field that stops here is a field the rest of
            # the system cannot see.
            "probability_past_cadence": bool(
                scope.get("probability_past_cadence")
            ),
            "probability_age_seconds": float(
                scope.get("probability_age_seconds") or 0.0
            ),
            "token_count": len(scope_tokens),
            "scope_manifest_sha256": manifest["snapshot_sha256"],
        })
    complete = (
        bool(scope_manifests)
        and len(quotes) == len(catalog["tokens"])
        and all(
            all(token in quotes for token in shard["tokens"])
            for shard in shards
        )
        and all(
            value.get("state") == "connected"
            for value in scope_manifests.values()
        )
    )
    root: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "state": "connected" if complete else "blocked",
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
        "quote_storage": "inline_scopes_v1",
        "scope_count": len(scope_manifests),
        "subscribed_token_count": len(catalog["tokens"]),
        "received_book_token_count": len(quotes),
        "connection_shard_count": len(shards),
        "connection_shards": [
            {
                "shard_id": int(shard["shard_id"]),
                "token_count": len(shard["tokens"]),
                "tokens_sha256": hashlib.sha256(
                    json.dumps(
                        list(shard["tokens"]),
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
            for shard in shards
        ],
        "catalog_sha256": str(catalog["catalog_sha256"]),
        "connection_observed_at": observed_at,
        "scopes": scope_summaries,
        "scope_manifests": scope_manifests,
    }
    _apply_no_live_boundary(root)
    root["snapshot_sha256"] = _manifest_sha256(root)
    return root


def _assert_output_disk_budget(path: Path) -> None:
    destination = Path(path).expanduser().resolve()
    parent = destination.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    try:
        free_bytes = int(shutil.disk_usage(parent).free)
    except OSError as exc:
        raise ContractError(
            "resident_target_ws_output_disk_probe_failed"
        ) from exc
    if free_bytes < MIN_TARGET_FREE_BYTES + MAX_JSON_OUTPUT_BYTES:
        raise ContractError(
            "resident_target_ws_output_min_free_space_guard"
        )


def _quote_economic_state(quote: Mapping[str, Any]) -> dict[str, Any]:
    """Return price/depth state without transport or observation clocks."""

    return {
        key: value
        for key, value in quote.items()
        if key not in {
            "book_observed_at",
            "created_at",
            "_stream_confirmed_at",
        }
    }


def _merge_book_quote(
    quotes: dict[str, dict[str, Any]],
    token_id: str,
    incoming: Mapping[str, Any],
    *,
    observed_at: str = "",
) -> bool:
    """Take a full venue book as a real re-observation of this token.

    A ``book`` frame is the venue answering "here is this token's book right
    now".  When the answer matches what we hold, that is an unchanged
    observation, not a missing one, and its clock must advance: this is the
    whole point of ``CONNECTION_REFRESH_SECONDS`` recycling the subscription
    before the economic lease expires.  Preserving the old clock here defeated
    that -- a genuinely quiet leg kept its first stamp forever and aged out of
    the 60s lease permanently, no matter how many times the venue re-sent its
    book (2026-08-21: every consumer scope shared one stamp ~161s behind the
    compile clock and 24 of 26 candidates died on ``production_quote_expired``).

    This is not the ``_stream_confirmed_at`` shortcut and must never become
    it.  A PING proves the socket is alive and says nothing about this book;
    a ``book`` frame carries the book.  ``created_at`` therefore stays put --
    it marks when this economic state first appeared, which an unchanged
    re-observation does not move.  The return value still reports economic
    change only, so callers keep their existing dirty/publish semantics.
    """

    current = quotes.get(token_id)
    if (
        current is not None
        and _quote_economic_state(current)
        == _quote_economic_state(incoming)
    ):
        if observed_at:
            current["book_observed_at"] = observed_at
        return False
    merged = dict(incoming)
    if (
        observed_at
        and current is not None
        and str(current.get("created_at") or "")
        and not str(merged.get("created_at") or "")
    ):
        merged["created_at"] = observed_at
    quotes[token_id] = merged
    return True


def _apply_price_changes(
    quotes: dict[str, dict[str, Any]],
    message: Mapping[str, Any],
    *,
    observed_at: str,
) -> set[str]:
    if str(message.get("event_type") or "").strip() != "price_change":
        return set()
    before_by_token: dict[str, tuple[dict[str, Any], str, str]] = {}
    confirmed_tokens: set[str] = set()
    for raw in message.get("price_changes") or []:
        if not isinstance(raw, Mapping):
            continue
        token_id = str(raw.get("asset_id") or "").strip()
        quote = quotes.get(token_id)
        if quote is None:
            continue
        confirmed_tokens.add(token_id)
        if token_id not in before_by_token:
            before_by_token[token_id] = (
                _quote_economic_state(quote),
                str(quote.get("book_observed_at") or ""),
                str(quote.get("created_at") or ""),
            )
        side = str(raw.get("side") or "").strip().upper()
        key = "bids" if side in {"BUY", "BID"} else (
            "asks" if side in {"SELL", "ASK"} else ""
        )
        if not key:
            continue
        try:
            price = float(raw.get("price"))
            size = float(raw.get("size"))
        except (TypeError, ValueError):
            continue
        levels = {
            float(item["price"]): float(item["size"])
            for item in quote.get(key) or []
            if isinstance(item, Mapping)
        }
        if size <= 0.0:
            levels.pop(price, None)
        else:
            levels[price] = size
        ordered = sorted(
            levels.items(),
            reverse=(key == "bids"),
        )[:5]
        quote[key] = [
            {"price": level_price, "size": level_size}
            for level_price, level_size in ordered
        ]
        quote["best_bid"] = (
            quote["bids"][0]["price"] if quote.get("bids") else None
        )
        quote["best_ask"] = (
            quote["asks"][0]["price"] if quote.get("asks") else None
        )
    for token_id, (before, old_observed_at, old_created_at) in (
        before_by_token.items()
    ):
        quote = quotes[token_id]
        if _quote_economic_state(quote) != before:
            quote["book_observed_at"] = observed_at
            quote["created_at"] = observed_at
        else:
            if old_observed_at:
                quote["book_observed_at"] = old_observed_at
            else:
                quote.pop("book_observed_at", None)
            if old_created_at:
                quote["created_at"] = old_created_at
            else:
                quote.pop("created_at", None)
    return confirmed_tokens


def _seed_books(
    previous: Mapping[str, Mapping[str, Any]] | None,
    tokens: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """The books the venue actually sent, for the tokens a generation carries.

    Only entries with ``asks`` and ``bids`` lists qualify: those are frames
    the venue answered for that token, each with its own economic clock.  A
    placeholder ``_confirm_shard_stream`` materialised carries neither; it
    describes a socket, not a market, and the next generation rebuilds it.
    """

    if not previous:
        return {}
    seeded: dict[str, dict[str, Any]] = {}
    for token in tokens:
        quote = previous.get(token)
        if not isinstance(quote, Mapping):
            continue
        if not isinstance(quote.get("asks"), list) or not isinstance(
            quote.get("bids"), list
        ):
            continue
        seeded[str(token)] = copy.deepcopy(dict(quote))
    return seeded


def _bookless_frame_token(message: Mapping[str, Any]) -> str:
    """The token a ``book`` frame answers for when it carries no usable book.

    ``_quote_from_message`` returns nothing for a book frame whose two sides
    are both empty (or crossed), so a token whose book has been fully
    cancelled leaves no trace on the quotes it updates.  Inside one socket
    generation that never mattered -- the next planned refresh started empty
    and the token surfaced as a placeholder.  A generation seeded from the
    previous one would carry the cancelled book forward instead, honestly
    stamped but describing a market that no longer exists; this names the
    token so the coordinator can drop it and let the placeholder say so.
    """

    event_type = str(
        message.get("event_type") or message.get("type") or ""
    ).lower()
    if event_type not in {"book", "orderbook", "book_snapshot"} and not (
        isinstance(message.get("asks"), list)
    ):
        return ""
    return str(message.get("asset_id") or message.get("token_id") or "").strip()


def _confirm_shard_stream(
    quotes: dict[str, dict[str, Any]],
    shard_tokens: set[str],
    *,
    observed_at: str,
) -> None:
    """Advance only transport freshness for every known book on one shard."""

    for token_id in shard_tokens:
        if token_id in quotes:
            quotes[token_id]["_stream_confirmed_at"] = observed_at
        else:
            # A confirmed shard stream that has produced no book event for a
            # token is reporting a real market state: a freshly listed
            # bucket with an empty book.  Materialise it as an explicitly
            # empty quote so the completeness invariant -- every token
            # accounted for before the manifest writes -- holds for listing
            # -day scopes instead of freezing the entire publisher output
            # until the last penny bucket trades.  The reader's
            # ``_quote_is_complete`` classifies these as unmaterialized, so
            # nothing downstream mistakes an empty book for a price.
            #
            # That reading is only true of a token the venue has had time
            # to answer for.  On a fresh socket the first frame lands before
            # the initial book dump has finished, and every token still
            # waiting would be materialised here as "nobody has quoted it"
            # -- 400 to 800 of them at once on every planned refresh
            # (measured 2026-09-02).  ``_publish_connection`` therefore
            # seeds a generation with the previous generation's books, so
            # this branch is reached only for a token no socket has ever
            # carried a book for.
            quotes[token_id] = {
                "token_id": token_id,
                "side": "YES",
                "book_observed_at": observed_at,
                "created_at": observed_at,
                "_stream_confirmed_at": observed_at,
            }


async def _text_ping(ws: Any) -> None:
    while True:
        await asyncio.sleep(TEXT_PING_SECONDS)
        await ws.send("PING")


async def _receive_connection_shard(
    shard: Mapping[str, Any],
    *,
    queue: asyncio.Queue[tuple[int, str | None]],
    proxy: str | None,
) -> None:
    """Stream one bounded official-WS shard into the generation coordinator.

    A normal return is also a failure: a generation is valid only while every
    shard remains connected.  This function deliberately has no disk or DB
    surface; the coordinator is the sole atomic manifest writer.
    """

    shard_id = int(shard["shard_id"])
    tokens = list(shard["tokens"])
    if not tokens or len(tokens) > MAX_TOKENS_PER_CONNECTION:
        raise ContractError("resident_target_ws_connection_token_cap_exceeded")
    subscription = {
        "assets_ids": tokens,
        "type": "market",
        "custom_feature_enabled": True,
    }
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connect_kwargs: dict[str, Any] = {
        "ssl": ssl_context,
        "ping_interval": 20,
        "ping_timeout": 20,
        "open_timeout": 20,
        "close_timeout": ONE_SHOT_CLOSE_TIMEOUT_SEC,
        "max_size": MAX_FRAME_BYTES,
    }
    # Always pass the proxy explicitly.  Omitting the kwarg does not mean
    # "direct": websockets defaults to ``proxy=True`` and silently honours
    # whatever HTTPS_PROXY the process inherited -- which is how a "direct"
    # publisher spent four hours dialling a dead local proxy on 7897 while
    # the listener beside it, which passes the kwarg, stayed connected.
    # ``None`` is the explicit spelling of direct.
    connect_kwargs["proxy"] = proxy if proxy else None
    async with websockets.connect(WS_URL, **connect_kwargs) as ws:
        await ws.send(json.dumps(subscription))
        ping_task = asyncio.create_task(_text_ping(ws))
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(),
                        timeout=RECEIVE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    await queue.put((shard_id, None))
                    continue
                await queue.put((shard_id, raw))
        finally:
            ping_task.cancel()


async def _publish_connection(
    catalog: Mapping[str, Any],
    *,
    db_path: Path,
    output_path: Path,
    proxy: str | None,
    carried_books: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Publish a complete multi-shard generation or publish nothing.

    Any task failure, disconnect, or catalog change cancels every shard before
    the outer resident loop starts a new generation.  This avoids blending
    quotes from different catalog generations.

    ``carried_books`` is the previous generation's books keyed by token, and
    this coroutine both reads and rewrites it: the generation starts from
    them and leaves its own behind for the next.  A book is a per-token fact
    the venue stated, stamped with its own ``book_observed_at``; the socket
    that carried it closing does not unsay it, and the fresh dump on the new
    socket re-observes it on arrival (``_merge_book_quote``).  What never
    crosses a generation is a *manifest*: that is still built only from this
    generation's ``quotes`` and written only once every shard of this
    generation has confirmed its stream.

    Why the seed exists.  Measured 2026-09-02: every planned refresh tore all
    three sockets down, and the venue's initial book dump for 400 tokens per
    socket took 10-53 s under load.  The first frame or two-second heartbeat
    on each shard confirmed the stream, ``_confirm_shard_stream`` materialised
    a bookless placeholder for every token still waiting, the completeness
    gate saw every token accounted for, and the complete manifest on disk was
    overwritten with 400-800 tokens marked unmaterialized.  Sampled over four
    minutes the file spent 40% of the time in that state; a worker reading it
    then refused 43 of 44 open scopes (``official_ws_selected_bucket_missing``)
    and 62 scopes died on ``production_quote_expired`` behind them.
    """

    tokens = list(catalog["tokens"])
    token_set = set(tokens)
    shards = list(catalog.get("connection_shards") or _connection_shards(tokens))
    shard_ids = [int(shard.get("shard_id", -1)) for shard in shards]
    shard_tokens = [
        str(token).strip()
        for shard in shards
        for token in (shard.get("tokens") or ())
    ]
    if (
        not shards
        or len(tokens) != len(token_set)
        or len(shard_ids) != len(set(shard_ids))
        or any(len(shard.get("tokens") or ()) > MAX_TOKENS_PER_CONNECTION for shard in shards)
        or len(shard_tokens) != len(set(shard_tokens))
        or sorted(shard_tokens) != sorted(tokens)
    ):
        raise ContractError("resident_target_ws_shard_catalog_invalid")
    assert_process_memory_budget(projected_bytes=len(tokens) * 64 * 1024)
    started_at = _now_utc()
    quotes: dict[str, dict[str, Any]] = _seed_books(carried_books, tokens)
    confirmed_shards: set[int] = set()
    queue: asyncio.Queue[tuple[int, str | None]] = asyncio.Queue()
    tokens_by_shard = {
        int(shard["shard_id"]): set(shard["tokens"])
        for shard in shards
    }
    tasks = {
        int(shard["shard_id"]): asyncio.create_task(
            _receive_connection_shard(shard, queue=queue, proxy=proxy)
        )
        for shard in shards
    }
    next_scope_probe = time.monotonic() + SCOPE_REFRESH_SECONDS
    connection_refresh_deadline = (
        time.monotonic() + CONNECTION_REFRESH_SECONDS
    )
    last_manifest_write = 0.0
    economic_dirty = False
    liveness_dirty = False
    try:
        while True:
            for shard_id, task in tasks.items():
                if task.done():
                    # A closed socket must not leave a partially fresh root
                    # manifest behind while other shards continue advancing.
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        raise ConnectionError(
                            f"resident_target_ws_shard_cancelled:{shard_id}"
                        ) from None
                    except Exception:
                        raise
                    raise ConnectionError(
                        f"resident_target_ws_shard_disconnected:{shard_id}"
                    )
            try:
                shard_id, raw = await asyncio.wait_for(
                    queue.get(), timeout=RECEIVE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                shard_id, raw = -1, None
            observed_at = _now_utc()
            if raw is not None:
                shard_tokens = tokens_by_shard.get(shard_id)
                if shard_tokens is None:
                    raise ContractError("resident_target_ws_unknown_shard_event")
                confirmed_shards.add(shard_id)
                for message in _iter_messages(raw):
                    parsed = _quote_from_message(message, observed_at=observed_at)
                    if parsed is None:
                        # A book frame with nothing on either side is the
                        # venue saying this token's book is gone.  Drop the
                        # carried book so the shard confirmation below
                        # materialises the placeholder that now describes it.
                        emptied = _bookless_frame_token(message)
                        if emptied and emptied in token_set:
                            if emptied not in shard_tokens:
                                raise ContractError(
                                    "resident_target_ws_shard_token_escape"
                                )
                            if quotes.pop(emptied, None) is not None:
                                economic_dirty = True
                    if parsed is not None and parsed[0] in token_set:
                        token_id, incoming = parsed
                        if token_id not in shard_tokens:
                            raise ContractError(
                                "resident_target_ws_shard_token_escape"
                            )
                        _merge_book_quote(
                            quotes,
                            token_id,
                            incoming,
                            observed_at=observed_at,
                        )
                        quotes[token_id]["_stream_confirmed_at"] = observed_at
                    confirmed = _apply_price_changes(
                        quotes, message, observed_at=observed_at
                    )
                    if parsed is not None or confirmed:
                        economic_dirty = True
                    for token_id in confirmed:
                        if token_id not in shard_tokens:
                            raise ContractError(
                                "resident_target_ws_shard_token_escape"
                            )
                # Any authenticated official message confirms the whole
                # socket generation, including economically unchanged books
                # on the same bounded shard. This transport clock never
                # changes their economic quote identity.
                _confirm_shard_stream(
                    quotes,
                    shard_tokens,
                    observed_at=observed_at,
                )
                liveness_dirty = True
            elif shard_id in tokens_by_shard:
                # The receive task emits a bounded timeout heartbeat even
                # when the official socket has no price frame.  Refresh only
                # the transport lease in memory; the periodic manifest write
                # below keeps this separate from economic quote timestamps.
                confirmed_shards.add(shard_id)
                _confirm_shard_stream(
                    quotes,
                    tokens_by_shard[shard_id],
                    observed_at=observed_at,
                )
                liveness_dirty = True
            if _future_probability_expired(catalog, observed_at_utc=observed_at):
                raise ScopeCatalogChanged("resident_target_ws_probability_expired")
            if time.monotonic() >= connection_refresh_deadline:
                raise ConnectionRefreshRequired(
                    "resident_target_ws_planned_refresh"
                )
            if time.monotonic() >= next_scope_probe:
                refreshed = _timed_scope(db_path=db_path, as_of_utc=observed_at)
                if (
                    refreshed["subscription_sha256"]
                    != catalog["subscription_sha256"]
                ):
                    # The token set moved: this socket is subscribed to the
                    # wrong thing and has to be rebuilt.
                    raise ScopeCatalogChanged("resident_target_ws_catalog_changed")
                if refreshed["catalog_sha256"] != catalog["catalog_sha256"]:
                    # Same tokens, newer probability metadata.  Adopt it and
                    # republish; tearing the sockets down here is what made
                    # every checkpoint anywhere in the network cost the whole
                    # book cadence.
                    catalog = refreshed
                    economic_dirty = True
                next_scope_probe = time.monotonic() + SCOPE_REFRESH_SECONDS
            if raw is None or len(quotes) != len(tokens):
                continue
            # Seeded books make every token present before every socket has
            # opened.  A manifest still claims one connected generation, so
            # it waits for each shard to have confirmed its own stream -- the
            # invariant the placeholders used to enforce implicitly, since
            # they only ever came from a confirmed shard.
            if confirmed_shards != set(tokens_by_shard):
                continue
            now = time.monotonic()
            write_due = (
                last_manifest_write <= 0.0
                or (
                    economic_dirty
                    and now - last_manifest_write >= MANIFEST_WRITE_INTERVAL_SECONDS
                )
                or (
                    liveness_dirty
                    and now - last_manifest_write >= MANIFEST_LIVENESS_WRITE_INTERVAL_SECONDS
                )
            )
            if not write_due:
                continue
            # Recheck immediately before the sole write.  A closed or failed
            # shard never permits a partial or cross-generation publication.
            if any(task.done() for task in tasks.values()):
                continue
            manifest = _build_multi_scope_manifest(
                catalog,
                quotes=quotes,
                observed_at=observed_at,
                started_at=started_at,
            )
            if manifest["state"] != "connected":
                continue
            _assert_output_disk_budget(output_path)
            write_bounded_json(output_path, manifest, limit=MAX_JSON_OUTPUT_BYTES)
            assert_process_memory_budget()
            last_manifest_write = now
            _note_manifest_write(time.monotonic())
            economic_dirty = False
            liveness_dirty = False
    finally:
        if carried_books is not None:
            carried_books.clear()
            carried_books.update(_seed_books(quotes, tokens))
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


async def run_resident_target_ws_publisher(
    *,
    db_path: Path = DEFAULT_TARGET_DB,
    output_path: Path = DEFAULT_TARGET_WS_MANIFEST,
) -> None:
    """Reconnect forever without disturbing the shared WS listener."""

    backoff = 1.0
    # Books outlive the socket that carried them; see _publish_connection.
    carried_books: dict[str, dict[str, Any]] = {}
    while True:
        try:
            as_of_utc = datetime.now(timezone.utc).isoformat().replace(
                "+00:00",
                "Z",
            )
            _PUBLISH_TRACE["connections"] += 1
            catalog = _timed_scope(db_path=db_path, as_of_utc=as_of_utc)
            await _publish_connection(
                catalog,
                db_path=Path(db_path).expanduser().resolve(),
                output_path=Path(output_path).expanduser().resolve(),
                proxy=_configured_proxy(
                    os.environ.get("PM_MARKET_WS_PROXY", "")
                ),
                carried_books=carried_books,
            )
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        # Both teardowns used to `continue` in silence, so the only trace a
        # rebuilt subscription left was a stack of transport EOFs that read
        # like network trouble.  Name the reason: these are the two knobs
        # that decide the book cadence, and neither is diagnosable without
        # knowing which one fired.
        except ConnectionRefreshRequired as exc:
            _log(f"[market_ws][target_exact] planned_refresh:{str(exc)[:120]}")
            backoff = 1.0
            continue
        except ScopeCatalogChanged as exc:
            _log(f"[market_ws][target_exact] catalog_changed:{str(exc)[:120]}")
            backoff = 1.0
            continue
        except Exception as exc:
            _log(
                "[market_ws][target_exact] "
                f"{type(exc).__name__}:{str(exc)[:240]}"
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, RETRY_MAX_SECONDS)


__all__ = [
    "MANIFEST_SCHEMA",
    "MAX_TARGET_STATIONS",
    "MAX_TARGET_SCOPES",
    "MAX_TARGET_TOKENS",
    "MAX_TOKENS_PER_CONNECTION",
    "SCHEMA",
    "ScopeCatalogChanged",
    "_apply_no_live_boundary",
    "_apply_price_changes",
    "_bookless_frame_token",
    "_confirm_shard_stream",
    "_seed_books",
    "_build_multi_scope_manifest",
    "_connection_shards",
    "_future_probability_expired",
    "_merge_book_quote",
    "_quote_economic_state",
    "_subscription_sha256",
    "_receive_connection_shard",
    "_scope",
    "run_resident_target_ws_publisher",
]
