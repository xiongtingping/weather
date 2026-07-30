#!/usr/bin/env python3
"""Polymarket official CLOB WebSocket listener for target quote evidence.

The listener owns only the official CLOB WS connection and the atomic local
manifest/token shards.  It does not refresh markets or archive quote history,
or authorize orders; target SSOT materialization is an explicit downstream
ingress after inventory/order-state/cash-phase evidence is available.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import ssl
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

import certifi
import websockets

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_runtime_env() -> None:
    """Keep direct WS diagnostics identical to the launchd environment."""

    path = Path(os.environ.get("PM_SHARED_POLY_ENV_PATH") or ROOT / ".env")
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not (key.startswith("PM_") or key in {
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
            "GLOBAL_AGENT_HTTP_PROXY",
        }):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


_load_runtime_env()

WS_URL = os.environ.get(
    "PM_MARKET_WS_URL",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
)
WS_PROXY_MODE = str(os.environ.get("PM_MARKET_WS_PROXY", "none") or "none").strip()
HEARTBEAT_PATH = Path(
    os.environ.get("PM_MARKET_WS_HEARTBEAT_PATH", str(ROOT / "data" / "market_ws_heartbeat.json"))
)
LATEST_QUOTES_PATH = Path(
    os.environ.get("PM_MARKET_WS_LATEST_QUOTES_PATH", str(ROOT / "data" / "market_ws_latest_quotes.json"))
)
QUOTE_SHARD_DIR = Path(
    os.environ.get("PM_MARKET_WS_QUOTE_SHARD_DIR", str(ROOT / "data" / "market_ws_quote_shards"))
)
TARGET_ACTIVATION_PATH = (
    ROOT
    / "config"
    / "trading"
    / "bayes_basket_target_production_no_live_activation_v1.json"
)


def _today_et() -> str:
    return datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date().isoformat()


def _registry_db_path() -> Path:
    return Path(os.environ.get("PM_MARKET_WS_DB_PATH", str(ROOT / "data" / "archive.db"))).expanduser().resolve()


@contextmanager
def _open_registry_ro() -> Iterable[sqlite3.Connection]:
    path = _registry_db_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=2000")
        yield conn
    finally:
        conn.close()


def _to_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _best_bid_ask_and_depth(message: Mapping[str, Any], *, levels: int) -> Tuple[
    Optional[float], Optional[float], Optional[float], float, float, Optional[float], Optional[float]
]:
    def parse(side: Any, *, reverse: bool) -> list[tuple[float, float]]:
        rows: list[tuple[float, float]] = []
        for raw in side if isinstance(side, list) else []:
            if not isinstance(raw, Mapping):
                continue
            price, size = _to_float(raw.get("price")), _to_float(raw.get("size"))
            if price is None or size is None or not 0 < price < 1 or size <= 0:
                continue
            rows.append((price, size))
        return sorted(rows, reverse=reverse)[: max(1, int(levels))]

    bids = parse(message.get("bids"), reverse=True)
    asks = parse(message.get("asks"), reverse=False)
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    return (
        best_bid,
        best_ask,
        spread,
        sum(size for _, size in bids),
        sum(size for _, size in asks),
        bids[0][1] if bids else None,
        asks[0][1] if asks else None,
    )


def _target_yes_quotes_enabled() -> bool:
    """Return whether the target no-live lane needs YES book evidence.

    The market WS is still evidence-only and never authorizes an order.  The
    target compiler cannot build a real cycle/leg set from NO-only books, so
    all active YES tokens are subscribed only when the target-owned activation
    proves the complete production-no-live boundary.  Missing, malformed,
    legacy or mutation-authorizing configuration fails closed.
    """

    try:
        value = json.loads(TARGET_ACTIVATION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(value, Mapping):
        return False
    return (
        value.get("schema") == "bayes_basket_target_production_no_live_activation_v1"
        and str(value.get("state") or "").strip() == "production_no_live"
        and str(value.get("execution_mode") or "").strip() == "production_no_live"
        and value.get("target_only") is True
        and value.get("production_mutation") is False
        and value.get("live_decision_write_authorized") is False
        and value.get("live_order_authorized") is False
        and value.get("live_submit_authorized") is False
        and value.get("places_orders") is False
        and value.get("writes_production_db") is False
        and value.get("calls_order_gateway") is False
    )


class LatestQuoteSnapshot:
    """Atomic local materialization of the latest official CLOB WS state."""

    SCHEMA = "clob_ws_latest_quote_snapshot_v1"
    STALE_TEMP_GRACE_SEC = 60.0

    def __init__(
        self,
        path: Path = LATEST_QUOTES_PATH,
        *,
        min_write_interval_sec: Optional[float] = None,
    ) -> None:
        self.path = Path(path)
        if min_write_interval_sec is None:
            min_write_interval_sec = os.environ.get(
                "PM_MARKET_WS_SNAPSHOT_WRITE_INTERVAL_SEC", "0.5"
            )
        self.min_write_interval_sec = max(0.0, float(min_write_interval_sec))
        self.quotes: Dict[str, Dict[str, Any]] = {}
        self.books: Dict[str, Dict[str, Dict[float, float] | str]] = {}
        self.active_shards: set[int] = set()
        # ``shard_tokens`` and the quote/book maps below describe the last
        # committed (servable) generation.  A reconnect is staged separately
        # so a half-materialized replacement cannot hide a still-valid book.
        self.shard_tokens: Dict[int, set[str]] = {}
        self.pending_shard_tokens: Dict[int, set[str]] = {}
        self.pending_quotes: Dict[str, Dict[str, Any]] = {}
        self.pending_books: Dict[str, Dict[str, Dict[float, float] | str]] = {}
        self.active_shard_generations: Dict[int, int] = {}
        self.pending_shard_generations: Dict[int, int] = {}
        self.active_shard_connection_observed_at: Dict[int, str] = {}
        self.pending_shard_connection_observed_at: Dict[int, str] = {}
        # Process-local proof that a shard belongs to this runtime topology.
        # Restored shards deliberately do not populate this set: their
        # committed books may bridge a restart, but they cannot prove that an
        # old shard index still belongs to the newly computed catalog.
        self.runtime_materialized_shards: set[int] = set()
        self._active_materialized_token_count: Optional[int] = None
        self._active_unmaterialized_tokens: set[str] = set()
        try:
            token_write_interval = float(
                os.environ.get("PM_MARKET_WS_TOKEN_WRITE_INTERVAL_SEC", "0.25")
            )
        except (TypeError, ValueError):
            token_write_interval = 0.25
        self._token_write_interval_sec = max(0.0, token_write_interval)
        self._last_token_write_monotonic: Dict[str, float] = {}
        self.expected_shards = 0
        self.connection_observed_at = ""
        self._last_write_monotonic = 0.0
        self.sharded = str(os.environ.get("PM_MARKET_WS_SHARDED_SNAPSHOT", "0")).strip().lower() in {
            "1", "true", "yes", "on", "y",
        }
        self.shard_dir = Path(os.environ.get("PM_MARKET_WS_QUOTE_SHARD_DIR", str(QUOTE_SHARD_DIR)))
        if self.sharded:
            self._cleanup_stale_temp_files()
            # Do not clear the committed files at process start.  The root
            # manifest and token shards form the last known evidence and must
            # bridge a daemon restart until the replacement subscriptions are
            # fully materialized.
            self._restore_committed_snapshot()

    def _cleanup_stale_temp_files(self) -> None:
        """Remove only old atomic-write remnants from exited writer PIDs.

        A live writer's PID is never removed, and the current writer's temp
        file is never touched.  This covers both the root manifest and token
        shards, bounding crash residue without deleting committed evidence or
        interfering with another WS process.
        """

        now = time.time()
        current_pid = os.getpid()
        candidates: set[Path] = set()
        for directory, patterns in (
            (self.shard_dir, (".*.json.*.tmp", ".*.json.tmp.*")),
            (self.path.parent, (f".{self.path.name}.*.tmp", f".{self.path.name}.tmp.*")),
        ):
            if not directory.is_dir():
                continue
            for pattern in patterns:
                try:
                    candidates.update(directory.glob(pattern))
                except OSError:
                    continue
        for path in sorted(candidates):
            match = re.search(r"\.(\d+)\.tmp$|\.tmp\.(\d+)$", path.name)
            if not match:
                continue
            pid = int(match.group(1) or match.group(2))
            if pid == current_pid:
                continue
            try:
                if now - path.stat().st_mtime < self.STALE_TEMP_GRACE_SEC:
                    continue
            except OSError:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
            except PermissionError:
                continue
            except OSError:
                # An indeterminate process probe is fail-closed: retain the
                # remnant and let a later daemon start re-check it.
                continue

    @staticmethod
    def _normalise_numbers(value: Any) -> Any:
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, list):
            return [LatestQuoteSnapshot._normalise_numbers(item) for item in value]
        if isinstance(value, dict):
            return {key: LatestQuoteSnapshot._normalise_numbers(item) for key, item in value.items()}
        return value

    @staticmethod
    def _canonical(value: Mapping[str, Any]) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _restore_committed_snapshot(self) -> None:
        """Rehydrate the last committed shard generation after a restart."""
        try:
            root = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, TypeError):
            return
        if not isinstance(root, Mapping) or root.get("schema") != self.SCHEMA:
            return
        active = {
            int(item) for item in root.get("active_shards") or []
            if str(item).lstrip("-").isdigit()
        }
        self.active_shards = active
        self.expected_shards = max(
            int(root.get("expected_shards") or 0), len(active),
        )
        self.connection_observed_at = str(root.get("connection_observed_at") or "")
        for raw_index, raw_generation in (
            root.get("active_shard_generations") or {}
        ).items():
            try:
                index, generation = int(raw_index), int(raw_generation)
            except (TypeError, ValueError):
                continue
            if index in active:
                self.active_shard_generations[index] = generation
        for raw_index, observed_at in (
            root.get("active_shard_connection_observed_at") or {}
        ).items():
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if index in active and str(observed_at or ""):
                self.active_shard_connection_observed_at[index] = str(observed_at)
        if not self.shard_dir.exists():
            return
        for path in sorted(self.shard_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                supplied = payload.pop("snapshot_sha256")
                if supplied != hashlib.sha256(self._canonical(payload)).hexdigest():
                    continue
                if (
                    payload.get("schema") != "clob_ws_token_quote_snapshot_v1"
                    or payload.get("shard_active") is not True
                    or payload.get("source") != "official_polymarket_clob_websocket"
                ):
                    continue
                token_id = str(payload.get("token_id") or "").strip()
                index = int(payload.get("shard_index"))
                quote = payload.get("quote")
                if not token_id or index not in active or not isinstance(quote, Mapping):
                    continue
            except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError):
                continue
            self.shard_tokens.setdefault(index, set()).add(token_id)
            self.quotes[token_id] = dict(quote)
            self.books[token_id] = {
                "bids": self._levels(quote.get("bids")),
                "asks": self._levels(quote.get("asks")),
                "observed_at": quote.get("book_observed_at") or quote.get("created_at") or "",
                "tick_size": quote.get("tick_size"),
                "depth_levels": max(
                    1,
                    len(quote.get("bids") or []),
                    len(quote.get("asks") or []),
                ),
            }
            if index not in self.active_shard_generations:
                try:
                    self.active_shard_generations[index] = int(payload.get("shard_generation"))
                except (TypeError, ValueError):
                    # Files written before generation fencing are generation 0
                    # and remain valid until the first complete replacement.
                    self.active_shard_generations[index] = 0

    def _token_shard_path(self, token_id: str) -> Path:
        filename = hashlib.sha256(token_id.encode("utf-8")).hexdigest() + ".json"
        return self.shard_dir / filename

    def _pending_shard_for_token(self, token_id: str) -> Optional[int]:
        return next(
            (index for index, tokens in self.pending_shard_tokens.items() if token_id in tokens),
            None,
        )

    def _active_shard_for_token(self, token_id: str) -> Optional[int]:
        return next(
            (index for index, tokens in self.shard_tokens.items() if token_id in tokens),
            None,
        )

    @staticmethod
    def _quote_is_materialized(quote: Any) -> bool:
        return (
            isinstance(quote, Mapping)
            and isinstance(quote.get("bids"), list)
            and isinstance(quote.get("asks"), list)
            and bool(quote.get("book_observed_at"))
        )

    def _write_token_shard(
        self,
        token_id: str,
        *,
        quote: Optional[Mapping[str, Any]] = None,
        shard_index: Optional[int] = None,
        shard_generation: Optional[int] = None,
        shard_active: Optional[bool] = None,
    ) -> None:
        if quote is None:
            quote = self.quotes.get(token_id)
        if not isinstance(quote, Mapping):
            return
        if shard_index is None:
            shard_index = self._active_shard_for_token(token_id)
            if shard_index is None:
                shard_index = self._pending_shard_for_token(token_id)
        if shard_generation is None and shard_index is not None:
            if shard_active is True:
                shard_generation = self.active_shard_generations.get(shard_index)
            else:
                shard_generation = self.pending_shard_generations.get(shard_index)
        if shard_active is None:
            shard_active = (
                shard_index is not None
                and shard_index in self.active_shards
                and token_id in self.shard_tokens.get(shard_index, set())
            )
        core = self._normalise_numbers(
            {
                "schema": "clob_ws_token_quote_snapshot_v1",
                "token_id": token_id,
                "quote": dict(quote),
                "shard_index": shard_index,
                "shard_generation": shard_generation,
                "shard_active": bool(shard_active),
                "source": "official_polymarket_clob_websocket",
                "evidence_only": True,
                "authorizes_order": False,
            }
        )
        payload = {**core, "snapshot_sha256": hashlib.sha256(self._canonical(core)).hexdigest()}
        self.shard_dir.mkdir(parents=True, exist_ok=True)
        target = self._token_shard_path(token_id)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _delete_token_shard(self, token_id: str) -> None:
        if not self.sharded:
            return
        self._token_shard_path(token_id).unlink(missing_ok=True)

    def _token_shard_is_valid(self, token_id: str, shard_index: int, generation: int) -> bool:
        """Verify the committed file, not just the in-memory quote."""
        if not self.sharded:
            return self._quote_is_materialized(self.quotes.get(token_id))
        try:
            payload = json.loads(self._token_shard_path(token_id).read_text(encoding="utf-8"))
            supplied = payload.pop("snapshot_sha256")
            digest = hashlib.sha256(self._canonical(payload)).hexdigest()
        except (OSError, TypeError, ValueError, KeyError):
            return False
        generation_matches = payload.get("shard_generation") == generation or (
            generation == 0 and payload.get("shard_generation") is None
        )
        return (
            supplied == digest
            and payload.get("schema") == "clob_ws_token_quote_snapshot_v1"
            and str(payload.get("token_id")) == token_id
            and payload.get("shard_index") == shard_index
            and generation_matches
            and payload.get("shard_active") is True
            and self._quote_is_materialized(payload.get("quote"))
        )

    def _active_tokens(self) -> set[str]:
        return set().union(*(self.shard_tokens.get(index, set()) for index in self.active_shards)) if self.active_shards else set()

    def _active_token_file_is_valid(self, token_id: str) -> bool:
        index = self._active_shard_for_token(token_id)
        if index is None:
            return False
        generation = self.active_shard_generations.get(index)
        return generation is not None and self._token_shard_is_valid(token_id, index, generation)

    def _refresh_active_materialization_cache(self) -> None:
        """Refresh committed materialization counts at a generation boundary."""

        active_tokens = self._active_tokens()
        self._active_unmaterialized_tokens = {
            token_id
            for token_id in active_tokens
            if not self._quote_is_materialized(self.quotes.get(token_id))
        }
        self._active_materialized_token_count = (
            len(active_tokens) - len(self._active_unmaterialized_tokens)
        )

    def _pending_tokens(self) -> set[str]:
        return set().union(*self.pending_shard_tokens.values()) if self.pending_shard_tokens else set()

    def _clear_pending_shard(self, index: int) -> None:
        for token_id in self.pending_shard_tokens.pop(index, set()):
            self.pending_quotes.pop(token_id, None)
            self.pending_books.pop(token_id, None)
        self.pending_shard_generations.pop(index, None)
        self.pending_shard_connection_observed_at.pop(index, None)

    def _discard_pending_materialization(self, index: int) -> None:
        """Forget provisional books but retain the failed token assignment."""
        for token_id in self.pending_shard_tokens.get(index, set()):
            self.pending_quotes.pop(token_id, None)
            self.pending_books.pop(token_id, None)
        self.pending_shard_connection_observed_at.pop(index, None)

    def _write(self, *, force: bool = False, dirty_tokens: Iterable[str] = ()) -> None:
        now = time.monotonic()
        dirty_token_set = {str(value) for value in dirty_tokens if str(value)}
        # Initial book arrays and quiet-socket liveness updates carry no
        # committed token file changes.  Check the write throttle before
        # rebuilding/sorting the full manifest; otherwise 1200-token shards
        # turn every incoming initial book into synchronous JSON work and can
        # starve the websocket receive loop.
        if (
            not force
            and not dirty_token_set
            and now - self._last_write_monotonic < self.min_write_interval_sec
        ):
            return
        if self.sharded:
            # Provisional quotes deliberately stay in memory.  Writing them
            # under the active token filename was the source of the false
            # quote-missing window during shard reconnects.
            for token_id in dirty_token_set:
                if self._active_shard_for_token(token_id) is not None:
                    if (
                        not force
                        and now - self._last_token_write_monotonic.get(token_id, 0.0)
                        < self._token_write_interval_sec
                    ):
                        continue
                    self._write_token_shard(token_id, shard_active=True)
                    self._last_token_write_monotonic[token_id] = now
                    was_unmaterialized = token_id in self._active_unmaterialized_tokens
                    if self._quote_is_materialized(self.quotes.get(token_id)):
                        self._active_unmaterialized_tokens.discard(token_id)
                        if was_unmaterialized and self._active_materialized_token_count is not None:
                            self._active_materialized_token_count += 1
                    else:
                        self._active_unmaterialized_tokens.add(token_id)
                        if not was_unmaterialized and self._active_materialized_token_count is not None:
                            self._active_materialized_token_count = max(
                                0, self._active_materialized_token_count - 1
                            )
        if not force and now - self._last_write_monotonic < self.min_write_interval_sec:
            return
        active_tokens = self._active_tokens()
        if force or self._active_materialized_token_count is None:
            self._refresh_active_materialization_cache()
        active_materialized = int(self._active_materialized_token_count or 0)
        pending_tokens = self._pending_tokens()
        pending_materialized = sum(
            1 for token_id in pending_tokens if self._quote_is_materialized(self.pending_quotes.get(token_id))
        )
        unmaterialized_tokens = sorted(
            token_id for token_id in pending_tokens
            if not self._quote_is_materialized(self.pending_quotes.get(token_id))
        )
        if self.sharded:
            unmaterialized_tokens.extend(sorted(self._active_unmaterialized_tokens))
        core = {
            "schema": self.SCHEMA,
            "state": "connected" if self.active_shards else "disconnected",
            "connection_observed_at": self.connection_observed_at,
            "active_shards": sorted(self.active_shards),
            "expected_shards": self.expected_shards,
            "quotes_by_token": {} if self.sharded else dict(sorted(self.quotes.items())),
            "quote_storage": "sha256_token_shards_v1" if self.sharded else "inline_v1",
            "quote_shard_dir": str(self.shard_dir) if self.sharded else "",
            "subscribed_token_count": sum(
                len(tokens) for index, tokens in self.shard_tokens.items() if index in self.active_shards
            ),
            "materialized_token_count": active_materialized,
            "provisional_token_count": pending_materialized,
            "pending_shards": sorted(self.pending_shard_tokens),
            "pending_subscribed_token_count": len(pending_tokens),
            "pending_materialized_token_count": pending_materialized,
            "unmaterialized_tokens": sorted(set(unmaterialized_tokens)),
            "active_shard_generations": {
                str(index): generation for index, generation in sorted(self.active_shard_generations.items())
                if index in self.active_shards
            },
            "active_shard_connection_observed_at": {
                str(index): observed_at
                for index, observed_at in sorted(self.active_shard_connection_observed_at.items())
                if index in self.active_shards and observed_at
            },
            "pending_shard_generations": {
                str(index): generation for index, generation in sorted(self.pending_shard_generations.items())
            },
            "source": "official_polymarket_clob_websocket",
            "evidence_only": True,
            "authorizes_order": False,
        }
        core = self._normalise_numbers(core)
        payload = {**core, "snapshot_sha256": hashlib.sha256(self._canonical(core)).hexdigest()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self._last_write_monotonic = now

    def connected(
        self, shard_index: int, asset_ids: Iterable[str] = (), *, expected_shards: int = 0,
    ) -> None:
        """Test/legacy helper for an already materialized shard."""
        index = int(shard_index)
        tokens = {str(token_id) for token_id in asset_ids if str(token_id)}
        self._clear_pending_shard(index)
        old_tokens = self.shard_tokens.get(index, set())
        generation = max(self.active_shard_generations.get(index, 0), self.pending_shard_generations.get(index, 0)) + 1
        self.active_shard_generations[index] = generation
        self.shard_tokens[index] = tokens
        self.active_shards.add(index)
        self.runtime_materialized_shards.add(index)
        self.expected_shards = max(self.expected_shards, int(expected_shards or 0), len(self.active_shards))
        for token_id in old_tokens - tokens:
            self.quotes.pop(token_id, None)
            self.books.pop(token_id, None)
            self._delete_token_shard(token_id)
        self._write(force=True)

    def connecting(
        self, shard_index: int, asset_ids: Iterable[str] = (), *, expected_shards: int = 0,
    ) -> None:
        """Stage a replacement without hiding the last committed generation."""

        index = int(shard_index)
        tokens = {str(token_id) for token_id in asset_ids if str(token_id)}
        self.runtime_materialized_shards.discard(index)
        self._clear_pending_shard(index)
        generation = max(self.active_shard_generations.get(index, 0), self.pending_shard_generations.get(index, 0)) + 1
        self.pending_shard_generations[index] = generation
        self.pending_shard_tokens[index] = tokens
        self.expected_shards = max(self.expected_shards, int(expected_shards or 0), len(self.active_shards))
        self._write(force=True)

    def activate_shard_if_materialized(self, shard_index: int) -> bool:
        """Advertise a shard only after every subscribed token has a complete book."""

        index = int(shard_index)
        tokens = self.pending_shard_tokens.get(index, set())
        if not tokens:
            return False
        ready = all(
            self._quote_is_materialized(self.pending_quotes.get(token_id))
            for token_id in tokens
        )
        if ready:
            generation = self.pending_shard_generations[index]
            # Stage every token file first.  The generation marker keeps both
            # the old root manifest and readers from accepting these files
            # until the new root manifest is atomically replaced.
            for token_id in tokens:
                self._write_token_shard(
                    token_id,
                    quote=self.pending_quotes[token_id],
                    shard_index=index,
                    shard_generation=generation,
                    shard_active=True,
                )
            if not all(self._token_shard_is_valid(token_id, index, generation) for token_id in tokens):
                return False
            old_tokens = self.shard_tokens.get(index, set())
            old_generation = self.active_shard_generations.get(index)
            for token_id in old_tokens - tokens:
                old_quote = self.quotes.get(token_id)
                if isinstance(old_quote, Mapping) and old_generation is not None:
                    # Invalidate the old file before the root switches.  A
                    # reader using the old root must not serve an orphan leg.
                    self._write_token_shard(
                        token_id,
                        quote=old_quote,
                        shard_index=index,
                        shard_generation=old_generation,
                        shard_active=False,
                    )
            for token_id in tokens:
                self.quotes[token_id] = self.pending_quotes[token_id]
                self.books[token_id] = self.pending_books.get(token_id, {})
            self.shard_tokens[index] = set(tokens)
            self.active_shard_generations[index] = generation
            self.active_shards.add(index)
            self.runtime_materialized_shards.add(index)
            pending_observed = self.pending_shard_connection_observed_at.get(index, "")
            if pending_observed:
                self.active_shard_connection_observed_at[index] = pending_observed
            self._clear_pending_shard(index)
            for token_id in old_tokens - tokens:
                self.quotes.pop(token_id, None)
                self.books.pop(token_id, None)
            self._write(force=True)
            for token_id in old_tokens - tokens:
                self._delete_token_shard(token_id)
            self._write(force=True)
        return ready

    def reconcile_runtime_topology(self, expected_shards: int) -> bool:
        """Atomically retire restored shards outside the current topology.

        A previous process may have committed more shards than the catalog
        currently requires.  Those books are intentionally retained while
        replacements materialize, but keeping their indices forever makes the
        root manifest internally inconsistent.  Shrink only after every
        current shard has been fully materialized by this process; restored
        evidence alone is never sufficient authority to delete anything.
        """

        count = int(expected_shards)
        if count <= 0:
            return False
        desired = set(range(count))
        if not desired.issubset(self.runtime_materialized_shards):
            return False
        if desired.intersection(self.pending_shard_tokens):
            return False
        if not desired.issubset(self.active_shards):
            return False
        for index in desired:
            tokens = self.shard_tokens.get(index, set())
            if not tokens or any(
                not self._quote_is_materialized(self.quotes.get(token_id))
                for token_id in tokens
            ):
                return False

        obsolete = set(self.active_shards) - desired
        if not obsolete and self.expected_shards == count:
            return True

        retained_tokens = set().union(
            *(self.shard_tokens.get(index, set()) for index in desired)
        )
        obsolete_tokens = set().union(
            *(self.shard_tokens.get(index, set()) for index in obsolete)
        ) if obsolete else set()

        for index in obsolete:
            self.active_shards.discard(index)
            self.runtime_materialized_shards.discard(index)
            self.shard_tokens.pop(index, None)
            self.active_shard_generations.pop(index, None)
            self.active_shard_connection_observed_at.pop(index, None)
            self._clear_pending_shard(index)

        # Re-assert every retained token file after removing the old ownership
        # maps.  Repartitioning can move a token between shard indices, and an
        # earlier replacement may have invalidated the old file temporarily.
        for index in sorted(desired):
            generation = self.active_shard_generations[index]
            for token_id in sorted(self.shard_tokens[index]):
                quote = self.quotes.get(token_id)
                if isinstance(quote, Mapping):
                    self._write_token_shard(
                        token_id,
                        quote=quote,
                        shard_index=index,
                        shard_generation=generation,
                        shard_active=True,
                    )

        for token_id in obsolete_tokens - retained_tokens:
            self.quotes.pop(token_id, None)
            self.books.pop(token_id, None)
            self._delete_token_shard(token_id)

        self.expected_shards = count
        self._refresh_active_materialization_cache()
        self._write(force=True)
        return True

    def shard_materialization_counts(self, shard_index: int) -> Tuple[int, int]:
        """Return complete initial books and subscribed tokens for one shard."""

        index = int(shard_index)
        pending_tokens = self.pending_shard_tokens.get(index, set())
        tokens = pending_tokens or self.shard_tokens.get(index, set())
        quotes = self.pending_quotes if pending_tokens else self.quotes
        materialized = sum(
            1
            for token_id in tokens
            if self._quote_is_materialized(quotes.get(token_id))
        )
        return materialized, len(tokens)

    @staticmethod
    def _levels(rows: Any) -> Dict[float, float]:
        result: Dict[float, float] = {}
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, Mapping):
                continue
            try:
                price, size = float(raw.get("price")), float(raw.get("size"))
            except (TypeError, ValueError):
                continue
            if 0 < price < 1 and size > 0 and math.isfinite(price) and math.isfinite(size):
                result[price] = size
        return result

    def observe_market_message(
        self, message: Mapping[str, Any], *, observed_at: str, depth_levels: int,
    ) -> None:
        event_type = str(message.get("event_type") or "").strip()
        if event_type == "book":
            token_id = str(message.get("asset_id") or "").strip()
            if token_id:
                books = self.pending_books if self._pending_shard_for_token(token_id) is not None else self.books
                books[token_id] = {
                    "bids": self._levels(message.get("bids")),
                    "asks": self._levels(message.get("asks")),
                    "observed_at": observed_at,
                    "tick_size": _to_float(message.get("tick_size")),
                    "depth_levels": max(1, int(depth_levels)),
                }
            return
        if event_type == "tick_size_change":
            token_id = str(message.get("asset_id") or "").strip()
            tick_size = _to_float(
                message.get("new_tick_size")
                or message.get("tick_size")
            )
            if (
                not token_id
                or tick_size is None
                or not 0.0 < tick_size < 1.0
            ):
                return
            is_pending = (
                self._pending_shard_for_token(token_id) is not None
            )
            books = self.pending_books if is_pending else self.books
            quotes = self.pending_quotes if is_pending else self.quotes
            book = books.get(token_id)
            if isinstance(book, dict):
                book["tick_size"] = tick_size
                book["observed_at"] = observed_at
            quote = quotes.get(token_id)
            if isinstance(quote, dict):
                quote["tick_size"] = tick_size
                if not is_pending:
                    self._write(dirty_tokens=[token_id])
            return
        if event_type != "price_change":
            return
        for raw in message.get("price_changes") or []:
            if not isinstance(raw, Mapping):
                continue
            token_id = str(raw.get("asset_id") or "").strip()
            books = self.pending_books if self._pending_shard_for_token(token_id) is not None else self.books
            book = books.get(token_id)
            side = str(raw.get("side") or "").strip().upper()
            try:
                price, size = float(raw.get("price")), float(raw.get("size"))
            except (TypeError, ValueError):
                continue
            level_key = "bids" if side in {"BUY", "BID"} else "asks" if side in {"SELL", "ASK"} else ""
            if not book or not level_key or not 0 < price < 1 or not math.isfinite(size):
                continue
            levels = book.get(level_key)
            if not isinstance(levels, dict):
                continue
            if size <= 0:
                levels.pop(price, None)
            else:
                levels[price] = size
            book["observed_at"] = observed_at

    def observe(self, rows: Iterable[Mapping[str, Any]], *, observed_at: str) -> None:
        self.observe_with_shard(rows, observed_at=observed_at, shard_index=None)

    def observe_with_shard(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        observed_at: str,
        shard_index: Optional[int],
    ) -> None:
        self.connection_observed_at = str(observed_at or "").strip()
        if shard_index is not None:
            index = int(shard_index)
            if index in self.pending_shard_tokens:
                self.pending_shard_connection_observed_at[index] = self.connection_observed_at
            elif index in self.active_shards:
                self.active_shard_connection_observed_at[index] = self.connection_observed_at
        dirty_tokens: list[str] = []
        for raw in rows:
            token_id = str(raw.get("token_id") or "").strip()
            if not token_id:
                continue
            is_pending = self._pending_shard_for_token(token_id) is not None
            quotes = self.pending_quotes if is_pending else self.quotes
            books = self.pending_books if is_pending else self.books
            next_quote = {
                key: raw.get(key)
                for key in (
                    "token_id", "slug", "event_date", "outcome", "side", "best_bid", "best_ask",
                    "spread", "bid_depth_topN", "ask_depth_topN", "source", "source_detail", "created_at",
                )
            }
            book = books.get(token_id)
            if isinstance(book, Mapping):
                depth = max(1, int(book.get("depth_levels") or 5))
                bids = book.get("bids") if isinstance(book.get("bids"), Mapping) else {}
                asks = book.get("asks") if isinstance(book.get("asks"), Mapping) else {}
                next_quote["bids"] = [
                    {"price": price, "size": bids[price]} for price in sorted(bids, reverse=True)[:depth]
                ]
                next_quote["asks"] = [
                    {"price": price, "size": asks[price]} for price in sorted(asks)[:depth]
                ]
                next_quote["book_observed_at"] = book.get("observed_at")
                if book.get("tick_size") is not None:
                    next_quote["tick_size"] = book.get("tick_size")
            previous = quotes.get(token_id)
            value_keys = (
                "best_bid", "best_ask", "bid_depth_topN",
                "ask_depth_topN", "bids", "asks", "tick_size",
            )
            if isinstance(previous, Mapping) and all(previous.get(key) == next_quote.get(key) for key in value_keys):
                continue
            quotes[token_id] = next_quote
            if not is_pending:
                dirty_tokens.append(token_id)
        self._write(dirty_tokens=dirty_tokens)

    def disconnected(self, shard_index: int, *, preserve_committed: bool = False) -> None:
        index = int(shard_index)
        self.runtime_materialized_shards.discard(index)
        # A failed/reloaded replacement is disposable.  The last committed
        # generation remains servable until it ages out naturally.
        had_pending = index in self.pending_shard_tokens
        if had_pending:
            # Keep the assignment visible as an actual unmaterialized gap so
            # diagnostics cannot confuse a failed initial shard with a clean
            # three-shard snapshot.  The next ``connecting`` call replaces
            # this failed assignment.
            self._discard_pending_materialization(index)
        else:
            self._clear_pending_shard(index)
        if index in self.active_shards and not had_pending and not preserve_committed:
            old_tokens = self.shard_tokens.pop(index, set())
            self.active_shards.discard(index)
            self.active_shard_generations.pop(index, None)
            self.active_shard_connection_observed_at.pop(index, None)
            for token_id in old_tokens:
                self.quotes.pop(token_id, None)
                self.books.pop(token_id, None)
                self._delete_token_shard(token_id)
        elif index not in self.active_shards:
            self.shard_tokens.pop(index, None)
            self.active_shard_generations.pop(index, None)
            self.active_shard_connection_observed_at.pop(index, None)
            for token_id in list(self.quotes):
                if self._active_shard_for_token(token_id) is None:
                    self.quotes.pop(token_id, None)
                    self.books.pop(token_id, None)
                    self._delete_token_shard(token_id)
        self._write(force=True)


LATEST_QUOTE_SNAPSHOT = LatestQuoteSnapshot()


def _utc_now_precise() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    text = str(raw).strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except Exception:
        return float(default)
    return value if math.isfinite(value) else float(default)


def _live_ssot_writer_active() -> bool:
    if not _env_truthy("PM_MARKET_WS_DEFER_DURING_LIVE_SSOT", True):
        return False
    lock_dir = ROOT / "data" / "live_ssot_writer.lock"
    if not lock_dir.is_dir():
        return False
    pid_text = ""
    try:
        pid_text = (lock_dir / "pid").read_text(encoding="utf-8").strip()
        pid = int(pid_text)
    except Exception:
        return True
    if pid <= 1:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def _live_ssot_defer_sleep_sec() -> float:
    return max(0.05, min(1.0, _env_float("PM_MARKET_WS_LIVE_SSOT_DEFER_SLEEP_SEC", 0.2)))


@dataclass(frozen=True)
class TokenBinding:
    token_id: str
    slug: str
    event_date: str
    outcome: str
    side: str


_BINDINGS_CACHE: Dict[Tuple[Any, ...], Tuple[Tuple[Any, ...], Dict[str, TokenBinding]]] = {}
_BINDING_MARKER_CACHE: Dict[Tuple[Any, ...], Tuple[float, Tuple[Any, ...]]] = {}


def _binding_catalog_marker(days_ahead: int, max_assets: int, basket_yes_enabled: bool) -> Tuple[Any, ...] | None:
    """Read a cheap SSOT marker before deciding whether to rebuild bindings."""

    cache_key = (int(days_ahead), int(max_assets), bool(basket_yes_enabled))
    marker_ttl = max(0.0, _env_float("PM_MARKET_WS_CATALOG_MARKER_CACHE_SEC", 1.0))
    now = time.monotonic()
    # Eligibility also changes when a station crosses its local settlement
    # midnight even if the registry rows themselves are unchanged.  Bind the
    # cache marker to the UTC minute so timezone-driven scope changes cannot
    # remain hidden behind an otherwise stable registry MAX(refreshed_at).
    utc_minute_bucket = int(time.time() // 60)
    cached = _BINDING_MARKER_CACHE.get(cache_key)
    if (
        cached is not None
        and now - cached[0] <= marker_ttl
        and cached[1][-1] == utc_minute_bucket
    ):
        return cached[1]

    try:
        with _open_registry_ro() as conn:
            row = conn.execute(
                """SELECT COUNT(*), COALESCE(MAX(refreshed_at),''), COALESCE(MAX(event_date),''),
                          COALESCE(MAX(yes_token),''), COALESCE(MAX(no_token),'')
                   FROM reported_trajectory_market_registry
                   WHERE source_lane IN ('amsc','awc') AND source_eligible=1
                     AND market_active=1 AND market_accepting_orders=1
                     AND COALESCE(yes_token,'')<>'' AND COALESCE(no_token,'')<>''"""
            ).fetchone()
    except Exception:
        # Keep the last known catalog marker during a short SQLite hiccup.  A
        # later probe will retry; tearing down every healthy WS shard for a
        # transient read error is slower and does not improve quote safety.
        if cached is not None and now - cached[0] <= max(marker_ttl, 1.0):
            return cached[1]
        return None
    if row is None:
        return None
    marker = (
        int(days_ahead), int(max_assets), bool(basket_yes_enabled), int(row[0] or 0),
        str(row[1] or ""), str(row[2] or ""), str(row[3] or ""), str(row[4] or ""),
        utc_minute_bucket,
    )
    _BINDING_MARKER_CACHE[cache_key] = (now, marker)
    return marker


class TickCoalescer:
    """Drop repeated identical quote rows while periodically refreshing freshness."""

    def __init__(self, same_quote_sec: float, same_quote_tolerance: float = 0.0) -> None:
        try:
            value = float(same_quote_sec)
        except Exception:
            value = 0.0
        self.same_quote_sec = max(0.0, value)
        try:
            tolerance = float(same_quote_tolerance)
        except Exception:
            tolerance = 0.0
        self.same_quote_tolerance = max(0.0, tolerance)
        self.skipped = 0
        self._last: Dict[
            Tuple[str, str, str, str],
            Tuple[Tuple[Optional[float], Optional[float], Optional[float], float, float], float],
        ] = {}

    @staticmethod
    def _key(row: Mapping[str, Any]) -> Tuple[str, str, str, str]:
        return (
            str(row.get("slug") or ""),
            str(row.get("event_date") or ""),
            str(row.get("outcome") or ""),
            str(row.get("side") or ""),
        )

    @staticmethod
    def _signature(row: Mapping[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float], float, float]:
        return (
            _to_float(row.get("best_bid")),
            _to_float(row.get("best_ask")),
            _to_float(row.get("spread")),
            float(row.get("bid_depth_topN") or 0.0),
            float(row.get("ask_depth_topN") or 0.0),
        )

    @staticmethod
    def _sig_almost_equal(
        prev: Tuple[Optional[float], Optional[float], Optional[float], float, float],
        curr: Tuple[Optional[float], Optional[float], Optional[float], float, float],
        *,
        tolerance: float,
    ) -> bool:
        prev_bid, prev_ask, prev_spread, prev_bid_depth, prev_ask_depth = prev
        curr_bid, curr_ask, curr_spread, curr_bid_depth, curr_ask_depth = curr
        for p_v, c_v in (
            (prev_bid, curr_bid),
            (prev_ask, curr_ask),
            (prev_spread, curr_spread),
            (prev_bid_depth, curr_bid_depth),
            (prev_ask_depth, curr_ask_depth),
        ):
            if p_v is None and c_v is None:
                continue
            if p_v is None or c_v is None:
                return False
            if abs(float(p_v) - float(c_v)) > float(tolerance):
                return False
        return True

    def filter_rows(self, rows: Iterable[Dict[str, Any]], *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        if self.same_quote_sec <= 0:
            return list(rows)
        current = time.monotonic() if now is None else float(now)
        out: List[Dict[str, Any]] = []
        for row in rows:
            key = self._key(row)
            sig = self._signature(row)
            previous = self._last.get(key)
            if (
                previous is not None
                and current - previous[1] < self.same_quote_sec
                and self._sig_almost_equal(previous[0], sig, tolerance=self.same_quote_tolerance)
            ):
                self.skipped += 1
                continue
            self._last[key] = (sig, current)
            out.append(row)
        return out


def _ws_proxy_candidates(mode_raw: str) -> List[Tuple[str, Any]]:
    def _explicit_http_proxy() -> Optional[str]:
        for key in ("PM_MARKET_WS_HTTP_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            raw = str(os.environ.get(key) or "").strip()
            if not raw:
                continue
            low = raw.lower()
            if low.startswith("http://") or low.startswith("https://"):
                return raw
        return None

    explicit_http_proxy = _explicit_http_proxy()
    mode = str(mode_raw or "").strip().lower()
    if mode in {"", "none", "off", "false", "0", "direct"}:
        return [("direct", None)]
    if mode in {"direct_then_env", "direct+env", "fallback"}:
        out: List[Tuple[str, Any]] = [("direct", None)]
        if explicit_http_proxy:
            out.append(("http_proxy_env", explicit_http_proxy))
        return out
    if mode in {"env", "http_proxy_env"}:
        if not explicit_http_proxy:
            raise ValueError("PM_MARKET_WS_PROXY=env requires an HTTP proxy environment variable")
        return [("http_proxy_env", explicit_http_proxy)]
    if mode in {"auto", "system"}:
        return [("auto", "__auto__")]
    # Explicit proxy URL (e.g. http://127.0.0.1:7897)
    if mode.startswith("http://") or mode.startswith("https://"):
        return [("custom", mode_raw)]
    raise ValueError(f"unsupported PM_MARKET_WS_PROXY mode: {mode_raw}")


def _normalize_csv_set(value: Any, *, fallback: Iterable[str]) -> set:
    if not isinstance(value, str):
        value = str(value or "").strip()
    if not value:
        return {v.strip().lower() for v in fallback if str(v).strip()}
    out = {v.strip().lower() for v in value.split(",")}
    return {v for v in out if v}


def _load_token_bindings(days_ahead: int, max_assets: int) -> Dict[str, TokenBinding]:
    # The market listener is allowed to prewarm a bounded future window so an
    # explicitly authorized next-day basket can pass the local WS evidence
    # gate.  Keep the window bounded and date-based; no secondary quote source.
    try:
        lookahead_days = max(0, min(int(days_ahead), 7))
    except (TypeError, ValueError):
        lookahead_days = 0
    today_utc = datetime.now(timezone.utc).date().isoformat()
    sql = """
        SELECT
            slug,event_date,outcome,yes_token,no_token,settlement_timezone
        FROM reported_trajectory_market_registry
        WHERE source_lane IN ('amsc','awc')
          AND source_eligible=1
          AND market_active=1
          AND market_accepting_orders=1
          AND COALESCE(yes_token,'')<>''
          AND COALESCE(no_token,'')<>''
          AND event_date >= date(?, '-1 day')
          AND event_date <= date(?, '+LOOKAHEAD day')
        ORDER BY event_date,slug,outcome
    """
    sql = sql.replace("+LOOKAHEAD day", f"+{lookahead_days} day")
    bindings: Dict[str, TokenBinding] = {}
    basket_yes_enabled = _target_yes_quotes_enabled()
    cache_key = (lookahead_days, int(max_assets), bool(basket_yes_enabled))
    marker = _binding_catalog_marker(lookahead_days, int(max_assets), basket_yes_enabled)
    cached = _BINDINGS_CACHE.get(cache_key)
    if marker is not None and cached is not None and cached[0] == marker:
        return dict(cached[1])
    try:
        with _open_registry_ro() as conn:
            rows = conn.execute(
                sql,
                (today_utc, today_utc),
            ).fetchall()
    except Exception:
        rows = []
    for row in rows:
        slug = str(row[0] or "").strip()
        event_date = str(row[1] or "").strip().split("T", 1)[0]
        outcome = str(row[2] or "").strip()
        timezone_name = str(row[5] or "").strip()
        try:
            local_event_date = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
        except Exception:
            continue
        try:
            event_date_value = datetime.fromisoformat(event_date).date()
        except ValueError:
            continue
        if (
            not slug
            or not event_date
            or not outcome
            or event_date_value < local_event_date
            or event_date_value > local_event_date + timedelta(days=lookahead_days)
        ):
            continue
        token_rows = [("NO", row[4])]
        if basket_yes_enabled:
            token_rows.append(("YES", row[3]))
        for side, token_raw in token_rows:
            token_id = str(token_raw or "").strip()
            if not token_id or token_id in bindings:
                continue
            bindings[token_id] = TokenBinding(
                token_id=token_id,
                slug=slug,
                event_date=event_date,
                outcome=outcome,
                side=side,
            )
            if max_assets > 0 and len(bindings) >= max_assets:
                if marker is not None:
                    _BINDINGS_CACHE[cache_key] = (marker, dict(bindings))
                return dict(bindings)
    if marker is not None:
        _BINDINGS_CACHE[cache_key] = (marker, dict(bindings))
    return bindings


def _quote_row(
    binding: TokenBinding,
    *,
    best_bid: Optional[float],
    best_ask: Optional[float],
    spread: Optional[float],
    bid_depth: float,
    ask_depth: float,
    created_at: str,
    source_detail: str,
) -> Dict[str, Any]:
    mid = None
    if best_bid is not None and best_ask is not None:
        mid = (float(best_bid) + float(best_ask)) / 2.0
    if spread is None and best_bid is not None and best_ask is not None:
        spread = max(float(best_ask) - float(best_bid), 0.0)
    return {
        "token_id": binding.token_id,
        "run_date": _today_et(),
        "event_date": binding.event_date,
        "slug": binding.slug,
        "outcome": binding.outcome,
        "side": binding.side,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "bid_depth_topN": float(bid_depth or 0.0),
        "ask_depth_topN": float(ask_depth or 0.0),
        "source": "clob_ws",
        "source_detail": source_detail,
        "created_at": created_at,
    }


def rows_from_ws_message(
    message: Mapping[str, Any],
    bindings: Mapping[str, TokenBinding],
    *,
    depth_levels: int,
    include_source_details: set,
    created_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    event_type = str(message.get("event_type") or "").strip()
    created = created_at or _utc_now_precise()
    out: List[Dict[str, Any]] = []
    if event_type == "book":
        if "book" not in include_source_details:
            return []
        token_id = str(message.get("asset_id") or "").strip()
        binding = bindings.get(token_id)
        if not binding:
            return []
        best_bid, best_ask, spread, bid_depth, ask_depth, _bid_size, _ask_size = _best_bid_ask_and_depth(
            dict(message),
            levels=int(depth_levels),
        )
        out.append(
            _quote_row(
                binding,
                best_bid=best_bid,
                best_ask=best_ask,
                spread=spread,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                created_at=created,
                source_detail="book",
            )
        )
        return out
    if event_type == "price_change":
        if "price_change" not in include_source_details:
            return []
        changes = message.get("price_changes") or []
        if not isinstance(changes, list):
            return []
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            token_id = str(change.get("asset_id") or "").strip()
            binding = bindings.get(token_id)
            if not binding:
                continue
            best_bid = _to_float(change.get("best_bid"))
            best_ask = _to_float(change.get("best_ask"))
            out.append(
                _quote_row(
                    binding,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    spread=None,
                    bid_depth=0.0,
                    ask_depth=0.0,
                    created_at=created,
                    source_detail="price_change",
                )
            )
        return out
    if event_type == "best_bid_ask":
        if "best_bid_ask" not in include_source_details:
            return []
        token_id = str(message.get("asset_id") or "").strip()
        binding = bindings.get(token_id)
        if not binding:
            return []
        out.append(
            _quote_row(
                binding,
                best_bid=_to_float(message.get("best_bid")),
                best_ask=_to_float(message.get("best_ask")),
                spread=_to_float(message.get("spread")),
                bid_depth=0.0,
                ask_depth=0.0,
                created_at=created,
                source_detail="best_bid_ask",
            )
        )
    return out


def _iter_payload_messages(raw: str) -> Iterable[Mapping[str, Any]]:
    if not raw or raw in {"PONG", "PING"}:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    def normalise(item: Any) -> Optional[Mapping[str, Any]]:
        if not isinstance(item, Mapping):
            return None
        # Polymarket sends the initial subscription snapshot as a JSON array
        # of book objects.  Unlike later price_change messages, those objects
        # do not carry event_type.  Preserve the payload and add only the
        # protocol discriminator so the initial book is materialized into the
        # same official WS snapshot path.
        if (
            not str(item.get("event_type") or "").strip()
            and "asset_id" in item
            and "bids" in item
            and "asks" in item
        ):
            value = dict(item)
            value["event_type"] = "book"
            return value
        return item

    if isinstance(payload, list):
        return [item for raw_item in payload if (item := normalise(raw_item)) is not None]
    if isinstance(payload, Mapping):
        item = normalise(payload)
        return [item] if item is not None else []
    return []


def _write_heartbeat(payload: Dict[str, Any]) -> None:
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = HEARTBEAT_PATH.with_suffix(HEARTBEAT_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    tmp.replace(HEARTBEAT_PATH)


async def _ping_loop(ws: Any, interval_sec: float, shard_index: Optional[int] = None) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        try:
            await ws.send("PING")
            if shard_index is not None:
                # A quiet book is still valid while the official socket is
                # alive.  Persist liveness separately from quote timestamps;
                # once send fails, this timestamp stops and the consumer's
                # normal freshness limit closes the evidence window.
                LATEST_QUOTE_SNAPSHOT.observe_with_shard(
                    [], observed_at=_utc_now_precise(), shard_index=shard_index,
                )
        except Exception:
            return


def _aggregate_shard_results(
    results: Iterable[Any],
) -> Tuple[int, int, int, List[str]]:
    total_materialized = 0
    total_received = 0
    total_skipped = 0
    errors: List[str] = []
    for shard_index, result in enumerate(results):
        # asyncio.CancelledError derives from BaseException on supported
        # Python versions.  Never send exception objects through the
        # successful tuple path.
        if isinstance(result, BaseException):
            errors.append(
                f"shard={shard_index} {result.__class__.__name__}: {str(result)[:240]}"
            )
            continue
        materialized, received, skipped = result
        total_materialized += int(materialized or 0)
        total_received += int(received or 0)
        total_skipped += int(skipped or 0)
    return total_materialized, total_received, total_skipped, errors


class SubscriptionCatalogChanged(RuntimeError):
    """The subscribed token set changed and this shard needs a rolling refresh."""


class ShardTopologyChanged(RuntimeError):
    """The number of required shards changed and the supervisor must reconfigure."""


class InitialShardMaterializationTimeout(RuntimeError):
    """A connected shard did not deliver every subscribed initial order book."""


def _release_guard_delay_sec(now_epoch: Optional[float] = None) -> float:
    """Delay planned refresh work beyond UTC :00/:30 report release windows."""
    epoch = time.time() if now_epoch is None else float(now_epoch)
    before = max(0.0, _env_float("PM_MARKET_WS_RELEASE_GUARD_BEFORE_SEC", 30.0))
    after = max(0.0, _env_float("PM_MARKET_WS_RELEASE_GUARD_AFTER_SEC", 180.0))
    phase = epoch % 1800.0
    if phase <= after:
        return after - phase
    if phase >= 1800.0 - before:
        return (1800.0 - phase) + after
    return 0.0


def _load_current_shard_catalog(
    args: argparse.Namespace, shard_index: int, shard_count: int,
) -> Tuple[List[str], Dict[str, TokenBinding]]:
    bindings = _load_token_bindings(args.days_ahead, args.max_assets)
    asset_ids = sorted(bindings.keys())
    shard_size = max(1, int(getattr(args, "shard_size", 400) or 400))
    chunks = [asset_ids[i : i + shard_size] for i in range(0, len(asset_ids), shard_size)]
    if len(chunks) != int(shard_count) or not 0 <= int(shard_index) < len(chunks):
        raise ShardTopologyChanged(
            f"expected_shards={shard_count} current_shards={len(chunks)} shard={shard_index}"
        )
    return chunks[int(shard_index)], bindings


def _subscription_catalog_unchanged(
    args: argparse.Namespace,
    asset_ids: List[str],
    *,
    shard_index: int,
    shard_count: int,
) -> bool:
    current_assets, _bindings = _load_current_shard_catalog(args, shard_index, shard_count)
    return current_assets == asset_ids


async def listen_once(
    args: argparse.Namespace,
    coalescer: Optional[TickCoalescer] = None,
) -> int:
    bindings = _load_token_bindings(args.days_ahead, args.max_assets)
    asset_ids = sorted(bindings.keys())
    if not asset_ids:
        _write_heartbeat(
            {
                "ok": False,
                "status": "no_assets",
                "updated_at": _utc_now_precise(),
                "assets": 0,
            }
        )
        await asyncio.sleep(args.reload_sec)
        return 0

    if coalescer is None:
        coalescer = TickCoalescer(
            float(args.coalesce_same_quote_sec),
            same_quote_tolerance=float(args.coalesce_same_quote_tolerance),
        )

    shard_size = max(1, int(getattr(args, "shard_size", 400) or 400))
    chunks = [asset_ids[i : i + shard_size] for i in range(0, len(asset_ids), shard_size)]
    if len(chunks) > 1:
        _write_heartbeat(
            {
                "ok": True,
                "status": "connecting",
                "updated_at": _utc_now_precise(),
                "assets": len(asset_ids),
                "shards": len(chunks),
                "shard_size": shard_size,
            }
        )
        tasks = [
            asyncio.create_task(
                _listen_asset_batch_resilient(
                    args,
                    chunk,
                    bindings,
                    total_assets=len(asset_ids),
                    shard_index=idx,
                    shard_count=len(chunks),
                    coalescer=coalescer,
                )
            )
            for idx, chunk in enumerate(chunks)
        ]
        # A transport failure belongs to one shard.  Each shard reconnects
        # independently while the healthy sibling remains subscribed.  The
        # manifest still requires every shard, so execution stays fail-closed
        # until the failed shard has rematerialized its quotes.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        total_materialized, total_received, total_skipped, errors = _aggregate_shard_results(results)
        _write_heartbeat(
            {
                "ok": not errors,
                "status": "connected" if not errors else "error",
                "updated_at": _utc_now_precise(),
                "assets": len(asset_ids),
                "shards": len(chunks),
                "shard_size": shard_size,
                "quotes_materialized": total_materialized,
                "quote_messages_received": total_received,
                "quotes_skipped_same": total_skipped,
                "coalesce_same_quote_sec": float(args.coalesce_same_quote_sec),
                "errors": errors[:3],
            }
        )
        if errors:
            raise RuntimeError("; ".join(errors[:2]))
        return total_materialized

    materialized, _received, _skipped = await _listen_asset_batch_resilient(
        args,
        asset_ids,
        bindings,
        total_assets=len(asset_ids),
        shard_index=0,
        shard_count=1,
        coalescer=coalescer,
    )
    return materialized


class ShardReconnectError(RuntimeError):
    """A shard exhausted its reconnect window while preserving root cause."""


async def _listen_asset_batch_resilient(
    args: argparse.Namespace,
    asset_ids: List[str],
    bindings: Mapping[str, TokenBinding],
    *,
    coalescer: Optional[TickCoalescer] = None,
    total_assets: int,
    shard_index: int,
    shard_count: int,
) -> Tuple[int, int, int]:
    retry_budget = max(0.0, _env_float("PM_MARKET_WS_SHARD_RETRY_BUDGET_SEC", 0.0))
    retry_deadline = time.monotonic() + retry_budget if retry_budget > 0 else None
    base_backoff = max(0.01, _env_float("PM_MARKET_WS_SHARD_RETRY_BASE_SEC", 0.25))
    max_backoff = max(base_backoff, _env_float("PM_MARKET_WS_SHARD_RETRY_MAX_SEC", 5.0))
    backoff = base_backoff
    first_error = ""
    last_error = ""
    attempts = 0
    while True:
        if retry_deadline is not None and time.monotonic() >= retry_deadline:
            break
        attempts += 1
        attempt_args = argparse.Namespace(**vars(args))
        try:
            return await _listen_asset_batch(
                attempt_args,
                asset_ids,
                bindings,
                coalescer=coalescer,
                total_assets=total_assets,
                shard_index=shard_index,
                shard_count=shard_count,
            )
        except asyncio.CancelledError:
            raise
        except SubscriptionCatalogChanged:
            asset_ids, bindings = _load_current_shard_catalog(args, shard_index, shard_count)
            backoff = base_backoff
            continue
        except ShardTopologyChanged:
            raise
        except Exception as exc:
            current_error = f"{exc.__class__.__name__}: {str(exc)[:240]}"
            if not first_error:
                first_error = current_error
            last_error = current_error
            _write_heartbeat(
                {
                    "ok": False,
                    "status": "shard_reconnecting",
                    "updated_at": _utc_now_precise(),
                    "assets": int(total_assets),
                    "failed_shard": int(shard_index),
                    "shards": int(shard_count),
                    "reconnect_attempt": attempts,
                    "first_error": first_error,
                    "last_error": last_error,
                    "ws_proxy_mode": WS_PROXY_MODE,
                }
            )
            if retry_deadline is not None and time.monotonic() >= retry_deadline:
                break
            sleep_for = backoff
            if retry_deadline is not None:
                sleep_for = min(sleep_for, max(0.0, retry_deadline - time.monotonic()))
            await asyncio.sleep(sleep_for)
            backoff = min(backoff * 1.7, max_backoff)
    raise ShardReconnectError(
        f"shard={shard_index} attempts={attempts} "
        f"first_error={first_error or 'reconnect_window_expired'} "
        f"last_error={last_error or first_error or 'reconnect_window_expired'}"
    )


async def _listen_asset_batch(
    args: argparse.Namespace,
    asset_ids: List[str],
    bindings: Mapping[str, TokenBinding],
    *,
    coalescer: Optional[TickCoalescer] = None,
    total_assets: int,
    shard_index: int,
    shard_count: int,
) -> Tuple[int, int, int]:
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    materialized = 0
    received = 0
    last_flush = time.monotonic()
    last_hb = 0.0
    if coalescer is None:
        coalescer = TickCoalescer(
            float(args.coalesce_same_quote_sec),
            same_quote_tolerance=float(args.coalesce_same_quote_tolerance),
        )
    subscription = {
        "assets_ids": asset_ids,
        "type": "market",
        "custom_feature_enabled": True,
    }
    base_connect_kwargs: Dict[str, Any] = {
        "ssl": ssl_context,
        "ping_interval": 20,
        "ping_timeout": 20,
        "open_timeout": 20,
        "close_timeout": 5,
        "max_size": 8 * 1024 * 1024,
    }
    proxy_candidates = _ws_proxy_candidates(WS_PROXY_MODE)
    if shard_index > 0:
        await asyncio.sleep(min(2.0, shard_index * 0.2))
    connect_errors: List[str] = []
    for proxy_label, proxy_value in proxy_candidates:
        connect_kwargs = dict(base_connect_kwargs)
        if proxy_value != "__auto__":
            connect_kwargs["proxy"] = proxy_value
        try:
            async with websockets.connect(WS_URL, **connect_kwargs) as ws:
                await ws.send(json.dumps(subscription))
                LATEST_QUOTE_SNAPSHOT.connecting(
                    shard_index, asset_ids, expected_shards=shard_count,
                )
                shard_ready = False
                materialization_timeout = max(
                    3.0,
                    _env_float("PM_MARKET_WS_INITIAL_MATERIALIZATION_TIMEOUT_SEC", 20.0),
                )
                materialization_deadline = time.monotonic() + materialization_timeout
                catalog_probe_sec = max(
                    1.0,
                    _env_float("PM_MARKET_WS_CATALOG_PROBE_SEC", 1.0),
                )
                ping_task = asyncio.create_task(
                    _ping_loop(ws, args.text_ping_sec, shard_index=shard_index)
                )
                planned_reload = False
                try:
                    stagger = max(0.0, _env_float("PM_MARKET_WS_SHARD_REFRESH_STAGGER_SEC", 5.0))
                    deadline = time.monotonic() + args.reload_sec + (shard_index * stagger)
                    next_catalog_probe = time.monotonic() + catalog_probe_sec
                    while True:
                        timeout = max(0.5, min(args.flush_sec, deadline - time.monotonic()))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            raw = ""
                        if raw:
                            inbound_at = _utc_now_precise()
                            LATEST_QUOTE_SNAPSHOT.observe_with_shard(
                                [], observed_at=inbound_at, shard_index=shard_index,
                            )
                            for message in _iter_payload_messages(str(raw)):
                                observed_at = inbound_at
                                LATEST_QUOTE_SNAPSHOT.observe_market_message(
                                    message, observed_at=observed_at, depth_levels=args.depth_levels,
                                )
                                rows = rows_from_ws_message(
                                    message,
                                    bindings,
                                    depth_levels=args.depth_levels,
                                    include_source_details=args.include_source_details,
                                    created_at=observed_at,
                                )
                                LATEST_QUOTE_SNAPSHOT.observe_with_shard(
                                    rows, observed_at=observed_at, shard_index=shard_index,
                                )
                                if not shard_ready:
                                    shard_ready = LATEST_QUOTE_SNAPSHOT.activate_shard_if_materialized(
                                        shard_index
                                    )
                                    if shard_ready:
                                        LATEST_QUOTE_SNAPSHOT.reconcile_runtime_topology(
                                            shard_count
                                        )
                                received += len(rows)
                        now = time.monotonic()
                        materialized_count, shard_token_count = (
                            LATEST_QUOTE_SNAPSHOT.shard_materialization_counts(shard_index)
                        )
                        if not shard_ready and now >= materialization_deadline:
                            # A stale subscription catalog can make a shard
                            # wait forever for tokens that have already
                            # disappeared from the authoritative registry.
                            # Preserve the all-initial-books safety contract,
                            # but give the resilient wrapper a chance to
                            # reload the current catalog before reconnecting.
                            try:
                                catalog_unchanged = _subscription_catalog_unchanged(
                                    args,
                                    asset_ids,
                                    shard_index=shard_index,
                                    shard_count=shard_count,
                                )
                            except ShardTopologyChanged:
                                raise
                            except Exception:
                                catalog_unchanged = True
                            if not catalog_unchanged:
                                raise SubscriptionCatalogChanged(
                                    f"shard={shard_index} catalog_changed_during_materialization"
                                )
                            raise InitialShardMaterializationTimeout(
                                f"shard={shard_index} materialized={materialized_count}/"
                                f"{shard_token_count} timeout_sec={materialization_timeout:g}"
                            )
                        # Copy-follow plans may be discovered after this
                        # socket was opened.  Probe the authoritative local
                        # catalog frequently enough to move those four legs
                        # into the priority portion of the bounded
                        # subscription; do not wait for the long normal
                        # reload interval.
                        if now >= next_catalog_probe:
                            try:
                                unchanged = _subscription_catalog_unchanged(
                                    args,
                                    asset_ids,
                                    shard_index=shard_index,
                                    shard_count=shard_count,
                                )
                            except ShardTopologyChanged:
                                raise
                            except Exception:
                                # A transient read failure must not tear down
                                # a healthy socket; the next probe retries.
                                unchanged = True
                            next_catalog_probe = now + catalog_probe_sec
                            if not unchanged:
                                planned_reload = True
                                break
                        if now - last_hb >= args.heartbeat_sec:
                            any_ready = bool(LATEST_QUOTE_SNAPSHOT.active_shards)
                            _write_heartbeat(
                                {
                                    "ok": any_ready,
                                    "status": "connected" if any_ready else "materializing",
                                    "updated_at": _utc_now_precise(),
                                    "assets": int(total_assets),
                                    "shard_assets": len(asset_ids),
                                    "shard_index": int(shard_index),
                                    "shards": int(shard_count),
                                    "shard_ready": bool(shard_ready),
                                    "shard_materialized_tokens": int(materialized_count),
                                    "shard_subscribed_tokens": int(shard_token_count),
                                    "active_shards": sorted(LATEST_QUOTE_SNAPSHOT.active_shards),
                                    "quotes_materialized": materialized,
                                    "quote_messages_received": received,
                                    "quotes_skipped_same": coalescer.skipped,
                                    "coalesce_same_quote_sec": float(coalescer.same_quote_sec),
                                    "ws_proxy_mode": WS_PROXY_MODE,
                                    "ws_proxy_active": proxy_label,
                                }
                            )
                            last_hb = now
                        if now >= deadline:
                            if bool(getattr(args, "once", False)):
                                break
                            guard_delay = _release_guard_delay_sec()
                            if guard_delay > 0:
                                deadline = now + guard_delay + (shard_index * stagger)
                                continue
                            try:
                                unchanged = _subscription_catalog_unchanged(
                                    args,
                                    asset_ids,
                                    shard_index=shard_index,
                                    shard_count=shard_count,
                                )
                            except ShardTopologyChanged:
                                raise
                            except Exception as exc:
                                # A catalog read failure must not tear down a
                                # healthy market socket. Retry the read later.
                                print(
                                    f"[market_ws] catalog_check_deferred shard={shard_index} "
                                    f"{exc.__class__.__name__}: {str(exc)[:160]}",
                                    flush=True,
                                )
                                deadline = now + min(max(5.0, args.reload_sec), 30.0)
                                continue
                            if unchanged:
                                deadline = now + args.reload_sec
                                continue
                            planned_reload = True
                            break
                finally:
                    # Keep the last committed generation readable across the
                    # reconnect boundary.  Its per-shard timestamp still
                    # enforces the consumer freshness limit.
                    LATEST_QUOTE_SNAPSHOT.disconnected(
                        shard_index, preserve_committed=True,
                    )
                    ping_task.cancel()
            if planned_reload:
                raise SubscriptionCatalogChanged(f"shard={shard_index} token catalog changed")
            return materialized, received, coalescer.skipped
        except (SubscriptionCatalogChanged, ShardTopologyChanged):
            raise
        except Exception as exc:
            connect_errors.append(f"{proxy_label}:{exc.__class__.__name__}:{str(exc)[:140]}")
            continue
    raise RuntimeError("; ".join(connect_errors[:3]) or "ws_connect_failed")


async def run_forever(args: argparse.Namespace) -> None:
    backoff = 1.0
    coalescer = TickCoalescer(
        float(args.coalesce_same_quote_sec),
        same_quote_tolerance=float(args.coalesce_same_quote_tolerance),
    )
    target_multi_scope_task: asyncio.Task[Any] | None = None
    target_multi_scope_enabled = str(
        os.environ.get("PM_TARGET_WS_VERTICAL_SLICE_ENABLED", "1")
    ).strip().lower() in {"1", "true", "yes", "on"}
    if target_multi_scope_enabled:
        from scripts.resident_target_ws_publisher import (
            run_resident_target_ws_publisher,
        )
        # One child task owns one official socket whose bounded token catalog
        # contains every eligible (station,event_date) scope.  Do not create
        # one task/socket per date and do not rotate dates: both approaches
        # can make the current P1 scope disappear while the next P0/P0-M
        # scope is being refreshed.
        target_multi_scope_task = asyncio.create_task(
            run_resident_target_ws_publisher(),
            name="target-resident-multi-scope-official-ws",
        )
    try:
        while True:
            try:
                count = await listen_once(args, coalescer=coalescer)
                print(f"[market_ws] cycle_ok materialized_quotes={count}", flush=True)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _write_heartbeat(
                    {
                        "ok": False,
                        "status": "error",
                        "error": f"{exc.__class__.__name__}: {str(exc)[:160]}",
                        "updated_at": _utc_now_precise(),
                    }
                )
                print(f"[market_ws] error {exc.__class__.__name__}: {exc}", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.7, 30.0)
    finally:
        if target_multi_scope_task is not None:
            target_multi_scope_task.cancel()
            await asyncio.gather(
                target_multi_scope_task,
                return_exceptions=True,
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize official CLOB WS quote snapshots; never write historical quote rows.")
    parser.add_argument("--days-ahead", type=int, default=int(os.environ.get("PM_MARKET_WS_DAYS_AHEAD", "3")))
    parser.add_argument("--max-assets", type=int, default=int(os.environ.get("PM_MARKET_WS_MAX_ASSETS", "2000")))
    parser.add_argument("--shard-size", type=int, default=int(os.environ.get("PM_MARKET_WS_SHARD_SIZE", "400")))
    parser.add_argument("--depth-levels", type=int, default=int(os.environ.get("PM_MARKET_WS_DEPTH_LEVELS", "5")))
    parser.add_argument("--reload-sec", type=float, default=float(os.environ.get("PM_MARKET_WS_RELOAD_SEC", "60")))
    parser.add_argument("--flush-sec", type=float, default=float(os.environ.get("PM_MARKET_WS_FLUSH_SEC", "1.0")))
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("PM_MARKET_WS_BATCH_SIZE", "100")))
    parser.add_argument("--heartbeat-sec", type=float, default=float(os.environ.get("PM_MARKET_WS_HEARTBEAT_SEC", "5")))
    parser.add_argument("--text-ping-sec", type=float, default=float(os.environ.get("PM_MARKET_WS_TEXT_PING_SEC", "0.5")))
    parser.add_argument(
        "--coalesce-same-quote-sec",
        type=float,
        default=float(os.environ.get("PM_MARKET_WS_COALESCE_SAME_QUOTE_SEC", "10")),
        help="Skip unchanged quote rows within this many seconds; 0 disables coalescing.",
    )
    parser.add_argument(
        "--coalesce-same-quote-tol",
        type=float,
        default=float(os.environ.get("PM_MARKET_WS_COALESCE_SAME_QUOTE_TOL", "0.0")),
        help="Quote/Depth tolerance for coalescing unchanged rows; default 0 keeps exact-match behavior.",
    )
    parser.add_argument(
        "--include-source-details",
        type=str,
        default=str(os.environ.get("PM_MARKET_WS_INCLUDE_SOURCE_DETAILS", "price_change,book")),
        help="Comma-separated websocket event types to store: price_change,book,best_bid_ask",
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    args.coalesce_same_quote_tolerance = float(args.coalesce_same_quote_tol)
    args.include_source_details = _normalize_csv_set(
        args.include_source_details,
        fallback=("price_change", "book", "best_bid_ask"),
    )
    return args


def main() -> None:
    args = parse_args()
    def _ignore_websockets_close_noise(loop: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
        exc = context.get("exception")
        if isinstance(exc, AttributeError) and "recv_messages" in str(exc):
            return
        loop.default_exception_handler(context)

    loop = asyncio.new_event_loop()
    loop.set_exception_handler(_ignore_websockets_close_noise)
    asyncio.set_event_loop(loop)
    try:
        if args.once:
            loop.run_until_complete(listen_once(args))
        else:
            loop.run_until_complete(run_forever(args))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
