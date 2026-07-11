# Option Flow Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 半日市的期权扫描/收盘榜时点正确；option_flow 与 feishu 消息表按保留期清理。

**Architecture:** calendar.session_close_utc + scheduler 门控；ledger.prune_option_flow 挂 maintenance。spec: docs/superpowers/specs/2026-07-11-option-flow-hygiene-design.md。

**Tech Stack:** Python 3.12, pandas_market_calendars, sqlite, pytest, mypy strict.

## Global Constraints

- 正常交易日行为不变（16:20 收盘榜、盘中 10:00–15:45）
- retention_days ≥ 30 校验；PENDING outbox 不受清理影响
- mypy strict、全量 pytest 全绿

---

### Task 1: calendar.session_close_utc

- [x] RED: 正常日 16:00 ET、2026-11-27 半日市 13:00 ET、周末 None
- [x] 实现（lru_cache per year，取 schedule.market_close）
- [x] GREEN → commit `feat: expose session close times`

### Task 2: 调度门控

- [x] RED: 盘中任务收盘后跳过；close 任务 cron 含 13:20+16:20；正常日 13:20 不执行/16:20 执行、半日市反之（monkeypatch session_close_utc）
- [x] 实现：`option_flow` 体内 after-close skip；`option_flow_close` 双时点 + `close+15min~close+55min` 窗口校验
- [x] GREEN → commit `feat: respect early closes in option jobs`

### Task 3: 保留清理

- [x] RED: `prune_option_flow(before)` 级联删除 scans/rows/outbox 并返回删除数、边界保留；`feishu_processed_messages` 清理；config retention_days<30 拒绝
- [x] 实现：config `option_flow.retention_days=120`；ledger 方法；maintenance 调用并记日志
- [x] GREEN → 全量 pytest+mypy → commit `feat: prune option flow history`
