# Draws pcs.ico (PNG frames 16..256): rounded dark tile with a cold-water drop. Run: powershell -File make_icon.ps1
Add-Type -AssemblyName System.Drawing
$sizes = 16, 24, 32, 48, 64, 128, 256
$frames = @()
foreach ($s in $sizes) {
    $bmp = New-Object System.Drawing.Bitmap $s, $s
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $g.Clear([System.Drawing.Color]::Transparent)
    $r = [single]($s * 0.22)
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $w = [single]($s - 1)
    $p.AddArc(0, 0, 2 * $r, 2 * $r, 180, 90); $p.AddArc($w - 2 * $r, 0, 2 * $r, 2 * $r, 270, 90)
    $p.AddArc($w - 2 * $r, $w - 2 * $r, 2 * $r, 2 * $r, 0, 90); $p.AddArc(0, $w - 2 * $r, 2 * $r, 2 * $r, 90, 90)
    $p.CloseFigure()
    $bg = New-Object System.Drawing.Drawing2D.LinearGradientBrush((New-Object System.Drawing.PointF 0, 0), (New-Object System.Drawing.PointF 0, $s),
        [System.Drawing.Color]::FromArgb(255, 28, 32, 40), [System.Drawing.Color]::FromArgb(255, 10, 12, 16))
    $g.FillPath($bg, $p)
    # drop: circle bottom + triangle top
    $cx = $s / 2.0; $cy = $s * 0.60; $rd = $s * 0.24
    $d = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d.AddArc([single]($cx - $rd), [single]($cy - $rd), [single](2 * $rd), [single](2 * $rd), -15, 210)
    $d.AddLine([single]($cx - $rd * 0.97), [single]($cy - $rd * 0.26), [single]$cx, [single]($s * 0.14))
    $d.AddLine([single]$cx, [single]($s * 0.14), [single]($cx + $rd * 0.97), [single]($cy - $rd * 0.26))
    $d.CloseFigure()
    $db = New-Object System.Drawing.Drawing2D.LinearGradientBrush((New-Object System.Drawing.PointF 0, ([single]($s * 0.14))), (New-Object System.Drawing.PointF 0, ([single]($cy + $rd))),
        [System.Drawing.Color]::FromArgb(255, 120, 220, 255), [System.Drawing.Color]::FromArgb(255, 10, 132, 255))
    $g.FillPath($db, $d)
    $g.Dispose()
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $frames += , @($s, $ms.ToArray())
    $bmp.Dispose()
}
$out = New-Object System.IO.MemoryStream
$bw = New-Object System.IO.BinaryWriter $out
$bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]$frames.Count)
$offset = 6 + 16 * $frames.Count
foreach ($f in $frames) {
    $s = $f[0]; $len = $f[1].Length
    $bw.Write([byte]($(if ($s -ge 256) { 0 } else { $s }))); $bw.Write([byte]($(if ($s -ge 256) { 0 } else { $s })))
    $bw.Write([byte]0); $bw.Write([byte]0); $bw.Write([uint16]1); $bw.Write([uint16]32)
    $bw.Write([uint32]$len); $bw.Write([uint32]$offset); $offset += $len
}
foreach ($f in $frames) { $bw.Write($f[1]) }
[System.IO.File]::WriteAllBytes((Join-Path $PSScriptRoot 'pcs.ico'), $out.ToArray())
"pcs.ico written"
