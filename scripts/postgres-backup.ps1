# Daily Postgres registry backup (pg_dump custom format).
#
# Reads credentials from DATABASE_URL in enrollment-api.env — do not hardcode passwords.
#
# Usage (manual — do not double-click the .ps1; use the .cmd or powershell -File):
#   .\scripts\postgres-backup.cmd
#   powershell -ExecutionPolicy Bypass -File .\scripts\postgres-backup.ps1
#
# Task Scheduler: point the action at postgres-backup.cmd (see
# docs/postgres-windows-server-setup.md §9).
param(
    [string]$EnvFile = "C:\kallon\config\enrollment-api.env",
    [string]$BackupDir = "C:\kallon\backups",
    [string]$PgDump = "C:\Program Files\PostgreSQL\16\bin\pg_dump.exe"
)

$ErrorActionPreference = "Stop"

function Write-Log([string]$Message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Write-Host $line
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
}

function Get-DatabaseUrlFromEnvFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Env file not found: $Path"
    }
    foreach ($raw in Get-Content -LiteralPath $Path) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        $eq = $line.IndexOf("=")
        if ($eq -lt 1) { continue }
        $name = $line.Substring(0, $eq).Trim()
        if ($name -ne "DATABASE_URL") { continue }
        return $line.Substring($eq + 1).Trim().Trim('"')
    }
    throw "DATABASE_URL not found in $Path"
}

function Parse-PostgresUrl([string]$Url) {
    if ($Url -notmatch '^postgresql://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/([^?]+)') {
        throw "DATABASE_URL is not a supported postgresql:// URL"
    }
    return @{
        User = [System.Uri]::UnescapeDataString($matches[1])
        Password = [System.Uri]::UnescapeDataString($matches[2])
        Host = $matches[3]
        Port = if ($matches[4]) { $matches[4] } else { "5432" }
        Database = [System.Uri]::UnescapeDataString($matches[5])
    }
}

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$logFile = Join-Path $BackupDir "backup.log"
$startLine = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') === backup run started (user=$env:USERNAME) ==="
Add-Content -LiteralPath $logFile -Value $startLine -Encoding UTF8

try {
    $dbUrl = Get-DatabaseUrlFromEnvFile -Path $EnvFile
    $db = Parse-PostgresUrl -Url $dbUrl

    if (-not (Test-Path -LiteralPath $PgDump)) {
        throw "pg_dump not found: $PgDump"
    }

    $outFile = Join-Path $BackupDir ("kallon_{0}.dump" -f (Get-Date -Format "yyyyMMdd"))
    Write-Log "Starting pg_dump -> $outFile"

    $env:PGPASSWORD = $db.Password
    & $PgDump -U $db.User -h $db.Host -p $db.Port -d $db.Database -Fc -f $outFile
    if ($LASTEXITCODE -ne 0) {
        throw "pg_dump exited with code $LASTEXITCODE"
    }

    $sizeMb = [math]::Round((Get-Item -LiteralPath $outFile).Length / 1MB, 2)
    Write-Log "Backup OK ($sizeMb MB)"
    exit 0
}
catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    exit 1
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}
