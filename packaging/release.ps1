#Requires -Version 5.1
<#
.SYNOPSIS
    XHS_ALL_IN_ONE 正式版发布工作流（打包 -> 校验 -> 冒烟 -> 打 tag -> Gitee Release）。

.DESCRIPTION
    「发版」是打包的下游动作。本脚本把设计 14 §8 的发版门禁串成一条可重复执行的链路：

      [1] 预检     工作区干净 / tag 不冲突 / 工具链就绪
      [2] 打包     build.ps1 -Version <x.y.z>
      [3] 校验     产物存在 + 体积合理 + 包内前端产物 = 源码 dist + 构建自检三段全绿
      [4] 冒烟     smoke-test.ps1（安装 -> health -> openapi paths -> 端口回退 -> 卸载）
      [5] 清残留   删 smoke-install 目录（否则下次手工装包会跳过「选择目标位置」页）
      [6] 打 tag   附注 tag + push
      [7] 发布     Gitee Release + 上传 exe 附件（同名附件用 --replace 覆盖）

    硬约束：**工作区必须干净** —— 否则「包」不对应任何确定提交，tag 与发布物脱钩。
    发布物一律走 Release 附件，**不进 git 仓库**（`.gitignore` 已排除 spider-xhs-setup-*.exe）。

.PARAMETER Version
    版本号 x.y.z，决定产物文件名 spider-xhs-setup-<Version>.exe 与 exe 版本信息。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File packaging\release.ps1 -Version 1.0.2

.EXAMPLE
    # 只打包 + 冒烟，不发版
    powershell -NoProfile -ExecutionPolicy Bypass -File packaging\release.ps1 -Version 1.0.2 -SkipTag -SkipRelease
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$TagName = "",
    [string]$ReleaseName = "",
    [string]$NotesFile = "",
    [string]$Owner = "Sir-yuChen",
    [string]$Repo = "xhs",
    [string]$Python = "",
    [switch]$SkipFrontend,
    [switch]$SkipSmoke,
    [switch]$SkipTag,
    [switch]$SkipRelease,
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$TmpDir = Join-Path $Root ".hermes\tmp"
if (!(Test-Path $TmpDir)) { New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null }

if ([string]::IsNullOrEmpty($TagName)) { $TagName = "v$Version" }
if ([string]::IsNullOrEmpty($ReleaseName)) { $ReleaseName = "$TagName · 正式版" }
$ExeName = "spider-xhs-setup-$Version.exe"
$ExePath = Join-Path $Root $ExeName

$script:StageNo = 0
function Write-Stage([string]$text) {
    $script:StageNo++
    Write-Host ""
    Write-Host ("=" * 72)
    Write-Host ("[{0}/7] {1}" -f $script:StageNo, $text)
    Write-Host ("=" * 72)
}
function Ok([string]$msg) { Write-Host "  OK    $msg" -ForegroundColor Green }
function Info([string]$msg) { Write-Host "  ..    $msg" }
function Warn([string]$msg) { Write-Host "  WARN  $msg" -ForegroundColor Yellow }
function Fail([string]$msg) {
    Write-Host ""
    Write-Host "  FAIL  $msg" -ForegroundColor Red
    exit 1
}

function Resolve-Python {
    if ($Python -and (Test-Path $Python)) { return $Python }
    if ($env:SPIDER_XHS_PYTHON -and (Test-Path $env:SPIDER_XHS_PYTHON)) { return $env:SPIDER_XHS_PYTHON }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# ---------------------------------------------------------------------------
Write-Stage "预检：工作区 / tag / 工具链"
# ---------------------------------------------------------------------------
$dirty = @(& git -C $Root status --porcelain)
if ($dirty.Count -gt 0 -and -not $AllowDirty) {
    Write-Host ($dirty | Select-Object -First 10 | Out-String)
    Fail "工作区不干净（$($dirty.Count) 项未提交）。发版要求「包 = 某个确定提交」：请先提交，或显式加 -AllowDirty 承担脱钩风险。"
}
$head = (& git -C $Root rev-parse --short HEAD).Trim()
Ok "工作区干净，HEAD=$head"

if (-not $SkipTag) {
    $localTag = (& git -C $Root tag -l $TagName)
    if ($localTag) { Fail "本地已存在 tag $TagName（换版本号，或删掉旧 tag 后重来）" }
    $remoteTag = (& git -C $Root ls-remote --tags origin "refs/tags/$TagName" 2>$null)
    if ($remoteTag) { Fail "远程已存在 tag $TagName（版本号已被占用）" }
    Ok "tag $TagName 未被占用"
}

$Py = Resolve-Python
if (-not $Py) { Fail "找不到 Python（可用 SPIDER_XHS_PYTHON 环境变量或 -Python 指定）" }
Ok "Python: $Py"

$iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { Fail "未找到 Inno Setup（$iscc）—— 打包机专属依赖，见 packaging/README.md" }
Ok "Inno Setup: $iscc"

# ---------------------------------------------------------------------------
Write-Stage "打包：build.ps1 -Version $Version"
# ---------------------------------------------------------------------------
$stale = @()
foreach ($d in @("dist", "build")) {
    $p = Join-Path $PSScriptRoot $d
    if (Test-Path $p) { $stale += $p }
}
if ($stale.Count -gt 0) {
    try {
        foreach ($p in $stale) { Remove-Item $p -Recurse -Force -ErrorAction Stop }
        Ok "已清旧产物（packaging\dist / build）"
    } catch {
        Warn "旧产物清理被拦：$($_.Exception.Message)"
        Warn "若构建失败，请手工删除 packaging\dist 与 packaging\build 后重跑"
    }
}
if (Test-Path $ExePath) {
    Remove-Item $ExePath -Force -ErrorAction SilentlyContinue
    Ok "已删旧安装包 $ExeName"
}

$buildLog = Join-Path $TmpDir "release-build-$Version.log"
$buildArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "build.ps1"), "-Version", $Version)
if ($SkipFrontend) { $buildArgs += "-SkipFrontend" }
Info "日志：$buildLog"
& powershell.exe @buildArgs | Out-File -FilePath $buildLog -Encoding utf8
if ($LASTEXITCODE -ne 0) { Fail "构建失败（exit=$LASTEXITCODE），日志：$buildLog" }
Ok "构建完成（exit=0）"

# ---------------------------------------------------------------------------
Write-Stage "校验：产物 / 包内前端产物 / 构建自检"
# ---------------------------------------------------------------------------
if (!(Test-Path $ExePath)) { Fail "产物不存在：$ExePath" }
$exeSize = (Get-Item $ExePath).Length
if ($exeSize -lt 80MB) { Fail "产物体积异常偏小：$exeSize B（历史基线 86–92MB）" }
Ok ("安装包 {0}（{1:N0} B / {2:N1} MB）" -f $ExeName, $exeSize, ($exeSize / 1MB))

$hash = (Get-FileHash -Path $ExePath -Algorithm SHA256).Hash.ToLower()
Ok "sha256 = $hash"

$bundleDist = Join-Path $PSScriptRoot "dist\SpiderXHS\frontend\dist\assets"
$srcDist = Join-Path $Root "frontend\dist\assets"
if ((Test-Path $bundleDist) -and (Test-Path $srcDist)) {
    $bundleJs = @(Get-ChildItem $bundleDist -Filter "index-*.js" | Select-Object -ExpandProperty Name)
    $srcJs = @(Get-ChildItem $srcDist -Filter "index-*.js" | Select-Object -ExpandProperty Name)
    if ($bundleJs.Count -eq 0) { Fail "包内找不到前端产物（frontend\dist\assets\index-*.js）" }
    $diff = @(Compare-Object $bundleJs $srcJs)
    if ($diff.Count -gt 0) {
        Fail "包内前端产物与源码 dist 不一致（包：$($bundleJs -join ',') / 源码：$($srcJs -join ',')）—— 本轮前端改动可能没进包"
    }
    Ok "包内前端产物与源码 dist 一致（$($bundleJs -join ',')）"
} else {
    Warn "跳过前端产物一致性核对（缺少 $bundleDist 或 $srcDist）"
}

$versionInfo = Join-Path $PSScriptRoot "version_info.txt"
if (Test-Path $versionInfo) {
    $viText = Get-Content $versionInfo -Raw
    $expect = "filevers=(" + ($Version -replace "\.", ", ") + ", 0)"
    if ($viText -notmatch [regex]::Escape($expect)) {
        Warn "version_info.txt 未见 $expect —— 请人工确认版本信息"
    } else {
        Ok "version_info.txt = $expect"
    }
}

$tail = (Get-Content $buildLog -Raw -ErrorAction SilentlyContinue)
if ($tail -match "全部通过") { Ok "构建自检：全部通过" } else { Fail "构建自检未见「全部通过」（查 $buildLog）" }
if ($tail -match "\[LEAK\]") { Fail "构建自检报 [LEAK]（不该进包的内容混入了）" }
if ($tail -match "\[MISSING\]") { Fail "构建自检报 [MISSING]（该进包的内容缺失）" }

# ---------------------------------------------------------------------------
Write-Stage "冒烟：smoke-test.ps1（发版门禁）"
# ---------------------------------------------------------------------------
if ($SkipSmoke) {
    Warn "已按 -SkipSmoke 跳过冒烟 —— 本次**无安装/运行验证结论**"
} else {
    $smokeLog = Join-Path $TmpDir "release-smoke-$Version.log"
    $smokeArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "smoke-test.ps1"), "-SetupExe", $ExeName)
    Info "日志：$smokeLog"
    & powershell.exe @smokeArgs | Out-File -FilePath $smokeLog -Encoding utf8
    $smokeCode = $LASTEXITCODE
    $smokeText = (Get-Content $smokeLog -Raw -ErrorAction SilentlyContinue)
    if ($smokeCode -ne 0 -or $smokeText -notmatch "冒烟测试全部通过") {
        Warn "冒烟未通过（exit=$smokeCode），日志：$smokeLog"
        Warn "注意：中途失败会在卸载表留下同 AppId 条目，下次手工装包会跳过「选择目标位置」页（packaging/README.md）"
        Fail "发版门禁未通过，已停止后续步骤"
    }
    Ok "冒烟全部通过"
    if ($smokeText -match "/openapi\.json paths:\s*(\d+)") {
        Ok "包内 /openapi.json paths = $($Matches[1])（与 AGENTS.md §5 声明值核对）"
    }
}

# ---------------------------------------------------------------------------
Write-Stage "清残留：冒烟静默安装目录"
# ---------------------------------------------------------------------------
$smokeDir = Join-Path $TmpDir "smoke-install"
if (Test-Path $smokeDir) {
    try {
        Remove-Item $smokeDir -Recurse -Force -ErrorAction Stop
        Ok "已清 $smokeDir"
    } catch {
        Warn "清理被拦：$($_.Exception.Message) —— 手工删除 $smokeDir"
    }
} else {
    Ok "无残留（$smokeDir 不存在）"
}
$leftover = @(Get-Process -Name "SpiderXHS" -ErrorAction SilentlyContinue)
if ($leftover.Count -gt 0) {
    Warn "仍有 SpiderXHS 进程在跑（PID $($leftover.Id -join ',')）—— 提权进程可能杀不掉，不影响发布"
}

# ---------------------------------------------------------------------------
Write-Stage "打 tag：$TagName"
# ---------------------------------------------------------------------------
if ($SkipTag) {
    Warn "已按 -SkipTag 跳过打 tag"
} else {
    $tagMsgFile = Join-Path $TmpDir "tag-msg-$TagName.txt"
    $tagMsg = @"
$TagName · 正式版

对应提交 $head（$( (& git -C $Root rev-parse --abbrev-ref HEAD).Trim() )）。
安装包：$ExeName — $exeSize B（$([math]::Round($exeSize / 1MB, 1)) MB）
sha256：$hash
构建：PyInstaller bundle + Inno Setup 自包含安装器，构建自检全绿$(if (-not $SkipSmoke) { "，发版门禁冒烟全部通过" })
"@
    [System.IO.File]::WriteAllText($tagMsgFile, $tagMsg, (New-Object System.Text.UTF8Encoding($false)))
    & git -C $Root tag -a $TagName -F $tagMsgFile
    if ($LASTEXITCODE -ne 0) { Fail "打 tag 失败" }
    Ok "已创建附注 tag $TagName"
    & git -C $Root push origin $TagName
    if ($LASTEXITCODE -ne 0) { Fail "推送 tag 失败（凭据或网络）" }
    Ok "已推送 tag 到 origin"
}

# ---------------------------------------------------------------------------
Write-Stage "发布：Gitee Release + 上传附件"
# ---------------------------------------------------------------------------
if ($SkipRelease) {
    Warn "已按 -SkipRelease 跳过发布"
} else {
    if ([string]::IsNullOrEmpty($NotesFile)) {
        $NotesFile = Join-Path $TmpDir "release-notes-$TagName.md"
        $notes = @"
## $TagName 正式版

**安装包**：``$ExeName`` — $([math]::Round($exeSize / 1MB, 1)) MB（$exeSize B）
**sha256**：``$hash``
**对应提交**：``$head``

安装器自包含（内置 Python 运行时 + 前端构建产物 + node 签名运行时），目标机器无需安装任何依赖。

> 本包由 ``$head`` 的干净工作区构建$(if (-not $SkipSmoke) { "，并已通过发版门禁冒烟测试" })。
"@
        [System.IO.File]::WriteAllText($NotesFile, $notes, (New-Object System.Text.UTF8Encoding($false)))
    }
    $relScript = Join-Path $PSScriptRoot "gitee_release.py"
    $relLog = Join-Path $TmpDir "release-publish-$TagName.log"
    Info "日志：$relLog"
    & $Py $relScript publish --owner $Owner --repo $Repo --tag $TagName --exe $ExePath --name $ReleaseName --body-file $NotesFile --replace --repo-dir $Root | Out-File -FilePath $relLog -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
        Get-Content $relLog -Tail 20
        Fail "发布失败，日志：$relLog"
    }
    Get-Content $relLog | Where-Object { $_ -match "附件上传成功|已删除旧附件|已更新 release 说明|已创建 release|RELEASE_PAGE" } | ForEach-Object { Ok $_.Trim() }
}

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host ("=" * 72)
Write-Host "发布工作流完成：$TagName"
Write-Host ("=" * 72)
Write-Host ("  提交        : $head")
Write-Host ("  安装包      : $ExePath")
Write-Host ("  字节数      : $exeSize")
Write-Host ("  sha256      : $hash")
Write-Host ("  Release 页面: https://gitee.com/$Owner/$Repo/releases/tag/$TagName")
Write-Host ("  下载直链    : https://gitee.com/$Owner/$Repo/releases/download/$TagName/$ExeName")
if ($SkipSmoke) { Write-Host "  ⚠️ 本次跳过了冒烟，无安装/运行验证结论" -ForegroundColor Yellow }
Write-Host ""
Write-Host "  提醒：仓库为私有，Release 页与下载直链需登录 Gitee 才能访问（未登录恒 403）。"
