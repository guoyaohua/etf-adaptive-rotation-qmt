from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from .config import AppConfig
from .indicators import (
    annualized_volatility,
    atr,
    ema_slope,
    moving_average,
    portfolio_volatility,
    simple_return,
)
from .version import STRATEGY_VERSION


@dataclass(frozen=True)
class AssetSignal:
    symbol: str
    role: str
    group: str
    close: float
    average_amount: float
    momentum: float
    volatility: float
    score: float
    above_trend: bool
    positive_slope: bool
    atr: float
    eligible: bool


@dataclass(frozen=True)
class TargetPortfolio:
    decision_date: pd.Timestamp
    regime: str
    weights: dict[str, float]
    signals: dict[str, AssetSignal]
    diagnostics: dict[str, float | int | str]


class RegimeRotationStrategy:
    def __init__(self, config: AppConfig):
        self.config = config
        self.params = config.strategy
        self.instruments = config.instrument_by_symbol

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return int(
            max(
                max(p["momentum_lookbacks"]) + p["momentum_skip_days"] + 1,
                p["growth_trend_sma"],
                p["defensive_trend_sma"],
                p["fast_ema"] + p["fast_slope_days"],
                p["volatility_lookback"] + 1,
                p["correlation_lookback"] + 1,
            )
        )

    def _asset_signal(self, symbol: str, frame: pd.DataFrame) -> AssetSignal:
        p = self.params
        instrument = self.instruments[symbol]
        close = frame["close"].dropna()
        if close.empty:
            return AssetSignal(symbol, instrument.role, instrument.group, np.nan, 0.0, np.nan, np.nan, np.nan, False, False, np.nan, False)

        momenta = [
            simple_return(close, int(lookback), int(p["momentum_skip_days"]))
            for lookback in p["momentum_lookbacks"]
        ]
        momentum = (
            float(np.dot(momenta, p["momentum_weights"]))
            if all(np.isfinite(value) for value in momenta)
            else np.nan
        )
        volatility = annualized_volatility(close, int(p["volatility_lookback"]))
        trend_window = int(
            p["growth_trend_sma"] if instrument.role == "growth" else p["defensive_trend_sma"]
        )
        trend = moving_average(close, trend_window)
        slope = ema_slope(close, int(p["fast_ema"]), int(p["fast_slope_days"]))
        average_amount = float(frame["amount"].dropna().tail(int(p["liquidity_lookback"])).mean())
        last_close = float(close.iloc[-1])
        above_trend = bool(np.isfinite(trend) and last_close > trend)
        positive_slope = bool(np.isfinite(slope) and slope > 0)
        risk_floor = max(float(p["score_volatility_floor"]), volatility) if np.isfinite(volatility) else np.nan
        # Volatility is already used again by inverse-volatility position sizing.
        # A fractional exponent keeps ranking risk-aware without applying the
        # full penalty twice and systematically starving stronger equity trends.
        score_exponent = float(p.get("score_volatility_exponent", 1.0))
        score = (
            momentum / (risk_floor**score_exponent)
            if np.isfinite(momentum) and np.isfinite(risk_floor)
            else np.nan
        )
        atr_value = atr(frame, int(p["atr_lookback"]))
        eligible = bool(
            instrument.t0
            and len(frame) >= self.warmup_bars
            and last_close >= float(p["min_price"])
            and average_amount >= float(p["min_average_amount"])
            and np.isfinite(score)
            and np.isfinite(atr_value)
        )
        return AssetSignal(
            symbol=symbol,
            role=instrument.role,
            group=instrument.group,
            close=last_close,
            average_amount=average_amount,
            momentum=momentum,
            volatility=volatility,
            score=score,
            above_trend=above_trend,
            positive_slope=positive_slope,
            atr=atr_value,
            eligible=eligible,
        )

    def _regime(self, signals: Mapping[str, AssetSignal]) -> tuple[str, dict[str, float]]:
        growth = [item for item in signals.values() if item.role == "growth" and item.eligible]
        if not growth:
            return "risk_off", {"growth_breadth": 0.0, "growth_median_momentum": np.nan}
        breadth = float(np.mean([item.above_trend and item.positive_slope for item in growth]))
        median_momentum = float(np.median([item.momentum for item in growth]))
        risk_on = (
            breadth >= float(self.params["risk_on_breadth"])
            and median_momentum > float(self.params["risk_on_median_momentum"])
        )
        return ("risk_on" if risk_on else "risk_off"), {
            "growth_breadth": breadth,
            "growth_median_momentum": median_momentum,
        }

    def _select(
        self,
        signals: Mapping[str, AssetSignal],
        regime: str,
        returns: pd.DataFrame,
    ) -> list[str]:
        p = self.params
        unified = str(p.get("allocation_mode", "regime")) == "unified"
        role = "growth" if regime == "risk_on" else "defensive"
        count = int(p["selection_count"] if unified or regime == "risk_on" else p["defensive_selection_count"])
        candidates = [
            item
            for item in signals.values()
            if item.role != "cash"
            and (unified or item.role == role)
            and item.eligible
            and item.above_trend
            and item.positive_slope
            and item.momentum > float(p["min_weighted_momentum"])
        ]
        candidates.sort(key=lambda item: (item.score, item.average_amount), reverse=True)

        selected: list[str] = []
        used_groups: set[str] = set()
        correlation = returns.tail(int(p["correlation_lookback"])).corr(min_periods=20)
        for item in candidates:
            if item.group in used_groups:
                continue
            too_correlated = False
            for chosen in selected:
                if item.symbol in correlation.index and chosen in correlation.columns:
                    value = correlation.loc[item.symbol, chosen]
                    if np.isfinite(value) and value > float(p["max_pairwise_correlation"]):
                        too_correlated = True
                        break
            if too_correlated:
                continue
            selected.append(item.symbol)
            used_groups.add(item.group)
            if len(selected) >= count:
                break
        return selected

    def _weights(self, selected: list[str], returns: pd.DataFrame, signals: Mapping[str, AssetSignal]) -> dict[str, float]:
        p = self.params
        if selected:
            inverse_vol = pd.Series({symbol: 1.0 / signals[symbol].volatility for symbol in selected})
            weights = inverse_vol / inverse_vol.sum()
            caps = pd.Series(
                {
                    symbol: float(
                        p.get("max_defensive_weight", p["max_asset_weight"])
                        if signals[symbol].role == "defensive"
                        else p["max_asset_weight"]
                    )
                    for symbol in selected
                }
            )
            weights = weights.clip(upper=caps)
            if weights.sum() > 0:
                weights *= min(1.0, float(p["max_gross_exposure"]) / float(weights.sum()))
            realized = portfolio_volatility(returns.tail(int(p["correlation_lookback"])), weights)
            if realized > 0:
                scale = min(1.0, float(p["target_annual_volatility"]) / realized)
                weights *= scale
            weights = weights.clip(upper=caps)
        else:
            weights = pd.Series(dtype=float)

        # Put part of otherwise idle risk budget into a defensive cash proxy,
        # but only while that group remains above its long trend with positive
        # momentum.  The fast-slope filter is intentionally not required here:
        # this is a reserve allocation, not a primary momentum selection.
        proxy_group = str(p.get("idle_cash_proxy_group", "")).strip()
        proxy_cap = float(p.get("idle_cash_proxy_max_weight", 0.0))
        if proxy_group and proxy_cap > 0:
            proxy_candidates = [
                item
                for item in signals.values()
                if item.group == proxy_group
                and item.eligible
                and item.above_trend
                and item.momentum > float(p["min_weighted_momentum"])
            ]
            if proxy_candidates:
                held_proxy = [symbol for symbol in selected if signals[symbol].group == proxy_group]
                proxy = held_proxy[0] if held_proxy else max(proxy_candidates, key=lambda item: item.score).symbol
                instrument_cap = float(
                    p.get("max_defensive_weight", p["max_asset_weight"])
                    if signals[proxy].role == "defensive"
                    else p["max_asset_weight"]
                )
                available = max(0.0, float(p["max_gross_exposure"]) - float(weights.sum()))
                addition = min(available, max(0.0, min(proxy_cap, instrument_cap) - float(weights.get(proxy, 0.0))))
                if addition > 1e-6:
                    weights.loc[proxy] = float(weights.get(proxy, 0.0)) + addition
                    realized = portfolio_volatility(
                        returns.tail(int(p["correlation_lookback"])), weights
                    )
                    if realized > 0:
                        weights *= min(1.0, float(p["target_annual_volatility"]) / realized)

        gross = float(weights.sum())
        if gross > float(p["max_gross_exposure"]):
            weights *= float(p["max_gross_exposure"]) / gross
        return {symbol: float(value) for symbol, value in weights.items() if value > 1e-6}

    def target(
        self,
        data: Mapping[str, pd.DataFrame],
        decision_date: str | pd.Timestamp,
    ) -> TargetPortfolio:
        date = pd.Timestamp(decision_date)
        sliced = {symbol: frame.loc[frame.index <= date] for symbol, frame in data.items() if symbol in self.instruments}
        signals = {symbol: self._asset_signal(symbol, frame) for symbol, frame in sliced.items() if not frame.empty}
        regime, diagnostics = self._regime(signals)
        closes = pd.DataFrame({symbol: frame["close"] for symbol, frame in sliced.items()})
        returns = closes.pct_change(fill_method=None)
        selected = self._select(signals, regime, returns)
        weights = self._weights(selected, returns, signals)
        diagnostics.update(
            {
                "strategy_version": STRATEGY_VERSION,
                "selected_count": len(selected),
                "eligible_count": sum(item.eligible for item in signals.values()),
                "gross_exposure": float(sum(weights.values())),
            }
        )
        return TargetPortfolio(date, regime, weights, signals, diagnostics)

    @staticmethod
    def aggregate_targets(
        targets: list[TargetPortfolio],
        sleeve_count: int,
    ) -> TargetPortfolio:
        """Average staggered sleeves; uninitialized sleeves remain cash."""
        if not targets or sleeve_count <= 0:
            raise ValueError("集成目标不能为空且子组合数量必须为正")
        combined: dict[str, float] = {}
        for target in targets[-sleeve_count:]:
            for symbol, weight in target.weights.items():
                combined[symbol] = combined.get(symbol, 0.0) + float(weight) / sleeve_count
        latest = targets[-1]
        regimes = [target.regime for target in targets[-sleeve_count:]]
        diagnostics = dict(latest.diagnostics)
        diagnostics.update(
            {
                "sleeves_initialized": len(targets[-sleeve_count:]),
                "sleeve_count": sleeve_count,
                "gross_exposure": float(sum(combined.values())),
            }
        )
        regime = "ensemble_risk_on" if regimes.count("risk_on") >= max(1, len(regimes) / 2) else "ensemble_risk_off"
        return TargetPortfolio(latest.decision_date, regime, combined, latest.signals, diagnostics)
