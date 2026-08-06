---
name: verify
description: 本仓库端到端验证配方——如何在生产调度器运行中驱动真实 pipeline/bot/卡片推送
---

# quant-signal 端到端验证配方

## 环境约束（先读，都踩过坑）

- **生产调度器常驻运行**（`quant-signal` 进程）。它持有两把锁：
  - `.venv\Scripts\quant-signal.exe` → `uv run`/`uv sync` 会因重装 exe 失败。
    **一律用 `.venv\Scripts\python.exe` 直接跑**。
  - `data\bars.duckdb` → Windows 排他锁，Copy-Item 都会失败。
    **用 `data\backups\<最近目录>\bars.duckdb` 的副本**构造 BarStore。
- `data\signals.db` 是 sqlite，多进程共享没问题，可直接用真实台账。
- 凭证在 `config/.env`，`load_settings()` 需 cwd = 仓库根。
- PowerShell 5.1 内联 `python -c` 引号会被吃掉 → 脚本写到 scratchpad 再跑。

## 驱动真实表面

```python
sys.path.insert(0, r"D:\Codex\量化交易\src"); os.chdir(仓库根)
settings = load_settings()
store = BarStore(备份副本路径)          # 唯一替身
ledger = SignalLedger(Path("data/signals.db"))
notifier = get_notifier(settings)       # FeishuAppNotifier → 真实推群！
engine = Engine(settings, store, get_source(settings), ledger, notifier,
                earnings_source=YFinanceEarnings(),
                option_chain_source=AlpacaOptionChainSource(key, secret))
```

- **调度 job 门控**：`sched = build_scheduler(engine, ledger, store, notifier,
  runtime=JobRuntime())`（不 start），`sched.get_job(id).func()` 直接触发，
  观察 `skip.non_trading_day` 等真实日历门控日志。
- **pipeline**：`engine.run_xxx(now)`，now 传目标时点（tz-aware）。
- **bot 指令**：`FeishuBotService(ledger, settings, LarkTransport(app_id, secret),
  clock=..., engine=engine)`，构造 `BotMessage`（message_id 必须唯一——
  ledger 按 message_id 幂等去重；sender 用 `allowed_open_ids[0]`，
  chat_id 用 `push_receive_id`）直接 `bot.handle(msg)`。
  发送失败会日志 `feishu_bot.send_failed`——**没有该日志即发送成功**。

## 注意

- 所有 Feishu 发送都是**真的**，会出现在用户的群里——先想好发几条。
- **不要**驱动 option_flow pipeline 做测试：durable outbox 按时间槽幂等落
  真实 signals.db，会污染生产扫描记录。
- structlog 不 setup 也会打到 stdout，足够取证；yfinance 对无财报标的
  打印裸 `HTTP Error 404` 属正常噪音。
- 新代码要生效于生产，需重启 quant-signal 进程（验证脚本是独立进程，
  不代表生产已更新）。
