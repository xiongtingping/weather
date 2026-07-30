from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.cash_phase import BasketCashPhasePolicyArtifact
from bayes_basket_target.cash_phase_authority import (
    CANDIDATE_AUTHORITY_SCOPE,
    FORMAL_AUTHORITY_SCOPE,
    CashPhaseAuthorityManifest,
    cash_phase_authority_candidates,
    load_cash_phase_authority_manifest,
    resolve_active_cash_phase_policy,
)
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.resident_market_ingress import _policy_for_scope


OBSERVED = "2026-07-27T12:00:00Z"
VALID_FROM = "2026-01-01T00:00:00Z"
VALID_UNTIL = "2027-01-01T00:00:00Z"


def _policy(
    version: str,
    *,
    timezone_name: str = "UTC",
    valid_from: str = VALID_FROM,
    valid_until: str = VALID_UNTIL,
    promoted: bool = True,
) -> BasketCashPhasePolicyArtifact:
    return BasketCashPhasePolicyArtifact.create(
        policy_version=version,
        station_id="TEST",
        station_timezone=timezone_name,
        heavy_to_standard_local_time="12:00",
        standard_to_tail_local_time="18:00",
        cash_by_phase={"HEAVY": 3.0, "STANDARD": 2.0, "TAIL": 1.0},
        valid_from_utc=valid_from,
        valid_until_utc=valid_until,
        evidence_status="PROMOTED" if promoted else "DESIGN_CANDIDATE",
        promoted=promoted,
    )


def _manifest(
    policy: BasketCashPhasePolicyArtifact,
    *,
    authority_scope: str = FORMAL_AUTHORITY_SCOPE,
    timezone_name: str | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
) -> CashPhaseAuthorityManifest:
    return CashPhaseAuthorityManifest.create(
        station_id=policy.station_id,
        policy_version=policy.policy_version,
        artifact_sha256=policy.content_sha256,
        station_timezone=timezone_name or policy.station_timezone,
        valid_from_utc=valid_from or policy.valid_from_utc,
        valid_until_utc=valid_until or policy.valid_until_utc,
        authority_scope=authority_scope,
    )


def test_exact_manifest_selects_named_policy_and_ignores_newer_version() -> None:
    old = _policy("cash-v1", promoted=False)
    newer = _policy("cash-v2", promoted=False)
    selected = resolve_active_cash_phase_policy(
        [old, newer],
        authority_manifest=_manifest(
            old, authority_scope=CANDIDATE_AUTHORITY_SCOPE
        ),
        station_id="TEST",
        observed_at_utc=OBSERVED,
        required_authority_scope=CANDIDATE_AUTHORITY_SCOPE,
    )
    assert selected is old


def test_duplicate_exact_authority_fails_closed_as_ambiguous() -> None:
    policy = _policy("cash-v1", promoted=False)
    with pytest.raises(ContractError, match="cash_phase_authority_ambiguous"):
        resolve_active_cash_phase_policy(
            [policy, policy],
            authority_manifest=_manifest(
                policy, authority_scope=CANDIDATE_AUTHORITY_SCOPE
            ),
            station_id="TEST",
            observed_at_utc=OBSERVED,
            required_authority_scope=CANDIDATE_AUTHORITY_SCOPE,
        )


def test_formal_authority_is_blocked_until_dedicated_oos_schema_exists() -> None:
    candidate = _policy("cash-candidate-v1", promoted=False)
    with pytest.raises(
        ContractError,
        match="cash_phase_formal_oos_authority_schema_missing",
    ):
        resolve_active_cash_phase_policy(
            [candidate],
            authority_manifest=_manifest(candidate),
            station_id="TEST",
            observed_at_utc=OBSERVED,
            required_authority_scope=FORMAL_AUTHORITY_SCOPE,
        )


def test_promoted_flag_cannot_substitute_for_formal_oos_authority() -> None:
    promoted = BasketCashPhasePolicyArtifact.create(
        policy_version="cash-offline-validated-v1",
        station_id="TEST",
        station_timezone="UTC",
        heavy_to_standard_local_time="12:00",
        standard_to_tail_local_time="18:00",
        cash_by_phase={"HEAVY": 3.0, "STANDARD": 2.0, "TAIL": 1.0},
        valid_from_utc=VALID_FROM,
        valid_until_utc=VALID_UNTIL,
        evidence_status="OFFLINE_VALIDATED",
        promoted=True,
    )
    with pytest.raises(
        ContractError,
        match="cash_phase_formal_oos_authority_schema_missing",
    ):
        cash_phase_authority_candidates(
            [promoted],
            station_id="TEST",
            station_timezone="UTC",
            observed_at_utc=OBSERVED,
            observed_cash_phase="STANDARD",
            authority_scope=FORMAL_AUTHORITY_SCOPE,
        )


def test_candidate_scope_can_select_exact_candidate_but_not_a_promoted_row() -> None:
    candidate = _policy("cash-candidate-v1", promoted=False)
    selected = resolve_active_cash_phase_policy(
        [candidate],
        authority_manifest=_manifest(
            candidate,
            authority_scope=CANDIDATE_AUTHORITY_SCOPE,
        ),
        station_id="TEST",
        observed_at_utc=OBSERVED,
        required_authority_scope=CANDIDATE_AUTHORITY_SCOPE,
    )
    assert selected is candidate

    promoted = _policy("cash-promoted-v1", promoted=True)
    with pytest.raises(ContractError, match="cash_phase_authority_formal_not_candidate"):
        resolve_active_cash_phase_policy(
            [promoted],
            authority_manifest=_manifest(
                promoted,
                authority_scope=CANDIDATE_AUTHORITY_SCOPE,
            ),
            station_id="TEST",
            observed_at_utc=OBSERVED,
            required_authority_scope=CANDIDATE_AUTHORITY_SCOPE,
        )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"timezone_name": "Asia/Seoul"}, "cash_phase_authority_timezone_mismatch"),
        ({"valid_from": "2026-02-01T00:00:00Z"}, "cash_phase_authority_validity_mismatch"),
        ({"valid_until": "2026-12-31T00:00:00Z"}, "cash_phase_authority_validity_mismatch"),
    ],
)
def test_manifest_binding_mismatch_fails_closed(kwargs: dict[str, str], reason: str) -> None:
    policy = _policy("cash-v1", promoted=False)
    with pytest.raises(ContractError, match=reason):
        resolve_active_cash_phase_policy(
            [policy],
            authority_manifest=_manifest(
                policy,
                authority_scope=CANDIDATE_AUTHORITY_SCOPE,
                **kwargs,
            ),
            station_id="TEST",
            observed_at_utc=OBSERVED,
            required_authority_scope=CANDIDATE_AUTHORITY_SCOPE,
        )


def test_manifest_scope_and_validity_are_hard_gates() -> None:
    policy = _policy("cash-v1", promoted=False)
    with pytest.raises(ContractError, match="cash_phase_authority_scope_mismatch"):
        resolve_active_cash_phase_policy(
            [policy],
            authority_manifest=_manifest(
                policy,
                authority_scope=FORMAL_AUTHORITY_SCOPE,
            ),
            station_id="TEST",
            observed_at_utc=OBSERVED,
            required_authority_scope=CANDIDATE_AUTHORITY_SCOPE,
        )
    with pytest.raises(ContractError, match="cash_phase_authority_validity_mismatch"):
        resolve_active_cash_phase_policy(
            [policy],
            authority_manifest=_manifest(
                policy, authority_scope=CANDIDATE_AUTHORITY_SCOPE
            ),
            station_id="TEST",
            observed_at_utc="2027-01-01T00:00:00Z",
            required_authority_scope=CANDIDATE_AUTHORITY_SCOPE,
        )


def test_resident_path_uses_exact_manifest_instead_of_row_heuristics() -> None:
    policy = _policy("cash-v1", promoted=False)
    newer = _policy("cash-v2", promoted=False)
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE basket_cash_phase_policy_artifact("
            "policy_version TEXT,station_id TEXT,station_timezone TEXT,"
            "heavy_to_standard_local_time TEXT,standard_to_tail_local_time TEXT,"
            "cash_by_phase_json TEXT,valid_from_utc TEXT,valid_until_utc TEXT,"
            "evidence_status TEXT,promoted INTEGER,content_sha256 TEXT)"
        )
        for item in (policy, newer):
            conn.execute(
                "INSERT INTO basket_cash_phase_policy_artifact VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item.policy_version,
                    item.station_id,
                    item.station_timezone,
                    item.heavy_to_standard_local_time,
                    item.standard_to_tail_local_time,
                    '{"HEAVY":3.0,"STANDARD":2.0,"TAIL":1.0}',
                    item.valid_from_utc,
                    item.valid_until_utc,
                    item.evidence_status,
                    int(item.promoted),
                    item.content_sha256,
                ),
            )
        selected = _policy_for_scope(
            conn,
            station_id="TEST",
            observed_at_utc=OBSERVED,
            policy_authority_scope=CANDIDATE_AUTHORITY_SCOPE,
            cash_phase_authority_manifest=_manifest(
                policy, authority_scope=CANDIDATE_AUTHORITY_SCOPE
            ),
        )
    assert selected.policy_version == "cash-v1"


def test_missing_manifest_fails_closed_before_policy_selection() -> None:
    policy = _policy("cash-v1")
    with pytest.raises(ContractError, match="cash_phase_authority_manifest_required"):
        resolve_active_cash_phase_policy(
            [policy],
            authority_manifest=None,
            station_id="TEST",
            observed_at_utc=OBSERVED,
            required_authority_scope=FORMAL_AUTHORITY_SCOPE,
        )


def test_persisted_manifest_loader_requires_one_exact_active_row() -> None:
    policy = _policy("cash-v1")
    manifest = _manifest(policy)
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE basket_cash_phase_authority_manifest("
            "manifest_sha256 TEXT,station_id TEXT,policy_version TEXT,"
            "artifact_sha256 TEXT,station_timezone TEXT,valid_from_utc TEXT,"
            "valid_until_utc TEXT,authority_scope TEXT)"
        )
        with pytest.raises(
            ContractError,
            match="cash_phase_authority_manifest_not_unique",
        ):
            load_cash_phase_authority_manifest(
                conn,
                station_id="TEST",
                observed_at_utc=OBSERVED,
                required_authority_scope=FORMAL_AUTHORITY_SCOPE,
            )
        values = manifest.to_dict()
        conn.execute(
            "INSERT INTO basket_cash_phase_authority_manifest VALUES"
            "(?,?,?,?,?,?,?,?)",
            (
                values["manifest_sha256"],
                values["station_id"],
                values["policy_version"],
                values["artifact_sha256"],
                values["station_timezone"],
                values["valid_from_utc"],
                values["valid_until_utc"],
                values["authority_scope"],
            ),
        )
        loaded = load_cash_phase_authority_manifest(
            conn,
            station_id="TEST",
            observed_at_utc=OBSERVED,
            required_authority_scope=FORMAL_AUTHORITY_SCOPE,
        )
        assert loaded == manifest


def test_removed_legacy_scope_is_rejected_and_formal_scope_fails_closed() -> None:
    promoted = _policy("cash-owned-v1", promoted=True)
    with pytest.raises(ContractError, match="cash_phase_authority_scope_invalid"):
        _manifest(promoted, authority_scope="LEGACY_SCOPE")
    with pytest.raises(
        ContractError,
        match="cash_phase_formal_oos_authority_schema_missing",
    ):
        resolve_active_cash_phase_policy(
            [promoted],
            authority_manifest=_manifest(promoted),
            station_id="TEST",
            observed_at_utc=OBSERVED,
            required_authority_scope=FORMAL_AUTHORITY_SCOPE,
        )

    candidate = _policy("cash-unowned-v1", promoted=False)
    assert resolve_active_cash_phase_policy(
        [candidate],
        authority_manifest=_manifest(
            candidate,
            authority_scope=CANDIDATE_AUTHORITY_SCOPE,
        ),
        station_id="TEST",
        observed_at_utc=OBSERVED,
        required_authority_scope=CANDIDATE_AUTHORITY_SCOPE,
    ) is candidate


def test_candidate_generation_is_strictly_half_open() -> None:
    policy = _policy(
        "cash-window-v1",
        valid_from="2026-07-27T12:00:00Z",
        valid_until="2026-07-27T13:00:00Z",
        promoted=False,
    )
    before = cash_phase_authority_candidates(
        [policy],
        station_id="TEST",
        station_timezone="UTC",
        observed_at_utc="2026-07-27T11:59:59.999999Z",
        observed_cash_phase="STANDARD",
    )
    at_start = cash_phase_authority_candidates(
        [policy],
        station_id="TEST",
        station_timezone="UTC",
        observed_at_utc="2026-07-27T12:00:00Z",
        observed_cash_phase="STANDARD",
    )
    at_end = cash_phase_authority_candidates(
        [policy],
        station_id="TEST",
        station_timezone="UTC",
        observed_at_utc="2026-07-27T13:00:00Z",
        observed_cash_phase="STANDARD",
    )
    assert before == ()
    assert len(at_start) == 1
    assert at_end == ()


@pytest.mark.parametrize("phase", [None, "", "UNKNOWN"])
def test_unknown_cash_phase_has_zero_candidates(phase: str | None) -> None:
    policy = _policy("cash-v1")
    assert cash_phase_authority_candidates(
        [policy],
        station_id="TEST",
        station_timezone="UTC",
        observed_at_utc=OBSERVED,
        observed_cash_phase=phase,
    ) == ()


def test_unobserved_cash_phase_has_zero_candidates() -> None:
    policy = BasketCashPhasePolicyArtifact.create(
        policy_version="cash-partial-v1",
        station_id="TEST",
        station_timezone="UTC",
        heavy_to_standard_local_time="12:00",
        standard_to_tail_local_time="18:00",
        cash_by_phase={"STANDARD": 2.0, "TAIL": 1.0},
        valid_from_utc=VALID_FROM,
        valid_until_utc=VALID_UNTIL,
        evidence_status="PROMOTED",
        promoted=True,
    )
    assert cash_phase_authority_candidates(
        [policy],
        station_id="TEST",
        station_timezone="UTC",
        observed_at_utc=OBSERVED,
        observed_cash_phase="HEAVY",
    ) == ()


def test_latent_policy_window_overlap_fails_closed() -> None:
    left = _policy(
        "cash-overlap-v1",
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2026-08-01T00:00:00Z",
        promoted=False,
    )
    right = _policy(
        "cash-overlap-v2",
        valid_from="2026-07-01T00:00:00Z",
        valid_until="2027-01-01T00:00:00Z",
        promoted=False,
    )
    with pytest.raises(
        ContractError,
        match="cash_phase_authority_policy_window_overlap",
    ):
        cash_phase_authority_candidates(
            [left, right],
            station_id="TEST",
            station_timezone="UTC",
            observed_at_utc=OBSERVED,
            observed_cash_phase="STANDARD",
            authority_scope=CANDIDATE_AUTHORITY_SCOPE,
        )


def test_adjacent_policy_windows_do_not_overlap() -> None:
    left = _policy(
        "cash-adjacent-v1",
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2026-07-27T12:00:00Z",
        promoted=False,
    )
    right = _policy(
        "cash-adjacent-v2",
        valid_from="2026-07-27T12:00:00Z",
        valid_until="2027-01-01T00:00:00Z",
        promoted=False,
    )
    candidates = cash_phase_authority_candidates(
        [left, right],
        station_id="TEST",
        station_timezone="UTC",
        observed_at_utc=OBSERVED,
        observed_cash_phase="STANDARD",
        authority_scope=CANDIDATE_AUTHORITY_SCOPE,
    )
    assert [item.policy_version for item in candidates] == ["cash-adjacent-v2"]
