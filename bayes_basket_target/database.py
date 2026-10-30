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
#: Where the store itself lives (2026-10-29, user 「ssd能否做一个临时的ssot，
#: 然后阶段性的搬迁至外置盘」): the repository is on the external spinning disk,
#: the store stays on the internal SSD, and ``DEFAULT_TARGET_DB`` is a symlink
#: to exactly this file.  It is the one symlink the boundary accepts.
HOT_ROOT = Path.home() / "weather_hot"
HOT_TARGET_DB = HOT_ROOT / "bayes_basket_target_ssot.db"
#: The market-data listener's own outputs: the book manifest (7 MB, rewritten
#: every 2 s), its heartbeat, the per-token quote shards (1,232 renames a
#: dump).  On the external spinning disk every one of those writes ran on the
#: listener's event loop; while WRF wrote its 30 MB restart files to the same
#: disk the loop sat in rename/write for minutes, the publisher wrote nothing,
#: and its watchdog restarted it 26 times in 15 hours (2026-10-29T09Z..
#: 10-30T00Z, 185-1207 s each) -- every lane and the operator's basket were
#: refused a stale book in between.  Plain files here, not symlinks.
MARKET_WS_HOT_DIR = HOT_ROOT / "market_ws"
DEFAULT_TARGET_PAUSE_MARKER = ROOT / "data" / "bayes_basket_target_migration_paused.flag"
PROTECTED_RESIDENT_WS_MANIFEST = (MARKET_WS_HOT_DIR / "market_ws_latest_quotes.json").resolve()
DEFAULT_TARGET_WS_MANIFEST = MARKET_WS_HOT_DIR / "bayes_basket_target_official_ws_manifest.json"

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
    if candidate.is_symlink() and candidate.resolve() != HOT_TARGET_DB.resolve():
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
    "HOT_ROOT",
    "HOT_TARGET_DB",
    "MARKET_WS_HOT_DIR",
    "PROTECTED_RESIDENT_WS_MANIFEST",
    "ROOT",
    "SHARED_LIVE_DB",
    "TARGET_ACTIVATION_DDL",
    "is_isolated_target_database",
    "resolve_target_database",
    "resolve_target_ws_manifest",
]
