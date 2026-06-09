# Diagnose Terra hub-ops SSH from the Windows control plane.
#
# Usage:
#   .\scripts\kallon-hub-ssh-verify.ps1
#   .\scripts\kallon-hub-ssh-verify.ps1 -HubHost 18.220.75.237
param(
    [string]$HubHost = "18.220.75.237",
    [string]$SshUser = "ubuntu",
    [string]$IdentityFile = $env:KALLON_OPS_SSH_IDENTITY_FILE
)

$ErrorActionPreference = "Continue"

function Show-Acl([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        Write-Host "  ACL: $Path"
        icacls $Path
    } else {
        Write-Host "  MISSING: $Path"
    }
}

Write-Host "=== Kallon hub SSH verify ==="
Write-Host "Hub: ${SshUser}@${HubHost}"
Write-Host ""

Write-Host "Environment:"
Write-Host "  KALLON_OPS_SSH_IDENTITY_FILE = $($env:KALLON_OPS_SSH_IDENTITY_FILE)"
Write-Host "  KALLON_OPS_SSH_PUBKEY_FILE    = $($env:KALLON_OPS_SSH_PUBKEY_FILE)"
Write-Host ""

$defaultPem = "C:\kallon\secrets\terra-hub-ops.pem"
if (-not $IdentityFile) { $IdentityFile = $defaultPem }

Write-Host "Identity file:"
Show-Acl $IdentityFile
if ($IdentityFile -ne $defaultPem) { Show-Acl $defaultPem }
Write-Host ""

$configPath = Join-Path $env:USERPROFILE ".ssh\config"
if (Test-Path $configPath) {
    Write-Host "~/.ssh/config (check for 'Host *' forcing a broken IdentityFile):"
    Get-Content $configPath | ForEach-Object { Write-Host "  $_" }
    Write-Host ""
}

Write-Host "Test 1: explicit -i + IdentitiesOnly=yes (what hub-provisioner uses):"
ssh -i $IdentityFile -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes `
    "${SshUser}@${HubHost}" "echo ok-explicit"
Write-Host "  exit: $LASTEXITCODE"
Write-Host ""

Write-Host "Test 2: plain ssh (uses ~/.ssh/config if present):"
ssh -o BatchMode=yes "${SshUser}@${HubHost}" "echo ok-config"
Write-Host "  exit: $LASTEXITCODE"
Write-Host ""

if ($LASTEXITCODE -ne 0) {
    Write-Host "Recovery:"
    Write-Host "  1. Run: .\scripts\install-terra-hub-ops-key.ps1 -SourcePem `"<your kallon-vps-key.pem>`""
    Write-Host "  2. Set KALLON_OPS_SSH_IDENTITY_FILE to the .pem (not just .pub)"
    Write-Host "  3. Remove or narrow Host * in ~/.ssh/config; use a host alias instead:"
    Write-Host "       Host kallon-hub-*"
    Write-Host "         User ubuntu"
    Write-Host "         IdentityFile C:\kallon\secrets\terra-hub-ops.pem"
    Write-Host "         IdentitiesOnly yes"
}
