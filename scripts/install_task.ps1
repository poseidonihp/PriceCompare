# Registra la tarea diaria en Windows Task Scheduler.
# Uso: desde PowerShell, parado en la raíz del proyecto:
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
# Por defecto corre todos los días a las 09:00. Cambia $triggerTime para ajustar.

param(
    [string]$TaskName = "priceCompare",
    [string]$TriggerTime = "09:00"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Error "No se encontró $venvPython. Primero crea el venv: python -m venv .venv"
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute $venvPython `
    -Argument "-m src.main" `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $TriggerTime

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -RunOnlyIfNetworkAvailable `
    -WakeToRun

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Comparador diario de precios" `
    -Force

Write-Host "Tarea '$TaskName' registrada. Correrá todos los días a las $TriggerTime."
Write-Host "Para ejecutarla manualmente ahora: Start-ScheduledTask -TaskName '$TaskName'"
