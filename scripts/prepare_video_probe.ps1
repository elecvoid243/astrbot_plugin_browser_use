param(
    [Parameter(Mandatory = $true)][string]$RuntimePythonDir,
    [Parameter(Mandatory = $true)][string]$ProbeDir,
    [Parameter(Mandatory = $true)][string]$WheelDir
)
$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $RuntimePythonDir).Path
$destination = [System.IO.Path]::GetFullPath($ProbeDir)
if ($destination.StartsWith($source, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'The isolated interpreter must be outside the original runtime.'
}
New-Item -ItemType Directory -Force -Path $destination | Out-Null
Get-ChildItem -LiteralPath $source -File | Copy-Item -Destination $destination
$sitePackages = Join-Path $destination 'Lib/site-packages'
$originalPackages = Join-Path $source 'Lib/site-packages'
$pathLines = @('python313.zip', '.', 'Lib/site-packages', $originalPackages, 'import site')
Set-Content -LiteralPath (Join-Path $destination 'python313._pth') -Value $pathLines -Encoding ascii
$env:PYTHONDONTWRITEBYTECODE = '1'
$interpreter = Join-Path $destination 'python.exe'
& $interpreter -B -m pip install --no-cache-dir --no-index --find-links $WheelDir --require-hashes --upgrade --target $sitePackages -r (Join-Path $PSScriptRoot 'video-probe-win-x64.lock')
if ($LASTEXITCODE -ne 0) { throw 'Isolated probe dependency installation failed.' }
& $interpreter -B -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Isolated probe dependencies are inconsistent.' }
