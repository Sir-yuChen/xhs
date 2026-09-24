# 停止 XHS_ALL_IN_ONE 的常驻任务与两个监听进程。
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\work\XHS\.workbuddy\scripts\stop-app.ps1
#
# 卸载计划任务（不再开机自启）：
#   Unregister-ScheduledTask -TaskName 'XHS-AllInOne-App' -Confirm:$false
$ErrorActionPreference = 'Continue'

$taskName = 'XHS-AllInOne-App'

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName
    Write-Host "stopped task: $taskName"
}

# 计划任务停掉后，子进程（uvicorn / vite）可能仍在监听，按端口收尾
foreach ($port in 8000, 5173) {
    $connections = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        try {
            Stop-Process -Id $connection.OwningProcess -Force -ErrorAction Stop
            Write-Host ("killed pid {0} on port {1}" -f $connection.OwningProcess, $port)
        } catch {
            Write-Host ("failed to kill pid {0} on port {1}: {2}" -f $connection.OwningProcess, $port, $_)
        }
    }
}

Start-Sleep -Seconds 2
$left = Get-NetTCPConnection -State Listen -LocalPort 8000, 5173 -ErrorAction SilentlyContinue
if ($left) {
    Write-Host 'WARN: 仍有监听：'
    $left | Format-Table -AutoSize | Out-String | Write-Host
} else {
    Write-Host 'ports 8000 / 5173 released'
}
