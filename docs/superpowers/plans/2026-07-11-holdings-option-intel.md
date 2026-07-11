# 持仓期权情报层实现计划

Spec: `docs/superpowers/specs/2026-07-11-holdings-option-intel-design.md`
每阶段 TDD：先写失败测试 → 实现 → 全绿提交。

## Phase 1 领域层（纯函数，无 IO）

- [ ] `src/quant_signal/options_intel.py`：`OptionChainContract` /
  `TopOIStrike` / `OptionIntel` / `OptionIntelPolicy` 数据类
- [ ] `compute_intel()`：ATM 选择、expected move、跨财报到期、ATM IV、
  RV20、P/C 量比与 OI 比、大 OI 行权价、降级 None 语义
- [ ] tests/test_options_intel.py 覆盖 spec §6 领域层全部用例

## Phase 2 数据层

- [ ] `alpaca_options.py` 新增 `AlpacaOptionChainSource`：
  `fetch_chain(underlying, session, max_expiry_days) -> tuple[OptionChainContract, ...]`
  （snapshots 分页 + contracts OI 分页 + 合并；404→空；页数上限）
- [ ] tests：mock client 分页/过滤/404/上限降级

## Phase 3 配置 + 台账

- [ ] `config.py` `OptionIntelSettings`（含校验）+ `Settings.option_intel`
- [ ] `ledger.py`：`option_intel_daily` 建表 + `save_option_intel_daily`
  （UNIQUE(session,symbol) upsert）+ `prune_option_intel(before)`
- [ ] maintenance 接入 prune

## Phase 4 管道 + 卡片 + 调度

- [ ] `notifier/cards.py` `option_intel_card(intels, session)`（多段+尾注）
- [ ] `pipelines/option_intel.py` `run(engine, now)`：持仓集合
  （observed ∪ virtual，剔非 USD，max_tickers 截断）、逐标的 fail-open、
  落库、发送
- [ ] engine 装配 `option_chain_source` / `run_option_intel`
- [ ] scheduler `option_intel` job（13:40/16:40 ET 双时点 + close 窗口校验）
- [ ] option_flow 卡片 📌 持仓交叉标记（cards 参数注入，pipeline 取持仓集合）

## Phase 5 bot 指令

- [ ] `feishu_bot.py`：`期权 <ticker>` 解析（OPTION_INTEL intent + arg）、
  现场拉取回复、非法参数文案；无参数走旧路径

## Phase 6 收尾

- [ ] settings.yaml 示例段 + README 新章节 + 非目标补充
- [ ] 全量 pytest + mypy strict
- [ ] 真实冒烟：拉 MU 链渲染卡片（console）
- [ ] 提交
