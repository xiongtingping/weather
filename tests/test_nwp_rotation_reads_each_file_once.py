"""The NWP rotation verifies each unchanged file once, not once per read.

2026-09-15: the resident's forecast turn held the single writer for about four
minutes, three and a half of them advancing sixty-odd rotation scopes -- about
3.9 s a call inside the resident.  Profiled on a read-only replica, most of a
call was re-reading the same two files: the official native inbox (4.7 MB,
shape walk plus content hash) two or three times, and the registry snapshot
(2.9 MB) up to three times.  Cached by file identity the replica went from 590
to 221 ms a call.  These tests pin what the cache must never change.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import bayes_basket_target.market_registry as mr
import bayes_basket_target.production_worker as pw
import bayes_basket_target.resource_bounds as rb
from bayes_basket_target.contracts import ContractError, content_sha256


@pytest.fixture(autouse=True)
def _empty_cache():
    rb._VERIFIED_FILE_CACHE.clear()
    yield
    rb._VERIFIED_FILE_CACHE.clear()


def _counting(loader):
    calls: list[Path] = []

    def wrapped(path):
        calls.append(Path(path))
        return loader(path)

    return wrapped, calls


def test_an_unchanged_file_is_loaded_once_and_each_caller_gets_its_own_copy(
    tmp_path,
) -> None:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"rows": [{"bid": 0.4}]}), encoding="utf-8")
    loader, calls = _counting(rb.load_bounded_json)

    first = rb.load_verified_file_cached(path, loader, namespace="t")
    first["rows"][0]["bid"] = 0.99
    second = rb.load_verified_file_cached(path, loader, namespace="t")

    assert len(calls) == 1
    assert second == {"rows": [{"bid": 0.4}]}


def test_a_replaced_file_is_read_again(tmp_path) -> None:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"v": 1}), encoding="utf-8")
    loader, calls = _counting(rb.load_bounded_json)
    assert rb.load_verified_file_cached(path, loader, namespace="t") == {"v": 1}

    replacement = tmp_path / "next.json"
    replacement.write_text(json.dumps({"v": 2}), encoding="utf-8")
    os.replace(replacement, path)       # how every publisher here writes

    assert rb.load_verified_file_cached(path, loader, namespace="t") == {"v": 2}
    assert len(calls) == 2


def test_a_failing_load_is_never_stored(tmp_path) -> None:
    path = tmp_path / "doc.json"
    path.write_text("{}", encoding="utf-8")
    attempts: list[int] = []

    def refuses(_path):
        attempts.append(1)
        raise ContractError("refused")

    for _ in range(2):
        with pytest.raises(ContractError):
            rb.load_verified_file_cached(path, refuses, namespace="t")
    assert len(attempts) == 2
    assert not rb._VERIFIED_FILE_CACHE


def test_two_loaders_of_one_file_do_not_answer_for_each_other(tmp_path) -> None:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"v": 1}), encoding="utf-8")
    assert rb.load_verified_file_cached(
        path, lambda p: "raw", namespace="a"
    ) == "raw"
    assert rb.load_verified_file_cached(
        path, lambda p: "verified", namespace="b"
    ) == "verified"


def test_the_cache_stays_bounded(tmp_path) -> None:
    for index in range(rb.VERIFIED_FILE_CACHE_ENTRIES + 3):
        path = tmp_path / f"doc{index}.json"
        path.write_text(json.dumps({"i": index}), encoding="utf-8")
        rb.load_verified_file_cached(path, rb.load_bounded_json, namespace="t")
    assert len(rb._VERIFIED_FILE_CACHE) == rb.VERIFIED_FILE_CACHE_ENTRIES


def test_the_registry_snapshot_is_verified_once_per_file(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "registry.json"
    path.write_text("{}", encoding="utf-8")
    loader, calls = _counting(lambda p: {"rows": [], "snapshot_sha256": "x"})
    monkeypatch.setattr(mr, "_read_payload", loader)

    mr.load_market_registry_snapshot(path)
    mr.load_market_registry_snapshot(path)

    assert len(calls) == 1


def _inbox(path: Path, packets: list[dict]) -> Path:
    identity = {"schema": "native_ensemble_forward_inbox_v1", "packets": packets}
    path.write_text(
        json.dumps({**identity, "content_sha256": content_sha256(identity)}),
        encoding="utf-8",
    )
    return path


def test_the_native_inbox_is_verified_once_whatever_the_callers_error_name(
    tmp_path, monkeypatch
) -> None:
    path = _inbox(tmp_path / "inbox.json", [{"station_id": "KMIA"}])
    parses: list = []
    real = pw.load_bounded_json

    def counting(p, **kw):
        parses.append(p)
        return real(p, **kw)

    monkeypatch.setattr(pw, "load_bounded_json", counting)

    assert pw._load_native_forward_inbox(path, error_code="a_invalid") == [
        {"station_id": "KMIA"}
    ]
    assert pw._load_native_forward_inbox(path, error_code="b_invalid") == [
        {"station_id": "KMIA"}
    ]
    assert len(parses) == 1


def test_a_tampered_inbox_is_refused_under_each_callers_own_name(
    tmp_path,
) -> None:
    path = _inbox(tmp_path / "inbox.json", [{"station_id": "KMIA"}])
    doc = json.loads(path.read_text())
    doc["packets"].append({"station_id": "EGLC"})     # hash no longer matches
    path.write_text(json.dumps(doc), encoding="utf-8")

    for code in ("a_invalid", "b_invalid"):
        with pytest.raises(ContractError, match=code):
            pw._load_native_forward_inbox(path, error_code=code)


def test_a_copy_handed_out_from_memory_is_the_callers_own_too(tmp_path) -> None:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"rows": [{"bid": 0.4}]}), encoding="utf-8")
    rb.load_verified_file_cached(path, rb.load_bounded_json, namespace="t")

    from_memory = rb.load_verified_file_cached(
        path, rb.load_bounded_json, namespace="t"
    )
    from_memory["rows"][0]["bid"] = 0.99

    assert rb.load_verified_file_cached(
        path, rb.load_bounded_json, namespace="t"
    ) == {"rows": [{"bid": 0.4}]}


def test_a_file_replaced_during_the_read_is_not_stored(tmp_path) -> None:
    """The capture pool renames a new inbox into place while the rotation
    reads the old one; what was read must not be filed under either file."""

    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"v": 1}), encoding="utf-8")

    def read_then_replaced(resolved):
        value = rb.load_bounded_json(resolved)
        replacement = tmp_path / "next.json"
        replacement.write_text(json.dumps({"v": 2, "pad": "x" * 10}), encoding="utf-8")
        os.replace(replacement, resolved)
        return value

    assert rb.load_verified_file_cached(
        path, read_then_replaced, namespace="t"
    ) == {"v": 1}
    assert not rb._VERIFIED_FILE_CACHE
    assert rb.load_verified_file_cached(
        path, rb.load_bounded_json, namespace="t"
    ) == {"v": 2, "pad": "x" * 10}
