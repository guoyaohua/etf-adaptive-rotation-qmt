from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PositionRiskState:
    entry_price: float
    atr_at_entry: float
    high_watermark: float


class StopEngine:
    def __init__(
        self,
        initial_stop_atr: float,
        trailing_activation_atr: float,
        trailing_stop_atr: float,
    ):
        self.initial_stop_atr = float(initial_stop_atr)
        self.trailing_activation_atr = float(trailing_activation_atr)
        self.trailing_stop_atr = float(trailing_stop_atr)

    def stop_price(self, state: PositionRiskState) -> float:
        initial = state.entry_price - self.initial_stop_atr * state.atr_at_entry
        gain = state.high_watermark - state.entry_price
        if gain < self.trailing_activation_atr * state.atr_at_entry:
            return initial
        trailing = state.high_watermark - self.trailing_stop_atr * state.atr_at_entry
        return max(initial, trailing)

    def exit_price(
        self,
        state: PositionRiskState,
        day_open: float,
        day_low: float,
    ) -> tuple[float | None, str | None]:
        stop = self.stop_price(state)
        if day_open <= stop:
            return day_open, "gap_stop"
        if day_low <= stop:
            reason = "trailing_stop" if stop > state.entry_price - self.initial_stop_atr * state.atr_at_entry else "initial_stop"
            return stop, reason
        return None, None


@dataclass
class PortfolioRiskState:
    peak_equity: float
    previous_equity: float
    cooldown_remaining: int = 0
    liquidate_next_open: bool = False
    last_trigger: str | None = None


class PortfolioRiskController:
    def __init__(
        self,
        initial_equity: float,
        soft_drawdown: float,
        soft_drawdown_scale: float,
        hard_drawdown: float,
        hard_cooldown_days: int,
        daily_loss_limit: float,
        daily_loss_cooldown_days: int,
    ):
        self.soft_drawdown = float(soft_drawdown)
        self.soft_drawdown_scale = float(soft_drawdown_scale)
        self.hard_drawdown = float(hard_drawdown)
        self.hard_cooldown_days = int(hard_cooldown_days)
        self.daily_loss_limit = float(daily_loss_limit)
        self.daily_loss_cooldown_days = int(daily_loss_cooldown_days)
        self.state = PortfolioRiskState(float(initial_equity), float(initial_equity))

    def begin_day(self) -> bool:
        liquidate = self.state.liquidate_next_open
        self.state.liquidate_next_open = False
        return liquidate

    def exposure_scale(self, equity: float) -> float:
        if self.state.cooldown_remaining > 0:
            return 0.0
        drawdown = 1.0 - float(equity) / max(self.state.peak_equity, 1e-12)
        return self.soft_drawdown_scale if drawdown >= self.soft_drawdown else 1.0

    def end_day(self, equity: float) -> None:
        equity = float(equity)
        if self.state.cooldown_remaining > 0:
            self.state.cooldown_remaining -= 1
        drawdown = 1.0 - equity / max(self.state.peak_equity, 1e-12)
        daily_return = equity / max(self.state.previous_equity, 1e-12) - 1.0
        hard_triggered = drawdown >= self.hard_drawdown
        if hard_triggered:
            self.state.liquidate_next_open = True
            self.state.cooldown_remaining = max(self.state.cooldown_remaining, self.hard_cooldown_days)
            self.state.last_trigger = "hard_drawdown"
        elif daily_return <= -self.daily_loss_limit:
            self.state.liquidate_next_open = True
            self.state.cooldown_remaining = max(self.state.cooldown_remaining, self.daily_loss_cooldown_days)
            self.state.last_trigger = "daily_loss"
        # Start a new risk epoch after a hard circuit breaker. Keeping the old
        # peak would retrigger the same breach every day after cooldown and
        # permanently freeze the strategy below its historic high watermark.
        self.state.peak_equity = equity if hard_triggered else max(self.state.peak_equity, equity)
        self.state.previous_equity = equity
