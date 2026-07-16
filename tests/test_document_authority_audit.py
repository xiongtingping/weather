from pathlib import Path

from scripts.document_authority_audit import build_audit


def test_repository_documents_are_authoritative_and_code_aligned() -> None:
    root = Path(__file__).resolve().parents[1]

    report = build_audit(root)

    assert report["ok"] is True, report["violations"]
    assert report["document_count"] >= 9
    assert all(
        row["status"] in {"accepted", "superseded"}
        for row in report["documents"]
        if row["authority"] in {"L0", "L1"}
    )
    assert all(
        row["status"] in {"accepted", "research", "superseded", "archived"}
        for row in report["documents"]
    )


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
