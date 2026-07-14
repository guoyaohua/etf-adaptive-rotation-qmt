from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from etf_rotation.config import load_config


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "configs" / "strategy.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["universe"] = [
        {"symbol": "GROWTH.SH", "name": "增长", "role": "growth", "group": "growth", "t0": True},
        {"symbol": "ALT.SH", "name": "替代", "role": "growth", "group": "alt", "t0": True},
        {"symbol": "BOND.SH", "name": "防守", "role": "defensive", "group": "bond", "t0": True},
    ]
    raw["strategy"].update(
        {
            "momentum_lookbacks": [10, 20, 40],
            "momentum_skip_days": 2,
            "growth_trend_sma": 50,
            "defensive_trend_sma": 30,
            "fast_ema": 20,
            "fast_slope_days": 10,
            "volatility_lookback": 20,
            "atr_lookback": 10,
            "correlation_lookback": 30,
            "liquidity_lookback": 10,
            "min_average_amount": 1,
            "selection_count": 2,
            "defensive_selection_count": 1,
            "idle_cash_proxy_group": "bond",
        }
    )
    raw["execution"]["initial_capital"] = 100000
    path = tmp_path / "strategy.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def config(config_path: Path):
    return load_config(config_path)


def market_frame(prices: np.ndarray, dates: pd.DatetimeIndex) -> pd.DataFrame:
    prices = np.asarray(prices, dtype=float)
    return pd.DataFrame(
        {
            "open": prices * 0.999,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": np.arange(len(prices)) + 10000,
            "amount": np.full(len(prices), 100_000_000.0),
        },
        index=dates,
    )


@pytest.fixture
def trending_data():
    dates = pd.bdate_range("2022-01-03", periods=260)
    growth = np.linspace(1.0, 1.8, len(dates))
    alt = np.linspace(1.0, 1.45, len(dates)) * (1 + 0.01 * np.sin(np.arange(len(dates))))
    bond = np.linspace(1.0, 1.05, len(dates))
    return {
        "GROWTH.SH": market_frame(growth, dates),
        "ALT.SH": market_frame(alt, dates),
        "BOND.SH": market_frame(bond, dates),
    }
