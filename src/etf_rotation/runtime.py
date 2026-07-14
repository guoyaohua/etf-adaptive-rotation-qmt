from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, time as wall_time, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator, Mapping
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
LEDGER_SCHEMA_VERSION = 1


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def iso_now() -> str:
    return now_shanghai().isoformat(timespec="seconds")


def is_continuous_trading_session(moment: datetime | None = None) -> bool:
    current = moment or now_shanghai()
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    else:
        current = current.astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return (
        wall_time(9, 30) <= current_time <= wall_time(11, 30)
        or wall_time(13, 0) <= current_time <= wall_time(14, 55)
    )


def last_completed_calendar_date(moment: datetime | None = None) -> str:
    current = moment or now_shanghai()
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    else:
        current = current.astimezone(SHANGHAI)
    if current.weekday() < 5 and current.time().replace(tzinfo=None) >= wall_time(15, 5):
        completed = current.date()
    else:
        completed = current.date() - timedelta(days=1)
    return completed.strftime("%Y%m%d")


def account_fingerprint(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def _lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - the production runtime is Windows/QMT
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - the production runtime is Windows/QMT
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def runtime_lock(path: str | Path) -> Iterator[None]:
    """Prevent two local processes from mutating/submitting this strategy."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = destination.open("a+b")
    acquired = False
    try:
        if destination.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        try:
            _lock_file(handle)
            acquired = True
        except OSError as exc:
            raise RuntimeError(f"另一个实盘/对账进程正在运行，拒绝并发操作: {destination}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} acquired={iso_now()}\n".encode("utf-8"))
        handle.flush()
        yield
    finally:
        try:
            if acquired:
                _unlock_file(handle)
        finally:
            handle.close()


def read_json(path: str | Path, default: Any = None) -> Any:
    source = Path(path)
    if not source.exists():
        return default
    return json.loads(source.read_text(encoding="utf-8"))


def stable_plan_id(
    strategy_tag: str,
    decision_date: str,
    capital: float,
    target_weights: Mapping[str, float],
    orders: Iterable[Mapping[str, Any]],
) -> str:
    normalized_orders = sorted(
        [
            {
                "symbol": str(order["symbol"]),
                "side": str(order["side"]),
                "quantity": int(order["quantity"]),
            }
            for order in orders
        ],
        key=lambda value: (value["side"], value["symbol"], value["quantity"]),
    )
    payload = {
        "strategy_tag": strategy_tag,
        "decision_date": decision_date,
        "capital": round(float(capital), 2),
        "target_weights": {key: round(float(value), 10) for key, value in sorted(target_weights.items())},
        "orders": normalized_orders,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def stable_risk_plan_id(
    strategy_tag: str,
    trade_date: str,
    orders: Iterable[Mapping[str, Any]],
) -> str:
    normalized = sorted(
        (str(order["symbol"]), str(order["side"]), str(order.get("reason", "")))
        for order in orders
    )
    encoded = json.dumps(
        {"strategy_tag": strategy_tag, "trade_date": trade_date, "risk_orders": normalized},
        ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    )
    return "risk-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def new_ledger(account_id: str, strategy_tag: str, capital: float) -> dict[str, Any]:
    if capital <= 0:
        raise ValueError("策略资金必须大于 0")
    if not strategy_tag.strip():
        raise ValueError("strategy_tag 不能为空")
    created_at = iso_now()
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "account_fingerprint": account_fingerprint(account_id),
        "strategy_tag": strategy_tag,
        "initial_capital": float(capital),
        "cash": float(capital),
        "positions": {},
        "processed_trade_ids": [],
        "plans": {},
        "peak_equity": float(capital),
        "previous_equity": float(capital),
        "cooldown_remaining": 0,
        "last_risk_date": None,
        # Only trades after ledger creation may be imported. This prevents a new
        # ledger from claiming historical fills that happen to share a tag.
        "trade_baseline_at": created_at,
        "created_at": created_at,
        "updated_at": created_at,
    }


def load_ledger(
    path: str | Path,
    account_id: str | None = None,
    strategy_tag: str | None = None,
    required: bool = True,
) -> dict[str, Any]:
    payload = read_json(path)
    if payload is None:
        if required:
            raise FileNotFoundError(f"策略持仓账本不存在: {Path(path)}；请先运行 ledger-init")
        return {}
    if int(payload.get("schema_version", 0)) != LEDGER_SCHEMA_VERSION:
        raise ValueError("策略持仓账本版本不兼容")
    if account_id and payload.get("account_fingerprint") != account_fingerprint(account_id):
        raise ValueError("策略持仓账本不属于当前 QMT 账户")
    if strategy_tag and payload.get("strategy_tag") != strategy_tag:
        raise ValueError("策略持仓账本的 strategy_tag 与配置不一致")
    return payload


def ledger_quantities(ledger: Mapping[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for symbol, value in ledger.get("positions", {}).items():
        quantity = int(value.get("quantity", 0)) if isinstance(value, Mapping) else int(value)
        if quantity > 0:
            result[str(symbol)] = quantity
    return result


def ledger_equity(ledger: Mapping[str, Any], prices: Mapping[str, float]) -> float:
    quantities = ledger_quantities(ledger)
    missing = sorted(set(quantities).difference(prices))
    if missing:
        raise ValueError(f"策略权益估值缺少实时价格: {missing}")
    return float(ledger.get("cash", 0.0)) + sum(
        quantity * float(prices[symbol]) for symbol, quantity in quantities.items()
    )


def _trade_key(trade: Mapping[str, Any]) -> str:
    raw = "|".join(
        str(trade.get(key, ""))
        for key in ("order_id", "symbol", "side", "quantity", "price", "traded_time")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _epoch_seconds(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return numeric
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.timestamp()


def normalize_trade_timestamp(value: Any) -> float | None:
    """Normalize QMT's epoch or YYYYmmddHHMMSS trade-time formats."""

    if isinstance(value, (int, float)):
        if float(value) <= 0:
            return None
        digits = str(int(value))
    else:
        digits = str(value).strip()
    if len(digits) == 14 and digits.isdigit():
        try:
            parsed = datetime.strptime(digits, "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
        except ValueError:
            return None
        return parsed.timestamp()
    return _epoch_seconds(value)


def _trade_identity(trade: Mapping[str, Any]) -> str:
    provided = str(trade.get("trade_id", "")).strip()
    if not provided:
        return _trade_key(trade)
    account = str(trade.get("account_id", "")).strip()
    return f"{account}:{provided}" if account else provided


def reconcile_ledger(
    ledger: dict[str, Any],
    trades: Iterable[Mapping[str, Any]],
    commission_rate: float,
    minimum_commission: float,
    atr_by_symbol: Mapping[str, float] | None = None,
    position_state_by_symbol: Mapping[str, Mapping[str, float]] | None = None,
    exit_state_by_symbol: Mapping[str, Mapping[str, float]] | None = None,
) -> tuple[dict[str, Any], int]:
    processed = set(str(value) for value in ledger.get("processed_trade_ids", []))
    positions = dict(ledger.get("positions", {}))
    cash = float(ledger.get("cash", ledger.get("initial_capital", 0.0)))
    baseline = _epoch_seconds(ledger.get("trade_baseline_at") or ledger.get("created_at"))
    applied = 0
    for trade in sorted(trades, key=lambda value: (str(value.get("traded_time", "")), _trade_identity(value))):
        traded_at = normalize_trade_timestamp(trade.get("traded_time"))
        if baseline is not None:
            if traded_at is None:
                raise ValueError("成交时间缺失或无法解析，无法安全判断是否早于账本基线")
            if traded_at < baseline:
                continue
        trade_id = _trade_identity(trade)
        if trade_id in processed:
            continue
        symbol = str(trade["symbol"])
        side = str(trade["side"]).upper()
        quantity = int(trade["quantity"])
        price = float(trade["price"])
        if side not in {"BUY", "SELL"} or quantity <= 0 or price <= 0:
            raise ValueError(f"无效成交记录: {symbol} {side} {quantity} @ {price}")
        amount = float(trade.get("amount") or quantity * price)
        broker_commission = trade.get("commission")
        commission = (
            float(broker_commission)
            if broker_commission is not None and float(broker_commission) > 0
            else max(float(minimum_commission), amount * float(commission_rate))
        )
        current = positions.get(symbol, {})
        if not isinstance(current, Mapping):
            current = {"quantity": int(current), "average_cost": price}
        current_quantity = int(current.get("quantity", 0))
        if side == "BUY":
            combined = current_quantity + quantity
            average_cost = (
                float(current.get("average_cost", price)) * current_quantity + price * quantity + commission
            ) / combined
            atr_value = float(current.get("atr_at_entry", 0.0))
            if atr_value <= 0 and atr_by_symbol and symbol in atr_by_symbol:
                atr_value = float(atr_by_symbol[symbol])
            if atr_value <= 0 and exit_state_by_symbol and symbol in exit_state_by_symbol:
                atr_value = float(exit_state_by_symbol[symbol].get("atr", 0.0))
            if atr_value <= 0 and position_state_by_symbol and symbol in position_state_by_symbol:
                atr_value = float(position_state_by_symbol[symbol].get("atr", 0.0))
            if atr_value <= 0:
                raise ValueError(f"买入成交 {symbol} 缺少有效入场 ATR，拒绝写入无保护持仓")
            positions[symbol] = {
                "quantity": combined,
                "average_cost": average_cost,
                "atr_at_entry": atr_value,
                "high_watermark": max(float(current.get("high_watermark", price)), price),
            }
            cash -= amount + commission
        else:
            if quantity > current_quantity:
                raise ValueError(f"成交卖出数量超过策略账本持仓: {symbol} {quantity}>{current_quantity}")
            remaining = current_quantity - quantity
            cash += amount - commission
            if remaining == 0:
                positions.pop(symbol, None)
            else:
                updated = dict(current)
                updated["quantity"] = remaining
                positions[symbol] = updated
        processed.add(trade_id)
        applied += 1
    ledger["positions"] = positions
    pending_exits = dict(ledger.get("pending_risk_exits", {}))
    pending_exit_dates = dict(ledger.get("pending_risk_exit_dates", {}))
    for symbol in set(pending_exits).difference(positions):
        pending_exits.pop(symbol, None)
        pending_exit_dates.pop(symbol, None)
    ledger["pending_risk_exits"] = pending_exits
    ledger["pending_risk_exit_dates"] = pending_exit_dates
    ledger["cash"] = cash
    ledger["processed_trade_ids"] = sorted(processed)[-10000:]
    ledger["updated_at"] = iso_now()
    return ledger, applied


def mark_plan(
    ledger: dict[str, Any],
    plan_id: str,
    status: str,
    order_ids: Iterable[int] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    plans = dict(ledger.get("plans", {}))
    previous = dict(plans.get(plan_id, {}))
    created_at = previous.get("created_at", iso_now())
    previous.update(
        {
            "status": status,
            "order_ids": [int(value) for value in (order_ids or previous.get("order_ids", []))],
            "created_at": created_at,
            "updated_at": iso_now(),
        }
    )
    if error:
        previous["error"] = str(error)[:500]
    plans[plan_id] = previous
    ledger["plans"] = plans
    ledger["updated_at"] = iso_now()
    return ledger


def mark_orders_from_remark(
    ledger: dict[str, Any],
    plan_id: str,
    orders: Iterable[Mapping[str, Any]],
    remark_to_order_ids: Mapping[str, Iterable[int]],
    remark: str,
) -> dict[str, Any]:
    order_list = list(orders)
    order_ids = sorted({int(value) for value in remark_to_order_ids.get(remark, []) if int(value) > 0})
    status = "submitted" if len(order_ids) >= len(order_list) else "partial_submit"
    return mark_plan(ledger, plan_id, status, order_ids=order_ids, error="从 QMT 委托备注恢复计划状态")


def is_plan_terminal(status: str | None) -> bool:
    return status in {
        "submitting",
        "submitted",
        "partial_submit",
        "manual_review",
        "reconciled",
        "no_orders",
    }


def recover_stale_submitting_plans(
    ledger: dict[str, Any],
    broker_order_ids: Iterable[int],
    max_age_minutes: float,
    moment: datetime | None = None,
) -> dict[str, Any]:
    """Resolve a crash window without ever auto-resubmitting an unknown plan."""
    now = moment or now_shanghai()
    known_orders = {int(value) for value in broker_order_ids}
    for plan_id, raw in ledger.get("plans", {}).items():
        if raw.get("status") != "submitting":
            continue
        started_at = _epoch_seconds(raw.get("updated_at") or raw.get("created_at"))
        if started_at is None or now.timestamp() - started_at <= float(max_age_minutes) * 60:
            continue
        recorded = {int(value) for value in raw.get("order_ids", [])}
        raw["status"] = "submitted" if recorded.intersection(known_orders) else "manual_review"
        raw["error"] = (
            "检测到超时 submitting 状态；已看到券商委托"
            if raw["status"] == "submitted"
            else "检测到超时 submitting 状态且无法证明未下单；禁止自动重试，请人工核对 QMT"
        )
        raw["updated_at"] = iso_now()
    ledger["updated_at"] = iso_now()
    return ledger


def update_monitor_heartbeat(ledger: dict[str, Any], trade_date: str, equity: float) -> dict[str, Any]:
    previous_date = ledger.get("monitor_trade_date")
    if previous_date != trade_date:
        if previous_date is not None:
            # The last observed equity of the previous session is the next
            # session's daily-loss baseline. Using today's first quote would
            # erase an overnight gap before risk evaluation.
            ledger["previous_equity"] = float(
                ledger.get("last_monitor_equity", ledger.get("previous_equity", equity))
            )
            cooldown = int(ledger.get("cooldown_remaining", 0))
            if cooldown > 0 and ledger.get("cooldown_started_date") != trade_date:
                ledger["cooldown_remaining"] = cooldown - 1
        else:
            ledger["previous_equity"] = float(equity)
        ledger["monitor_trade_date"] = trade_date
    ledger["last_monitor_equity"] = float(equity)
    ledger["last_monitor_at"] = iso_now()
    ledger["updated_at"] = iso_now()
    return ledger


def evaluate_live_risk(
    ledger: dict[str, Any],
    prices: Mapping[str, float],
    risk: Mapping[str, Any],
    trade_date: str,
) -> tuple[dict[str, Any], dict[str, str], float]:
    positions = ledger.get("positions", {})
    missing = sorted(set(positions).difference(prices))
    if missing:
        raise ValueError(f"持仓缺少有效实时价格: {missing}")
    # Once a risk exit fires it remains latched until reconciliation proves the
    # position is gone. A transient price rebound or a canceled order must not
    # silently disarm the protection.
    exits = {
        str(symbol): str(reason)
        for symbol, reason in ledger.get("pending_risk_exits", {}).items()
        if symbol in positions
    }
    exit_dates = {
        str(symbol): str(value)
        for symbol, value in ledger.get("pending_risk_exit_dates", {}).items()
        if symbol in positions
    }
    market_value = 0.0
    for symbol, raw in positions.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"持仓 {symbol} 缺少成本和 ATR 状态，无法执行保护性止损")
        quantity = int(raw.get("quantity", 0))
        price = float(prices[symbol])
        entry = float(raw.get("average_cost", 0.0))
        atr_value = float(raw.get("atr_at_entry", 0.0))
        if quantity <= 0 or price <= 0 or entry <= 0 or atr_value <= 0:
            raise ValueError(f"持仓 {symbol} 的数量/价格/成本/ATR 状态无效")
        high = max(float(raw.get("high_watermark", entry)), price)
        raw["high_watermark"] = high
        minimum_distance = float(risk.get("minimum_stop_distance", 0.0)) * entry
        initial_stop = entry - max(float(risk["initial_stop_atr"]) * atr_value, minimum_distance)
        stop = initial_stop
        if high - entry >= float(risk["trailing_activation_atr"]) * atr_value:
            trailing_distance = max(float(risk["trailing_stop_atr"]) * atr_value, minimum_distance)
            stop = max(stop, high - trailing_distance)
        raw["active_stop"] = stop
        if price <= stop:
            exits[symbol] = "trailing_stop" if stop > initial_stop else "initial_stop"
            exit_dates.setdefault(symbol, trade_date)
        market_value += quantity * price

    equity = float(ledger.get("cash", 0.0)) + market_value
    peak = max(float(ledger.get("peak_equity", equity)), equity)
    previous = max(float(ledger.get("previous_equity", equity)), 1e-12)
    drawdown = 1.0 - equity / max(peak, 1e-12)
    daily_return = equity / previous - 1.0
    portfolio_exit_already_latched = any(
        str(reason).startswith("portfolio_") for reason in exits.values()
    )
    if not exits and drawdown >= float(risk["hard_drawdown"]):
        exits = {symbol: "portfolio_hard_drawdown" for symbol in positions}
        for symbol in positions:
            exit_dates.setdefault(symbol, trade_date)
        ledger["cooldown_remaining"] = max(
            int(ledger.get("cooldown_remaining", 0)), int(risk["hard_cooldown_days"])
        )
        ledger["cooldown_started_date"] = trade_date
        ledger["peak_equity"] = equity
        ledger["last_risk_trigger"] = "hard_drawdown"
    elif not exits and daily_return <= -float(risk["daily_loss_limit"]):
        exits = {symbol: "portfolio_daily_loss" for symbol in positions}
        for symbol in positions:
            exit_dates.setdefault(symbol, trade_date)
        ledger["cooldown_remaining"] = max(
            int(ledger.get("cooldown_remaining", 0)), int(risk["daily_loss_cooldown_days"])
        )
        ledger["cooldown_started_date"] = trade_date
        ledger["last_risk_trigger"] = "daily_loss"
        ledger["peak_equity"] = peak
    elif not portfolio_exit_already_latched:
        ledger["peak_equity"] = peak
    ledger["pending_risk_exits"] = exits
    ledger["pending_risk_exit_dates"] = exit_dates
    ledger["last_equity"] = equity
    ledger["last_drawdown"] = drawdown
    ledger["updated_at"] = iso_now()
    return ledger, exits, equity


def finalize_live_day(ledger: dict[str, Any], equity: float, trade_date: str) -> dict[str, Any]:
    if ledger.get("last_finalized_date") == trade_date:
        return ledger
    cooldown = int(ledger.get("cooldown_remaining", 0))
    if cooldown > 0 and ledger.get("cooldown_started_date") != trade_date:
        ledger["cooldown_remaining"] = cooldown - 1
    ledger["previous_equity"] = float(equity)
    ledger["last_finalized_date"] = trade_date
    ledger["updated_at"] = iso_now()
    return ledger
