[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
$temporaryRoot = Join-Path $projectRoot '.tmp'
$logRoot = Join-Path $temporaryRoot 'schedule-logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

$runStamp = '{0}-{1}' -f (Get-Date -Format 'yyyyMMdd-HHmmss-fff'), $PID
$statusLog = Join-Path $logRoot "scheduled-$runStamp.status.log"
$stdoutLog = Join-Path $logRoot "scheduled-$runStamp.stdout.log"
$stderrLog = Join-Path $logRoot "scheduled-$runStamp.stderr.log"
$lockPath = Join-Path $temporaryRoot 'scheduled-run.lock'
$lockHandle = $null

function Write-ScheduleLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss.fffK'), $Message
    Add-Content -LiteralPath $statusLog -Value $line -Encoding UTF8
}

try {
    $timeZone = [System.TimeZoneInfo]::Local
    if ($timeZone.Id -ne 'China Standard Time') {
        throw "The Windows time zone is '$($timeZone.Id)', not 'China Standard Time'; refusing to run at the wrong local time."
    }

    try {
        $lockHandle = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch [System.IO.IOException] {
        Write-ScheduleLog 'Skipped: another scheduled run still owns the project lock.'
        exit 0
    }

    $lockHandle.SetLength(0)
    $lockText = "pid=$PID`r`nstarted=$(Get-Date -Format 'o')`r`n"
    $lockBytes = [System.Text.Encoding]::UTF8.GetBytes($lockText)
    $lockHandle.Write($lockBytes, 0, $lockBytes.Length)
    $lockHandle.Flush()

    $pythonConsole = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $pythonWindowed = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonConsole -PathType Leaf)) {
        throw 'Missing .venv\Scripts\python.exe. Run setup.ps1 first.'
    }

    $runtimePython = if (Test-Path -LiteralPath $pythonWindowed -PathType Leaf) {
        $pythonWindowed
    }
    else {
        $pythonConsole
    }

    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    $env:TEMP = $temporaryRoot
    $env:TMP = $temporaryRoot

    Write-ScheduleLog "Starting: $runtimePython -m app.runner scheduled"
    Write-ScheduleLog "stdout=$stdoutLog"
    Write-ScheduleLog "stderr=$stderrLog"

    $process = Start-Process `
        -FilePath $runtimePython `
        -ArgumentList @('-m', 'app.runner', 'scheduled') `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru `
        -Wait

    $exitCode = $process.ExitCode
    Write-ScheduleLog "Finished with exit code $exitCode."
    exit $exitCode
}
catch {
    Write-ScheduleLog ("Failed: {0}" -f $_.Exception.Message)
    exit 1
}
finally {
    if ($null -ne $lockHandle) {
        $lockHandle.Dispose()
    }
}
