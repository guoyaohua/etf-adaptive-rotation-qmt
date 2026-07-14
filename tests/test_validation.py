from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import numpy as np

from etf_rotation.backtest import Backtester
from etf_rotation.validation import (
    check_prefix_invariance,
    compare_backtest_prefix,
    market_data_fingerprint,
    rolling_window_metrics,
    run_robustness_validation,
)


def test_market_data_fingerprint_changes_with_a_bar(trending_data):
    changed = deepcopy(trending_data)
    original = market_data_fingerprint(trending_data)
    changed["GROWTH.SH"].iloc[-1, changed["GROWTH.SH"].columns.get_loc("close")] *= 1.01
    assert market_data_fingerprint(changed) != original


def test_backtest_is_prefix_invariant(config, trending_data):
    full = Backtester(config).run(trending_data)
    result = check_prefix_invariance(config, trending_data, full, prefix_ratio=0.70)
    assert result.passed, result.mismatches
    assert result.compared_equity_rows > 0
    assert result.compared_targets > 0


def test_prefix_comparison_detects_rewritten_history(config, trending_data):
    full = Backtester(config).run(trending_data)
    cutoff = full.equity.index[int(len(full.equity) * 0.70)]
    corrupted_equity = full.equity.loc[:cutoff].copy()
    corrupted_equity.iloc[-1, corrupted_equity.columns.get_loc("equity")] += 1.0
    corrupted = replace(full, equity=corrupted_equity)
    result = compare_backtest_prefix(full, corrupted, cutoff)
    assert not result.passed
    assert any(item.startswith("equity:") for item in result.mismatches)


def test_rolling_windows_use_rebased_equity(config, trending_data):
    result = Backtester(config).run(trending_data)
    windows = rolling_window_metrics(result, window_months=6, step_months=3)
    assert len(windows) >= 2
    assert all(item["start"] >= result.metrics["start"] for item in windows)
    assert all(np.isfinite(item["cagr"]) for item in windows)


def test_validation_harness_runs_multiple_costs(config, trending_data):
    report = run_robustness_validation(
        config,
        trending_data,
        cost_multipliers=(1.0, 2.0),
        rolling_window_months=6,
        rolling_step_months=3,
        minimum_rolling_windows=1,
        prefix_ratio=0.70,
    )
    assert set(report["cost_scenarios"]) == {"1x", "2x"}
    assert report["prefix_invariance"]["passed"]
    assert len(report["market_data_sha256"]) == 64
    assert len(report["code_sha256"]) == 64
