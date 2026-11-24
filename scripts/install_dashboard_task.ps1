# Registra el dashboard como tarea de Windows que arranca con el equipo, sin iniciar sesión.
# Uso: desde PowerShell COMO ADMINISTRADOR, parado en la raíz del proyecto:
#   powershell -ExecutionPolicy Bypass -File scripts\install_dashboard_task.ps1
# Antes hay que configurar el login: .venv\Scripts\python.exe scripts\set_dashboard_password.py
# Desinstalar: Unregister-ScheduledTask -TaskName "priceCompare-dashboard" -Confirm:$false

param(
    [string]$TaskName = "priceCompare-dashboard",
    [int]$WatchdogMinutes = 5
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Ejecuta este script en PowerShell como administrador: el disparador 'al iniciar el equipo' lo exige."
    exit 1
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "No se encontró $venvPython. Primero crea el venv: python -m venv .venv"
    exit 1
}

$configPath = Join-Path $projectRoot "config.json"
$dashboardConfig = $null
if (Test-Path $configPath) {
    $dashboardConfig = (Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json).dashboard
}
if ($null -eq $dashboardConfig -or -not $dashboardConfig.password_hash) {
    Write-Error "Falta configurar el login: .venv\Scripts\python.exe scripts\set_dashboard_password.py"
    exit 1
}
$port = 8000
if ($dashboardConfig.port) { $port = $dashboardConfig.port }

$action = New-ScheduledTaskAction `
    -Execute $venvPython `
    -Argument "-m src.dashboard" `
    -WorkingDirectory $projectRoot

# Arranca con el equipo; la repetición relanza el dashboard si el proceso murió.
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $WatchdogMinutes)

# Sin límite de ejecución (el valor por defecto de 72 h detendría el servidor).
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd

# S4U: corre con tu usuario aunque nadie haya iniciado sesión en el mini PC.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($startupTrigger, $watchdogTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description "Dashboard web de priceCompare, siempre activo" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host "Tarea '$TaskName' registrada e iniciada."
Write-Host "Arranca con el equipo y se revisa cada $WatchdogMinutes minutos por si el proceso se detuvo."
Write-Host "Comprobar: http://127.0.0.1:$port/login  |  Logs: $projectRoot\logs\dashboard.log"
Write-Host "Detener: Stop-ScheduledTask -TaskName '$TaskName'  |  Iniciar: Start-ScheduledTask -TaskName '$TaskName'"
