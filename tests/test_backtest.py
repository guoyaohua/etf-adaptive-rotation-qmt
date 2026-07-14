import pandas as pd

from etf_rotation.backtest import Backtester
from etf_rotation.schedule import is_rebalance_date
from etf_rotation.backtest import calculate_metrics


def test_backtest_runs_with_costs_and_limits_exposure(config, trending_data):
    result = Backtester(config).run(trending_data)
    assert result.metrics["valid"]
    assert not result.equity.empty
    assert result.equity["gross_exposure"].max() <= config.strategy["max_gross_exposure"] + 0.01
    assert result.metrics["commission"] >= 0


def test_no_trade_band_reduces_small_rebalances(config, trending_data):
    result = Backtester(config).run(trending_data)
    if not result.fills.empty:
        assert set(result.fills["reason"]).issubset(
            {"scheduled_rebalance", "initial_stop", "trailing_stop", "gap_stop", "portfolio_circuit_breaker"}
        )


def test_month_end_schedule_only_emits_one_target_per_month(config, trending_data):
    strategy = dict(config.strategy)
    strategy["rebalance_schedule"] = "month_end"
    from dataclasses import replace
    result = Backtester(replace(config, strategy=strategy)).run(trending_data)
    if not result.targets.empty:
        dates = result.targets["date"].dt.to_period("M")
        assert not dates.duplicated().any()


def test_fixed_week_phase_is_stable_across_backtest_start(config):
    params = dict(config.strategy)
    params.update(rebalance_schedule="fixed_weeks", rebalance_interval_weeks=4, rebalance_phase_weeks=2)
    dates = [pd.Timestamp("2024-01-05") + pd.Timedelta(weeks=i) for i in range(12)]
    chosen = [date for date in dates if is_rebalance_date(date, params)]
    assert all((right - left).days == 28 for left, right in zip(chosen, chosen[1:]))


def test_annual_returns_include_year_boundary_gap():
    equity = pd.DataFrame(
        {"equity": [100.0, 110.0, 99.0, 108.0]},
        index=pd.to_datetime(["2023-01-03", "2023-12-29", "2024-01-02", "2024-12-31"]),
    )
    metrics = calculate_metrics(equity, pd.DataFrame(), 100.0)
    assert metrics["positive_year_ratio"] == 0.5


def test_missing_quote_does_not_break_backtest(config, trending_data):
    # A missing bar in one instrument must not create a synthetic execution.
    date = trending_data["GROWTH.SH"].index[-10]
    trending_data["GROWTH.SH"] = trending_data["GROWTH.SH"].drop(index=date)
    result = Backtester(config).run(trending_data)
    assert result.metrics["valid"]


def test_opening_gap_stop_precedes_rebalance_and_blocks_same_open_reentry(config, trending_data):
    data = {symbol: frame.copy() for symbol, frame in trending_data.items()}
    dates = data["GROWTH.SH"].index
    gap_date = next(date for date in dates[180:] if date.weekday() == 0)
    previous_close = float(data["GROWTH.SH"].loc[:gap_date, "close"].iloc[-2])
    gap_open = previous_close * 0.50
    data["GROWTH.SH"].loc[gap_date, "open"] = gap_open
    data["GROWTH.SH"].loc[gap_date, "low"] = gap_open * 0.99

    result = Backtester(config).run(data)
    day_fills = result.fills[
        (result.fills["date"] == gap_date) & (result.fills["symbol"] == "GROWTH.SH")
    ]

    assert (day_fills["reason"] == "gap_stop").any()
    assert not (day_fills["side"] == "BUY").any()
