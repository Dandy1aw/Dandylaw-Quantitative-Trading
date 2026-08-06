# 外部研究基准适配层设计

## 目标

允许当前量化系统只读导入 `worth-buy-stocks` 的 canonical backtest JSON，校验语义哈希和关键合约后生成标准化摘要，用于后续与本系统研究产物比较。

## 边界

- 外部 Skill 独立安装和运行，本项目不自动下载、更新或执行它。
- 不复制外部仓库源码，不导入其 Python 包，不依赖其 SQLite 文件。
- 只使用公开 JSON 合约实现互操作；外部仓库当前没有明确 LICENSE，不进行源码重分发。
- 外部结论只是 research benchmark，不写入交易信号、持仓或执行计划。

## 输入合约

输入必须是 JSON object，至少包含：

- `artifact_version`
- `contract_version`
- `model_version`
- `semantic_hash`
- `generated_at`
- `config`
- `provenance`
- `validation.status`

`validation.status` 只允许 `supports`、`inconclusive`、`contradicts` 或 `invalid_run`。`config` 必须包含单一 `symbol`、`start`、`end`、`data_source`、`feed` 和 `adjustment`。

## 语义哈希校验

校验时复制 JSON 值，删除顶层 `semantic_hash` 和 `generated_at`，同时删除 `provenance.db_path`；使用 UTF-8、key 排序、紧凑 JSON 和 SHA-256 重算。不允许 NaN/Infinity。哈希不匹配必须拒绝导入。

## 输出

输出不包含 bars 或 timeline，只包含：来源、版本、模型版本、语义哈希、生成时间、标的、研究区间、数据模式、feed、复权口径、验证状态和 warning 数量。

命令行入口：

```powershell
python -m quant_signal.external_benchmark C:\path\to\artifact.json
```

stdout 只输出一个标准 JSON object；校验失败退出码非零并向 stderr 输出错误。

## 验收

- 有效外部产物可转换为确定性摘要。
- 篡改内容但未更新哈希时必须拒绝。
- 缺失字段、错误类型、不支持状态和非有限数值必须拒绝。
- 摘要不携带价格 bars、评分 timeline 或账户数据。
- 适配层不依赖外部仓库存在，单元测试全部离线。
