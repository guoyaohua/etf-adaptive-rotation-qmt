# ETF Regime Rotation QMT

面向沪深市场可日内回转交易（T+0）的跨境、商品与债券 ETF 的 QMT 策略工程。GitHub 仓库建议名称：`etf-regime-rotation-qmt`。

本项目把收益目标表述为“提高样本外风险调整后收益，并限制尾部回撤”，不承诺稳定盈利或固定收益率。任何历史回测都可能受幸存者偏差、行情复权、申赎/溢价、成交冲击及制度变化影响。默认禁止真实下单。

## 策略摘要

1. 周五收盘后，仅用已完成日线计算 20/60/120 日跳过近 5 日的风险调整动量。四个错峰月度子组合每周轮换一个，降低单一月末时点依赖。
2. 用长期均线与快速均线斜率做绝对趋势过滤，并记录风险资产趋势广度作为诊断。
3. 股票型、黄金和国债 ETF 在同一个相对强弱框架内竞争；无有效资产时持有现金。
4. 每个子组合内同一风险暴露组最多一只，并拒绝高度相关的新增持仓；聚合后的单组权重自然受 40% 上限约束，1% 调仓容忍带减少小额无效成交。
5. 用逆波动率初始权重和组合波动率目标控制风险仓位，并保留至少 10% 现金。
6. 信号在收盘生成，下一交易日开盘成交；回测包含佣金、最低佣金、滑点、整手约束和双倍成本压力测试。
7. 初始 ATR 止损、滞后式 ATR 跟踪退出、组合软/硬回撤降仓及单日亏损熔断构成独立风控层。T+0 资格用于当天退出，不采用高换手的强制尾盘清仓。

## 安装

```powershell
cd D:\Project\etf-regime-rotation-qmt
python -m pip install -e .[dev]
```

QMT 的 `xtquant` 由交易端环境提供，不写入公共依赖。

## 获取数据与回测

先启动 QMT 行情端，然后执行：

```powershell
etf-rr download --config configs/strategy.yaml --start 20150101 --end 20260710
etf-rr backtest --config configs/strategy.yaml --start 20150101 --end 20260710 --output reports/latest
```

如果不安装命令行入口，可使用：

```powershell
python -m etf_rotation.cli backtest --config configs/strategy.yaml --start 20150101 --end 20260710
```

报告同时给出基础成本、双倍成本、年度表现和预设上线门槛。当前冻结版本在 2015-01-05 至 2026-07-10 的 QMT 日线上，基础成本 CAGR 约 4.82%、最大回撤约 6.82%、Sharpe 约 0.85；双倍成本 CAGR 约 3.93%。这些数据参与了候选选择，不是未触碰样本，也不构成未来盈利保证。门槛失败时应保持模拟盘，不能继续调参直到同一测试集“变绿”。

## 生成最新信号

```powershell
etf-rr signal --config configs/strategy.yaml
```

## QMT 订单计划

账号信息只从环境变量读取：

```powershell
$env:QMT_CLIENT_PATH = '<本机 QMT userdata_mini 路径>'
$env:QMT_ACCOUNT_ID = '<资金账号>'
etf-rr qmt-plan --config configs/strategy.yaml --capital 100000
```

`qmt-plan` 只查询账户和输出订单计划，不下单。真实执行需要同时满足以下三项：

- 配置中把 `qmt.allow_live_orders` 改为 `true`；
- 命令显式加入 `--execute`；
- 输入精确确认短语 `LIVE_ETF_RR`。

卖出计划只认可 `runtime/owned_positions.json` 中由本策略记录、且账户仍实际持有的数量；没有账本时不会把同代码的人工持仓当作策略仓位卖出。当前版本不会自动维护实盘成交账本，因此仍定位为研究和订单计划工具；完成回调、部分成交、撤单与崩溃恢复前，不应启用 `--execute`。

`--capital` 是本策略独立的资金上限，不会默认使用整个账户净值。正式使用前还必须确认券商对每个 ETF 的 T+0 资格、交易费率和最小委托单位，并先完成至少 20 个交易日的模拟盘影子运行。

账号、密码、Key、Token、客户端路径不得写入 YAML 或源码。环境变量只在当前机器设置；`.env*`、本地配置、行情、报告、订单计划和运行状态默认不进入 Git。提交前运行：

```powershell
python scripts/security_check.py
```

## 测试

```powershell
python -m pytest
python -m compileall -q src
```

测试覆盖无未来函数、相关性/分组约束、仓位上限、ATR 风控、成交成本与真实下单安全锁。

## 目录

```text
configs/          策略、风控、成交和 QMT 配置
docs/             参考策略审查与验证规范
src/etf_rotation/ 研究、回测、信号、风险和 QMT 适配器
tests/            单元与回归测试
data/qmt/         本地行情缓存（不入 Git）
reports/          回测报告（不入 Git）
runtime/          实盘状态与订单计划（不入 Git）
```

## 实盘边界

- 固定当前 ETF 列表会带来幸存者偏差；严肃研究应保存每个历史时点的可交易名单。
- 跨境 ETF 可能因海外休市、额度、折溢价出现与指数不同步的行情，策略只用二级市场价格并设置成交额过滤，不能消除该风险。
- 日线回测不能证明日内止损一定按目标价成交；压力测试仍可能低估极端跳空。
- 不在同一账户中混用人工同代码持仓；计划器发现账户数量大于策略账本数量时会直接拒绝相关计划。
