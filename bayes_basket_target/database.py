"""Database boundary for the isolated Bayes × basket target lane.

The target lane is a separate no-live deployment. Its default database is
never the resident live database, and every CLI that accepts ``--db`` can use
this module to reject an accidental shared-live path before opening SQLite.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHARED_LIVE_DB = (ROOT / "data" / "archive.db").resolve()
DEFAULT_TARGET_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_TARGET_PAUSE_MARKER = ROOT / "data" / "bayes_basket_target_migration_paused.flag"
PROTECTED_RESIDENT_WS_MANIFEST = (ROOT / "data" / "market_ws_latest_quotes.json").resolve()
DEFAULT_TARGET_WS_MANIFEST = ROOT / "data" / "bayes_basket_target_official_ws_manifest.json"

TARGET_ACTIVATION_DDL = """
CREATE TABLE IF NOT EXISTS basket_strategy_activation_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  generation INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('disabled','enabled','paused')),
  strategy_id TEXT NOT NULL,
  activation_sha256 TEXT NOT NULL,
  activation_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
"""


def resolve_target_database(path: Path | str | None = None) -> Path:
    """Resolve and validate a target DB path without creating it."""

    candidate = DEFAULT_TARGET_DB if path is None else Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError("target_database_symlink_forbidden")
    resolved = candidate.resolve()
    if resolved == SHARED_LIVE_DB:
        raise ValueError("target_database_must_not_be_shared_live_database")
    return resolved


def is_isolated_target_database(path: Path | str) -> bool:
    """Return whether ``path`` is outside the resident shared-live DB."""

    try:
        return resolve_target_database(path) != SHARED_LIVE_DB
    except (OSError, ValueError):
        return False


def resolve_target_ws_manifest(path: Path | str | None = None) -> Path:
    """Resolve a target-owned WS artifact and reject the protected resident input."""

    candidate = DEFAULT_TARGET_WS_MANIFEST if path is None else Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError("target_ws_manifest_symlink_forbidden")
    resolved = candidate.resolve()
    if resolved == PROTECTED_RESIDENT_WS_MANIFEST:
        raise ValueError("target_ws_manifest_must_not_be_protected_resident_manifest")
    return resolved


__all__ = [
    "DEFAULT_TARGET_DB",
    "DEFAULT_TARGET_PAUSE_MARKER",
    "DEFAULT_TARGET_WS_MANIFEST",
    "PROTECTED_RESIDENT_WS_MANIFEST",
    "ROOT",
    "SHARED_LIVE_DB",
    "TARGET_ACTIVATION_DDL",
    "is_isolated_target_database",
    "resolve_target_database",
    "resolve_target_ws_manifest",
]
