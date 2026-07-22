# 大市值分板块候选与 AI 上涨逻辑实施计划

> 当前会话内联执行，严格测试先行；不调用用户禁用的 skill，不覆盖无关工作区修改。

**目标：** 正式推荐只保留市值不少于 1000 亿美元的公司，按 GICS 行业每组 Top3、最多 3 个行业，并为每只推荐股生成有证据的上涨逻辑、行业地位、壁垒和反证。

**架构：** 新建独立公司画像与行业选择领域模块；yfinance 只负责提供原始画像，SQLite 负责 TTL 缓存。候选技术规则先产出宽候选池，再由确定性行业选择器过滤和排名；AI 只接收最终候选、结构化画像和近期新闻，不参与筛选与交易价位计算。

**技术栈：** Python 3.12、Pydantic、dataclasses、SQLite、yfinance、Alpaca News、pytest、mypy。

---

## 任务 1：公司画像与行业选择领域模型

**文件：** 新建 `src/quant_signal/company_profiles.py`，新建 `tests/test_company_profiles.py`，修改 `src/quant_signal/config.py`。

- [ ] 先写失败测试：1000 亿边界、缺画像/ETF fail closed、每行业 Top3、最多 3 个行业、稳定排序、策略排名和市值排名。
- [ ] 运行 `uv run pytest -q tests/test_company_profiles.py`，确认类型和选择函数缺失导致失败。
- [ ] 实现 `CompanyProfile`、`RankedSectorCandidate`、`select_sector_candidates()` 和配置字段。
- [ ] 重跑聚焦测试至通过。

## 任务 2：yfinance 画像源与 SQLite TTL 缓存

**文件：** 修改 `src/quant_signal/datafeed/fundamentals.py`、`src/quant_signal/ledger.py`、`tests/test_fundamentals.py`、`tests/test_ledger.py`。

- [ ] 写失败测试：字段映射、无效市值、标准 GICS 行业、成功缓存 7 天、失败缓存 6 小时、过期后刷新。
- [ ] 运行聚焦测试确认失败。
- [ ] 扩展 `FundamentalsSource.profiles()`，新增 `company_profiles` 表和缓存读写方法。
- [ ] 重跑聚焦测试至通过，原 `quality_flags()` 行为不变。

## 任务 3：候选管道接入大市值与行业配额

**文件：** 修改 `src/quant_signal/pipelines/us_briefing.py`、`src/quant_signal/candidate_lanes.py`、`tests/test_us_briefing_pipeline.py`、`tests/test_candidate_lanes.py`。

- [ ] 写失败测试：半导体候选不能挤掉其他有信号行业；低市值、缺画像、ETF 不进入正式候选；画像局部失败记录明确原因。
- [ ] 运行测试确认现有流程仍按 lane 取 Top3 而失败。
- [ ] 增加宽候选池输出，在统一简报 pipeline 中批量读取/刷新画像并调用行业选择器。
- [ ] 把行业、行业排名、市值和画像日期写入候选 payload 与运行语义哈希。
- [ ] 重跑候选与 pipeline 测试。

## 任务 4：有证据的 AI 公司研究

**文件：** 修改 `src/quant_signal/ai_briefing.py`、`src/quant_signal/pipelines/us_briefing.py`、`tests/test_ai_briefing.py`。

- [ ] 写失败测试：AI 只能分析最终 ticker；每股固定上涨逻辑/行业地位/壁垒/反证四行；每股不超过 220 字；拒绝未知 ticker、改写结构化排名和无证据绝对措辞。
- [ ] 运行测试确认新上下文与解析器不存在。
- [ ] 新增批量 `CompanyRationaleAIContext`、受限 prompt 和 `parse_company_rationales()`。
- [ ] pipeline 为最终候选读取最近 7 天最多 5 条 Alpaca 新闻并一次性调用 AI；失败时返回空研究映射。
- [ ] 重跑 AI 与 pipeline 测试。

## 任务 5：分行业卡片

**文件：** 修改 `src/quant_signal/notifier/cards.py`、`tests/test_cards.py`。

- [ ] 写失败测试：按 GICS 行业分节，每节最多3只；显示策略排名、市值排名、市值、画像日期及四行 AI；无 AI 时显示“研究分析暂不可用”。
- [ ] 运行测试确认旧平铺候选卡失败。
- [ ] 实现移动端分组渲染和长度降级顺序，保留价格、失效价、目标价与免责声明。
- [ ] 重跑卡片测试。

## 任务 6：回测与偏差标记

**文件：** 修改 `research/backtest_us_candidate_lanes.py`、`tests/test_us_candidate_replay.py`。

- [ ] 写失败测试：报告同时输出 baseline、sector-quota 和 current-large-cap-sensitivity；敏感性结果必须带 `CURRENT_PROFILE_LOOKAHEAD=true`。
- [ ] 运行测试确认报告字段缺失。
- [ ] 将行业配额选择器接入历史日循环，增加行业集中度和半导体占比指标。
- [ ] 支持从 JSON 读取当前公司画像；不允许把敏感性结果标记为无偏。
- [ ] 运行回测并生成 Markdown/JSON 报告。

## 任务 7：端到端验证与重启

**文件：** 修改 `config/settings.yaml`、必要 README/运维文档。

- [ ] 配置市值门槛、行业配额、画像 TTL、AI 字数和新闻窗口。
- [ ] 运行所有聚焦测试、`uv run pytest -q`、`uv run mypy src`、`git diff --check`。
- [ ] 使用仓库 `verify` 配方和 `.venv\\Scripts\\python.exe` 验证真实调度器装配；不污染 option-flow 生产 slot。
- [ ] 停止旧 `quant-signal` PID，使用现有部署入口启动新进程。
- [ ] 验证新 PID、命令行、端口/锁、调度任务、最新日志和连续失败计数。
