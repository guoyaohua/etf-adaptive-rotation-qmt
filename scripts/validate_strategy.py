from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_rotation.config import load_config  # noqa: E402
from etf_rotation.data import CsvMarketDataStore  # noqa: E402
from etf_rotation.validation import (  # noqa: E402
    run_robustness_validation,
    write_validation_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行防未来函数、滚动窗口和成本压力验证")
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--output", default="reports/validation-latest")
    parser.add_argument("--cost-multipliers", default="1,2,3")
    parser.add_argument("--rolling-window-months", type=int, default=36)
    parser.add_argument("--rolling-step-months", type=int, default=12)
    parser.add_argument("--minimum-rolling-windows", type=int, default=3)
    parser.add_argument("--prefix-ratio", type=float, default=0.75)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    store = CsvMarketDataStore(config.resolve_path(str(config.qmt["data_directory"])))
    data = store.load(config.symbols, args.start, args.end)
    multipliers = tuple(float(item.strip()) for item in args.cost_multipliers.split(",") if item.strip())
    report = run_robustness_validation(
        config,
        data,
        cost_multipliers=multipliers,
        rolling_window_months=args.rolling_window_months,
        rolling_step_months=args.rolling_step_months,
        minimum_rolling_windows=args.minimum_rolling_windows,
        prefix_ratio=args.prefix_ratio,
    )
    write_validation_report(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"报告: {(Path(args.output) / 'VALIDATION.md').resolve()}")
    return 0 if report["passed_all_gates"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
