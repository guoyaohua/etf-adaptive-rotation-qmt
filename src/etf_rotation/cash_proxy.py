from __future__ import annotations

from datetime import date as date_type
from typing import Any, Mapping

import numpy as np
import pandas as pd


def _settings(config: Any) -> Mapping[str, Any]:
    settings = getattr(config, "cash_proxy", {})
    return settings if isinstance(settings, Mapping) else {}


def cash_proxy_symbol(config: Any) -> str | None:
    """Return the configured money-market ETF, if the policy is enabled."""

    settings = _settings(config)
    if not bool(settings.get("enabled", False)):
        return None
    symbol = str(settings.get("symbol", "")).strip()
    return symbol or None


def _month_day(value: str) -> tuple[int, int]:
    try:
        parsed = pd.Timestamp(f"2000-{value}")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效月日: {value}") from exc
    return int(parsed.month), int(parsed.day)


def is_cash_proxy_blackout(config: Any, value: str | pd.Timestamp | date_type) -> bool:
    """Whether *value* is inside the inclusive distribution-avoidance window."""

    if cash_proxy_symbol(config) is None:
        return False
    settings = _settings(config)
    start = _month_day(str(settings["blackout_start"]))
    end = _month_day(str(settings["blackout_end"]))
    timestamp = pd.Timestamp(value)
    current = (int(timestamp.month), int(timestamp.day))
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def blackout_symbols(config: Any, value: str | pd.Timestamp | date_type) -> set[str]:
    symbol = cash_proxy_symbol(config)
    return {symbol} if symbol is not None and is_cash_proxy_blackout(config, value) else set()


def constrain_cash_proxy_weights(
    config: Any, weights: Mapping[str, float], value: str | pd.Timestamp | date_type
) -> dict[str, float]:
    blocked = blackout_symbols(config, value)
    return {
        str(symbol): float(weight)
        for symbol, weight in weights.items()
        if symbol not in blocked and float(weight) > 0
    }


def _cash_proxy_signal_frame(
    frame: pd.DataFrame, settings: Mapping[str, Any]
) -> pd.DataFrame:
    """Attach a causal total-return close while preserving raw execution OHLC.

    The money-market ETF quote accrues above a configured par value and resets
    after its annual distribution.  The exact cash distribution is deliberately
    not booked: the strategy is forced flat across the reset window.  For signal
    continuity only, an observed reset is reconstructed from the previous close
    and par value.  This uses no future observation.
    """

    result = frame.copy()
    raw_close = result["close"].astype(float)
    raw_return = raw_close.pct_change(fill_method=None)
    threshold = float(settings["reset_return_threshold"])
    reset_mask = raw_return <= threshold
    reset_dates = list(pd.DatetimeIndex(result.index[reset_mask]))

    unexpected = [
        item.date().isoformat()
        for item in reset_dates
        if not _date_in_window(
            item,
            str(settings["reset_window_start"]),
            str(settings["reset_window_end"]),
        )
    ]
    if unexpected:
        raise ValueError(
            "货币 ETF 在允许的年末复位窗口外出现异常价格复位，拒绝生成信号: "
            + ", ".join(unexpected)
        )

    anchor = float(settings["reset_anchor_price"])
    tolerance = float(settings["reset_price_tolerance"])
    adjusted_return = raw_return.copy()
    adjustments: list[dict[str, float | str]] = []
    for reset_date in reset_dates:
        previous = float(raw_close.shift(1).loc[reset_date])
        current = float(raw_close.loc[reset_date])
        if (
            not np.isfinite(previous)
            or not np.isfinite(current)
            or previous <= anchor
            or abs(current - anchor) > tolerance
        ):
            raise ValueError(
                f"货币 ETF 疑似复位但价格不符合面值重置约束: "
                f"{reset_date.date()} previous={previous:g}, current={current:g}"
            )
        estimated_distribution = previous - anchor
        reconstructed = (current + estimated_distribution) / previous - 1.0
        adjusted_return.loc[reset_date] = reconstructed
        adjustments.append(
            {
                "date": reset_date.date().isoformat(),
                "raw_return": float(raw_return.loc[reset_date]),
                "signal_return": float(reconstructed),
                "estimated_distribution": float(estimated_distribution),
            }
        )

    signal_close = anchor * (1.0 + adjusted_return.fillna(0.0)).cumprod()
    result["signal_close"] = signal_close
    result.attrs.update(frame.attrs)
    result.attrs["cash_proxy_adjustments"] = adjustments
    result.attrs["cash_proxy_signal_mode"] = str(settings["signal_mode"])
    return result


def _date_in_window(
    value: str | pd.Timestamp | date_type, start_value: str, end_value: str
) -> bool:
    start = _month_day(start_value)
    end = _month_day(end_value)
    timestamp = pd.Timestamp(value)
    current = (int(timestamp.month), int(timestamp.day))
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def prepare_signal_data(
    config: Any, data: Mapping[str, pd.DataFrame]
) -> dict[str, pd.DataFrame]:
    """Return signal frames without changing raw OHLC execution data."""

    result = dict(data)
    symbol = cash_proxy_symbol(config)
    if symbol is None:
        return result
    if symbol not in data:
        raise ValueError(f"现金代理缺少必需行情: {symbol}")
    settings = _settings(config)
    if str(settings.get("signal_mode", "")) != "par_reset":
        raise ValueError("cash_proxy.signal_mode 当前只支持 par_reset")
    result[symbol] = _cash_proxy_signal_frame(data[symbol], settings)
    return result


def signal_adjustment_audit(
    config: Any, data: Mapping[str, pd.DataFrame]
) -> dict[str, Any]:
    symbol = cash_proxy_symbol(config)
    if symbol is None:
        return {"enabled": False}
    prepared = prepare_signal_data(config, data)
    frame = prepared[symbol]
    return {
        "enabled": True,
        "symbol": symbol,
        "signal_mode": frame.attrs.get("cash_proxy_signal_mode"),
        "blackout_start": str(_settings(config)["blackout_start"]),
        "blackout_end": str(_settings(config)["blackout_end"]),
        "reset_window_start": str(_settings(config)["reset_window_start"]),
        "reset_window_end": str(_settings(config)["reset_window_end"]),
        "adjustments": list(frame.attrs.get("cash_proxy_adjustments", [])),
    }
