param(
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
$launcherLog = "launcher-stop-{0}.log" -f (Get-LauncherTimestamp)

try {
    Assert-LauncherWindows
    $lockHandle = Acquire-LauncherLock -Context $context
    Write-LauncherLog -Context $context -Name $launcherLog -Message "Stopping PaperAlgo launcher." | Out-Null

    $runtime = Read-LauncherRuntime -Context $context
    if ($null -eq $runtime) {
        if (-not $Quiet) {
            Show-LauncherMessage -Title "PaperAlgo stopped" -Message "PaperAlgo is already stopped." -TimeoutSeconds 5
        }
        exit 0
    }

    $apiRecord = Get-RuntimeRecord -Runtime $runtime -Role "api"
    $workerRecord = Get-RuntimeRecord -Runtime $runtime -Role "worker"
    $apiIdentity = Test-LauncherProcessRecord -Context $context -Record $apiRecord -Role "api"
    $workerIdentity = Test-LauncherProcessRecord -Context $context -Record $workerRecord -Role "worker"

    if ((-not $apiIdentity.IsMatch -and -not $apiIdentity.IsMissing) -or (-not $workerIdentity.IsMatch -and -not $workerIdentity.IsMissing)) {
        $reason = "api={0}; worker={1}" -f $apiIdentity.Reason, $workerIdentity.Reason
        throw ("Refusing to stop because launcher process identity is incomplete or mismatched: {0}" -f $reason)
    }

    if ($apiIdentity.IsMatch) {
        Assert-NoUnsafeJobs -Context $context
        $apiIdentity = Test-LauncherProcessRecord -Context $context -Record $apiRecord -Role "api"
        $workerIdentity = Test-LauncherProcessRecord -Context $context -Record $workerRecord -Role "worker"
        if ((-not $apiIdentity.IsMatch -and -not $apiIdentity.IsMissing) -or (-not $workerIdentity.IsMatch -and -not $workerIdentity.IsMissing)) {
            $reason = "api={0}; worker={1}" -f $apiIdentity.Reason, $workerIdentity.Reason
            throw ("Refusing to stop after final identity check: {0}" -f $reason)
        }
    } elseif ($workerIdentity.IsMatch) {
        throw "FastAPI is not readable while the Worker is still recorded as running; refusing to stop."
    }

    if ($workerIdentity.IsMatch) {
        $stoppedWorker = Stop-VerifiedLauncherProcess -Context $context -Record $workerRecord -Role "worker" -TimeoutSeconds 8
        if (-not $stoppedWorker) {
            throw "SQLite Worker did not exit within the bounded wait."
        }
    }

    if ($apiIdentity.IsMatch) {
        $stoppedApi = Stop-VerifiedLauncherProcess -Context $context -Record $apiRecord -Role "api" -TimeoutSeconds 8
        if (-not $stoppedApi) {
            throw "FastAPI did not exit within the bounded wait."
        }
    }

    Archive-LauncherRuntime -Context $context -Reason "stopped"
    Write-LauncherLog -Context $context -Name $launcherLog -Message "PaperAlgo launcher stopped successfully." | Out-Null
    if (-not $Quiet) {
        Show-LauncherMessage -Title "PaperAlgo stopped" -Message "PaperAlgo has been stopped. Existing browser tabs were left open." -TimeoutSeconds 5
    }
    exit 0
} catch {
    $message = [string]$_.Exception.Message
    $logPath = Write-LauncherLog -Context $context -Name $launcherLog -Message ("ERROR: {0}" -f $message)
    if (-not $Quiet) {
        Show-LauncherMessage -Title "PaperAlgo stop refused" -Message ("{0}`n`nLogs: {1}" -f $message, $context.LogDirectory)
    }
    Write-Error ("PaperAlgo stop refused. See {0}" -f $logPath)
    exit 1
} finally {
    Close-LauncherLock -LockHandle $lockHandle
}
