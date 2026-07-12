from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .backtest import BacktestResult
from .config import AppConfig


def _json_value(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def evaluate_gates(
    base: Mapping[str, Any],
    stress: Mapping[str, Any],
    config: AppConfig,
) -> dict[str, bool]:
    validation = config.validation
    return {
        "minimum_years": bool(base.get("years", 0) >= float(validation["minimum_years"])),
        "maximum_drawdown": bool(base.get("max_drawdown", np.inf) <= float(validation["maximum_drawdown"])),
        "minimum_calmar": bool(base.get("calmar", -np.inf) >= float(validation["minimum_calmar"])),
        "minimum_sharpe": bool(base.get("sharpe", -np.inf) >= float(validation["minimum_sharpe"])),
        "positive_year_ratio": bool(
            base.get("positive_year_ratio", -np.inf) >= float(validation["minimum_positive_year_ratio"])
        ),
        "positive_stress_cagr": bool(
            not validation.get("require_positive_stress_cagr", True) or stress.get("cagr", -np.inf) > 0
        ),
    }


def write_backtest_report(
    base: BacktestResult,
    stress: BacktestResult,
    config: AppConfig,
    output: str | Path,
) -> dict[str, Any]:
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    base.equity.to_csv(directory / "equity.csv")
    base.fills.to_csv(directory / "fills.csv", index=False)
    base.targets.to_json(directory / "targets.json", orient="records", force_ascii=False, indent=2, date_format="iso")
    stress.equity.to_csv(directory / "equity_stress.csv")
    gates = evaluate_gates(base.metrics, stress.metrics, config)
    summary = {
        "base": _json_value(base.metrics),
        "stress": _json_value(stress.metrics),
        "gates": gates,
        "passed_all_gates": all(gates.values()),
        "disclaimer": "历史结果不是未来收益保证；门槛通过只代表可进入下一阶段验证。",
    }
    with (directory / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    def percent(value: Any) -> str:
        return "N/A" if value is None or not np.isfinite(value) else f"{value:.2%}"

    def number(value: Any) -> str:
        return "N/A" if value is None or not np.isfinite(value) else f"{value:.3f}"

    markdown = f"""# ETF 策略回测报告

> 结论：{'通过预设研究门槛，可进入模拟盘验证' if summary['passed_all_gates'] else '未通过全部预设门槛，不应进入实盘'}。历史结果不保证未来收益。

| 指标 | 基础成本 | 双倍成本 |
|---|---:|---:|
| 区间 | {base.metrics.get('start')} ~ {base.metrics.get('end')} | {stress.metrics.get('start')} ~ {stress.metrics.get('end')} |
| CAGR | {percent(base.metrics.get('cagr'))} | {percent(stress.metrics.get('cagr'))} |
| 最大回撤 | {percent(base.metrics.get('max_drawdown'))} | {percent(stress.metrics.get('max_drawdown'))} |
| Sharpe | {number(base.metrics.get('sharpe'))} | {number(stress.metrics.get('sharpe'))} |
| Sortino | {number(base.metrics.get('sortino'))} | {number(stress.metrics.get('sortino'))} |
| Calmar | {number(base.metrics.get('calmar'))} | {number(stress.metrics.get('calmar'))} |
| 成交笔数 | {base.metrics.get('fills', 0)} | {stress.metrics.get('fills', 0)} |
| 佣金 | {base.metrics.get('commission', 0):.2f} | {stress.metrics.get('commission', 0):.2f} |
| 滑点成本 | {base.metrics.get('slippage', 0):.2f} | {stress.metrics.get('slippage', 0):.2f} |

## 研究门槛

"""
    for name, passed in gates.items():
        markdown += f"- {'通过' if passed else '失败'}：`{name}`\n"
    markdown += "\n## 重要限制\n\n- 当前列表可能含幸存者偏差；正式结论需使用历史成分。\n- 日线 OHLC 无法刻画日内路径与极端流动性，ATR 止损可能按更差价格成交。\n- 需要在未触碰样本和至少 20 个交易日模拟盘上再次验证。\n"
    (directory / "REPORT.md").write_text(markdown, encoding="utf-8")
    return summary
