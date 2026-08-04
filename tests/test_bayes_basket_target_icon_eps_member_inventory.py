from __future__ import annotations

import bz2
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError
from scripts.decode_bayes_basket_target_icon_eps_member_inventory import (
    decode_member_inventory,
)


def _message(member: int, *, count: int = 40, template: int = 1) -> bytes:
    section1 = (5).to_bytes(4, "big") + b"\x01"
    section4 = bytearray(37)
    section4[0:4] = (37).to_bytes(4, "big")
    section4[4] = 4
    section4[7:9] = template.to_bytes(2, "big")
    section4[9] = 0
    section4[10] = 0
    section4[34] = 192
    section4[35] = member
    section4[36] = count
    total = 16 + len(section1) + len(section4) + 4
    section0 = b"GRIB" + b"\x00\x00" + b"\x00\x02" + total.to_bytes(8, "big")
    return section0 + section1 + bytes(section4) + b"7777"


def _raw(tmp_path: Path, messages: list[bytes]) -> Path:
    path = tmp_path / "eps.grib2.bz2"
    path.write_bytes(bz2.compress(b"".join(messages)))
    return path


def test_exact_member_set_decodes_streaming(tmp_path: Path) -> None:
    result = decode_member_inventory(
        _raw(tmp_path, [_message(member) for member in range(1, 41)])
    )
    assert result["member_ids"] == list(range(1, 41))
    assert result["member_count"] == 40
    assert result["product_definition_template"] == "4.1"


def test_missing_or_duplicate_member_fails_closed(tmp_path: Path) -> None:
    messages = [_message(member) for member in range(1, 40)] + [_message(39)]
    with pytest.raises(ContractError, match="member_set_not_1_through_40"):
        decode_member_inventory(_raw(tmp_path, messages))


def test_wrong_forecast_count_or_template_fails_closed(tmp_path: Path) -> None:
    messages = [_message(member) for member in range(1, 41)]
    messages[4] = _message(5, count=39)
    with pytest.raises(ContractError, match="forecast_count_not_40"):
        decode_member_inventory(_raw(tmp_path, messages))
    messages[4] = _message(5, template=0)
    with pytest.raises(ContractError, match="template_not_4_1"):
        decode_member_inventory(_raw(tmp_path, messages))
