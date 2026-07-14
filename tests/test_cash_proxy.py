from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from etf_rotation.backtest import Backtester
from etf_rotation.cash_proxy import (
    constrain_cash_proxy_weights,
    is_cash_proxy_blackout,
    prepare_signal_data,
    signal_adjustment_audit,
)
from etf_rotation.config import Instrument
from etf_rotation.indicators import atr
from etf_rotation.runtime import evaluate_live_risk, new_ledger
from etf_rotation.strategy import RegimeRotationStrategy, TargetPortfolio


RISK = {
    "initial_stop_atr": 2.5,
    "trailing_activation_atr": 1.5,
    "trailing_stop_atr": 3.0,
    "minimum_stop_distance": 0.015,
    "hard_drawdown": 0.12,
    "hard_cooldown_days": 10,
    "daily_loss_limit": 0.02,
    "daily_loss_cooldown_days": 5,
}


def _enabled_config(config):
    settings = {
        "enabled": True,
        "symbol": "CASH.SH",
        "signal_mode": "par_reset",
        "reset_anchor_price": 100.0,
        "reset_return_threshold": -0.005,
        "reset_price_tolerance": 0.50,
        "reset_window_start": "12-27",
        "reset_window_end": "12-31",
        "blackout_start": "12-15",
        "blackout_end": "01-15",
    }
    strategy = dict(config.strategy)
    strategy.update(
        idle_cash_proxy_group="money_market",
        idle_cash_proxy_max_weight=0.30,
    )
    universe = (*config.universe, Instrument("CASH.SH", "现金代理", "cash", "money_market", True))
    return replace(config, universe=universe, strategy=strategy, cash_proxy=settings)


def _frame(close: np.ndarray, dates: pd.DatetimeIndex) -> pd.DataFrame:
    values = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "open": values * 0.999,
            "high": values * 1.001,
            "low": values * 0.998,
            "close": values,
            "volume": np.full(len(values), 10_000.0),
            "amount": np.full(len(values), 100_000_000.0),
        },
        index=dates,
    )


def test_cash_proxy_blackout_wraps_year_and_is_inclusive(config):
    enabled = _enabled_config(config)

    expected = {
        "2024-12-14": False,
        "2024-12-15": True,
        "2024-12-31": True,
        "2025-01-01": True,
        "2025-01-15": True,
        "2025-01-16": False,
    }

    assert {date: is_cash_proxy_blackout(enabled, date) for date in expected} == expected
    assert constrain_cash_proxy_weights(
        enabled, {"GROWTH.SH": 0.4, "CASH.SH": 0.3}, "2025-01-15"
    ) == {"GROWTH.SH": 0.4}


def test_signal_reconstruction_is_causal_and_preserves_raw_ohlc(config):
    enabled = _enabled_config(config)
    dates = pd.to_datetime(
        ["2024-12-26", "2024-12-27", "2024-12-30", "2024-12-31"]
    )
    raw = _frame(np.array([100.90, 101.00, 100.01, 100.02]), dates)
    original = raw.copy(deep=True)

    prepared = prepare_signal_data(enabled, {"CASH.SH": raw})["CASH.SH"]

    pd.testing.assert_frame_equal(raw, original)
    pd.testing.assert_frame_equal(prepared.loc[:, original.columns], original)
    expected_reset_return = (100.01 + (101.00 - 100.0)) / 101.00 - 1.0
    assert prepared["signal_close"].pct_change().loc["2024-12-30"] == pytest.approx(
        expected_reset_return
    )
    assert prepared["signal_close"].pct_change().loc["2024-12-30"] > -0.005

    audit = signal_adjustment_audit(enabled, {"CASH.SH": raw})
    assert audit["adjustments"] == [
        {
            "date": "2024-12-30",
            "raw_return": pytest.approx(100.01 / 101.00 - 1.0),
            "signal_return": pytest.approx(expected_reset_return),
            "estimated_distribution": pytest.approx(1.0),
        }
    ]

    prefix = prepare_signal_data(
        enabled, {"CASH.SH": raw.loc[:"2024-12-30"]}
    )["CASH.SH"]
    pd.testing.assert_series_equal(
        prefix["signal_close"], prepared.loc[:"2024-12-30", "signal_close"]
    )


def test_signal_reconstruction_rejects_unexpected_or_ambiguous_reset(config):
    enabled = _enabled_config(config)

    outside = _frame(
        np.array([101.0, 100.0]), pd.to_datetime(["2025-02-03", "2025-02-04"])
    )
    with pytest.raises(ValueError, match="复位窗口外出现异常价格复位"):
        prepare_signal_data(enabled, {"CASH.SH": outside})

    ambiguous = _frame(
        np.array([101.0, 99.0]), pd.to_datetime(["2024-12-27", "2024-12-30"])
    )
    with pytest.raises(ValueError, match="价格不符合面值重置约束"):
        prepare_signal_data(enabled, {"CASH.SH": ambiguous})

    with pytest.raises(ValueError, match="现金代理缺少必需行情"):
        prepare_signal_data(enabled, {})


def test_disabled_cash_proxy_is_equivalent_to_unmodified_signal_data(config, trending_data):
    prepared = prepare_signal_data(config, trending_data)

    assert prepared.keys() == trending_data.keys()
    assert all(prepared[symbol] is frame for symbol, frame in trending_data.items())
    assert signal_adjustment_audit(config, trending_data) == {"enabled": False}


def test_future_rows_cannot_change_cash_proxy_signal_or_target(config, trending_data):
    enabled = _enabled_config(config)
    data = {symbol: frame.copy() for symbol, frame in trending_data.items()}
    dates = data["GROWTH.SH"].index
    data["CASH.SH"] = _frame(np.linspace(100.0, 101.0, len(dates)), dates)
    decision_date = dates[-21]

    baseline_data = prepare_signal_data(enabled, data)
    baseline = RegimeRotationStrategy(enabled).target(baseline_data, decision_date)
    mutated = {symbol: frame.copy() for symbol, frame in data.items()}
    future = dates > decision_date
    mutated["CASH.SH"].loc[future, "close"] *= np.linspace(1.0, 1.02, future.sum())
    replay_data = prepare_signal_data(enabled, mutated)
    replay = RegimeRotationStrategy(enabled).target(replay_data, decision_date)

    pd.testing.assert_series_equal(
        baseline_data["CASH.SH"].loc[:decision_date, "signal_close"],
        replay_data["CASH.SH"].loc[:decision_date, "signal_close"],
    )
    assert replay.weights == baseline.weights
    assert replay.regime == baseline.regime


def test_cash_role_never_enters_primary_ranking_and_respects_proxy_cap(config, trending_data):
    enabled = _enabled_config(config)
    data = {symbol: frame.copy() for symbol, frame in trending_data.items()}
    dates = data["GROWTH.SH"].index
    data["CASH.SH"] = _frame(np.linspace(100.0, 110.0, len(dates)), dates)

    decision_date = pd.Timestamp("2022-12-14")
    target = RegimeRotationStrategy(enabled).target(data, decision_date)

    assert target.weights["CASH.SH"] <= 0.30 + 1e-12
    assert target.diagnostics["selected_count"] <= enabled.strategy["selection_count"]


def test_cash_proxy_atr_uses_raw_execution_ohlc(config):
    enabled = _enabled_config(config)
    dates = pd.bdate_range("2024-01-02", periods=80)
    raw = _frame(np.linspace(100.0, 101.0, len(dates)), dates)
    signal = np.linspace(100.0, 200.0, len(dates))
    raw["signal_close"] = signal

    asset = RegimeRotationStrategy(enabled)._asset_signal("CASH.SH", raw)

    assert asset.atr == pytest.approx(atr(raw, enabled.strategy["atr_lookback"]))


def test_stale_cash_proxy_sleeves_are_removed_inside_blackout(config):
    enabled = _enabled_config(config)
    strategy = RegimeRotationStrategy(enabled)
    target = TargetPortfolio(
        pd.Timestamp("2024-12-13"),
        "risk_off",
        {"CASH.SH": 0.30},
        {},
        {},
    )
    blackout_target = replace(target, decision_date=pd.Timestamp("2024-12-20"))

    aggregate = strategy.aggregate_targets([target, blackout_target], sleeve_count=4)

    assert aggregate.weights == {}
    assert aggregate.diagnostics["gross_exposure"] == 0.0


def test_live_blackout_exit_is_latched_and_does_not_change_cash(config):
    ledger = new_ledger("account", "TAG", 10_000)
    ledger.update(
        {
            "cash": 5_000.0,
            "peak_equity": 10_000.0,
            "previous_equity": 10_000.0,
            "positions": {
                "CASH.SH": {
                    "quantity": 50,
                    "average_cost": 100.0,
                    "atr_at_entry": 0.1,
                    "high_watermark": 100.0,
                }
            },
        }
    )

    ledger, exits, equity = evaluate_live_risk(
        ledger,
        {"CASH.SH": 100.0},
        RISK,
        "2024-12-16",
        {"CASH.SH": "cash_distribution_blackout"},
    )
    ledger, repeated, repeated_equity = evaluate_live_risk(
        ledger, {"CASH.SH": 100.0}, RISK, "2024-12-16"
    )

    assert exits == repeated == {"CASH.SH": "cash_distribution_blackout"}
    assert ledger["pending_risk_exit_dates"] == {"CASH.SH": "2024-12-16"}
    assert ledger["cash"] == 5_000.0
    assert equity == repeated_equity == 10_000.0


def test_blackout_exit_does_not_suppress_portfolio_circuit_breaker():
    ledger = new_ledger("account", "TAG", 10_000)
    position = {
        "quantity": 50,
        "average_cost": 100.0,
        "atr_at_entry": 20.0,
        "high_watermark": 100.0,
    }
    ledger.update(
        {
            "cash": 0.0,
            "peak_equity": 10_000.0,
            "previous_equity": 10_000.0,
            "positions": {"CASH.SH": dict(position), "RISK.SH": dict(position)},
        }
    )

    ledger, exits, equity = evaluate_live_risk(
        ledger,
        {"CASH.SH": 80.0, "RISK.SH": 80.0},
        RISK,
        "2024-12-16",
        {"CASH.SH": "cash_distribution_blackout"},
    )

    assert equity == 8_000.0
    assert exits == {
        "CASH.SH": "portfolio_hard_drawdown",
        "RISK.SH": "portfolio_hard_drawdown",
    }
    assert ledger["last_risk_trigger"] == "hard_drawdown"
    assert ledger["cooldown_remaining"] == RISK["hard_cooldown_days"]


def test_forced_cash_exit_does_not_suppress_portfolio_circuit_breaker(config):
    ledger = new_ledger("account", "TAG", 10_000)
    ledger.update(
        {
            "cash": 0.0,
            "peak_equity": 12_000.0,
            "previous_equity": 10_000.0,
            "positions": {
                "CASH.SH": {
                    "quantity": 100,
                    "average_cost": 100.0,
                    "atr_at_entry": 10.0,
                    "high_watermark": 100.0,
                }
            },
        }
    )

    ledger, exits, _ = evaluate_live_risk(
        ledger,
        {"CASH.SH": 100.0},
        RISK,
        "2024-12-16",
        {"CASH.SH": "cash_distribution_blackout"},
    )

    assert exits == {"CASH.SH": "portfolio_hard_drawdown"}
    assert ledger["cooldown_remaining"] == RISK["hard_cooldown_days"]
    assert ledger["last_risk_trigger"] == "hard_drawdown"


def test_backtest_exits_before_reset_and_never_credits_distribution(config):
    enabled = _enabled_config(config)
    dates = pd.bdate_range("2023-01-02", "2024-01-31")
    down = np.linspace(2.0, 1.0, len(dates))
    data = {
        "GROWTH.SH": _frame(down, dates),
        "ALT.SH": _frame(down * 0.9, dates),
        "BOND.SH": _frame(down * 0.8, dates),
    }
    reset_date = pd.Timestamp("2023-12-29")
    before_reset = dates < reset_date
    cash_close = np.empty(len(dates), dtype=float)
    cash_close[before_reset] = 100.0 + np.arange(before_reset.sum()) * 0.005
    cash_close[~before_reset] = 100.0 + np.arange((~before_reset).sum()) * 0.005
    data["CASH.SH"] = _frame(cash_close, dates)

    result = Backtester(enabled).run(data)
    cash_fills = result.fills[result.fills["symbol"] == "CASH.SH"]
    blackout_sells = cash_fills[
        cash_fills["reason"] == "cash_distribution_blackout"
    ]

    assert not blackout_sells.empty
    assert blackout_sells.iloc[0]["date"] == pd.Timestamp("2023-12-15")
    in_blackout = cash_fills["date"].map(
        lambda value: is_cash_proxy_blackout(enabled, value)
    )
    assert not (in_blackout & (cash_fills["side"] == "BUY")).any()
    assert result.equity.loc["2023-12-29", "cash"] == pytest.approx(
        result.equity.loc["2023-12-15", "cash"]
    )
    assert result.equity.loc["2023-12-29", "equity"] == pytest.approx(
        result.equity.loc["2023-12-15", "equity"]
    )
