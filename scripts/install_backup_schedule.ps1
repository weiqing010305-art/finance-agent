param(
    [string]$TaskName = "FinScope-Hourly-Encrypted-Backup"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackupScript = Join-Path $PSScriptRoot "backup_formal.ps1"
if (-not (Test-Path -LiteralPath $BackupScript -PathType Leaf)) { throw "backup script is missing" }

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$BackupScript`" create"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -Once -At ([DateTime]::Now.AddMinutes(5)) -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Hourly encrypted PostgreSQL and MinIO backup for the local FinScope demo" -Force | Out-Null
Write-Host "Scheduled task installed: $TaskName"
