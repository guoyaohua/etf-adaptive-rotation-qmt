from dataclasses import replace

import numpy as np

from etf_rotation.strategy import RegimeRotationStrategy


def test_risk_on_selects_positive_growth(config, trending_data):
    target = RegimeRotationStrategy(config).target(trending_data, max(trending_data["GROWTH.SH"].index))
    assert target.regime == "risk_on"
    assert "GROWTH.SH" in target.weights
    assert sum(target.weights.values()) <= config.strategy["max_gross_exposure"] + 1e-12
    assert all(weight <= config.strategy["max_asset_weight"] + 1e-12 for weight in target.weights.values())
    assert target.diagnostics["strategy_version"] == "0.5.2"


def test_future_data_cannot_change_past_signal(config, trending_data):
    strategy = RegimeRotationStrategy(config)
    decision_date = trending_data["GROWTH.SH"].index[-21]
    baseline = strategy.target(trending_data, decision_date)
    mutated = {symbol: frame.copy() for symbol, frame in trending_data.items()}
    mutated["GROWTH.SH"].loc[mutated["GROWTH.SH"].index > decision_date, "close"] *= 100
    replay = strategy.target(mutated, decision_date)
    assert replay.weights == baseline.weights
    assert replay.regime == baseline.regime


def test_group_constraint_keeps_one_symbol(config, trending_data):
    raw = config.instrument_by_symbol
    assert raw["GROWTH.SH"].group != raw["ALT.SH"].group
    target = RegimeRotationStrategy(config).target(trending_data, max(trending_data["GROWTH.SH"].index))
    groups = [raw[symbol].group for symbol in target.weights]
    assert len(groups) == len(set(groups))


def test_staggered_sleeves_leave_uninitialized_capital_in_cash(config, trending_data):
    strategy = RegimeRotationStrategy(config)
    date = max(trending_data["GROWTH.SH"].index)
    target = strategy.target(trending_data, date)
    aggregate = strategy.aggregate_targets([target], sleeve_count=4)
    assert abs(sum(aggregate.weights.values()) - sum(target.weights.values()) / 4) < 1e-12


def test_score_volatility_exponent_controls_only_ranking_penalty(config, trending_data):
    full = RegimeRotationStrategy(config)._asset_signal("GROWTH.SH", trending_data["GROWTH.SH"])
    params = dict(config.strategy)
    params["score_volatility_exponent"] = 0.0
    unpenalized = RegimeRotationStrategy(replace(config, strategy=params))._asset_signal(
        "GROWTH.SH", trending_data["GROWTH.SH"]
    )

    assert unpenalized.momentum == full.momentum
    assert unpenalized.volatility == full.volatility
    assert unpenalized.score == unpenalized.momentum
    assert full.score != unpenalized.score


def test_idle_cash_proxy_adds_only_trending_positive_defensive_asset(config, trending_data):
    params = dict(config.strategy)
    params.update(idle_cash_proxy_group="bond", idle_cash_proxy_max_weight=0.30)
    strategy = RegimeRotationStrategy(replace(config, strategy=params))

    target = strategy.target(trending_data, max(trending_data["GROWTH.SH"].index))

    assert "BOND.SH" in target.weights
    assert target.weights["BOND.SH"] <= 0.30 + 1e-12
    assert sum(target.weights.values()) <= config.strategy["max_gross_exposure"] + 1e-12


def test_idle_cash_proxy_stays_cash_when_defensive_trend_is_negative(config, trending_data):
    data = {symbol: frame.copy() for symbol, frame in trending_data.items()}
    data["BOND.SH"].loc[:, "close"] = np.linspace(1.05, 0.80, len(data["BOND.SH"]))
    data["BOND.SH"].loc[:, "open"] = data["BOND.SH"]["close"] * 0.999
    data["BOND.SH"].loc[:, "high"] = data["BOND.SH"]["close"] * 1.01
    data["BOND.SH"].loc[:, "low"] = data["BOND.SH"]["close"] * 0.99
    params = dict(config.strategy)
    params.update(idle_cash_proxy_group="bond", idle_cash_proxy_max_weight=0.30)

    target = RegimeRotationStrategy(replace(config, strategy=params)).target(
        data, max(data["BOND.SH"].index)
    )

    assert "BOND.SH" not in target.weights
