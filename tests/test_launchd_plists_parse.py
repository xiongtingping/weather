"""Every LaunchAgent in launchd/ parses the way the installer reads it.

scripts/install_launchagent.sh loads each plist with plistlib (expat), which
is stricter than launchd and ``plutil -lint``: a ``--`` inside an XML
comment passes both and fails the installer before anything is installed
(2026-09-24, the research render's plist, "not well-formed").
"""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("path", sorted((ROOT / "launchd").glob("*.plist")), ids=lambda p: p.name)
def test_the_plist_parses_as_the_installer_reads_it(path: Path) -> None:
    with path.open("rb") as handle:
        document = plistlib.load(handle)
    assert document["Label"] == path.name.removesuffix(".plist")
