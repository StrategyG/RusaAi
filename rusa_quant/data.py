"""Validated time-series input and chronological splitting."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def load_time_series(
    path: str | Path,
    date_col: str = "date",
    value_col: str = "close",
    min_observations: int = 60,
) -> pd.Series:
    """Load a CSV as a sorted numeric Series indexed by timezone-naive UTC timestamps."""
    frame = pd.read_csv(path)
    missing = {date_col, value_col} - set(frame.columns)
    if missing:
        raise ValueError(f"CSV is missing required columns: {sorted(missing)}")

    frame = frame[[date_col, value_col]].copy()
    frame[date_col] = pd.to_datetime(
        frame[date_col], errors="coerce", utc=True
    ).dt.tz_convert(None)
    frame[value_col] = pd.to_numeric(frame[value_col], errors="coerce")
    frame = frame.dropna().sort_values(date_col)
    frame = frame.groupby(date_col, as_index=False)[value_col].mean()

    values = frame[value_col].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("Time-series values must all be finite.")
    if len(frame) < min_observations:
        raise ValueError(
            f"At least {min_observations} valid observations are required; got {len(frame)}."
        )

    result = pd.Series(
        values,
        index=pd.DatetimeIndex(frame[date_col], name=date_col),
        name=value_col,
        dtype=float,
    )
    return result.sort_index()


def chronological_split(
    series: pd.Series,
    test_size: float = 0.2,
    min_train_size: int = 30,
) -> tuple[pd.Series, pd.Series]:
    """Split without shuffling so future observations never enter training."""
    if not 0 < test_size < 0.5:
        raise ValueError("test_size must be greater than 0 and less than 0.5.")
    test_count = max(1, int(np.ceil(len(series) * test_size)))
    split_at = len(series) - test_count
    if split_at < min_train_size:
        raise ValueError(
            f"Not enough data: need at least {min_train_size} training observations."
        )
    return series.iloc[:split_at].copy(), series.iloc[split_at:].copy()


def make_future_index(
    index: pd.DatetimeIndex,
    periods: int,
    freq: str | None = None,
) -> pd.DatetimeIndex:
    """Create future timestamps using inferred frequency or median time delta."""
    if periods < 1:
        raise ValueError("periods must be >= 1.")
    if len(index) < 2:
        raise ValueError("At least two timestamps are required to infer frequency.")

    inferred = freq or (pd.infer_freq(index) if len(index) >= 3 else None)
    if inferred:
        return pd.date_range(start=index[-1], periods=periods + 1, freq=inferred)[1:]

    deltas = index.to_series().diff().dropna()
    median_delta = deltas.median()
    if pd.isna(median_delta) or median_delta <= pd.Timedelta(0):
        raise ValueError("Could not infer a positive time interval from the index.")
    return pd.date_range(
        start=index[-1] + median_delta,
        periods=periods,
        freq=median_delta,
    )
