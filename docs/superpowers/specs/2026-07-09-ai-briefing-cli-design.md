# AI 早报观点 CLI 接入设计

## 目标

在每天盘前早报生成后，额外调用本机 AI CLI 输出一张“AI早报观点”卡片。第一版只支持 Claude Code CLI 与 Codex CLI，不接 API。

## 范围

- AI 只做观点解读，不生成交易信号、不改持仓、不写台账。
- 原早报、动量榜单、利空错杀等既有链路保持原样。
- CLI 超时、返回非 0、输出为空或解析失败时，仅记录日志并跳过 AI 卡片。

## 接入方式

新增配置 `ai_briefing`：

- `enabled`: 是否启用。
- `provider`: `claude_code_cli` 或 `codex_cli`。
- `command`: CLI 可执行文件，默认分别是 `claude` / `codex`。
- `timeout_seconds`: 子进程超时。
- `max_chars`: prompt 最大长度，防止把过长上下文交给 CLI。

Claude Code 使用非交互 print mode：`claude -p <prompt>`。

Codex 使用非交互 exec：`codex exec --sandbox read-only --skip-git-repo-check <prompt>`。

## 数据流

`premarket.run()` 生成：

1. 今日去重后可推送信号。
2. 早报卡片。
3. 动量全池榜单卡片。
4. AI 上下文摘要。

若启用 `ai_briefing`，在动量榜单之后调用 CLI provider，将返回文本包装成 `🤖 AI早报观点` 报告卡发送。

## Prompt 约束

Prompt 必须要求 AI：

- 只能评论输入里的量化信号。
- 不得新增未出现的交易标的或交易动作。
- 明确区分“量化信号”和“主观观点”。
- 输出简洁中文 Markdown。
- 包含“仅供观察，不构成投资建议”。

## 测试策略

- provider 命令参数测试：Claude Code 与 Codex CLI 分别生成正确命令。
- provider 降级测试：超时、非 0、空输出返回 `None`。
- prompt 测试：不包含环境变量、webhook、secret 等敏感内容。
- 卡片测试：AI 文本被渲染为报告卡。
- premarket 集成测试：启用时发送 AI 卡；失败时不影响原卡。
