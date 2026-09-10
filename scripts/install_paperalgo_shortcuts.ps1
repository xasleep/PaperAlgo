param(
    [string]$ShortcutDirectory = "",
    [switch]$SkipBuild,
    [switch]$Quiet
)

Set-StrictMode -Version 2.0

function Get-AbsolutePath {
    param(
        [Parameter(Mandatory=$true)][string]$PathValue,
        [string]$BasePath = ""
    )
    $expanded = [Environment]::ExpandEnvironmentVariables($PathValue)
    if (-not [System.IO.Path]::IsPathRooted($expanded)) {
        if (-not $BasePath) {
            $BasePath = (Get-Location).Path
        }
        $expanded = Join-Path $BasePath $expanded
    }
    return [System.IO.Path]::GetFullPath($expanded).TrimEnd('\')
}

function Show-InstallMessage {
    param(
        [Parameter(Mandatory=$true)][string]$Title,
        [Parameter(Mandatory=$true)][string]$Message
    )
    try {
        $shell = New-Object -ComObject WScript.Shell
        $null = $shell.Popup($Message, 0, $Title, 48)
        return
    } catch {
    }
    Write-Host $Message
}

function Quote-ShortcutArgument {
    param([Parameter(Mandatory=$true)][string]$Value)
    return ('"{0}"' -f ($Value -replace '"', '\"'))
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw "PaperAlgo shortcuts can be installed only on Windows."
    }
    $repoRoot = Get-AbsolutePath -PathValue (Split-Path -Parent $PSScriptRoot)
    $webUiRoot = Join-Path $repoRoot "web_ui"
    $nodeModules = Join-Path $webUiRoot "node_modules"
    $nodeCommand = Get-Command node -ErrorAction Stop
    $npmCommand = Get-Command npm.cmd -ErrorAction Stop
    if (-not (Test-Path -LiteralPath $nodeModules -PathType Container)) {
        throw "Missing web_ui\node_modules. Install existing npm dependencies before installing shortcuts."
    }
    if (-not $SkipBuild) {
        Push-Location $webUiRoot
        try {
            & $npmCommand.Source run build
            if ($LASTEXITCODE -ne 0) {
                throw "npm run build failed."
            }
            & $npmCommand.Source run verify:same-origin
            if ($LASTEXITCODE -ne 0) {
                throw "npm run verify:same-origin failed."
            }
        } finally {
            Pop-Location
        }
    }

    $desktop = if ($ShortcutDirectory) {
        Get-AbsolutePath -PathValue $ShortcutDirectory -BasePath $repoRoot
    } else {
        [Environment]::GetFolderPath("Desktop")
    }
    New-Item -ItemType Directory -Force -Path $desktop | Out-Null

    $pythonwPath = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
    if (-not (Test-Path -LiteralPath $pythonwPath -PathType Leaf)) {
        throw "Missing .venv\Scripts\pythonw.exe. Create the project virtual environment before installing shortcuts."
    }
    $startScriptPath = Get-AbsolutePath -PathValue (Join-Path $PSScriptRoot "start_paperalgo.pyw")
    if (-not (Test-Path -LiteralPath $startScriptPath -PathType Leaf)) {
        throw "Missing scripts\start_paperalgo.pyw."
    }

    $startShortcutName = "$([char]0x542F)$([char]0x52A8) PaperAlgo.lnk"
    $legacyStopShortcutName = "$([char]0x505C)$([char]0x6B62) PaperAlgo.lnk"
    $legacyStopShortcutPath = Join-Path $desktop $legacyStopShortcutName
    Remove-Item -LiteralPath $legacyStopShortcutPath -Force -ErrorAction SilentlyContinue

    $shell = New-Object -ComObject WScript.Shell
    $shortcutPath = Join-Path $desktop $startShortcutName
    $temporaryShortcutPath = Join-Path $desktop ("PaperAlgo-start-{0}.lnk" -f ([guid]::NewGuid().ToString("N")))
    $shortcut = $shell.CreateShortcut($temporaryShortcutPath)
    $shortcut.TargetPath = $pythonwPath
    $shortcut.Arguments = Quote-ShortcutArgument -Value $startScriptPath
    $shortcut.WorkingDirectory = $repoRoot
    $shortcut.IconLocation = $pythonwPath
    $shortcut.Save()
    Move-Item -LiteralPath $temporaryShortcutPath -Destination $shortcutPath -Force
    if (-not $Quiet) {
        Show-InstallMessage -Title "PaperAlgo shortcut installed" -Message ("PaperAlgo shortcut is installed in: {0}" -f $desktop)
    }
    exit 0
} catch {
    $message = [string]$_.Exception.Message
    if (-not $Quiet) {
        Show-InstallMessage -Title "PaperAlgo shortcut install failed" -Message $message
    }
    Write-Error $message
    exit 1
}
