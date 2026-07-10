# Load Kallon control-plane env vars from enrollment-api.env into this PowerShell session.
#
# Usage:
#   . .\scripts\load-control-plane.ps1
#   . .\scripts\load-control-plane.ps1 -EnvFile C:\kallon\config\enrollment-api.env
param(
    [string]$EnvFile = "C:\kallon\config\enrollment-api.env"
)

if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Env file not found: $EnvFile"
}

Get-Content -LiteralPath $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $eq = $line.IndexOf("=")
    if ($eq -lt 1) { return }
    $name = $line.Substring(0, $eq).Trim()
    $value = $line.Substring($eq + 1).Trim().Trim('"')
    Set-Item -Path "Env:$name" -Value $value
}

Write-Host "Loaded control-plane env from $EnvFile"
