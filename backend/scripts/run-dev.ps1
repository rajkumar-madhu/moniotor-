# Start LinkedEye Django dev server (loads .env automatically via settings.py)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (Test-Path ".\docker\vault-django.env.ps1") {
    . .\docker\vault-django.env.ps1
}

$bindHost = if ($env:DJANGO_BIND_HOST) { $env:DJANGO_BIND_HOST } else { "0.0.0.0" }
$bindPort = if ($env:DJANGO_PORT) { $env:DJANGO_PORT } else { "8000" }
if (-not $env:PORTAL_URL) {
    $env:PORTAL_URL = "http://127.0.0.1:$bindPort"
}
Write-Host "Running migrations..."
python manage.py migrate --noinput
Write-Host "Starting Django on http://127.0.0.1:$bindPort ..."
python manage.py runserver "$bindHost`:$bindPort"
