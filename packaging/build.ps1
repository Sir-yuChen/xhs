# =============================================================================
# SpiderXHS 一键打包脚本（设计 14 §6）
#
# 用法（在任意目录）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build.ps1 -Version 1.0.1
#
# 开关：
#   -Version <x.y.z>   版本号（默认 1.0.0）；同时决定产物文件名
#   -SkipFrontend      跳过 npm ci + vite build（dist 已是最新时）
#   -SkipInstaller     只出 PyInstaller bundle，不调 ISCC
#   -Clean             先删 packaging/{build,dist,.venv-build} 与 node-runtime
#
# 产物：<项目根>\spider-xhs-setup-<版本>.exe
# 后续发新版只需改 -Version 重跑本脚本。
# =============================================================================
param(
    [string]$Version = "1.0.0",
    [switch]$SkipFrontend,
    [switch]$SkipInstaller,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$DistDir = Join-Path $ScriptDir "dist"
$BuildDir = Join-Path $ScriptDir "build"
$VenvDir = Join-Path $ScriptDir ".venv-build"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$NodeRuntimeDir = Join-Path $ScriptDir "node-runtime"
$Bundle = Join-Path $DistDir "SpiderXHS"

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

# --- 版本号规范化：VSVersionInfo / Inno 需要四段 ---------------------------
$VersionParts = $Version.Split(".")
if ($VersionParts.Count -ge 4) {
    $PackageVersion = $Version
} else {
    $PackageVersion = "$Version.0"
}

# --- 0. 可选清理 -----------------------------------------------------------
if ($Clean) {
    Write-Step "清理旧构建产物"
    foreach ($dir in @($DistDir, $BuildDir, $VenvDir, $NodeRuntimeDir)) {
        if (Test-Path $dir) {
            Remove-Item $dir -Recurse -Force
        }
    }
}

# --- 1. 前置检查 -----------------------------------------------------------
Write-Step "检查构建依赖"

$Python = $env:SPIDER_XHS_PYTHON
if (-not $Python) {
    $Known = "C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe"
    if (Test-Path $Known) {
        $Python = $Known
    } else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd) { $Python = $cmd.Source }
    }
}
if (-not $Python -or -not (Test-Path $Python)) {
    Fail "未找到 Python 3.12（可用环境变量 SPIDER_XHS_PYTHON 指定）。"
}
$pyVersion = & $Python --version 2>&1
Write-Host "  python: $Python ($pyVersion)"

$NodeCmd = Get-Command node -ErrorAction SilentlyContinue
if (-not $NodeCmd) {
    Fail "未找到 node（签名链路与 node-runtime 打包都需要）。"
}
$NpmCmd = Get-Command npm -ErrorAction SilentlyContinue
if (-not $NpmCmd) {
    Fail "未找到 npm。"
}
Write-Host "  node:   $($NodeCmd.Source) $(& node --version)"

$ISCC = $null
foreach ($candidate in @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)) {
    if (Test-Path $candidate) { $ISCC = $candidate }
}
if (-not $ISCC) {
    $isccCmd = Get-Command ISCC -ErrorAction SilentlyContinue
    if ($isccCmd) { $ISCC = $isccCmd.Source }
}
if (-not $ISCC -and -not $SkipInstaller) {
    Fail "未找到 Inno Setup（ISCC.exe）。安装：winget install -e --id JRSoftware.InnoSetup；或加 -SkipInstaller 只出 bundle。"
}

# --- 2. 构建 venv（从系统 Python3.12 派生，隔离，不污染用户环境） ------------
Write-Step "准备构建 venv（$VenvDir）"
if (-not (Test-Path $VenvPython)) {
    & $Python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Fail "venv 创建失败" }
}
# 注意：这里**不**升级 venv 自带的 pip。`pip install --upgrade pip` 会走
# 「先卸载旧 pip」路径，在带删除拦截的受控环境里会直接失败；venv 自带 pip
# 已足够新，升级对打包没有收益。
& $VenvPython -m pip install --quiet --disable-pip-version-check -r (Join-Path $Root "requirements.txt") pyinstaller
if ($LASTEXITCODE -ne 0) { Fail "依赖安装失败" }
$pyInstallerVersion = & $VenvPython -m PyInstaller --version
Write-Host "  PyInstaller $pyInstallerVersion"

# --- 3. 前端构建 -----------------------------------------------------------
if (-not $SkipFrontend) {
    Write-Step "构建前端（npm install + vite build）"
    Push-Location (Join-Path $Root "frontend")
    try {
        # 用 npm install 而不是 npm ci：ci 会整体删除 node_modules，而开发机的
        # Vite dev server 可能正锁着 esbuild.exe（EPERM 删不动 ⇒ 构建失败）。
        # npm install 同样遵循 package-lock.json，只补差异。
        & npm install --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { Fail "npm install 失败" }
        & npm run build
        if ($LASTEXITCODE -ne 0) { Fail "vite build 失败" }
    } finally {
        Pop-Location
    }
} else {
    Write-Step "跳过前端构建（-SkipFrontend）"
}

# --- 4. 版本信息 + 图标 ----------------------------------------------------
Write-Step "生成版本信息与图标"
$VersionInfoFile = Join-Path $ScriptDir "version_info.txt"
$VersionTuple = ($PackageVersion.Split(".") | ForEach-Object { [int]$_ }) -join ", "
@"
# UTF-8
# 由 packaging/build.ps1 生成，请勿手改。
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($VersionTuple),
    prodvers=($VersionTuple),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
    ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'040904B0',
        [StringStruct(u'FileDescription', 'SpiderXHS desktop'),
        StringStruct(u'FileVersion', '$PackageVersion'),
        StringStruct(u'InternalName', 'SpiderXHS'),
        StringStruct(u'LegalCopyright', 'SpiderXHS'),
        StringStruct(u'OriginalFilename', 'SpiderXHS.exe'),
        StringStruct(u'ProductName', 'SpiderXHS'),
        StringStruct(u'ProductVersion', '$PackageVersion')])
      ]),
    VarFileInfo([VarStruct(u'Translation', [2052, 1200])])
  ]
)
"@ | Set-Content -Path $VersionInfoFile -Encoding utf8

& $VenvPython (Join-Path $ScriptDir "make_icon.py") (Join-Path $Root "frontend\public\logo.jpg") (Join-Path $ScriptDir "assets\app.ico")
if ($LASTEXITCODE -ne 0) { Fail "图标生成失败" }

# --- 5. node 运行时暂存（只打 crypto-js 一棵树） ---------------------------
Write-Step "准备 node 运行时"
$NodeExe = $NodeCmd.Source
if (-not (Test-Path (Join-Path $NodeRuntimeDir "node_modules\crypto-js"))) {
    New-Item -ItemType Directory -Force -Path $NodeRuntimeDir | Out-Null
    @'
{
  "name": "spider-xhs-node-runtime",
  "private": true,
  "dependencies": {
    "crypto-js": "^4.2.0"
  }
}
'@ | Set-Content -Path (Join-Path $NodeRuntimeDir "package.json") -Encoding utf8
    Push-Location $NodeRuntimeDir
    try {
        & npm install --omit=dev --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { Fail "node-runtime npm install 失败" }
    } finally {
        Pop-Location
    }
}
Write-Host "  node.exe: $NodeExe"

# --- 6. PyInstaller --------------------------------------------------------
Write-Step "PyInstaller 打包（onedir）"
& $VenvPython -m PyInstaller --noconfirm --clean `
    --distpath $DistDir --workpath $BuildDir `
    (Join-Path $ScriptDir "SpiderXHS.spec")
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller 失败" }
if (-not (Test-Path (Join-Path $Bundle "SpiderXHS.exe"))) {
    Fail "未找到产物 $Bundle\SpiderXHS.exe"
}

# --- 7. 组装 bundle --------------------------------------------------------
Write-Step "组装 bundle（frontend/dist + config + runtime/node）"
# ⚠️ 目标必须写到 frontend\dist 这一层：Copy-Item 在目标不存在时会把「源目录本身」
# 当成目标名拷贝（少一层 dist ⇒ 启动器的 FRONTEND_BUILD_DIR 指空）。
New-Item -ItemType Directory -Force -Path (Join-Path $Bundle "frontend") | Out-Null
Copy-Item (Join-Path $Root "frontend\dist") (Join-Path $Bundle "frontend\dist") -Recurse -Force
New-Item -ItemType Directory -Force -Path (Join-Path $Bundle "config") | Out-Null
Copy-Item (Join-Path $Root "config\default.yaml") (Join-Path $Bundle "config\default.yaml") -Force
# 应用初始化数据（设计 15 §4.5）：整目录随包 —— 装完任务中心即有那批定时任务。
# ⚠️ 目标是**已存在**的 config\ 目录 ⇒ 源目录被复制到其下，落点 config\initial_data\（正确）；
#    与上面 frontend\dist 那条同源坑（目标不存在时源目录会被当成目标名）。
Copy-Item (Join-Path $Root "config\initial_data") (Join-Path $Bundle "config") -Recurse -Force

# 7a. 裁掉明确不用的内容（用户口径：不必要内容不打包）：
#   cv2/data/*.xml = haarcascade 等模型文件（约 10MB）。本仓库 cv2 只用了
#   imdecode / imencode / VideoCapture（媒体校验），无任何级联分类器用法
#   （已全仓 grep 确认），删除不影响运行。
$cv2Data = Join-Path $Bundle "_internal\cv2\data"
if (Test-Path $cv2Data) {
    $cv2Size = (Get-ChildItem $cv2Data -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
    # ADDED 2026-09-26 v1.0.1: trim is a SIZE optimization, not a correctness gate.
    # In managed envs Remove-Item is intercepted/routed to the recycle bin; if the bin
    # refuses the write the whole build used to die. Not trimmed = +10MB, still works.
    try {
        Remove-Item $cv2Data -Recurse -Force -ErrorAction Stop
        Write-Host ("  trimmed cv2/data ({0:N1} MB, no cascade-classifier usage)" -f $cv2Size)
    } catch {
        Write-Host ("  [WARN] cv2/data trim failed ({0:N1} MB kept in bundle, no functional impact): {1}" -f $cv2Size, $_.Exception.Message) -ForegroundColor Yellow
    }
}

$BundleNode = Join-Path $Bundle "runtime\node"
New-Item -ItemType Directory -Force -Path $BundleNode | Out-Null
Copy-Item $NodeExe $BundleNode -Force
# node_modules 放【安装根】（与开发仓仓库根 node_modules 同构）——node 的
# require 解析有两条路，放这里两条都覆盖：
#   ① 文件式（subprocess 调 node <js>）：从 _internal/xhs_utils/.../js 逐级向上，
#      穿过 _internal 到达安装根；
#   ② PyExecJS 管道式（源码走 stdin，cwd = 安装根）：直接从安装根解析。
# 放 runtime/node/ 下两条路都够不着（开发机正常是因为仓库根有 node_modules）。
Copy-Item (Join-Path $NodeRuntimeDir "node_modules") (Join-Path $Bundle "node_modules") -Recurse -Force
& (Join-Path $BundleNode "node.exe") -e "require('crypto-js'); console.log('  node smoke: crypto-js OK, node ' + process.version)"
if ($LASTEXITCODE -ne 0) { Fail "bundled node / crypto-js 自检失败" }

$bundleSize = (Get-ChildItem $Bundle -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("  bundle 大小: {0:N1} MB" -f $bundleSize)

# --- 8. Inno Setup ---------------------------------------------------------
if ($SkipInstaller) {
    Write-Step "跳过安装器（-SkipInstaller），bundle 在 $Bundle"
    exit 0
}

Write-Step "ISCC 生成安装器"
& $ISCC "/DAppVersion=$Version" "/DAppPackageVersion=$PackageVersion" (Join-Path $ScriptDir "installer.iss")
if ($LASTEXITCODE -ne 0) { Fail "ISCC 失败" }

$SetupExe = Join-Path $Root "spider-xhs-setup-$Version.exe"
if (-not (Test-Path $SetupExe)) {
    Fail "未找到安装器 $SetupExe"
}

# --- 9. 汇总（含「不必要内容」审计） -----------------------------------------
$setupSize = (Get-Item $SetupExe).Length / 1MB
Write-Host ""
Write-Host "打包完成（$Version）" -ForegroundColor Green
Write-Host "  安装器: $SetupExe ($($setupSize.ToString('N1')) MB)"
Write-Host "  bundle: $Bundle ($($bundleSize.ToString('N1')) MB)"
Write-Host "  安装后数据目录: <安装目录>\data 与 <安装目录>\backend\app\storage"

Write-Step "bundle 体积 top 15（每次构建都过一眼，防不必要内容混入）"
Get-ChildItem (Join-Path $Bundle "_internal") -Directory | ForEach-Object {
    $size = (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum / 1MB
    [PSCustomObject]@{ Name = $_.Name; SizeMB = [math]::Round($size, 1) }
} | Sort-Object SizeMB -Descending | Select-Object -First 15 | Format-Table -AutoSize | Out-String | Write-Host

Write-Step "冗余内容自检（禁止项必须不存在；node_modules 只许 crypto-js）"
$forbidden = @(
    @{ Path = (Join-Path $Bundle "_internal\tests"); Label = "tests/" },
    # 测试代码不进安装包（2026-09-24 用户口径）：pytest 的 excludes 已挡，
    # 这里加**正向断言** —— 哪天依赖变更把它带进来，构建当场红，而不是发出去才发现。
    @{ Path = (Join-Path $Bundle "_internal\pytest"); Label = "pytest" },
    @{ Path = (Join-Path $Bundle "_internal\_pytest"); Label = "_pytest" },
    @{ Path = (Join-Path $Bundle "_internal\docs"); Label = "docs/" },
    # 2026-09-26 v1.0.1 新增：整仓库排查后确认「与运行时无关、且不该出现」的项。
    # 这些目前都没进包，加正向断言是为了防哪天改 spec / 组装逻辑时被带进来。
    @{ Path = (Join-Path $Bundle "_internal\static"); Label = "static/（v1 期界面截图，2026-09-26 已从仓库移除）" },
    @{ Path = (Join-Path $Bundle "_internal\author"); Label = "author/（Logo / 收款码等无关资产）" },
    @{ Path = (Join-Path $Bundle "_internal\.hermes"); Label = ".hermes/（本地规范/记忆/日志）" },
    @{ Path = (Join-Path $Bundle "_internal\.git"); Label = ".git/" },
    @{ Path = (Join-Path $Bundle "data"); Label = "data/（开发库与备份，安装后由运行期生成）" },
    @{ Path = (Join-Path $Bundle "docs"); Label = "docs/（工程文档）" },
    @{ Path = (Join-Path $Bundle "frontend\src"); Label = "frontend/src（前端源码，只打 dist 构建产物）" },

    @{ Path = (Join-Path $Bundle "_internal\data"); Label = "data/（开发库）" },
    @{ Path = (Join-Path $Bundle "_internal\frontend\node_modules"); Label = "frontend/node_modules" },
    @{ Path = (Join-Path $Bundle "_internal\node_modules"); Label = "_internal/node_modules（crypto-js 只许在安装根）" },
    @{ Path = (Join-Path $Bundle "runtime\node\node_modules"); Label = "runtime/node/node_modules（crypto-js 只许在安装根）" }
)
$leak = $false
foreach ($f in $forbidden) {
    if (Test-Path $f.Path) {
        Write-Host "  [LEAK] $($f.Label) 混进了 bundle：$($f.Path)" -ForegroundColor Red
        $leak = $true
    }
}
$nm = Join-Path $Bundle "node_modules"
if (-not (Test-Path $nm)) {
    Write-Host "  [LEAK] 安装根缺少 node_modules（crypto-js 放这里两条 require 路才走得通）" -ForegroundColor Red
    $leak = $true
} else {
    $extra = Get-ChildItem $nm -Directory | Where-Object { $_.Name -ne "crypto-js" -and $_.Name -notlike ".*" }
    if ($extra) {
        Write-Host "  [LEAK] node_modules 含预期外包：$($extra.Name -join ', ')" -ForegroundColor Red
        $leak = $true
    } else {
        Write-Host "  node_modules 仅含 crypto-js"
    }
}
# 正向断言：应用初始化数据必须随包（设计 15 §4.5 / 设计 10 §7.3 / 设计 14 §8-8）。
# 缺了 = 用户装完任务中心是空的、设置页没有那批配置行 —— 与上面「禁止项」相反方向的安全网：
# 只查「不该有的没有」会漏掉「该有的没有」。
# ⚠️ 每个数据文件都要**单独断言**（2026-09-27 补 system_settings）：只断言一个文件，
#    另一个悄悄漏掉时自检照样「全部通过」。
foreach ($rel in @(
    "config\initial_data\scheduled_jobs.json",
    "config\initial_data\system_settings.json"
)) {
    $initialData = Join-Path $Bundle $rel
    if (Test-Path $initialData) {
        Write-Host "  应用初始化数据: $rel"
    } else {
        Write-Host "  [MISSING] 应用初始化数据未随包：$initialData" -ForegroundColor Red
        $leak = $true
    }
}
if ($leak) { Fail "bundle 自检未通过（见上方 [LEAK] / [MISSING] 行）" }
Write-Host "  全部通过"
