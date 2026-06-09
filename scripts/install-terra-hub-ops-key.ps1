# Install the Terra hub-ops SSH key on the Windows control plane.
#
# Copies your existing Lightsail PEM to C:\kallon\secrets\terra-hub-ops.pem,
# fixes ACLs for OpenSSH, derives terra-hub-ops.pub, and smoke-tests SSH.
#
# Usage:
#   .\scripts\install-terra-hub-ops-key.ps1 -SourcePem "C:\path\to\kallon-vps-key.pem"
#   .\scripts\install-terra-hub-ops-key.ps1 -SourcePem "..." -HubHost 18.220.75.237
param(
    [Parameter(Mandatory = $true)]
    [string]$SourcePem,

    [string]$DestDir = "C:\kallon\secrets",
    [string]$HubHost = "18.220.75.237",
    [string]$SshUser = "ubuntu"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $SourcePem)) {
    throw "Source PEM not found: $SourcePem"
}

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null

$destPem = Join-Path $DestDir "terra-hub-ops.pem"
$destPub = Join-Path $DestDir "terra-hub-ops.pub"

# Copy before tightening ACLs (re-run safe: unlock destination if we locked it earlier).
if (Test-Path -LiteralPath $destPem) {
    icacls $destPem /grant:r "${env:USERNAME}:(F)" 2>$null | Out-Null
}
Copy-Item -LiteralPath $SourcePem -Destination $destPem -Force

# OpenSSH on Windows rejects private keys readable by Administrators / SYSTEM / inherited ACLs.
icacls $DestDir /inheritance:r | Out-Null
icacls $DestDir /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null
icacls $DestPem /inheritance:r | Out-Null
icacls $DestPem /grant:r "${env:USERNAME}:(R)" | Out-Null
icacls $DestPem /remove "NT AUTHORITY\SYSTEM" "BUILTIN\Administrators" "Everyone" 2>$null | Out-Null

# Avoid Out-File BOM / CRLF quirks; use cmd redirect for a clean single-line pubkey.
cmd /c "ssh-keygen -y -f `"$destPem`" > `"$destPub`""

if (-not (Test-Path -LiteralPath $destPub) -or (Get-Item $destPub).Length -lt 20) {
    throw "Failed to derive public key at $destPub"
}

Write-Host ""
Write-Host "Files:"
Write-Host "  PEM: $destPem"
Write-Host "  PUB: $destPub"
Write-Host ""
Write-Host "Set for this session (hub-provisioner + enrollment API):"
Write-Host "  `$env:KALLON_OPS_SSH_IDENTITY_FILE = `"$destPem`""
Write-Host "  `$env:KALLON_OPS_SSH_PUBKEY_FILE = `"$destPub`""
Write-Host ""

Write-Host "Smoke test (IdentitiesOnly=yes - same flags Python uses):"
ssh -i $destPem -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes `
    "${SshUser}@${HubHost}" "sudo wg show wg0 public-key"

if ($LASTEXITCODE -ne 0) {
    throw "SSH smoke test failed. If interactive ssh also fails, check ~/.ssh/config and remove Host * IdentityFile until this test passes."
}

Write-Host ""
Write-Host "OK - terra-hub-ops key installed and verified."
