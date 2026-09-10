Set-StrictMode -Version 2.0

function Get-LauncherTimestamp {
    return (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
}

function Assert-LauncherWindows {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw "PaperAlgo launcher is supported only on Windows."
    }
}

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

function New-LauncherContext {
    param(
        [Parameter(Mandatory=$true)][string]$ScriptRoot,
        [int]$Port = 8000,
        [string]$LocalDirectory = "",
        [string]$RunsDirectory = "",
        [string]$DatabasePath = "",
        [string]$LauncherDirectory = ""
    )
    $repoRoot = Get-AbsolutePath -PathValue (Split-Path -Parent $ScriptRoot)
    $localDir = if ($LocalDirectory) { Get-AbsolutePath -PathValue $LocalDirectory -BasePath $repoRoot } else { Join-Path $repoRoot ".local" }
    $runsDir = if ($RunsDirectory) { Get-AbsolutePath -PathValue $RunsDirectory -BasePath $repoRoot } else { Join-Path $repoRoot "runs" }
    $dbPath = if ($DatabasePath) { Get-AbsolutePath -PathValue $DatabasePath -BasePath $repoRoot } else { Join-Path $localDir "paper2code.db" }
    $launcherDir = if ($LauncherDirectory) { Get-AbsolutePath -PathValue $LauncherDirectory -BasePath $repoRoot } else { Join-Path $localDir "launcher" }
    $logDir = Join-Path $launcherDir "logs"
    New-Item -ItemType Directory -Force -Path $launcherDir | Out-Null
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    return [pscustomobject]@{
        RepoRoot = $repoRoot
        ScriptRoot = (Get-AbsolutePath -PathValue $ScriptRoot)
        LocalDirectory = (Get-AbsolutePath -PathValue $localDir)
        RunsDirectory = (Get-AbsolutePath -PathValue $runsDir)
        DatabasePath = (Get-AbsolutePath -PathValue $dbPath)
        LauncherDirectory = (Get-AbsolutePath -PathValue $launcherDir)
        LogDirectory = (Get-AbsolutePath -PathValue $logDir)
        RuntimePath = (Join-Path $launcherDir "runtime.json")
        LockPath = (Join-Path $launcherDir "launcher.lock")
        Port = $Port
        PythonPath = (Join-Path $repoRoot ".venv\Scripts\python.exe")
        DistIndexPath = (Join-Path $repoRoot "web_ui\dist\index.html")
        AppUrl = ("http://127.0.0.1:{0}" -f $Port)
        HealthUrl = ("http://127.0.0.1:{0}/api/v1/health" -f $Port)
        JobsUrl = ("http://127.0.0.1:{0}/api/v1/jobs?limit=200" -f $Port)
    }
}

function Acquire-LauncherLock {
    param([Parameter(Mandatory=$true)]$Context)
    try {
        return [System.IO.File]::Open(
            $Context.LockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch {
        throw "Another PaperAlgo launcher operation is already running."
    }
}

function Close-LauncherLock {
    param($LockHandle)
    if ($null -ne $LockHandle) {
        $LockHandle.Dispose()
    }
}

function Write-LauncherLog {
    param(
        [Parameter(Mandatory=$true)]$Context,
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$Message
    )
    $path = Join-Path $Context.LogDirectory $Name
    $line = "{0} {1}" -f (Get-Date).ToUniversalTime().ToString("o"), $Message
    Add-Content -LiteralPath $path -Value $line -Encoding UTF8
    return $path
}

function Show-LauncherMessage {
    param(
        [Parameter(Mandatory=$true)][string]$Title,
        [Parameter(Mandatory=$true)][string]$Message,
        [int]$TimeoutSeconds = 0
    )
    try {
        $shell = New-Object -ComObject WScript.Shell
        $null = $shell.Popup($Message, $TimeoutSeconds, $Title, 48)
        return
    } catch {
    }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $null = [System.Windows.Forms.MessageBox]::Show($Message, $Title)
        return
    } catch {
    }
    Write-Host $Message
}

function ConvertTo-LauncherJson {
    param([Parameter(Mandatory=$true)]$Value)
    return ($Value | ConvertTo-Json -Depth 8)
}

function Save-LauncherRuntime {
    param(
        [Parameter(Mandatory=$true)]$Context,
        [Parameter(Mandatory=$true)]$Processes
    )
    $runtime = [pscustomobject]@{
        schemaVersion = 1
        repoRoot = $Context.RepoRoot
        port = $Context.Port
        updatedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
        processes = @($Processes)
    }
    $json = ConvertTo-LauncherJson -Value $runtime
    $tempPath = "{0}.tmp-{1}" -f $Context.RuntimePath, ([guid]::NewGuid().ToString("N"))
    [System.IO.File]::WriteAllText($tempPath, $json, [System.Text.Encoding]::UTF8)
    if (Test-Path -LiteralPath $Context.RuntimePath) {
        $backupPath = "{0}.bak-{1}" -f $Context.RuntimePath, ([guid]::NewGuid().ToString("N"))
        [System.IO.File]::Replace($tempPath, $Context.RuntimePath, $backupPath)
        Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
    } else {
        [System.IO.File]::Move($tempPath, $Context.RuntimePath)
    }
}

function Read-LauncherRuntime {
    param([Parameter(Mandatory=$true)]$Context)
    if (-not (Test-Path -LiteralPath $Context.RuntimePath)) {
        return $null
    }
    try {
        $raw = Get-Content -LiteralPath $Context.RuntimePath -Raw -Encoding UTF8
        return ($raw | ConvertFrom-Json)
    } catch {
        $stamp = Get-LauncherTimestamp
        $corruptPath = Join-Path $Context.LauncherDirectory ("runtime.corrupt.{0}.json" -f $stamp)
        Move-Item -LiteralPath $Context.RuntimePath -Destination $corruptPath -Force
        return $null
    }
}

function Archive-LauncherRuntime {
    param(
        [Parameter(Mandatory=$true)]$Context,
        [string]$Reason = "stopped"
    )
    if (Test-Path -LiteralPath $Context.RuntimePath) {
        $stamp = Get-LauncherTimestamp
        $archivePath = Join-Path $Context.LauncherDirectory ("runtime.{0}.{1}.json" -f $Reason, $stamp)
        Move-Item -LiteralPath $Context.RuntimePath -Destination $archivePath -Force
    }
}

function Get-RuntimeRecord {
    param(
        $Runtime,
        [Parameter(Mandatory=$true)][string]$Role
    )
    if ($null -eq $Runtime -or $null -eq $Runtime.processes) {
        return $null
    }
    foreach ($record in @($Runtime.processes)) {
        if ([string]$record.role -eq $Role) {
            return $record
        }
    }
    return $null
}

function Get-Win32ProcessInfo {
    param([Parameter(Mandatory=$true)][int]$ProcessIdValue)
    try {
        return Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ProcessIdValue) -ErrorAction Stop
    } catch {
        return Get-WmiObject Win32_Process -Filter ("ProcessId = {0}" -f $ProcessIdValue) -ErrorAction Stop
    }
}

function Test-ProcessIsSelfOrDescendant {
    param(
        [Parameter(Mandatory=$true)][int]$ProcessIdValue,
        [Parameter(Mandatory=$true)][int]$RootProcessId
    )
    if ($ProcessIdValue -eq $RootProcessId) {
        return $true
    }
    $current = $ProcessIdValue
    for ($depth = 0; $depth -lt 8; $depth++) {
        try {
            $processInfo = Get-Win32ProcessInfo -ProcessIdValue $current
        } catch {
            return $false
        }
        if ($null -eq $processInfo -or $null -eq $processInfo.ParentProcessId) {
            return $false
        }
        $parent = [int]$processInfo.ParentProcessId
        if ($parent -eq $RootProcessId) {
            return $true
        }
        if ($parent -eq 0 -or $parent -eq $current) {
            return $false
        }
        $current = $parent
    }
    return $false
}

function Get-ProcessIdentitySnapshot {
    param(
        [Parameter(Mandatory=$true)]$ProcessObject,
        [Parameter(Mandatory=$true)][string]$Role,
        [Parameter(Mandatory=$true)]$Context,
        [Parameter(Mandatory=$true)][string]$StdoutLog,
        [Parameter(Mandatory=$true)][string]$StderrLog
    )
    $processIdValue = [int]$ProcessObject.Id
    $processInfo = Get-Win32ProcessInfo -ProcessIdValue $processIdValue
    if ($null -eq $processInfo -or -not $processInfo.ExecutablePath -or -not $processInfo.CommandLine) {
        throw "Could not record process identity for $Role."
    }
    $started = (Get-Process -Id $processIdValue -ErrorAction Stop).StartTime.ToUniversalTime()
    return [pscustomobject]@{
        role = $Role
        processId = $processIdValue
        processStartTimeUtc = $started.ToString("o")
        processStartTimeTicks = $started.Ticks
        executablePath = (Get-AbsolutePath -PathValue $processInfo.ExecutablePath)
        commandRole = $Role
        repoRoot = $Context.RepoRoot
        startedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
        stdoutLog = (Get-AbsolutePath -PathValue $StdoutLog)
        stderrLog = (Get-AbsolutePath -PathValue $StderrLog)
        port = $Context.Port
    }
}

function Test-LauncherProcessRecord {
    param(
        [Parameter(Mandatory=$true)]$Context,
        $Record,
        [Parameter(Mandatory=$true)][string]$Role
    )
    if ($null -eq $Record) {
        return [pscustomobject]@{ IsMatch = $false; IsMissing = $true; Reason = "record_missing"; Process = $null }
    }
    $required = @("role", "processId", "processStartTimeTicks", "executablePath", "commandRole", "repoRoot", "port")
    foreach ($field in $required) {
        if (-not ($Record.PSObject.Properties.Name -contains $field) -or $null -eq $Record.$field -or [string]$Record.$field -eq "") {
            return [pscustomobject]@{ IsMatch = $false; IsMissing = $false; Reason = "runtime_identity_incomplete"; Process = $null }
        }
    }
    if ([string]$Record.role -ne $Role -or [string]$Record.commandRole -ne $Role) {
        return [pscustomobject]@{ IsMatch = $false; IsMissing = $false; Reason = "role_mismatch"; Process = $null }
    }
    if ((Get-AbsolutePath -PathValue ([string]$Record.repoRoot)) -ne $Context.RepoRoot) {
        return [pscustomobject]@{ IsMatch = $false; IsMissing = $false; Reason = "repo_root_mismatch"; Process = $null }
    }
    if ([int]$Record.port -ne [int]$Context.Port) {
        return [pscustomobject]@{ IsMatch = $false; IsMissing = $false; Reason = "port_mismatch"; Process = $null }
    }
    try {
        $processIdValue = [int]$Record.processId
        $processObject = Get-Process -Id $processIdValue -ErrorAction Stop
    } catch {
        return [pscustomobject]@{ IsMatch = $false; IsMissing = $true; Reason = "process_missing"; Process = $null }
    }
    $actualTicks = $processObject.StartTime.ToUniversalTime().Ticks
    if ([int64]$Record.processStartTimeTicks -ne [int64]$actualTicks) {
        return [pscustomobject]@{ IsMatch = $false; IsMissing = $false; Reason = "process_start_time_mismatch"; Process = $processObject }
    }
    try {
        $processInfo = Get-Win32ProcessInfo -ProcessIdValue $processIdValue
    } catch {
        return [pscustomobject]@{ IsMatch = $false; IsMissing = $false; Reason = "process_identity_unreadable"; Process = $processObject }
    }
    if ($null -eq $processInfo -or -not $processInfo.ExecutablePath -or -not $processInfo.CommandLine) {
        return [pscustomobject]@{ IsMatch = $false; IsMissing = $false; Reason = "process_identity_unreadable"; Process = $processObject }
    }
    $actualExecutable = Get-AbsolutePath -PathValue $processInfo.ExecutablePath
    if ($actualExecutable -ne (Get-AbsolutePath -PathValue ([string]$Record.executablePath))) {
        return [pscustomobject]@{ IsMatch = $false; IsMissing = $false; Reason = "executable_path_mismatch"; Process = $processObject }
    }
    if ($actualExecutable -ne (Get-AbsolutePath -PathValue $Context.PythonPath)) {
        return [pscustomobject]@{ IsMatch = $false; IsMissing = $false; Reason = "python_venv_mismatch"; Process = $processObject }
    }
    $commandLine = [string]$processInfo.CommandLine
    $lowerCommand = $commandLine.ToLowerInvariant()
    $lowerPython = (Get-AbsolutePath -PathValue $Context.PythonPath).ToLowerInvariant()
    if (-not $lowerCommand.Contains($lowerPython)) {
        return [pscustomobject]@{ IsMatch = $false; IsMissing = $false; Reason = "command_python_mismatch"; Process = $processObject }
    }
    if ($Role -eq "api") {
        $needles = @("-m", "uvicorn", "web_api.main:app", "--host", "127.0.0.1", "--port", [string]$Context.Port)
    } else {
        $needles = @("-m", "web_api.worker")
    }
    foreach ($needle in $needles) {
        if (-not $lowerCommand.Contains($needle.ToLowerInvariant())) {
            return [pscustomobject]@{ IsMatch = $false; IsMissing = $false; Reason = ("command_line_mismatch_{0}" -f $Role); Process = $processObject }
        }
    }
    return [pscustomobject]@{ IsMatch = $true; IsMissing = $false; Reason = "matched"; Process = $processObject }
}

function Get-PortListeners {
    param([Parameter(Mandatory=$true)][int]$Port)
    try {
        return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } catch {
        throw "Could not inspect TCP port $Port."
    }
}

function Assert-PortAvailableOrManaged {
    param(
        [Parameter(Mandatory=$true)]$Context,
        $ApiRecord = $null
    )
    $listeners = @(Get-PortListeners -Port $Context.Port)
    if ($listeners.Count -eq 0) {
        return
    }
    if ($null -ne $ApiRecord) {
        $identity = Test-LauncherProcessRecord -Context $Context -Record $ApiRecord -Role "api"
        if ($identity.IsMatch) {
            foreach ($listener in $listeners) {
                if (Test-ProcessIsSelfOrDescendant -ProcessIdValue ([int]$listener.OwningProcess) -RootProcessId ([int]$ApiRecord.processId)) {
                    return
                }
            }
        }
    }
    throw ("Port {0} is already occupied by a process that is not managed by this launcher." -f $Context.Port)
}

function Set-LauncherEnvironment {
    param([Parameter(Mandatory=$true)]$Context)
    $values = @{
        "JOB_RUNTIME" = "sqlite"
        "PAPER2CODE_LOCAL_DIR" = $Context.LocalDirectory
        "PAPER2CODE_RUNS_DIR" = $Context.RunsDirectory
        "PAPER2CODE_DB_PATH" = $Context.DatabasePath
        "PYTHONUNBUFFERED" = "1"
    }
    $previous = @{}
    foreach ($name in $values.Keys) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, [string]$values[$name], "Process")
    }
    return $previous
}

function Restore-LauncherEnvironment {
    param([Parameter(Mandatory=$true)]$Previous)
    foreach ($name in $Previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $Previous[$name], "Process")
    }
}

function Start-LauncherManagedProcess {
    param(
        [Parameter(Mandatory=$true)]$Context,
        [Parameter(Mandatory=$true)][string]$Role
    )
    $stamp = Get-LauncherTimestamp
    $stdoutLog = Join-Path $Context.LogDirectory ("{0}-{1}-stdout.log" -f $Role, $stamp)
    $stderrLog = Join-Path $Context.LogDirectory ("{0}-{1}-stderr.log" -f $Role, $stamp)
    if ($Role -eq "api") {
        $arguments = @("-m", "uvicorn", "web_api.main:app", "--host", "127.0.0.1", "--port", [string]$Context.Port)
    } elseif ($Role -eq "worker") {
        $arguments = @("-m", "web_api.worker")
    } else {
        throw "Unknown launcher role: $Role."
    }
    $processObject = Start-Process `
        -FilePath $Context.PythonPath `
        -ArgumentList $arguments `
        -WorkingDirectory $Context.RepoRoot `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -WindowStyle Hidden `
        -PassThru
    Start-Sleep -Milliseconds 250
    return Get-ProcessIdentitySnapshot -ProcessObject $processObject -Role $Role -Context $Context -StdoutLog $stdoutLog -StderrLog $stderrLog
}

function Stop-VerifiedLauncherProcess {
    param(
        [Parameter(Mandatory=$true)]$Context,
        [Parameter(Mandatory=$true)]$Record,
        [Parameter(Mandatory=$true)][string]$Role,
        [int]$TimeoutSeconds = 8
    )
    $identity = Test-LauncherProcessRecord -Context $Context -Record $Record -Role $Role
    if (-not $identity.IsMatch) {
        if ($identity.IsMissing) {
            return $true
        }
        throw ("Refusing to stop {0}: {1}." -f $Role, $identity.Reason)
    }
    Stop-Process -Id ([int]$Record.processId) -ErrorAction Stop
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $null = Get-Process -Id ([int]$Record.processId) -ErrorAction Stop
            Start-Sleep -Milliseconds 250
        } catch {
            return $true
        }
    }
    return $false
}

function Assert-LauncherPrerequisites {
    param([Parameter(Mandatory=$true)]$Context)
    if (-not (Test-Path -LiteralPath $Context.PythonPath -PathType Leaf)) {
        throw ("Missing Python virtual environment executable: {0}" -f $Context.PythonPath)
    }
    if (-not (Test-Path -LiteralPath $Context.DistIndexPath -PathType Leaf)) {
        throw ("Missing built WebUI. Re-run scripts\install_paperalgo_shortcuts.ps1 before using the desktop shortcut.")
    }
    $code = "import uvicorn, web_api.main, web_api.worker"
    $output = & $Context.PythonPath -c $code 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw ("Python import check failed: {0}" -f (($output | Out-String).Trim()))
    }
}

function Test-ApiHealth {
    param([Parameter(Mandatory=$true)]$Context)
    try {
        $response = Invoke-RestMethod -Uri $Context.HealthUrl -Method Get -TimeoutSec 5
        return ($null -ne $response -and [string]$response.status -eq "ok")
    } catch {
        return $false
    }
}

function Test-WebUiRoot {
    param([Parameter(Mandatory=$true)]$Context)
    try {
        $response = Invoke-WebRequest -Uri $Context.AppUrl -UseBasicParsing -TimeoutSec 5
        return ([int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 300 -and ([string]$response.Content).Contains("root"))
    } catch {
        return $false
    }
}

function Wait-ForApiReady {
    param(
        [Parameter(Mandatory=$true)]$Context,
        [int]$TimeoutSeconds = 30
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ((Test-ApiHealth -Context $Context) -and (Test-WebUiRoot -Context $Context)) {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Open-LauncherBrowser {
    param([Parameter(Mandatory=$true)]$Context)
    Start-Process -FilePath $Context.AppUrl | Out-Null
}

function Assert-NoUnsafeJobs {
    param([Parameter(Mandatory=$true)]$Context)
    try {
        $response = Invoke-RestMethod -Uri $Context.JobsUrl -Method Get -TimeoutSec 8
    } catch {
        throw "Could not read local job status; refusing to stop PaperAlgo."
    }
    if ($null -eq $response -or -not ($response.PSObject.Properties.Name -contains "jobs")) {
        throw "Local job status response is not understood; refusing to stop PaperAlgo."
    }
    foreach ($job in @($response.jobs)) {
        $jobId = [string]$job.job_id
        $status = [string]$job.status
        $executionStatus = [string]$job.execution_status
        if (-not $executionStatus) {
            $executionStatus = $status
        }
        $recoveryStatus = [string]$job.recovery_status
        $processState = [string]$job.process_state
        $cancelUnavailable = [string]$job.cancel_unavailable_reason
        $isTerminal = @("completed", "failed", "canceled") -contains $executionStatus
        if (-not $isTerminal) {
            throw ("Job {0} is not terminal ({1}); cancel it in WebUI or wait before stopping." -f $jobId, $executionStatus)
        }
        if ($job.process_active -eq $true) {
            throw ("Job {0} still has an active process; refusing to stop." -f $jobId)
        }
        if ($processState -eq "detached" -or $cancelUnavailable -eq "process_identity_unresolved" -or $executionStatus -eq "identity_unresolved") {
            throw ("Job {0} has unresolved process identity; refusing to stop." -f $jobId)
        }
        if ($recoveryStatus -and -not (@("none", "completed", "failed") -contains $recoveryStatus)) {
            throw ("Job {0} recovery status is {1}; refusing to stop." -f $jobId, $recoveryStatus)
        }
    }
}
