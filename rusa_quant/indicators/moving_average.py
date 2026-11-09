"""Simple moving-average indicators."""
from __future__ import annotations

import pandas as pd


def moving_averages(
    series: pd.Series,
    windows: tuple[int, ...] = (5, 10, 30),
) -> pd.DataFrame:
    """Return rolling simple moving averages; early rows remain NaN."""
    if not windows or any(not isinstance(window, int) or window < 1 for window in windows):
        raise ValueError("windows must contain positive integers.")
    result = pd.DataFrame(index=series.index)
    for window in windows:
        result[f"MA{window}"] = series.rolling(window=window, min_periods=window).mean()
    return result
