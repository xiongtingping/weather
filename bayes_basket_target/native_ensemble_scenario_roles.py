"""Pure selection of family-deduplicated native-ensemble scenario roles.

This module turns the *raw member bucket distributions* carried by a native
ensemble forward packet into three descriptive scenario roles:

``M``
    A cross-family Top-2 consensus bucket.  A family contributes at most one
    vote for a bucket, even when it has several products (for example IFS and
    AIFS, or ICON Global and ICON Seamless).

``H`` / ``L``
    A warm/cold competing scenario peak.  It must be a global mode of one
    product, be within two settlement layers of ``M``, and have non-zero
    adjacent support from another independent family.

The result is evidence about scenario structure only.  Raw member frequency
is deliberately never exposed as a P0 position or an execution quantity.
The functions have no I/O, mutate no caller-owned object, and produce a
content-addressed, deterministically ordered mapping.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import ContractError, content_sha256


SCHEMA = "native_ensemble_scenario_roles_v1"
METHOD_VERSION = "family_deduplicated_three_role_scenario_buckets_v1"
DEFAULT_MAX_ROLE_DISTANCE = 2

# The selector is intentionally bounded.  These limits are large enough for
# the current station-day product catalog but prevent an accidental packet or
# malformed fixture from creating an unbounded in-memory scan.
MAX_PRODUCTS = 128
MAX_BUCKETS = 256
MAX_MEMBERS_PER_PRODUCT = 512
MAX_LINEAGE_FIELDS = 64
MAX_LINEAGE_ITEMS = 128
MAX_LINEAGE_DEPTH = 3
EPSILON = 1e-12


def _required_text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"scenario_{name}_missing")
    return result


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"scenario_{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"scenario_{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"scenario_{name}_invalid")
    return result


def _bounded_json_value(value: Any, *, depth: int = 0) -> Any:
    """Copy a small JSON-shaped lineage value without retaining huge input."""

    if depth > MAX_LINEAGE_DEPTH:
        return str(value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        items = sorted(
            ((str(key), item) for key, item in value.items()),
            key=lambda row: row[0],
        )[:MAX_LINEAGE_FIELDS]
        return {
            key: _bounded_json_value(item, depth=depth + 1)
            for key, item in items
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            _bounded_json_value(item, depth=depth + 1)
            for item in list(value)[:MAX_LINEAGE_ITEMS]
        ]
    return str(value)


def _bounded_mapping(name: str, value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ContractError(f"scenario_{name}_invalid")
    return _bounded_json_value(value)


def _as_product_sequence(
    products: Sequence[Mapping[str, Any]] | None,
    systems: Sequence[Mapping[str, Any]] | None,
    forward_packet: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    chosen = products
    if chosen is not None and systems is not None:
        raise ContractError("scenario_products_and_systems_both_supplied")
    if chosen is None:
        chosen = systems
    packet = forward_packet
    if chosen is None and packet is not None:
        raw_systems = packet.get("systems")
        if isinstance(raw_systems, Sequence) and not isinstance(
            raw_systems, (str, bytes, bytearray)
        ):
            chosen = raw_systems
    if chosen is None or isinstance(chosen, (str, bytes, bytearray)):
        raise ContractError("scenario_products_missing")
    if not isinstance(chosen, Sequence) or len(chosen) == 0:
        raise ContractError("scenario_products_missing")
    if len(chosen) > MAX_PRODUCTS:
        raise ContractError("scenario_products_too_many")
    if any(not isinstance(item, Mapping) for item in chosen):
        raise ContractError("scenario_product_invalid")
    return list(chosen)


def _packet_value(
    forward_packet: Mapping[str, Any] | None,
    *keys: str,
) -> Any:
    if forward_packet is None:
        return None
    nested = forward_packet.get("packet")
    candidates = [forward_packet]
    if isinstance(nested, Mapping):
        candidates.append(nested)
    for candidate in candidates:
        for key in keys:
            if key in candidate and candidate[key] is not None:
                return candidate[key]
    return None


def _bucket_order(
    *,
    bucket_ordinals: Mapping[str, Any] | None,
    bucket_intervals: Mapping[str, Any] | None,
    probability_keys: Sequence[str],
) -> tuple[list[str], dict[str, int], dict[str, list[float | None]] | None]:
    ordinal_map: dict[str, int] = {}
    interval_map: dict[str, list[float | None]] | None = None
    if bucket_ordinals is not None:
        if not isinstance(bucket_ordinals, Mapping):
            raise ContractError("scenario_bucket_ordinals_invalid")
        if not bucket_ordinals or len(bucket_ordinals) > MAX_BUCKETS:
            raise ContractError("scenario_bucket_ordinals_invalid")
        for raw_bucket, raw_ordinal in bucket_ordinals.items():
            bucket = _required_text("bucket", raw_bucket)
            if isinstance(raw_ordinal, bool):
                raise ContractError("scenario_bucket_ordinal_invalid")
            try:
                ordinal = int(raw_ordinal)
            except (TypeError, ValueError) as exc:
                raise ContractError("scenario_bucket_ordinal_invalid") from exc
            if float(raw_ordinal) != float(ordinal) or ordinal < 0:
                raise ContractError("scenario_bucket_ordinal_invalid")
            if bucket in ordinal_map or ordinal in ordinal_map.values():
                raise ContractError("scenario_bucket_ordinal_duplicate")
            ordinal_map[bucket] = ordinal
    if bucket_intervals is not None:
        if not isinstance(bucket_intervals, Mapping):
            raise ContractError("scenario_bucket_intervals_invalid")
        if not bucket_intervals or len(bucket_intervals) > MAX_BUCKETS:
            raise ContractError("scenario_bucket_intervals_invalid")
        parsed: list[tuple[str, float | None, float | None]] = []
        for raw_bucket, raw_bounds in bucket_intervals.items():
            bucket = _required_text("bucket", raw_bucket)
            if (
                not isinstance(raw_bounds, Sequence)
                or isinstance(raw_bounds, (str, bytes, bytearray))
                or len(raw_bounds) != 2
            ):
                raise ContractError("scenario_bucket_interval_invalid")
            lower = (
                None
                if raw_bounds[0] is None
                else _finite("bucket_lower", raw_bounds[0])
            )
            upper = (
                None
                if raw_bounds[1] is None
                else _finite("bucket_upper", raw_bounds[1])
            )
            if lower is not None and upper is not None and lower >= upper:
                raise ContractError("scenario_bucket_interval_invalid")
            parsed.append((bucket, lower, upper))
        parsed.sort(
            key=lambda row: (
                float("-inf") if row[1] is None else row[1],
                float("inf") if row[2] is None else row[2],
                row[0],
            )
        )
        interval_map = {
            bucket: [lower, upper]
            for bucket, lower, upper in parsed
        }
        interval_order = [row[0] for row in parsed]
        if ordinal_map:
            if set(ordinal_map) != set(interval_order):
                raise ContractError("scenario_bucket_contract_mismatch")
            if sorted(ordinal_map, key=ordinal_map.get) != interval_order:
                raise ContractError("scenario_bucket_contract_mismatch")
        else:
            ordinal_map = {
                bucket: index for index, bucket in enumerate(interval_order)
            }
    if not ordinal_map:
        raise ContractError("scenario_bucket_order_missing")
    order = [
        bucket
        for bucket, _ordinal in sorted(
            ordinal_map.items(), key=lambda row: (row[1], row[0])
        )
    ]
    known = set(order)
    if set(probability_keys) - known:
        raise ContractError("scenario_probability_bucket_unknown")
    if not set(order).intersection(probability_keys):
        raise ContractError("scenario_probability_buckets_missing")
    return order, ordinal_map, interval_map


def _probabilities_from_members(
    product: Mapping[str, Any],
    *,
    order: Sequence[str],
    intervals: Mapping[str, Sequence[float | None]] | None,
) -> dict[str, float]:
    raw_members = product.get("members")
    if not isinstance(raw_members, Sequence) or isinstance(
        raw_members, (str, bytes, bytearray)
    ):
        raise ContractError("scenario_raw_members_missing")
    if not raw_members or len(raw_members) > MAX_MEMBERS_PER_PRODUCT:
        raise ContractError("scenario_raw_members_invalid")
    counts = {bucket: 0 for bucket in order}
    for member in raw_members:
        if not isinstance(member, Mapping):
            raise ContractError("scenario_raw_member_invalid")
        raw_bucket = member.get("bucket")
        if raw_bucket is None:
            raw_bucket = member.get("bucket_id")
        if raw_bucket is not None:
            bucket = _required_text("member_bucket", raw_bucket)
            if bucket not in counts:
                raise ContractError("scenario_member_bucket_unknown")
        else:
            if intervals is None:
                raise ContractError("scenario_member_intervals_missing")
            raw_value = member.get("raw_tmax_c")
            if raw_value is None:
                raw_value = member.get("tmax_c")
            value = _finite("member_tmax_c", raw_value)
            matches = [
                bucket
                for bucket, bounds in intervals.items()
                if (
                    (bounds[0] is None or value >= float(bounds[0]))
                    and (bounds[1] is None or value < float(bounds[1]))
                )
            ]
            if len(matches) != 1:
                raise ContractError("scenario_member_bucket_not_exhaustive")
            bucket = matches[0]
        counts[bucket] += 1
    total = float(len(raw_members))
    return {bucket: counts[bucket] / total for bucket in order}


def _raw_probabilities(
    product: Mapping[str, Any],
    *,
    order: Sequence[str],
    intervals: Mapping[str, Sequence[float | None]] | None,
) -> dict[str, float]:
    raw = product.get("raw_bucket_probabilities")
    if raw is None:
        raw = product.get("raw_probabilities")
    if raw is None:
        raw = product.get("bucket_probabilities")
    if raw is None and product.get("raw_bucket_counts") is not None:
        raw = product.get("raw_bucket_counts")
    if raw is None:
        return _probabilities_from_members(
            product, order=order, intervals=intervals
        )
    if not isinstance(raw, Mapping) or not raw or len(raw) > MAX_BUCKETS:
        raise ContractError("scenario_raw_probabilities_invalid")
    known = set(order)
    if set(str(key) for key in raw) - known:
        raise ContractError("scenario_probability_bucket_unknown")
    result: dict[str, float] = {}
    for bucket in order:
        value = raw.get(bucket, raw.get(str(bucket), 0.0))
        value = _finite("raw_bucket_probability", value)
        if value < 0.0:
            raise ContractError("scenario_raw_probability_negative")
        result[bucket] = value
    total = sum(result.values())
    if total <= 0.0:
        raise ContractError("scenario_raw_probability_total_invalid")
    # Normalizing only removes transport rounding/count-scale differences; it
    # does not calibrate or convert the member distribution into a position.
    return {bucket: result[bucket] / total for bucket in order}


def _product_id(product: Mapping[str, Any]) -> str:
    for key in ("ensemble_system_id", "system_id", "product_id", "model_id"):
        value = str(product.get(key) or "").strip()
        if value:
            return value
    raise ContractError("scenario_product_id_missing")


def _family_id(product: Mapping[str, Any]) -> str:
    for key in ("independence_family_id", "family_id", "model_family_id"):
        value = str(product.get(key) or "").strip()
        if value:
            return value
    raise ContractError("scenario_family_id_missing")


def _reliability(
    product: Mapping[str, Any],
    *,
    product_id: str,
    family_id: str,
    by_product: Mapping[str, Any] | None,
    by_family: Mapping[str, Any] | None,
    by_system: Mapping[str, Any] | None,
) -> float:
    raw: Any = None
    if by_product is not None and product_id in by_product:
        raw = by_product[product_id]
    elif by_system is not None and product_id in by_system:
        raw = by_system[product_id]
    elif by_family is not None and family_id in by_family:
        raw = by_family[family_id]
    else:
        for key in (
            "oos_reliability",
            "reliability_weight",
            "oos_reliability_weight",
        ):
            if product.get(key) is not None:
                raw = product[key]
                break
    value = 1.0 if raw is None else _finite("oos_reliability", raw)
    if not 0.0 <= value <= 1.0:
        raise ContractError("scenario_oos_reliability_invalid")
    return value


def _product_lineage(
    product: Mapping[str, Any],
    *,
    product_id: str,
    family_id: str,
    probabilities: Mapping[str, float],
) -> dict[str, Any]:
    lineage_raw = product.get("lineage")
    lineage = _bounded_mapping("product_lineage", lineage_raw)
    result: dict[str, Any] = {
        "ensemble_system_id": product_id,
        "independence_family_id": family_id,
        "source_class": str(product.get("source_class") or ""),
        "aggregator_or_republisher": bool(
            product.get("aggregator_or_republisher", False)
        ),
        "member_set_sha256": str(product.get("member_set_sha256") or ""),
        "source_content_sha256": str(
            product.get("content_sha256")
            or product.get("snapshot_content_sha256")
            or ""
        ),
        "raw_bucket_probability_sha256": content_sha256(
            {bucket: probabilities[bucket] for bucket in sorted(probabilities)}
        ),
    }
    if lineage is not None:
        result["upstream_lineage"] = lineage
    return result


def _center_buckets(
    *,
    p1_center_bucket: str | None,
    p0m_center_bucket: str | None,
    p0_center_bucket: str | None,
    center_buckets: Mapping[str, Any] | None,
    known: set[str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    supplied = {
        "P1": p1_center_bucket,
        "P0-M": p0m_center_bucket,
        "P0": p0_center_bucket,
    }
    if center_buckets is not None:
        if not isinstance(center_buckets, Mapping):
            raise ContractError("scenario_center_buckets_invalid")
        for key, value in center_buckets.items():
            supplied[str(key).strip().upper()] = value
    for stage, raw_bucket in supplied.items():
        if raw_bucket in (None, ""):
            continue
        bucket = _required_text("center_bucket", raw_bucket)
        if bucket not in known:
            raise ContractError("scenario_center_bucket_unknown")
        result[stage] = bucket
    return dict(sorted(result.items()))


def _center_distance(
    bucket: str,
    *,
    ordinals: Mapping[str, int],
    centers: Mapping[str, str],
) -> int:
    if not centers:
        return 0
    return min(
        abs(ordinals[bucket] - ordinals[center])
        for center in centers.values()
    )


def _local_probability(
    probabilities: Mapping[str, float],
    *,
    bucket: str,
    ordinals: Mapping[str, int],
) -> float:
    target = ordinals[bucket]
    return sum(
        value
        for candidate, value in probabilities.items()
        if abs(ordinals[candidate] - target) <= 1
    )


def _vote_choice(candidates: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    return min(
        candidates,
        key=lambda item: (
            int(item["rank"]),
            -float(item["probability"]),
            -float(item["local_probability"]),
            -float(item["oos_reliability"]),
            str(item["product_id"]),
        ),
    )


def _family_votes(
    products: Sequence[Mapping[str, Any]],
    *,
    order: Sequence[str],
    ordinals: Mapping[str, int],
) -> dict[str, list[dict[str, Any]]]:
    by_bucket: dict[str, list[dict[str, Any]]] = {bucket: [] for bucket in order}
    families = sorted({str(product["family_id"]) for product in products})
    for bucket in order:
        votes: list[dict[str, Any]] = []
        for family in families:
            candidates: list[dict[str, Any]] = []
            for product in products:
                if product["family_id"] != family:
                    continue
                ranking = product["ranking"]
                if bucket not in product["top2"]:
                    continue
                rank = ranking.index(bucket) + 1
                candidates.append({
                    "product_id": product["product_id"],
                    "family_id": family,
                    "bucket": bucket,
                    "rank": rank,
                    "probability": product["probabilities"][bucket],
                    "local_probability": _local_probability(
                        product["probabilities"],
                        bucket=bucket,
                        ordinals=ordinals,
                    ),
                    "oos_reliability": product["oos_reliability"],
                    "lineage": product["lineage"],
                })
            if candidates:
                votes.append(dict(_vote_choice(candidates)))
        by_bucket[bucket] = sorted(
            votes,
            key=lambda item: (str(item["family_id"]), str(item["product_id"])),
        )
    return by_bucket


def _consensus_role(
    *,
    bucket: str,
    votes: Sequence[Mapping[str, Any]],
    ordinals: Mapping[str, int],
    centers: Mapping[str, str],
) -> dict[str, Any]:
    family_ranks = sorted(int(vote["rank"]) for vote in votes)
    role_score = {
        "independent_family_count": len(votes),
        "family_rank_score": sum(3 - rank for rank in family_ranks),
        "family_ranks": family_ranks,
        "adjacent_probability_sum": sum(
            float(vote["local_probability"]) for vote in votes
        ),
        "oos_reliability_sum": sum(
            float(vote["oos_reliability"]) for vote in votes
        ),
        "center_distance_layers": _center_distance(
            bucket, ordinals=ordinals, centers=centers
        ),
    }
    source_products = [
        {
            "ensemble_system_id": str(vote["product_id"]),
            "independence_family_id": str(vote["family_id"]),
            "top2_rank": int(vote["rank"]),
            "raw_bucket_probability": float(vote["probability"]),
            "lineage": vote["lineage"],
        }
        for vote in votes
    ]
    source_products.sort(
        key=lambda item: (
            str(item["independence_family_id"]),
            str(item["ensemble_system_id"]),
        )
    )
    return {
        "role": "M",
        "bucket": bucket,
        "selection_reason": "at_least_two_independent_families_top2_consensus",
        "score": role_score,
        "supporting_families": sorted(
            {str(vote["family_id"]) for vote in votes}
        ),
        "supporting_products": source_products,
        "lineage": {
            "role": "M",
            "source_products": source_products,
            "supporting_families": sorted(
                {str(vote["family_id"]) for vote in votes}
            ),
        },
    }


def _cross_family_support(
    candidate: Mapping[str, Any],
    products: Sequence[Mapping[str, Any]],
    *,
    ordinals: Mapping[str, int],
) -> tuple[list[dict[str, Any]], float]:
    family_best: dict[str, dict[str, Any]] = {}
    candidate_family = str(candidate["family_id"])
    bucket = str(candidate["bucket"])
    for product in products:
        family = str(product["family_id"])
        if family == candidate_family:
            continue
        adjacent_probability = _local_probability(
            product["probabilities"], bucket=bucket, ordinals=ordinals
        )
        if adjacent_probability <= 0.0:
            continue
        support = {
            "ensemble_system_id": str(product["product_id"]),
            "independence_family_id": family,
            "adjacent_probability": adjacent_probability,
            "bucket_probability": float(product["probabilities"][bucket]),
            "lineage": product["lineage"],
        }
        previous = family_best.get(family)
        if previous is None or (
            -float(support["adjacent_probability"]),
            -float(support["bucket_probability"]),
            -float(product["oos_reliability"]),
            str(product["product_id"]),
        ) < (
            -float(previous["adjacent_probability"]),
            -float(previous["bucket_probability"]),
            -float(previous.get("oos_reliability", 1.0)),
            str(previous["ensemble_system_id"]),
        ):
            support["oos_reliability"] = product["oos_reliability"]
            family_best[family] = support
    supports = sorted(
        family_best.values(),
        key=lambda item: (
            str(item["independence_family_id"]),
            str(item["ensemble_system_id"]),
        ),
    )
    return supports, sum(
        float(item["adjacent_probability"]) for item in supports
    )


def _mode_candidates(
    products: Sequence[Mapping[str, Any]],
    *,
    mode_bucket: str,
    mode_probability: float,
    m_bucket: str,
    ordinals: Mapping[str, int],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    m_ordinal = ordinals[m_bucket]
    mode_ordinal = ordinals[mode_bucket]
    if mode_ordinal == m_ordinal:
        return result
    side = "warm" if mode_ordinal > m_ordinal else "cold"
    distance = abs(mode_ordinal - m_ordinal)
    for product in products:
        if mode_bucket not in product["modes"]:
            continue
        result.append({
            "bucket": mode_bucket,
            "side": side,
            "distance_layers": distance,
            "mode_probability": mode_probability,
            "product_id": product["product_id"],
            "family_id": product["family_id"],
            "oos_reliability": product["oos_reliability"],
            "lineage": product["lineage"],
        })
    return result


def _tail_sort_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        -float(candidate["tail_score"]),
        -int(candidate["cross_family_support_count"]),
        -float(candidate["cross_family_adjacent_probability"]),
        -float(candidate["oos_reliability"]),
        -float(candidate["mode_probability"]),
        int(candidate["distance_layers"]),
        str(candidate["product_id"]),
        int(candidate["bucket_ordinal"]),
        str(candidate["bucket"]),
    )


def _tail_role(
    candidate: Mapping[str, Any],
    *,
    role: str,
) -> dict[str, Any]:
    return {
        "role": role,
        "bucket": str(candidate["bucket"]),
        "selection_reason": (
            "global_mode_with_cross_family_adjacent_support"
        ),
        "score": {
            "raw_mode_probability": float(candidate["mode_probability"]),
            "oos_reliability": float(candidate["oos_reliability"]),
            "cross_family_support_count": int(
                candidate["cross_family_support_count"]
            ),
            "cross_family_adjacent_probability": float(
                candidate["cross_family_adjacent_probability"]
            ),
            "distance_layers": int(candidate["distance_layers"]),
            "distance_factor": float(candidate["distance_factor"]),
            "tail_score": float(candidate["tail_score"]),
        },
        "source_product": {
            "ensemble_system_id": str(candidate["product_id"]),
            "independence_family_id": str(candidate["family_id"]),
            "lineage": candidate["lineage"],
        },
        "supporting_families": list(candidate["supporting_families"]),
        "supporting_products": list(candidate["supporting_products"]),
        "lineage": {
            "role": role,
            "source_product": {
                "ensemble_system_id": str(candidate["product_id"]),
                "independence_family_id": str(candidate["family_id"]),
                "lineage": candidate["lineage"],
            },
            "supporting_families": list(candidate["supporting_families"]),
            "supporting_products": list(candidate["supporting_products"]),
        },
    }


def select_native_ensemble_scenario_roles(
    products: Sequence[Mapping[str, Any]] | None = None,
    bucket_ordinals: Mapping[str, Any] | None = None,
    *,
    systems: Sequence[Mapping[str, Any]] | None = None,
    forward_packet: Mapping[str, Any] | None = None,
    bucket_intervals: Mapping[str, Any] | None = None,
    station_id: str | None = None,
    event_date: str | None = None,
    settlement_timezone: str | None = None,
    p1_center_bucket: str | None = None,
    p0m_center_bucket: str | None = None,
    p0_center_bucket: str | None = None,
    p0_m_center_bucket: str | None = None,
    center_buckets: Mapping[str, Any] | None = None,
    oos_reliability_by_product: Mapping[str, Any] | None = None,
    oos_reliability_by_system: Mapping[str, Any] | None = None,
    oos_reliability_by_family: Mapping[str, Any] | None = None,
    max_role_distance: int = DEFAULT_MAX_ROLE_DISTANCE,
) -> dict[str, Any]:
    """Select ``L/M/H`` scenario roles from raw member distributions.

    ``products`` is the same shape as ``official_native_forward_packet``'s
    ``systems`` list.  Each product must provide either
    ``raw_bucket_probabilities`` or a bounded ``members`` list.  The latter
    uses ``raw_tmax_c`` plus ``bucket_intervals`` to construct member buckets.

    The function intentionally accepts a small ``forward_packet`` convenience
    input so callers can pass the existing packet shape without reshaping it.
    It never reads calibrated P0 probabilities, never computes position sizes,
    and never performs I/O.
    """

    if isinstance(max_role_distance, bool) or int(max_role_distance) != max_role_distance:
        raise ContractError("scenario_max_role_distance_invalid")
    max_distance = int(max_role_distance)
    if max_distance < 0 or max_distance > 2:
        raise ContractError("scenario_max_role_distance_invalid")
    if forward_packet is not None and not isinstance(forward_packet, Mapping):
        raise ContractError("scenario_forward_packet_invalid")

    if bucket_ordinals is None:
        packet_ordinals = _packet_value(
            forward_packet, "bucket_ordinals"
        )
        if packet_ordinals is None:
            envelope = _packet_value(
                forward_packet, "native_probability_envelope"
            )
            if isinstance(envelope, Mapping):
                packet_ordinals = envelope.get("bucket_ordinals")
        bucket_ordinals = packet_ordinals
    if bucket_intervals is None:
        bucket_intervals = _packet_value(forward_packet, "bucket_intervals")

    raw_products = _as_product_sequence(products, systems, forward_packet)
    probability_keys: list[str] = []
    for product in raw_products:
        raw = product.get("raw_bucket_probabilities")
        if raw is None:
            raw = product.get("raw_probabilities")
        if raw is None:
            raw = product.get("bucket_probabilities")
        if raw is None:
            raw = product.get("raw_bucket_counts")
        if isinstance(raw, Mapping):
            probability_keys.extend(str(key) for key in raw)
        elif isinstance(product.get("members"), Sequence):
            for member in product["members"]:
                if isinstance(member, Mapping):
                    bucket = member.get("bucket", member.get("bucket_id"))
                    if bucket is not None:
                        probability_keys.append(str(bucket))
    if not probability_keys and bucket_intervals is not None:
        probability_keys = [str(key) for key in bucket_intervals]
    order, ordinals, intervals = _bucket_order(
        bucket_ordinals=bucket_ordinals,
        bucket_intervals=bucket_intervals,
        probability_keys=probability_keys,
    )

    p1_center = p1_center_bucket
    p0m_center = p0m_center_bucket or p0_m_center_bucket
    p0_center = p0_center_bucket
    if p1_center is None:
        p1_center = _packet_value(forward_packet, "p1_center_bucket")
    if p0m_center is None:
        p0m_center = _packet_value(
            forward_packet, "p0m_center_bucket", "p0_m_center_bucket"
        )
    if p0_center is None:
        p0_center = _packet_value(forward_packet, "p0_center_bucket")
    centers = _center_buckets(
        p1_center_bucket=p1_center,
        p0m_center_bucket=p0m_center,
        p0_center_bucket=p0_center,
        center_buckets=center_buckets,
        known=set(order),
    )

    normalized: list[dict[str, Any]] = []
    seen_products: set[str] = set()
    for raw_product in raw_products:
        product_id = _product_id(raw_product)
        family_id = _family_id(raw_product)
        if product_id in seen_products:
            raise ContractError("scenario_product_id_duplicate")
        seen_products.add(product_id)
        probabilities = _raw_probabilities(
            raw_product, order=order, intervals=intervals
        )
        ranking = sorted(
            order,
            key=lambda bucket: (
                -probabilities[bucket],
                ordinals[bucket],
                bucket,
            ),
        )
        # A zero-mass bucket is not a meaningful "high-ranking" second
        # scenario.  This also prevents sparse/count-style inputs from
        # manufacturing consensus out of shared zeroes.
        ranking = [bucket for bucket in ranking if probabilities[bucket] > EPSILON]
        mode_probability = probabilities[ranking[0]]
        modes = [
            bucket
            for bucket in order
            if abs(probabilities[bucket] - mode_probability) <= EPSILON
        ]
        reliability = _reliability(
            raw_product,
            product_id=product_id,
            family_id=family_id,
            by_product=oos_reliability_by_product,
            by_family=oos_reliability_by_family,
            by_system=oos_reliability_by_system,
        )
        normalized.append({
            "product_id": product_id,
            "family_id": family_id,
            "probabilities": probabilities,
            "ranking": ranking,
            "top2": tuple(ranking[:2]),
            "modes": modes,
            "mode_probability": mode_probability,
            "oos_reliability": reliability,
            "lineage": _product_lineage(
                raw_product,
                product_id=product_id,
                family_id=family_id,
                probabilities=probabilities,
            ),
        })
    normalized.sort(key=lambda product: (product["product_id"], product["family_id"]))

    votes_by_bucket = _family_votes(
        normalized, order=order, ordinals=ordinals
    )
    consensus_candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for bucket in order:
        votes = votes_by_bucket[bucket]
        if len(votes) < 2:
            continue
        family_ranks = sorted(int(vote["rank"]) for vote in votes)
        role_score = _consensus_role(
            bucket=bucket,
            votes=votes,
            ordinals=ordinals,
            centers=centers,
        )["score"]
        consensus_candidates.append((
            (
                -len(votes),
                -int(role_score["family_rank_score"]),
                tuple(family_ranks),
                -float(role_score["adjacent_probability_sum"]),
                -float(role_score["oos_reliability_sum"]),
                int(role_score["center_distance_layers"]),
                -sum(float(vote["probability"]) for vote in votes),
                ordinals[bucket],
                bucket,
            ),
            _consensus_role(
                bucket=bucket,
                votes=votes,
                ordinals=ordinals,
                centers=centers,
            ),
        ))
    consensus_candidates.sort(key=lambda item: item[0])
    m_role = consensus_candidates[0][1] if consensus_candidates else None
    m_bucket = None if m_role is None else str(m_role["bucket"])

    tail_candidates: list[dict[str, Any]] = []
    if m_bucket is not None:
        for product in normalized:
            for mode_bucket in product["modes"]:
                mode_candidates = _mode_candidates(
                    [product],
                    mode_bucket=mode_bucket,
                    mode_probability=product["probabilities"][mode_bucket],
                    m_bucket=m_bucket,
                    ordinals=ordinals,
                )
                for candidate in mode_candidates:
                    supports, support_mass = _cross_family_support(
                        candidate, normalized, ordinals=ordinals
                    )
                    distance = int(candidate["distance_layers"])
                    cross_count = len(supports)
                    distance_factor = 1.0 / (1.0 + float(distance))
                    candidate.update({
                        "bucket_ordinal": ordinals[mode_bucket],
                        "cross_family_support_count": cross_count,
                        "cross_family_adjacent_probability": support_mass,
                        "supporting_families": [
                            str(item["independence_family_id"])
                            for item in supports
                        ],
                        "supporting_products": supports,
                        "distance_factor": distance_factor,
                        "tail_score": (
                            float(candidate["mode_probability"])
                            * float(candidate["oos_reliability"])
                            * float(cross_count)
                            * distance_factor
                        ),
                    })
                    tail_candidates.append(candidate)

    selected_by_side: dict[str, dict[str, Any] | None] = {
        "cold": None,
        "warm": None,
    }
    for side in ("cold", "warm"):
        eligible = [
            candidate
            for candidate in tail_candidates
            if candidate["side"] == side
            and int(candidate["distance_layers"]) <= max_distance
            and int(candidate["cross_family_support_count"]) >= 1
        ]
        # A family may expose several products/modes.  Pick one winner per
        # family before selecting the side winner, so family multiplicity can
        # never create a second tail vote.
        family_winners: dict[str, dict[str, Any]] = {}
        for candidate in eligible:
            family = str(candidate["family_id"])
            previous = family_winners.get(family)
            if previous is None or _tail_sort_key(candidate) < _tail_sort_key(previous):
                family_winners[family] = candidate
        if family_winners:
            selected_by_side[side] = min(
                family_winners.values(), key=_tail_sort_key
            )

    selected_candidate_ids = {
        (str(candidate["side"]), str(candidate["product_id"]), str(candidate["bucket"]))
        for candidate in selected_by_side.values()
        if candidate is not None
    }
    observations: list[dict[str, Any]] = []
    if m_bucket is not None:
        for candidate in tail_candidates:
            candidate_id = (
                str(candidate["side"]),
                str(candidate["product_id"]),
                str(candidate["bucket"]),
            )
            if candidate_id in selected_candidate_ids:
                continue
            reasons: list[str] = []
            if int(candidate["distance_layers"]) > max_distance:
                reasons.append("outside_max_role_distance")
            if int(candidate["cross_family_support_count"]) < 1:
                reasons.append("cross_family_adjacent_support_missing")
            if not reasons:
                reasons.append("lower_tail_score_than_selected_role")
            observations.append({
                "observation_role": (
                    "deep_cold" if candidate["side"] == "cold" else "deep_warm"
                ),
                "bucket": str(candidate["bucket"]),
                "side": str(candidate["side"]),
                "distance_layers": int(candidate["distance_layers"]),
                "mode_probability": float(candidate["mode_probability"]),
                "oos_reliability": float(candidate["oos_reliability"]),
                "cross_family_support_count": int(
                    candidate["cross_family_support_count"]
                ),
                "cross_family_adjacent_probability": float(
                    candidate["cross_family_adjacent_probability"]
                ),
                "tail_score": float(candidate["tail_score"]),
                "reasons": reasons,
                "source_product": {
                    "ensemble_system_id": str(candidate["product_id"]),
                    "independence_family_id": str(candidate["family_id"]),
                },
                "supporting_families": list(candidate["supporting_families"]),
                "supporting_products": list(candidate["supporting_products"]),
                "lineage": {
                    "observation_role": (
                        "deep_cold" if candidate["side"] == "cold" else "deep_warm"
                    ),
                    "source_product": {
                        "ensemble_system_id": str(candidate["product_id"]),
                        "independence_family_id": str(candidate["family_id"]),
                        "lineage": candidate["lineage"],
                    },
                    "supporting_products": list(candidate["supporting_products"]),
                },
            })
    observations.sort(
        key=lambda item: (
            0 if item["side"] == "cold" else 1,
            int(item["distance_layers"]),
            -float(item["tail_score"]),
            str(item["source_product"]["ensemble_system_id"]),
            str(item["bucket"]),
        )
    )

    l_candidate = selected_by_side["cold"]
    h_candidate = selected_by_side["warm"]
    l_role = None if l_candidate is None else _tail_role(l_candidate, role="L")
    h_role = None if h_candidate is None else _tail_role(h_candidate, role="H")
    identity = {
        "schema": SCHEMA,
        "method_version": METHOD_VERSION,
        "station_id": str(
            station_id
            if station_id is not None
            else _packet_value(forward_packet, "station_id") or ""
        ).strip().upper(),
        "event_date": str(
            event_date
            if event_date is not None
            else _packet_value(forward_packet, "event_date") or ""
        ).strip(),
        "settlement_timezone": str(
            settlement_timezone
            if settlement_timezone is not None
            else _packet_value(forward_packet, "settlement_timezone") or ""
        ).strip(),
        "bucket_ordinals": {
            bucket: ordinals[bucket] for bucket in order
        },
        "bucket_contract_sha256": content_sha256({
            "bucket_ordinals": {bucket: ordinals[bucket] for bucket in order},
            "bucket_intervals": intervals,
        }),
        "center_buckets": centers,
        "source_product_ids": [
            str(product["product_id"]) for product in normalized
        ],
        "source_family_ids": sorted(
            {str(product["family_id"]) for product in normalized}
        ),
        "source_product_lineage": [
            product["lineage"] for product in normalized
        ],
        "consensus_candidate_count": len(consensus_candidates),
        "m_bucket": m_role,
        "l_bucket": l_role,
        "h_bucket": h_role,
        "observations": observations,
        "raw_member_probability_is_not_p0_position": True,
        "position_authority": "P0/P0-M/P1_AND_EXECUTION_POLICY",
        "orders_submitted": 0,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }
    result = {
        **identity,
        "roles": {"L": l_role, "M": m_role, "H": h_role},
        "selected": {"L": l_role, "M": m_role, "H": h_role},
        "buckets": {
            "L": None if l_role is None else l_role["bucket"],
            "M": None if m_role is None else m_role["bucket"],
            "H": None if h_role is None else h_role["bucket"],
        },
        "observation_candidates": observations,
        "observations_by_side": {
            "deep_cold": [
                item for item in observations if item["observation_role"] == "deep_cold"
            ],
            "deep_warm": [
                item for item in observations if item["observation_role"] == "deep_warm"
            ],
        },
        "blockers": (
            []
            if m_role is not None
            else ["no_bucket_has_two_independent_family_top2_support"]
        ),
    }
    result["content_sha256"] = content_sha256(result)
    return result


# Keep the names readable to callers working at either the "build" or
# "select" layer.  They intentionally refer to the same pure implementation.
build_native_ensemble_scenario_roles = select_native_ensemble_scenario_roles
build_scenario_roles = select_native_ensemble_scenario_roles
select_scenario_roles = select_native_ensemble_scenario_roles


__all__ = [
    "DEFAULT_MAX_ROLE_DISTANCE",
    "METHOD_VERSION",
    "SCHEMA",
    "build_native_ensemble_scenario_roles",
    "build_scenario_roles",
    "select_native_ensemble_scenario_roles",
    "select_scenario_roles",
]
