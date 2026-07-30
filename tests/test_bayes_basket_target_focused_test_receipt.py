from __future__ import annotations

from pathlib import Path

from scripts import bayes_basket_target_focused_test_receipt as receipt_module
from scripts.bayes_basket_target_focused_test_receipt import build_receipt


def test_focused_test_receipt_accepts_zero_failure_matrix(tmp_path: Path) -> None:
    xml = tmp_path / "pytest.xml"
    xml.write_text(
        '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0" time="1.25">'
        '<testcase classname="tests.test_a" name="test_a" file="tests/test_a.py"/>'
        '<testcase classname="tests.test_b" name="test_b" file="tests/test_b.py"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    result = build_receipt(xml, root_cause_regressions={})

    assert result["status"] == "PASS"
    assert result["tests"] == 2
    assert result["test_file_count"] == 2


def test_focused_test_receipt_blocks_any_failure(tmp_path: Path) -> None:
    xml = tmp_path / "pytest.xml"
    xml.write_text(
        '<testsuites><testsuite tests="1" failures="1" errors="0" skipped="0" time="0.1">'
        '<testcase classname="tests.test_a" name="test_a" file="tests/test_a.py">'
        '<failure message="boom"/></testcase></testsuite></testsuites>',
        encoding="utf-8",
    )

    result = build_receipt(xml, root_cause_regressions={})

    assert result["status"] == "BLOCKED"
    assert result["checks"]["failures_zero"] is False


def test_focused_test_receipt_defaults_are_canonical_and_not_date_scoped() -> None:
    assert receipt_module.DEFAULT_XML.name == "bayes_basket_target_focused_tests.xml"
    assert receipt_module.DEFAULT_OUTPUT.name == "bayes_basket_target_focused_test_receipt.json"
    assert "current_et_" not in str(receipt_module.DEFAULT_XML)
    assert "current_et_" not in str(receipt_module.DEFAULT_OUTPUT)
