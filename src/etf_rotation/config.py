from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    role: str
    group: str
    t0: bool


@dataclass(frozen=True)
class AppConfig:
    path: Path
    project: Mapping[str, Any]
    universe: tuple[Instrument, ...]
    strategy: Mapping[str, Any]
    risk: Mapping[str, Any]
    execution: Mapping[str, Any]
    qmt: Mapping[str, Any]
    validation: Mapping[str, Any]

    @property
    def symbols(self) -> list[str]:
        return [item.symbol for item in self.universe]

    @property
    def instrument_by_symbol(self) -> dict[str, Instrument]:
        return {item.symbol: item for item in self.universe}

    def resolve_path(self, value: str) -> Path:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate
        return (self.path.parent.parent / candidate).resolve()


def _required(data: Mapping[str, Any], key: str) -> Any:
    if key not in data:
        raise ValueError(f"配置缺少必填字段: {key}")
    return data[key]


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError("配置文件顶层必须是映射")

    universe_raw = _required(raw, "universe")
    universe = tuple(Instrument(**item) for item in universe_raw)
    if not universe:
        raise ValueError("ETF 标的池不能为空")
    symbols = [item.symbol for item in universe]
    if len(symbols) != len(set(symbols)):
        raise ValueError("ETF 标的池包含重复代码")
    if any(not item.t0 for item in universe):
        raise ValueError("本项目只接受显式标记为 T+0 的标的")

    strategy = _required(raw, "strategy")
    lookbacks = list(_required(strategy, "momentum_lookbacks"))
    weights = list(_required(strategy, "momentum_weights"))
    if len(lookbacks) != len(weights) or not lookbacks:
        raise ValueError("动量周期与权重数量必须一致且非空")
    if abs(sum(float(value) for value in weights) - 1.0) > 1e-9:
        raise ValueError("动量权重之和必须为 1")
    if float(strategy["max_gross_exposure"]) > 1.0:
        raise ValueError("最大总仓位不能超过 100%")
    if float(strategy["max_asset_weight"]) > float(strategy["max_gross_exposure"]):
        raise ValueError("单资产上限不能高于组合总仓位上限")

    return AppConfig(
        path=config_path,
        project=raw.get("project", {}),
        universe=universe,
        strategy=strategy,
        risk=_required(raw, "risk"),
        execution=_required(raw, "execution"),
        qmt=_required(raw, "qmt"),
        validation=_required(raw, "validation"),
    )
