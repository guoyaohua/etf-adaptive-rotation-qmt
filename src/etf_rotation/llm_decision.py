from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .runtime import atomic_write_json, read_json
from .strategy import TargetPortfolio

ALLOWED_ACTIONS = {"KEEP", "REDUCE", "EXIT"}
SYSTEM_PROMPT = """
你是 ETF 周度组合的风险复核员。量化模型已经完成趋势、动量、相关性、波动率和仓位计算。
你只能减少风险，不能新增 ETF、不能提高任何权重、不能绕过量化风控。
请仅输出一个 JSON 对象，不要 Markdown，不要额外文字。格式：
{
  "portfolio_action": "KEEP|REDUCE|EXIT",
  "confidence": 0.0,
  "risk_scale": 1.0,
  "symbol_scales": {"代码": 1.0},
  "reason": "简洁、可审计的理由",
  "risk_flags": ["风险标签"]
}
规则：KEEP 的 risk_scale 必须为 1；REDUCE 的 risk_scale 必须在 0 到 1 之间；EXIT 必须为 0。
symbol_scales 只允许包含输入目标中的代码且取值在 0 到 1；省略表示 1。
如果证据不足，选择 KEEP 并如实降低 confidence。
""".strip()


@dataclass(frozen=True)
class ModelVote:
    model: str
    action: str
    confidence: float
    risk_scale: float
    symbol_scales: dict[str, float]
    reason: str
    risk_flags: tuple[str, ...] = ()
    raw_response: str = ""
    success: bool = True
    error: str = ""


@dataclass(frozen=True)
class LLMDecisionResult:
    action: str
    confidence: float
    risk_scale: float
    symbol_scales: dict[str, float]
    reason: str
    votes: tuple[ModelVote, ...] = ()
    expected_models: tuple[str, ...] = ()
    used_fallback: bool = False
    failure_policy: str = "quant_only"
    decision_id: str = ""
    from_cache: bool = False


def _serialize_vote(vote: ModelVote) -> dict[str, Any]:
    return {**asdict(vote), "risk_flags": list(vote.risk_flags)}


def _serialize_result(result: LLMDecisionResult) -> dict[str, Any]:
    return {
        "action": result.action, "confidence": result.confidence, "risk_scale": result.risk_scale,
        "symbol_scales": result.symbol_scales, "reason": result.reason,
        "votes": [_serialize_vote(vote) for vote in result.votes],
        "expected_models": list(result.expected_models), "used_fallback": result.used_fallback,
        "failure_policy": result.failure_policy, "decision_id": result.decision_id,
        "from_cache": result.from_cache,
    }


def _extract_json(text: str) -> Mapping[str, Any]:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    else:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型响应不包含 JSON 对象")
        cleaned = cleaned[start : end + 1]
    payload = json.loads(cleaned)
    if not isinstance(payload, Mapping):
        raise ValueError("模型响应顶层必须是 JSON 对象")
    return payload


def parse_model_vote(text: str, model: str, allowed_symbols: Iterable[str]) -> ModelVote:
    allowed = set(allowed_symbols)
    payload = _extract_json(text)
    action = str(payload.get("portfolio_action", payload.get("action", ""))).upper()
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"不支持的 LLM 动作: {action}")
    confidence = float(payload.get("confidence"))
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence 必须在 0 到 1 之间")
    requested_scale = float(payload.get("risk_scale", 1.0))
    if not math.isfinite(requested_scale) or not 0.0 <= requested_scale <= 1.0:
        raise ValueError("risk_scale 必须在 0 到 1 之间")
    if action == "KEEP" and abs(requested_scale - 1.0) > 1e-9:
        raise ValueError("KEEP 的 risk_scale 必须为 1")
    if action == "REDUCE" and not 0.0 <= requested_scale < 1.0:
        raise ValueError("REDUCE 的 risk_scale 必须小于 1")
    if action == "EXIT" and abs(requested_scale) > 1e-9:
        raise ValueError("EXIT 的 risk_scale 必须为 0")

    raw_scales = payload.get("symbol_scales", {})
    if not isinstance(raw_scales, Mapping):
        raise ValueError("symbol_scales 必须是映射")
    unknown = sorted(set(map(str, raw_scales)).difference(allowed))
    if unknown:
        raise ValueError(f"LLM 返回了量化目标以外的代码: {unknown}")
    symbol_scales: dict[str, float] = {}
    for symbol, raw in raw_scales.items():
        scale = float(raw)
        if not math.isfinite(scale) or not 0.0 <= scale <= 1.0:
            raise ValueError(f"{symbol} 的缩放比例必须在 0 到 1 之间")
        symbol_scales[str(symbol)] = scale
    if action == "KEEP" and any(abs(scale - 1.0) > 1e-9 for scale in symbol_scales.values()):
        raise ValueError("KEEP 不能隐藏单标的缩仓；应返回 REDUCE")
    reason = str(payload.get("reason", "")).strip()
    if not reason:
        raise ValueError("模型必须给出 reason")
    flags = payload.get("risk_flags", [])
    if not isinstance(flags, list) or any(not isinstance(value, str) for value in flags):
        raise ValueError("risk_flags 必须是字符串列表")
    return ModelVote(model, action, confidence, requested_scale, symbol_scales, reason, tuple(flags), text)


def _fallback_result(
    votes: list[ModelVote], expected_models: tuple[str, ...], failure_policy: str, reason: str
) -> LLMDecisionResult:
    if failure_policy == "error":
        raise RuntimeError(reason)
    if failure_policy == "all_cash":
        return LLMDecisionResult(
            "EXIT", 0.0, 0.0, {}, reason, tuple(votes), expected_models, True, failure_policy
        )
    return LLMDecisionResult(
        "KEEP", 0.0, 1.0, {}, reason, tuple(votes), expected_models, True, failure_policy
    )


def aggregate_votes(
    votes: Iterable[ModelVote],
    expected_models: Iterable[str],
    min_confidence: float = 0.70,
    min_valid_votes: int = 1,
    consensus_ratio: float = 0.50,
    failure_policy: str = "quant_only",
) -> LLMDecisionResult:
    if failure_policy not in {"quant_only", "all_cash", "error"}:
        raise ValueError("未知 LLM failure_policy")
    if not 0.0 <= float(min_confidence) <= 1.0:
        raise ValueError("min_confidence 必须在 0 到 1 之间")
    if not 0.5 <= float(consensus_ratio) <= 1.0:
        raise ValueError("consensus_ratio 必须在 0.5 到 1 之间")
    vote_list = list(votes)
    expected = tuple(expected_models)
    valid: list[ModelVote] = []
    for vote in vote_list:
        if not vote.success:
            continue
        if vote.confidence < min_confidence:
            valid.append(ModelVote(
                vote.model, "KEEP", vote.confidence, 1.0, {},
                f"置信度 {vote.confidence:.0%} 低于门槛，降级 KEEP；{vote.reason}",
                vote.risk_flags, vote.raw_response, True, vote.error,
            ))
        else:
            valid.append(vote)
    if len(valid) < int(min_valid_votes):
        return _fallback_result(vote_list, expected, failure_policy, "有效 LLM 票数不足，执行失败策略")

    counts = {action: sum(vote.action == action for vote in valid) for action in ALLOWED_ACTIONS}
    winner = max(("KEEP", "REDUCE", "EXIT"), key=lambda action: counts[action])
    if counts[winner] / len(valid) <= float(consensus_ratio):
        return _fallback_result(vote_list, expected, failure_policy, f"模型无明确多数: {counts}")
    winners = [vote for vote in valid if vote.action == winner]
    confidence = sum(vote.confidence for vote in winners) / len(winners)
    if winner == "KEEP":
        scale, symbol_scales = 1.0, {}
    elif winner == "EXIT":
        scale, symbol_scales = 0.0, {}
    else:
        # Risk votes aggregate conservatively: the strictest winning scale is
        # applied, never an average that dilutes one model's warning.
        scale = min(vote.risk_scale for vote in winners)
        symbols = set().union(*(vote.symbol_scales for vote in winners))
        symbol_scales = {
            symbol: min(vote.symbol_scales.get(symbol, 1.0) for vote in winners)
            for symbol in sorted(symbols)
        }
    reason = f"{winner} 获得 {counts[winner]}/{len(valid)} 有效票；" + " | ".join(vote.reason for vote in winners)
    return LLMDecisionResult(winner, confidence, scale, symbol_scales, reason, tuple(vote_list), expected, False, failure_policy)


def apply_llm_overlay(
    target: TargetPortfolio,
    result: LLMDecisionResult,
    max_scale_down: float = 1.0,
    allow_exits: bool = True,
) -> TargetPortfolio:
    if not 0.0 <= float(max_scale_down) <= 1.0:
        raise ValueError("max_scale_down 必须在 0 到 1 之间")
    if result.action not in ALLOWED_ACTIONS:
        raise ValueError("LLM 最终动作无效")
    floor = max(0.0, 1.0 - float(max_scale_down))
    requested_combined = {
        symbol: float(result.risk_scale) * min(1.0, max(0.0, result.symbol_scales.get(symbol, 1.0)))
        for symbol in target.weights
    }
    combined = {symbol: min(1.0, max(floor, scale)) for symbol, scale in requested_combined.items()}
    if result.action == "EXIT" and not allow_exits:
        combined = {symbol: 1.0 for symbol in target.weights}
    weights = {symbol: weight * combined[symbol] for symbol, weight in target.weights.items()}
    weights = {symbol: value for symbol, value in weights.items() if value > 1e-12}
    if any(weights.get(symbol, 0.0) > weight + 1e-12 for symbol, weight in target.weights.items()):
        raise AssertionError("LLM overlay 不得放大量化目标权重")
    diagnostics = dict(target.diagnostics)
    diagnostics.update({
        "llm_action": result.action,
        "llm_confidence": result.confidence,
        "llm_risk_scale": min(combined.values(), default=1.0),
        "llm_used_fallback": result.used_fallback,
        "llm_decision_id": result.decision_id,
        "gross_exposure": float(sum(weights.values())),
    })
    return TargetPortfolio(target.decision_date, target.regime, weights, target.signals, diagnostics)


def build_prompt(target: TargetPortfolio, instrument_names: Mapping[str, str]) -> str:
    rows = []
    for symbol, signal in sorted(target.signals.items(), key=lambda item: item[1].score if math.isfinite(item[1].score) else -math.inf, reverse=True):
        rows.append({
            "symbol": symbol, "name": instrument_names.get(symbol, symbol), "role": signal.role,
            "eligible": signal.eligible, "above_trend": signal.above_trend,
            "positive_slope": signal.positive_slope, "momentum": _finite(signal.momentum),
            "volatility": _finite(signal.volatility), "score": _finite(signal.score),
            "target_weight": target.weights.get(symbol, 0.0),
        })
    payload = {
        "decision_date": target.decision_date.date().isoformat(),
        "regime": target.regime,
        "quant_target_weights": target.weights,
        "quant_diagnostics": {
            str(key): _json_safe(value) for key, value in target.diagnostics.items()
            if not str(key).startswith("llm_")
        },
        "assets": rows,
    }
    return "请复核以下周度量化目标。输入数据：\n" + json.dumps(
        payload, ensure_ascii=False, sort_keys=True, allow_nan=False
    )


def _finite(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _decision_id(target: TargetPortfolio, models: Iterable[str], prompt: str) -> str:
    payload = {
        "date": target.decision_date.isoformat(), "weights": target.weights,
        "models": list(models), "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def _settings_fingerprint(settings: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "provider", "mode", "models", "temperature", "seed", "max_tokens",
        "min_confidence", "min_valid_votes", "consensus_ratio", "failure_policy",
        "allow_exits", "max_scale_down",
    )
    return {key: settings.get(key) for key in keys}


def _response_text(response: Any) -> str:
    try:
        return str(response.choices[0].message.content or "")
    except (AttributeError, IndexError, TypeError):
        text = getattr(response, "output_text", None)
        if text is not None:
            return str(text)
        if isinstance(response, Mapping) and response.get("output_text") is not None:
            return str(response["output_text"])
        output = response.get("output") if isinstance(response, Mapping) else getattr(response, "output", None)
        extracted = _text_fragment(output)
        if extracted:
            return extracted
        raise ValueError("LiteLLM 响应中没有可读取文本")


def _text_fragment(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(_text_fragment(item) for item in value)
    if isinstance(value, Mapping):
        for key in ("text", "content", "output_text"):
            text = _text_fragment(value.get(key))
            if text:
                return text
        return ""
    for attr in ("text", "content", "output_text"):
        if hasattr(value, attr):
            text = _text_fragment(getattr(value, attr))
            if text:
                return text
    return ""


class LiteLLMCaller:
    def __init__(self, settings: Mapping[str, Any]):
        self.settings = settings
        key_env = str(settings.get("api_key_env", "GITHUB_TOKEN"))
        self._auth_value = os.environ.get(key_env, "")
        if not self._auth_value:
            raise RuntimeError(f"LLM 已启用，但环境变量 {key_env} 未设置")
        try:
            import litellm
        except ImportError as exc:
            raise RuntimeError("缺少 LiteLLM；请运行 pip install -e '.[llm]'") from exc
        self.litellm = litellm
        self.litellm.suppress_debug_info = True
        self.litellm.drop_params = True
        responses_models = {"gpt-5.3-codex", "gpt-5.4", "gpt-5.5", "gpt-5.6-sol"}
        responses_models.update(
            model.split("/", 1)[1]
            for model in settings.get("models", [])
            if str(model).startswith("github_copilot/") and "codex" in str(model).lower()
        )
        register = {
            f"github_copilot/{name}": {
                "mode": "responses",
                "supported_endpoints": ["/v1/responses"],
                "litellm_provider": "github_copilot",
            }
            for name in responses_models
        }
        self.litellm.register_model(register)

    def __call__(self, model: str, system_prompt: str, user_prompt: str) -> str:
        if any(name in model.lower() for name in ("codex", "gpt-5.4", "gpt-5.5", "gpt-5.6-sol")):
            return self._call_responses(model, system_prompt, user_prompt)
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": float(self.settings.get("temperature", 0.0)),
            "seed": int(self.settings.get("seed", 42)),
            "timeout": float(self.settings.get("timeout_seconds", 120)),
            "max_tokens": int(self.settings.get("max_tokens", 1200)),
            "api_key": self._auth_value,
        }
        attempts = max(1, int(self.settings.get("max_retries", 2)) + 1)
        for attempt in range(attempts):
            try:
                return _response_text(self.litellm.completion(**kwargs))
            except Exception:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(min(8.0, 2.0 ** attempt + random.random() * 0.25))
        raise AssertionError("unreachable")

    def _call_responses(self, model: str, system_prompt: str, user_prompt: str) -> str:
        attempts = max(1, int(self.settings.get("max_retries", 2)) + 1)
        for attempt in range(attempts):
            try:
                response_kwargs = {
                    "model": model,
                    "input": f"{system_prompt}\n\n{user_prompt}",
                    "max_output_tokens": int(self.settings.get("max_tokens", 1200)),
                    "timeout": float(self.settings.get("timeout_seconds", 120)),
                    "api_key": self._auth_value,
                }
                response = self.litellm.responses(**response_kwargs)
                return _response_text(response)
            except Exception:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(min(8.0, 2.0 ** attempt + random.random() * 0.25))
        raise AssertionError("unreachable")


def run_llm_decision(
    target: TargetPortfolio,
    settings: Mapping[str, Any],
    instrument_names: Mapping[str, str],
    cache_directory: str | Path,
    refresh: bool = False,
    caller: Callable[[str, str, str], str] | None = None,
) -> LLMDecisionResult:
    models = tuple(str(model) for model in settings["models"])
    if str(settings.get("provider", "litellm")) != "litellm":
        raise ValueError("当前只支持 LiteLLM provider")
    prompt = build_prompt(target, instrument_names)
    fingerprint = _settings_fingerprint(settings)
    decision_id = _decision_id(
        target, models, prompt + json.dumps(fingerprint, ensure_ascii=True, sort_keys=True)
    )
    cache_path = Path(cache_directory) / f"{target.decision_date.date()}-{decision_id}.json"
    if not refresh:
        cached = read_json(cache_path)
        if (
            isinstance(cached, Mapping)
            and int(cached.get("schema_version", 0)) == 1
            and cached.get("decision_id") == decision_id
            and isinstance(cached.get("result"), Mapping)
        ):
            try:
                result = _result_from_dict(cached["result"])
                _validate_cached_result(result, set(target.weights), decision_id, set(models))
                return LLMDecisionResult(
                    result.action, result.confidence, result.risk_scale, result.symbol_scales, result.reason,
                    result.votes, result.expected_models, result.used_fallback, result.failure_policy,
                    result.decision_id, True,
                )
            except (KeyError, TypeError, ValueError):
                # A truncated or manually edited cache must never reach order
                # generation. Recompute it through the normal validated path.
                pass

    invoke = caller or LiteLLMCaller(settings)
    votes: list[ModelVote] = []
    max_workers = 1 if str(settings.get("mode", "single")) == "single" else max(1, len(models))
    active_models = models[:1] if str(settings.get("mode", "single")) == "single" else models
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(invoke, model, SYSTEM_PROMPT, prompt): model for model in active_models}
        for future in as_completed(futures):
            model = futures[future]
            try:
                raw = future.result()
                votes.append(parse_model_vote(raw, model, target.weights))
            except Exception as exc:
                votes.append(ModelVote(model, "KEEP", 0.0, 1.0, {}, "模型调用或解析失败", (), "", False, str(exc)[:500]))
    result = aggregate_votes(
        votes, active_models, float(settings.get("min_confidence", 0.70)),
        int(settings.get("min_valid_votes", 1)), float(settings.get("consensus_ratio", 0.50)),
        str(settings.get("failure_policy", "quant_only")),
    )
    result = LLMDecisionResult(
        result.action, result.confidence, result.risk_scale, result.symbol_scales, result.reason,
        result.votes, result.expected_models, result.used_fallback, result.failure_policy, decision_id, False,
    )
    atomic_write_json(cache_path, {
        "schema_version": 1, "decision_id": decision_id, "created_at": time.time(),
        "models": models, "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "settings": fingerprint,
        "result": _serialize_result(result),
    })
    return result


def _result_from_dict(raw: Mapping[str, Any]) -> LLMDecisionResult:
    votes = tuple(
        ModelVote(**{**vote, "risk_flags": tuple(vote.get("risk_flags", []))})
        for vote in raw.get("votes", [])
    )
    return LLMDecisionResult(
        action=str(raw["action"]), confidence=float(raw["confidence"]), risk_scale=float(raw["risk_scale"]),
        symbol_scales={str(k): float(v) for k, v in raw.get("symbol_scales", {}).items()},
        reason=str(raw.get("reason", "")), votes=votes, expected_models=tuple(raw.get("expected_models", [])),
        used_fallback=bool(raw.get("used_fallback", False)), failure_policy=str(raw.get("failure_policy", "quant_only")),
        decision_id=str(raw.get("decision_id", "")), from_cache=bool(raw.get("from_cache", False)),
    )


def _validate_cached_result(
    result: LLMDecisionResult, allowed_symbols: set[str], decision_id: str, configured_models: set[str]
) -> None:
    if result.decision_id != decision_id:
        raise ValueError("LLM 缓存 decision_id 不匹配")
    if result.action not in ALLOWED_ACTIONS:
        raise ValueError("LLM 缓存动作无效")
    if not math.isfinite(result.confidence) or not 0.0 <= result.confidence <= 1.0:
        raise ValueError("LLM 缓存置信度无效")
    if not math.isfinite(result.risk_scale) or not 0.0 <= result.risk_scale <= 1.0:
        raise ValueError("LLM 缓存风险比例无效")
    if set(result.symbol_scales).difference(allowed_symbols):
        raise ValueError("LLM 缓存包含量化目标外代码")
    if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in result.symbol_scales.values()):
        raise ValueError("LLM 缓存单标的比例无效")
    if set(result.expected_models).difference(configured_models):
        raise ValueError("LLM 缓存模型集合不匹配")
    if any(vote.model not in configured_models for vote in result.votes):
        raise ValueError("LLM 缓存投票模型不匹配")
