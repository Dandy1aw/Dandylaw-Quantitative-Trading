# 美股半自动量化信号系统 (quant-signal)

只产生信号并推送飞书，不自动下单——用户在券商 App 手动执行交易决策。

## 架构

```
┌──────────────┐    ┌───────────────┐    ┌───────────────┐
│  data-feed    │ →  │ signal-engine  │ →  │ notifier       │
│  行情采集      │    │ 策略调度+信号   │    │ 飞书卡片+去重   │
└──────┬───────┘    └───────┬───────┘    └───────────────┘
       ↓                    ↓
   duckdb(bars)        sqlite(signals)
                            ↑
                    ┌───────────────┐
                    │  research      │  离线回测（vectorbt + walk-forward）
                    └───────────────┘
```

单进程 monorepo，APScheduler 按 NYSE 交易日历调度盘前/盘中/盘后/维护/心跳五个任务。
数据源（yfinance/Alpaca）与通知器（Console/飞书）都是可切换的抽象，凭证未配置时
自动降级为 yfinance + Console，不影响开发和回测。

## 快速开始

```bash
uv sync --all-extras
uv run python -m quant_signal.ingest --days 730   # 拉取 2 年历史日线入 duckdb
uv run quant-signal                                # 启动调度器（前台运行）
```

回测（研究用）：

```bash
uv run python research/backtest_momentum.py
uv run python research/backtest_breakout.py
uv run python research/walkforward.py              # 无未来函数交叉验证
```

## 凭证配置

复制 `config/.env.example` 为 `config/.env`，按下面步骤获取两个凭证后填入。

### 1. Alpaca paper 账户（免费 IEX 行情）

1. 访问 [alpaca.markets](https://alpaca.markets) → 右上角 **Sign Up**，用邮箱注册并验证
2. 登录后进入 Dashboard，左侧导航确认处于 **Paper Trading** 模式（默认即是，无需申请实盘权限）
3. 左侧菜单找到 **API Keys**（或 Dashboard 首页的 "View API Keys"）
4. 点击 **Generate New Key**，会显示一对 `API Key ID` 和 `Secret Key`（Secret 只显示一次，务必立即复制）
5. 把两个值填入 `config/.env`：
   ```
   ALPACA_KEY=你的API Key ID
   ALPACA_SECRET=你的Secret Key
   ```
6. 把 `config/settings.yaml` 里的 `data_source: yfinance` 改成 `data_source: alpaca`
7. 验证：`uv run python -m quant_signal.ingest --days 30`，duckdb 里应能看到 `source='alpaca'` 的数据

### 2. 飞书自定义机器人 webhook

1. 打开目标飞书群 → 右上角设置（齿轮图标）→ **群机器人**
2. 点击 **添加机器人** → 选择 **自定义机器人**
3. 填写机器人名称（如"量化信号"），可选头像，点击 **添加**
4. 复制生成的 **Webhook 地址**（形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx`）
5. （建议）在安全设置中启用**关键词**校验，填一个所有卡片标题都会出现的词，比如 `信号`；如果本系统卡片标题不含该词会导致发送被拒，可根据 `notifier/cards.py` 里的标题格式调整关键词或改用"签名校验"方式
6. 把 webhook 填入 `config/.env`：
   ```
   FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx
   ```
7. 验证：`uv run python -m quant_signal.notifier.feishu --test`，飞书群应收到三张测试卡片（信号/早报/告警），中文和 emoji 显示正常

## 数据源限制说明

- yfinance 免费接口盘中数据延迟约 15 分钟；盘中突破信号在 yfinance 模式下卡片会标注"⚠️ 数据延迟约15分钟，仅供观察"，正式盘中信号建议接入 Alpaca 后使用
- Alpaca 免费行情为 **IEX 单一交易所数据**，与全市场 NBBO 报价存在差异，信号价格仅供参考

## 风险与合规提示

- 本系统仅生成参考信号，**不构成投资建议**；所有交易由用户人工决策并在券商 App 执行
- 系统在任何情况下都不实现自动下单逻辑，不接入任何券商交易 API
- 回测结果（`research/reports/`）仅供策略评估参考，不保证未来表现

## 目录说明

- `src/quant_signal/` — 主程序：数据层、策略、引擎、通知、调度
- `research/` — 离线回测脚本，import `src/` 下同一份策略代码，不重复实现
- `config/settings.yaml` — 标的池、策略参数、去重限流配置
- `config/.env` — 凭证（不提交 git）
- `data/` — duckdb/sqlite 数据文件（不提交 git）
- `logs/` — structlog JSON 日志与 Console 通知器的 `signals.jsonl`（不提交 git）
