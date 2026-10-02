# Package the Fundravers FR artwork, plus a download badge, at app icon sizes.
# Also writes fripper-icon-1024.png, the master used for the macOS app icon.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$logo = [System.Drawing.Image]::FromFile((Join-Path $PSScriptRoot 'fundravers-logo.png'))
# Master: unchanged logo with a white download badge in the lower-right corner.
$source = [System.Drawing.Bitmap]::new(1024, 1024)
$g = [System.Drawing.Graphics]::FromImage($source)
try {
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.DrawImage($logo, [System.Drawing.Rectangle]::new(0, 0, 1024, 1024))
    $cx = 790; $cy = 790; $r = 200; $ring = 34
    $black = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::Black)
    $white = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::White)
    $g.FillEllipse($black, $cx - $r - $ring, $cy - $r - $ring, 2 * ($r + $ring), 2 * ($r + $ring))
    $g.FillEllipse($white, $cx - $r, $cy - $r, 2 * $r, 2 * $r)
    $g.FillRectangle($black, $cx - 30, $cy - 125, 60, 120)          # arrow shaft
    $g.FillPolygon($black, [System.Drawing.PointF[]]@(
        [System.Drawing.PointF]::new($cx - 100, $cy - 20), [System.Drawing.PointF]::new($cx + 100, $cy - 20),
        [System.Drawing.PointF]::new($cx, $cy + 85)))                   # arrow head
    $g.FillRectangle($black, $cx - 105, $cy + 108, 210, 34)          # tray
    $black.Dispose(); $white.Dispose()
} finally { $g.Dispose(); $logo.Dispose() }
$source.Save((Join-Path $PSScriptRoot 'fripper-icon-1024.png'), [System.Drawing.Imaging.ImageFormat]::Png)
$entries = [System.Collections.Generic.List[object]]::new()
function ConvertTo-IconDib($bitmap) {
    # Classic 32-bit BMP icon entry: header, bottom-up BGRA rows, empty AND mask.
    $size = $bitmap.Width
    $rect = [System.Drawing.Rectangle]::new(0, 0, $size, $size)
    $data = $bitmap.LockBits($rect, 'ReadOnly', [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $pixels = [byte[]]::new($data.Stride * $size)
    [System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $pixels, 0, $pixels.Length)
    $bitmap.UnlockBits($data)
    $out = [System.IO.MemoryStream]::new()
    $w = [System.IO.BinaryWriter]::new($out)
    $w.Write([uint32]40); $w.Write([int32]$size); $w.Write([int32]($size * 2)); $w.Write([uint16]1); $w.Write([uint16]32)
    foreach ($i in 1..6) { $w.Write([uint32]0) }
    for ($y = $size - 1; $y -ge 0; $y--) { $w.Write($pixels, $y * $data.Stride, $size * 4) }
    $w.Write([byte[]]::new([math]::Ceiling($size / 32) * 4 * $size))
    $w.Flush()
    return $out.ToArray()
}
try {
    foreach ($size in @(16, 20, 24, 32, 40, 48, 64, 128, 256)) {
        $bitmap = [System.Drawing.Bitmap]::new($size, $size)
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $stream = [System.IO.MemoryStream]::new()
        try {
            $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
            $graphics.DrawImage($source, [System.Drawing.Rectangle]::new(0, 0, $size, $size))
            $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
            $bytes = $stream.ToArray()
            if ($size -lt 256) { $entries.Add(@{ Size = $size; Bytes = (ConvertTo-IconDib $bitmap) }) }
            else { $entries.Add(@{ Size = $size; Bytes = $bytes }) }   # PNG only at 256, for every icon reader
            if ($size -eq 64) { [System.IO.File]::WriteAllBytes((Join-Path $PSScriptRoot 'fripper.png'), $bytes) }
        } finally {
            $graphics.Dispose()
            $bitmap.Dispose()
            $stream.Dispose()
        }
    }
    $file = [System.IO.File]::Create((Join-Path $PSScriptRoot 'fripper.ico'))
    $writer = [System.IO.BinaryWriter]::new($file)
    try {
        $writer.Write([uint16]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]$entries.Count)
        $offset = 6 + 16 * $entries.Count
        foreach ($entry in $entries) {
            $dimension = if ($entry.Size -eq 256) { 0 } else { $entry.Size }
            $writer.Write([byte]$dimension)
            $writer.Write([byte]$dimension)
            $writer.Write([byte]0)
            $writer.Write([byte]0)
            $writer.Write([uint16]1)
            $writer.Write([uint16]32)
            $writer.Write([uint32]$entry.Bytes.Length)
            $writer.Write([uint32]$offset)
            $offset += $entry.Bytes.Length
        }
        foreach ($entry in $entries) { $writer.Write([byte[]]$entry.Bytes) }
    } finally { $writer.Dispose() }
} finally { $source.Dispose() }
Write-Host 'Packaged FR artwork with download badge as fripper.png, fripper.ico and fripper-icon-1024.png.'
