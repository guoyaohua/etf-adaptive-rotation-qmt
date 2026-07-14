import pandas as pd
import pytest

from etf_rotation.backtest import Backtester
from etf_rotation.cli import _require_calendar_match
from etf_rotation.schedule import is_rebalance_date, rebalance_dates, scheduled_dates
from etf_rotation.backtest import calculate_metrics
from etf_rotation.schedule import compare_exchange_calendar, exchange_sessions


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


def test_staggered_schedule_uses_last_session_after_holiday_week_completes(config):
    params = dict(config.strategy)
    params.update(
        rebalance_schedule="staggered_weeks",
        rebalance_weekday=4,
        rebalance_calendar="XSHG",
    )
    calendar = pd.to_datetime([
        "2024-03-29", "2024-04-01", "2024-04-02", "2024-04-03"
    ])

    # The official exchange calendar already establishes that Thursday and
    # Friday are holidays, so Wednesday's completed close is actionable.
    assert rebalance_dates(calendar, params, completed_through="2024-04-03") == [
        pd.Timestamp("2024-03-29"), pd.Timestamp("2024-04-03")
    ]
    assert rebalance_dates(calendar, params, completed_through="2024-04-05") == [
        pd.Timestamp("2024-03-29"), pd.Timestamp("2024-04-03")
    ]


def test_staggered_schedule_does_not_treat_missing_symbol_bar_as_holiday(config):
    params = dict(config.strategy)
    params.update(
        rebalance_schedule="staggered_weeks",
        rebalance_weekday=4,
        rebalance_calendar="XSHG",
    )
    exchange_calendar = pd.to_datetime([
        "2024-03-22", "2024-03-25", "2024-03-26", "2024-03-27",
        "2024-03-28",
    ])

    # The usable common data ends Thursday, but an observed Friday session in
    # the union calendar proves this was a data gap, not an exchange holiday.
    assert scheduled_dates(
        exchange_calendar,
        "2024-03-28",
        params,
        completed_through="2024-03-31",
    ) == [pd.Timestamp("2024-03-22")]


def test_exchange_calendar_detects_a_missing_market_session():
    observed = pd.to_datetime(["2024-03-25", "2024-03-26", "2024-03-27", "2024-03-28"])

    result = compare_exchange_calendar(
        observed, "XSHG", completed_through="2024-03-29"
    )

    assert not result["passed"]
    assert result["missing_sessions"] == ["2024-03-29"]
    assert len(result["sessions_sha256"]) == 64
    assert result["library_version"]


def test_exchange_calendar_detects_a_completely_stale_latest_session():
    observed = pd.to_datetime(["2024-03-25", "2024-03-26", "2024-03-27", "2024-03-28", "2024-03-29"])

    result = compare_exchange_calendar(
        observed, "XSHG", completed_through="2024-04-01"
    )

    assert not result["passed"]
    assert result["missing_sessions"] == ["2024-04-01"]


def test_requested_backtest_end_rejects_a_stale_complete_market(config, trending_data):
    latest = max(frame.index.max() for frame in trending_data.values())
    next_session = exchange_sessions(
        "24/5", latest + pd.Timedelta(days=1), latest + pd.Timedelta(days=7)
    )[0]

    with pytest.raises(RuntimeError, match="missing"):
        _require_calendar_match(
            config, trending_data, next_session, "运行测试回测"
        )


def test_exchange_calendar_requires_library_coverage():
    with pytest.raises(ValueError, match="请升级 exchange-calendars"):
        exchange_sessions("XSHG", "2035-01-01", "2035-01-31")


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
