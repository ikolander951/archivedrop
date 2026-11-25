param([string]$Exe)
# Registers the "check the list every 15 minutes" background task for the current user (no admin needed).
$a = New-ScheduledTaskAction -Execute $Exe -Argument '--background' -WorkingDirectory (Split-Path $Exe)
$t1 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 15)
$t2 = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
     -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$p = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'ArchiveDrop' -Action $a -Trigger @($t1, $t2) -Settings $s -Principal $p -Force | Out-Null
