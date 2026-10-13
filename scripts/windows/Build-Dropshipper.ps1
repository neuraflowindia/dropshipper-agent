<# Build the console-free .NET Framework app with its own multi-size icon.
Uses the Windows compiler and drawing libraries already installed; no download.
#>
[CmdletBinding()]
param([string] $RepositoryPath)
$ErrorActionPreference = 'Stop'
if (-not $RepositoryPath) { $RepositoryPath = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent }
$RepositoryPath = (Resolve-Path -LiteralPath $RepositoryPath).Path
$output = Join-Path $RepositoryPath 'dist\windows'
$assetDirectory = Join-Path $RepositoryPath 'assets\windows'
New-Item -ItemType Directory -Force -Path $output, $assetDirectory | Out-Null
Add-Type -AssemblyName System.Drawing
$sizes = @(16, 24, 32, 48, 64, 128, 256)
$images = New-Object 'System.Collections.Generic.List[byte[]]'
foreach ($size in $sizes) {
    $bitmap = New-Object System.Drawing.Bitmap($size, $size)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.ScaleTransform(($size / 256.0), ($size / 256.0))
    $background = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(117, 37, 57))
    $cream = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(255, 247, 231), 13)
    $gold = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(229, 188, 111), 15)
    $cream.StartCap = $cream.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $cream.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $gold.StartCap = $gold.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddArc(4, 4, 64, 64, 180, 90); $path.AddArc(188, 4, 64, 64, 270, 90)
    $path.AddArc(188, 188, 64, 64, 0, 90); $path.AddArc(4, 188, 64, 64, 90, 90); $path.CloseFigure()
    $graphics.FillPath($background, $path)
    # A package and a small research lens: readable even in the notification tray.
    $graphics.DrawLines($cream, [System.Drawing.PointF[]]@(
        [System.Drawing.PointF]::new(54, 88), [System.Drawing.PointF]::new(116, 55),
        [System.Drawing.PointF]::new(178, 88), [System.Drawing.PointF]::new(116, 123),
        [System.Drawing.PointF]::new(54, 88), [System.Drawing.PointF]::new(54, 163),
        [System.Drawing.PointF]::new(116, 198), [System.Drawing.PointF]::new(116, 123)))
    $graphics.DrawLine($cream, 116, 198, 145, 182)
    $graphics.DrawLine($cream, 178, 88, 178, 111)
    $graphics.DrawLine($cream, 84, 72, 147, 106)
    $graphics.DrawEllipse($gold, 141, 126, 52, 52)
    $graphics.DrawLine($gold, 185, 170, 212, 198)
    $stream = New-Object System.IO.MemoryStream
    $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $images.Add($stream.ToArray())
    if ($size -eq 256) { $bitmap.Save((Join-Path $assetDirectory 'neuraflow-dropshipper.png'), [System.Drawing.Imaging.ImageFormat]::Png) }
    $stream.Dispose(); $path.Dispose(); $cream.Dispose(); $gold.Dispose(); $background.Dispose(); $graphics.Dispose(); $bitmap.Dispose()
}
$iconPath = Join-Path $assetDirectory 'neuraflow-dropshipper.ico'
$iconStream = [System.IO.File]::Create($iconPath)
$writer = New-Object System.IO.BinaryWriter($iconStream)
try {
    $writer.Write([UInt16]0); $writer.Write([UInt16]1); $writer.Write([UInt16]$sizes.Count)
    $offset = 6 + 16 * $sizes.Count
    for ($i = 0; $i -lt $sizes.Count; $i++) {
        $dimension = if ($sizes[$i] -eq 256) { 0 } else { $sizes[$i] }
        $writer.Write([byte]$dimension); $writer.Write([byte]$dimension); $writer.Write([byte]0); $writer.Write([byte]0)
        $writer.Write([UInt16]1); $writer.Write([UInt16]32)
        $writer.Write([UInt32]$images[$i].Length); $writer.Write([UInt32]$offset)
        $offset += $images[$i].Length
    }
    foreach ($imageBytes in $images) { $writer.Write([byte[]]$imageBytes) }
} finally { $writer.Dispose(); $iconStream.Dispose() }
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'The installed Windows .NET compiler was not found.' }
$executable = Join-Path $output 'NeuraFlow Dropshipper.exe'
$stagingExecutable = Join-Path $output ('NeuraFlow Dropshipper.build-' + [Guid]::NewGuid().ToString('N') + '.exe')
& $compiler /nologo /target:winexe /optimize+ /platform:anycpu "/win32icon:$iconPath" "/out:$stagingExecutable" `
    "/win32manifest:$(Join-Path $PSScriptRoot 'DropshipperDesktop.manifest')" `
    /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll `
    (Join-Path $PSScriptRoot 'DropshipperDesktop.cs')
if ($LASTEXITCODE -ne 0) { throw 'The desktop app did not compile. No scheduled task or shortcut was changed.' }
try {
    if (Test-Path -LiteralPath $executable) {
        $backupDirectory = Join-Path $RepositoryPath 'data\operations\desktop'
        New-Item -ItemType Directory -Force -Path $backupDirectory | Out-Null
        # PowerShell coerces a null backup string to an illegal empty path.
        # Keep one explicit previous binary instead; replacement stays atomic.
        [System.IO.File]::Replace($stagingExecutable, $executable, (Join-Path $backupDirectory 'previous-launcher.exe'))
    }
    else { [System.IO.File]::Move($stagingExecutable, $executable) }
} catch {
    throw ('Windows could not replace the executable: ' + $_.Exception.GetBaseException().Message + ' The previous executable was preserved. If the app is running, Exit it before rebuilding.')
}
[pscustomobject]@{Executable=$executable; Icon=$iconPath; SHA256=(Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash} | ConvertTo-Json -Compress
