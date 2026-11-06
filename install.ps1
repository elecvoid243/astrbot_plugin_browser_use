param([string]$AstrBotDataDir)

$ErrorActionPreference = 'Stop'
$sourcePath = Join-Path $PSScriptRoot 'astrbot_plugin_browser_use'
if (-not (Test-Path -LiteralPath (Join-Path $sourcePath 'runtime/python/python.exe') -PathType Leaf)) {
    throw 'Use this installer from the extracted complete offline package.'
}
if (-not $AstrBotDataDir) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = 'Select the existing AstrBot data directory. Stop AstrBot before installing.'
    $dialog.ShowNewFolderButton = $false
    try {
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            throw 'Installation cancelled.'
        }
        $AstrBotDataDir = $dialog.SelectedPath
    } finally {
        $dialog.Dispose()
    }
}
$dataPath = [System.IO.Path]::GetFullPath($AstrBotDataDir)
if (-not (Test-Path -LiteralPath $dataPath -PathType Container)) {
    throw 'The AstrBot data directory does not exist.'
}
$pluginsPath = Join-Path $dataPath 'plugins'
if (-not (Test-Path -LiteralPath $pluginsPath -PathType Container)) {
    throw 'Select the existing AstrBot data directory containing the plugins folder.'
}
$destinationPath = [System.IO.Path]::GetFullPath((Join-Path $pluginsPath 'astrbot_plugin_browser_use'))
if (-not $destinationPath.StartsWith($pluginsPath.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'The installation destination escaped the AstrBot plugins directory.'
}
if (Test-Path -LiteralPath $destinationPath) {
    throw 'The plugin directory already exists. Stop AstrBot and move the old plugin out of the plugins folder before installing this package. Persistent data is stored separately under plugin_data.'
}
# Windows native loaders can reject long DLL paths even when file copies succeed.
foreach ($nativeFile in Get-ChildItem -LiteralPath (Join-Path $sourcePath 'runtime') -Recurse -File) {
    if ($nativeFile.Extension -notin @('.dll', '.pyd', '.exe')) { continue }
    $relativeNative = $nativeFile.FullName.Substring($sourcePath.Length).TrimStart('\', '/')
    if ((Join-Path $destinationPath $relativeNative).Length -ge 260) {
        throw 'The installation path is too long for bundled native libraries. Select a shorter AstrBot data directory.'
    }
}
Copy-Item -LiteralPath $sourcePath -Destination $pluginsPath -Recurse
Write-Host "Installed: $destinationPath"
Write-Host 'Start AstrBot. Use the plugin switch in the dashboard to start or stop Browser Use MCP.'
