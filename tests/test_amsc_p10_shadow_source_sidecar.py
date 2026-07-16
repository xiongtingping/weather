from __future__ import annotations

import json
import ssl

from amsc_p10_shadow_bundle_loader import FORWARD_NEW_ONLY, load_amsc_p10_shadow_bundle
from amsc_p10_shadow_source_sidecar import (
    build_amsc_verified_ssl_context,
    capture_amsc_p10_shadow_source_attempt,
    fetch_amsc_with_observed_transport,
)


CURRENT_SLOT_PAYLOAD = {
    "code": 200,
    "data": [
        {"cccc": "ZUUU", "content": "METAR ZUUU 051430Z 18003MPS CAVOK 32/20 Q1001 NOSIG="},
        {"cccc": "ZUUU", "content": "TAF ZUUU 050904Z 0512/0612 20003MPS 6000 SCT040 TX36/0607Z TN27/0522Z="},
    ],
}


def _transport(*, poll_started_at: str, response_completed_at: str) -> dict[str, str]:
    return {
        "poll_started_at": poll_started_at,
        "response_started_at": "2026-07-05T14:30:56Z",
        "response_completed_at": response_completed_at,
        "transport_mode": "fixture",
    }


def test_sidecar_raw_archives_then_publishes_only_a_new_exact_slot_primary_bundle(tmp_path) -> None:
    archive_dir = tmp_path / "archive"
    inbox_dir = tmp_path / "inbox"

    result = capture_amsc_p10_shadow_source_attempt(
        payload=CURRENT_SLOT_PAYLOAD,
        transport=_transport(
            poll_started_at="2026-07-05T14:30:55Z",
            response_completed_at="2026-07-05T14:31:00Z",
        ),
        raw_archive_dir=archive_dir,
        inbox_dir=inbox_dir,
        report_count=6,
    )

    assert result["ok"] is True
    assert result["state"] == "published"
    assert result["raw_archive_dir"] == str(archive_dir.resolve())
    assert result["slot_fresh"] is True
    assert result["bundle_load"]["selected_primary_count"] == 1
    assert result["source_selection"]["candidate_count"] == 1
    assert result["boundary"]["writes_archive_db"] is False
    assert result["boundary"]["generic_task_queue"] is False
    assert result["boundary"]["subprocess_per_event"] is False
    inbox_path = inbox_dir / next(iter(path.name for path in inbox_dir.glob("*.json")))
    published = json.loads(inbox_path.read_text(encoding="utf-8"))
    loaded = load_amsc_p10_shadow_bundle(
        published,
        raw_archive_dir=archive_dir,
        mode=FORWARD_NEW_ONLY,
    )
    assert loaded["ok"] is True
    assert loaded["selected_primary_count"] == 1
    assert loaded["bundle"]["source_observations"][0]["payload"]["first_seen_at"] == "2026-07-05T14:31:00Z"


def test_sidecar_archives_stale_response_but_does_not_publish_it_as_the_current_slot(tmp_path) -> None:
    result = capture_amsc_p10_shadow_source_attempt(
        payload=CURRENT_SLOT_PAYLOAD,
        transport=_transport(
            poll_started_at="2026-07-05T15:00:55Z",
            response_completed_at="2026-07-05T15:01:00Z",
        ),
        raw_archive_dir=tmp_path / "archive",
        inbox_dir=tmp_path / "inbox",
        report_count=6,
    )

    assert result["ok"] is False
    assert result["state"] == "blocked_or_stale"
    assert result["slot_fresh"] is False
    assert "amsc_p10_sidecar_expected_slot_not_observed" in result["blockers"]
    assert not list((tmp_path / "inbox").glob("*.json"))


def test_sidecar_never_represents_already_archived_raw_as_a_new_forward_inbox_event(tmp_path) -> None:
    archive_dir = tmp_path / "archive"
    inbox_dir = tmp_path / "inbox"
    first = capture_amsc_p10_shadow_source_attempt(
        payload=CURRENT_SLOT_PAYLOAD,
        transport=_transport(
            poll_started_at="2026-07-05T14:30:55Z",
            response_completed_at="2026-07-05T14:31:00Z",
        ),
        raw_archive_dir=archive_dir,
        inbox_dir=inbox_dir,
        report_count=6,
    )
    second = capture_amsc_p10_shadow_source_attempt(
        payload=CURRENT_SLOT_PAYLOAD,
        transport=_transport(
            poll_started_at="2026-07-05T14:30:56Z",
            response_completed_at="2026-07-05T14:31:01Z",
        ),
        raw_archive_dir=archive_dir,
        inbox_dir=inbox_dir,
        report_count=6,
    )

    assert first["state"] == "published"
    # A later retry remains in the same expected slot but first-seen stays at
    # the original archive receipt, so no second P10 arrival is emitted.
    assert second["ok"] is True
    assert second["state"] == "ready_without_new_primary"
    assert second["archive"]["objects_written"] == 0
    assert second["bundle_load"]["selected_primary_count"] == 0
    assert len(list(inbox_dir.glob("*.json"))) == 1


def test_direct_sidecar_uses_certifi_plus_pinned_intermediate_without_disabling_tls(monkeypatch) -> None:
    seen: dict = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"code":200,"data":[]}'

    def fake_urlopen(_request, *, timeout, context):
        seen["timeout"] = timeout
        seen["context"] = context
        return Response()

    import amsc_p10_shadow_source_sidecar as sidecar

    monkeypatch.setattr(sidecar.urllib.request, "urlopen", fake_urlopen)
    payload, transport = fetch_amsc_with_observed_transport("token-redacted", count=1, timeout_seconds=3)

    context = build_amsc_verified_ssl_context()
    assert payload == {"code": 200, "data": []}
    assert seen["timeout"] == 3
    assert seen["context"].verify_mode == ssl.CERT_REQUIRED
    assert seen["context"].check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert transport["response_headers_observed"] is True
    assert "no_subprocess_fallback" in transport["transport_mode"]
