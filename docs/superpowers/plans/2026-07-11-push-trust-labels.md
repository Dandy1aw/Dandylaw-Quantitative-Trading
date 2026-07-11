# Push Trust Labels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 行动卡标注账户快照超龄；SELL 信号卡与早报标注历史低胜率。

**Architecture:** 纯展示层：execution_brief 组卡处加超龄行；cards.py 加 SELL 脚注常量。spec: docs/superpowers/specs/2026-07-11-push-trust-labels-design.md。

**Tech Stack:** Python 3.12, pytest, mypy strict.

## Global Constraints

- 不改变 sizing/BLOCKED/去重/信号生成逻辑，纯文案追加
- 阈值复用 execution_plan.screenshot_max_age_hours，不新增配置
- mypy strict、全量 pytest 全绿

---

### Task 1: 账户快照超龄提醒

- [ ] RED: 行动卡在快照新鲜/超龄/从未导入三种情形下的提醒文案
- [ ] 实现：execution_brief 组卡处按 observed_at 计算年龄追加 ⚠️ 行
- [ ] GREEN → commit `feat: warn on stale screenshot accounts`

### Task 2: SELL 低胜率脚注

- [ ] RED: signal_card SELL 有脚注/BUY 无；早报含 SELL 一条脚注、多 SELL 不重复、纯 BUY 无
- [ ] 实现：cards.py 常量 `_SELL_RELIABILITY_NOTE` + signal_card/早报表格脚注
- [ ] GREEN → 全量 pytest+mypy → commit `feat: label sell signals as low reliability`
