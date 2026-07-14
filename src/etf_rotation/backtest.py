from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from .config import AppConfig
from .execution import CostModel, Fill, target_quantities
from .risk import PortfolioRiskController, PositionRiskState, StopEngine
from .schedule import is_rebalance_date
from .strategy import RegimeRotationStrategy, TargetPortfolio


@dataclass
class BacktestResult:
    equity: pd.DataFrame
    fills: pd.DataFrame
    targets: pd.DataFrame
    metrics: dict[str, float | int | str | bool]


class Backtester:
    def __init__(self, config: AppConfig, cost_multiplier: float = 1.0):
        self.config = config
        self.strategy = RegimeRotationStrategy(config)
        execution = config.execution
        self.cost_model = CostModel(
            execution["commission_rate"],
            execution["minimum_commission"],
            execution["slippage_rate"],
            multiplier=cost_multiplier,
        )
        risk = config.risk
        self.stop_engine = StopEngine(
            risk["initial_stop_atr"],
            risk["trailing_activation_atr"],
            risk["trailing_stop_atr"],
            risk.get("minimum_stop_distance", 0.0),
        )
        self.cost_multiplier = float(cost_multiplier)

    @staticmethod
    def _calendar(data: Mapping[str, pd.DataFrame]) -> pd.DatetimeIndex:
        indices = [frame.index for frame in data.values() if not frame.empty]
        if not indices:
            raise ValueError("回测行情为空")
        calendar = indices[0]
        for index in indices[1:]:
            calendar = calendar.union(index)
        return calendar.sort_values().unique()

    @staticmethod
    def _bar(data: Mapping[str, pd.DataFrame], symbol: str, date: pd.Timestamp) -> pd.Series | None:
        frame = data.get(symbol)
        if frame is None or date not in frame.index:
            return None
        row = frame.loc[date]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        return row

    @staticmethod
    def _mark_value(
        cash: float,
        positions: Mapping[str, int],
        prices: Mapping[str, float],
    ) -> float:
        return float(cash + sum(quantity * prices.get(symbol, 0.0) for symbol, quantity in positions.items()))

    def _record_fill(
        self,
        records: list[dict],
        date: pd.Timestamp,
        fill: Fill,
    ) -> None:
        record = asdict(fill)
        record["date"] = date
        record["cost_multiplier"] = self.cost_multiplier
        records.append(record)

    def run(self, data: Mapping[str, pd.DataFrame]) -> BacktestResult:
        execution = self.config.execution
        initial_capital = float(execution["initial_capital"])
        lot_size = int(execution["lot_size"])
        cash = initial_capital
        positions: dict[str, int] = {}
        position_risk: dict[str, PositionRiskState] = {}
        latest_close: dict[str, float] = {}
        pending_target: TargetPortfolio | None = None
        sleeve_targets: list[TargetPortfolio] = []
        fills: list[dict] = []
        targets: list[dict] = []
        equity_rows: list[dict] = []
        calendar = self._calendar(data)
        risk_config = self.config.risk
        risk_controller = PortfolioRiskController(
            initial_capital,
            risk_config["soft_drawdown"],
            risk_config["soft_drawdown_scale"],
            risk_config["hard_drawdown"],
            risk_config["hard_cooldown_days"],
            risk_config["daily_loss_limit"],
            risk_config["daily_loss_cooldown_days"],
        )

        for date in calendar:
            bars = {symbol: self._bar(data, symbol, date) for symbol in self.config.symbols}
            bars = {symbol: row for symbol, row in bars.items() if row is not None}
            if not bars:
                continue
            open_prices = {symbol: float(row["open"]) for symbol, row in bars.items() if row["open"] > 0}
            close_prices = {symbol: float(row["close"]) for symbol, row in bars.items() if row["close"] > 0}
            latest_close.update(close_prices)

            force_liquidate = risk_controller.begin_day()
            if force_liquidate:
                for symbol in sorted(list(positions)):
                    price = open_prices.get(symbol)
                    if price is None:
                        continue
                    fill = self.cost_model.fill(symbol, "SELL", positions[symbol], price, "portfolio_circuit_breaker")
                    cash += fill.notional - fill.commission
                    self._record_fill(fills, date, fill)
                    positions.pop(symbol, None)
                    position_risk.pop(symbol, None)
                if positions:
                    # Suspended/missing-open instruments must be retried instead
                    # of silently surviving the circuit breaker.
                    risk_controller.state.liquidate_next_open = True
                pending_target = None

            # Opening gaps are observable before the scheduled rebalance.  A
            # position that has already crossed its stop must be exited before
            # any target increase is applied, and it must not be bought back at
            # the same opening print.  The old order (rebalance first, stop
            # second) could add to a losing position and immediately liquidate
            # the enlarged quantity at that identical open, an impossible and
            # cost-distorting path.
            opening_stop_symbols: set[str] = set()
            if not force_liquidate:
                for symbol in sorted(list(positions)):
                    row = bars.get(symbol)
                    state = position_risk.get(symbol)
                    if row is None or state is None:
                        continue
                    day_open = float(row["open"])
                    if day_open > self.stop_engine.stop_price(state):
                        continue
                    fill = self.cost_model.fill(symbol, "SELL", positions[symbol], day_open, "gap_stop")
                    cash += fill.notional - fill.commission
                    self._record_fill(fills, date, fill)
                    positions.pop(symbol, None)
                    position_risk.pop(symbol, None)
                    opening_stop_symbols.add(symbol)

            # Rebalance at the next available open after a completed decision day.
            if pending_target is not None and not force_liquidate:
                equity_at_open = self._mark_value(cash, positions, {**latest_close, **open_prices})
                scale = risk_controller.exposure_scale(equity_at_open)
                scaled_weights = {symbol: weight * scale for symbol, weight in pending_target.weights.items()}
                desired = target_quantities(scaled_weights, equity_at_open, open_prices, lot_size)
                all_symbols = set(positions).union(desired)
                block_new_buys = bool(set(positions).difference(open_prices))
                min_change = float(self.config.strategy["min_weight_change"])
                # A no-trade band avoids paying spread/commission merely to chase
                # small inverse-volatility weight changes every week. Full exits
                # and new entries remain actionable.
                for symbol in list(all_symbols):
                    current = positions.get(symbol, 0)
                    target_quantity = desired.get(symbol, 0)
                    price = open_prices.get(symbol)
                    if price is None or current == 0 or target_quantity == 0:
                        continue
                    change_weight = abs(target_quantity - current) * price / max(equity_at_open, 1e-12)
                    if change_weight < min_change:
                        desired[symbol] = current

                # Sells are processed before buys, and only the strategy-owned quantity is touched.
                for symbol in sorted(all_symbols):
                    current = positions.get(symbol, 0)
                    target = desired.get(symbol, 0)
                    delta = target - current
                    price = open_prices.get(symbol)
                    if delta >= 0 or price is None:
                        continue
                    quantity = min(-delta, current)
                    fill = self.cost_model.fill(symbol, "SELL", quantity, price, "scheduled_rebalance")
                    cash += fill.notional - fill.commission
                    positions[symbol] = current - quantity
                    self._record_fill(fills, date, fill)
                    if positions[symbol] <= 0:
                        positions.pop(symbol, None)
                        position_risk.pop(symbol, None)

                for symbol in sorted(all_symbols):
                    if block_new_buys:
                        break
                    if symbol in opening_stop_symbols:
                        continue
                    current = positions.get(symbol, 0)
                    target = desired.get(symbol, 0)
                    delta = target - current
                    price = open_prices.get(symbol)
                    if delta <= 0 or price is None:
                        continue
                    affordable = int(max(0.0, cash - self.cost_model.minimum_commission) / (price * (1 + self.cost_model.slippage_rate)))
                    affordable = (affordable // lot_size) * lot_size
                    quantity = min(delta, affordable)
                    if quantity <= 0:
                        continue
                    fill = self.cost_model.fill(symbol, "BUY", quantity, price, "scheduled_rebalance")
                    total = fill.notional + fill.commission
                    if total > cash + 1e-8:
                        continue
                    cash -= total
                    positions[symbol] = current + quantity
                    self._record_fill(fills, date, fill)
                    signal = pending_target.signals.get(symbol)
                    atr_value = signal.atr if signal is not None else np.nan
                    if np.isfinite(atr_value):
                        previous = position_risk.get(symbol)
                        if previous is None:
                            position_risk[symbol] = PositionRiskState(fill.fill_price, float(atr_value), fill.fill_price)
                        else:
                            combined_quantity = current + quantity
                            average = (previous.entry_price * current + fill.fill_price * quantity) / combined_quantity
                            position_risk[symbol] = PositionRiskState(average, previous.atr_at_entry, max(previous.high_watermark, fill.fill_price))
                pending_target = None

            # Opening gaps were handled before rebalancing.  The remaining OHLC
            # check models an intraday touch at the stop price.
            for symbol in sorted(list(positions)):
                row = bars.get(symbol)
                state = position_risk.get(symbol)
                if row is None or state is None:
                    continue
                exit_reference, reason = self.stop_engine.exit_price(
                    state, float(row["open"]), float(row["low"])
                )
                if exit_reference is not None:
                    fill = self.cost_model.fill(symbol, "SELL", positions[symbol], exit_reference, reason or "risk_stop")
                    cash += fill.notional - fill.commission
                    self._record_fill(fills, date, fill)
                    positions.pop(symbol, None)
                    position_risk.pop(symbol, None)
                else:
                    state.high_watermark = max(state.high_watermark, float(row["high"]))

            equity = self._mark_value(cash, positions, latest_close)
            gross = sum(positions.get(symbol, 0) * latest_close.get(symbol, 0.0) for symbol in positions)
            risk_controller.end_day(equity)
            equity_rows.append(
                {
                    "date": date,
                    "equity": equity,
                    "cash": cash,
                    "gross_exposure": gross / equity if equity > 0 else 0.0,
                    "positions": len(positions),
                    "cooldown": risk_controller.state.cooldown_remaining,
                }
            )

            # Weekly signal is generated after close and can only affect a later session.
            schedule = str(self.config.strategy.get("rebalance_schedule", "fixed_weeks"))
            due = is_rebalance_date(date, self.config.strategy)
            if due:
                target = self.strategy.target(data, date)
                if schedule == "staggered_weeks":
                    sleeve_count = int(self.config.strategy.get("rebalance_sleeves", 4))
                    sleeve_targets.append(target)
                    sleeve_targets = sleeve_targets[-sleeve_count:]
                    target = self.strategy.aggregate_targets(sleeve_targets, sleeve_count)
                # During a circuit-breaker cooldown, any pending risk-on target is
                # discarded. It will be recomputed from fresh data after cooldown.
                pending_target = target if risk_controller.state.cooldown_remaining == 0 else None
                targets.append(
                    {
                        "date": date,
                        "regime": target.regime,
                        "weights": target.weights,
                        **target.diagnostics,
                    }
                )

        equity_frame = pd.DataFrame(equity_rows).set_index("date") if equity_rows else pd.DataFrame()
        fill_frame = pd.DataFrame(fills)
        target_frame = pd.DataFrame(targets)
        metrics = calculate_metrics(equity_frame, fill_frame, initial_capital)
        metrics["cost_multiplier"] = self.cost_multiplier
        return BacktestResult(equity_frame, fill_frame, target_frame, metrics)


def calculate_metrics(
    equity: pd.DataFrame,
    fills: pd.DataFrame,
    initial_capital: float,
) -> dict[str, float | int | str | bool]:
    if equity.empty:
        return {"valid": False, "reason": "no_equity_rows"}
    series = equity["equity"].astype(float)
    returns = series.pct_change(fill_method=None).dropna()
    elapsed_days = max((series.index[-1] - series.index[0]).days, 1)
    years = elapsed_days / 365.25
    total_return = float(series.iloc[-1] / initial_capital - 1.0)
    cagr = float((series.iloc[-1] / initial_capital) ** (1.0 / years) - 1.0) if years > 0 else np.nan
    running_max = series.cummax()
    drawdown = series / running_max - 1.0
    max_drawdown = float(-drawdown.min())
    volatility = float(returns.std(ddof=1) * np.sqrt(252)) if len(returns) > 1 else np.nan
    sharpe = float(returns.mean() / returns.std(ddof=1) * np.sqrt(252)) if len(returns) > 1 and returns.std(ddof=1) > 0 else np.nan
    downside = returns[returns < 0].std(ddof=1)
    sortino = float(returns.mean() / downside * np.sqrt(252)) if np.isfinite(downside) and downside > 0 else np.nan
    calmar = float(cagr / max_drawdown) if max_drawdown > 0 else np.nan
    # Group explicitly by calendar year for compatibility across pandas 2.x
    # (the year-end alias changed from "A" to "YE" in newer releases).
    year_ends = series.groupby(series.index.year).last()
    annual = year_ends.pct_change(fill_method=None)
    if len(annual):
        annual.iloc[0] = float(year_ends.iloc[0] / initial_capital - 1.0)
    positive_year_ratio = float((annual > 0).mean()) if len(annual) else np.nan
    return {
        "valid": True,
        "start": str(series.index[0].date()),
        "end": str(series.index[-1].date()),
        "years": years,
        "final_equity": float(series.iloc[-1]),
        "total_return": total_return,
        "cagr": cagr,
        "annualized_volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
        "positive_year_ratio": positive_year_ratio,
        "fills": int(len(fills)),
        "commission": float(fills["commission"].sum()) if not fills.empty else 0.0,
        "slippage": float(fills["slippage"].sum()) if not fills.empty else 0.0,
    }
