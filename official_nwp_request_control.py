"""Small persistent request gate for low-frequency official NWP collectors.

The gate is intentionally outside the trading and posterior paths.  It only
coordinates direct source requests across monitor processes and records
provider throttling so a retry cannot immediately recreate the burst.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import email.utils
import fcntl
import json
from pathlib import Path
import os
import time
from typing import Any, Iterator, Mapping


SCHEMA_VERSION = "official_nwp_request_control_v1"
CAPTURE_LEASE_NAME = "official_nwp_capture.lock"


def _now_epoch() -> float:
    return time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def request_control_path(source_root: Path, provider_id: str) -> Path:
    """Return one shared gate for curve and live-snapshot roots."""

    configured = str(os.environ.get("PM_OFFICIAL_NWP_REQUEST_CONTROL_ROOT") or "").strip()
    root = Path(configured).expanduser() if configured else Path(source_root).expanduser().resolve().parent / "official_nwp_request_control"
    return root / f"{str(provider_id).strip()}.json"


def capture_lease_path(source_root: Path) -> Path:
    """Return the shared lease for one complete curve capture."""

    configured = str(os.environ.get("PM_OFFICIAL_NWP_CAPTURE_LEASE_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(source_root).expanduser().resolve().parent / CAPTURE_LEASE_NAME


def try_acquire_capture_lease(source_root: Path) -> Any | None:
    """Acquire the non-blocking process-lifetime curve-capture lease."""

    path = capture_lease_path(source_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({"pid": os.getpid(), "started_at": _iso(_now_epoch())}) + "\n")
    handle.flush()
    return handle


def parse_retry_after(value: Any, *, now_epoch: float | None = None) -> float | None:
    """Parse both HTTP delta-seconds and HTTP-date Retry-After values."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, parsed.timestamp() - float(now_epoch if now_epoch is not None else _now_epoch()))


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return _mapping(value)


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(json.dumps(dict(state), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _locked_state(path: Path) -> Iterator[dict[str, Any]]:
    """Hold a tiny sidecar lock only while reading/updating the JSON state."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _read_state(path)
        try:
            yield state
        finally:
            _write_state(path, state)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def reserve_request_slot(
    *,
    path: Path,
    provider_id: str,
    min_interval_sec: float,
    now_epoch: float | None = None,
    sleep_fn: Any = time.sleep,
) -> dict[str, Any]:
    """Reserve a cross-process provider slot and sleep until it is usable."""

    now = float(now_epoch if now_epoch is not None else _now_epoch())
    interval = max(0.0, float(min_interval_sec))
    with _locked_state(path) as state:
        last_reserved = float(state.get("last_reserved_epoch") or 0.0)
        cooldown_until = float(state.get("cooldown_until_epoch") or 0.0)
        slot = max(now, last_reserved + interval, cooldown_until)
        wait_sec = max(0.0, slot - now)
        state.update(
            {
                "schema": SCHEMA_VERSION,
                "provider_id": str(provider_id),
                "last_reserved_epoch": slot,
                "last_reserved_at": _iso(slot),
                "updated_at": _iso(now),
            }
        )
    if wait_sec > 0.0:
        sleep_fn(wait_sec)
    return {"slot_epoch": slot, "wait_sec": wait_sec}


def record_provider_response(
    *,
    path: Path,
    provider_id: str,
    status_code: int | None,
    headers: Mapping[str, Any] | None = None,
    error: str = "",
    now_epoch: float | None = None,
    base_failure_cooldown_sec: float = 5.0,
    max_cooldown_sec: float = 900.0,
) -> dict[str, Any]:
    """Record response health and extend cooldown after throttling/transient failures."""

    now = float(now_epoch if now_epoch is not None else _now_epoch())
    header_map = {str(key).lower(): value for key, value in _mapping(headers).items()}
    retry_after = parse_retry_after(header_map.get("retry-after"), now_epoch=now)
    code = int(status_code) if status_code is not None else None
    throttled = code == 429
    # A missing object/run (404) is a provider publication state, not a
    # transport failure. Only retryable HTTP responses, or a transport
    # exception without an HTTP status, may extend the cooldown.
    transient = code in {408, 425, 429, 500, 502, 503, 504} or (code is None and bool(error))
    with _locked_state(path) as state:
        failures = int(state.get("consecutive_failures") or 0)
        if transient:
            failures += 1
        else:
            failures = 0
        exponential = min(float(max_cooldown_sec), float(base_failure_cooldown_sec) * (2 ** max(0, failures - 1)))
        cooldown = max(float(retry_after or 0.0), exponential if transient else 0.0)
        cooldown_until = now + min(float(max_cooldown_sec), cooldown)
        state.update(
            {
                "schema": SCHEMA_VERSION,
                "provider_id": str(provider_id),
                "last_status_code": code,
                "last_error": str(error or ""),
                "last_retry_after_sec": retry_after,
                "consecutive_failures": failures,
                "cooldown_until_epoch": cooldown_until if transient else 0.0,
                "cooldown_until": _iso(cooldown_until) if transient else "",
                "last_response_at": _iso(now),
                "updated_at": _iso(now),
            }
        )
    return {
        "status_code": code,
        "throttled": throttled,
        "retry_after_sec": retry_after,
        "cooldown_sec": min(float(max_cooldown_sec), cooldown),
        "cooldown_until": _iso(cooldown_until) if transient else "",
    }


__all__ = [
    "CAPTURE_LEASE_NAME",
    "SCHEMA_VERSION",
    "capture_lease_path",
    "parse_retry_after",
    "record_provider_response",
    "request_control_path",
    "reserve_request_slot",
    "try_acquire_capture_lease",
]
