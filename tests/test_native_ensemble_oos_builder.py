from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.native_ensemble_oos_builder import (
    FORECAST_REDUCER,
    MODEL_INPUT_KIND,
    OBJECT_TIME_EVIDENCE_KIND,
    build_native_ensemble_contract_event_identity,
    build_native_ensemble_oos_event_inputs,
    validate_native_ensemble_oos_event_input,
)
from bayes_basket_target.native_ensemble_oos_contract import (
    EVENT_FIELDS,
    CanonicalNativeEnsembleOOSEventFact,
)


def _rehash(raw: dict[str, object]) -> None:
    raw["content_sha256"] = content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    )


def _packet(provider: str) -> dict[str, object]:
    if provider == "GEFS":
        schema = "noaa_gefs_official_historical_multistation_members_v2"
        capture_schema = "noaa_gefs_official_historical_station_members_v2"
        system_id = "NOAA_GEFS_05"
        family = "NOAA_GEFS"
        member_count = 31
        horizon_key = "forecast_hour"
        member_ids = ["control", *[f"perturbed_{value:02d}" for value in range(1, 31)]]
        roles = ["CONTROL", *["PERTURBED"] * 30]
    else:
        schema = "ecmwf_ifs_open_pf50_historical_multistation_members_v1"
        capture_schema = "ecmwf_ifs_open_pf50_historical_station_members_v1"
        system_id = "ECMWF_IFS_ENS_OPEN_PF50"
        family = "ECMWF"
        member_count = 50
        horizon_key = "forecast_step"
        member_ids = [f"perturbed_{value:02d}" for value in range(1, 51)]
        roles = ["PERTURBED"] * 50
    event_date = date(2026, 7, 2)
    run_init = datetime(2026, 6, 30, 18, tzinfo=timezone.utc)
    archive_query = "2026-08-05T00:00:00.000Z"
    horizons = (36, 39, 42, 45, 48, 51, 54, 57)
    captures: list[dict[str, object]] = []
    for station_offset, station_id in enumerate(("CYYZ", "RJTT", "RKSI")):
        members = [
            {
                "ensemble_system_id": system_id,
                "member_id": member_id,
                "member_role": role,
                "raw_tmax_c": 20.0 + station_offset + index / 10,
            }
            for index, (member_id, role) in enumerate(zip(member_ids, roles))
        ]
        receipts: list[dict[str, object]] = []
        for member_index, member_id in enumerate(member_ids):
            for horizon in horizons:
                receipt: dict[str, object] = {
                    horizon_key: horizon,
                    "member_id": member_id,
                    "object_key": f"native/{system_id}/{member_id}/{horizon}",
                    "message_sha256": content_sha256([member_id, horizon, "message"]),
                    "index_sha256": content_sha256([member_id, horizon, "index"]),
                    "last_modified": "Tue, 30 Jun 2026 22:%02d:00 GMT" % (member_index % 60),
                    "range_start": member_index * 1_000_000 + horizon,
                    "range_end": member_index * 1_000_000 + horizon + 10,
                }
                if provider == "GEFS":
                    receipt["index_line"] = f"index:{member_id}:{horizon}"
                else:
                    receipt["index_row_sha256"] = content_sha256(
                        [member_id, horizon, "index-row"]
                    )
                receipts.append(receipt)
        member_identity = [
            {
                "member_id": row["member_id"],
                "member_role": row["member_role"],
                "raw_tmax_c": row["raw_tmax_c"],
            }
            for row in members
        ]
        capture_identity: dict[str, object] = {
            "schema": capture_schema,
            "station_id": station_id,
            "event_date": event_date.isoformat(),
            "settlement_timezone": "America/New_York",
            "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
            "run_cycle": "18Z",
            "ensemble_system_id": system_id,
            "independence_family_id": family,
            "member_count": member_count,
            "members": members,
            "member_set_sha256": content_sha256(member_identity),
            "field_receipt_count": len(receipts),
            "field_receipt_set_sha256": content_sha256(receipts),
            "field_receipts": receipts,
            "archive_query_at_utc": archive_query,
            "source_class": "OFFICIAL_NATIVE_ARCHIVE",
            "native_run_init_known": True,
            "native_publication_at_known": False,
            "offline_chronological_oos_eligible": True,
            "training_only": True,
            "holdout_eligible": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        }
        captures.append({**capture_identity, "content_sha256": content_sha256(capture_identity)})
    packet_identity: dict[str, object] = {
        "schema": schema,
        "event_date": event_date.isoformat(),
        "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
        "run_cycle": "18Z",
        "ensemble_system_id": system_id,
        "independence_family_id": family,
        "station_count": len(captures),
        "station_ids": ["CYYZ", "RJTT", "RKSI"],
        "captures": captures,
        "archive_query_at_utc": archive_query,
        "training_only": True,
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**packet_identity, "content_sha256": content_sha256(packet_identity)}


class NativeEnsembleOOSBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.packet_directory = Path(self._temporary_directory.name).resolve()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _events(
        self, provider: str, packet: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        source = _packet(provider) if packet is None else packet
        path = self.packet_directory / f"{provider.lower()}-packet.json"
        path.write_text(
            json.dumps(source, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return build_native_ensemble_oos_event_inputs(
            source, source_packet_ref=path
        )

    def test_gefs_and_pf50_create_complete_recomputable_member_set_inputs(self) -> None:
        for provider, expected_members in (("GEFS", 31), ("PF50", 50)):
            with self.subTest(provider=provider):
                events = self._events(provider)
                self.assertEqual([event["station_id"] for event in events], ["CYYZ", "RJTT", "RKSI"])
                event = events[0]
                self.assertEqual(event["model_input_kind"], MODEL_INPUT_KIND)
                self.assertEqual(event["forecast_reducer"], FORECAST_REDUCER)
                self.assertEqual(event["settlement_timezone"], "America/New_York")
                self.assertEqual(event["lead_bucket"], "d2")
                self.assertEqual(event["run_cycle"], "18Z")
                self.assertEqual(event["member_count"], expected_members)
                self.assertEqual(event["expected_member_count"], expected_members)
                expected_product = {
                    "GEFS": ("GFS", "NOAA", "noaa_gefs_0p5", "NOAA"),
                    "PF50": (
                        "ECMWF", "ECMWF",
                        "ecmwf_ifs_ens_open_pf50", "ECMWF",
                    ),
                }[provider]
                self.assertEqual(
                    (
                        event["model_id"], event["provider_id"],
                        event["provider_product_id"], event["independence_family_id"],
                    ),
                    expected_product,
                )
                self.assertNotIn("raw_object_sha256", event)
                self.assertAlmostEqual(
                    event["forecast_value"],
                    sum(20.0 + index / 10 for index in range(expected_members)) / expected_members,
                )
                self.assertEqual(len(event["member_evidence"]), expected_members)
                self.assertTrue(all(row["field_receipt_count"] == 8 for row in event["member_evidence"]))
                self.assertTrue(all(
                    row["official_object_available_by_utc_evidence_kind"]
                    == OBJECT_TIME_EVIDENCE_KIND
                    for row in event["member_evidence"]
                ))
                self.assertEqual(
                    event["raw_message_vector_sha256"],
                    content_sha256(event["raw_message_sha256s"]),
                )
                self.assertEqual(
                    event["raw_object_id_vector_sha256"],
                    content_sha256(event["raw_object_ids"]),
                )
                self.assertTrue(event["raw_object_ids"])
                self.assertTrue(Path(event["source_packet_ref"]).is_absolute())
                self.assertEqual(
                    set(event["member_evidence_vector"][0]),
                    {
                        "member_id", "role", "value_c",
                        "source_member_evidence_sha256",
                        "field_receipt_manifest_sha256",
                    },
                )
                validate_native_ensemble_oos_event_input(event)

    def test_adapter_and_exact_truth_make_contract_ready_identity_without_object_sha_alias(self) -> None:
        event = self._events("GEFS")[0]
        identity = build_native_ensemble_contract_event_identity(
            event,
            {
                "station_id": "CYYZ",
                "event_date": "2026-07-02",
                "forecast_decision_at_utc": "2026-07-01T00:00:00.000Z",
                "canonical_truth_fact_content_sha256": "a" * 64,
                "truth_id": "awc:CYYZ:2026-07-02:r1",
                "truth_revision_no": 1,
                "truth_content_sha256": "b" * 64,
                "truth_value_c": 25.0,
                "station_day_end_utc": "2026-07-03T04:00:00.000Z",
                "truth_canonical_committed_at_utc": "2026-07-03T05:00:00.000Z",
            },
        )
        self.assertEqual(identity["model_id"], "GFS")
        self.assertEqual(identity["availability_evidence_kind"], OBJECT_TIME_EVIDENCE_KIND)
        self.assertEqual(identity["raw_object_ids"], event["raw_object_ids"])
        self.assertEqual(set(identity), EVENT_FIELDS)
        self.assertEqual(
            identity["adapter_event_input_content_sha256"],
            event["content_sha256"],
        )
        self.assertEqual(
            identity["archive_retrieved_at_utc"], event["archive_query_at_utc"]
        )
        self.assertEqual(
            CanonicalNativeEnsembleOOSEventFact.from_mapping(identity).raw,
            identity,
        )
        self.assertEqual(
            identity["content_sha256"],
            content_sha256({key: value for key, value in identity.items() if key != "content_sha256"}),
        )

    def test_incomplete_member_field_receipts_fail_closed(self) -> None:
        packet = _packet("GEFS")
        capture = packet["captures"][0]
        assert isinstance(capture, dict)
        receipts = capture["field_receipts"]
        assert isinstance(receipts, list)
        receipts.pop()
        capture["field_receipt_count"] = len(receipts)
        capture["field_receipt_set_sha256"] = content_sha256(receipts)
        _rehash(capture)
        _rehash(packet)
        with self.assertRaisesRegex(ContractError, "native_oos_capture_receipt_count"):
            self._events("GEFS", packet)

    def test_evidence_hash_is_tamper_evident_and_singular_object_field_is_rejected(self) -> None:
        event = self._events("PF50")[0]
        tampered = deepcopy(event)
        tampered["member_evidence"][0]["field_receipts"][0]["raw_object_id"] = "changed"
        with self.assertRaisesRegex(ContractError, "native_oos_event_content_sha_mismatch"):
            validate_native_ensemble_oos_event_input(tampered)

        singular = deepcopy(event)
        singular["raw_object_sha256"] = "0" * 64
        singular["content_sha256"] = content_sha256(
            {key: value for key, value in singular.items() if key != "content_sha256"}
        )
        with self.assertRaisesRegex(ContractError, "native_oos_event_singular_raw_object_forbidden"):
            validate_native_ensemble_oos_event_input(singular)


if __name__ == "__main__":
    unittest.main()


def test_new_artifacts_name_the_seat_rule_that_actually_ran() -> None:
    """The method string is the artifact's only human-readable rule claim.

    It carried "train_rmse_inverse_family_capped_v1" long after e1c05256
    promoted the minimum-variance seat (282/282 live scopes in the
    seat-weight journal), and the stale literal kept being read as the
    serving rule -- including by this session.  New artifacts write the
    rule that actually divided the weight; the repair lane keeps the old
    literal on purpose, because it reproduces pre-bridge identities whose
    content sha covers that exact string.
    """

    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    builder = (root / "bayes_basket_target" / "native_ensemble_oos.py"
               ).read_text(encoding="utf-8")
    # The live builder composes the prefix from the recorded rule...
    assert '"train_rmse_inverse_family_capped_v1::"\n' not in builder
    assert '.get(\n                            "weight_rule"\n' in builder
    assert '"seat_weight_rule_unrecorded"' in builder
    # ...and nothing serves off the prefix: transforms key on contained
    # tags, so renaming cannot change any served math.
    assert "in artifact_combination_method" in (
        root / "bayes_basket_target" / "native_ensemble_p0.py"
    ).read_text(encoding="utf-8")
    # The repair lane must keep the historical literal byte-for-byte.
    repair = (root / "scripts" / "plan_native_ensemble_activation_repair.py"
              ).read_text(encoding="utf-8")
    assert '"train_rmse_inverse_family_capped_v1::"' in repair
