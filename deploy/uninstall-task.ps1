# 停止并删除 quant-signal 计划任务（以管理员身份运行）。
$ErrorActionPreference = 'Continue'
$TaskName = 'quant-signal'

try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "已删除计划任务 '$TaskName'。运行中的进程会在下次退出后不再拉起。" -ForegroundColor Yellow
Write-Host "如需立即停止仍在跑的进程: Get-Process quant-signal | Stop-Process" -ForegroundColor Yellow
