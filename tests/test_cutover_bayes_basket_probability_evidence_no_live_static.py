from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_bayes_basket_probability_evidence_no_live.sh"
REPLACEMENT = "scripts/cutover_bayes_basket_target_probability_evidence_no_live.sh"

FORBIDDEN_LEGACY_CONTROL = (
    "system/",
    "LaunchDaemon",
    "sudo",
    "launchctl",
    "sqlite",
    "psql",
    "curl",
    "python",
    "touch",
    "mkdir",
    "rm ",
    "mv ",
    "cp ",
    "tee ",
)


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _non_comment_commands(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_retired_entry_fails_before_any_legacy_control_or_state_change() -> None:
    text = _text()
    commands = _non_comment_commands(text)

    assert commands[0] == "exit 78"
    assert commands.count("exit 78") == 1
    assert REPLACEMENT in text
    assert all(marker not in text for marker in FORBIDDEN_LEGACY_CONTROL)


def test_retired_entry_invocation_is_side_effect_free(tmp_path: Path) -> None:
    before = sorted(
        path.relative_to(tmp_path)
        for path in tmp_path.rglob("*")
    )

    for args in ([], ["--legacy-confirmation"]):
        completed = subprocess.run(
            ["/bin/bash", str(SCRIPT), *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert completed.returncode == 78
        assert completed.stdout == ""
        assert completed.stderr == ""
        assert sorted(
            path.relative_to(tmp_path)
            for path in tmp_path.rglob("*")
        ) == before
