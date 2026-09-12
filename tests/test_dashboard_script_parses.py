"""The page's own script has to parse.

Every panel on this board is a JavaScript template literal held inside a
Python string, so nothing between writing it and a reader opening the page
ever looks at its syntax: ``py_compile`` sees a valid string, the render
guard compares mtimes, and the build writes the file and exits 0.

2026-09-13 is the judgement.  A panel's template literal lost its closing
backtick during an edit.  The build succeeded, the payload was correct, the
HTML was written -- and the whole board below that function was dead,
because one unterminated literal swallows the rest of the script.  It would
have sat on the page until somebody opened it.

So the script is parsed.  Not executed and not linted: parsed, which is the
only failure this class of edit produces and the one nothing else catches.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


def _scripts() -> list[str]:
    page = C.render_html({"generated_at_utc": "x"}, started_at=0.0)
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", page, re.S)
    assert bodies, "the page carries no inline script to check"
    return bodies


def test_the_page_carries_the_panels_as_inline_script():
    joined = "\n".join(_scripts())
    # A canary from the section this guard was written for.
    assert "function edgeBandHealth()" in joined
    assert "function betaDial(" in joined


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on this machine"
)
def test_every_inline_script_parses(tmp_path):
    for index, body in enumerate(_scripts()):
        target = tmp_path / f"panel_{index}.js"
        target.write_text(body, encoding="utf-8")
        done = subprocess.run(
            ["node", "--check", str(target)],
            capture_output=True, text=True,
        )
        assert done.returncode == 0, (
            f"inline script #{index} does not parse:\n{done.stderr}"
        )
