# 快速开始

本文把安装、初始化、模拟运行与实盘开关集中在一处。第一次使用时，请保持真实下单关闭，并至少完成 20 个交易日的向前模拟。

## 环境要求

- Windows；
- Python 3.10 或更高版本；
- 已安装并启动 QMT，当前 Python 环境可导入 `xtquant`；
- 账户已向券商确认相关 ETF 的当日交易资格。

## 1. 安装

```powershell
git clone https://github.com/guoyaohua/etf-adaptive-rotation-qmt.git
Set-Location etf-adaptive-rotation-qmt
.\scripts\install.ps1
```

若 PowerShell 阻止本地脚本，只对当前窗口临时放行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
```

## 2. 创建本地配置

```powershell
Copy-Item configs\local.example.yaml configs\local.yaml
$env:QMT_CLIENT_PATH = '<QMT userdata_mini 路径>'
$env:QMT_ACCOUNT_ID = '<资金账号>'
```

账号、路径、Token 和密码不得写入 YAML。`configs/local.yaml`、行情、账本和报告均已被 Git 忽略。首次使用必须保持：

```yaml
qmt:
  allow_live_orders: false
```

## 3. 初始化

```powershell
.\scripts\setup.ps1 -Capital 100000 -Connect
```

`Capital` 是策略可使用的资金上限，不是单笔买入金额。初始化会：

1. 创建与账户指纹绑定的本地策略账本；
2. 下载日线并校验交易所日历；
3. 运行环境体检；
4. 在交易时段生成联网 dry-run 计划，休市时生成离线信号。

已有 `runtime/state.json` 时脚本会原样保留。不要通过删除账本来绕过持仓归属或成交对账。

## 4. 先跑 dry-run

交易时段运行：

```powershell
.\scripts\live.ps1
```

它会刷新已完成日线、聚合四份周度信号、对账成交、检查风险并生成 `runtime/latest_order_plan.json`，默认不会下单。

只看离线权重：

```powershell
etf-rr signal --config configs/local.yaml --output runtime/latest_signal.json
```

环境体检：

```powershell
etf-rr doctor --config configs/local.yaml --connect
```

## 5. 回测与稳健性验证

```powershell
.\scripts\backtest.ps1 -Start 20150101 -End 20260710
.\scripts\validate.ps1 -Start 20150101 -End 20260710 -Output reports\validation-latest
```

完整验证包含 1×/2×/3× 成本压力、36 个月滚动窗口、前缀不变性、交易所日历和货币 ETF 价格复位审计。

## 6. 启用实盘

只有在回测、至少 20 个交易日模拟盘和券商规则核验完成后，才在本机把 `allow_live_orders` 改为 `true`。

```powershell
.\scripts\live.ps1 -Execute
```

命令仍要求人工输入 `LIVE_ETF_RR`。委托成交后运行：

```powershell
.\scripts\reconcile.ps1
```

盘中保护性退出需要另开窗口：

```powershell
.\scripts\live-monitor.ps1 -Execute
```

监控程序不是券商端止损或 Windows 服务。窗口关闭、休眠、QMT 或网络断开都会停止保护。

## 实盘提交前检查

- `qmt.allow_live_orders: true` 只存在于本地配置；
- 已显式使用 `-Execute` 并人工确认；
- 策略账本、账户和 `strategy_tag` 一致；
- 同代码没有人工或其他策略持仓；
- 报价新鲜且没有在途委托；
- 没有错过信号后的首个执行日；
- 真实佣金与滑点没有长期超出回测假设。

安全与敏感信息规则见 [SECURITY.md](SECURITY.md)。
