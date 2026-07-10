# Negative Overreaction Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可按首次发布时间回放的利空新闻层、确定性错杀识别状态机、观察卡流水线和历史事件回测。

**Architecture:** 新闻抓取、持久化、分类和价格状态机相互独立。生产流水线只生成观察案例并写入独立 NewsStore；研究脚本先从价格历史筛选冲击日，再按窄时间窗抓取新闻并做三组对照，避免下载全市场全部新闻。

**Tech Stack:** Python 3.11、httpx、pandas、SQLite、pytest、mypy、Alpaca Historical News API。

---

### Task 1: 新闻模型、Alpaca 数据源与持久化

**Files:**
- Create: `src/quant_signal/datafeed/news.py`
- Create: `src/quant_signal/news_store.py`
- Test: `tests/test_news.py`
- Test: `tests/test_news_store.py`

- [ ] 写失败测试，固定分页请求、RFC-3339 时间、symbol 去重、原始快照和首次可见时间不可被更新覆盖。
- [ ] 运行 `uv run pytest tests/test_news.py tests/test_news_store.py -q`，确认因模块缺失失败。
- [ ] 实现 `NewsArticle`、`NewsSource`、`AlpacaNewsSource.fetch()` 和 SQLite `NewsStore`。
- [ ] 重跑目标测试，要求全部通过。

### Task 2: 硬否决分类与价格状态机

**Files:**
- Create: `src/quant_signal/news_classifier.py`
- Create: `src/quant_signal/strategies/negative_overreaction.py`
- Test: `tests/test_news_classifier.py`
- Test: `tests/test_negative_overreaction.py`

- [ ] 写失败测试：造假/偿债/稀释命中硬否决；短期利润偏差和分析师降级可观察；未知消息不得触发。
- [ ] 写失败测试：冲击需达到 8% 或 2ATR、放量 1.8 倍、事件前在 200 日线上；至少两日企稳、五日过期、次日开盘成交。
- [ ] 运行目标测试确认预期失败。
- [ ] 实现固定结构 `NewsAssessment`、`classify_news()`、`detect_shock()` 和 `confirm_reversal()`。
- [ ] 重跑目标测试，要求全部通过。

### Task 3: 观察流水线与调度

**Files:**
- Create: `src/quant_signal/pipelines/negative_overreaction.py`
- Modify: `src/quant_signal/engine.py`
- Modify: `src/quant_signal/main.py`
- Modify: `src/quant_signal/scheduler.py`
- Modify: `src/quant_signal/notifier/cards.py`
- Test: `tests/test_negative_overreaction_pipeline.py`
- Modify: `tests/test_scheduler.py`

- [ ] 写失败测试：无新闻源安全跳过；可修复利空+价格确认产生观察卡；硬否决不推卡；调度包含收盘后观察任务。
- [ ] 运行目标测试确认失败。
- [ ] 注入 NewsSource/NewsStore，实现最近事件增量抓取、案例去重和观察卡；每天 16:45 ET 执行。
- [ ] 重跑流水线和调度测试。

### Task 4: 历史事件回测与报告

**Files:**
- Create: `research/backtest_negative_overreaction.py`
- Create: `src/quant_signal/overreaction_backtest.py`
- Test: `tests/test_overreaction_backtest.py`
- Create: `research/reports/negative_overreaction_run20260709.md`（脚本生成）

- [ ] 写失败测试：纯价格、硬规则、严格消息三组使用相同事件和次日开盘；40 日退出、止损和修复止盈无未来函数。
- [ ] 运行目标测试确认失败。
- [ ] 实现事件回放、交易指标和 Alpaca 新闻窄窗缓存；脚本默认读取本地研究 bars。
- [ ] 运行单测后执行真实历史回测，生成报告；不足 100 笔时明确裁决为仅观察。

### Task 5: 完整验证

**Files:**
- Modify: `docs/superpowers/specs/2026-07-09-negative-overreaction-design.md`（仅记录必要实现差异）

- [ ] 运行 `uv run pytest -q`。
- [ ] 运行 `uv run mypy src`。
- [ ] 运行 `uv run python -m py_compile` 检查新增研究脚本。
- [ ] 运行 `git diff --check` 并审计未覆盖已有用户改动。
