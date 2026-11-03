#!/usr/bin/env python3
"""Turn the rendered dashboard into an Artifact-publishable page.

``calibration_dashboard.py`` writes a complete standalone document --
doctype, html, head, body -- which is right for a local file and wrong
for an Artifact: the publisher wraps the uploaded file in its own
skeleton, so a second set of those tags nests a document inside a
document.  This strips the wrapper and leaves the page's own content.

Two things are deliberately NOT touched.  The stylesheet is the board's
own design system and stays exactly as rendered.  The payload is copied
byte for byte, so the published page and the local one are the same page
-- if they ever disagree, the bug is here and not in what was measured.

The one behavioural change is the theme bootstrap.  The board stamps
``data-theme`` on the root before first paint, reading its own stored
choice and falling back to the OS preference.  Inside the Artifact viewer
the root may already carry a stamp -- the reader's explicit light/dark
choice in the surrounding UI -- and the board's bootstrap would overwrite
it, so a reader who set the viewer to light would still get a dark board.
The patched order is: this page's own saved choice, then the viewer's
stamp, then the OS.  A reader who has clicked the board's own toggle has
said something more specific than the viewer setting; a reader who has
not gets what the viewer asked for.

Re-run after every render.  The output is a plain file; publishing it is
a separate step (the Artifact tool), which is what keeps a re-render from
silently republishing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IN = ROOT / "tmp" / "cal_dashboard.html"
DEFAULT_OUT = ROOT / "tmp" / "cal_dashboard_artifact.html"

# What the publisher's own skeleton provides and this file must not repeat.
# The tag name must end the token (whitespace or ``>``): a bare ``<head[^>]*>``
# also matched inside the page's script -- ``ri+dr<headRows.length) ...`` up to
# the next ``>`` -- and the published board died on a SyntaxError.
_WRAPPER = (
    re.compile(r"<!doctype html>\s*", re.IGNORECASE),
    re.compile(r"<html(?:\s[^>]*)?>\s*", re.IGNORECASE),
    re.compile(r"</html>\s*", re.IGNORECASE),
    re.compile(r"<head(?:\s[^>]*)?>\s*", re.IGNORECASE),
    re.compile(r"</head>\s*", re.IGNORECASE),
    re.compile(r"<body(?:\s[^>]*)?>\s*", re.IGNORECASE),
    re.compile(r"</body>\s*", re.IGNORECASE),
)

_BOOTSTRAP_FROM = """    var saved = localStorage.getItem("board-theme");
    var dark = window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme =
      saved || (dark ? "dark" : "light");"""

_BOOTSTRAP_TO = """    var saved = localStorage.getItem("board-theme");
    // The Artifact viewer may have stamped the reader's explicit choice
    // here already; overwriting it would ignore what they asked for.
    var host = document.documentElement.dataset.theme;
    var dark = window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme =
      saved || host || (dark ? "dark" : "light");"""


def convert(source: str) -> str:
    title_match = re.search(r"<title>.*?</title>", source, re.DOTALL)
    if title_match is None:
        raise SystemExit("dashboard has no <title>; refusing to publish")
    title = title_match.group(0)
    body = source.replace(title, "", 1)
    for pattern in _WRAPPER:
        body = pattern.sub("", body)
    if _BOOTSTRAP_FROM not in body:
        raise SystemExit(
            "theme bootstrap not found in the expected form; the generator "
            "changed and this patch would silently no-op"
        )
    body = body.replace(_BOOTSTRAP_FROM, _BOOTSTRAP_TO, 1)
    # Title first: only the first 8KB of the file is scanned for it, and
    # leading it removes any doubt about which 8KB that is.
    return title + "\n" + body.lstrip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="source", type=Path, default=DEFAULT_IN)
    parser.add_argument("--out", dest="target", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    source = args.source.expanduser().resolve(strict=True)
    text = source.read_text(encoding="utf-8")
    converted = convert(text)
    for pattern in _WRAPPER:
        if pattern.search(converted):
            raise SystemExit(f"wrapper tag survived conversion: {pattern.pattern}")
    args.target.write_text(converted, encoding="utf-8")
    size_mb = len(converted.encode("utf-8")) / 1_048_576
    print(f"{args.target} ({size_mb:.2f} MB)")
    if size_mb > 16:
        print("WARNING: over the 16MB artifact ceiling", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
