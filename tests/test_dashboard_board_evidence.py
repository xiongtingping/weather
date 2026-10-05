"""The board prints no number of its own: dated readings come from
config/ui/board_evidence_v1.json through payload.board_evidence, and the
five go-live gates publish the thresholds their code reads."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as cd  # noqa: E402

REGISTRY = ROOT / "config" / "ui" / "board_evidence_v1.json"


def test_registry_loads_with_dated_sourced_entries() -> None:
    out = cd.collect_board_evidence(REGISTRY)
    assert out["available"] and out["schema"] == "board_evidence_v1"
    for key, entry in out["entries"].items():
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["measured_on"]), key
        assert entry["source"], key
        assert isinstance(entry["values"], dict) and entry["values"], key


def test_every_reading_the_page_asks_for_is_registered() -> None:
    template = cd.HTML_TEMPLATE
    asked = set(re.findall(r'(?:withEv|EV)\("([a-z0-9_]+)"', template))
    assert asked, "the template reads no evidence at all"
    registered = set(json.loads(REGISTRY.read_text(encoding="utf-8"))["entries"])
    assert asked <= registered, sorted(asked - registered)


def test_a_missing_registry_is_said_not_invented(tmp_path: Path) -> None:
    out = cd.collect_board_evidence(tmp_path / "absent.json")
    assert out["available"] is False and out["entries"] == {}
    assert "reason" in out


