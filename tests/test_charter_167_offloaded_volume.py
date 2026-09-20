"""Charter 167: the cold directories live on an external volume, and a
reader that would silently read them as empty asserts instead.

User ruling 2026-09-20 (「移动电脑磁盘内容过去（不影响生产的，不依赖外置硬盘
的内容）」).  What these bind is the failure mode, measured rather than
assumed: four operations on a dangling symlink raise, and four return
"nothing is there".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.offloaded_paths import (  # noqa: E402
    OffloadedVolumeMissing,
    assert_present,
    is_offloaded,
)


@pytest.fixture()
def dangling(tmp_path: Path) -> Path:
    link = tmp_path / "research"
    link.symlink_to(tmp_path / "no_such_volume" / "research")
    return link


def test_only_an_unplugged_symlink_counts_as_offloaded(tmp_path: Path, dangling: Path) -> None:
    """Narrow on purpose.  A real directory is not offloaded, and neither is
    a symlink that resolves -- only the unplugged case is interesting, and
    the check never goes looking for mount points."""

    real = tmp_path / "plain"
    real.mkdir()
    live = tmp_path / "live"
    live.symlink_to(real)
    assert is_offloaded(dangling) is True
    assert is_offloaded(real) is False
    assert is_offloaded(live) is False
    assert is_offloaded(tmp_path / "does_not_exist_at_all") is False


def test_the_four_silent_operations_are_why_this_exists(dangling: Path) -> None:
    """Measured 2026-09-20, not assumed.  These four say "nothing is there"
    on an unplugged volume, and a reader that believes them reports no data."""

    assert dangling.exists() is False
    assert dangling.is_dir() is False
    assert list(dangling.glob("*")) == []
    assert list(os.walk(dangling)) == []


def test_the_loud_operations_need_no_guard(dangling: Path) -> None:
    """The other half of the measurement: these raise by themselves, so the
    guard is not needed in front of them.  In particular ``mkdir`` does NOT
    quietly create a directory over the dangling link."""

    with pytest.raises(FileExistsError):
        dangling.mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileNotFoundError):
        os.listdir(dangling)
    with pytest.raises(FileNotFoundError):
        dangling.resolve(strict=True)
    with pytest.raises(FileNotFoundError):
        (dangling / "f.txt").open("w")


def test_assert_present_passes_through_and_names_the_target(tmp_path: Path, dangling: Path) -> None:
    real = tmp_path / "plain"
    real.mkdir()
    assert assert_present(real) == real
    with pytest.raises(OffloadedVolumeMissing) as caught:
        assert_present(dangling, what="data/research")
    message = str(caught.value)
    assert "data/research" in message and "no_such_volume" in message


def test_the_two_silent_readers_call_the_guard() -> None:
    """The readers that would otherwise glob or test ``exists()`` on an
    unplugged volume: the resource audit (which would report the store as
    SMALLER than it is, in the middle of running out of disk) and the
    historical-truth rebuild.  Asserted on the call, not on the source text,
    so a rename does not leave this passing."""

    import ast

    for name, expected in (
        ("scripts/bayes_basket_target_resource_audit.py", 1),
        ("scripts/rebuild_bayes_basket_target_historical_truth.py", 2),
    ):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == assert_present.__name__
        ]
        assert len(calls) == expected, f"{name} has {len(calls)} guards, expected {expected}"
        imported = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "bayes_basket_target.offloaded_paths"
        ]
        assert imported, f"{name} does not import the guard"
