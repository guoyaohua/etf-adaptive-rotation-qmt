from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .backtest import BacktestResult, Backtester, calculate_metrics
from .config import AppConfig
from .reporting import evaluate_gates
from .version import STRATEGY_VERSION


@dataclass(frozen=True)
class PrefixInvarianceResult:
    passed: bool
    cutoff: str
    compared_equity_rows: int
    compared_fills: int
    compared_targets: int
    mismatches: tuple[str, ...]


def _json_value(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def market_data_fingerprint(data: Mapping[str, pd.DataFrame]) -> str:
    """Return a deterministic digest of the exact bars used by validation."""
    digest = hashlib.sha256()
    for symbol in sorted(data):
        frame = data[symbol].sort_index()
        digest.update(symbol.encode("utf-8"))
        digest.update(b"\0")
        hashed = pd.util.hash_pandas_object(frame, index=True, categorize=True)
        digest.update(hashed.to_numpy(dtype=np.uint64).tobytes())
        digest.update("|".join(map(str, frame.columns)).encode("utf-8"))
    return digest.hexdigest()


def source_tree_fingerprint(directory: str | Path) -> str:
    """Fingerprint Python implementation files, including dirty worktrees."""
    root = Path(directory).resolve()
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    if not files:
        raise ValueError(f"没有可用于指纹的 Python 源文件: {root}")
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _numeric(value: Any, default: float = np.nan) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if np.isfinite(number) else default


def _slice_data(
    data: Mapping[str, pd.DataFrame],
    end: pd.Timestamp,
) -> dict[str, pd.DataFrame]:
    return {symbol: frame.loc[frame.index <= end].copy() for symbol, frame in data.items()}


def _compare_frames(
    expected: pd.DataFrame,
    actual: pd.DataFrame,
    label: str,
) -> str | None:
    try:
        pd.testing.assert_frame_equal(
            expected,
            actual,
            check_exact=False,
            check_dtype=False,
            check_freq=False,
            rtol=1e-10,
            atol=1e-8,
        )
    except AssertionError as exc:
        detail = str(exc).splitlines()[0] if str(exc) else "frame differs"
        return f"{label}: {detail}"
    return None


def compare_backtest_prefix(
    full: BacktestResult,
    prefix: BacktestResult,
    cutoff: str | pd.Timestamp,
) -> PrefixInvarianceResult:
    """Check that adding future bars cannot rewrite already-known results."""
    date = pd.Timestamp(cutoff)
    expected_equity = full.equity.loc[full.equity.index <= date]
    actual_equity = prefix.equity.loc[prefix.equity.index <= date]

    expected_fills = full.fills.copy()
    if not expected_fills.empty:
        expected_fills = expected_fills.loc[pd.to_datetime(expected_fills["date"]) <= date]
    actual_fills = prefix.fills.copy()
    if not actual_fills.empty:
        actual_fills = actual_fills.loc[pd.to_datetime(actual_fills["date"]) <= date]
    expected_fills = expected_fills.reset_index(drop=True)
    actual_fills = actual_fills.reset_index(drop=True)

    expected_targets = full.targets.copy()
    if not expected_targets.empty:
        expected_targets = expected_targets.loc[pd.to_datetime(expected_targets["date"]) <= date]
    actual_targets = prefix.targets.copy()
    if not actual_targets.empty:
        actual_targets = actual_targets.loc[pd.to_datetime(actual_targets["date"]) <= date]
    expected_targets = expected_targets.reset_index(drop=True)
    actual_targets = actual_targets.reset_index(drop=True)

    mismatches = tuple(
        item
        for item in (
            _compare_frames(expected_equity, actual_equity, "equity"),
            _compare_frames(expected_fills, actual_fills, "fills"),
            _compare_frames(expected_targets, actual_targets, "targets"),
        )
        if item is not None
    )
    return PrefixInvarianceResult(
        passed=not mismatches,
        cutoff=date.date().isoformat(),
        compared_equity_rows=len(expected_equity),
        compared_fills=len(expected_fills),
        compared_targets=len(expected_targets),
        mismatches=mismatches,
    )


def check_prefix_invariance(
    config: AppConfig,
    data: Mapping[str, pd.DataFrame],
    full_result: BacktestResult,
    *,
    cutoff: str | pd.Timestamp | None = None,
    prefix_ratio: float = 0.75,
) -> PrefixInvarianceResult:
    if full_result.equity.empty:
        raise ValueError("完整回测没有净值，无法进行前缀不变性检查")
    if not 0.1 <= float(prefix_ratio) <= 0.9:
        raise ValueError("prefix_ratio 必须在 0.1 到 0.9 之间")

    calendar = full_result.equity.index
    if cutoff is None:
        position = min(len(calendar) - 2, max(1, int((len(calendar) - 1) * prefix_ratio)))
        date = pd.Timestamp(calendar[position])
    else:
        eligible = calendar[calendar <= pd.Timestamp(cutoff)]
        if eligible.empty:
            raise ValueError("前缀截止日早于回测起始日")
        date = pd.Timestamp(eligible[-1])
    if date >= pd.Timestamp(calendar[-1]):
        raise ValueError("前缀截止日必须早于完整回测结束日")

    prefix_result = Backtester(config, cost_multiplier=1.0).run(_slice_data(data, date))
    return compare_backtest_prefix(full_result, prefix_result, date)


def rolling_window_metrics(
    result: BacktestResult,
    *,
    window_months: int = 36,
    step_months: int = 12,
) -> list[dict[str, Any]]:
    """Evaluate complete, overlapping calendar windows without refitting."""
    if window_months <= 0 or step_months <= 0:
        raise ValueError("滚动窗口与步长必须为正整数月")
    if result.equity.empty:
        return []

    first = pd.Timestamp(result.equity.index[0])
    last = pd.Timestamp(result.equity.index[-1])
    starts: list[pd.Timestamp] = []
    candidate = first
    while candidate + pd.DateOffset(months=window_months) <= last:
        starts.append(candidate)
        candidate += pd.DateOffset(months=step_months)

    # Also include an end-anchored window so the latest complete period is not
    # omitted merely because it does not align with the first observation.
    final_anchor = last - pd.DateOffset(months=window_months)
    if final_anchor >= first and all(abs((item - final_anchor).days) > 5 for item in starts):
        starts.append(final_anchor)
    starts.sort()

    windows: list[dict[str, Any]] = []
    for requested_start in starts:
        requested_end = requested_start + pd.DateOffset(months=window_months)
        equity = result.equity.loc[
            (result.equity.index >= requested_start) & (result.equity.index <= requested_end)
        ]
        if len(equity) < 2:
            continue
        fills = result.fills
        if not fills.empty:
            dates = pd.to_datetime(fills["date"])
            fills = fills.loc[(dates >= equity.index[0]) & (dates <= equity.index[-1])]
        metrics = calculate_metrics(equity, fills, float(equity["equity"].iloc[0]))
        windows.append(
            {
                "requested_start": requested_start.date().isoformat(),
                "requested_end": requested_end.date().isoformat(),
                **_json_value(metrics),
            }
        )
    return windows


def _cost_key(multiplier: float) -> str:
    return f"{multiplier:g}x"


def run_robustness_validation(
    config: AppConfig,
    data: Mapping[str, pd.DataFrame],
    *,
    cost_multipliers: Sequence[float] = (1.0, 2.0, 3.0),
    rolling_window_months: int = 36,
    rolling_step_months: int = 12,
    minimum_rolling_windows: int = 3,
    prefix_ratio: float = 0.75,
) -> dict[str, Any]:
    multipliers = sorted({float(value) for value in cost_multipliers}.union({1.0}))
    if any(value <= 0 for value in multipliers):
        raise ValueError("成本倍数必须大于 0")

    results = {value: Backtester(config, cost_multiplier=value).run(data) for value in multipliers}
    base = results[1.0]
    stress = results[max(multipliers)]
    prefix = check_prefix_invariance(
        config, data, base, prefix_ratio=prefix_ratio
    )
    windows = rolling_window_metrics(
        base, window_months=rolling_window_months, step_months=rolling_step_months
    )

    configured = evaluate_gates(base.metrics, stress.metrics, config)
    cagrs = [float(results[value].metrics.get("cagr", np.nan)) for value in multipliers]
    rolling_cagrs = [_numeric(item.get("cagr")) for item in windows]
    rolling_drawdowns = [_numeric(item.get("max_drawdown"), np.inf) for item in windows]
    gates = {f"configured_{name}": passed for name, passed in configured.items()}
    gates.update(
        {
            "prefix_invariance": prefix.passed,
            "all_cost_scenarios_positive_cagr": bool(
                cagrs and all(np.isfinite(value) and value > 0 for value in cagrs)
            ),
            "cagr_nonincreasing_as_cost_rises": bool(
                all(right <= left + 1e-10 for left, right in zip(cagrs, cagrs[1:]))
            ),
            "minimum_rolling_windows": len(windows) >= int(minimum_rolling_windows),
            "all_rolling_windows_positive_cagr": bool(
                rolling_cagrs
                and all(np.isfinite(value) and value > 0 for value in rolling_cagrs)
            ),
            "rolling_drawdown_within_limit": bool(
                rolling_drawdowns
                and max(rolling_drawdowns) <= float(config.validation["maximum_drawdown"])
            ),
        }
    )

    config_bytes = Path(config.path).read_bytes()
    index = base.equity.index
    rows = {symbol: len(frame) for symbol, frame in data.items()}
    finite_windows = [item for item in windows if np.isfinite(_numeric(item.get("cagr")))]
    worst_rolling = None
    if finite_windows:
        worst_rolling = {
            "cagr": min(finite_windows, key=lambda item: _numeric(item.get("cagr"))),
            "sharpe": min(
                finite_windows, key=lambda item: _numeric(item.get("sharpe"), np.inf)
            ),
            "max_drawdown": max(
                finite_windows, key=lambda item: _numeric(item.get("max_drawdown"), -np.inf)
            ),
        }

    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "strategy_version": STRATEGY_VERSION,
        "config_path": str(config.path),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "code_sha256": source_tree_fingerprint(Path(__file__).parent),
        "market_data_sha256": market_data_fingerprint(data),
        "data": {
            "start": index[0].date().isoformat() if len(index) else None,
            "end": index[-1].date().isoformat() if len(index) else None,
            "symbols": sorted(data),
            "rows": rows,
        },
        "assumptions": {
            "cost_multipliers": multipliers,
            "rolling_window_months": rolling_window_months,
            "rolling_step_months": rolling_step_months,
            "minimum_rolling_windows": minimum_rolling_windows,
            "prefix_ratio": prefix_ratio,
            "parameters_refit_per_window": False,
        },
        "cost_scenarios": {
            _cost_key(value): _json_value(results[value].metrics) for value in multipliers
        },
        "prefix_invariance": _json_value(prefix.__dict__),
        "rolling_windows": windows,
        "worst_rolling": worst_rolling,
        "gates": gates,
        "passed_all_gates": all(gates.values()),
        "limitations": [
            "现有 ETF 池可能存在幸存者偏差，不能替代历史可交易成分数据。",
            "日线 OHLC 不能还原盘中路径、折溢价、停牌与极端流动性。",
            "历史区间已参与研究；通过仅允许进入向前模拟，不代表未来盈利。",
        ],
    }


def write_validation_report(report: Mapping[str, Any], output: str | Path) -> None:
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "validation.json").write_text(
        json.dumps(_json_value(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def percent(value: Any) -> str:
        number = _numeric(value)
        return "N/A" if not np.isfinite(number) else f"{number:.2%}"

    def number(value: Any) -> str:
        number_value = _numeric(value)
        return "N/A" if not np.isfinite(number_value) else f"{number_value:.3f}"

    scenarios = report["cost_scenarios"]
    scenario_rows = "\n".join(
        f"| {name} | {percent(metrics.get('cagr'))} | {percent(metrics.get('max_drawdown'))} | "
        f"{number(metrics.get('sharpe'))} | {number(metrics.get('calmar'))} |"
        for name, metrics in scenarios.items()
    )
    rolling_rows = "\n".join(
        f"| {item['start']} ~ {item['end']} | {percent(item.get('cagr'))} | "
        f"{percent(item.get('max_drawdown'))} | {number(item.get('sharpe'))} | "
        f"{number(item.get('calmar'))} |"
        for item in report["rolling_windows"]
    ) or "| 无完整窗口 | - | - | - | - |"
    gate_rows = "\n".join(
        f"- {'通过' if passed else '失败'}：`{name}`"
        for name, passed in report["gates"].items()
    )
    prefix = report["prefix_invariance"]
    limitations = "\n".join(f"- {item}" for item in report["limitations"])
    markdown = f"""# 策略稳健性验证报告 (v{report['strategy_version']})

> 结论：{'通过研究门槛，可进入向前模拟' if report['passed_all_gates'] else '未通过稳健性门槛，不应进入实盘'}。历史结果不保证未来收益。

- 数据区间：`{report['data']['start']}` ~ `{report['data']['end']}`
- 代码指纹：`{report['code_sha256']}`
- 行情指纹：`{report['market_data_sha256']}`
- 配置指纹：`{report['config_sha256']}`

## 成本压力

| 成本倍数 | CAGR | 最大回撤 | Sharpe | Calmar |
|---:|---:|---:|---:|---:|
{scenario_rows}

## 前缀不变性

- 截止日：`{prefix['cutoff']}`
- 结论：{'通过' if prefix['passed'] else '失败'}
- 比较：{prefix['compared_equity_rows']} 个净值日、{prefix['compared_fills']} 笔成交、{prefix['compared_targets']} 个目标
- 差异：{'; '.join(prefix['mismatches']) if prefix['mismatches'] else '无'}

## 滚动窗口（参数冻结）

| 区间 | CAGR | 最大回撤 | Sharpe | Calmar |
|---|---:|---:|---:|---:|
{rolling_rows}

## 验证门槛

{gate_rows}

## 已知限制

{limitations}
"""
    (directory / "VALIDATION.md").write_text(markdown, encoding="utf-8")
