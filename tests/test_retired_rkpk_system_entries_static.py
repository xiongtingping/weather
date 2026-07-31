from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPLACEMENT = (
    "scripts/cutover_bayes_basket_target_probability_evidence_no_live.sh"
)
TARGETCTL = "scripts/targetctl.sh"
SCRIPTS = (
    "cutover_rkpk_jit_ws_no_live.sh",
    "cutover_rkpk_preliminary_ws_decision_no_live.sh",
    "cutover_rkpk_vertical_slice_v4_no_live.sh",
    "reload_p0_economic_identity_system_no_live.sh",
    "reload_probability_consumer_authority_system_no_live.sh",
)
FORBIDDEN = (
    "system/",
    "LaunchDaemon",
    "sudo",
    "launchctl",
    "sqlite",
    "python",
    "touch",
    "write",
)


def _commands(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "#!"))
    ]


def test_all_rkpk_and_system_reload_entries_are_uniformly_retired() -> None:
    for name in SCRIPTS:
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        commands = _commands(text)
        assert commands[0] == "exit 78", name
        assert commands.count("exit 78") == 1, name
        assert REPLACEMENT in text, name
        assert TARGETCTL in text, name
        assert all(marker not in text for marker in FORBIDDEN), name


def test_all_retired_entries_are_side_effect_free(tmp_path: Path) -> None:
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    for name in SCRIPTS:
        completed = subprocess.run(
            ["/bin/bash", str(ROOT / "scripts" / name), "--old-confirmation"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert completed.returncode == 78, name
        assert sorted(
            path.relative_to(tmp_path) for path in tmp_path.rglob("*")
        ) == before, name
