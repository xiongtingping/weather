"""A verified inline manifest is parsed once per file, and every caller gets its own copy.

One evidence pass materializes a day's merged manifest station by station, and
each station's cache row and snapshot re-read and re-verified the whole
document -- measured 2026-09-14 on a 45-station, 1.39 MB manifest: 65-100 ms a
read, 45 reads a cache pass and 90 a snapshot pass, 92% and 87% of those
passes, six passes a cycle.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from bayes_basket_target import market_ingress as mi
from bayes_basket_target.contracts import content_sha256


def _inline_manifest(path: Path, *, bid: float = 0.40) -> Path:
    quote = {
        "token_id": "yes-38", "side": "YES", "best_bid": bid, "best_ask": 0.42,
        "bids": [{"price": bid, "size": 10.0}], "asks": [{"price": 0.42, "size": 10.0}],
        "book_observed_at": "2026-07-23T05:00:00Z", "created_at": "2026-07-23T05:00:00Z",
    }
    root = {
        "schema": mi.MANIFEST_SCHEMA, "state": "connected", "source": "official_polymarket_clob_websocket",
        "evidence_only": True, "authorizes_order": False, "active_shards": [0], "expected_shards": 1,
        "quote_storage": "inline_v1", "quotes_by_token": {"yes-38": quote},
    }
    root["snapshot_sha256"] = __import__("hashlib").sha256(mi._canonical(root)).hexdigest()
    path.write_text(json.dumps(root), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _empty_cache():
    mi._VERIFIED_MANIFEST_CACHE.clear()
    yield
    mi._VERIFIED_MANIFEST_CACHE.clear()


def _count_parses(monkeypatch) -> list[Path]:
    seen: list[Path] = []
    real = mi._read_json

    def counting(path, name):
        seen.append(Path(path))
        return real(path, name)

    monkeypatch.setattr(mi, "_read_json", counting)
    return seen


def test_the_same_file_is_parsed_once_and_answers_the_same(tmp_path, monkeypatch) -> None:
    path = _inline_manifest(tmp_path / "m.json")
    parses = _count_parses(monkeypatch)
    first = mi.load_official_ws_quotes(path)
    second = mi.load_official_ws_quotes(path)
    assert len(parses) == 1
    assert first == second


def test_a_caller_that_mutates_its_copy_cannot_reach_the_next_caller(tmp_path) -> None:
    path = _inline_manifest(tmp_path / "m.json")
    root, quotes = mi.load_official_ws_quotes(path)
    quotes["yes-38"]["best_bid"] = 0.99
    root["state"] = "tampered"
    again_root, again_quotes = mi.load_official_ws_quotes(path)
    assert again_quotes["yes-38"]["best_bid"] == 0.40
    assert again_root["state"] == "connected"


def test_a_rewritten_file_is_read_again(tmp_path, monkeypatch) -> None:
    path = _inline_manifest(tmp_path / "m.json")
    assert mi.load_official_ws_quotes(path)[1]["yes-38"]["best_bid"] == 0.40
    replacement = _inline_manifest(tmp_path / "next.json", bid=0.41)
    os.replace(replacement, path)            # how the publisher writes: a new file
    assert mi.load_official_ws_quotes(path)[1]["yes-38"]["best_bid"] == 0.41


def test_a_file_that_fails_verification_fails_every_time(tmp_path, monkeypatch) -> None:
    path = _inline_manifest(tmp_path / "m.json")
    doc = json.loads(path.read_text())
    doc["state"] = "tampered"                 # hash no longer matches
    path.write_text(json.dumps(doc), encoding="utf-8")
    parses = _count_parses(monkeypatch)
    for _ in range(2):
        with pytest.raises(mi.ContractError, match="content_sha_mismatch"):
            mi.load_official_ws_quotes(path)
    assert len(parses) == 2


def test_a_sharded_manifest_is_never_stored(tmp_path, monkeypatch) -> None:
    from tests.test_bayes_basket_target_market_ingress import _manifest

    path = _manifest(tmp_path)               # sha256_token_shards_v1
    parses = _count_parses(monkeypatch)
    mi.load_official_ws_quotes(path)
    mi.load_official_ws_quotes(path)
    assert len(parses) == 2
    assert not mi._VERIFIED_MANIFEST_CACHE
