import pytest
import pandas as pd

from etf_rotation.cli import _exclude_latched_risk_exits
from etf_rotation.execution import CostModel, round_lot, target_quantities
from etf_rotation.qmt import QmtBroker, build_order_plan, load_owned_positions
from etf_rotation.risk import PortfolioRiskController, PositionRiskState, StopEngine
from etf_rotation.strategy import TargetPortfolio


def test_cost_model_is_adverse_and_has_minimum_commission():
    model = CostModel(0.0001, 5, 0.0005)
    buy = model.fill("ETF.SH", "BUY", 100, 1.0, "test")
    sell = model.fill("ETF.SH", "SELL", 100, 1.0, "test")
    assert buy.fill_price > 1.0
    assert sell.fill_price < 1.0
    assert buy.commission == 5
    assert sell.commission == 5


def test_lot_rounding_and_target_quantities():
    assert round_lot(199, 100) == 100
    assert target_quantities({"ETF.SH": 0.5}, 100_000, {"ETF.SH": 2.0}, 100)["ETF.SH"] == 25_000


def test_gap_stop_uses_worse_open():
    engine = StopEngine(2.0, 1.5, 3.0)
    state = PositionRiskState(entry_price=10.0, atr_at_entry=0.2, high_watermark=10.0)
    price, reason = engine.exit_price(state, day_open=9.0, day_low=8.8)
    assert price == 9.0
    assert reason == "gap_stop"


def test_minimum_stop_distance_prevents_tiny_atr_stop():
    engine = StopEngine(2.5, 1.5, 3.0, minimum_stop_distance=0.015)
    state = PositionRiskState(entry_price=100.0, atr_at_entry=0.10, high_watermark=100.0)

    assert engine.stop_price(state) == pytest.approx(98.5)
    price, reason = engine.exit_price(state, day_open=99.0, day_low=98.4)
    assert price == pytest.approx(98.5)
    assert reason == "initial_stop"


def test_order_plan_sells_before_buys_and_does_not_oversell():
    plan = build_order_plan(
        {"NEW.SH": 0.4},
        {"OLD.SH": 500},
        {"NEW.SH": 2.0, "OLD.SH": 1.0},
        100_000,
        lot_size=100,
    )
    assert plan[0].side == "SELL"
    assert plan[0].quantity == 500
    assert plan[1].side == "BUY"


def test_live_confirmation_has_exact_phrase():
    assert QmtBroker.CONFIRMATION == "LIVE_ETF_RR"


def test_hard_drawdown_starts_new_risk_epoch():
    controller = PortfolioRiskController(100.0, 0.08, 0.5, 0.12, 2, 0.02, 1)
    controller.end_day(87.0)
    assert controller.state.liquidate_next_open
    assert controller.state.peak_equity == 87.0
    assert controller.begin_day()
    controller.end_day(87.0)
    assert not controller.state.liquidate_next_open


def test_missing_owned_position_ledger_is_empty(tmp_path):
    assert load_owned_positions(tmp_path / "missing.json") == {}


def test_manual_same_code_position_cannot_be_sold():
    plan = build_order_plan(
        {},
        {"ETF.SH": 1000},
        {"ETF.SH": 1.0},
        100_000,
        sellable_positions={"ETF.SH": 200},
    )
    assert len(plan) == 1
    assert plan[0].quantity == 200


def test_zero_sellable_quantity_blocks_sell():
    plan = build_order_plan(
        {}, {"ETF.SH": 1000}, {"ETF.SH": 1.0}, 100_000, sellable_positions={}
    )
    assert plan == []


def test_no_trade_band_never_blocks_complete_exit_or_new_entry():
    exits = build_order_plan(
        {}, {"OLD.SH": 100}, {"OLD.SH": 1.0}, 100_000,
        min_weight_change=0.01, sellable_positions={"OLD.SH": 100},
    )
    entries = build_order_plan(
        {"NEW.SH": 0.005}, {}, {"NEW.SH": 1.0}, 100_000,
        min_weight_change=0.01,
    )

    assert [(item.side, item.quantity) for item in exits] == [("SELL", 100)]
    assert [(item.side, item.quantity) for item in entries] == [("BUY", 500)]


def test_latched_live_risk_exit_cannot_be_reopened_by_weekly_target():
    target = TargetPortfolio(
        pd.Timestamp("2025-01-03"), "risk_on", {"EXIT.SH": 0.2, "KEEP.SH": 0.3}, {}, {}
    )

    filtered = _exclude_latched_risk_exits(target, {"EXIT.SH": "initial_stop"})

    assert filtered.weights == {"KEEP.SH": 0.3}
    assert filtered.diagnostics["live_risk_exits"] == {"EXIT.SH": "initial_stop"}
