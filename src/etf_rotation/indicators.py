from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def simple_return(prices: pd.Series, lookback: int, skip: int = 0) -> float:
    clean = prices.dropna()
    required = lookback + skip + 1
    if len(clean) < required:
        return math.nan
    end = -1 - skip if skip else -1
    start = -1 - skip - lookback
    base = float(clean.iloc[start])
    final = float(clean.iloc[end])
    if base <= 0:
        return math.nan
    return final / base - 1.0


def annualized_volatility(prices: pd.Series, lookback: int) -> float:
    returns = prices.pct_change(fill_method=None).dropna().tail(lookback)
    if len(returns) < max(10, lookback // 2):
        return math.nan
    value = float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS))
    return value if value > 1e-8 else math.nan


def moving_average(prices: pd.Series, lookback: int) -> float:
    clean = prices.dropna().tail(lookback)
    if len(clean) < lookback:
        return math.nan
    return float(clean.mean())


def ema_slope(prices: pd.Series, span: int, slope_days: int) -> float:
    clean = prices.dropna()
    if len(clean) < span + slope_days:
        return math.nan
    ema = clean.ewm(span=span, adjust=False).mean()
    previous = float(ema.iloc[-1 - slope_days])
    return float(ema.iloc[-1] / previous - 1.0) if previous > 0 else math.nan


def atr(frame: pd.DataFrame, lookback: int) -> float:
    required = {"high", "low", "close"}
    if not required.issubset(frame.columns):
        raise ValueError(f"ATR 缺少字段: {sorted(required.difference(frame.columns))}")
    previous_close = frame["close"].shift(1)
    ranges = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    values = ranges.dropna().tail(lookback)
    if len(values) < lookback:
        return math.nan
    return float(values.mean())


def portfolio_volatility(returns: pd.DataFrame, weights: pd.Series) -> float:
    symbols = [symbol for symbol in weights.index if symbol in returns.columns and weights[symbol] > 0]
    if not symbols:
        return 0.0
    clean = returns[symbols].dropna(how="any")
    if len(clean) < 10:
        variances = returns[symbols].var(ddof=1).fillna(0.0).to_numpy()
        daily = float(np.sqrt(np.sum(np.square(weights[symbols].to_numpy()) * variances)))
    else:
        covariance = clean.cov().to_numpy()
        vector = weights[symbols].to_numpy(dtype=float)
        daily = float(np.sqrt(max(vector @ covariance @ vector, 0.0)))
    return daily * math.sqrt(TRADING_DAYS)
