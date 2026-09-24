# 注册（或重新注册）XHS_ALL_IN_ONE 的常驻计划任务 XHS-AllInOne-App。
#
# 形态对齐 D:\hindsight\scripts\register-cp-task.ps1（那台机器上已被验证过的做法）：
#   登录触发 + 不因电池停止 + 无执行时长上限 + 失败重启 3 次 + 只保留一个实例。
#
# 与 Hindsight 那几个任务的唯一差别：这里用 **pythonw.exe** 跑 Python 监督进程
# （run-app.py，内部再拉起 uvicorn 与 vite），因此**全程没有控制台窗口**。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\work\XHS\.workbuddy\scripts\register-app-task.ps1
#
# 停止 / 卸载：
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\work\XHS\.workbuddy\scripts\stop-app.ps1
#   Unregister-ScheduledTask -TaskName 'XHS-AllInOne-App' -Confirm:$false
$ErrorActionPreference = 'Stop'

$taskName = 'XHS-AllInOne-App'
$pythonw = 'C:\Users\admin\AppData\Local\Programs\Python\Python312\pythonw.exe'
$launcher = 'D:\work\XHS\.workbuddy\scripts\run-app.py'
$root = 'D:\work\XHS'

if (-not (Test-Path $pythonw)) {
    Write-Host "ERROR: pythonw not found: $pythonw"
    exit 1
}
if (-not (Test-Path $launcher)) {
    Write-Host "ERROR: launcher not found: $launcher"
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument ('"' + $launcher + '"') `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

$desc = 'XHS_ALL_IN_ONE local app: FastAPI(8000, serves frontend/dist) + Vite dev server(5173) on 127.0.0.1, real DB data/spider_xhs.db'

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description $desc `
    -Force | Out-Null

Write-Host ("registered: " + $taskName)

Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 6
Write-Host ("state: " + (Get-ScheduledTask -TaskName $taskName).State)
