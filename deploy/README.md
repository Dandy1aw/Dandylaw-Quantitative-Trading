# 本地 24 小时部署（Windows）

把 quant-signal 作为常驻服务跑在本机：开机自启、崩溃自动重启、日志落盘。
后续要迁云 VPS 时，这套包装脚本的思路（supervisor 重启循环 + 时间戳日志）可直接照搬成 systemd。

## 组成

| 文件 | 作用 |
|---|---|
| `run-service.ps1` | 监护脚本：循环拉起 `.venv\Scripts\quant-signal.exe`，退出后按退避自动重启；每次运行写一份 `logs\service-YYYYMMDD-HHmmss.log`（UTF-8，实时），重启历史写 `logs\service-supervisor.log`，自动清理 14 天前旧日志 |
| `install-task.ps1` | 注册计划任务（**管理员**）：开机自启 + 注销后仍运行（S4U，免存密码）+ 失败重试 |
| `uninstall-task.ps1` | 停止并删除计划任务（**管理员**） |
| `status.ps1` | 查看任务/进程状态与最近日志（无需管理员） |

> 脚本一律从 `$PSScriptRoot` 推导仓库路径，不硬编码中文路径——否则 Windows PowerShell 5.1
> 会按 ANSI 读取无 BOM 的 `.ps1`，把 `量化交易` 读成乱码导致找不到路径。

## 两种运行档位

### A. 登录即启动（当前已装，无需管理员）
用户登录 Windows 后自动拉起；注销/切换用户会停。适合"机器常开且保持登录"的桌面。
本次已用此档位注册并启动，服务正在运行。

### B. 开机自启 + 注销后仍运行（推荐，需管理员一次）
**以管理员身份**打开 PowerShell，执行：
```powershell
& 'D:\claudeCode\量化交易\deploy\install-task.ps1'
```
用 S4U 登录类型：无需保存密码，注销后照常运行（本服务只做对外 HTTP：Alpaca/yfinance/飞书，
不依赖需要用户凭证的网络共享，S4U 完全够用）。装完即时启动一次，之后每次开机自动运行。

## 必做：关掉睡眠/休眠（否则睡着就不推信号）
```powershell
powercfg /change standby-timeout-ac 0     # 接电源永不睡眠
powercfg /change hibernate-timeout-ac 0
```
笔记本合盖也要跑的话，再到"控制面板 → 电源选项 → 选择合上盖子的功能"设为"不采取任何操作"。

## 日常操作

```powershell
# 看状态 + 最近日志
& 'D:\claudeCode\量化交易\deploy\status.ps1'

# 手动重启（停任务→进程会退出→再启动）
Stop-ScheduledTask -TaskName quant-signal; Start-ScheduledTask -TaskName quant-signal

# 只停进程（supervisor 会在几秒后自动拉起）——真正停服务要停/删任务
Get-Process quant-signal | Stop-Process -Force

# 彻底停止：停并禁用任务
Stop-ScheduledTask -TaskName quant-signal
Get-Process quant-signal -ErrorAction SilentlyContinue | Stop-Process -Force

# 卸载（管理员）
& 'D:\claudeCode\量化交易\deploy\uninstall-task.ps1'
```

## 前置条件（本机已满足）
- `uv` 已装，`.venv` 已构建（`.venv\Scripts\quant-signal.exe` 存在）。若换机器：`uv sync` 重建。
- `config\.env` 存在且含 `ALPACA_KEY / ALPACA_SECRET / FEISHU_WEBHOOK`（gitignore，不入库）。
- `data\`（duckdb + sqlite）随仓库持久化，重启不丢台账。
- 系统时钟建议保持 NTP 同步（调度按美东/UTC 时刻触发）。

## 排障
- 服务没推送？先 `status.ps1` 看进程在不在、`service-supervisor.log` 有没有反复 `exited`。
- 反复秒退：多为 `config\.env` 缺失/凭证失效，或 duckdb 被其他进程占用。看当前
  `service-*.log` / `service-*.err.log` 里的 Python 报错。
- 注意别用"进程内流重定向"（`& exe *>> 文件`）启动：计划任务无控制台上下文下会让 exe 秒退，
  本脚本已改用 `Start-Process -PassThru + WaitForExit`。
