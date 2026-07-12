from __future__ import annotations

import json
import os
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .config import AppConfig
from .execution import target_quantities


@dataclass(frozen=True)
class PlannedOrder:
    symbol: str
    side: str
    quantity: int
    price: float
    reason: str


def build_order_plan(
    target_weights: Mapping[str, float],
    current_positions: Mapping[str, int],
    prices: Mapping[str, float],
    total_asset: float,
    lot_size: int = 100,
    min_weight_change: float = 0.0,
    sellable_positions: Mapping[str, int] | None = None,
) -> list[PlannedOrder]:
    desired = target_quantities(target_weights, total_asset, prices, lot_size)
    orders: list[PlannedOrder] = []
    all_symbols = set(current_positions).union(desired)
    for symbol in all_symbols:
        price = prices.get(symbol)
        if price is None or price <= 0:
            continue
        current = int(current_positions.get(symbol, 0))
        target = int(desired.get(symbol, 0))
        delta = target - current
        if abs(delta * price) / max(total_asset, 1e-12) < min_weight_change:
            continue
        if delta < 0:
            sellable = current if sellable_positions is None else int(sellable_positions.get(symbol, 0))
            quantity = min(-delta, current, sellable)
            if quantity > 0:
                orders.append(PlannedOrder(symbol, "SELL", quantity, price, "target_rebalance"))
        elif delta > 0:
            orders.append(PlannedOrder(symbol, "BUY", delta, price, "target_rebalance"))
    return sorted(orders, key=lambda item: (item.side != "SELL", item.symbol))


class QmtBroker:
    """QMT adapter with three independent live-order safety locks."""

    CONFIRMATION = "LIVE_ETF_RR"

    def __init__(self, config: AppConfig):
        self.config = config
        client_key = str(config.qmt["client_path_env"])
        account_key = str(config.qmt["account_id_env"])
        self.client_path = os.environ.get(client_key)
        self.account_id = os.environ.get(account_key)
        if not self.client_path or not self.account_id:
            raise RuntimeError(f"请设置环境变量 {client_key} 和 {account_key}")
        try:
            from xtquant import xtconstant, xtdata
            from xtquant.xttrader import XtQuantTrader
            from xtquant.xttype import StockAccount
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("当前 Python 环境没有 xtquant") from exc
        self.xtconstant = xtconstant
        self.xtdata = xtdata
        self.account = StockAccount(self.account_id)
        self.trader = XtQuantTrader(self.client_path, random.randint(100000, 999999999))
        self.trader.start()
        connect = self.trader.connect()
        if connect != 0:
            raise RuntimeError(f"连接 QMT 交易端失败，错误码: {connect}")
        subscribe = self.trader.subscribe(self.account)
        if subscribe != 0:
            raise RuntimeError(f"订阅 QMT 账户失败，错误码: {subscribe}")

    def snapshot(
        self,
        owned_positions: Mapping[str, int] | None = None,
    ) -> tuple[Any, dict[str, int], dict[str, int], dict[str, int], dict[str, float]]:
        asset = self.trader.query_stock_asset(self.account)
        if asset is None:
            raise RuntimeError("QMT 查询资产失败")
        positions = self.trader.query_stock_positions(self.account) or []
        account_quantities = {
            item.stock_code: int(item.volume)
            for item in positions
            if item.stock_code in self.config.symbols
        }
        account_sellable = {
            item.stock_code: int(getattr(item, "can_use_volume", 0))
            for item in positions
            if item.stock_code in self.config.symbols
        }
        if owned_positions is None:
            # Read-only planning must not assume that same-code manual holdings
            # belong to this strategy. With no ledger, buys may be planned but
            # no existing account position is eligible for sale.
            strategy_quantities: dict[str, int] = {}
        else:
            strategy_quantities = {
                symbol: min(int(quantity), account_quantities.get(symbol, 0))
                for symbol, quantity in owned_positions.items()
                if int(quantity) > 0 and account_quantities.get(symbol, 0) > 0
            }
        ticks = self.xtdata.get_full_tick(self.config.symbols)
        prices: dict[str, float] = {}
        for symbol, tick in ticks.items():
            quote_time = tick.get("time", 0)
            if quote_time:
                quote_seconds = float(quote_time) / 1000.0 if float(quote_time) > 10_000_000_000 else float(quote_time)
                age = time.time() - quote_seconds
                if age > float(self.config.execution["max_quote_age_seconds"]):
                    continue
            ask = tick.get("askPrice", [0])[0]
            bid = tick.get("bidPrice", [0])[0]
            last = tick.get("lastPrice", 0)
            prices[symbol] = float(ask or bid or last)
        return asset, account_quantities, account_sellable, strategy_quantities, prices

    def execute(
        self,
        orders: list[PlannedOrder],
        confirmation: str,
    ) -> list[int]:
        if not bool(self.config.qmt.get("allow_live_orders", False)):
            raise PermissionError("配置 qmt.allow_live_orders=false，禁止真实下单")
        if confirmation != self.CONFIRMATION:
            raise PermissionError("真实下单确认短语不匹配")
        pending = self.trader.query_stock_orders(self.account, cancelable_only=True) or []
        pending_symbols = {item.stock_code for item in pending}
        order_ids: list[int] = []
        for order in orders:
            if order.symbol in pending_symbols:
                raise RuntimeError(f"{order.symbol} 已有在途委托，拒绝重复下单")
            side = self.xtconstant.STOCK_BUY if order.side == "BUY" else self.xtconstant.STOCK_SELL
            price_type = (
                self.xtconstant.MARKET_SH_CONVERT_5_CANCEL
                if order.symbol.endswith(".SH")
                else self.xtconstant.MARKET_SZ_CONVERT_5_CANCEL
            )
            order_id = self.trader.order_stock(
                self.account,
                order.symbol,
                side,
                order.quantity,
                price_type,
                order.price,
                str(self.config.execution["strategy_tag"]),
                order.reason,
            )
            if order_id <= 0:
                raise RuntimeError(f"{order.symbol} 下单失败: {order_id}")
            order_ids.append(int(order_id))
        return order_ids


def save_plan(orders: list[PlannedOrder], output: str | Path, metadata: Mapping[str, Any]) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "orders": [asdict(order) for order in orders],
        "metadata": dict(metadata),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load_owned_positions(path: str | Path) -> dict[str, int]:
    ledger = Path(path)
    if not ledger.exists():
        return {}
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    positions = payload.get("positions", payload)
    if not isinstance(positions, dict):
        raise ValueError("策略持仓账本格式错误")
    return {str(symbol): int(quantity) for symbol, quantity in positions.items() if int(quantity) > 0}
