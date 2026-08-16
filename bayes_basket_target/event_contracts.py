"""Strict target-only contracts for weak forecast and canonical observation events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import (
    MAX_REPORT_TRANSMISSION_LEAD_SECONDS,
    ContractError,
    content_sha256,
)


_MAX_TRANSMISSION_LEAD = timedelta(
    seconds=MAX_REPORT_TRANSMISSION_LEAD_SECONDS
)


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def _date(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        datetime.strptime(result, "%Y-%m-%d")
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid")
    return result


def _timezone(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        ZoneInfo(result)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    return result


def _strict_fields(raw: Mapping[str, Any], *, allowed: set[str], contract: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ContractError(
            f"{contract}_unknown_fields:" + ",".join(sorted(str(item) for item in unknown))
        )


def _parsed(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class ForecastRevision:
    revision_id: str
    station_id: str
    event_date: str
    revision_kind: str
    sequence: int
    issued_at_utc: str
    received_at_utc: str
    valid_from_utc: str
    valid_until_utc: str
    source_object_sha256: str
    supersedes_revision_id: str | None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ForecastRevision":
        allowed = {
            "schema",
            "revision_id",
            "station_id",
            "event_date",
            "revision_kind",
            "sequence",
            "issued_at_utc",
            "received_at_utc",
            "valid_from_utc",
            "valid_until_utc",
            "source_object_sha256",
            "supersedes_revision_id",
        }
        _strict_fields(raw, allowed=allowed, contract="forecast_revision")
        if raw.get("schema") not in {None, "bayes_basket_forecast_revision_v1"}:
            raise ContractError("forecast_revision_schema_invalid")
        kind = _text("revision_kind", raw.get("revision_kind")).upper()
        if kind not in {"TAF", "AMD", "COR"}:
            raise ContractError("forecast_revision_kind_invalid")
        try:
            sequence = int(raw.get("sequence"))
        except (TypeError, ValueError) as exc:
            raise ContractError("forecast_revision_sequence_invalid") from exc
        if sequence < 1:
            raise ContractError("forecast_revision_sequence_invalid")
        supersedes = str(raw.get("supersedes_revision_id") or "").strip() or None
        issued = _timestamp("issued_at_utc", raw.get("issued_at_utc"))
        received = _timestamp("received_at_utc", raw.get("received_at_utc"))
        valid_from = _timestamp("valid_from_utc", raw.get("valid_from_utc"))
        valid_until = _timestamp("valid_until_utc", raw.get("valid_until_utc"))
        if _parsed(issued) > _parsed(received):
            raise ContractError("forecast_revision_received_before_issue")
        if _parsed(valid_from) >= _parsed(valid_until):
            raise ContractError("forecast_revision_valid_window_invalid")
        if sequence == 1 and (kind != "TAF" or supersedes is not None):
            raise ContractError("forecast_revision_root_invalid")
        if sequence >= 2 and supersedes is None:
            raise ContractError("forecast_revision_supersession_required")
        return cls(
            revision_id=_text("revision_id", raw.get("revision_id")),
            station_id=_text("station_id", raw.get("station_id")).upper(),
            event_date=_date("event_date", raw.get("event_date")),
            revision_kind=kind,
            sequence=sequence,
            issued_at_utc=issued,
            received_at_utc=received,
            valid_from_utc=valid_from,
            valid_until_utc=valid_until,
            source_object_sha256=_sha("source_object_sha256", raw.get("source_object_sha256")),
            supersedes_revision_id=supersedes,
        )

    @property
    def content_sha256(self) -> str:
        return content_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "bayes_basket_forecast_revision_v1",
            "revision_id": self.revision_id,
            "station_id": self.station_id,
            "event_date": self.event_date,
            "revision_kind": self.revision_kind,
            "sequence": self.sequence,
            "issued_at_utc": self.issued_at_utc,
            "received_at_utc": self.received_at_utc,
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
            "source_object_sha256": self.source_object_sha256,
            "supersedes_revision_id": self.supersedes_revision_id,
        }


def validate_forecast_revision_chain(
    revisions: Iterable[ForecastRevision | Mapping[str, Any]],
) -> tuple[ForecastRevision, ...]:
    rows = tuple(
        item if isinstance(item, ForecastRevision) else ForecastRevision.from_mapping(item)
        for item in revisions
    )
    if not rows:
        raise ContractError("forecast_revision_chain_empty")
    ordered = tuple(sorted(rows, key=lambda item: (item.sequence, item.revision_id)))
    if len({item.revision_id for item in ordered}) != len(ordered):
        raise ContractError("forecast_revision_id_duplicate")
    scope = {(item.station_id, item.event_date) for item in ordered}
    if len(scope) != 1:
        raise ContractError("forecast_revision_scope_mixed")
    if ordered[0].revision_kind != "TAF":
        raise ContractError("forecast_revision_root_missing")
    for index, item in enumerate(ordered):
        if item.sequence != index + 1:
            raise ContractError("forecast_revision_sequence_gap_or_duplicate")
        if index == 0:
            continue
        predecessor = ordered[index - 1]
        if item.supersedes_revision_id != predecessor.revision_id:
            raise ContractError("forecast_revision_not_linear_supersession")
        if _parsed(item.received_at_utc) < _parsed(predecessor.received_at_utc):
            raise ContractError("forecast_revision_arrival_rewind")
    return ordered


@dataclass(frozen=True)
class CanonicalObservationEvent:
    observation_id: str
    station_id: str
    event_date: str
    settlement_timezone: str
    source_lane: str
    report_type: str
    sequence: int
    observed_at_utc: str
    received_at_utc: str
    raw_sha256: str
    qc_state: str
    supersedes_observation_id: str | None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalObservationEvent":
        allowed = {
            "schema",
            "observation_id",
            "station_id",
            "event_date",
            "settlement_timezone",
            "source_lane",
            "report_type",
            "sequence",
            "observed_at_utc",
            "received_at_utc",
            "raw_sha256",
            "qc_state",
            "supersedes_observation_id",
        }
        _strict_fields(raw, allowed=allowed, contract="canonical_observation")
        if raw.get("schema") not in {
            None,
            "bayes_basket_canonical_observation_event_v2",
        }:
            raise ContractError("canonical_observation_schema_invalid")
        lane = _text("source_lane", raw.get("source_lane")).lower()
        if lane not in {"amsc", "awc", "asos_1min", "madis_hfmetar"}:
            raise ContractError("canonical_observation_source_lane_invalid")
        report_type = _text("report_type", raw.get("report_type")).upper()
        if report_type not in {"METAR", "SPECI", "ASOS_1MIN", "ASOS_HF"}:
            raise ContractError("canonical_observation_report_type_invalid")
        qc_state = _text("qc_state", raw.get("qc_state")).upper()
        if qc_state not in {"ACCEPTED", "REVIEW_REQUIRED"}:
            raise ContractError("canonical_observation_qc_state_invalid")
        try:
            sequence = int(raw.get("sequence"))
        except (TypeError, ValueError) as exc:
            raise ContractError("canonical_observation_sequence_invalid") from exc
        if sequence < 1:
            raise ContractError("canonical_observation_sequence_invalid")
        supersedes = str(raw.get("supersedes_observation_id") or "").strip() or None
        if supersedes is not None and qc_state != "REVIEW_REQUIRED":
            raise ContractError("canonical_observation_revision_requires_review")
        observed = _timestamp("observed_at_utc", raw.get("observed_at_utc"))
        received = _timestamp("received_at_utc", raw.get("received_at_utc"))
        # A routine report goes on the wire slightly before the minute it is
        # stamped for, so observed may lead received by a transmission
        # jitter.  Same allowance as the P1 reader and the settlement reader;
        # all three have to agree or whichever is strictest closes the
        # station-day for the other two.
        if (
            _parsed(observed) - _parsed(received)
            > _MAX_TRANSMISSION_LEAD
        ):
            raise ContractError("canonical_observation_received_before_observed")
        return cls(
            observation_id=_text("observation_id", raw.get("observation_id")),
            station_id=_text("station_id", raw.get("station_id")).upper(),
            event_date=_date("event_date", raw.get("event_date")),
            settlement_timezone=_timezone(
                "settlement_timezone", raw.get("settlement_timezone")
            ),
            source_lane=lane,
            report_type=report_type,
            sequence=sequence,
            observed_at_utc=observed,
            received_at_utc=received,
            raw_sha256=_sha("raw_sha256", raw.get("raw_sha256")),
            qc_state=qc_state,
            supersedes_observation_id=supersedes,
        )

    @property
    def content_sha256(self) -> str:
        return content_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "bayes_basket_canonical_observation_event_v2",
            "observation_id": self.observation_id,
            "station_id": self.station_id,
            "event_date": self.event_date,
            "settlement_timezone": self.settlement_timezone,
            "source_lane": self.source_lane,
            "report_type": self.report_type,
            "sequence": self.sequence,
            "observed_at_utc": self.observed_at_utc,
            "received_at_utc": self.received_at_utc,
            "raw_sha256": self.raw_sha256,
            "qc_state": self.qc_state,
            "supersedes_observation_id": self.supersedes_observation_id,
        }


def validate_canonical_observation_sequence(
    observations: Iterable[CanonicalObservationEvent | Mapping[str, Any]],
) -> tuple[CanonicalObservationEvent, ...]:
    rows = tuple(
        item
        if isinstance(item, CanonicalObservationEvent)
        else CanonicalObservationEvent.from_mapping(item)
        for item in observations
    )
    if not rows:
        raise ContractError("canonical_observation_sequence_empty")
    ordered = tuple(sorted(rows, key=lambda item: (item.sequence, item.observation_id)))
    if len({item.observation_id for item in ordered}) != len(ordered):
        raise ContractError("canonical_observation_id_duplicate")
    if len(
        {
            (item.station_id, item.event_date, item.settlement_timezone)
            for item in ordered
        }
    ) != 1:
        raise ContractError("canonical_observation_scope_mixed")
    for index, item in enumerate(ordered):
        if item.sequence != index + 1:
            raise ContractError("canonical_observation_sequence_gap_or_duplicate")
        if index == 0:
            if item.supersedes_observation_id is not None:
                raise ContractError("canonical_observation_first_supersedes_invalid")
            continue
        predecessor = ordered[index - 1]
        if _parsed(item.received_at_utc) < _parsed(predecessor.received_at_utc):
            raise ContractError("canonical_observation_arrival_rewind")
        if item.supersedes_observation_id is not None:
            if item.supersedes_observation_id != predecessor.observation_id:
                raise ContractError("canonical_observation_revision_not_linear")
            if item.qc_state != "REVIEW_REQUIRED":
                raise ContractError("canonical_observation_revision_requires_review")
    return ordered


__all__ = [
    "CanonicalObservationEvent",
    "ForecastRevision",
    "validate_canonical_observation_sequence",
    "validate_forecast_revision_chain",
]
