"""The width receipt records the station scale it applied, not an open edge's tail scale.

The open-edge loop in ``apply_center_width_calibration`` once rebound ``scale``,
so every receipt with an open edge recorded the last tail scale under
``station_width_scale`` (ZUUU 0.0806, EGLC 7.8e-6 on 2026-09-25) while the
composed lambda had read the true scale.  The arithmetic was right.
"""
from __future__ import annotations

import pytest

from bayes_basket_target.center_width_calibration import apply_center_width_calibration, served_bucket_axis

INTERVALS = {"19 or below": (None, 19.5), "20": (19.5, 20.5), "21": (20.5, 21.5),
             "22": (21.5, 22.5), "23 or above": (22.5, None)}
ORDER = list(INTERVALS)
P = {"19 or below": 0.1, "20": 0.2, "21": 0.4, "22": 0.2, "23 or above": 0.1}


@pytest.mark.parametrize("scale", [1.0, 0.8, 1.2])
def test_the_receipt_records_the_station_scale_it_applied(scale):
    centers, edges = served_bucket_axis(INTERVALS, ORDER)
    _out, receipt = apply_center_width_calibration(
        P, delta_c=0.0, width_lambda=0.75, ordered_keys=ORDER, key_centers_c=centers,
        station_width_scale=scale, open_edge_bounds_c=edges,
    )
    assert receipt["station_width_scale"] == scale
    assert receipt["composed_width_lambda_unfloored"] == pytest.approx(0.75 * scale * receipt["sigma_floor_lift"])
    tails = receipt["open_edge_tail_scale_c"]
    assert tails["lower"] is not None and tails["upper"] is not None
    assert scale not in (tails["lower"], tails["upper"])
