# Instala el worker de Contenido automatizado en una PC con Windows.
# Ejecutar en PowerShell, con el usuario que va a quedar logueado en Claude Code:
#   powershell -ExecutionPolicy Bypass -File .\instalar_worker_windows.ps1
# Requisitos previos: Python 3 (python.org, con "Add to PATH") y Claude Code:
#   irm https://claude.ai/install.ps1 | iex      y después   claude   (para loguearte)
$ErrorActionPreference = 'Stop'
$aqui = Split-Path -Parent $MyInvocation.MyCommand.Path

$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { throw 'No encontré Python (pythonw.exe). Instalalo desde python.org con "Add python.exe to PATH".' }
$claude = (Get-Command claude.exe -ErrorAction SilentlyContinue).Source
if (-not $claude) { $claude = Join-Path $env:USERPROFILE '.local\bin\claude.exe' }
if (-not (Test-Path $claude)) { throw 'No encontré Claude Code. Instalalo con: irm https://claude.ai/install.ps1 | iex' }

$env_file = Join-Path $aqui 'claude_worker.env'
if (-not (Test-Path $env_file)) {
    $url  = Read-Host 'URL de Odoo [https://odoo.aftermoves.com]'; if (-not $url) { $url = 'https://odoo.aftermoves.com' }
    $db   = Read-Host 'Base de datos [Aftermoves_Produccion]';    if (-not $db)  { $db = 'Aftermoves_Produccion' }
    $user = Read-Host 'Usuario de Odoo'
    $key  = Read-Host 'API key de Odoo (Preferencias > Seguridad de la cuenta)'
    @(
        "AC_ODOO_URL=$url", "AC_ODOO_DB=$db", "AC_ODOO_USER=$user", "AC_ODOO_KEY=$key",
        'AC_INTERVAL=30', "AC_CLAUDE=$claude", "AC_LOG=$(Join-Path $aqui 'claude_worker.log')"
    ) | Set-Content -Encoding UTF8 $env_file
    Write-Host "Configuración guardada en $env_file"
}

Write-Host 'Prueba: proceso lo pendiente una vez...'
& ($python -replace 'pythonw.exe$', 'python.exe') (Join-Path $aqui 'claude_worker.py') --once
if ($LASTEXITCODE -ne 0) { throw 'La prueba falló: revisá claude_worker.log.' }

# Tarea programada: arranca al iniciar sesión este usuario y se reinicia si se cae.
$accion  = New-ScheduledTaskAction -Execute $python -Argument "`"$(Join-Path $aqui 'claude_worker.py')`"" -WorkingDirectory $aqui
$disparo = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$ajustes = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
           -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'Contenido automatizado - worker' -Action $accion -Trigger $disparo `
    -Settings $ajustes -Description 'Resuelve con Claude Code las solicitudes de Odoo en modo Claude Code' -Force | Out-Null
Start-ScheduledTask -TaskName 'Contenido automatizado - worker'
Write-Host 'Listo: el worker queda corriendo y arranca solo cada vez que inicia sesión este usuario.'
Write-Host 'Para que arranque sin intervención después de un corte de luz, activá el inicio de sesión automático de Windows.'
