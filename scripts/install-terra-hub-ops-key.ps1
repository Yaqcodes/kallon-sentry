# Install the Terra hub-ops SSH key on the Windows control plane.
#
# Copies your existing Lightsail PEM to C:\kallon\secrets\terra-hub-ops.pem,
# fixes ACLs for OpenSSH, derives terra-hub-ops.pub, and smoke-tests SSH.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\install-terra-hub-ops-key.ps1 `
#     -SourcePem "C:\path\to\kallon-vps-key.pem"
#
# Re-run on an existing install (fix ACLs + .pub only; no copy):
#   powershell -ExecutionPolicy Bypass -File .\scripts\install-terra-hub-ops-key.ps1 -Repair
param(
    [string]$SourcePem = "",

    [switch]$Repair,

    [string]$DestDir = "C:\kallon\secrets",
    [string]$HubHost = "18.220.75.237",
    [string]$SshUser = "ubuntu"
)

$ErrorActionPreference = "Stop"

function Resolve-FullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path)
}

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null

$destPem = Join-Path $DestDir "terra-hub-ops.pem"
$destPub = Join-Path $DestDir "terra-hub-ops.pub"
$destPemFull = Resolve-FullPath $destPem

if ($Repair) {
    if (-not (Test-Path -LiteralPath $destPemFull)) {
        throw "-Repair requires existing $destPemFull. Pass -SourcePem with your original kallon-vps-key.pem."
    }
    Write-Host "Repair mode: fixing ACLs and re-deriving .pub at $destPemFull"
} else {
    if (-not $SourcePem) {
        throw "-SourcePem is required. Use your original kallon-vps-key.pem (NOT $destPemFull). Or -Repair if already installed."
    }
    if (-not (Test-Path -LiteralPath $SourcePem)) {
        throw "Source PEM not found: $SourcePem"
    }

    $sourceFull = Resolve-FullPath $SourcePem

    if ($sourceFull -ieq $destPemFull) {
        Write-Host "Source equals destination - treating as repair (use -Repair next time)."
    } else {
        if (Test-Path -LiteralPath $destPemFull) {
            icacls $destPemFull /grant:r "${env:USERNAME}:(F)" 2>$null | Out-Null
        }
        Copy-Item -LiteralPath $SourcePem -Destination $destPemFull -Force
        Write-Host "Copied $SourcePem -> $destPemFull"
    }
}

# OpenSSH on Windows rejects private keys readable by Administrators / SYSTEM / inherited ACLs.
icacls $DestDir /inheritance:r | Out-Null
icacls $DestDir /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null
icacls $destPemFull /inheritance:r | Out-Null
icacls $destPemFull /grant:r "${env:USERNAME}:(R)" | Out-Null
icacls $destPemFull /remove "NT AUTHORITY\SYSTEM" "BUILTIN\Administrators" "Everyone" 2>$null | Out-Null

cmd /c "ssh-keygen -y -f `"$destPemFull`" > `"$destPub`""

if (-not (Test-Path -LiteralPath $destPub) -or (Get-Item $destPub).Length -lt 20) {
    throw "Failed to derive public key at $destPub"
}

Write-Host ""
Write-Host "Files:"
Write-Host "  PEM: $destPemFull"
Write-Host "  PUB: $destPub"
Write-Host ""
Write-Host "Set for this session (hub-provisioner + enrollment API):"
Write-Host "  `$env:KALLON_OPS_SSH_IDENTITY_FILE = `"$destPemFull`""
Write-Host "  `$env:KALLON_OPS_SSH_PUBKEY_FILE = `"$destPub`""
Write-Host ""

Write-Host "Smoke test (IdentitiesOnly=yes - same flags hub-provisioner uses):"
ssh -i $destPemFull -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes `
    "${SshUser}@${HubHost}" "sudo wg show wg0 public-key"

if ($LASTEXITCODE -ne 0) {
    throw "SSH smoke test failed."
}

Write-Host ""
Write-Host "OK - terra-hub-ops key ready. Hub-provisioner will use explicit -i (Test 1 in kallon-hub-ssh-verify.ps1)."
