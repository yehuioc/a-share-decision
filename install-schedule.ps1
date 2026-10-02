[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [ValidateNotNullOrEmpty()]
    [ValidatePattern('^[^\\/:*?"<>|]+$')]
    [string]$TaskName = 'A-Share-Decision-Scheduled',

    [ValidateNotNullOrEmpty()]
    [ValidatePattern('^[^\\/:*?"<>|]+$')]
    [string]$BackgroundTaskName = 'A-Share-Decision-Background'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-BeijingTimeZone {
    $timeZone = [System.TimeZoneInfo]::Local
    if ($timeZone.Id -ne 'China Standard Time') {
        throw "The Windows time zone must be 'China Standard Time' (Beijing time). Current value: '$($timeZone.Id)'."
    }
}

function Get-NextDailyOccurrence {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateRange(0, 23)]
        [int]$Hour
    )

    $now = Get-Date
    $candidate = $now.Date.AddHours($Hour)
    if ($candidate -le $now) {
        $candidate = $candidate.AddDays(1)
    }
    return $candidate
}

function Resolve-AccountSid {
    param([Parameter(Mandatory = $true)][string]$AccountId)

    try {
        if ($AccountId -match '^S-\d(?:-\d+)+$') {
            return ([System.Security.Principal.SecurityIdentifier]$AccountId).Value
        }
        $account = New-Object System.Security.Principal.NTAccount($AccountId)
        return $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
    }
    catch {
        return $null
    }
}

function Write-ScheduleState {
    param(
        [Parameter(Mandatory = $true)][bool]$Installed,
        [Parameter(Mandatory = $true)][string]$Note
    )

    $automationRoot = Split-Path -Parent $metadataPath
    New-Item -ItemType Directory -Path $automationRoot -Force | Out-Null
    $state = [ordered]@{
        installed  = $Installed
        times      = @('13:00', '19:00')
        timezone   = 'Asia/Shanghai'
        task_names = @($TaskName, $BackgroundTaskName)
        note       = $Note
    }
    $json = ($state | ConvertTo-Json -Depth 4) + [Environment]::NewLine
    $temporaryPath = Join-Path $automationRoot ('.schedule-{0}-{1}.tmp' -f $PID, [Guid]::NewGuid().ToString('N'))
    try {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($temporaryPath, $json, $utf8NoBom)
        Move-Item -LiteralPath $temporaryPath -Destination $metadataPath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

Assert-BeijingTimeZone
Import-Module ScheduledTasks -ErrorAction Stop

$projectRoot = $PSScriptRoot
$scheduleRunnerPath = Join-Path $projectRoot 'schedule-run.ps1'
$backgroundRunnerPath = Join-Path $projectRoot 'start-background.ps1'
$metadataPath = Join-Path $projectRoot 'data\automation\schedule.json'
foreach ($requiredScript in @($scheduleRunnerPath, $backgroundRunnerPath)) {
    if (-not (Test-Path -LiteralPath $requiredScript -PathType Leaf)) {
        throw "Required task script is missing: $requiredScript"
    }
}

$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $windowsPowerShell -PathType Leaf)) {
    $windowsPowerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
}

function New-HiddenPowerShellAction {
    param([Parameter(Mandatory = $true)][string]$ScriptPath)

    $arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $ScriptPath.Replace('"', '""')
    return New-ScheduledTaskAction `
        -Execute $windowsPowerShell `
        -Argument $arguments `
        -WorkingDirectory $projectRoot
}

$scheduleAction = New-HiddenPowerShellAction -ScriptPath $scheduleRunnerPath
$backgroundAction = New-HiddenPowerShellAction -ScriptPath $backgroundRunnerPath

$scheduleTriggers = @(
    New-ScheduledTaskTrigger -Daily -At (Get-NextDailyOccurrence -Hour 13)
    New-ScheduledTaskTrigger -Daily -At (Get-NextDailyOccurrence -Hour 19)
)

$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$currentUser = $currentIdentity.Name
$currentSid = $currentIdentity.User.Value
$backgroundTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

$scheduleSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$backgroundSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval ([TimeSpan]::FromMinutes(1))

$scheduleDescription = 'A-share decision scheduled research at 13:00 and 19:00 Beijing time. Requires this Windows user to remain signed in.'
$backgroundDescription = 'A-share decision local web service on 127.0.0.1:8765. Starts at user logon and does not require Codex.'
$successNote = 'Windows 登录后自动启动页面，无需打开 Codex。定时研究需要电脑开机且该用户已登录，锁屏可运行；关机期间不能运行，重新开机并登录后补跑最近时段。睡眠唤醒受硬件和电源策略限制。'
$registrationPerformed = $false

if ($PSCmdlet.ShouldProcess("Scheduled tasks '$TaskName' and '$BackgroundTaskName' for $currentUser", 'Register or update')) {
    Write-ScheduleState -Installed $false -Note 'Windows task installation is in progress.'
    try {
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $scheduleAction `
            -Trigger $scheduleTriggers `
            -Principal $principal `
            -Settings $scheduleSettings `
            -Description $scheduleDescription `
            -Force | Out-Null

        Register-ScheduledTask `
            -TaskName $BackgroundTaskName `
            -Action $backgroundAction `
            -Trigger $backgroundTrigger `
            -Principal $principal `
            -Settings $backgroundSettings `
            -Description $backgroundDescription `
            -Force | Out-Null

        foreach ($registeredTaskName in @($TaskName, $BackgroundTaskName)) {
            $registeredTask = Get-ScheduledTask -TaskName $registeredTaskName -ErrorAction Stop
            $registeredSid = Resolve-AccountSid -AccountId ([string]$registeredTask.Principal.UserId)
            if ($registeredSid -ne $currentSid) {
                throw "Task '$registeredTaskName' is not assigned to the current user '$currentUser'."
            }
        }

        Write-ScheduleState -Installed $true -Note $successNote
        $registrationPerformed = $true
    }
    catch {
        try {
            Write-ScheduleState -Installed $false -Note 'Windows task installation failed. Review the PowerShell error and retry.'
        }
        catch {
            Write-Warning 'Task installation failed and schedule.json could not be updated.'
        }
        throw
    }
}

[pscustomobject]@{
    Installed           = $registrationPerformed
    TaskNames           = @($TaskName, $BackgroundTaskName)
    User                = $currentUser
    UserSid             = $currentSid
    TimeZone            = 'Asia/Shanghai (Windows: China Standard Time)'
    DailyTimes          = @('13:00', '19:00')
    ScheduleRunner      = $scheduleRunnerPath
    BackgroundRunner    = $backgroundRunnerPath
    Metadata            = $metadataPath
    MultipleInstances   = 'IgnoreNew'
    ExecutionTimeLimit  = 'Unlimited (PT0S)'
    StartWhenAvailable  = $true
    LoginRequirement    = 'The Windows user must remain signed in; a locked session is acceptable.'
    PowerRequirement    = 'A powered-off PC cannot run tasks. Wake depends on hardware and Windows power policy.'
}
