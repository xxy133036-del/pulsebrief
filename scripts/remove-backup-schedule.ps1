$ErrorActionPreference = "Stop"
$taskName = "PulseBrief PostgreSQL Backup"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Output "Removed '$taskName'. Existing backup files were kept."
}
else {
    Write-Output "'$taskName' is not installed."
}
