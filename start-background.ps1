[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
$temporaryRoot = Join-Path $projectRoot '.tmp'
$logRoot = Join-Path $temporaryRoot 'background-logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$runStamp = '{0}-{1}' -f (Get-Date -Format 'yyyyMMdd-HHmmss-fff'), $PID
$statusLog = Join-Path $logRoot "server-$runStamp.status.log"
$stdoutLog = Join-Path $logRoot "server-$runStamp.stdout.log"
$stderrLog = Join-Path $logRoot "server-$runStamp.stderr.log"
$pidPath = Join-Path $temporaryRoot 'background-server.pid'
$port = 8765
$expectedDataRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot 'data')).TrimEnd('\')

function Write-BackgroundLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss.fffK'), $Message
    Add-Content -LiteralPath $statusLog -Value $line -Encoding UTF8
}

function Test-WorkbenchService {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -Method Get -TimeoutSec 3
        if ([string]$health.status -ne 'ok' -or [string]$health.product -ne 'a-share-decision') {
            return $false
        }
        if ([string]::IsNullOrWhiteSpace([string]$health.version)) {
            return $false
        }
        $actualDataRoot = [System.IO.Path]::GetFullPath([string]$health.data_root).TrimEnd('\')
        return $actualDataRoot -eq $expectedDataRoot
    }
    catch {
        return $false
    }
}

function Test-LocalPort {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $attempt = $client.BeginConnect('127.0.0.1', $port, $null, $null)
        if (-not $attempt.AsyncWaitHandle.WaitOne(750)) {
            return $false
        }
        $client.EndConnect($attempt)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Get-WorkbenchServicePid {
    try {
        $connection = Get-NetTCPConnection `
            -LocalAddress '127.0.0.1' `
            -LocalPort $port `
            -State Listen `
            -ErrorAction Stop | Select-Object -First 1
        if ($null -ne $connection -and [int]$connection.OwningProcess -gt 0) {
            return [int]$connection.OwningProcess
        }
    }
    catch {
        if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
            $savedPid = 0
            if ([int]::TryParse((Get-Content -LiteralPath $pidPath -Raw).Trim(), [ref]$savedPid)) {
                if ($null -ne (Get-Process -Id $savedPid -ErrorAction SilentlyContinue)) {
                    return $savedPid
                }
            }
        }
    }
    return $null
}

try {
    if (Test-WorkbenchService) {
        $existingPid = Get-WorkbenchServicePid
        if ($null -eq $existingPid) {
            throw 'The service is healthy, but its listening process ID could not be resolved.'
        }
        Write-BackgroundLog "Service already healthy on http://127.0.0.1:$port/ with pid $existingPid."
        [pscustomobject]@{
            health = 'healthy'
            pid     = $existingPid
            reused  = $true
            url     = "http://127.0.0.1:$port/"
        }
        exit 0
    }

    if (Test-LocalPort) {
        throw "Port $port is occupied by a process that did not return this project's health state."
    }

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

    Write-BackgroundLog "Starting: $runtimePython -m app.cli serve --port $port"
    Write-BackgroundLog "stdout=$stdoutLog"
    Write-BackgroundLog "stderr=$stderrLog"

    $process = Start-Process `
        -FilePath $runtimePython `
        -ArgumentList @('-m', 'app.cli', 'serve', '--port', "$port") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru


    $startupDeadline = [DateTime]::UtcNow.AddSeconds(20)
    $healthy = $false
    while ([DateTime]::UtcNow -lt $startupDeadline) {
        if ($process.HasExited) {
            throw "The background service exited before passing health checks. Exit code: $($process.ExitCode)."
        }
        if (Test-WorkbenchService) {
            $healthy = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not $healthy) {
        try {
            if (-not $process.HasExited) {
                $process.Kill()
                $process.WaitForExit()
            }
        }
        catch {
            Write-BackgroundLog ("Failed to stop unhealthy child process: {0}" -f $_.Exception.Message)
        }
        throw "The background service did not pass http://127.0.0.1:$port/api/health health checks within 20 seconds."
    }

    if ($process.HasExited) {
        throw "The background service exited immediately after passing health checks. Exit code: $($process.ExitCode)."
    }

    [System.IO.File]::WriteAllText($pidPath, [string]$process.Id, [System.Text.Encoding]::ASCII)
    Write-BackgroundLog "Healthy on http://127.0.0.1:$port/ with pid $($process.Id); launcher is returning."
    [pscustomobject]@{
        health = 'healthy'
        pid     = $process.Id
        reused  = $false
        url     = "http://127.0.0.1:$port/"
    }
    exit 0
}
catch {
    Write-BackgroundLog ("Failed: {0}" -f $_.Exception.Message)
    exit 1
}
