# =============================================================================
# SpiderXHS 安装包冒烟测试（设计 14 §8 发版门禁）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File packaging\smoke-test.ps1 `
#       -SetupExe spider-xhs-setup-1.0.0.exe [-InstallDir <目录>]
#
# 覆盖：静默安装 → 文件齐全 → 起服务(指定端口，避开开发机 8000) → health /
# index.html / openapi → 数据目录落安装目录 → 端口回退 → bundled node 本地
# 签名栈 → 卸载后程序文件清除、数据保留。
# =============================================================================
param(
    [string]$SetupExe = "",
    [string]$InstallDir = "",
    [int]$Port = 8123
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir

if (-not $SetupExe) { $SetupExe = Join-Path $Root "spider-xhs-setup-1.0.0.exe" }
if (-not (Test-Path $SetupExe)) { Write-Host "ERROR: 安装器不存在 $SetupExe" -ForegroundColor Red; exit 1 }
if (-not $InstallDir) { $InstallDir = Join-Path $Root ".workbuddy\tmp\smoke-install" }

function Write-Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Fail([string]$m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

$AppExe = Join-Path $InstallDir "SpiderXHS.exe"
$Base = "http://127.0.0.1:$Port"

# --- 0. 清理上一次残留 -------------------------------------------------------
Write-Step "清理旧冒烟残留"
Get-Process -Name "SpiderXHS" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force }

# --- 1. 静默安装 -------------------------------------------------------------
Write-Step "静默安装到 $InstallDir"
$installLog = Join-Path $Root ".workbuddy\tmp\smoke-install.log"
$proc = Start-Process -FilePath $SetupExe -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
    "/DIR=$InstallDir", "/LOG=$installLog"
) -Wait -PassThru
if ($proc.ExitCode -ne 0) { Fail "安装器退出码 $($proc.ExitCode)，见 $installLog" }

# --- 2. 文件齐全性 -----------------------------------------------------------
Write-Step "校验安装产物"
foreach ($rel in @(
    "SpiderXHS.exe", "_internal", "runtime\node\node.exe",
    "frontend\dist\index.html", "config\default.yaml",
    # 应用初始化数据（设计 15 §4.5）：缺了 = 装完任务中心是空的
    "config\initial_data\scheduled_jobs.json"
)) {
    $p = Join-Path $InstallDir $rel
    if (-not (Test-Path $p)) { Fail "缺少 $rel" }
}
Write-Host "  文件齐全（exe / _internal / runtime/node / frontend/dist / config / initial_data）"

# 测试代码不得进包（2026-09-24 用户口径）
foreach ($rel in @("_internal\tests", "_internal\pytest", "_internal\_pytest")) {
    $p = Join-Path $InstallDir $rel
    if (Test-Path $p) { Fail "测试产物混进安装目录：$rel" }
}
Write-Host "  无测试产物（tests / pytest / _pytest 均不存在）"

# 初始化数据可解析（不是空文件/坏 JSON）
$initialJson = Get-Content (Join-Path $InstallDir "config\initial_data\scheduled_jobs.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $initialJson.scheduled_jobs -or $initialJson.scheduled_jobs.Count -lt 1) {
    Fail "应用初始化数据里没有定时任务条目"
}
$initialCount = $initialJson.scheduled_jobs.Count
Write-Host "  应用初始化数据: $initialCount 条定时任务"

# --- 3. 启动 + 健康检查 -------------------------------------------------------
Write-Step "启动服务（端口 $Port，--no-browser）"
$app = Start-Process -FilePath $AppExe -ArgumentList @("--port", "$Port", "--no-browser") -PassThru
$ok = $false
foreach ($i in 1..60) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -Uri "$Base/api/health" -TimeoutSec 3 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}
if (-not $ok) {
    $log = Join-Path $InstallDir "logs\launcher.log"
    if (Test-Path $log) { Get-Content $log -Tail 30 }
    Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
    Fail "服务 60s 内未就绪"
}
Write-Host "  /api/health 200"

$index = Invoke-WebRequest -Uri "$Base/" -TimeoutSec 5 -UseBasicParsing
if ($index.Content -notmatch 'id="root"') { Fail "/ 未返回前端 index.html" }
Write-Host "  / 返回 index.html"

$openapi = Invoke-WebRequest -Uri "$Base/openapi.json" -TimeoutSec 10 -UseBasicParsing | ConvertFrom-Json
$paths = @($openapi.paths.PSObject.Properties.Name)
Write-Host "  /openapi.json paths: $($paths.Count)"

# 3b. 日志完整性（治 alembic fileConfig 黑洞 + 验证托盘真的起来了）。
#     服务就绪/托盘就绪两行在 health 通过后毫秒级写入，轮询等最多 10s。
#     ⚠️ 两个编码/共享坑：① 必须 `-Encoding UTF8` —— launcher 日志是 Python
#     写的 UTF-8 无 BOM，PS5.1 默认按 GBK 解码会把中文搅成乱码 ⇒ 匹配假失败；
#     ② 必须用 Get-Content 而非 File.ReadAllText —— 后者默认 FileShare.Read，
#     打不开正被 launcher 追加的日志（IOException 另一进程正在使用）。
$logFile = Join-Path $InstallDir "logs\launcher.log"
$logOk = $false
foreach ($i in 1..10) {
    Start-Sleep -Seconds 1
    if (Test-Path $logFile) {
        $logText = Get-Content $logFile -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
        if ($logText -and $logText -match "服务就绪" -and $logText -match "托盘图标就绪" -and $logText -match "GET /api/health") {
            $logOk = $true
            break
        }
    }
}
if (-not $logOk) {
    if (Test-Path $logFile) { Get-Content $logFile -Tail 15 }
    Fail "launcher.log 关键行缺失（服务就绪/托盘图标就绪/访问日志）——alembic fileConfig 日志黑洞回归或托盘未起"
}
Write-Host "  launcher.log 关键行齐全（服务就绪 / 托盘图标就绪 / 访问日志）"

# --- 4. 数据目录落安装目录 ----------------------------------------------------
Write-Step "校验数据目录落在安装目录"
foreach ($rel in @(
    "data\spider_xhs.db", "data\secret.key",
    "backend\app\storage\media", "backend\app\storage\exports",
    "backend\app\storage\tmp_note_publish", "logs\launcher.log"
)) {
    $p = Join-Path $InstallDir $rel
    if (-not (Test-Path $p)) { Fail "缺少 $rel" }
}
Write-Host "  data / storage / logs 均在安装目录下"

# --- 4b. 应用初始化数据已**落成任务行**（设计 15 §4.5） ------------------------
# 判据必须落在「行」上而不是「文件」上：文件在而没播种 = 用户打开任务中心还是空的。
Write-Step "校验定时任务已随安装铺设（首启播种）"
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) {
    Write-Host "  [跳过] 本机无 python，无法查库行数（文件侧已在第 2 段校验）" -ForegroundColor Yellow
} else {
    $dbPath = (Join-Path $InstallDir "data\spider_xhs.db")
    $code = "import sqlite3;c=sqlite3.connect(r'$dbPath');print(len(c.execute('select id from scheduled_jobs where is_builtin=1').fetchall()))"
    $seeded = (& $py -c $code | Select-Object -Last 1)
    if ($seeded -is [array]) { $seeded = $seeded[-1] }
    $seeded = "$seeded".Trim()
    if ($seeded -ne "$initialCount") {
        Fail "定时任务未随安装铺设：库里 $seeded 行，初始化数据 $initialCount 条（首启播种失效）"
    }
    Write-Host "  已铺设 $seeded 个内置任务行（= 初始化数据条数）"
}

# --- 5. 端口回退 --------------------------------------------------------------
Write-Step "端口回退测试（占住 $Port → 应改起 $($Port+1)）"
Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
$listener.Start()
$app2 = Start-Process -FilePath $AppExe -ArgumentList @("--port", "$Port", "--no-browser") -PassThru
$ok2 = $false
foreach ($i in 1..40) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$($Port+1)/api/health" -TimeoutSec 3 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ok2 = $true; break }
    } catch { }
}
$listener.Stop()
if (-not $ok2) {
    Stop-Process -Id $app2.Id -Force -ErrorAction SilentlyContinue
    Fail "端口回退失败（$Port 被占时应起 $($Port+1)）"
}
Write-Host "  回退到 $($Port+1) 成功"
Stop-Process -Id $app2.Id -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

# --- 6. bundled node 本地签名栈 ----------------------------------------------
Write-Step "bundled node 本地签名栈自检"
$nodeExe = Join-Path $InstallDir "runtime\node\node.exe"
$signJs = Join-Path $InstallDir "_internal\xhs_utils\xhs_core\js\sign.js"
$sigJs = Join-Path $InstallDir "_internal\xhs_utils\xhs_creator\js\xhs_creator_signature.js"
if (-not (Test-Path $signJs)) { Fail "签名 JS 未打包：$signJs" }

# 6a. 文件式解析：require 绝对路径的 JS，其内部 require('crypto-js') 从 JS
#     所在目录逐级向上（_internal/xhs_utils/... → 安装根 node_modules）
$sigJsFwd = $sigJs.Replace('\', '/')
$probe1 = & $nodeExe -e "require('$sigJsFwd'); console.log('file-resolve OK')" 2>&1
if ($LASTEXITCODE -ne 0) { Fail "文件式 require crypto-js 失败：$probe1" }
Write-Host "  文件式解析（JS 目录向上 → 安装根 node_modules）OK"

# 6b. 管道式解析：PyExecJS 把源码走 stdin 喂给 node，模块解析基准是 cwd（= 安装根）
Push-Location $InstallDir
try {
    $probe2 = "require('crypto-js'); console.log('pipe-resolve OK')" | & $nodeExe 2>&1
    if ($LASTEXITCODE -ne 0) { Fail "管道式 require crypto-js 失败：$probe2" }
} finally { Pop-Location }
Write-Host "  管道式解析（cwd = 安装根）OK"

# 6c. 真签名计算：sign.js CLI + 完整载荷（纯本地计算，零网络）。
#     必填入参见 sign.js::signFull：cookie(a1) / loadts / seq / envConst /
#     envFpTail(14 元素数组)。
$inputJson = Join-Path $Root ".workbuddy\tmp\smoke-sign-input.json"
$payload = '{"api":"/api/sns/web/v1/user/posted","data":{"cursor":"","num":30},"cookie":"a1=68f8a5e0f1c34d9e8b2a7c6d5e4f3a2b; webId=smoke-test; loadts=1709905283","loadts":1709905283,"seq":1,"envConst":"smoke","envFpTail":[1,2,3,4,5,6,7,8,9,10,11,12,13,14],"tier":"0301"}'
# ⚠️ 必须写【无 BOM】UTF-8：PS5.1 的 Set-Content -Encoding utf8 会带 BOM，
# node 的 JSON.parse 会被 BOM 卡死（SyntaxError: Unexpected token）。
[System.IO.File]::WriteAllText($inputJson, $payload, (New-Object System.Text.UTF8Encoding $false))
$nodeOut = & $nodeExe $signJs "@$inputJson" 2>&1
if ($LASTEXITCODE -ne 0) { Fail "sign.js 签名计算失败：$nodeOut" }
$nodeText = ($nodeOut | Out-String).Trim()
if ($nodeText -notmatch '"x3"') { Fail "签名输出缺少 x3 字段：$nodeText" }
Write-Host "  sign.js 签名输出: $($nodeText.Substring(0, [Math]::Min(80, $nodeText.Length)))..."

# --- 7. 卸载（语义：只删程序文件，数据永远保留） ------------------------------
Write-Step "卸载（静默；无弹窗，数据按语义保留）"
$unins = Join-Path $InstallDir "unins000.exe"
if (-not (Test-Path $unins)) { Fail "找不到 $unins" }
$uproc = Start-Process -FilePath $unins -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") -Wait -PassThru
if ($uproc.ExitCode -ne 0) { Fail "卸载退出码 $($uproc.ExitCode)" }
if (Test-Path $AppExe) { Fail "卸载后 SpiderXHS.exe 仍在" }
if (-not (Test-Path (Join-Path $InstallDir "data\spider_xhs.db"))) { Fail "卸载后数据目录被删（应保留）" }
Write-Host "  程序文件已清除，data/ 按预期保留"

Write-Host ""
Write-Host "冒烟测试全部通过" -ForegroundColor Green
