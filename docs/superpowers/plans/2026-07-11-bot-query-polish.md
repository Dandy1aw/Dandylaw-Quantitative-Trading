# Bot Query Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 期权指令交易日回退、新增 信号/扫描/健康 指令、PARTIAL 待确认持久化。

**Architecture:** 全部在 feishu_bot.py + ledger.py + scheduler.py(runtime 注入) 内完成；spec: docs/superpowers/specs/2026-07-11-bot-query-polish-design.md。

**Tech Stack:** Python 3.12, sqlite, pytest, mypy strict.

## Global Constraints

- 新指令进群 @ 只读集合；回退最多 5 个交易日；PARTIAL 单行表新覆盖旧
- mypy strict、全量 pytest 全绿

---

### Task 1: 期权指令交易日回退

- [x] RED: `test_options_query_falls_back_to_last_trading_day`（昨日快照可查）、`test_options_query_gives_up_after_five_sessions`
- [x] 实现 `_reply_options` 沿 `previous_trading_day` 回退 ≤5 次
- [x] GREEN → commit `feat: fall back option query to last session`

### Task 2: JobRuntime 注入 + snapshot()

- [x] RED: scheduler 测试 `build_scheduler(..., runtime=custom)` 复用注入实例；`JobRuntime.snapshot()` 返回 last_success/duration/running_since
- [x] 实现：`build_scheduler` 加 `runtime: JobRuntime | None = None`；`snapshot()` 方法；main 先建 runtime 传给 bot 与 scheduler
- [x] GREEN → commit `feat: share job runtime with the bot`

### Task 3: 信号/扫描/健康 指令

- [x] RED: 三个指令的渲染与空态、`健康` 无 runtime 降级、群 @ 走群回复
- [x] 实现：`_TEXT_COMMANDS` 加 signals/scan/health；`FeishuBotService(..., runtime=None)`；渲染方法
- [x] GREEN → commit `feat: add signals scan health commands`

### Task 4: PARTIAL 持久化

- [x] RED: ledger `save_pending_import/pop_pending_import` 往返、单行覆盖；service 重建后确认导入仍成功；过期路径
- [x] 实现：`feishu_pending_imports` 单行表 + pydantic JSON 序列化；service 改用台账
- [x] GREEN → 全量 pytest+mypy → commit `feat: persist pending partial imports`
