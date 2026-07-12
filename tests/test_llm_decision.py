from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from etf_rotation.config import load_config
from etf_rotation.llm_decision import (
    LLMDecisionResult,
    LiteLLMCaller,
    ModelVote,
    aggregate_votes,
    apply_llm_overlay,
    build_prompt,
    parse_model_vote,
    run_llm_decision,
    _response_text,
)
from etf_rotation.strategy import AssetSignal, TargetPortfolio


def _target() -> TargetPortfolio:
    signals = {
        "AAA.SH": AssetSignal(
            symbol="AAA.SH",
            role="growth",
            group="equity",
            close=10.0,
            average_amount=100_000_000.0,
            momentum=0.12,
            volatility=0.20,
            score=0.60,
            above_trend=True,
            positive_slope=True,
            atr=0.30,
            eligible=True,
        ),
        "BBB.SH": AssetSignal(
            symbol="BBB.SH",
            role="defensive",
            group="gold",
            close=5.0,
            average_amount=80_000_000.0,
            momentum=0.06,
            volatility=0.10,
            score=0.60,
            above_trend=True,
            positive_slope=True,
            atr=0.10,
            eligible=True,
        ),
    }
    return TargetPortfolio(
        decision_date=pd.Timestamp("2026-07-10"),
        regime="risk_on",
        weights={"AAA.SH": 0.40, "BBB.SH": 0.30},
        signals=signals,
        diagnostics={"gross_exposure": 0.70, "selected_count": 2},
    )


def _response(
    action: str = "KEEP",
    confidence: float = 0.90,
    risk_scale: float = 1.0,
    symbol_scales: dict[str, float] | None = None,
    reason: str = "风险可控",
) -> str:
    return json.dumps(
        {
            "portfolio_action": action,
            "confidence": confidence,
            "risk_scale": risk_scale,
            "symbol_scales": symbol_scales or {},
            "reason": reason,
            "risk_flags": [],
        },
        ensure_ascii=False,
    )


def _vote(model: str, action: str, confidence: float, scale: float) -> ModelVote:
    return ModelVote(model, action, confidence, scale, {}, f"{model}:{action}")


def test_parse_model_vote_accepts_fenced_json_and_bounded_reduction():
    raw = "结果如下：\n```json\n" + _response(
        "REDUCE", 0.88, 0.75, {"AAA.SH": 0.50}, "海外波动上升"
    ) + "\n```"

    vote = parse_model_vote(raw, "model-a", {"AAA.SH", "BBB.SH"})

    assert vote.action == "REDUCE"
    assert vote.risk_scale == pytest.approx(0.75)
    assert vote.symbol_scales == {"AAA.SH": 0.50}
    assert vote.raw_response == raw


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (_response("REDUCE", 0.9, 1.10), "risk_scale"),
        (_response("REDUCE", 0.9, 0.8, {"NOT-IN-TARGET.SH": 0.5}), "量化目标以外"),
        (_response("REDUCE", 0.9, 0.8, {"AAA.SH": 1.1}), "缩放比例"),
        (_response("KEEP", 0.9, 0.8), "KEEP"),
        (_response("EXIT", 0.9, 0.1), "EXIT"),
    ],
)
def test_parse_model_vote_rejects_attempts_to_escape_the_risk_envelope(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_model_vote(raw, "model-a", {"AAA.SH", "BBB.SH"})


def test_keep_cannot_hide_a_per_symbol_reduction():
    raw = _response("KEEP", 0.9, 1.0, {"AAA.SH": 0.5})

    with pytest.raises(ValueError, match="KEEP"):
        parse_model_vote(raw, "model-a", {"AAA.SH", "BBB.SH"})


def test_vote_requires_a_strict_majority_and_tie_uses_safe_fallback():
    result = aggregate_votes(
        [_vote("model-a", "EXIT", 0.9, 0.0), _vote("model-b", "KEEP", 0.9, 1.0)],
        ["model-a", "model-b"],
        failure_policy="quant_only",
    )

    assert result.action == "KEEP"
    assert result.risk_scale == 1.0
    assert result.used_fallback is True
    assert "无明确多数" in result.reason


def test_vote_ignores_failed_models_and_uses_strictest_winning_reduction():
    votes = [
        _vote("model-a", "REDUCE", 0.90, 0.60),
        ModelVote("model-b", "KEEP", 0.0, 1.0, {}, "调用失败", success=False, error="timeout"),
        ModelVote("model-c", "REDUCE", 0.80, 0.80, {"AAA.SH": 0.50}, "波动上升"),
    ]

    result = aggregate_votes(votes, ["model-a", "model-b", "model-c"], min_valid_votes=2)

    assert result.action == "REDUCE"
    assert result.confidence == pytest.approx(0.85)
    assert result.risk_scale == pytest.approx(0.60)
    assert result.symbol_scales == {"AAA.SH": pytest.approx(0.50)}


def test_low_confidence_risk_action_is_conservatively_downgraded_to_keep():
    result = aggregate_votes(
        [_vote("model-a", "REDUCE", 0.50, 0.10)],
        ["model-a"],
        min_confidence=0.70,
    )

    assert result.action == "KEEP"
    assert result.risk_scale == 1.0
    assert result.used_fallback is False


@pytest.mark.parametrize(
    ("policy", "action", "scale"),
    [("quant_only", "KEEP", 1.0), ("all_cash", "EXIT", 0.0)],
)
def test_insufficient_valid_votes_follow_configured_failure_policy(policy, action, scale):
    failed = ModelVote("model-a", "KEEP", 0.0, 1.0, {}, "失败", success=False)

    result = aggregate_votes([failed], ["model-a"], min_valid_votes=1, failure_policy=policy)

    assert result.action == action
    assert result.risk_scale == scale
    assert result.used_fallback is True


def test_error_failure_policy_stops_instead_of_silently_trading():
    with pytest.raises(RuntimeError, match="有效 LLM 票数不足"):
        aggregate_votes([], ["model-a"], min_valid_votes=1, failure_policy="error")


def test_overlay_can_only_preserve_or_reduce_quant_weights():
    target = _target()
    result = LLMDecisionResult(
        "REDUCE", 0.9, 0.8, {"AAA.SH": 0.5, "UNKNOWN.SH": 0.0}, "降低风险"
    )

    overlaid = apply_llm_overlay(target, result)

    assert overlaid.weights == {"AAA.SH": pytest.approx(0.16), "BBB.SH": pytest.approx(0.24)}
    assert set(overlaid.weights) <= set(target.weights)
    assert all(overlaid.weights[symbol] <= weight for symbol, weight in target.weights.items())
    assert overlaid.diagnostics["gross_exposure"] == pytest.approx(0.40)


def test_max_scale_down_caps_the_total_reduction_not_each_multiplier():
    target = _target()
    result = LLMDecisionResult(
        "REDUCE", 0.9, 0.10, {"AAA.SH": 0.10, "BBB.SH": 0.10}, "要求大幅减仓"
    )

    overlaid = apply_llm_overlay(target, result, max_scale_down=0.25)

    assert overlaid.weights == {"AAA.SH": pytest.approx(0.30), "BBB.SH": pytest.approx(0.225)}


def test_disabling_llm_exits_keeps_the_quant_target_on_exit_vote():
    target = _target()
    result = LLMDecisionResult("EXIT", 0.95, 0.0, {}, "建议清仓")

    overlaid = apply_llm_overlay(target, result, max_scale_down=1.0, allow_exits=False)

    assert overlaid.weights == target.weights


def test_prompt_contains_only_market_decision_data_not_environment_secrets(monkeypatch):
    auth_value = "sentinel-credential-value"
    monkeypatch.setenv("GITHUB_TOKEN", auth_value)
    monkeypatch.setenv("QMT_ACCOUNT_ID", "sensitive-account-id")

    prompt = build_prompt(_target(), {"AAA.SH": "成长ETF", "BBB.SH": "黄金ETF"})

    assert "AAA.SH" in prompt
    assert "quant_target_weights" in prompt
    assert auth_value not in prompt
    assert "sensitive-account-id" not in prompt


def test_single_mode_calls_only_one_model(tmp_path: Path):
    called: list[str] = []

    def caller(model: str, _system: str, _prompt: str) -> str:
        called.append(model)
        return _response()

    result = run_llm_decision(
        _target(),
        {"provider": "litellm", "mode": "single", "models": ["test/model-a", "test/model-b"]},
        {"AAA.SH": "A", "BBB.SH": "B"},
        tmp_path,
        caller=caller,
    )

    assert called == ["test/model-a"]
    assert result.expected_models == ("test/model-a",)


def test_vote_mode_calls_all_models_and_records_each_vote(tmp_path: Path):
    called: list[str] = []

    def caller(model: str, _system: str, _prompt: str) -> str:
        called.append(model)
        return _response("REDUCE", 0.9, 0.8, reason=model)

    result = run_llm_decision(
        _target(),
        {"provider": "litellm", "mode": "vote", "models": ["test/model-a", "test/model-b"], "min_valid_votes": 2},
        {},
        tmp_path,
        caller=caller,
    )

    assert set(called) == {"test/model-a", "test/model-b"}
    assert {vote.model for vote in result.votes} == {"test/model-a", "test/model-b"}
    assert result.action == "REDUCE"


def test_decision_cache_is_idempotent_and_restores_typed_votes(tmp_path: Path):
    calls = 0

    def caller(_model: str, _system: str, _prompt: str) -> str:
        nonlocal calls
        calls += 1
        payload = json.loads(_response())
        payload["risk_flags"] = ["event-risk"]
        return json.dumps(payload)

    settings = {"provider": "litellm", "mode": "single", "models": ["test/model-a"]}
    first = run_llm_decision(_target(), settings, {}, tmp_path, caller=caller)
    second = run_llm_decision(_target(), settings, {}, tmp_path, caller=caller)

    assert calls == 1
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.decision_id == first.decision_id
    assert isinstance(second.votes[0], ModelVote)
    assert second.votes[0].risk_flags == ("event-risk",)


def test_tampered_cache_is_recomputed_before_use(tmp_path: Path):
    calls = 0

    def caller(_model: str, _system: str, _prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _response()

    settings = {"provider": "litellm", "mode": "single", "models": ["test/model-a"]}
    first = run_llm_decision(_target(), settings, {}, tmp_path, caller=caller)
    cache_path = next(tmp_path.glob("*.json"))
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    cached["result"]["symbol_scales"] = {"NOT-IN-TARGET.SH": 1.0}
    cache_path.write_text(json.dumps(cached), encoding="utf-8")

    second = run_llm_decision(_target(), settings, {}, tmp_path, caller=caller)

    assert first.decision_id == second.decision_id
    assert calls == 2
    assert second.from_cache is False
    assert second.symbol_scales == {}


def test_cache_schema_mismatch_is_recomputed(tmp_path: Path):
    calls = 0

    def caller(_model: str, _system: str, _prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _response()

    settings = {"provider": "litellm", "mode": "single", "models": ["test/model-a"]}
    run_llm_decision(_target(), settings, {}, tmp_path, caller=caller)
    cache_path = next(tmp_path.glob("*.json"))
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    cached["schema_version"] = 999
    cache_path.write_text(json.dumps(cached), encoding="utf-8")

    result = run_llm_decision(_target(), settings, {}, tmp_path, caller=caller)

    assert calls == 2
    assert result.from_cache is False


def test_refresh_bypasses_cache_and_setting_changes_get_a_new_decision_id(tmp_path: Path):
    calls = 0

    def caller(_model: str, _system: str, _prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _response()

    settings = {"mode": "single", "models": ["model-a"], "min_confidence": 0.70}
    first = run_llm_decision(_target(), settings, {}, tmp_path, caller=caller)
    refreshed = run_llm_decision(_target(), settings, {}, tmp_path, refresh=True, caller=caller)
    changed = run_llm_decision(
        _target(), {**settings, "min_confidence": 0.80}, {}, tmp_path, caller=caller
    )

    assert calls == 3
    assert refreshed.from_cache is False
    assert refreshed.decision_id == first.decision_id
    assert changed.decision_id != first.decision_id


def test_malformed_model_output_is_a_failed_vote_and_uses_quant_fallback(tmp_path: Path):
    result = run_llm_decision(
        _target(),
        {"mode": "single", "models": ["model-a"], "failure_policy": "quant_only"},
        {},
        tmp_path,
        caller=lambda *_args: "this is not JSON",
    )

    assert result.action == "KEEP"
    assert result.used_fallback is True
    assert result.votes[0].success is False
    assert "JSON" in result.votes[0].error


def test_missing_api_token_fails_before_any_provider_call(monkeypatch):
    monkeypatch.delenv("ETF_LLM_TEST_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="ETF_LLM_TEST_TOKEN"):
        LiteLLMCaller({"api_key_env": "ETF_LLM_TEST_TOKEN"})


def test_litellm_chat_call_receives_token_without_mutating_environment(monkeypatch):
    import sys
    from types import SimpleNamespace

    captured: dict = {}
    fake = SimpleNamespace(
        suppress_debug_info=False,
        drop_params=False,
        register_model=lambda _models: None,
    )

    def completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_response()))]
        )

    fake.completion = completion
    fake.responses = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "litellm", fake)
    monkeypatch.setenv("ETF_LLM_TEST_TOKEN", "sentinel-value")

    caller = LiteLLMCaller({"api_key_env": "ETF_LLM_TEST_TOKEN", "max_retries": 0})
    raw = caller("provider/model-a", "system", "user")

    assert json.loads(raw)["portfolio_action"] == "KEEP"
    assert captured["api_key"] == "sentinel-value"
    assert captured["messages"][1]["content"] == "user"
    assert caller._auth_value == "sentinel-value"


def test_responses_only_copilot_model_uses_litellm_responses(monkeypatch):
    import sys
    from types import SimpleNamespace

    calls = {"completion": 0, "responses": 0}
    registered: dict = {}
    fake = SimpleNamespace(suppress_debug_info=False, drop_params=False)

    def register_model(models):
        registered.update(models)

    def completion(**_kwargs):
        calls["completion"] += 1
        raise AssertionError("responses-only model must not use completion")

    def responses(**kwargs):
        calls["responses"] += 1
        assert kwargs["api_key"] == "sentinel-value"
        return type("Response", (), {"output_text": _response()})()

    fake.register_model = register_model
    fake.completion = completion
    fake.responses = responses
    monkeypatch.setitem(sys.modules, "litellm", fake)
    monkeypatch.setenv("ETF_LLM_TEST_TOKEN", "sentinel-value")

    caller = LiteLLMCaller({"api_key_env": "ETF_LLM_TEST_TOKEN", "max_retries": 0})
    raw = caller("github_copilot/gpt-5.6-sol", "system", "user")

    assert json.loads(raw)["portfolio_action"] == "KEEP"
    assert calls == {"completion": 0, "responses": 1}
    assert registered["github_copilot/gpt-5.6-sol"]["mode"] == "responses"


def test_responses_api_nested_output_text_is_extracted():
    response = {"output": [{"content": [{"text": "{\"portfolio_action\":\"KEEP\"}"}]}]}

    assert _response_text(response) == '{"portfolio_action":"KEEP"}'


def _write_llm_config(config_path: Path, tmp_path: Path, changes: dict) -> Path:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["llm"].update(changes)
    destination = tmp_path / "llm-config.yaml"
    destination.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return destination


def test_default_config_uses_disabled_single_copilot_model(config_path: Path):
    config = load_config(config_path)

    assert config.llm["enabled"] is False
    assert config.llm["provider"] == "litellm"
    assert config.llm["mode"] == "single"
    assert config.llm["models"] == ["github_copilot/gemini-3-pro-preview"]
    assert config.llm["api_key_env"] == "GITHUB_TOKEN"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"mode": "single", "models": ["provider/a", "provider/b"]}, "只能配置一个模型"),
        ({"mode": "vote", "models": ["provider/a", "provider/a"]}, "重复模型"),
        ({"mode": "vote", "models": ["provider/a", "provider/b"], "min_valid_votes": 3}, "实际参与模型数"),
        ({"provider": "custom"}, "只支持 litellm"),
        ({"models": ["missing-provider-prefix"]}, "provider/model"),
    ],
)
def test_llm_configuration_rejects_ambiguous_or_unreachable_voting(
    config_path: Path, tmp_path: Path, changes: dict, message: str
):
    path = _write_llm_config(config_path, tmp_path, changes)

    with pytest.raises(ValueError, match=message):
        load_config(path)


@pytest.mark.parametrize("field", ["api_key", "token", "password", "secret"])
def test_llm_configuration_rejects_inline_secret_fields(
    config_path: Path, tmp_path: Path, field: str
):
    path = _write_llm_config(config_path, tmp_path, {field: "must-never-be-in-yaml"})

    with pytest.raises(ValueError, match="环境变量"):
        load_config(path)
