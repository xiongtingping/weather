"""The calibration lanes' envelope reads, cached once a day is frozen.

User ruling 2026-10-01 (「现在做」).  The width/centre lane
(``center_width_store.serving_cells_by_day``) and the off-centre lane
(``band_reliability_store.serving_lane_by_station_day``) refit nightly on
every decision since their evidence anchor (charter 202's epoch), and each
sampled decision is rebuilt from its envelope through
``probability_bridge.history_envelope_payload`` -- which kept every envelope
since the anchor in the SSOT, and in a few weeks would have stopped the
envelope's cold-tier offload outright (charter 238).

So a payload the SSOT verified (its rebuilt hash equals the bridge row's) is
kept here once its event day is FREEZE_LAG_DAYS past: the probabilities, the
bucket ordinals and, of the lineage, the keys the two lanes read
(``PAYLOAD_LINEAGE_KEYS``; a test scans the readers so a new key cannot be
read off a cached payload unseen).  Every later read of that row comes from
here.  Each completed sampler pass is recorded, so the offload lane knows
which days every sampled row of has been kept.

The cache is a convenience, never an authority: if it cannot be opened, the
SSOT is read as before.  It never sits on the external volume.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "serving_payload_cache.db"
SCHEMA = "serving_payload_cache_v1"
FREEZE_LAG_DAYS = 3
#: What the two lanes read off a payload's lineage (``_serving_cell``,
#: ``_receipt_of``, ``_pre_band_claim`` and the off-centre sampler).
PAYLOAD_LINEAGE_KEYS = (
    "band_reliability_json",
    "bucket_intervals_c_json",
    "center_width_json",
    "hard_excluded_buckets",
    "model_center_c",
    "system_weights_json",
    "taf_observed_tx_c",
)
DDL = """
CREATE TABLE IF NOT EXISTS payload (
  checkpoint_id TEXT NOT NULL,
  envelope_content_sha256 TEXT NOT NULL,
  event_date TEXT NOT NULL,
  body TEXT NOT NULL,
  schema TEXT NOT NULL,
  frozen_at_utc TEXT NOT NULL,
  PRIMARY KEY(checkpoint_id, envelope_content_sha256)
);
CREATE TABLE IF NOT EXISTS sampler_pass (
  sampler TEXT NOT NULL,
  stage TEXT NOT NULL,
  pre_band INTEGER NOT NULL,
  min_decision_at_utc TEXT NOT NULL,
  frozen_through TEXT NOT NULL,
  completed_at_utc TEXT NOT NULL,
  PRIMARY KEY(sampler, stage, pre_band, min_decision_at_utc)
);
"""

_LOCK = threading.Lock()
_CONNECTIONS: dict[str, sqlite3.Connection | None] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def frozen_through(now: datetime | None = None) -> str:
    return ((now or _utc_now()).date() - timedelta(days=FREEZE_LAG_DAYS)).isoformat()


#: The store whose rows this cache keeps.  Any other store (a test fixture,
#: a copy) is read as before and never written here: a fixture's "pass"
#: recorded against the production cache would let the offload lane move
#: rows no pass ever kept.
PRODUCTION_STORE = ROOT / "data" / "bayes_basket_target_ssot.db"


def _serves(conn: sqlite3.Connection, cache_path: Path | str | None) -> bool:
    if cache_path is not None:
        return True
    try:
        main = next(
            (str(row[2]) for row in conn.execute("PRAGMA database_list") if row[1] == "main"), ""
        )
    except sqlite3.Error:
        return False
    return bool(main) and Path(main).resolve() == PRODUCTION_STORE.resolve()


def _cache(path: Path | str | None = None) -> sqlite3.Connection | None:
    """This process's connection to the cache, or None when it cannot be had."""

    key = str(Path(path) if path is not None else CACHE_PATH)
    with _LOCK:
        if key in _CONNECTIONS:
            return _CONNECTIONS[key]
        try:
            Path(key).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(key, timeout=30.0, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(DDL)
        except (OSError, sqlite3.Error):
            conn = None
        _CONNECTIONS[key] = conn
        return conn


def _project(payload: Mapping[str, Any]) -> dict[str, Any]:
    lineage = payload.get("lineage") or {}
    return {
        "probabilities": payload.get("probabilities"),
        "bucket_ordinals": payload.get("bucket_ordinals"),
        "lineage": {key: lineage[key] for key in PAYLOAD_LINEAGE_KEYS if key in lineage},
    }


def cached_history_envelope_payload(
    conn: sqlite3.Connection,
    *,
    checkpoint_id: str,
    envelope_content_sha256: str,
    stored: str | None = None,
    cache_path: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """``history_envelope_payload``, read from the cache once its day is frozen.

    A row carrying its body inline (``stored``, before 2026-08-26) needs no
    envelope and is passed straight through.
    """

    from .probability_bridge import history_envelope_payload  # noqa: PLC0415

    if stored:
        return history_envelope_payload(
            conn, checkpoint_id=checkpoint_id,
            envelope_content_sha256=envelope_content_sha256, stored=stored,
        )
    cache = _cache(cache_path) if _serves(conn, cache_path) else None
    if cache is not None:
        try:
            row = cache.execute(
                "SELECT body FROM payload WHERE checkpoint_id=? AND envelope_content_sha256=? AND schema=?",
                (str(checkpoint_id), str(envelope_content_sha256), SCHEMA),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            return json.loads(row[0])
    payload = history_envelope_payload(
        conn, checkpoint_id=checkpoint_id, envelope_content_sha256=envelope_content_sha256,
    )
    event_date = str(payload.get("event_date") or "")
    if cache is not None and event_date and event_date <= frozen_through(now):
        try:
            cache.execute(
                "INSERT OR REPLACE INTO payload VALUES(?,?,?,?,?,?)",
                (
                    str(checkpoint_id), str(envelope_content_sha256), event_date,
                    json.dumps(_project(payload), sort_keys=True, ensure_ascii=False),
                    SCHEMA, (now or _utc_now()).isoformat(timespec="seconds"),
                ),
            )
        except sqlite3.Error:
            pass
    return payload


def record_pass(
    sampler: str, *, stage: str, pre_band: bool, min_decision_at_utc: str | None,
    conn: sqlite3.Connection | None = None,
    cache_path: Path | str | None = None, now: datetime | None = None,
) -> None:
    """A sampler read every row it wanted off ``conn``: days up to frozen_through are kept."""

    if cache_path is None and (conn is None or not _serves(conn, None)):
        return
    cache = _cache(cache_path)
    if cache is None:
        return
    moment = now or _utc_now()
    try:
        cache.execute(
            "INSERT OR REPLACE INTO sampler_pass VALUES(?,?,?,?,?,?)",
            (
                sampler, str(stage), 1 if pre_band else 0, str(min_decision_at_utc or ""),
                frozen_through(moment), moment.isoformat(timespec="seconds"),
            ),
        )
    except sqlite3.Error:
        pass


def covered_through(
    sampler: str, *, stage: str, anchor_utc: str, cache_path: Path | str | None = None,
    now: datetime | None = None, max_age_days: int = 3,
) -> str | None:
    """The last frozen day a recent complete pass of ``sampler`` kept every row of.

    A pass counts when it read from no later than ``anchor_utc`` (its rows
    are a superset of the lane's) and finished within ``max_age_days``.
    None when there is none: the caller then keeps everything from the anchor.
    """

    cache = _cache(cache_path)
    if cache is None:
        return None
    since = ((now or _utc_now()) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    try:
        row = cache.execute(
            "SELECT MAX(frozen_through) FROM sampler_pass"
            " WHERE sampler=? AND stage=? AND min_decision_at_utc<=? AND completed_at_utc>=?",
            (sampler, str(stage), str(anchor_utc), since),
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


__all__ = [
    "covered_through",
    "CACHE_PATH",
    "FREEZE_LAG_DAYS",
    "PAYLOAD_LINEAGE_KEYS",
    "SCHEMA",
    "cached_history_envelope_payload",
    "frozen_through",
    "record_pass",
]
