"""Hub peer-add hook for the enrollment API.

When a tower enrolls, its WireGuard public key must become a peer on the
customer hub. This module isolates *how* that happens so the API stays testable:

  * subprocess (production, DEFAULT): runs kallon-gateway-add-peer.sh against
    the customer's gateway host. By default it builds an argv LIST and runs
    bash directly (shell=False), which sidesteps every shell-quoting pitfall
    (spaces in paths, cmd.exe-vs-bash quoting, stray characters in a key) and
    works identically on Windows (Git Bash) and Linux. If KALLON_ADDPEER_CMD
    is set, that template is honored instead via a shell. Retries on transient
    failure (SSH/network blips) so a momentary hub hiccup does not strand a
    tower.
  * noop (tests / explicit lab opt-in only): record-only; operator adds peers
    manually. NEVER the implicit default — must be set explicitly via
    KALLON_PEER_BACKEND=noop, and every use logs at ERROR so a forgotten
    lab setting is impossible to miss in production logs.

Selected by KALLON_PEER_BACKEND = subprocess | noop. Defaults to subprocess:
a production enrollment API with no peer-add configured should fail loudly
(missing script / SSH key / bash) rather than silently pretend to succeed.
"""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

log = logging.getLogger("enrollment.peering")

# Resolve the repo root the same way main.py does, so the default add-peer
# command works no matter what the process's working directory is (a common
# footgun: NSSM/systemd often set AppDirectory/WorkingDirectory to
# infra/enrollment-api, not the repo root, which breaks a bare relative path).
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ADDPEER_SCRIPT = _REPO_ROOT / "scripts" / "kallon-gateway-add-peer.sh"

# Common Git-for-Windows bash location, used only if `bash` is not on PATH.
_WIN_GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"

PEER_ADD_RETRIES = int(os.environ.get("KALLON_PEER_ADD_RETRIES", "3"))
PEER_ADD_RETRY_BACKOFF_SEC = float(os.environ.get("KALLON_PEER_ADD_RETRY_BACKOFF_SEC", "3"))


class PeerAdder(Protocol):
    def add_peer(self, *, gateway_host: str, pubkey: str, vpn_ip: str, device_id: str) -> None: ...


def find_bash() -> str | None:
    """Locate a bash interpreter to run the (POSIX) add-peer script.

    On Windows the script is a .sh and MUST be run through Git Bash — cmd.exe
    cannot execute it. On Linux bash is virtually always present.
    """
    found = shutil.which("bash")
    if found:
        return found
    if os.name == "nt" and os.path.isfile(_WIN_GIT_BASH):
        return _WIN_GIT_BASH
    return None


def _run_with_retries(cmd, *, shell: bool, device_id: str, describe: str) -> None:
    """Run cmd (argv list or shell string) with a few retries + backoff.

    Raises RuntimeError with the last stderr/stdout on persistent failure.
    """
    last_err = ""
    for attempt in range(1, PEER_ADD_RETRIES + 1):
        log.info("add_peer attempt %d/%d: %s", attempt, PEER_ADD_RETRIES, describe)
        try:
            res = subprocess.run(
                cmd, shell=shell, capture_output=True, text=True, timeout=60
            )
        except OSError as exc:
            # e.g. bash not found / not executable — no point retrying.
            raise RuntimeError(f"could not launch add-peer for device={device_id}: {exc}") from exc
        if res.returncode == 0:
            if attempt > 1:
                log.info("add_peer succeeded on attempt %d for device=%s", attempt, device_id)
            return
        last_err = res.stderr.strip() or res.stdout.strip()
        log.warning(
            "add_peer attempt %d/%d failed (rc=%d) device=%s: %s",
            attempt, PEER_ADD_RETRIES, res.returncode, device_id, last_err,
        )
        if attempt < PEER_ADD_RETRIES:
            time.sleep(PEER_ADD_RETRY_BACKOFF_SEC * attempt)
    raise RuntimeError(
        f"add-peer failed after {PEER_ADD_RETRIES} attempts for device={device_id}: {last_err}"
    )


class NoopPeerAdder:
    """Records the intent only — a human / Option C runbook adds the peer.

    Only reachable via an EXPLICIT KALLON_PEER_BACKEND=noop (tests and
    deliberate lab opt-in). Logs at ERROR, not WARNING, so this can never be
    mistaken for normal production behavior in the logs.
    """

    def add_peer(self, *, gateway_host: str, pubkey: str, vpn_ip: str, device_id: str) -> None:
        log.error(
            "NOOP peer add (KALLON_PEER_BACKEND=noop): device=%s vpn_ip=%s host=%s "
            "- the hub was NOT updated. Add the peer manually via "
            "kallon-gateway-add-peer.sh, or set KALLON_PEER_BACKEND=subprocess "
            "for automatic peer-add.", device_id, vpn_ip, gateway_host,
        )


class ArgvPeerAdder:
    """Default backend: build an argv list and run it directly (shell=False).

    No shell means no quoting ambiguity — a key with odd characters, a path
    with spaces, or the cmd.exe-vs-bash single/double-quote mismatch on Windows
    all become non-issues. `prefix` is e.g. [bash, script] on Windows/Linux.
    """

    def __init__(self, prefix: list[str]) -> None:
        self._prefix = prefix

    def add_peer(self, *, gateway_host: str, pubkey: str, vpn_ip: str, device_id: str) -> None:
        argv = self._prefix + [
            "--gateway-host", gateway_host,
            "--pubkey", pubkey,
            "--vpn-ip", f"{vpn_ip}/32",
            "--device-id", device_id,
        ]
        _run_with_retries(argv, shell=False, device_id=device_id, describe=" ".join(argv))


class SubprocessPeerAdder:
    """Honors an explicit KALLON_ADDPEER_CMD template via a shell.

    The script itself is idempotent. Retries transient failures. Callers who
    set KALLON_ADDPEER_CMD own their own quoting (documented per-platform).
    """

    def __init__(self, cmd_template: str) -> None:
        # Template supports {gateway_host} {pubkey} {vpn_ip} {device_id}.
        self._tpl = cmd_template

    def add_peer(self, *, gateway_host: str, pubkey: str, vpn_ip: str, device_id: str) -> None:
        cmd = self._tpl.format(
            gateway_host=shlex.quote(gateway_host),
            pubkey=shlex.quote(pubkey),
            vpn_ip=shlex.quote(f"{vpn_ip}/32"),
            device_id=shlex.quote(device_id),
        )
        _run_with_retries(cmd, shell=True, device_id=device_id, describe=cmd)


def get_peer_adder() -> PeerAdder:
    backend = os.environ.get("KALLON_PEER_BACKEND", "subprocess").lower()
    if backend == "noop":
        return NoopPeerAdder()
    if backend != "subprocess":
        log.warning("unknown KALLON_PEER_BACKEND=%r; defaulting to subprocess", backend)

    tpl = os.environ.get("KALLON_ADDPEER_CMD")
    if tpl:
        return SubprocessPeerAdder(tpl)

    # Default (no template): run the tracked script via bash with a real argv.
    bash = find_bash()
    if bash:
        return ArgvPeerAdder([bash, str(DEFAULT_ADDPEER_SCRIPT)])
    # Last resort: rely on the script's own shebang (POSIX only). On Windows
    # this will fail loudly, which startup_check() already warns about.
    log.error(
        "no bash interpreter found (install Git Bash on Windows, or set "
        "KALLON_ADDPEER_CMD); attempting direct script execution"
    )
    return ArgvPeerAdder([str(DEFAULT_ADDPEER_SCRIPT)])


def startup_check() -> None:
    """Fail loudly at process startup, not silently on the first real tower.

    Called once from main.py's startup hook. Never raises — logs everything a
    human needs to fix a misconfigured production deploy before it strands a
    tower in the field.
    """
    backend = os.environ.get("KALLON_PEER_BACKEND", "subprocess").lower()
    if backend == "noop":
        log.error(
            "KALLON_PEER_BACKEND=noop — automatic hub peer-add is DISABLED. "
            "This must never be set in production; every enrollment will require "
            "a manual kallon-gateway-add-peer.sh run on the hub."
        )
        return

    tpl = os.environ.get("KALLON_ADDPEER_CMD", "")
    script_path = DEFAULT_ADDPEER_SCRIPT

    if tpl:
        log.info("peer-add backend=subprocess ready (KALLON_ADDPEER_CMD template set)")
    else:
        if not script_path.is_file():
            log.error(
                "peer-add misconfigured: default script not found at %s and "
                "KALLON_ADDPEER_CMD is unset. Auto peer-add WILL fail on every "
                "enrollment until this is fixed.", script_path,
            )
        bash = find_bash()
        if not bash:
            log.error(
                "no bash interpreter found — the add-peer script (%s) cannot run. "
                "On Windows install Git Bash (or set KALLON_ADDPEER_CMD). Auto "
                "peer-add WILL fail on every enrollment until this is fixed.",
                script_path,
            )
        else:
            log.info(
                "peer-add backend=subprocess ready (bash=%s script=%s)", bash, script_path
            )

    identity_file = os.environ.get("KALLON_OPS_SSH_IDENTITY_FILE", "")
    if not identity_file:
        log.warning(
            "KALLON_OPS_SSH_IDENTITY_FILE is unset — SSH to hubs will fall back "
            "to the default identity/agent, which usually fails for a service "
            "account. Set it explicitly for reliable unattended peer-add."
        )
    elif not Path(identity_file).is_file():
        log.error(
            "KALLON_OPS_SSH_IDENTITY_FILE=%s does not exist — auto peer-add WILL "
            "fail on every enrollment until this is fixed.", identity_file,
        )
