# 可选 LLM 风险复核

LLM 位于量化目标之后、订单计划之前。它不是另一个选股器，只能对量化目标返回 `KEEP`、`REDUCE` 或 `EXIT`，不能新增 ETF、提高权重或绕过既有风控。默认关闭，因此项目公布的历史回测仍是可复现的纯量化结果。

## 安装

```powershell
.\scripts\install.ps1 -WithLlm
```

## 单模型

```yaml
llm:
  enabled: true
  mode: single
  models: ["github_copilot/gemini-3-pro-preview"]
```

## 多模型投票

```yaml
llm:
  enabled: true
  mode: vote
  models:
    - github_copilot/gemini-3-pro-preview
    - github_copilot/claude-sonnet-4.5
    - github_copilot/gpt-5.2
  min_valid_votes: 2
  consensus_ratio: 0.50
  failure_policy: quant_only
```

只有严格过半的有效票会生效；低于 `min_confidence` 的动作降级为 `KEEP`。合法票不足、调用失败或无明确多数时：

- `quant_only`：保留原量化目标；
- `all_cash`：清空目标；
- `error`：终止计划。

## 凭据与缓存

Token 只通过环境变量传入：

```powershell
$env:GITHUB_TOKEN = '<GitHub Token>'
.\scripts\llm-signal.ps1
```

每周结果缓存在 `runtime/llm/`。同一目标和配置默认复用缓存；只有显式使用 `-Refresh` 或 `--refresh-llm` 才重新调用。缓存与 Token 均不得提交 Git。

## 如何评价 LLM

LLM 输出具有非确定性，供应商还可能升级同名模型。因此不能用当前模型回填历史并把结果当成真实回测。应封存每周输入、模型版本、原始响应和最终动作，单独做向前模拟。
