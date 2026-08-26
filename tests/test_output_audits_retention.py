"""Age is the wrong question for an audits entry that source code reads.

The pass prunes ``output/audits`` by age, and on 2026-08-26 that deleted the
sealed authority projection ``pr12_awc_observation_sweep.sh`` reads on every
tick: built once, read every ten minutes, never rewritten, so it ages like a
one-off dump and is read like a pointer.  The observation-identity lane exited
78 on the next tick and stayed down for a day.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "scripts"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from output_audits_retention import (  # noqa: E402
    build_retention_plan,
)


def _aged(path: Path, days: float) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    import os

    os.utime(path, (stamp, stamp))


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    audits = tmp_path / "audits"
    audits.mkdir()
    source = tmp_path / "src"
    source.mkdir()
    return audits, source


def test_a_referenced_entry_outlives_the_window(tmp_path: Path) -> None:
    audits, source = _workspace(tmp_path)
    pointer = audits / "sealed_authority_projection_20260805T120000Z.json"
    pointer.write_text("{}", encoding="utf-8")
    _aged(pointer, 90)
    (source / "sweep.sh").write_text(
        'PROJECTION="$ROOT/output/audits/'
        'sealed_authority_projection_20260805T120000Z.json"\n',
        encoding="utf-8",
    )

    plan = build_retention_plan(
        audits_root=audits, retention_days=14, reference_roots=[source]
    )

    assert plan["stale_count"] == 0
    assert plan["kept_referenced_count"] == 1
    kept = plan["kept_referenced_entries"][0]
    assert kept["path"] == str(pointer)
    assert kept["referenced_by"].endswith("sweep.sh")


def test_an_unreferenced_entry_still_ages_out(tmp_path: Path) -> None:
    """The guard must not become a blanket amnesty for everything old."""

    audits, source = _workspace(tmp_path)
    dump = audits / "one_off_investigation_current_v3.json"
    dump.write_text("{}", encoding="utf-8")
    _aged(dump, 90)
    (source / "sweep.sh").write_text("echo unrelated\n", encoding="utf-8")

    plan = build_retention_plan(
        audits_root=audits, retention_days=14, reference_roots=[source]
    )

    assert plan["kept_referenced_count"] == 0
    assert [item["path"] for item in plan["stale_entries"]] == [str(dump)]


def test_a_fresh_entry_is_never_scanned_for(tmp_path: Path) -> None:
    """Only entries age would discard need the scan; the rest are kept anyway."""

    audits, source = _workspace(tmp_path)
    fresh = audits / "written_this_morning.json"
    fresh.write_text("{}", encoding="utf-8")
    (source / "sweep.sh").write_text("written_this_morning.json\n", encoding="utf-8")

    plan = build_retention_plan(
        audits_root=audits, retention_days=14, reference_roots=[source]
    )

    assert plan["stale_count"] == 0
    assert plan["kept_referenced_count"] == 0
    assert plan["kept_fresh_count"] == 1


def test_a_referenced_directory_entry_is_kept_whole(tmp_path: Path) -> None:
    audits, source = _workspace(tmp_path)
    packet = audits / "native_history_packet"
    packet.mkdir()
    body = packet / "run.json"
    body.write_text("{}", encoding="utf-8")
    _aged(body, 90)
    _aged(packet, 90)
    (source / "loader.py").write_text(
        'PACKET = ROOT / "output" / "audits" / "native_history_packet"\n',
        encoding="utf-8",
    )

    plan = build_retention_plan(
        audits_root=audits, retention_days=14, reference_roots=[source]
    )

    assert plan["stale_count"] == 0
    assert plan["kept_referenced_count"] == 1
    assert plan["kept_referenced_entries"][0]["file_count"] == 1
