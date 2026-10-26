"""JAXA temp downloads never outlive their reader (2026-10-26: 749 MiB of tmp*.nc)."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from bayes_basket_target import jaxa_ptree_radiation as jaxa


def _workdir(tmp_path):
    return tmp_path / "jaxa_ptree_tmp"


def test_a_curl_timeout_leaves_no_partial_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(jaxa, "CACHE_PATH", tmp_path / "cache.json")

    def timing_out(*args, timeout):
        raise subprocess.TimeoutExpired("curl", timeout)

    monkeypatch.setattr(jaxa, "_curl", timing_out)
    with pytest.raises(subprocess.TimeoutExpired):
        jaxa._download("/pub/x.nc")
    assert list(_workdir(tmp_path).iterdir()) == []


def test_a_killed_reader_s_file_is_swept_on_the_next_download(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(jaxa, "CACHE_PATH", tmp_path / "cache.json")
    workdir = _workdir(tmp_path)
    workdir.mkdir()
    orphan, live = workdir / "tmporphan.nc", workdir / "tmplive.nc"
    orphan.write_bytes(b"x")
    live.write_bytes(b"x")
    old = time.time() - jaxa.STALE_DOWNLOAD_SECONDS - 60
    os.utime(orphan, (old, old))

    monkeypatch.setattr(jaxa, "_curl", lambda *args, timeout: subprocess.CompletedProcess(args, 0, b"", b""))
    path = jaxa._download("/pub/x.nc")
    assert not orphan.exists()
    assert live.exists()
    os.unlink(path)
