# Strategy Validation Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让策略回测使用可实现的次日开盘成交口径，并补齐全市场扫描的稳健排名和 20 交易日绩效闭环。

**Architecture:** 把撮合时序提取为研究层纯函数，供动量和趋势闸门回测共用；扫描评分继续保持纯函数，数据源选择留在 pipeline；固定期限交易在绩效层独立构建，不伪造 SELL 台账信号。OOS 工具只消费历史 bars，不改生产参数。

**Tech Stack:** Python 3.11、pandas、vectorbt、pytest、mypy、DuckDB/SQLite。

---

### Task 1: 可实现的回测撮合

**Files:**
- Create: `src/quant_signal/research_execution.py`
- Modify: `research/backtest_momentum.py`
- Modify: `research/backtest_trend_gate.py`
- Test: `tests/test_research_execution.py`

- [ ] 写失败测试：目标权重在信号日为零、下一 bar 才生效；缺失开盘价不允许成交。
- [ ] 运行 `uv run pytest tests/test_research_execution.py -q`，确认因模块/函数缺失失败。
- [ ] 实现 `next_bar_targets()` 与 `build_next_open_portfolio()`；后者向 vectorbt 传入 `price=open`、`fees=0.0005`、`slippage=0.0005`。
- [ ] 两个主回测统一调用共享撮合函数，并在报告写明执行假设。
- [ ] 运行该测试及相关研究 smoke test。

### Task 2: 稳健扫描排名与完整成交量

**Files:**
- Modify: `src/quant_signal/scanner.py`
- Modify: `src/quant_signal/datafeed/alpaca_source.py`
- Modify: `src/quant_signal/pipelines/market_scan.py`
- Modify: `tests/test_scanner.py`
- Modify: `tests/test_market_scan.py`

- [ ] 写失败测试：因子使用截尾百分位；Alpaca 部分成交量标记会选择完整成交量扫描源；完整源失败时不产生候选。
- [ ] 运行目标测试，确认按预期失败。
- [ ] 用 5%/95% 截尾后的百分位替换 z-score；保持 40/30/30。
- [ ] 为 Alpaca 暴露 `partial_market_volume=True`；pipeline 对该源使用注入的 yfinance 扫描日线源。
- [ ] 扫描结束只缓存 Top1 的评价所需日线，卡片披露稳健排名与 20 日评价口径。
- [ ] 运行 scanner/market_scan 测试。

### Task 3: Top1 固定期限绩效闭环

**Files:**
- Modify: `src/quant_signal/performance.py`
- Modify: `src/quant_signal/pipelines/performance.py`
- Modify: `tests/test_performance.py`

- [ ] 写失败测试：market_scan 信号下一交易日开盘入场，第 20 个交易日收盘离场；不足 20 日为未平仓；无下一开盘跳过。
- [ ] 运行目标测试，确认函数缺失失败。
- [ ] 实现 `build_horizon_trades()`，并让普通往返构建器排除 `market_scan`。
- [ ] performance pipeline 读取扫描标的日线，合并固定期限交易后再汇总。
- [ ] 更新周报说明，避免继续声称按信号价成交。
- [ ] 运行 performance 与 pipeline 相关测试。

### Task 4: 滚动样本外实验门禁

**Files:**
- Create: `research/oos_validation.py`
- Modify: `research/walkforward.py`
- Test: `tests/test_oos_validation.py`

- [ ] 写失败测试：滚动切分的训练、测试区间严格不重叠且测试窗只位于训练窗之后。
- [ ] 运行目标测试，确认模块缺失失败。
- [ ] 实现滚动切分器和参数选择/测试记录结构，候选包含基线、排名缓冲和绝对动量门槛。
- [ ] 将旧脚本描述改为“确定性与未来函数检查”，不再声称 OOS。
- [ ] 生成或更新研究报告；若缓存不足则记录不可下结论，不改生产配置。
- [ ] 运行 OOS 单测与脚本 smoke test。

### Task 5: 全量验证

**Files:**
- Modify: `docs/superpowers/specs/2026-07-08-strategy-validation-hardening-design.md`（仅在实现与设计出现必要差异时）

- [ ] 运行 `uv run pytest -q`，要求 0 failures。
- [ ] 运行 `uv run mypy src`，要求 Success。
- [ ] 运行关键研究脚本或离线 smoke test，确认无同 bar 成交。
- [ ] 检查 `git diff --check` 与 `git status --short`，确认未覆盖用户备份改动。
