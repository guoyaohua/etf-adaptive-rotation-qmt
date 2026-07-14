from __future__ import annotations

import json

import pytest

from etf_rotation.runtime import (
    atomic_write_json,
    evaluate_live_risk,
    finalize_live_day,
    load_ledger,
    ledger_equity,
    mark_orders_from_remark,
    new_ledger,
    normalize_trade_timestamp,
    reconcile_ledger,
    runtime_lock,
    stable_plan_id,
    stable_risk_plan_id,
    update_monitor_heartbeat,
)


RISK = {
    "initial_stop_atr": 2.5,
    "trailing_activation_atr": 1.5,
    "trailing_stop_atr": 3.0,
    "minimum_stop_distance": 0.0,
    "hard_drawdown": 0.12,
    "hard_cooldown_days": 10,
    "daily_loss_limit": 0.02,
    "daily_loss_cooldown_days": 5,
}


def test_ledger_is_account_bound_without_storing_account_id(tmp_path):
    path = tmp_path / "state.json"
    ledger = new_ledger("sensitive-account-id", "UNIQUE_TAG", 100_000)
    atomic_write_json(path, ledger)

    loaded = load_ledger(path, "sensitive-account-id", "UNIQUE_TAG")

    assert loaded["initial_capital"] == 100_000
    assert "sensitive-account-id" not in path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="不属于当前 QMT 账户"):
        load_ledger(path, "another-account", "UNIQUE_TAG")
    with pytest.raises(ValueError, match="strategy_tag"):
        load_ledger(path, "sensitive-account-id", "ANOTHER_TAG")


def test_ledger_equity_marks_positions_to_market():
    ledger = new_ledger("account", "TAG", 1_000)
    ledger["cash"] = 200.0
    ledger["positions"] = {"ETF.SH": {"quantity": 100, "average_cost": 10.0}}

    assert ledger_equity(ledger, {"ETF.SH": 8.0}) == pytest.approx(1_000.0)


def test_runtime_lock_rejects_concurrent_owner(tmp_path):
    lock_path = tmp_path / "strategy.lock"
    with runtime_lock(lock_path):
        with pytest.raises(RuntimeError, match="另一个实盘/对账进程"):
            with runtime_lock(lock_path):
                pass


def test_reconcile_filters_history_and_is_idempotent():
    ledger = new_ledger("account", "TAG", 10_000)
    ledger["trade_baseline_at"] = "2025-01-02T10:00:00+08:00"
    trades = [
        {
            "trade_id": "old",
            "symbol": "ETF.SH",
            "side": "BUY",
            "quantity": 100,
            "price": 2.0,
            "traded_time": "2025-01-02T09:59:59+08:00",
        },
        {
            "trade_id": "new",
            "symbol": "ETF.SH",
            "side": "BUY",
            "quantity": 100,
            "price": 2.0,
            "traded_time": "2025-01-02T10:00:01+08:00",
        },
    ]

    ledger, applied = reconcile_ledger(ledger, trades, 0.0001, 5.0, {"ETF.SH": 0.1})
    ledger, applied_again = reconcile_ledger(ledger, trades, 0.0001, 5.0, {"ETF.SH": 0.1})

    assert applied == 1
    assert applied_again == 0
    assert ledger["positions"]["ETF.SH"]["quantity"] == 100
    assert ledger["positions"]["ETF.SH"]["atr_at_entry"] == 0.1
    assert ledger["cash"] == pytest.approx(9_795.0)
    assert ledger["processed_trade_ids"] == ["new"]


def test_reconcile_prefers_broker_reported_commission():
    ledger = new_ledger("account", "TAG", 10_000)
    ledger["trade_baseline_at"] = "2025-01-02T10:00:00+08:00"
    trade = {
        "trade_id": "broker-fee",
        "symbol": "ETF.SH",
        "side": "BUY",
        "quantity": 100,
        "price": 2.0,
        "amount": 200.0,
        "commission": 1.25,
        "traded_time": "2025-01-02T10:00:01+08:00",
    }

    ledger, applied = reconcile_ledger(ledger, [trade], 0.0001, 5.0, {"ETF.SH": 0.1})

    assert applied == 1
    assert ledger["cash"] == pytest.approx(9_798.75)


def test_reconcile_falls_back_when_broker_commission_is_zero():
    ledger = new_ledger("account", "TAG", 10_000)
    ledger["trade_baseline_at"] = "2025-01-02T10:00:00+08:00"
    trade = {
        "trade_id": "zero-fee", "symbol": "ETF.SH", "side": "BUY",
        "quantity": 100, "price": 2.0, "amount": 200.0, "commission": 0.0,
        "traded_time": "2025-01-02T10:00:01+08:00",
    }

    ledger, _ = reconcile_ledger(ledger, [trade], 0.0001, 5.0, {"ETF.SH": 0.1})

    assert ledger["cash"] == pytest.approx(9_795.0)


def test_reconcile_rejects_unparseable_trade_time_after_baseline():
    ledger = new_ledger("account", "TAG", 10_000)
    trade = {
        "trade_id": "unknown-time",
        "symbol": "ETF.SH",
        "side": "BUY",
        "quantity": 100,
        "price": 2.0,
        "traded_time": 0,
    }

    with pytest.raises(ValueError, match="成交时间"):
        reconcile_ledger(ledger, [trade], 0.0001, 5.0, {"ETF.SH": 0.1})


def test_qmt_compact_trade_time_is_normalized():
    assert normalize_trade_timestamp(20250102100001) == pytest.approx(1735783201.0)


def test_reconcile_rejects_buy_without_entry_atr():
    ledger = new_ledger("account", "TAG", 10_000)
    ledger["trade_baseline_at"] = "2025-01-02T10:00:00+08:00"
    trade = {
        "trade_id": "no-atr",
        "symbol": "ETF.SH",
        "side": "BUY",
        "quantity": 100,
        "price": 2.0,
        "traded_time": "2025-01-02T10:00:01+08:00",
    }

    with pytest.raises(ValueError, match="入场 ATR"):
        reconcile_ledger(ledger, [trade], 0.0001, 5.0)


def test_reconcile_add_on_prefers_existing_position_atr():
    ledger = new_ledger("account", "TAG", 10_000)
    ledger["trade_baseline_at"] = "2025-01-02T10:00:00+08:00"
    ledger["positions"] = {
        "ETF.SH": {"quantity": 100, "average_cost": 2.0, "atr_at_entry": 0.1, "high_watermark": 2.0}
    }
    trade = {
        "trade_id": "add-on", "symbol": "ETF.SH", "side": "BUY",
        "quantity": 100, "price": 2.1, "traded_time": "2025-01-02T10:00:01+08:00",
    }

    ledger, applied = reconcile_ledger(
        ledger, [trade], 0.0001, 5.0,
        position_state_by_symbol={"ETF.SH": {"atr": 0.8}},
        exit_state_by_symbol={"ETF.SH": {"atr": 0.1}},
    )

    assert applied == 1
    assert ledger["positions"]["ETF.SH"]["atr_at_entry"] == pytest.approx(0.1)


def test_plan_id_is_stable_across_mapping_and_order_sequence():
    first = stable_plan_id(
        "TAG",
        "2025-01-03",
        100_000,
        {"B.SH": 0.3, "A.SH": 0.4},
        [
            {"symbol": "B.SH", "side": "BUY", "quantity": 100},
            {"symbol": "A.SH", "side": "SELL", "quantity": 200},
        ],
    )
    second = stable_plan_id(
        "TAG",
        "2025-01-03",
        100_000.001,
        {"A.SH": 0.4, "B.SH": 0.3},
        [
            {"symbol": "A.SH", "side": "SELL", "quantity": 200},
            {"symbol": "B.SH", "side": "BUY", "quantity": 100},
        ],
    )

    assert first == second


def test_risk_plan_id_does_not_change_after_partial_fill():
    first = stable_risk_plan_id(
        "TAG", "2025-01-03",
        [{"symbol": "ETF.SH", "side": "SELL", "quantity": 1000, "reason": "initial_stop"}],
    )
    remaining = stable_risk_plan_id(
        "TAG", "2025-01-03",
        [{"symbol": "ETF.SH", "side": "SELL", "quantity": 400, "reason": "initial_stop"}],
    )

    assert first == remaining


def test_remark_recovery_distinguishes_partial_submission():
    ledger = new_ledger("account", "TAG", 10_000)
    orders = [
        {"symbol": "A.SH", "side": "BUY", "quantity": 100},
        {"symbol": "B.SH", "side": "BUY", "quantity": 100},
    ]

    ledger = mark_orders_from_remark(ledger, "p1", orders, {"RR:p1": [123]}, "RR:p1")

    assert ledger["plans"]["p1"]["status"] == "partial_submit"
    assert ledger["plans"]["p1"]["order_ids"] == [123]


def test_live_risk_trailing_stop_updates_state():
    ledger = new_ledger("account", "TAG", 890)
    ledger.update(
        {
            "cash": 0.0,
            "peak_equity": 890.0,
            "previous_equity": 890.0,
            "positions": {
                "ETF.SH": {
                    "quantity": 100,
                    "average_cost": 10.0,
                    "atr_at_entry": 1.0,
                    "high_watermark": 12.0,
                }
            },
        }
    )

    ledger, exits, equity = evaluate_live_risk(ledger, {"ETF.SH": 8.9}, RISK, "2025-01-03")

    assert equity == pytest.approx(890.0)
    assert exits == {"ETF.SH": "trailing_stop"}
    assert ledger["positions"]["ETF.SH"]["active_stop"] == pytest.approx(9.0)


def test_live_risk_uses_same_minimum_stop_distance_as_backtest():
    ledger = new_ledger("account", "TAG", 9_900)
    ledger.update(
        {
            "cash": 0.0,
            "peak_equity": 10_000.0,
            "previous_equity": 10_000.0,
            "positions": {
                "ETF.SH": {
                    "quantity": 100,
                    "average_cost": 100.0,
                    "atr_at_entry": 0.1,
                    "high_watermark": 100.0,
                }
            },
        }
    )
    risk = {**RISK, "minimum_stop_distance": 0.015}

    ledger, exits, _ = evaluate_live_risk(ledger, {"ETF.SH": 99.0}, risk, "2025-01-03")

    assert exits == {}
    assert ledger["positions"]["ETF.SH"]["active_stop"] == pytest.approx(98.5)


def test_live_risk_exit_remains_latched_after_price_rebound():
    ledger = new_ledger("account", "TAG", 1_000)
    ledger.update(
        {
            "cash": 0.0,
            "peak_equity": 890.0,
            "previous_equity": 890.0,
            "positions": {
                "ETF.SH": {
                    "quantity": 100,
                    "average_cost": 10.0,
                    "atr_at_entry": 1.0,
                    "high_watermark": 12.0,
                }
            },
        }
    )

    ledger, exits, _ = evaluate_live_risk(ledger, {"ETF.SH": 8.9}, RISK, "2025-01-03")
    assert exits == {"ETF.SH": "trailing_stop"}
    ledger, exits, _ = evaluate_live_risk(ledger, {"ETF.SH": 10.5}, RISK, "2025-01-03")

    assert exits == {"ETF.SH": "trailing_stop"}


def test_hard_drawdown_liquidates_and_cooldown_decrements_once_per_day():
    ledger = new_ledger("account", "TAG", 1_000)
    ledger.update(
        {
            "cash": 0.0,
            "peak_equity": 1_000.0,
            "previous_equity": 900.0,
            "positions": {
                "ETF.SH": {
                    "quantity": 100,
                    "average_cost": 10.0,
                    "atr_at_entry": 1.0,
                    "high_watermark": 10.0,
                }
            },
        }
    )

    ledger, exits, equity = evaluate_live_risk(ledger, {"ETF.SH": 8.0}, RISK, "2025-01-03")
    ledger = finalize_live_day(ledger, equity, "2025-01-03")
    ledger = finalize_live_day(ledger, equity, "2025-01-03")

    assert exits == {"ETF.SH": "portfolio_hard_drawdown"}
    assert ledger["cooldown_remaining"] == 10
    assert ledger["peak_equity"] == pytest.approx(800.0)

    ledger = finalize_live_day(ledger, equity, "2025-01-06")
    assert ledger["cooldown_remaining"] == 9


def test_monitor_heartbeat_sets_daily_baseline_once():
    ledger = new_ledger("account", "TAG", 1_000)

    ledger = update_monitor_heartbeat(ledger, "2025-01-03", 990.0)
    ledger = update_monitor_heartbeat(ledger, "2025-01-03", 950.0)

    assert ledger["previous_equity"] == pytest.approx(990.0)
    ledger["cooldown_remaining"] = 2
    ledger = update_monitor_heartbeat(ledger, "2025-01-06", 960.0)
    assert ledger["previous_equity"] == pytest.approx(950.0)
    assert ledger["cooldown_remaining"] == 1


def test_live_risk_rejects_missing_price():
    ledger = new_ledger("account", "TAG", 1_000)
    ledger["positions"] = {
        "ETF.SH": {
            "quantity": 100,
            "average_cost": 10.0,
            "atr_at_entry": 1.0,
            "high_watermark": 10.0,
        }
    }

    with pytest.raises(ValueError, match="缺少有效实时价格"):
        evaluate_live_risk(ledger, {}, RISK, "2025-01-03")
