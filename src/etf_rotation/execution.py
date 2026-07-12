from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str
    quantity: int
    reference_price: float
    fill_price: float
    commission: float
    slippage: float
    reason: str

    @property
    def notional(self) -> float:
        return self.quantity * self.fill_price


class CostModel:
    def __init__(
        self,
        commission_rate: float,
        minimum_commission: float,
        slippage_rate: float,
        multiplier: float = 1.0,
    ):
        self.commission_rate = float(commission_rate) * float(multiplier)
        self.minimum_commission = float(minimum_commission) * float(multiplier)
        self.slippage_rate = float(slippage_rate) * float(multiplier)

    def fill(self, symbol: str, side: str, quantity: int, price: float, reason: str) -> Fill:
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"未知方向: {side}")
        if quantity <= 0 or price <= 0:
            raise ValueError("成交数量和价格必须为正")
        direction = 1.0 if side == "BUY" else -1.0
        fill_price = float(price) * (1.0 + direction * self.slippage_rate)
        commission = max(self.minimum_commission, quantity * fill_price * self.commission_rate)
        slippage = abs(fill_price - float(price)) * quantity
        return Fill(symbol, side, int(quantity), float(price), fill_price, commission, slippage, reason)


def round_lot(quantity: float, lot_size: int) -> int:
    if quantity <= 0:
        return 0
    return int(quantity // lot_size) * lot_size


def target_quantities(
    weights: Mapping[str, float],
    equity: float,
    prices: Mapping[str, float],
    lot_size: int,
) -> dict[str, int]:
    result: dict[str, int] = {}
    for symbol, weight in weights.items():
        price = prices.get(symbol)
        if price is None or price <= 0 or weight <= 0:
            continue
        result[symbol] = round_lot(float(equity) * float(weight) / price, lot_size)
    return result
