# UZI-Skill 深度分析信息增强设计

> 本文档是对 [`quant-signal-spec.md`](../../../quant-signal-spec.md) 的增量扩展。
> 背景：quant-signal 目前有 4 个纯函数量化策略（动量轮动/RSI回归/MACD/布林带），
> 用户希望额外接入本机已安装的 UZI-Skill（`stock-deep-analyzer` 插件，65位
> "投资大佬"规则化评分 + 财务建模 + 杀猪盘检测）作为**信息增强**，不是替代
> 现有策略——量化信号照常产出，UZI-Skill 只是给已有信号补充一层独立的
> 定性/半定量参考视角。

## 1. 关键技术前提（已实测验证）

- UZI-Skill 的 `run.py` 可以在设置 `UZI_CLI_ONLY=1` 环境变量后**纯 headless
  子进程运行**，无需 Claude 会话。实测对 MU 跑 `--depth lite`：18.8 秒完成
  数据抓取，全程（含报告组装）不到 1 分钟
- 结构化结果写在 `{run.py 所在目录}/.cache/{ticker}/synthesis.json`，关键
  字段：`overall_score`（float，0-100 综合评分）、`verdict_label`（结论，如
  "谨慎 · 1派看多/6派看空"）、`panel_consensus`（float）、`risks`（风险点
  字符串列表）
- 缺少 Claude agent 生成的 `agent_analysis.json` 时会降级为"脚本骨架
  synthesis"，`agent_reviewed=False`，报告质量打折但不会报错/中断——这正是
  headless 场景的预期路径，UZI-Skill 自身设计为容忍这种情况
- 用户本机系统 Python 已经装好 UZI-Skill 所需依赖（akshare/baostock/
  playwright/rich），无需额外配置即可跑通
- 实测发现真实价值案例：MU（我们系统当前动量排名第一的 BUY 目标）在
  UZI-Skill 里综合评分仅 49.8/100、"谨慎 · 1派看多/6派看空"——量化动量
  信号和这层深度分析给出了相反方向的参考，这恰好是增强层该捕捉的分歧信号

## 2. 架构与数据流

```
Engine.run_enrichment(now)
    │
    ├─ 监控范围 = 持仓(ledger.get_holdings) ∪ 今日全部策略BUY信号(ledger.signals_on)
    │   （去重后按 max_tickers 截断）
    │
    ├─ 对每个标的调用 enrichment.run_uzi_analysis(ticker, ...)
    │     │
    │     ├─ subprocess: UZI_CLI_ONLY=1 python run.py {ticker} --no-browser --depth lite
    │     ├─ 读 {run.py目录}/.cache/{ticker}/synthesis.json
    │     └─ 失败/超时/解析错误 → 返回 None，记 warning，跳过该标的（不中断其余标的）
    │
    └─ 汇总成功的结果 → 组一张"🔍 深度分析"卡 → notifier.send
```

不复用 momentum_rotation 等策略的 `Signal`/`generate(bars)` 接口——这层增强
产出的不是可回测的交易信号，是外部工具的定性/半定量参考，混进 Signal 体系
会破坏"纯函数、可回测"的既有契约。用独立的数据结构和独立的卡片。

## 3. 新模块：`src/quant_signal/enrichment.py`

```python
def run_uzi_analysis(
    ticker: str,
    run_py_path: Path,
    python_exe: str = "python",
    depth: str = "lite",
    timeout_seconds: int = 120,
) -> dict[str, object] | None:
    """headless 调用 UZI-Skill，返回 synthesis.json 里的关键字段，失败返回 None。"""
```

内部：
1. `env = {**os.environ, "UZI_CLI_ONLY": "1", "UZI_NO_AUTO_OPEN": "1"}`
2. `subprocess.run([python_exe, str(run_py_path), ticker, "--no-browser", "--depth", depth], env=env, timeout=timeout_seconds, capture_output=True)`
3. `synthesis.json` 路径 = `run_py_path.parent / ".cache" / ticker / "synthesis.json"`
   （UZI-Skill 内部会自行 `os.chdir` 到它自己的 scripts 目录，缓存相对该目录）
4. 提取 `ticker, name, overall_score, verdict_label, panel_consensus, risks`
5. 任何异常（`subprocess.TimeoutExpired`、非零退出码、文件不存在、JSON 解析
   失败、字段缺失）一律捕获，记 `log.warning`，返回 `None`——这是尽力而为的
   增强层，不允许任何异常向上传播影响主流程

## 4. 配置（settings.yaml 新增 `enrichment` 顶层块）

```yaml
enrichment:
  enabled: false           # 默认关闭；需要用户本机已装好 UZI-Skill 才有意义
  uzi_run_py: ""            # UZI-Skill run.py 的绝对路径，用户自行填写
  python_exe: "python"      # 调用 UZI-Skill 用的解释器（独立于 quant-signal 自己的 uv 环境）
  depth: lite               # lite | medium | deep，对应 UZI-Skill 的 --depth
  timeout_seconds: 120
  max_tickers: 8            # 每天最多分析几只，控制总耗时（每只约30-60秒）
```

`enabled: false` 时，`Engine.run_enrichment` 直接跳过（不调度、不报错）。
`Settings` 模型新增对应字段，`config.py` 按已有模式加一个 `EnrichmentSettings`
子模型即可，无需改动 `.env`（这个功能不涉及新的密钥）。

## 5. Engine 新方法：`run_enrichment(now)`

```python
def run_enrichment(self, now: datetime) -> None:
    cfg = self.settings.enrichment
    if not cfg.enabled:
        return
    held = set(self.ledger.get_holdings(self.momentum.strategy_id))
    today_buys = {
        r["ticker"] for r in self.ledger.signals_on(now.date()) if r["direction"] == "buy"
    }
    watch_set = sorted(held | today_buys)[: cfg.max_tickers]
    if not watch_set:
        return

    results = []
    for ticker in watch_set:
        r = run_uzi_analysis(ticker, Path(cfg.uzi_run_py), cfg.python_exe, cfg.depth, cfg.timeout_seconds)
        if r is not None:
            results.append(r)
    if not results:
        return
    self.notifier.send(build_enrichment_card(results, held))
```

`build_enrichment_card`（放 `notifier/cards.py`）：每行标的 + 综合评分 +
结论 + 主要风险（取前 1-2 条）；若某标的的 `overall_score < 50` 或
`verdict_label` 含"看空"/"谨慎"关键词，而该标的同时是我们系统今日的 BUY
目标，**这一行额外加 ⚠️ 前缀**，明确提示信号分歧（如实测的 MU 案例）。

## 6. 调度

新增 `enrichment` job：工作日、NYSE 交易日历门控（与 premarket 一致，
含义是"美股开盘相关的深度分析"）、**08:45 ET**（晚于 08:00 ET 的 premarket
15 分钟，确保当天 BUY 目标已经算出来）。`enabled=false` 时 job 仍注册，
但 `run_enrichment` 内部直接 return，不做任何调用——保持"job 列表固定，
行为由配置控制"的一致性，不需要动态增删 job。

## 7. 测试范围

- `enrichment.run_uzi_analysis`：mock `subprocess.run` + 临时目录下伪造
  `synthesis.json`，覆盖成功解析、非零退出码、超时、JSON 缺字段四种场景，
  全部走 graceful-None 路径
- `Engine.run_enrichment`：`enabled=false` 时验证不调用 `run_uzi_analysis`；
  `enabled=true` 时验证监控范围计算（持仓∪今日BUY，按 max_tickers 截断）、
  推送卡片、分歧标注逻辑
- `scheduler.py`：新增 job 的注册断言

## 8. 明确排除的范围

- 不解析/展示 UZI-Skill 生成的完整 HTML 报告（那份报告仅供用户自己需要时
  手动打开查看，`enrichment` 卡片只放结构化摘要）
- 不尝试触发或管理 `agent_analysis.json`（那需要真实 Claude 会话，超出本次
  headless 集成范围；缺失时用 UZI-Skill 自己的降级路径，报告质量打折但
  可用）
- 不做 A股标的的适配验证（用户当前持仓是美股/港股/韩股，UZI-Skill 对
  A股支持更完整，但本次不测试，行为未知）
- `depth` 目前只验证过 `lite`，`medium`/`deep` 档位耗时未知，配置项留着
  但不在本次验收范围内强制测试
