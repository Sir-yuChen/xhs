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
if (-not $InstallDir) { $InstallDir = Join-Path $Root ".hermes\tmp\smoke-install" }

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
$installLog = Join-Path $Root ".hermes\tmp\smoke-install.log"
$proc = Start-Process -FilePath $SetupExe -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
    "/DIR=$InstallDir", "/LOG=$installLog"
) -Wait -PassThru
if ($proc.ExitCode -ne 0) { Fail "安装器退出码 $($proc.ExitCode)，见 $installLog" }

# --- 2. 文件齐全性 -----------------------------------------------------------
Write-Step "校验安装产物"
foreach ($rel in @(
    "SpiderXHS.exe", "_internal", "node_modules\crypto-js",
    "frontend\dist\index.html", "config\default.yaml",
    # 应用初始化数据（设计 15 §4.5 / 设计 10 §7.3）：缺了 = 装完任务中心 / 设置页是空的
    "config\initial_data\scheduled_jobs.json",
    "config\initial_data\system_settings.json",
    "config\initial_data\llm_gateway_providers.json"
)) {
    $p = Join-Path $InstallDir $rel
    if (-not (Test-Path $p)) { Fail "缺少 $rel" }
}
Write-Host "  文件齐全（exe / _internal / node_modules/crypto-js / frontend/dist / config / initial_data）"

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

# 运行时配置初始化数据同样必须可解析（设计 10 §7.3）：缺了 = 装完设置页「系统参数」卡是空的
$settingsJson = Get-Content (Join-Path $InstallDir "config\initial_data\system_settings.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $settingsJson.system_settings -or $settingsJson.system_settings.Count -lt 1) {
    Fail "应用初始化数据里没有运行时配置条目"
}
$settingsCount = $settingsJson.system_settings.Count
Write-Host "  应用初始化数据: $settingsCount 条运行时配置"

# 服务商种子初始化数据同样必须可解析（2026-10-13）：缺了 = 装完服务商列表是空的
$providersJson = Get-Content (Join-Path $InstallDir "config\initial_data\llm_gateway_providers.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $providersJson.providers -or $providersJson.providers.Count -lt 1) {
    Fail "应用初始化数据里没有服务商种子条目"
}
$providersCount = $providersJson.providers.Count
Write-Host "  应用初始化数据: $providersCount 个服务商种子"

# 版本历史必须**完整随包**（2026-10-15 用户报障补的判据 —— 任务卡 t_ad5b82f0）：
# 装机后「检查版本更新」弹窗里只有 1 条、且简介为空。当时包内 `config/version.json`
# 的 `versions` 只剩 1 条，而**构建全绿、本冒烟也全过** —— 因为这里只校验了
# 「文件存在 / 可解析」。⇒ 判据必须咬住**语义**：条数 == 源码条数，
# 且 current 那条的 `built_at` / `summary` 都非空（弹窗折叠标题就用这两个）。
# ⚠️ 与源码对比而非写死条数：写死「恰好 N 条」一发版就假红（同
#    `test_shipped_version_json_has_the_baseline_releases` 的教训）。
#: ⚠️⚠️ **不用 PS 的 `ConvertFrom-Json`** —— 它在 PS 5.1 上对这份数据**解析失败/解成空**
#: （2026-10-15 实测：`-Raw` 报「无效的对象...位置 685」；管道喂单行长 JSON 解成 1 条空对象）。
#: 用它写守卫 ⇒ **守卫恒假**（永远是空对象、比较永远"相等"）——比没有守卫更危险。
#: ⇒ 交 Python（打包/冒烟链路本就有 Python 依赖）。
#: ⚠️ 必须自己解析解释器：冒烟是**独立脚本**（release.ps1 阶段 4 调），不继承构建脚本的变量。
#:   2026-10-15 实测踩到：这里曾写成 `& $Py`（构建脚本 `build.ps1` 的变量名），
#:   而那时 smoke-test.ps1 里**没有 `$Py`** ⇒ 在 `$ErrorActionPreference = "Stop"` 下
#:   直接抛 `RuntimeException`（"& 运算符后面的表达式不是有效对象"），
#:   冒烟在**校验安装产物之前**就死掉 —— 守卫比没有更坏（把整条门禁带崩）。
#: 找一个可用的 Python（与 release.ps1 的 Resolve-Python 同候选集）。
$Py = $null
if ($env:SPIDER_XHS_PYTHON -and (Test-Path $env:SPIDER_XHS_PYTHON)) { $Py = $env:SPIDER_XHS_PYTHON }
if (-not $Py) {
    foreach ($c in @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
    )) { if (Test-Path $c) { $Py = $c; break } }
}
if (-not $Py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $Py = $cmd.Source }
}
if (-not $Py) { Fail "找不到 Python（版本历史审计需要；可用 SPIDER_XHS_PYTHON 指定）" }

#: ⚠️⚠️ **审计脚本必须落到临时文件再执行，不能用 `-c` 传多行中文代码**。
#:   2026-10-15 实测：PS 5.1 把**含非 ASCII 的 `-c` 参数**按控制台代码页编码后交给
#:   native python.exe ⇒ 中文字符串字面量被破坏（`SyntaxError: '(' was never closed`），
#:   守卫**恒红**。写文件走 UTF-8（无 BOM），命令行上只有 ASCII 路径，绕开整类坑。
#:   （同理必须传 `-c`/文件而不是裸变量：裸 `& $Py $code` 会把多行代码当**文件名**。）
$pyAuditDir = Join-Path $Root ".hermes\tmp"
New-Item -ItemType Directory -Force -Path $pyAuditDir | Out-Null
$pyAuditFile = Join-Path $pyAuditDir "smoke_version_audit.py"
$pyVersionAudit = @'
import json, sys
shipped_path, source_path = sys.argv[1:3]

def load(p):
    return json.load(open(p, encoding="utf-8-sig"))

shipped, source = load(shipped_path), load(source_path)
sv = shipped.get("versions") or []
pv = source.get("versions") or []
problems = []
if len(sv) != len(pv):
    problems.append("包内版本历史只剩 %d 条（源码 %d 条）—— 更新弹窗会看不到历史版本" % (len(sv), len(pv)))
if not sv:
    problems.append("包内版本历史为空")
head = next((h for h in sv if h.get("version") == shipped.get("current")), None)
if head is None:
    problems.append("包内版本历史里找不到 current=%s 那条" % shipped.get("current"))
else:
    for field in ("built_at", "summary"):
        if not str(head.get(field) or "").strip():
            problems.append("包内 %s 的 %s 为空 —— 更新弹窗里该条没有更新时间/简介" % (shipped.get("current"), field))
for line in problems:
    print("  [VERSION] " + line, file=sys.stderr)
print(len(sv))
sys.exit(1 if problems else 0)
'@
#: ⚠️ 必须【无 BOM】UTF-8：PS 5.1 的 Set-Content -Encoding utf8 带 BOM，
#: python 读源码文件遇 BOM 会 `SyntaxError: invalid non-printable character`。
[System.IO.File]::WriteAllText($pyAuditFile, $pyVersionAudit, (New-Object System.Text.UTF8Encoding $false))
$shippedCount = & $Py $pyAuditFile (Join-Path $InstallDir "config\version.json") (Join-Path $Root "config\version.json")
if ($LASTEXITCODE -ne 0) {
    Fail "包内 config/version.json 历史不完整（见上方 [VERSION] 行）"
}
$shippedCount = "$shippedCount".Trim()
Write-Host "  版本历史完整: $shippedCount 条（current 那条的 built_at/summary 齐）"

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
#
# ⚠️ 判据里那条访问日志**不能**用就绪轮询打出的 `GET /api/health`：
#     alembic 的 `fileConfig` 会在启动迁移期间**禁用全部 logger**（`launcher.log`
#     黑洞），`launcher.ensure_file_logging()` 要等**服务就绪之后**才把它们挂回来
#     ⇒ 就绪轮询的成功请求可能正好落在「恢复之前」而**不落日志**（2026-09-27 实测
#     踩到，同一份代码时红时绿 = 竞态）。这里**显式再打一次 health**：那时日志一定
#     已恢复，判据才是确定性的（安全网不许条件性）。
$null = Invoke-WebRequest -Uri "$Base/api/health" -TimeoutSec 5 -UseBasicParsing
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

# --- 4b. 初始化数据随「注册」铺设（DEF-066：按用户各持一份；定时任务 + 运行时配置） -------
# 判据必须落在「行」上而不是「文件」上：文件在而没播种 = 用户打开任务中心 / 设置页还是空的。
# 2026-09-26 v1.0.1 改口径：DEF-066 之前内置任务是全局共享行，「首启就能查到行」是当时的正确判据；
#   改成按用户各持一份后，全新安装没有任何用户 -> 首启 0 行才是正确行为，旧断言会次次误报。
#   真正的判据：注册一个用户 -> 立刻给这个用户铺满 N 条。
Write-Step "校验初始化数据随注册铺设（定时任务 + 运行时配置，按用户各持一份）"
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) {
    Write-Host "  [跳过] 本机无 python，无法查库行数（文件侧已在第 2 段校验）" -ForegroundColor Yellow
} else {
    $dbPath = (Join-Path $InstallDir "data\spider_xhs.db")
    $countSql = "import sqlite3;c=sqlite3.connect(r'$dbPath');print(len(c.execute('select id from scheduled_jobs where is_builtin=1').fetchall()))"
    $before = (& $py -c $countSql | Select-Object -Last 1)
    $before = "$before".Trim()
    if ($before -ne "0") { Fail "全新安装未注册时库里应 0 行（内置任务按用户各持一份），实测 $before 行" }
    Write-Host "  未注册时 0 行（符合按用户隔离口径）"
    $smokeUser = "smoke" + (Get-Date -Format "HHmmss")
    $body = (@{ username = $smokeUser; password = "Smoke@20260101" } | ConvertTo-Json)
    try {
        $null = Invoke-WebRequest -Uri "$Base/api/auth/register" -Method POST -ContentType "application/json" -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 15 -UseBasicParsing
    } catch {
        Fail "注册冒烟用户失败（播种链路失效）：$($_.Exception.Message)"
    }
    $seeded = (& $py -c $countSql | Select-Object -Last 1)
    if ($seeded -is [array]) { $seeded = $seeded[-1] }
    $seeded = "$seeded".Trim()
    if ($seeded -ne "$initialCount") {
        Fail "定时任务播种失效：注册后库里 $seeded 行，初始化数据 $initialCount 条"
    }
    Write-Host "  注册后铺设 $seeded 个内置任务行（= 初始化数据条数）"

    # 运行时配置行同样必须随注册铺设（设计 10 §7.3）—— 判据落在「行」上而不是「文件」上：
    # 文件在而没播种 = 用户打开设置页「系统参数」卡还是空的。
    $settingsSql = "import sqlite3;c=sqlite3.connect(r'$dbPath');print(len(c.execute('select id from system_settings').fetchall()))"
    $settingsSeeded = (& $py -c $settingsSql | Select-Object -Last 1)
    if ($settingsSeeded -is [array]) { $settingsSeeded = $settingsSeeded[-1] }
    $settingsSeeded = "$settingsSeeded".Trim()
    if ($settingsSeeded -ne "$settingsCount") {
        Fail "运行时配置播种失效：注册后库里 $settingsSeeded 行，初始化数据 $settingsCount 条"
    }
    Write-Host "  注册后铺设 $settingsSeeded 行运行时配置（= 初始化数据条数）"
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
# node.exe 已外置（首次启动下载到 deps/node/）=> 用系统 node 验证解析路径；
# 装包版运行时用用户下载的那份，模块解析规则与系统 node 同构。
$nodeExe = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $nodeExe) { Fail "构建机没有 node，无法验证 crypto-js 解析" }
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
$inputJson = Join-Path $Root ".hermes\tmp\smoke-sign-input.json"
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
