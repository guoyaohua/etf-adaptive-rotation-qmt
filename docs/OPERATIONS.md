# 运行与命令手册

## 常用入口

| 入口 | 需要 QMT | 会下单 | 主要产物 |
|---|---:|---:|---|
| `scripts/install.ps1` | 否 | 否 | 安装、测试结果 |
| `scripts/setup.ps1` | 是 | 否 | 账本、日线、首份计划 |
| `etf-rr signal` | 只需缓存日线 | 否 | `runtime/latest_signal.json` |
| `etf-rr backtest` | 只需缓存日线 | 否 | `reports/<name>/` |
| `scripts/live.ps1` | 是 | 否 | `runtime/latest_order_plan.json` |
| `scripts/live.ps1 -Execute` | 是 | 是 | QMT 委托与计划状态 |
| `scripts/live-monitor.ps1` | 是 | 否 | 实时风险判断日志 |
| `scripts/live-monitor.ps1 -Execute` | 是 | 是 | 风险退出委托 |
| `scripts/reconcile.ps1` | 是 | 否 | 更新后的策略账本 |
| `scripts/llm-signal.ps1` | 行情缓存与 LLM | 否 | LLM 信号和审计缓存 |

## 下载日线

```powershell
etf-rr download --config configs/local.yaml --start 20150101 --end 20260710
```

当 `--end` 是当前盘中或未来日期时，下载器会截断到最后已完成日线，并在 `metadata.json` 中记录 `completed_through`。

## 回测

```powershell
etf-rr backtest `
  --config configs/local.yaml `
  --start 20150101 `
  --end 20260710 `
  --output reports/latest
```

输出包括 Markdown 报告、摘要 JSON、权益曲线、成交和目标记录。研究门槛失败时命令返回非零退出码。

## 稳健性验证

```powershell
.\scripts\validate.ps1 -Start 20150101 -End 20260710 -Output reports\validation-latest
```

验证报告记录策略版本、代码/配置/行情指纹、成本压力、滚动窗口、前缀不变性、交易日历和货币 ETF 企业行动策略。

## 成交对账

```powershell
etf-rr reconcile --config configs/local.yaml
```

程序只读取本策略标签的新成交，并按成交 ID 幂等更新 `runtime/state.json`。账本创建前的成交由时间基线排除。

## 可选 LLM 风险复核

LLM 只能维持或降低量化权重，不能新增标的或加仓。默认关闭，历史回测也不包含 LLM。

安装支持：

```powershell
.\scripts\install.ps1 -WithLlm
```

单模型配置示例：

```yaml
llm:
  enabled: true
  mode: single
  models: ["github_copilot/gemini-3-pro-preview"]
```

多模型投票示例：

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

Token 只通过 `GITHUB_TOKEN` 环境变量传入。缓存位于 `runtime/llm/`，不会提交 Git。

## 本地产物

- `data/qmt/`：行情缓存；
- `reports/`：回测与验证报告；
- `runtime/`：账户账本、计划和 LLM 缓存。

三者均不进入 Git。
