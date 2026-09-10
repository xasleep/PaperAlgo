param(
    [switch]$NoBrowser,
    [int]$Port = 8000,
    [string]$LocalDirectory = "",
    [string]$RunsDirectory = "",
    [string]$DatabasePath = "",
    [string]$LauncherDirectory = "",
    [switch]$Quiet
)

. (Join-Path $PSScriptRoot "paperalgo_launcher_common.ps1")

$context = New-LauncherContext `
    -ScriptRoot $PSScriptRoot `
    -Port $Port `
    -LocalDirectory $LocalDirectory `
    -RunsDirectory $RunsDirectory `
    -DatabasePath $DatabasePath `
    -LauncherDirectory $LauncherDirectory
$lockHandle = $null
$startedRecords = @()
$launcherLog = "launcher-start-{0}.log" -f (Get-LauncherTimestamp)

try {
    Assert-LauncherWindows
    $lockHandle = Acquire-LauncherLock -Context $context
    Write-LauncherLog -Context $context -Name $launcherLog -Message "Starting PaperAlgo launcher." | Out-Null
    Assert-LauncherPrerequisites -Context $context

    $runtime = Read-LauncherRuntime -Context $context
    $apiRecord = Get-RuntimeRecord -Runtime $runtime -Role "api"
    $workerRecord = Get-RuntimeRecord -Runtime $runtime -Role "worker"
    $apiIdentity = Test-LauncherProcessRecord -Context $context -Record $apiRecord -Role "api"
    $workerIdentity = Test-LauncherProcessRecord -Context $context -Record $workerRecord -Role "worker"

    if ((-not $apiIdentity.IsMatch -and -not $apiIdentity.IsMissing) -or (-not $workerIdentity.IsMatch -and -not $workerIdentity.IsMissing)) {
        $reason = "api={0}; worker={1}" -f $apiIdentity.Reason, $workerIdentity.Reason
        throw ("Existing launcher runtime cannot be trusted: {0}" -f $reason)
    }

    Assert-PortAvailableOrManaged -Context $context -ApiRecord $apiRecord

    $records = @()
    $environmentSnapshot = Set-LauncherEnvironment -Context $context
    try {
        if ($apiIdentity.IsMatch) {
            $records += $apiRecord
        } else {
            $newApi = Start-LauncherManagedProcess -Context $context -Role "api"
            $startedRecords += $newApi
            $records += $newApi
        }

        if ($workerIdentity.IsMatch) {
            $records += $workerRecord
        } else {
            $newWorker = Start-LauncherManagedProcess -Context $context -Role "worker"
            $startedRecords += $newWorker
            $records += $newWorker
        }
    } finally {
        Restore-LauncherEnvironment -Previous $environmentSnapshot
    }

    Save-LauncherRuntime -Context $context -Processes $records

    if (-not (Wait-ForApiReady -Context $context -TimeoutSeconds 30)) {
        throw "FastAPI health or WebUI root check did not pass within 30 seconds."
    }

    Start-Sleep -Seconds 2
    $workerCurrent = Get-RuntimeRecord -Runtime (Read-LauncherRuntime -Context $context) -Role "worker"
    $workerCurrentIdentity = Test-LauncherProcessRecord -Context $context -Record $workerCurrent -Role "worker"
    if (-not $workerCurrentIdentity.IsMatch) {
        throw ("SQLite Worker is not alive after startup: {0}" -f $workerCurrentIdentity.Reason)
    }

    if (-not $NoBrowser) {
        Open-LauncherBrowser -Context $context
    }
    Write-LauncherLog -Context $context -Name $launcherLog -Message "PaperAlgo launcher started successfully." | Out-Null
    exit 0
} catch {
    $message = [string]$_.Exception.Message
    $logPath = Write-LauncherLog -Context $context -Name $launcherLog -Message ("ERROR: {0}" -f $message)
    foreach ($record in @($startedRecords)) {
        try {
            $null = Stop-VerifiedLauncherProcess -Context $context -Record $record -Role ([string]$record.role) -TimeoutSeconds 8
        } catch {
            Write-LauncherLog -Context $context -Name $launcherLog -Message ("Cleanup refused or failed for {0}: {1}" -f $record.role, $_.Exception.Message) | Out-Null
        }
    }
    if ($startedRecords.Count -gt 0) {
        Archive-LauncherRuntime -Context $context -Reason "failed"
    }
    if (-not $Quiet) {
        Show-LauncherMessage -Title "PaperAlgo start failed" -Message ("{0}`n`nLogs: {1}" -f $message, $context.LogDirectory)
    }
    Write-Error ("PaperAlgo start failed. See {0}" -f $logPath)
    exit 1
} finally {
    Close-LauncherLock -LockHandle $lockHandle
}
