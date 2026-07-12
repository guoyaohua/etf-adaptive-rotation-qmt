from __future__ import annotations

from typing import Mapping, Any

import pandas as pd


def is_rebalance_date(date: pd.Timestamp, params: Mapping[str, Any]) -> bool:
    date = pd.Timestamp(date).normalize()
    weekday_due = date.weekday() == int(params["rebalance_weekday"])
    if not weekday_due:
        return False
    schedule = str(params.get("rebalance_schedule", "fixed_weeks"))
    if schedule == "staggered_weeks":
        return True
    if schedule == "month_end":
        return (date + pd.Timedelta(days=7)).month != date.month
    if schedule == "fixed_weeks":
        interval = int(params.get("rebalance_interval_weeks", 1))
        phase = int(params.get("rebalance_phase_weeks", 0))
        if interval <= 0 or not 0 <= phase < interval:
            raise ValueError("固定周调度的 interval/phase 配置无效")
        week_index = int((date - pd.Timestamp("1970-01-05")).days // 7)
        return (week_index - phase) % interval == 0
    raise ValueError(f"未知再平衡计划: {schedule}")


def scheduled_dates(
    calendar: pd.DatetimeIndex,
    as_of: str | pd.Timestamp,
    params: Mapping[str, Any],
    count: int | None = None,
) -> list[pd.Timestamp]:
    cutoff = pd.Timestamp(as_of).normalize()
    eligible = [pd.Timestamp(date) for date in calendar if pd.Timestamp(date) <= cutoff and is_rebalance_date(date, params)]
    if not eligible:
        raise ValueError(f"{cutoff.date()} 之前没有满足调度规则的决策日")
    if count is None:
        count = int(params.get("rebalance_sleeves", 4)) if params.get("rebalance_schedule") == "staggered_weeks" else 1
    return eligible[-int(count):]
