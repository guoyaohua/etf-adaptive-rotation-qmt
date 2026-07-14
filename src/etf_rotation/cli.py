from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import time as wall_time
from pathlib import Path

import pandas as pd

from .backtest import Backtester
from .config import AppConfig, load_config
from .data import CsvMarketDataStore, QmtDailyDownloader
from .llm_decision import apply_llm_overlay, run_llm_decision
from .qmt import OrderSubmissionError, PlannedOrder, QmtBroker, build_order_plan, save_plan
from .reporting import write_backtest_report
from .runtime import (
    atomic_write_json,
    evaluate_live_risk,
    is_continuous_trading_session,
    is_plan_terminal,
    last_completed_calendar_date,
    ledger_quantities,
    ledger_equity,
    load_ledger,
    mark_plan,
    mark_orders_from_remark,
    new_ledger,
    now_shanghai,
    reconcile_ledger,
    recover_stale_submitting_plans,
    runtime_lock,
    stable_plan_id,
    stable_risk_plan_id,
    update_monitor_heartbeat,
)
from .schedule import compare_exchange_calendar, is_exchange_session, scheduled_dates
from .strategy import RegimeRotationStrategy, TargetPortfolio
from .version import STRATEGY_VERSION


def _store(config: AppConfig) -> CsvMarketDataStore:
    return CsvMarketDataStore(config.resolve_path(str(config.qmt["data_directory"])))


def _latest_common_date(data: dict[str, pd.DataFrame]) -> pd.Timestamp:
    indices = [frame.index for frame in data.values() if not frame.empty]
    if not indices:
        raise RuntimeError("没有可用行情日期")
    common = indices[0]
    for index in indices[1:]:
        common = common.intersection(index)
    if common.empty:
        latest_by_symbol = {symbol: str(frame.index.max().date()) for symbol, frame in data.items() if not frame.empty}
        raise RuntimeError(f"标的之间没有共同行情日期: {latest_by_symbol}")
    return pd.Timestamp(common.max())


def _market_calendar(data: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    calendar = pd.DatetimeIndex([])
    for frame in data.values():
        if not frame.empty:
            calendar = calendar.union(frame.index)
    if calendar.empty:
        raise RuntimeError("没有可用于校验的行情日期")
    return calendar.sort_values().unique()


def _require_calendar_match(
    config: AppConfig,
    data: dict[str, pd.DataFrame],
    completed_through: str | pd.Timestamp | None,
    action: str,
) -> dict[str, object]:
    result = compare_exchange_calendar(
        _market_calendar(data),
        str(config.strategy["rebalance_calendar"]),
        completed_through=completed_through,
    )
    if not result["passed"]:
        raise RuntimeError(
            f"行情日期与交易所日历不一致，拒绝{action}："
            f"missing={result['missing_sessions']}, "
            f"unexpected={result['unexpected_sessions']}"
        )
    return result


def _target_payload(target: TargetPortfolio) -> dict:
    return {
        "strategy_version": STRATEGY_VERSION,
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


def _apply_configured_llm(
    config: AppConfig, target: TargetPortfolio, refresh: bool = False
) -> tuple[TargetPortfolio, dict | None]:
    settings = config.llm
    if not bool(settings.get("enabled", False)):
        return target, None
    cache = config.resolve_path(str(settings.get("cache_directory", "runtime/llm")))
    names = {item.symbol: item.name for item in config.universe}
    result = run_llm_decision(target, settings, names, cache, refresh=refresh)
    overlaid = apply_llm_overlay(
        target, result, float(settings.get("max_scale_down", 1.0)), bool(settings.get("allow_exits", True))
    )
    return overlaid, asdict(result)


def _position_state_payload(targets: list[TargetPortfolio]) -> dict[str, dict[str, float]]:
    """Keep signal state for every symbol contributed by any active sleeve."""
    result: dict[str, dict[str, float]] = {}
    for target in targets:
        for symbol in target.weights:
            signal = target.signals.get(symbol)
            if signal is not None:
                result[symbol] = {"atr": float(signal.atr)}
    return result


def _exit_state_from_ledger(ledger: dict) -> dict[str, dict[str, float]]:
    return {
        symbol: {"atr": float(value.get("atr_at_entry", 0.0))}
        for symbol, value in ledger.get("positions", {}).items()
        if isinstance(value, dict) and float(value.get("atr_at_entry", 0.0)) > 0
    }


def _scheduled_targets(
    strategy: RegimeRotationStrategy,
    data: dict[str, pd.DataFrame],
    as_of: pd.Timestamp,
    completed_through: str | pd.Timestamp | None = None,
) -> tuple[TargetPortfolio, list[TargetPortfolio]]:
    calendar = _market_calendar(data)
    _require_calendar_match(
        strategy.config, data, completed_through or as_of, "生成信号"
    )
    dates = scheduled_dates(
        calendar.sort_values().unique(),
        as_of,
        strategy.config.strategy,
        completed_through=completed_through,
    )
    targets = [strategy.target(data, date) for date in dates]
    if str(strategy.config.strategy.get("rebalance_schedule")) == "staggered_weeks":
        return strategy.aggregate_targets(targets, int(strategy.config.strategy.get("rebalance_sleeves", 4))), targets
    return targets[-1], targets


def _scheduled_target(
    strategy: RegimeRotationStrategy,
    data: dict[str, pd.DataFrame],
    as_of: pd.Timestamp,
    completed_through: str | pd.Timestamp | None = None,
) -> TargetPortfolio:
    return _scheduled_targets(strategy, data, as_of, completed_through)[0]


def _completed_download_end(
    requested_end: str | pd.Timestamp, completed_through: str | pd.Timestamp
) -> str:
    requested = pd.Timestamp(requested_end).normalize()
    completed = pd.Timestamp(completed_through).normalize()
    return min(requested, completed).strftime("%Y%m%d")


def command_download(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    actual_end = _completed_download_end(
        args.end, last_completed_calendar_date()
    )
    if pd.Timestamp(args.start).normalize() > pd.Timestamp(actual_end).normalize():
        raise ValueError(
            f"下载起点 {args.start} 晚于最后可用完整日线 {pd.Timestamp(actual_end).date()}"
        )
    downloader = QmtDailyDownloader()
    data = downloader.download(config.symbols, args.start, actual_end, args.timeout)
    _require_calendar_match(config, data, actual_end, "保存不完整下载")
    metadata = {
        "source": "QMT xtdata",
        "start_requested": args.start,
        "end_requested": args.end,
        "completed_through": actual_end,
        "symbols_requested": config.symbols,
        "rows": {symbol: len(frame) for symbol, frame in data.items()},
    }
    _store(config).save(data, metadata)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


def command_backtest(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    data = _store(config).load(config.symbols, args.start, args.end, require_all=True)
    _require_calendar_match(config, data, args.end, "运行回测")
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
    store = _store(config)
    data = store.load(config.symbols, require_all=True)
    date = pd.Timestamp(args.date) if args.date else _latest_common_date(data)
    metadata = store.load_metadata()
    completed_through = (
        args.completed_through
        if args.completed_through
        else (
            min(
                pd.Timestamp(args.date).normalize(),
                pd.Timestamp(last_completed_calendar_date()).normalize(),
            ).strftime("%Y%m%d")
            if args.date
            else metadata.get("completed_through")
        )
    )
    if completed_through is None:
        raise RuntimeError(
            "行情元数据缺少 completed_through，无法确认节假日调度边界；"
            "请先运行 download，或显式传入 --completed-through"
        )
    strategy = RegimeRotationStrategy(config)
    target = _scheduled_target(strategy, data, date, completed_through)
    payload = _target_payload(target)
    llm_result = None
    if args.with_llm or bool(config.llm.get("enabled", False)):
        if args.with_llm and not bool(config.llm.get("enabled", False)):
            raise RuntimeError("--with-llm 要求配置 llm.enabled=true")
        target, llm_result = _apply_configured_llm(config, target, refresh=args.refresh_llm)
        payload = _target_payload(target)
        payload["llm_decision"] = llm_result
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _runtime_paths(config: AppConfig) -> tuple[Path, Path, Path]:
    runtime = config.resolve_path(str(config.qmt["runtime_directory"]))
    return runtime, runtime / "state.json", runtime / "latest_order_plan.json"


def _runtime_lock_path(config: AppConfig) -> Path:
    return config.resolve_path(str(config.qmt["runtime_directory"])) / "strategy.lock"


def _environment_names(config: AppConfig) -> tuple[str, str]:
    return str(config.qmt["client_path_env"]), str(config.qmt["account_id_env"])


def command_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    client_key, account_key = _environment_names(config)
    checks: list[dict[str, str | bool]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    check("config", True, str(config.path))
    check("python", sys.version_info >= (3, 10), sys.version.split()[0])
    client_path = os.environ.get(client_key, "")
    account_id = os.environ.get(account_key, "")
    check(client_key, bool(client_path), "已设置" if client_path else "未设置")
    check(account_key, bool(account_id), "已设置" if account_id else "未设置")
    check("QMT client path", bool(client_path and Path(client_path).exists()), "路径存在" if client_path and Path(client_path).exists() else "路径不存在")
    try:
        from xtquant import xtdata  # noqa: F401
        check("xtquant", True, "可导入")
    except Exception as exc:
        check("xtquant", False, str(exc))
    try:
        store = _store(config)
        data = store.load(config.symbols, require_all=True)
        metadata = store.load_metadata()
        if not metadata.get("completed_through"):
            raise RuntimeError(
                "行情元数据缺少 completed_through，请先重新下载完整日线"
            )
        _require_calendar_match(
            config, data, metadata.get("completed_through"), "通过环境体检"
        )
        latest = _latest_common_date(data)
        row_counts = [len(frame) for frame in data.values()]
        check("daily data", min(row_counts) >= RegimeRotationStrategy(config).warmup_bars, f"最新 {latest.date()}，最少 {min(row_counts)} 行")
    except Exception as exc:
        check("daily data", False, str(exc))
    runtime, ledger_path, _ = _runtime_paths(config)
    check("runtime directory", True, str(runtime))
    if account_id:
        try:
            load_ledger(ledger_path, account_id, str(config.execution["strategy_tag"]), required=True)
            check("strategy ledger", True, str(ledger_path))
        except Exception as exc:
            check("strategy ledger", False, str(exc))
    else:
        check("strategy ledger", False, "需要先设置账户环境变量")
    if bool(config.llm.get("enabled", False)):
        key_env = str(config.llm.get("api_key_env", "GITHUB_TOKEN"))
        check(key_env, bool(os.environ.get(key_env)), "已设置" if os.environ.get(key_env) else "未设置")
        try:
            import litellm  # noqa: F401
            check("litellm", True, "可导入")
        except Exception as exc:
            check("litellm", False, str(exc))

    if args.connect and client_path and account_id:
        broker: QmtBroker | None = None
        try:
            broker = QmtBroker(config)
            asset, positions, _, _, prices = broker.snapshot({})
            check("QMT connection", True, f"账户资产可查询；持仓 {len(positions)}；有效报价 {len(prices)}")
        except Exception as exc:
            check("QMT connection", False, str(exc))
        finally:
            if broker:
                broker.close()
    print(json.dumps({"passed": all(item["passed"] for item in checks), "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if all(item["passed"] for item in checks) else 2


def command_ledger_init(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _, account_key = _environment_names(config)
    account_id = os.environ.get(account_key)
    if not account_id:
        raise RuntimeError(f"请先设置环境变量 {account_key}")
    _, ledger_path, _ = _runtime_paths(config)
    with runtime_lock(_runtime_lock_path(config)):
        if ledger_path.exists():
            raise FileExistsError(
                f"账本已存在: {ledger_path}；为避免丢失持仓归属，程序不允许覆盖。"
                "如确需重建，请先停止策略并人工备份、核对和移走旧账本"
            )
        ledger = new_ledger(account_id, str(config.execution["strategy_tag"]), float(args.capital))
        atomic_write_json(ledger_path, ledger)
    print(json.dumps({"created": str(ledger_path), "capital": float(args.capital)}, ensure_ascii=False, indent=2))
    return 0


def _reconcile(
    config: AppConfig, broker: QmtBroker, ledger_path: Path,
    target: TargetPortfolio | None = None,
    position_state: dict[str, dict[str, float]] | None = None,
    exit_state: dict[str, dict[str, float]] | None = None,
) -> tuple[dict, int]:
    _, account_key = _environment_names(config)
    account_id = os.environ.get(account_key)
    if not account_id:
        raise RuntimeError(f"请先设置环境变量 {account_key}")
    ledger = load_ledger(ledger_path, account_id, str(config.execution["strategy_tag"]), required=True)
    if exit_state is None:
        exit_state = _exit_state_from_ledger(ledger)
    ledger = recover_stale_submitting_plans(
        ledger,
        broker.query_strategy_order_ids(),
        float(config.qmt.get("live_plan_max_age_minutes", 20)),
    )
    atrs = {symbol: signal.atr for symbol, signal in target.signals.items()} if target else {}
    ledger, applied = reconcile_ledger(
        ledger,
        broker.query_strategy_trades(),
        float(config.execution["commission_rate"]),
        float(config.execution["minimum_commission"]),
        atrs,
        position_state,
        exit_state,
    )
    atomic_write_json(ledger_path, ledger)
    return ledger, applied


def command_reconcile(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _, ledger_path, plan_path = _runtime_paths(config)
    position_state: dict[str, dict[str, float]] = {}
    if plan_path.exists():
        try:
            plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
            raw_state = plan_payload.get("metadata", {}).get("position_state", {})
            if isinstance(raw_state, dict):
                position_state = raw_state
        except (OSError, ValueError):
            position_state = {}
    with runtime_lock(_runtime_lock_path(config)):
        broker = QmtBroker(config)
        try:
            ledger, applied = _reconcile(config, broker, ledger_path, position_state=position_state)
            print(json.dumps({"new_trades": applied, "positions": ledger_quantities(ledger), "cash": ledger["cash"]}, ensure_ascii=False, indent=2))
            return 0
        finally:
            broker.close()


def _build_live_plan(
    config: AppConfig,
    broker: QmtBroker,
    target: TargetPortfolio,
    ledger: dict,
    capital: float,
) -> tuple[list, dict, dict, dict]:
    strategy_positions = ledger_quantities(ledger)
    asset, account_positions, account_sellable, _, prices = broker.snapshot(strategy_positions)
    missing_account_positions = {
        symbol: quantity - account_positions.get(symbol, 0)
        for symbol, quantity in strategy_positions.items()
        if quantity > account_positions.get(symbol, 0)
    }
    if missing_account_positions:
        raise RuntimeError(f"策略账本持仓高于账户实仓，拒绝下单: {missing_account_positions}")
    ownership_conflicts = {
        symbol: account_positions[symbol] - strategy_positions.get(symbol, 0)
        for symbol in account_positions
        if account_positions[symbol] > strategy_positions.get(symbol, 0)
        and (symbol in target.weights or strategy_positions.get(symbol, 0) > 0)
    }
    if ownership_conflicts:
        raise RuntimeError(f"存在同代码人工/其他策略持仓，拒绝混仓: {ownership_conflicts}")
    if capital <= 0 or capital > float(asset.total_asset):
        raise ValueError("策略资金必须大于 0 且不超过账户总资产")
    missing_quotes = sorted(set(target.weights).union(strategy_positions).difference(prices))
    if missing_quotes:
        raise RuntimeError(f"缺少有效实时行情（可能过期）: {missing_quotes}")
    sellable = {symbol: min(quantity, account_sellable.get(symbol, 0)) for symbol, quantity in strategy_positions.items()}
    orders = build_order_plan(
        target.weights,
        strategy_positions,
        prices,
        capital,
        lot_size=int(config.execution["lot_size"]),
        min_weight_change=float(config.strategy["min_weight_change"]),
        sellable_positions=sellable,
    )
    return orders, account_positions, strategy_positions, prices


def _risk_scale_from_ledger(config: AppConfig, ledger: dict, equity: float) -> float:
    if int(ledger.get("cooldown_remaining", 0)) > 0:
        return 0.0
    peak = max(float(ledger.get("peak_equity", equity)), 1e-12)
    drawdown = 1.0 - float(equity) / peak
    if drawdown >= float(config.risk["hard_drawdown"]):
        return 0.0
    if drawdown >= float(config.risk["soft_drawdown"]):
        return float(config.risk["soft_drawdown_scale"])
    return 1.0


def _exclude_latched_risk_exits(
    target: TargetPortfolio, risk_exits: dict[str, str]
) -> TargetPortfolio:
    if not risk_exits:
        return target
    return TargetPortfolio(
        target.decision_date,
        target.regime,
        {symbol: weight for symbol, weight in target.weights.items() if symbol not in risk_exits},
        target.signals,
        {**target.diagnostics, "live_risk_exits": dict(risk_exits)},
    )


def command_live_once(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.execute and args.ignore_session:
        raise ValueError("--ignore-session 只能生成调试计划，不能与 --execute 同时使用")
    calendar_name = str(config.strategy["rebalance_calendar"])
    if not args.ignore_session and not is_continuous_trading_session(
        exchange_calendar=calendar_name
    ):
        raise RuntimeError("当前不在连续交易时段（09:30-11:30 / 13:00-14:55），拒绝运行实盘批次")
    end = last_completed_calendar_date()
    start = str(config.qmt.get("history_start", "20150101"))
    downloader = QmtDailyDownloader()
    downloaded = downloader.download(config.symbols, start, end, int(args.timeout))
    _require_calendar_match(config, downloaded, end, "保存实盘行情")
    _store(config).save(
        downloaded,
        {
            "source": "QMT xtdata",
            "start_requested": start,
            "end_requested": end,
            "completed_through": end,
        },
    )
    data = _store(config).load(config.symbols, require_all=True)
    as_of = _latest_common_date(data)
    strategy = RegimeRotationStrategy(config)
    target, sleeve_targets = _scheduled_targets(strategy, data, as_of, completed_through=end)
    position_state = _position_state_payload(sleeve_targets)
    target, llm_result = _apply_configured_llm(config, target, refresh=args.refresh_llm)
    first_session_after_signal = as_of.normalize() == target.decision_date.normalize()
    if args.execute and not first_session_after_signal and not args.allow_late:
        raise RuntimeError(
            f"最新已完成交易日 {as_of.date()} 晚于信号日 {target.decision_date.date()}；"
            "这不是信号后的首个交易日，拒绝追单。确需人工接管请显式加 --allow-late"
        )
    runtime, ledger_path, plan_path = _runtime_paths(config)
    with runtime_lock(_runtime_lock_path(config)):
        broker: QmtBroker | None = None
        try:
            broker = QmtBroker(config)
            return _run_live_once_locked(args, config, broker, ledger_path, plan_path, target, position_state, llm_result)
        finally:
            if broker is not None:
                broker.close()


def _run_live_once_locked(
    args: argparse.Namespace, config: AppConfig, broker: QmtBroker, ledger_path: Path,
    plan_path: Path, target: TargetPortfolio, position_state: dict[str, dict[str, float]],
    llm_result: dict | None,
) -> int:
        ledger, applied_before = _reconcile(config, broker, ledger_path, target, position_state)
        requested_capital = float(args.capital if args.capital is not None else ledger["initial_capital"])
        if requested_capital <= 0:
            raise ValueError("策略资金上限必须大于 0")
        strategy_positions = ledger_quantities(ledger)
        _, _, _, _, valuation_prices = broker.snapshot(strategy_positions)
        equity = ledger_equity(ledger, valuation_prices)
        trade_date = now_shanghai().date().isoformat()
        ledger = update_monitor_heartbeat(ledger, trade_date, equity)
        ledger, risk_exits, equity = evaluate_live_risk(ledger, valuation_prices, config.risk, trade_date)
        atomic_write_json(ledger_path, ledger)
        if risk_exits:
            # A weekly target must not reopen a symbol whose protective exit is
            # already latched.  Full target exits bypass the no-trade band, so
            # the resulting plan prioritizes the same risk action as monitor.
            target = _exclude_latched_risk_exits(target, risk_exits)
        capital = min(requested_capital, equity)
        risk_scale = _risk_scale_from_ledger(config, ledger, equity)
        if risk_scale < 1.0:
            target = TargetPortfolio(
                target.decision_date,
                target.regime,
                {symbol: weight * risk_scale for symbol, weight in target.weights.items()},
                target.signals,
                {**target.diagnostics, "live_risk_scale": risk_scale},
            )
        orders, account_positions, strategy_positions, prices = _build_live_plan(config, broker, target, ledger, capital)
        order_payloads = [asdict(order) for order in orders]
        plan_id = stable_plan_id(
            str(config.execution["strategy_tag"]),
            target.decision_date.isoformat(),
            capital,
            target.weights,
            order_payloads,
        )
        orders = [
            PlannedOrder(order.symbol, order.side, order.quantity, order.price, f"RR:{plan_id}")
            for order in orders
        ]
        existing = ledger.get("plans", {}).get(plan_id, {})
        broker_remarks = broker.query_strategy_order_remarks()
        matching_remark = next((remark for remark in {plan_id, f"RR:{plan_id}"} if remark in broker_remarks), None)
        if matching_remark is not None:
            ledger = mark_orders_from_remark(ledger, plan_id, [asdict(order) for order in orders], broker_remarks, matching_remark)
            atomic_write_json(ledger_path, ledger)
            raise RuntimeError(f"QMT 已存在计划 {plan_id} 的委托备注，拒绝重复下单")
        if existing.get("status") == "manual_review":
            raise RuntimeError(f"计划 {plan_id} 状态需要人工核对 QMT，禁止自动重试")
        if is_plan_terminal(existing.get("status")):
            raise RuntimeError(f"计划 {plan_id} 已处理，状态={existing.get('status')}；拒绝重复下单")
        save_plan(
            orders,
            plan_path,
            {
                "plan_id": plan_id,
                "target": _target_payload(target),
                "strategy_capital": capital,
                "strategy_capital_limit": requested_capital,
                "strategy_equity_observed": equity,
                "account_positions_observed": account_positions,
                "strategy_positions_observed": strategy_positions,
                "prices": prices,
                "reconciled_trades_before_plan": applied_before,
                "position_state": position_state,
                "llm_decision": llm_result,
            },
        )
        print(plan_path.read_text(encoding="utf-8"))
        if not orders:
            ledger = mark_plan(ledger, plan_id, "no_orders")
            atomic_write_json(ledger_path, ledger)
            print("当前目标与策略持仓一致，无需下单。")
            return 0
        if not args.execute:
            print("仅生成实盘计划，未下单。加入 --execute 后仍需输入确认短语。")
            return 0
        confirmation = input(f"输入 {QmtBroker.CONFIRMATION} 确认提交计划 {plan_id}: ").strip()
        if confirmation != QmtBroker.CONFIRMATION:
            raise PermissionError("确认短语不匹配，未下单")
        plan_age = (time.time() - plan_path.stat().st_mtime) / 60.0
        max_age = float(config.qmt.get("live_plan_max_age_minutes", 20))
        if plan_age > max_age:
            raise RuntimeError(f"订单计划已生成 {plan_age:.1f} 分钟，超过 {max_age:g} 分钟上限；请重新生成")
        ledger = mark_plan(ledger, plan_id, "submitting")
        atomic_write_json(ledger_path, ledger)
        try:
            order_ids = broker.execute(orders, confirmation)
        except OrderSubmissionError as exc:
            status = "partial_submit" if exc.submitted_order_ids else "submit_error"
            ledger = mark_plan(ledger, plan_id, status, order_ids=exc.submitted_order_ids, error=str(exc))
            atomic_write_json(ledger_path, ledger)
            raise
        except Exception as exc:
            ledger = mark_plan(ledger, plan_id, "submit_error", error=str(exc))
            atomic_write_json(ledger_path, ledger)
            raise
        ledger = mark_plan(ledger, plan_id, "submitted", order_ids=order_ids)
        atomic_write_json(ledger_path, ledger)
        print(json.dumps({"plan_id": plan_id, "submitted_order_ids": order_ids, "next": "成交后运行 etf-rr reconcile"}, ensure_ascii=False, indent=2))
        return 0


def command_live_monitor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.execute and args.ignore_session:
        raise ValueError("--ignore-session 只能演练风控，不能与 --execute 同时使用")
    _, ledger_path, _ = _runtime_paths(config)
    with runtime_lock(_runtime_lock_path(config)):
        broker: QmtBroker | None = None
        try:
            broker = QmtBroker(config)
            return _run_live_monitor_locked(args, config, broker, ledger_path)
        finally:
            if broker is not None:
                broker.close()


def _run_live_monitor_locked(
    args: argparse.Namespace, config: AppConfig, broker: QmtBroker, ledger_path: Path
) -> int:
    try:
        confirmation = ""
        if args.execute:
            confirmation = input(f"输入 {QmtBroker.CONFIRMATION} 确认启动受保护风控监控: " ).strip()
            if confirmation != QmtBroker.CONFIRMATION:
                raise PermissionError("确认短语不匹配，未启动实盘风控")
        while True:
            current = now_shanghai()
            current_time = current.time().replace(tzinfo=None)
            if not args.ignore_session and not is_continuous_trading_session(
                current, str(config.strategy["rebalance_calendar"])
            ):
                if args.once:
                    raise RuntimeError("当前不在连续交易时段，风控检查已安全停止")
                if (
                    is_exchange_session(
                        current.date().isoformat(),
                        str(config.strategy["rebalance_calendar"]),
                    )
                    and wall_time(11, 30) < current_time < wall_time(13, 0)
                ):
                    time.sleep(min(30, max(1, int(args.interval))))
                    continue
                print("已离开连续交易时段，风控监控正常结束。")
                return 0
            ledger, applied = _reconcile(config, broker, ledger_path)
            strategy_positions = ledger_quantities(ledger)
            _, account_positions, account_sellable, _, prices = broker.snapshot(strategy_positions)
            conflicts = {
                symbol: account_positions.get(symbol, 0) - quantity
                for symbol, quantity in strategy_positions.items()
                if account_positions.get(symbol, 0) != quantity
            }
            if conflicts:
                raise RuntimeError(f"策略账本与账户持仓不一致，停止监控: {conflicts}")
            trade_date = now_shanghai().date().isoformat()
            current_equity = float(ledger.get("cash", 0.0)) + sum(
                quantity * prices[symbol] for symbol, quantity in strategy_positions.items()
            )
            ledger = update_monitor_heartbeat(ledger, trade_date, current_equity)
            ledger, exits, equity = evaluate_live_risk(ledger, prices, config.risk, trade_date)
            risk_orders = [
                PlannedOrder(symbol, "SELL", min(strategy_positions[symbol], account_sellable.get(symbol, 0)), prices[symbol], reason)
                for symbol, reason in exits.items()
                if min(strategy_positions[symbol], account_sellable.get(symbol, 0)) > 0
            ]
            unsellable_exits = sorted(set(exits).difference(order.symbol for order in risk_orders))
            if unsellable_exits:
                atomic_write_json(ledger_path, ledger)
                raise RuntimeError(f"风险退出持仓当前无可用数量，停止监控并要求人工核对: {unsellable_exits}")
            trigger_dates = ledger.get("pending_risk_exit_dates", {})
            trigger_key = "|".join(
                sorted({str(trigger_dates.get(order.symbol, trade_date)) for order in risk_orders})
            ) or trade_date
            risk_id = stable_risk_plan_id(
                str(config.execution["strategy_tag"]), trigger_key, [asdict(order) for order in risk_orders]
            )
            risk_orders = [
                PlannedOrder(order.symbol, order.side, order.quantity, order.price, f"RISK:{risk_id}")
                for order in risk_orders
            ]
            existing = ledger.get("plans", {}).get(risk_id, {})
            submitted: list[int] = []
            broker_remarks = broker.query_strategy_order_remarks() if risk_orders else {}
            if risk_orders and f"RISK:{risk_id}" in broker_remarks:
                ledger = mark_orders_from_remark(
                    ledger, risk_id, [asdict(order) for order in risk_orders], broker_remarks, f"RISK:{risk_id}"
                )
                atomic_write_json(ledger_path, ledger)
                raise RuntimeError(f"QMT 已存在风控计划 {risk_id} 的委托备注，拒绝重复下单")
            if risk_orders and existing.get("status") == "manual_review":
                raise RuntimeError(f"风控计划 {risk_id} 状态需要人工核对 QMT，禁止自动重试")
            if risk_orders and args.execute and is_plan_terminal(existing.get("status")):
                # Do not resubmit blindly. If an earlier risk order was rejected
                # or canceled, reconciliation must first put it into an explicit
                # retryable state after an operator has checked QMT.
                raise RuntimeError(
                    f"风险退出计划 {risk_id} 已处理，状态={existing.get('status')}；"
                    "持仓仍在且禁止自动重复下单，请立即人工核查 QMT 委托/成交"
                )
            if risk_orders and args.execute:
                ledger = mark_plan(ledger, risk_id, "submitting")
                atomic_write_json(ledger_path, ledger)
                try:
                    submitted = broker.execute(risk_orders, confirmation)
                except OrderSubmissionError as exc:
                    ledger = mark_plan(
                        ledger, risk_id, "partial_submit" if exc.submitted_order_ids else "submit_error",
                        order_ids=exc.submitted_order_ids, error=str(exc),
                    )
                    atomic_write_json(ledger_path, ledger)
                    raise
                ledger = mark_plan(ledger, risk_id, "submitted", order_ids=submitted)
            atomic_write_json(ledger_path, ledger)
            print(json.dumps({
                "time": now_shanghai().isoformat(timespec="seconds"), "equity": equity,
                "new_trades": applied, "risk_exits": exits, "submitted_order_ids": submitted,
                "mode": "execute" if args.execute else "dry-run",
            }, ensure_ascii=False))
            if args.once:
                return 0
            time.sleep(max(1, int(args.interval)))
    except KeyboardInterrupt:
        print("收到停止指令，风控监控正常结束。")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ETF Adaptive Rotation for QMT")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {STRATEGY_VERSION}"
    )
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
    signal.add_argument(
        "--completed-through",
        help="已确认完成的日历日；历史休市周回放时用于区分周四收盘与周五休市",
    )
    signal.add_argument("--output")
    signal.add_argument("--with-llm", action="store_true", help="按配置运行 LLM 风险复核")
    signal.add_argument("--refresh-llm", action="store_true", help="忽略本周 LLM 缓存并重新调用")
    signal.set_defaults(func=command_signal)

    doctor = subparsers.add_parser("doctor", help="检查配置、环境变量、行情、账本和可选 QMT 连接")
    doctor.add_argument("--config", default="configs/strategy.yaml")
    doctor.add_argument("--connect", action="store_true", help="同时连接 QMT 查询账户与行情")
    doctor.set_defaults(func=command_doctor)

    ledger_init = subparsers.add_parser("ledger-init", help="创建与当前账户绑定的策略持仓账本")
    ledger_init.add_argument("--config", default="configs/strategy.yaml")
    ledger_init.add_argument("--capital", type=float, required=True)
    ledger_init.set_defaults(func=command_ledger_init)

    reconcile = subparsers.add_parser("reconcile", help="查询本策略成交并幂等更新持仓账本")
    reconcile.add_argument("--config", default="configs/strategy.yaml")
    reconcile.set_defaults(func=command_reconcile)

    live_once = subparsers.add_parser("live-once", help="刷新数据、生成幂等计划，并可在确认后提交一次实盘批次")
    live_once.add_argument("--config", default="configs/strategy.yaml")
    live_once.add_argument("--capital", type=float, help="覆盖账本中的策略初始资金")
    live_once.add_argument("--timeout", type=int, default=300)
    live_once.add_argument("--execute", action="store_true")
    live_once.add_argument("--ignore-session", action="store_true", help="仅供调试；即使非交易时段也生成计划")
    live_once.add_argument("--allow-late", action="store_true", help="显式允许错过首个交易日后追单（高风险）")
    live_once.add_argument("--refresh-llm", action="store_true", help="忽略本周 LLM 缓存并重新调用")
    live_once.set_defaults(func=command_live_once)

    live_monitor = subparsers.add_parser("live-monitor", help="轮询策略持仓并执行 ATR/组合风险退出")
    live_monitor.add_argument("--config", default="configs/strategy.yaml")
    live_monitor.add_argument("--interval", type=int, default=5, help="轮询秒数")
    live_monitor.add_argument("--execute", action="store_true")
    live_monitor.add_argument("--once", action="store_true", help="只检查一次后退出")
    live_monitor.add_argument("--ignore-session", action="store_true", help="仅供非交易时段 dry-run 调试")
    live_monitor.set_defaults(func=command_live_monitor)
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
