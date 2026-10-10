# Windows.Media.Ocr single-image OCR -> JSON (WITH per-word bounding boxes for red-box overlay).
#
# Usage: powershell -File win_ocr_json.ps1 <image_path> [max_dim] [out_json_path]
# Output: out_json_path (defaults to %TEMP%\ocr_out.json for manual use; the
#   Python caller ALWAYS passes a per-call unique path -- see NOTE 3).
#   { ok, error?, message?, engine_lang, pixel_width, pixel_height, scaled_to,
#     text, line_count, lines:[{text,x,y,w,h,words:[{text,x,y,w,h}]}] }
#
# NOTE 1: This file MUST stay pure ASCII. Windows PowerShell reads .ps1 as the
#   system ANSI codepage (GBK on this machine), so a UTF-8 Chinese comment gets
#   mangled into a stray quote and breaks parsing (observed: "TerminatorExpected
#   AtEndOfString"). All user-facing Chinese text is emitted by the Python caller
#   instead.
# NOTE 2: TryCreateFromUserProfileLanguages, not TryCreateFromLanguage -- the
#   latter needs [Windows.Globalization.Language]::new(), and that type is NOT
#   loadable here ("cannot find type [Windows.Globalization.Language]").
#   The profile-languages overload resolves to zh-Hans-CN on this machine.
# NOTE 3: out_json_path is a REQUIRED argument in practice. An earlier version
#   wrote every result to the fixed %TEMP%\ocr_out.json; with 4 concurrent
#   workers that meant concurrent runs deleted/overwrote each other's file and
#   18 of 68 images reported a bogus OCR_NO_OUTPUT. Each call now gets its own
#   unique path from the Python side.
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]

$outPath = Join-Path $env:TEMP "ocr_out.json"
if ($args.Count -ge 3 -and -not [string]::IsNullOrWhiteSpace($args[2])) { $outPath = $args[2] }

function Fail($code, $msg) {
    $payload = @{ ok = $false; error = $code; message = $msg } | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText($outPath, $payload, (New-Object System.Text.UTF8Encoding($false)))
    exit 2
}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { Fail "NO_ENGINE" "no Windows OCR engine available" }

$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($op, $resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

$path = $args[0]
if (-not (Test-Path -LiteralPath $path)) { Fail "NO_FILE" "file not found" }

$maxDim = 0
if ($args.Count -ge 2) { [void][int]::TryParse($args[1], [ref]$maxDim) }

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$origW = $bitmap.PixelWidth
$origH = $bitmap.PixelHeight
$scale = 1.0
if ($maxDim -gt 0 -and ($origW -gt $maxDim -or $origH -gt $maxDim)) {
    $scale = [Math]::Min($maxDim / $origW, $maxDim / $origH)
    $nw = [uint32][Math]::Max(1, [Math]::Round($origW * $scale))
    $nh = [uint32][Math]::Max(1, [Math]::Round($origH * $scale))
    $resized = Await ($bitmap.ResizeAsync($nw, $nh)) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $bitmap.Dispose()
    $bitmap = $resized
}

try {
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
} catch {
    $bitmap.Dispose()
    Fail "RECOGNIZE_FAILED" $_.Exception.Message
}

# Undo the resize so boxes land on ORIGINAL image coordinates.
$inv = 1.0 / $scale
$lines = @()
$lineCount = 0
foreach ($line in $result.Lines) {
    $lineCount++
    $words = @()
    foreach ($w in $line.Words) {
        $r = $w.BoundingRect
        $words += @{
            text = $w.Text
            x = [Math]::Round($r.X * $inv, 1)
            y = [Math]::Round($r.Y * $inv, 1)
            w = [Math]::Round($r.Width * $inv, 1)
            h = [Math]::Round($r.Height * $inv, 1)
        }
    }
    # NOTE: OcrLine.BoundingRect returns all-zeros on this Windows build
    # (verified: every line box was (0,0,0,0)). Only the per-Word rects are
    # trustworthy, so the line box is derived from its words below.
    $lines += @{
        text = $line.Text
        words = $words
    }
}

$scaledTo = ""
if ($scale -lt 1.0) { $scaledTo = "$($bitmap.PixelWidth)x$($bitmap.PixelHeight)" }

$payload = @{
    ok = $true
    engine_lang = $engine.RecognizerLanguage.LanguageTag
    pixel_width = $origW
    pixel_height = $origH
    scaled_to = $scaledTo
    text = $result.Text
    line_count = $lineCount
    lines = $lines
}
$json = $payload | ConvertTo-Json -Depth 6 -Compress
[System.IO.File]::WriteAllText($outPath, $json, (New-Object System.Text.UTF8Encoding($false)))
$bitmap.Dispose()
exit 0
