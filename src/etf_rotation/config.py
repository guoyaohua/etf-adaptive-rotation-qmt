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
    llm: Mapping[str, Any]
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


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge nested mappings while replacing scalars and lists."""
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(existing, value)
        else:
            result[key] = value
    return result


def _load_raw_config(config_path: Path, chain: tuple[Path, ...] = ()) -> dict[str, Any]:
    resolved = config_path.resolve()
    if resolved in chain:
        cycle = " -> ".join(str(path) for path in (*chain, resolved))
        raise ValueError(f"配置 extends 出现循环引用: {cycle}")
    if not resolved.is_file():
        raise FileNotFoundError(f"配置文件不存在: {resolved}")
    with resolved.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError("配置文件顶层必须是映射")

    extends = raw.pop("extends", None)
    if extends is None:
        return raw
    if not isinstance(extends, str) or not extends.strip():
        raise ValueError("extends 必须是非空配置文件路径")
    parent_path = Path(extends)
    if not parent_path.is_absolute():
        parent_path = resolved.parent / parent_path
    parent = _load_raw_config(parent_path, (*chain, resolved))
    return _deep_merge(parent, raw)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    raw = _load_raw_config(config_path)

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
    strategy_tag = str(_required(_required(raw, "execution"), "strategy_tag")).strip()
    if not strategy_tag:
        raise ValueError("execution.strategy_tag 不能为空")
    llm = raw.get("llm", {})
    if not isinstance(llm, Mapping):
        raise ValueError("llm 配置必须是映射")
    if llm:
        inline_secret_fields = {"api_key", "token", "password", "secret"}.intersection(llm)
        if inline_secret_fields:
            fields = ", ".join(sorted(inline_secret_fields))
            raise ValueError(
                f"llm 禁止内联敏感字段 ({fields})；凭据只能通过 api_key_env 指定的环境变量读取"
            )
        mode = str(llm.get("mode", "single"))
        if mode not in {"single", "vote"}:
            raise ValueError("llm.mode 只能是 single 或 vote")
        models = llm.get("models", [])
        if not isinstance(models, list) or not models or any(not str(model).strip() for model in models):
            raise ValueError("llm.models 必须是非空模型列表")
        if len(set(map(str, models))) != len(models):
            raise ValueError("llm.models 不能包含重复模型")
        if len(models) > 9:
            raise ValueError("llm.models 最多配置 9 个模型")
        provider = str(llm.get("provider", "litellm"))
        if provider != "litellm":
            raise ValueError("当前 llm.provider 只支持 litellm")
        if any("/" not in str(model) for model in models):
            raise ValueError("llm.models 必须使用 LiteLLM provider/model 格式")
        if mode == "single" and len(models) != 1:
            raise ValueError("llm.mode=single 时必须且只能配置一个模型")
        if not 0.0 <= float(llm.get("min_confidence", 0.70)) <= 1.0:
            raise ValueError("llm.min_confidence 必须在 0 到 1 之间")
        if not 0.5 <= float(llm.get("consensus_ratio", 0.50)) <= 1.0:
            raise ValueError("llm.consensus_ratio 必须在 0.5 到 1 之间")
        if int(llm.get("min_valid_votes", 1)) <= 0:
            raise ValueError("llm.min_valid_votes 必须大于 0")
        effective_models = 1 if mode == "single" else len(models)
        if int(llm.get("min_valid_votes", 1)) > effective_models:
            raise ValueError("llm.min_valid_votes 不能超过实际参与模型数")
        failure_policy = str(llm.get("failure_policy", "quant_only"))
        if failure_policy not in {"quant_only", "all_cash", "error"}:
            raise ValueError("llm.failure_policy 只能是 quant_only、all_cash 或 error")
        if not 0.0 <= float(llm.get("max_scale_down", 1.0)) <= 1.0:
            raise ValueError("llm.max_scale_down 必须在 0 到 1 之间")
        if float(llm.get("timeout_seconds", 120)) <= 0:
            raise ValueError("llm.timeout_seconds 必须大于 0")
        if int(llm.get("max_tokens", 1200)) <= 0:
            raise ValueError("llm.max_tokens 必须大于 0")
        if int(llm.get("max_retries", 2)) < 0:
            raise ValueError("llm.max_retries 不能为负数")
        temperature = float(llm.get("temperature", 0.0))
        if not 0.0 <= temperature <= 2.0:
            raise ValueError("llm.temperature 必须在 0 到 2 之间")
        key_env = str(llm.get("api_key_env", "")).strip()
        if not key_env:
            raise ValueError("llm.api_key_env 必须是非空环境变量名")
        cache_directory = str(llm.get("cache_directory", "")).strip()
        if not cache_directory:
            raise ValueError("llm.cache_directory 不能为空")

    return AppConfig(
        path=config_path,
        project=raw.get("project", {}),
        universe=universe,
        strategy=strategy,
        risk=_required(raw, "risk"),
        execution=_required(raw, "execution"),
        qmt=_required(raw, "qmt"),
        llm=llm,
        validation=_required(raw, "validation"),
    )
