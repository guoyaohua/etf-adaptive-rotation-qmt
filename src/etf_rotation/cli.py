from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from .backtest import Backtester
from .config import AppConfig, load_config
from .data import CsvMarketDataStore, QmtDailyDownloader
from .qmt import QmtBroker, build_order_plan, load_owned_positions, save_plan
from .reporting import write_backtest_report
from .schedule import scheduled_dates
from .strategy import RegimeRotationStrategy, TargetPortfolio


def _store(config: AppConfig) -> CsvMarketDataStore:
    return CsvMarketDataStore(config.resolve_path(str(config.qmt["data_directory"])))


def _latest_common_date(data: dict[str, pd.DataFrame]) -> pd.Timestamp:
    dates = [frame.index.max() for frame in data.values() if not frame.empty]
    if not dates:
        raise RuntimeError("没有可用行情日期")
    return max(dates)


def _target_payload(target: TargetPortfolio) -> dict:
    return {
        "decision_date": target.decision_date.isoformat(),
        "regime": target.regime,
        "weights": target.weights,
        "diagnostics": target.diagnostics,
        "selected": {
            symbol: {
                "momentum": target.signals[symbol].momentum,
                "volatility": target.signals[symbol].volatility,
                "score": target.signals[symbol].score,
                "atr": target.signals[symbol].atr,
            }
            for symbol in target.weights
        },
    }


def _scheduled_target(
    strategy: RegimeRotationStrategy,
    data: dict[str, pd.DataFrame],
    as_of: pd.Timestamp,
) -> TargetPortfolio:
    calendar = pd.DatetimeIndex([])
    for frame in data.values():
        calendar = calendar.union(frame.index)
    dates = scheduled_dates(calendar.sort_values().unique(), as_of, strategy.config.strategy)
    targets = [strategy.target(data, date) for date in dates]
    if str(strategy.config.strategy.get("rebalance_schedule")) == "staggered_weeks":
        return strategy.aggregate_targets(targets, int(strategy.config.strategy.get("rebalance_sleeves", 4)))
    return targets[-1]


def command_download(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    downloader = QmtDailyDownloader()
    data = downloader.download(config.symbols, args.start, args.end, args.timeout)
    metadata = {
        "source": "QMT xtdata",
        "start_requested": args.start,
        "end_requested": args.end,
        "symbols_requested": config.symbols,
        "rows": {symbol: len(frame) for symbol, frame in data.items()},
    }
    _store(config).save(data, metadata)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


def command_backtest(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    data = _store(config).load(config.symbols, args.start, args.end)
    base = Backtester(config, cost_multiplier=1.0).run(data)
    stress_multiplier = float(config.execution["stress_cost_multiplier"])
    stress = Backtester(config, cost_multiplier=stress_multiplier).run(data)
    output = Path(args.output)
    summary = write_backtest_report(base, stress, config, output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"报告: {(output / 'REPORT.md').resolve()}")
    return 0 if summary["passed_all_gates"] else 2


def command_signal(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    data = _store(config).load(config.symbols)
    date = pd.Timestamp(args.date) if args.date else _latest_common_date(data)
    strategy = RegimeRotationStrategy(config)
    target = _scheduled_target(strategy, data, date)
    payload = _target_payload(target)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def command_qmt_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    data = _store(config).load(config.symbols)
    date = pd.Timestamp(args.date) if args.date else _latest_common_date(data)
    strategy = RegimeRotationStrategy(config)
    target = _scheduled_target(strategy, data, date)
    broker = QmtBroker(config)
    runtime = config.resolve_path(str(config.qmt["runtime_directory"]))
    ledger_path = runtime / "owned_positions.json"
    owned_positions = load_owned_positions(ledger_path)
    asset, account_positions, account_sellable, strategy_positions, prices = broker.snapshot(owned_positions)
    ownership_conflicts = {
        symbol: account_positions[symbol] - strategy_positions.get(symbol, 0)
        for symbol in account_positions
        if account_positions[symbol] > strategy_positions.get(symbol, 0)
        and (symbol in target.weights or strategy_positions.get(symbol, 0) > 0)
    }
    if ownership_conflicts:
        raise RuntimeError(
            "账户存在与策略目标同代码、但不属于策略账本的持仓；为防止混仓已拒绝计划: "
            f"{ownership_conflicts}"
        )
    capital = float(args.capital)
    if capital <= 0 or capital > float(asset.total_asset):
        raise ValueError("--capital 必须大于 0 且不超过账户总资产")
    missing_quotes = sorted(set(target.weights).union(strategy_positions).difference(prices))
    if missing_quotes:
        raise RuntimeError(f"目标持仓缺少有效实时行情（可能已过期）: {missing_quotes}")
    sellable_positions = {
        symbol: min(strategy_positions.get(symbol, 0), account_sellable.get(symbol, 0))
        for symbol in strategy_positions
    }
    orders = build_order_plan(
        target.weights,
        strategy_positions,
        prices,
        capital,
        lot_size=int(config.execution["lot_size"]),
        min_weight_change=float(config.strategy["min_weight_change"]),
        sellable_positions=sellable_positions,
    )
    output = Path(args.output) if args.output else runtime / "latest_order_plan.json"
    save_plan(
        orders,
        output,
        {
            "target": _target_payload(target),
            "execute_requested": args.execute,
            "strategy_capital": capital,
            "account_positions_observed": account_positions,
            "strategy_positions_observed": strategy_positions,
            "owned_position_ledger": str(ledger_path),
            "warning": "卖单数量仅来自策略持仓账本与账户持仓的交集",
        },
    )
    print(output.read_text(encoding="utf-8"))
    if not args.execute:
        print("仅生成计划，未下单。")
        return 0
    confirmation = input(f"输入 {QmtBroker.CONFIRMATION} 确认真实下单: " ).strip()
    order_ids = broker.execute(orders, confirmation)
    print(json.dumps({"submitted_order_ids": order_ids}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ETF Adaptive Rotation for QMT")
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser("download", help="从本机 QMT 下载日线")
    download.add_argument("--config", required=True)
    download.add_argument("--start", required=True, help="YYYYMMDD")
    download.add_argument("--end", required=True, help="YYYYMMDD")
    download.add_argument("--timeout", type=int, default=300)
    download.set_defaults(func=command_download)

    backtest = subparsers.add_parser("backtest", help="运行基础和压力成本回测")
    backtest.add_argument("--config", required=True)
    backtest.add_argument("--start")
    backtest.add_argument("--end")
    backtest.add_argument("--output", default="reports/latest")
    backtest.set_defaults(func=command_backtest)

    signal = subparsers.add_parser("signal", help="生成最新目标权重")
    signal.add_argument("--config", required=True)
    signal.add_argument("--date")
    signal.add_argument("--output")
    signal.set_defaults(func=command_signal)

    qmt_plan = subparsers.add_parser("qmt-plan", help="查询 QMT 并生成订单计划")
    qmt_plan.add_argument("--config", required=True)
    qmt_plan.add_argument("--date")
    qmt_plan.add_argument("--capital", type=float, required=True, help="本策略可使用的资金上限")
    qmt_plan.add_argument("--output")
    qmt_plan.add_argument("--execute", action="store_true")
    qmt_plan.set_defaults(func=command_qmt_plan)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("操作已取消", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
