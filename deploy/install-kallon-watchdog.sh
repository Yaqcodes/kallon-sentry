#!/usr/bin/env bash
# install-kallon-watchdog.sh
#
# Installs the Kallon health & tamper watchdog on a Jetson Orin Nano.
# Idempotent: safe to re-run after editing /etc/kallon/device.env.
#
# Steps:
#   1. Verify the repo layout (working directory must contain kallon_watchdog.py).
#   2. Add the runtime user to the gpio and i2c groups.
#   3. Install Python dependencies from requirements.txt.
#   4. Create /etc/kallon, write a template device.env and alert.key if missing.
#   5. Install the systemd unit and (optionally) enable it.
#
# Run as root on the Jetson:
#
#   sudo deploy/install-kallon-watchdog.sh
#
# After install, edit /etc/kallon/device.env and replace /etc/kallon/alert.key,
# then: sudo systemctl restart kallon-watchdog && journalctl -u kallon-watchdog -f

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/kallon}"
RUNTIME_USER="${RUNTIME_USER:-khalifa}"
CONFIG_DIR="/etc/kallon"
ENV_FILE="${CONFIG_DIR}/device.env"
KEY_FILE="${CONFIG_DIR}/alert.key"
UNIT_SRC="${REPO_DIR}/deploy/kallon-watchdog.service.example"
UNIT_DST="/etc/systemd/system/kallon-watchdog.service"

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must be run as root (use sudo)." >&2
    exit 1
  fi
}

verify_repo() {
  if [[ ! -f "${REPO_DIR}/kallon_watchdog.py" ]]; then
    echo "ERROR: ${REPO_DIR}/kallon_watchdog.py not found." >&2
    echo "       Set REPO_DIR=/path/to/repo if your checkout is elsewhere." >&2
    exit 1
  fi
  if [[ ! -f "${UNIT_SRC}" ]]; then
    echo "ERROR: ${UNIT_SRC} not found." >&2
    exit 1
  fi
}

ensure_groups() {
  if ! id -u "${RUNTIME_USER}" >/dev/null 2>&1; then
    echo "ERROR: user ${RUNTIME_USER} does not exist." >&2
    exit 1
  fi
  for grp in gpio i2c; do
    if getent group "${grp}" >/dev/null; then
      if ! id -nG "${RUNTIME_USER}" | tr ' ' '\n' | grep -qx "${grp}"; then
        echo "Adding ${RUNTIME_USER} to ${grp} group."
        usermod -aG "${grp}" "${RUNTIME_USER}"
      fi
    else
      echo "WARN: group ${grp} does not exist on this system; skipping."
    fi
  done
}

install_python_deps() {
  if [[ -f "${REPO_DIR}/requirements.txt" ]]; then
    echo "Installing Python dependencies from requirements.txt."
    sudo -u "${RUNTIME_USER}" pip3 install --user -r "${REPO_DIR}/requirements.txt"
  else
    echo "WARN: ${REPO_DIR}/requirements.txt not found; skipping pip install."
  fi
}

create_config_dir() {
  install -d -m 0750 -o root -g "${RUNTIME_USER}" "${CONFIG_DIR}"
}

write_env_template() {
  if [[ -f "${ENV_FILE}" ]]; then
    echo "Keeping existing ${ENV_FILE}."
    return
  fi
  cat > "${ENV_FILE}" <<'EOF'
# /etc/kallon/device.env
# Per-device configuration consumed by kallon-watchdog.service.
# Edit the values below; restart the service after any change.

DEVICE_ID=kallon-unit-001
ALERT_WEBHOOK_URL=http://10.50.0.1:8080/alerts
ALERT_KEY_PATH=/etc/kallon/alert.key

# Comma-separated. Use the local mediamtx URL once it is up; one camera for now.
RTSP_URLS=rtsp://127.0.0.1:8554/cam1

# Tuning (defaults shown).
POLL_INTERVAL_SEC=10
TEMP_TRIGGER_C=80
TEMP_CLEAR_C=75
DEDUP_WINDOW_SEC=60

# Hardware wiring (Jetson Orin Nano J12, BOARD numbering).
MPU_I2C_BUS=7
MPU_I2C_ADDR=0x68
GPIO_REED_PIN=31
GPIO_LDR_PIN=33
# Motion detection: fire TAMPER_IMPACT if any accel axis changes by this
# much (milligravities) between consecutive polls. 150mg catches a lift/drop.
MPU_ACCEL_THRESHOLD_MG=150

# Disabled on the current bench unit. Flip to 1 once hardware is installed.
ENABLE_NVME=0
NVME_DEVICE=/dev/nvme0
ENABLE_POWER_ADC=0
EOF
  chown root:"${RUNTIME_USER}" "${ENV_FILE}"
  chmod 0640 "${ENV_FILE}"
  echo "Wrote ${ENV_FILE} (edit before starting the service)."
}

write_key_template() {
  if [[ -f "${KEY_FILE}" ]]; then
    echo "Keeping existing ${KEY_FILE}."
    return
  fi
  echo "Generating a random 32-byte HMAC key at ${KEY_FILE}."
  echo "Share the same value with the customer NOC verifier."
  head -c 32 /dev/urandom | base64 > "${KEY_FILE}"
  chown root:"${RUNTIME_USER}" "${KEY_FILE}"
  chmod 0640 "${KEY_FILE}"
}

install_systemd_unit() {
  install -m 0644 "${UNIT_SRC}" "${UNIT_DST}"
  systemctl daemon-reload
  echo "Installed ${UNIT_DST}."
}

main() {
  require_root
  verify_repo
  ensure_groups
  install_python_deps
  create_config_dir
  write_env_template
  write_key_template
  install_systemd_unit

  echo
  echo "Install complete."
  echo "Next steps:"
  echo "  1. Edit ${ENV_FILE} for this device."
  echo "  2. Confirm ${KEY_FILE} matches the NOC verifier."
  echo "  3. systemctl enable --now kallon-watchdog"
  echo "  4. journalctl -u kallon-watchdog -f"
}

main "$@"
