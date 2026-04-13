import unittest
from unittest import mock

import scan_weather_market as swm


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class SettledPositionStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_cache = dict(swm.POSITIONS_CACHE)
        self._orig_disabled = swm.ACCOUNT_REQUESTS_DISABLED
        self._orig_enabled = swm.POSITIONS_SYNC_ENABLED
        self._orig_url = swm.POSITIONS_API_URL
        self._orig_auth = swm.POSITIONS_API_AUTH
        self._orig_key = swm.POSITIONS_API_KEY

    def tearDown(self) -> None:
        swm.POSITIONS_CACHE.clear()
        swm.POSITIONS_CACHE.update(self._orig_cache)
        swm.ACCOUNT_REQUESTS_DISABLED = self._orig_disabled
        swm.POSITIONS_SYNC_ENABLED = self._orig_enabled
        swm.POSITIONS_API_URL = self._orig_url
        swm.POSITIONS_API_AUTH = self._orig_auth
        swm.POSITIONS_API_KEY = self._orig_key

    def test_fetch_positions_by_token_keeps_redeemable_metadata(self) -> None:
        swm.ACCOUNT_REQUESTS_DISABLED = False
        swm.POSITIONS_SYNC_ENABLED = True
        swm.POSITIONS_API_URL = "https://example.test/positions"
        swm.POSITIONS_API_AUTH = ""
        swm.POSITIONS_API_KEY = ""
        swm.POSITIONS_CACHE.clear()
        swm.POSITIONS_CACHE.update({"ts": 0.0, "updated_at": "", "error": "", "data": {}})

        payload = [
            {
                "asset": "tok_yes",
                "size": "2.5",
                "avgPrice": "0.44",
                "redeemable": True,
                "mergeable": False,
            }
        ]

        with mock.patch.object(swm, "_positions_api_url", return_value="https://example.test/positions"), mock.patch.object(
            swm, "requests_get", return_value=_StubResponse(payload)
        ):
            mapped, err = swm.fetch_positions_by_token(force=True)

        self.assertEqual(err, "")
        self.assertIn("tok_yes", mapped)
        self.assertAlmostEqual(float(mapped["tok_yes"]["size"] or 0.0), 2.5, places=6)
        self.assertAlmostEqual(float(mapped["tok_yes"]["avg_price"] or 0.0), 0.44, places=6)
        self.assertEqual(mapped["tok_yes"]["redeemable"], True)
        self.assertEqual(mapped["tok_yes"]["mergeable"], False)

    def test_classify_settled_position_status_splits_redeem_stage(self) -> None:
        self.assertEqual(
            swm._classify_settled_position_status({"redeemable": False}, claim_enabled=False),
            "settled_wait_redeemable",
        )
        self.assertEqual(
            swm._classify_settled_position_status({"redeemable": True}, claim_enabled=False),
            "settled_claim_ready",
        )
        self.assertEqual(
            swm._classify_settled_position_status({"redeemable": True}, claim_enabled=True),
            "settled_claim_pending",
        )
        self.assertEqual(
            swm._classify_settled_position_status({}, claim_enabled=False),
            "settled_position_open",
        )


if __name__ == "__main__":
    unittest.main()
