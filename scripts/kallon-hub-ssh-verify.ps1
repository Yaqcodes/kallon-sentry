# Diagnose Terra hub-ops SSH from the Windows control plane.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\kallon-hub-ssh-verify.ps1 -HubHost 18.220.75.237
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
    Write-Host "~/.ssh/config:"
    Get-Content $configPath | ForEach-Object { Write-Host "  $_" }
    Write-Host ""
}

Write-Host "Test 1: explicit -i + IdentitiesOnly=yes (hub-provisioner + enrollment API):"
ssh -i $IdentityFile -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes `
    "${SshUser}@${HubHost}" "echo ok-explicit"
$test1 = $LASTEXITCODE
Write-Host "  exit: $test1"
Write-Host ""

Write-Host "Test 2: plain ssh (optional; uses ~/.ssh/config only):"
ssh -o BatchMode=yes "${SshUser}@${HubHost}" "echo ok-config"
$test2 = $LASTEXITCODE
Write-Host "  exit: $test2"
Write-Host ""

if ($test1 -eq 0) {
    Write-Host "PASS: Test 1 succeeded - you can run hub-provision and enrollment API peer-add."
    if ($test2 -ne 0) {
        Write-Host "NOTE: Test 2 failed - plain 'ssh ubuntu@...' is broken by ~/.ssh/config."
        Write-Host "      This does NOT block hub-provisioner. To fix interactive ssh:"
        Write-Host "        Remove 'Host *' IdentityFile from $configPath"
        Write-Host "        Or use: ssh -i $IdentityFile -o IdentitiesOnly=yes ${SshUser}@${HubHost}"
    }
} else {
    Write-Host "FAIL: Test 1 failed - hub-provisioner will not work until this passes."
    Write-Host "Recovery:"
    Write-Host "  1. Install from ORIGINAL key (not terra-hub-ops.pem):"
    Write-Host "       powershell -ExecutionPolicy Bypass -File .\scripts\install-terra-hub-ops-key.ps1 -SourcePem `"C:\path\to\kallon-vps-key.pem`""
    Write-Host "  2. Or repair existing: ... install-terra-hub-ops-key.ps1 -Repair"
    Write-Host "  3. Set `$env:KALLON_OPS_SSH_IDENTITY_FILE to the .pem (not .pub)"
}
