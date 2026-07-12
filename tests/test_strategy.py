import numpy as np

from etf_rotation.strategy import RegimeRotationStrategy


def test_risk_on_selects_positive_growth(config, trending_data):
    target = RegimeRotationStrategy(config).target(trending_data, max(trending_data["GROWTH.SH"].index))
    assert target.regime == "risk_on"
    assert "GROWTH.SH" in target.weights
    assert sum(target.weights.values()) <= config.strategy["max_gross_exposure"] + 1e-12
    assert all(weight <= config.strategy["max_asset_weight"] + 1e-12 for weight in target.weights.values())


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
