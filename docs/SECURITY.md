# 安全与敏感信息规范

## 不得进入 Git/GitHub

- 券商资金账号、登录账号、密码；
- API Key、Access Token、Secret、私钥、证书；
- QMT 客户端真实安装路径或 `userdata_mini` 路径；
- `.env`、`*.local.yaml`、行情缓存、订单计划、持仓账本、运行日志；
- 包含个人交易、资产或成交明细的回测/实盘报告。

程序只从环境变量读取 `QMT_CLIENT_PATH`、`QMT_ACCOUNT_ID` 和可选的 `GITHUB_TOKEN`。`.env.example` 只能保留空值。LLM Token、模型端点密钥和原始响应缓存不得进入 Git。

提交前运行：

```powershell
python scripts/security_check.py
git status --short
```

扫描器只报告文件与行号，不输出疑似秘密本身。CI 会重复扫描。扫描不能覆盖所有秘密格式，人工审查 `git diff --cached` 仍是必需步骤。
