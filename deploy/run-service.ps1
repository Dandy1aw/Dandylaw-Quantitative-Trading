# quant-signal 常驻服务包装脚本
# 作用：拉起 .venv 里的 quant-signal.exe，崩溃/退出后自动重启。
#       每次运行写一份带时间戳的独立日志 logs\service-YYYYMMDD-HHmmss.log（UTF-8，实时更新），
#       重启历史记录在 logs\service-supervisor.log。
# 由计划任务 "quant-signal" 在登录/开机时调用（见 install-task.ps1）。
#
# 说明：
#  - 路径从 $PSScriptRoot 推导（deploy 的父目录即仓库根），不硬编码中文路径，
#    避免 Windows PowerShell 5.1 按 ANSI 读取无 BOM 脚本时把中文路径读乱。
#  - 用 Start-Process -Wait -PassThru 启动 exe（而非“ & exe *>> 文件”）：后者在
#    无控制台的计划任务上下文里会让 exe 立即退出；前者与手动启动行为一致、稳定，
#    还能拿到真实退出码；Python 以 UTF-8 输出，日志文件编码干净。

$ErrorActionPreference = 'Continue'

$env:PYTHONIOENCODING = 'utf-8'   # 让 Python 以 UTF-8 输出，日志编码一致
$env:PYTHONUNBUFFERED  = '1'

$Repo   = Split-Path -Parent $PSScriptRoot
$Exe    = Join-Path $Repo '.venv\Scripts\quant-signal.exe'
$LogDir = Join-Path $Repo 'logs'
$SupLog = Join-Path $LogDir 'service-supervisor.log'

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
Set-Location $Repo

function Write-Sup([string]$msg) {
    "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg |
        Add-Content -Path $SupLog -Encoding UTF8
}

Write-Sup "supervisor started (exe=$Exe)"

# 清理 14 天前的旧运行日志（每次重启会新建一份带时间戳的日志，避免无限累积）
Get-ChildItem $LogDir -Filter 'service-2*.log' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

$backoff = 5   # 崩溃后重启退避（秒），指数增长封顶 60s
while ($true) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $out   = Join-Path $LogDir "service-$stamp.log"
    $err   = Join-Path $LogDir "service-$stamp.err.log"

    Write-Sup "starting -> $out"
    $t0 = Get-Date
    try {
        $proc = Start-Process -FilePath $Exe -WorkingDirectory $Repo -NoNewWindow -PassThru `
            -RedirectStandardOutput $out -RedirectStandardError $err
        $proc.WaitForExit()
        $code = $proc.ExitCode
    } catch {
        Write-Sup ("start FAILED: " + ($_ | Out-String).Trim())
        $code = -1
    }
    $ran = (Get-Date) - $t0

    Write-Sup ("exited code=$code ran=$([int]$ran.TotalSeconds)s; restart in $backoff s")

    Start-Sleep -Seconds $backoff
    if ($ran.TotalSeconds -gt 120) { $backoff = 5 } else { $backoff = [Math]::Min($backoff * 2, 60) }
}
