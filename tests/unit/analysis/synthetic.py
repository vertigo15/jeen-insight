"""Synthetic series generators shared by the analysis tests."""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

import numpy as np
import pandas as pd

FREQ = {"day": "D", "week": "W-MON", "month": "MS"}
PERIOD = {"day": 7, "week": 52, "month": 12}


def seasonal_series(
    *,
    n: int,
    grain: str = "week",
    start: str = "2024-01-01",
    level: float = 100_000.0,
    slope: float = 300.0,
    amplitude: float = 20_000.0,
    noise: float = 3_000.0,
    seed: int = 7,
    anomalies: Sequence[int] = (),
    anomaly_factor: float = 1.4,
) -> Tuple[pd.DatetimeIndex, np.ndarray]:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    m = PERIOD[grain]
    y = level + slope * t + amplitude * np.sin(2 * np.pi * t / m) + rng.normal(0, noise, n)
    for i, a in enumerate(anomalies):
        y[a] *= anomaly_factor if i % 2 == 0 else 1.0 / anomaly_factor
    idx = pd.date_range(start, periods=n, freq=FREQ[grain])
    return idx, y


def white_noise(*, n: int, grain: str = "week", mean: float = 1000.0, sd: float = 50.0, seed: int = 1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq=FREQ[grain])
    return idx, rng.normal(mean, sd, n)


def to_payload(idx: Iterable[pd.Timestamp], y: Iterable[float], *, drop: Sequence[int] = ()) -> Dict:
    rows: List[Dict] = []
    for i, (d, v) in enumerate(zip(idx, y)):
        if i in drop:
            continue
        rows.append({"ts": pd.Timestamp(d).date().isoformat(), "value": float(v)})
    return {"columns": ["ts", "value"], "rows": rows}


def series_request(**over) -> Dict:
    base = {"table": "FactInternetSales", "date_column": "OrderDate", "measure_column": "Profit", "grain": "week"}
    base.update(over)
    return base
