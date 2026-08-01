#Requires -Version 5.1
Unregister-ScheduledTask -TaskName "InferenceGate" -Confirm:$false -ErrorAction Stop
Write-Host "Scheduled task 'InferenceGate' removed."
