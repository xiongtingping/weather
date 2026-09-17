from pathlib import Path

from scripts.document_authority_audit import build_audit


def test_audit_blocks_retired_identity_in_normative_document(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "bad.md").write_text(
        """---
status: accepted
authority: L1
owner: test
effective_from: 2026-07-11
---
sequential_remaining_warming_bayes_runtime_v2
""",
        encoding="utf-8",
    )
    (tmp_path / "sequential_remaining_warming_bayes_runtime.py").write_text(
        "sequential_remaining_warming_bayes_runtime_v31",
        encoding="utf-8",
    )
    (tmp_path / "sequential_bayes_execution_contract.py").write_text(
        "sequential_remaining_warming_bayes_runtime_v31",
        encoding="utf-8",
    )

    report = build_audit(tmp_path)

    assert report["ok"] is False
    assert any(row["code"] == "retired_identity_in_normative_document" for row in report["violations"])


def test_audit_blocks_stale_v31_current_claim_in_normative_document(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "bad.md").write_text(
        """---
status: accepted
authority: L1
owner: test
effective_from: 2026-07-15
---
候选必须携带 v3.1 composer、posterior identity。
""",
        encoding="utf-8",
    )

    report = build_audit(tmp_path)

    assert report["ok"] is False
    assert any(
        row["code"] == "retired_identity_in_normative_document"
        and "候选必须携带 v3.1 composer" in row["details"]
        for row in report["violations"]
    )
