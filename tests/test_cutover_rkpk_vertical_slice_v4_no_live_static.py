from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_rkpk_vertical_slice_v4_no_live.sh"
REPLACEMENT = (
    "scripts/cutover_bayes_basket_target_probability_evidence_no_live.sh"
)


def test_rkpk_vertical_entry_is_retired_before_any_state_change() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    commands = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "#!"))
    ]

    assert commands[0] == "exit 78"
    assert commands.count("exit 78") == 1
    assert REPLACEMENT in text
    assert "scripts/targetctl.sh" in text
    for forbidden in (
        "system/",
        "LaunchDaemon",
        "sudo",
        "launchctl",
        "sqlite",
        "python",
        "touch",
        "write",
    ):
        assert forbidden not in text


def test_rkpk_vertical_entry_is_side_effect_free(tmp_path: Path) -> None:
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    completed = subprocess.run(
        ["/bin/bash", str(SCRIPT), "--legacy-confirmation"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 78
    assert sorted(
        path.relative_to(tmp_path) for path in tmp_path.rglob("*")
    ) == before
