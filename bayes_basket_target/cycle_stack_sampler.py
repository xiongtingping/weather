"""Charter 180: where the production cycle's Python time goes.

The statement ledger (charter 177) names the SQL a cycle spends its time
on.  After charters 178 and 179 that is ~26s of a 103-186s cycle, and the
stages that are left -- the early P1 pass, the decision loop's quote
refresh, the replay compile -- spend the rest in Python the stage marks
cannot see inside: a refresh span of 13.8s (2026-09-21 10:39Z) was 9.8s of
named sub-spans and 4s of nothing.  Three hand-taken stack samples once
pointed at 1-2ms statements while the 75ms ones went unseen; a sampler that
runs all cycle long does not have that problem.

One daemon thread wakes every ``interval`` seconds, reads the cycle
thread's frame from ``sys._current_frames()`` and keeps the stack as a
tuple of interned ``file:function:firstline`` keys.  It never touches the
connection or the worker; it reads frames and appends to a list.  At the
end of the cycle the samples are summarized against the stage marks, which
are taken only when a stage ends, so a sample belongs to the first stage
whose mark falls after it.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
_ROOT_PREFIX = str(ROOT) + os.sep
_REPO_DIRS = (
    str(ROOT / "bayes_basket_target") + os.sep,
    str(ROOT / "scripts") + os.sep,
)
#: The statement probe's own frames: time in them is SQL, and the frame
#: that asked for the SQL is the one worth naming.
_PROBE_FUNCTIONS = frozenset({
    "execute", "executemany", "executescript", "fetchone", "fetchall",
    "fetchmany", "__next__", "__iter__",
})
_PROBE_FILE = str(ROOT / "bayes_basket_target" / "production_worker.py")
DEFAULT_INTERVAL_SEC = 0.05
#: Enough for a 40-minute cycle at the default interval.
MAX_SAMPLES = 50_000
_MAX_DEPTH = 80


def sampler_enabled() -> bool:
    return str(os.environ.get("PM_CYCLE_STACK_SAMPLER", "1")).strip() not in {
        "0", "false", "off", "",
    }


def _key(code) -> str:
    filename = code.co_filename
    if filename.startswith(_ROOT_PREFIX):
        filename = filename[len(_ROOT_PREFIX):]
    else:
        filename = os.path.basename(filename)
    return sys.intern(f"{filename}:{code.co_name}:{code.co_firstlineno}")


def _is_repo(code) -> bool:
    return code.co_filename.startswith(_REPO_DIRS)


def _is_probe(code) -> bool:
    return code.co_filename == _PROBE_FILE and code.co_name in _PROBE_FUNCTIONS


class CycleStackSampler:
    """Samples one thread's stack until stopped."""

    def __init__(
        self,
        target_ident: int,
        *,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
        max_samples: int = MAX_SAMPLES,
    ) -> None:
        self.target_ident = int(target_ident)
        self.interval_sec = max(0.005, float(interval_sec))
        self.max_samples = int(max_samples)
        # (perf_counter, leaf key, innermost repo key, repo keys outermost..in)
        self.samples: list[tuple[float, str, str, tuple[str, ...]]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "CycleStackSampler":
        self._thread = threading.Thread(
            target=self._loop, name="cycle_stack_sampler", daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> list[tuple[float, str, str, tuple[str, ...]]]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        return self.samples

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            if len(self.samples) >= self.max_samples:
                return
            frame = sys._current_frames().get(self.target_ident)
            if frame is None:
                continue
            now = time.perf_counter()
            leaf = _key(frame.f_code)
            innermost_repo = ""
            repo: list[str] = []
            depth = 0
            while frame is not None and depth < _MAX_DEPTH:
                code = frame.f_code
                if _is_repo(code):
                    key = _key(code)
                    repo.append(key)
                    if not innermost_repo and not _is_probe(code):
                        innermost_repo = key
                frame = frame.f_back
                depth += 1
            del frame
            repo.reverse()
            self.samples.append((now, leaf, innermost_repo, tuple(repo)))


def _stage_of(stamp: float, marks: Sequence[tuple[str, float]]) -> str:
    for stage, marked_at in marks:
        if stamp <= marked_at:
            return stage
    return "after_last_mark"


def summarize_samples(
    samples: Sequence[tuple[float, str, str, tuple[str, ...]]],
    *,
    marks: Sequence[tuple[str, float]],
    interval_sec: float,
    top: int = 15,
    stage_top: int = 4,
    stages: int = 6,
) -> dict[str, Any]:
    """Seconds by innermost frame, by innermost repo frame, by repo function
    anywhere on the stack (once per sample), and the innermost repo frames
    of the slowest stages."""

    seconds = float(interval_sec)
    leaf = Counter()
    repo_self = Counter()
    repo_cum = Counter()
    by_stage: dict[str, Counter] = {}
    stage_totals = Counter()
    ordered_marks = sorted(marks, key=lambda item: item[1])
    for stamp, leaf_key, repo_key, repo_keys in samples:
        leaf[leaf_key] += 1
        repo_self[repo_key or "<outside repo>"] += 1
        for key in set(repo_keys):
            repo_cum[key] += 1
        stage = _stage_of(stamp, ordered_marks)
        stage_totals[stage] += 1
        by_stage.setdefault(stage, Counter())[repo_key or "<outside repo>"] += 1

    def rows(counter: Counter, limit: int) -> list[list[Any]]:
        return [[key, round(count * seconds, 2)] for key, count in counter.most_common(limit)]

    return {
        "interval_ms": round(seconds * 1000.0, 1),
        "samples": len(samples),
        "sampled_sec": round(len(samples) * seconds, 1),
        "leaf_top": rows(leaf, top),
        "repo_self_top": rows(repo_self, top),
        "repo_cum_top": rows(repo_cum, top),
        "by_stage": {
            stage: {
                "sec": round(count * seconds, 1),
                "repo_self_top": rows(by_stage[stage], stage_top),
            }
            for stage, count in stage_totals.most_common(stages)
        },
    }


__all__ = [
    "CycleStackSampler",
    "DEFAULT_INTERVAL_SEC",
    "sampler_enabled",
    "summarize_samples",
]
