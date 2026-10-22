# XHS 本地常驻服务 —— Windows 计划任务注册脚本
#
# 为什么需要它：
#   常驻服务由计划任务 `XHS-AllInOne-App` 拉起 launcher（scripts/run-app.py）。
#   计划任务是**系统状态，不在版本库里** ⇒ 换机器 / 重装系统后它就不存在了，
#   于是「服务跑一会儿就没了 / 后端老是自己死掉」这类问题会原样重演。
#   ⇒ 把创建过程脚本化，换机器时一条命令重建。
#
# 用法（**普通权限即可**，不要用管理员跑到别的用户下）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Unregister
#
# 三个设计要点（每一条都对应一次真实故障）：
#   1. **每 5 分钟兜底触发**：只有登录触发时，launcher 被杀 / 被系统回收后没人拉起，
#      时间一长就成了「服务自己死了」。有兜底后 5 分钟内自动恢复。
#   2. **MultipleInstances = IgnoreNew**：兜底触发会反复命中，若允许并行就会起第二个
#      launcher 抢 8000，抢不到的那个每 5 秒崩一次，把日志刷爆（历史故障形态）。
#      （launcher 自己还有 msvcrt 单实例锁，这里是第二道保险。）
#   3. **ExecutionTimeLimit = PT0S**：常驻进程不能被执行时限杀。设成 3 天的话，
#      第 3 天服务就会"莫名其妙"消失。

param(
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

$TaskName = 'XHS-AllInOne-App'
$Root     = Split-Path -Parent $PSScriptRoot          # scripts\ 的上一级 = 项目根
$Launcher = Join-Path $Root 'scripts\run-app.py'
$PythonW  = 'C:\Users\admin\AppData\Local\Programs\Python\Python312\pythonw.exe'
$UserId   = "$env:USERDOMAIN\$env:USERNAME"

function Write-Step($msg) { Write-Host "  - $msg" }

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已删除计划任务 $TaskName"
    } else {
        Write-Host "计划任务 $TaskName 不存在，无需删除"
    }
    return
}

Write-Host "注册计划任务 $TaskName"
Write-Step "项目根 : $Root"
Write-Step "launcher: $Launcher"
Write-Step "pythonw : $PythonW"
Write-Step "运行用户: $UserId"

if (-not (Test-Path $Launcher)) { throw "launcher 不存在：$Launcher" }
if (-not (Test-Path $PythonW))  { throw "pythonw 不存在：$PythonW" }

# --- 触发器：登录一次 + 每 5 分钟兜底 -------------------------------------
# StartBoundary 用固定过去时刻，避免"未来才生效"；Repetition 无 Duration = 永久重复。
$rep = New-ScheduledTaskTrigger -Once -At '2020-01-01T00:00:00' `
    -RepetitionInterval (New-TimeSpan -Minutes 5)

$logon = New-ScheduledTaskTrigger -AtLogOn -User $UserId

# --- 动作 ----------------------------------------------------------------
# 工作目录必须显式给：pythonw 从计划任务启动时的默认 cwd 是 system32，
# 相对路径（config/、data/）全会指错地方。
$action = New-ScheduledTaskAction -Execute $PythonW `
    -Argument ('"{0}"' -f $Launcher) `
    -WorkingDirectory $Root

# --- 设置 ----------------------------------------------------------------
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# --- 主体：交互式令牌（要读写用户目录下的数据） --------------------------
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName `
    -Action $action `
    -Trigger @($logon, $rep) `
    -Settings $settings `
    -Principal $principal `
    -Description 'XHS 本地常驻服务 launcher' `
    -Force | Out-Null

# --- 回读校验（不靠"注册成功"这句话，靠实际读回来的值） ------------------
$t = Get-ScheduledTask -TaskName $TaskName
$triggers = ($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ', '
Write-Host ""
Write-Host "已注册，回读校验："
Write-Step "触发器   : $triggers"
Write-Step "动作     : $($t.Actions[0].Execute) $($t.Actions[0].Arguments)"
Write-Step "工作目录 : $($t.Actions[0].WorkingDirectory)"
Write-Step "多实例   : $($t.Settings.MultipleInstances)"
Write-Step "执行时限 : $($t.Settings.ExecutionTimeLimit)"

# 兜底触发是否真的带 5 分钟重复（这是本脚本的核心，单独断言）
$withRep = $t.Triggers | Where-Object { $_.Repetition.Interval -eq 'PT5M' }
if (-not $withRep) { throw "兜底触发器没有 5 分钟重复周期 —— 服务死掉后不会自动恢复" }
if ($t.Settings.MultipleInstances -ne 'IgnoreNew') { throw "MultipleInstances 不是 IgnoreNew —— 会起两个 launcher 抢端口" }
if ($t.Settings.ExecutionTimeLimit -ne 'PT0S') { throw "执行时限不是 PT0S —— 常驻进程会被超时杀掉" }

Write-Host ""
Write-Host "全部校验通过。现在启动一次：" -ForegroundColor Green
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
Write-Host "  （launcher 已在后台拉起；服务就绪后用 http://127.0.0.1:8000 访问）"
