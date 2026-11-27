"""Central settings for racecard rate smoothing and component weights.

Predicted-odds beta smoothing and same-distance/draw 平滑加權率 components share
RATE_SMOOTH_PRIOR / RATE_SMOOTH_CONSTANT.

達標率 uses RATE_SMOOTH_CONSTANT only; its prior is
CONSTANT × field-mean raw attainment (computed per race, not hardcoded).

加權總勝出率 uses the four non-attainment component weights only.
平滑加權率 uses those four plus RATE_WEIGHT_ATTAINMENT.
"""

from __future__ import annotations

from hppv.schema import (
    RACECARD_DRAW_PLACE_RATE_COLUMN,
    RACECARD_DRAW_WIN_RATE_COLUMN,
    RACECARD_SAME_DISTANCE_PLACE_RATE_COLUMN,
    RACECARD_SAME_DISTANCE_WIN_RATE_COLUMN,
)

# Beta-like smoothing: (k + PRIOR) / (n + CONSTANT)
# Matches predicted-odds defaults: alpha=1, alpha+beta=10.
RATE_SMOOTH_PRIOR: float = 1.0
RATE_SMOOTH_CONSTANT: float = 10.0

# Component weights (同程勝 : 同程位 : 檔位勝 : 檔位上名) = 2:1:2:1
RATE_WEIGHT_SAME_DISTANCE_WIN: int = 2
RATE_WEIGHT_SAME_DISTANCE_PLACE: int = 1
RATE_WEIGHT_DRAW_WIN: int = 2
RATE_WEIGHT_DRAW_PLACE: int = 1
# Fifth component for 平滑加權率 only (W).
RATE_WEIGHT_ATTAINMENT: int = 1

RATE_COMPONENT_WEIGHTS: tuple[tuple[str, int], ...] = (
    (RACECARD_SAME_DISTANCE_WIN_RATE_COLUMN, RATE_WEIGHT_SAME_DISTANCE_WIN),
    (RACECARD_SAME_DISTANCE_PLACE_RATE_COLUMN, RATE_WEIGHT_SAME_DISTANCE_PLACE),
    (RACECARD_DRAW_WIN_RATE_COLUMN, RATE_WEIGHT_DRAW_WIN),
    (RACECARD_DRAW_PLACE_RATE_COLUMN, RATE_WEIGHT_DRAW_PLACE),
)

RATE_COMPONENT_WEIGHT_TOTAL: int = sum(w for _c, w in RATE_COMPONENT_WEIGHTS)
RATE_SMOOTHED_WEIGHT_TOTAL: int = (
    RATE_COMPONENT_WEIGHT_TOTAL + int(RATE_WEIGHT_ATTAINMENT)
)
