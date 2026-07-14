from __future__ import annotations

from functools import lru_cache
import hashlib
from importlib.metadata import version as package_version
from typing import Mapping, Any

import exchange_calendars as xcals
import pandas as pd


_WEEKDAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def _normalized_index(values: pd.DatetimeIndex) -> pd.DatetimeIndex:
    result = pd.DatetimeIndex(values)
    if result.tz is not None:
        result = result.tz_localize(None)
    return result.normalize().sort_values().unique()


@lru_cache(maxsize=8)
def _exchange_calendar(name: str):
    calendar_name = str(name).strip()
    if not calendar_name:
        raise ValueError("交易所日历名称不能为空")
    try:
        return xcals.get_calendar(calendar_name)
    except Exception as exc:
        raise ValueError(f"无法加载交易所日历 {calendar_name}: {exc}") from exc


def exchange_calendar_bounds(name: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    calendar = _exchange_calendar(name)
    bounds = _normalized_index(pd.DatetimeIndex([calendar.first_session, calendar.last_session]))
    return pd.Timestamp(bounds[0]), pd.Timestamp(bounds[-1])


def exchange_sessions(
    name: str, start: str | pd.Timestamp, end: str | pd.Timestamp
) -> pd.DatetimeIndex:
    first, last = exchange_calendar_bounds(name)
    start_date = pd.Timestamp(start).normalize()
    end_date = pd.Timestamp(end).normalize()
    if end_date < start_date:
        return pd.DatetimeIndex([])
    if start_date < first or end_date > last:
        raise ValueError(
            f"交易所日历 {name} 仅覆盖 {first.date()} 至 {last.date()}，"
            f"请求区间为 {start_date.date()} 至 {end_date.date()}；请升级 exchange-calendars"
        )
    calendar = _exchange_calendar(name)
    return _normalized_index(calendar.sessions_in_range(start_date, end_date))


def is_exchange_session(date: str | pd.Timestamp, name: str) -> bool:
    value = pd.Timestamp(date).normalize()
    first, last = exchange_calendar_bounds(name)
    if value < first or value > last:
        raise ValueError(
            f"交易所日历 {name} 不覆盖 {value.date()}；请升级 exchange-calendars"
        )
    return bool(_exchange_calendar(name).is_session(value))


def compare_exchange_calendar(
    observed: pd.DatetimeIndex,
    name: str,
    completed_through: str | pd.Timestamp | None = None,
) -> dict[str, object]:
    dates = _normalized_index(observed)
    if dates.empty:
        raise ValueError("无法校验空行情日历")
    completed = (
        pd.Timestamp(completed_through).normalize()
        if completed_through is not None
        else pd.Timestamp(dates.max()).normalize()
    )
    checked = dates[dates <= completed]
    if checked.empty:
        raise ValueError("已确认完成日早于全部行情日期")
    expected = exchange_sessions(name, checked.min(), completed)
    missing = expected.difference(checked)
    extra = checked.difference(expected)
    digest = hashlib.sha256(
        "|".join(
            pd.Timestamp(date).date().isoformat() for date in expected
        ).encode("ascii")
    ).hexdigest()
    first, last = exchange_calendar_bounds(name)
    return {
        "name": str(name),
        "library_version": package_version("exchange-calendars"),
        "calendar_first_session": first.date().isoformat(),
        "calendar_last_session": last.date().isoformat(),
        "sessions_sha256": digest,
        "start": pd.Timestamp(dates.min()).date().isoformat(),
        "end": completed.date().isoformat(),
        "observed_sessions": len(checked),
        "expected_sessions": len(expected),
        "missing_sessions": [pd.Timestamp(date).date().isoformat() for date in missing],
        "unexpected_sessions": [pd.Timestamp(date).date().isoformat() for date in extra],
        "passed": bool(missing.empty and extra.empty),
    }


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


def rebalance_dates(
    calendar: pd.DatetimeIndex,
    params: Mapping[str, Any],
    completed_through: str | pd.Timestamp | None = None,
) -> list[pd.Timestamp]:
    """Resolve decision sessions without treating an unfinished week as complete.

    Staggered weekly schedules use the configured weekday when it trades.  If
    that weekday is an exchange holiday, the exchange calendar identifies the
    week's final session as soon as its close is completed.
    ``completed_through`` is deliberately separate from the last bar: on a
    normal Friday morning the latest completed bar may be Thursday, but the
    known Friday session prevents Thursday from being promoted prematurely.
    """
    dates = _normalized_index(calendar)
    if dates.empty:
        return []
    completed = (
        pd.Timestamp(completed_through).normalize()
        if completed_through is not None
        else pd.Timestamp(dates.max()).normalize()
    )
    dates = dates[dates <= completed]
    if dates.empty:
        return []

    schedule = str(params.get("rebalance_schedule", "fixed_weeks"))
    if schedule != "staggered_weeks":
        return [pd.Timestamp(date) for date in dates if is_rebalance_date(date, params)]

    weekday = int(params["rebalance_weekday"])
    if not 0 <= weekday <= 6:
        raise ValueError("rebalance_weekday 必须在 0 到 6 之间")
    frequency = f"W-{_WEEKDAY_NAMES[weekday]}"
    query_start = pd.Timestamp(dates.min()).to_period(frequency).start_time.normalize()
    query_end = completed.to_period(frequency).end_time.normalize()
    first, _ = exchange_calendar_bounds(str(params.get("rebalance_calendar", "XSHG")))
    official = exchange_sessions(
        str(params.get("rebalance_calendar", "XSHG")),
        max(query_start, first),
        query_end,
    )
    periods = official.to_period(frequency)
    observed = set(dates)
    decisions: list[pd.Timestamp] = []
    for period in periods.unique():
        sessions = official[periods == period]
        decision = pd.Timestamp(sessions.max()).normalize()
        if decision <= completed and decision in observed:
            decisions.append(decision)
    return decisions


def scheduled_dates(
    calendar: pd.DatetimeIndex,
    as_of: str | pd.Timestamp,
    params: Mapping[str, Any],
    count: int | None = None,
    completed_through: str | pd.Timestamp | None = None,
) -> list[pd.Timestamp]:
    cutoff = pd.Timestamp(as_of).normalize()
    completed = cutoff if completed_through is None else pd.Timestamp(completed_through).normalize()
    eligible = [date for date in rebalance_dates(calendar, params, completed) if date <= cutoff]
    if not eligible:
        raise ValueError(f"{cutoff.date()} 之前没有满足调度规则的决策日")
    if count is None:
        count = int(params.get("rebalance_sleeves", 4)) if params.get("rebalance_schedule") == "staggered_weeks" else 1
    return eligible[-int(count):]
