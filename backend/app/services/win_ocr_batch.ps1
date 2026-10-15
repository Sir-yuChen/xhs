# Windows.Media.Ocr BATCH OCR -> one JSON per image (WITH per-word bounding boxes).
#
# Usage: powershell -File win_ocr_batch.ps1 <list_file> <max_dim> <out_dir>
#   <list_file> : UTF-8 text, one absolute image path per line.
#   <max_dim>   : resize long edge before OCR (0 = no resize).
#   <out_dir>   : directory; each image writes <uuid>.json (uuid from the list's
#                 sidecar mapping file, see NOTE 4). Actually the caller passes a
#                 THIRD file: a TSV of "<uuid>\t<image_path>" so this script knows
#                 the exact output name per image.
#
# NOTE 1: pure ASCII (PowerShell 5.1 reads .ps1 as GBK; UTF-8 Chinese breaks it).
# NOTE 2: TryCreateFromUserProfileLanguages (not TryCreateFromLanguage).
# NOTE 3: WHY BATCH -- measured: each `powershell -File` cold start costs ~1.2s;
#   actual OCR is only ~0.3s. Spawning one process per image means ~80% of the
#   wall time is process startup. Running all images in ONE process (engine
#   assembled once) is ~5x faster end-to-end.
# NOTE 4: the caller writes a TSV "<uuid>\t<abs_image_path>" so this script
#   writes each result to "<out_dir>\<uuid>.json" -- avoids filename collisions
#   and keeps the Python side's mapping trivial.
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]

function Fail-To($outPath, $code, $msg) {
    $payload = @{ ok = $false; error = $code; message = $msg } | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText($outPath, $payload, (New-Object System.Text.UTF8Encoding($false)))
}

$mapFile = $args[0]
$maxDim = 0
if ($args.Count -ge 2) { [void][int]::TryParse($args[1], [ref]$maxDim) }
$outDir = $args[2]

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { Write-Output "NO_ENGINE"; exit 2 }

$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

$rows = Get-Content -LiteralPath $mapFile -Encoding UTF8
foreach ($row in $rows) {
    if ([string]::IsNullOrWhiteSpace($row)) { continue }
    $parts = $row -split "\t", 2
    $uid = $parts[0]
    $path = $parts[1]
    $outPath = Join-Path $outDir "$uid.json"
    try {
        if (-not (Test-Path -LiteralPath $path)) { Fail-To $outPath "NO_FILE" "file not found"; continue }
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $origW = $bitmap.PixelWidth
        $origH = $bitmap.PixelHeight
        $scale = 1.0
        if ($maxDim -gt 0 -and ($origW -gt $maxDim -or $origH -gt $maxDim)) {
            $scale = [Math]::Min($maxDim / $origW, $maxDim / $origH)
            # NOTE 5: SoftwareBitmap has NO ResizeAsync -- the WinRT way is
            #   BitmapTransform.ScaleRatio + decoder.GetSoftwareBitmapAsync(transform).
            #   Re-decoding with a transform is also the only path that keeps the
            #   decoder's own pixel format (mixing formats breaks OcrEngine).
            $transform = New-Object Windows.Graphics.Imaging.BitmapTransform
            $transform.ScaledWidth = [uint32][Math]::Max(1, [Math]::Round($origW * $scale))
            $transform.ScaledHeight = [uint32][Math]::Max(1, [Math]::Round($origH * $scale))
            $transform.InterpolationMode = [Windows.Graphics.Imaging.BitmapInterpolationMode]::Fant
            $resized = Await ($decoder.GetSoftwareBitmapAsync(
                [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
                [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied,
                $transform,
                [Windows.Graphics.Imaging.ExifOrientationMode]::RespectExifOrientation,
                [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage
            )) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $bitmap.Dispose()
            $bitmap = $resized
        }
        try {
            $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        } catch {
            $bitmap.Dispose()
            Fail-To $outPath "RECOGNIZE_FAILED" $_.Exception.Message
            $stream.Dispose()
            continue
        }
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
            $lines += @{ text = $line.Text; words = $words }
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
        $stream.Dispose()
    } catch {
        Fail-To $outPath "OCR_FAILED" $_.Exception.Message
    }
}
exit 0
