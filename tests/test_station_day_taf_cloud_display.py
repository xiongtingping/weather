"""Cloud-height labels must stay tied to the TAF the P0 actually referenced."""

import hashlib
import sqlite3

from bayes_basket_target.station_day_board import _taf_cloud_evidence, _taf_cloud_layers


def test_cloud_base_groups_and_unknowns_do_not_claim_total_cloud_cover():
    layers = _taf_cloud_layers("SCT050 BKN080 SCT250 SCT050 FEW/// VV003 FEW010CB")
    assert [l["base_band"] for l in layers] == [
        "below_2km", "2_to_6km", "at_or_above_6km", "unknown", "vertical_visibility", "below_2km",
    ]
    assert layers[0]["height_m_agl"] == 1524
    assert layers[0]["height_ft_agl"] == 5000
    assert layers[-1]["convective"] == "CB"
    assert layers[3]["height_m_agl"] is None
    assert all("cloud_cover_fraction" not in layer for layer in layers)
    assert _taf_cloud_layers("CAVOK NSC SKC") == []


def test_cloud_base_group_boundaries_are_in_metres_not_code_digits():
    layers = _taf_cloud_layers("SCT065 SCT066 SCT196 SCT197")
    assert [l["base_band"] for l in layers] == ["below_2km", "2_to_6km", "2_to_6km", "at_or_above_6km"]


def test_only_exact_revision_and_raw_hash_can_classify_the_witness():
    raw = "TAF ZUUU 070905Z 0712/0812 02003MPS 6000 SCT050 TX24/0807Z"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE calibration_forward_taf_capture (station_id,event_date,revision_id,content_sha256,source_class,source_object_sha256)")
    conn.execute("INSERT INTO calibration_forward_taf_capture VALUES(?,?,?,?,?,?)",
                 ("ZUUU", "2026-10-08", "bound-revision", "bound-capture", "official_forward_native_receipt", digest))
    witness = {"forecast_revision_id": "bound-revision", "capture_content_sha256": "bound-capture"}
    revision = {"station_id": "ZUUU", "event_date": "2026-10-08", "revision_id": "bound-revision", "source_object_sha256": digest}
    capture = {"forecast_revision": revision, "raw_taf": raw}
    inbox = {"captures": [capture]}
    result = _taf_cloud_evidence(conn, "ZUUU", "2026-10-08", witness, inbox)
    assert result["state"] == "matched" and result["layers"][0]["code"] == "SCT050"
    # A newer unrelated TAF, mismatched date, or altered raw text is not a
    # substitute for the report behind the 38% the P0 already recorded.
    wrong_revision = {**capture, "forecast_revision": {**revision, "revision_id": "newer-revision"}}
    assert _taf_cloud_evidence(conn, "ZUUU", "2026-10-08", witness, {"captures": [wrong_revision]})["state"] == "unavailable"
    assert _taf_cloud_evidence(conn, "ZUUU", "2026-10-09", witness, inbox)["state"] == "unavailable"
    assert _taf_cloud_evidence(conn, "ZUUU", "2026-10-08", witness, {"captures": [{**capture, "raw_taf": raw.replace("SCT050", "BKN250")}]})["state"] == "unavailable"
    assert _taf_cloud_evidence(conn, "ZUUU", "2026-10-08", {}, inbox)["layers"] == []
    assert _taf_cloud_evidence(conn, "ZUUU", "2026-10-08", witness, {})["state"] == "unavailable"
    conn.close()
