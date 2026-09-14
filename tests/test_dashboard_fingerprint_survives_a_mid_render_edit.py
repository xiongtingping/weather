"""A section-cache fingerprint is the loaded code's, not the file's as it is now.

A render takes forty minutes and ``calibration_dashboard.py`` is edited by
several sessions a day.  ``inspect.getsource`` re-reads a file whose mtime has
moved and slices it at the running code's line numbers, so an edit landing
mid-render handed the fingerprint a fragment of the new file at the old
offsets.  2026-09-14: the 05:56Z render died on ``tokenize.TokenError`` at
06:11Z, minutes after such an edit, and the board fell 91 minutes behind.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _load_copy(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    target = scripts / "calibration_dashboard.py"
    shutil.copy(ROOT / "scripts" / "calibration_dashboard.py", target)
    spec = importlib.util.spec_from_file_location("_dashboard_copy_under_test", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, target


def _edit_above_everything(target: Path) -> None:
    # What a session's edit does to every function below it: shifts it, and
    # leaves text at the old offsets that is not the function any more --
    # here, the middle of a string that never closes on its first line.
    text = target.read_text(encoding="utf-8")
    head, rest = text.split("\n", 1)
    padding = "".join(f'_PAD_{i} = """\nnot code {i}\n"""\n' for i in range(40))
    target.write_text(head + "\n" + padding + rest, encoding="utf-8")


def test_the_fingerprint_does_not_move_or_die_when_the_file_is_edited_mid_render(tmp_path) -> None:
    module, target = _load_copy(tmp_path)
    collectors = [module.collect_chain, module.collect_band_stages, module._band_chain_link]
    before = [module._section_fingerprint(c) for c in collectors]
    salt_before = module._center_landing_salt()

    _edit_above_everything(target)

    after = [module._section_fingerprint(c) for c in collectors]
    assert after == before
    assert module._center_landing_salt() == salt_before
    # And it is the function's own text, not a neighbour's.
    assert module._loaded_source(module.collect_chain).lstrip().startswith("def collect_chain(")


def test_the_fingerprint_still_follows_a_real_change_of_code(tmp_path) -> None:
    module, target = _load_copy(tmp_path)
    before = module._section_fingerprint(module.collect_chain)

    # A fresh process on the edited file sees the edit.
    text = target.read_text(encoding="utf-8").replace(
        '"④ 概率分档校准"', '"④ 概率分档校准 (edited)"', 1
    )
    target.write_text(text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_dashboard_copy_edited", target)
    edited = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(edited)

    assert edited._section_fingerprint(edited.collect_chain) != before


def test_something_that_is_not_this_modules_function_still_gets_a_key(tmp_path) -> None:
    module, _target = _load_copy(tmp_path)
    assert module._loaded_source(len)  # a builtin: repr, never an exception
    assert module._loaded_source(module._loaded_source).lstrip().startswith(
        "def _loaded_source("
    )
