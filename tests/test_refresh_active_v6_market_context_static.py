"""What survives of the market-context refresh check.

The ordering half of this test read scripts/polymarket_monitor_daemon.sh
-- that the target capture stage ran before the context refresh, and that
a failure there was non-blocking.  That daemon was deleted on 2026-08-04
and nothing supervises those stages from a shell any more, so the
assertion could only raise FileNotFoundError.  Retired 2026-09-07 with its
subject.

The half that is about the running system is kept: the refresh declares
itself unable to block the physical lane, which is the property the
ordering was there to protect.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_market_context_refresh_never_blocks_the_physical_lane() -> None:
    refresh = (ROOT / "scripts/refresh_active_v6_market_context.py").read_text(
        encoding="utf-8"
    )

    assert '"blocks_physical_x": False' in refresh
