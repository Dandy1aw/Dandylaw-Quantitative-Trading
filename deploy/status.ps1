# 查看 quant-signal 服务状态与最近日志（无需管理员）。
$Repo   = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Repo 'logs'

$task = Get-ScheduledTask -TaskName 'quant-signal' -ErrorAction SilentlyContinue
if ($task) {
    $info = $task | Get-ScheduledTaskInfo
    Write-Host ("计划任务: {0}  上次运行: {1}  上次结果: 0x{2:X}" -f `
        $task.State, $info.LastRunTime, $info.LastTaskResult) -ForegroundColor Cyan
    $triggerTypes = @($task.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ', '
    Write-Host ("身份: LogonType={0}  RunLevel={1}  Triggers={2}" -f `
        $task.Principal.LogonType, $task.Principal.RunLevel, $triggerTypes) -ForegroundColor Cyan
} else {
    Write-Host "计划任务 'quant-signal' 未注册。" -ForegroundColor Yellow
}

$proc = Get-Process quant-signal -ErrorAction SilentlyContinue
if ($proc) {
    Write-Host ("进程在运行: PID {0}  内存 {1:N0} MB  启动 {2}" -f `
        $proc.Id, ($proc.WorkingSet64/1MB), $proc.StartTime) -ForegroundColor Green
} else {
    Write-Host "进程未在运行。" -ForegroundColor Yellow
}

$sup = Join-Path $LogDir 'service-supervisor.log'
if (Test-Path $sup) {
    Write-Host "`n--- 监护日志末尾 8 行（重启历史）---" -ForegroundColor Cyan
    Get-Content $sup -Tail 8
}

$latest = Get-ChildItem $LogDir -Filter 'service-2*.log' -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike '*.err.log' } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($latest) {
    Write-Host "`n--- 当前运行日志末尾 20 行 ($($latest.Name)) ---" -ForegroundColor Cyan
    Get-Content $latest.FullName -Tail 20
} else {
    Write-Host "`n暂无运行日志。" -ForegroundColor Yellow
}
