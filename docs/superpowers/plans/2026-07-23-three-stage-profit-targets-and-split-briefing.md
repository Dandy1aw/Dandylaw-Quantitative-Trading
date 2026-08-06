# 三级止盈与拆分早报实施计划

> **执行说明：** 本计划在当前任务内按 TDD 逐项执行；不使用 `using-superpowers` skill。当前工作区包含上一阶段尚未提交的行业候选功能，因此只做精确补丁，不创建提交。

**目标：** 将候选止盈从“宽止损 × 固定风险收益倍数”改为 +5%/+10%/+15% 三级止盈，用可回测的近期买盘资金强度选择建议档位，并把早报总览与行业候选拆开、中文化。

**架构：** `candidate_lanes.py` 负责从日线 OHLCV 计算买盘资金代理、三级止盈和建议档位；`us_briefing.py` 保留结构化字段并发送一张行动总览及若干行业候选卡；`cards.py` 只负责中文展示和阻断原因翻译。`target_price` 保持为下游单一止盈兼容字段，其值等于当前建议档位，完整三级价位另存为结构化字段。

**技术栈：** Python 3.12、pandas、Pydantic、pytest、Feishu 卡片通知。

---

### 任务一：三级止盈与买盘资金强度

**文件：**
- 修改：`src/quant_signal/candidate_lanes.py`
- 修改：`src/quant_signal/config.py`
- 测试：`tests/test_candidate_lanes.py`

- [ ] 先写失败测试：三级止盈严格等于 `entry_high × 1.05/1.10/1.15`。
- [ ] 先写失败测试：近期上涨日放量且收盘靠近日高时建议第三档，弱买盘只建议第一档。
- [ ] 先写失败测试：近期阻力位会降低建议档位，但不改写三个固定止盈点。
- [ ] 运行候选测试，确认因缺少结构化字段和计算逻辑而失败。
- [ ] 增加 `profit_target_1/2/3`、`recommended_target_stage`、`recent_buying_notional`、`buying_pressure_score/label` 和 `nearby_resistance`。
- [ ] 以最近5日上涨日成交额及收盘区间位置估算买盘资金，以20日成交额基线归一化；明确该指标不是逐笔净流入。
- [ ] 将兼容字段 `target_price` 设为建议档位对应价格，移除宽止损对止盈价的放大作用。
- [ ] 运行候选测试并确认通过。

### 任务二：中文行业、阻断原因与候选文案

**文件：**
- 修改：`src/quant_signal/notifier/cards.py`
- 测试：`tests/test_cards.py`

- [ ] 先写失败测试：Technology、Semiconductors、Computer Hardware 分别显示为科技、半导体、计算机硬件。
- [ ] 先写失败测试：`STOP_TOO_WIDE` 显示为“仅观察：止损距离过大”，不再显示成可买入候选。
- [ ] 先写失败测试：候选展示三级止盈、建议档位、买盘资金金额和强度说明。
- [ ] 运行卡片测试，确认新文案断言失败。
- [ ] 增加集中式中文标签与美元金额中文单位格式化。
- [ ] 渲染“止盈1/2/3”和“买盘资金强度”，并保留股票代码及 ATR 等专用名词。
- [ ] 运行卡片测试并确认通过。

### 任务三：拆分行动总览与行业候选通知

**文件：**
- 修改：`src/quant_signal/notifier/cards.py`
- 修改：`src/quant_signal/pipelines/us_briefing.py`
- 测试：`tests/test_cards.py`
- 测试：`tests/test_us_briefing_pipeline.py`

- [ ] 先写失败测试：卡片工厂返回一张行动总览及按中文行业分组的候选卡。
- [ ] 先写失败测试：总览不包含逐公司长文；行业卡仅包含本行业候选。
- [ ] 先写失败测试：通知链路全部发送成功才将运行标记为已送达。
- [ ] 运行相关测试，确认缺少拆分卡片接口而失败。
- [ ] 保留 `us_briefing_card` 兼容接口，新增拆分卡片接口供正式 pipeline 使用。
- [ ] 总览保留市场、持仓、风险、数据边界和精简 AI 观点；候选按行业拆卡。
- [ ] pipeline 顺序发送全部卡片，并把所有标题写入审计载荷。
- [ ] 运行卡片与 pipeline 测试并确认通过。

### 任务四：AI 文案约束与端到端验证

**文件：**
- 修改：`src/quant_signal/ai_briefing.py`
- 测试：`tests/test_ai_briefing.py`

- [ ] 先写失败测试：AI 提示要求中文、百分比和“亿美元”格式，禁止裸露长整数。
- [ ] 运行 AI 测试并确认新约束缺失。
- [ ] 更新提示词，只允许解释结构化止盈和买盘强度，不得自行修改价格。
- [ ] 运行候选、卡片、pipeline、AI 测试。
- [ ] 运行完整 pytest、mypy 和仓库差异检查。
- [ ] 使用生产调度器的真实 pipeline 做不发送的端到端预演，确认卡片数量、中文文案和三级止盈；真实发送与服务重启仅在当前用户请求明确包含时执行。
