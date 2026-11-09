"""Bollinger Bands indicator."""
from __future__ import annotations

import pandas as pd


def bollinger_bands(
    series: pd.Series,
    window: int = 20,
    num_std: float = 2.0,
) -> pd.DataFrame:
    """Calculate middle, upper, lower bands, and normalized band width."""
    if window < 2:
        raise ValueError("window must be >= 2.")
    if num_std <= 0:
        raise ValueError("num_std must be positive.")

    middle = series.rolling(window=window, min_periods=window).mean()
    deviation = series.rolling(window=window, min_periods=window).std(ddof=0)
    upper = middle + num_std * deviation
    lower = middle - num_std * deviation
    width = (upper - lower) / middle.where(middle != 0)

    return pd.DataFrame(
        {
            "BB_MIDDLE": middle,
            "BB_UPPER": upper,
            "BB_LOWER": lower,
            "BB_WIDTH": width,
        },
        index=series.index,
    )
