# 注册 Windows 计划任务，让 quant-signal 开机自启、注销后仍运行、崩溃自动重启。
# 需以【管理员】身份运行：右键 PowerShell → 以管理员身份运行，再执行本脚本。
#
# 登录类型用 S4U：无需保存密码，注销后照常运行；只是拿不到需要用户凭证的
# 网络共享——本服务只做对外 HTTP（Alpaca/yfinance/飞书），不受影响。
#
# 路径从 $PSScriptRoot 推导，不硬编码中文路径（避免无 BOM 脚本被 PS5.1 读乱）。

$ErrorActionPreference = 'Stop'

$Repo     = Split-Path -Parent $PSScriptRoot
$Wrapper  = Join-Path $PSScriptRoot 'run-service.ps1'
$TaskName = 'quant-signal'
$UserId   = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path $Wrapper)) { throw "找不到包装脚本: $Wrapper" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Wrapper`""

$trigger = New-ScheduledTaskTrigger -AtStartup

$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "已注册计划任务 '$TaskName'（开机自启，账户 $UserId，S4U）。" -ForegroundColor Green

# 立即启动一次（无需等下次开机）
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5
$state = (Get-ScheduledTask -TaskName $TaskName).State
$info  = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
Write-Host ("当前状态: {0}  上次运行结果: 0x{1:X}" -f $state, $info.LastTaskResult)
Write-Host "日志: $Repo\logs\service-YYYYMMDD.log" -ForegroundColor Cyan
