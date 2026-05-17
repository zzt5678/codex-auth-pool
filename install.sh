#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
LOCAL_BIN_DIR="${HOME}/.local/bin"
PROJECT_LAUNCHER="${PROJECT_DIR}/codex-auth-pool"
OS_NAME="$(uname -s)"

next_command() {
  if [[ "${OS_NAME}" == "Darwin" ]]; then
    echo "codex-auth-pool init --install-launchd --restart-after-switch"
  elif [[ "${OS_NAME}" == "Linux" ]]; then
    echo "codex-auth-pool init --install-systemd"
  else
    echo "codex-auth-pool init"
  fi
}

restart_background_service_if_present() {
  if [[ "${OS_NAME}" == "Darwin" ]]; then
    local plist="${HOME}/Library/LaunchAgents/ai.codex.auth.pool.plist"
    if [[ -f "${plist}" ]] && command -v launchctl >/dev/null 2>&1; then
      if launchctl print-disabled "gui/$(id -u)" 2>/dev/null | grep -q '"ai.codex.auth.pool" => disabled'; then
        echo "[codex-auth-pool] Existing launchd agent is disabled; leaving it stopped."
        echo "[codex-auth-pool] Re-enable manually with: launchctl enable gui/$(id -u)/ai.codex.auth.pool"
        return
      fi
      if launchctl kickstart -k "gui/$(id -u)/ai.codex.auth.pool" >/dev/null 2>&1; then
        echo "[codex-auth-pool] Reloaded existing launchd agent: ai.codex.auth.pool"
      elif launchctl bootout "gui/$(id -u)/ai.codex.auth.pool" >/dev/null 2>&1 || true; launchctl bootstrap "gui/$(id -u)" "${plist}" >/dev/null 2>&1; then
        echo "[codex-auth-pool] Reloaded existing launchd agent: ai.codex.auth.pool"
      else
        echo "[codex-auth-pool] Warning: failed to reload existing launchd agent automatically."
        echo "[codex-auth-pool] Run: codex-auth-pool launchd-install --restart-after-switch"
      fi
    fi
    return
  fi

  if [[ "${OS_NAME}" == "Linux" ]]; then
    if command -v systemctl >/dev/null 2>&1 && systemctl --user cat codex-auth-pool.service >/dev/null 2>&1; then
      systemctl --user daemon-reload >/dev/null 2>&1 || true
      if systemctl --user restart codex-auth-pool.service >/dev/null 2>&1; then
        echo "[codex-auth-pool] Reloaded existing systemd user service: codex-auth-pool.service"
      else
        echo "[codex-auth-pool] Warning: failed to reload existing systemd user service automatically."
        echo "[codex-auth-pool] Run: codex-auth-pool systemd-install --interval-seconds 600"
      fi
    fi
  fi
}

find_python() {
  local candidate
  for candidate in python3.12 python3.11 python3.10 python3; do
    if ! command -v "${candidate}" >/dev/null 2>&1; then
      continue
    fi
    if "${candidate}" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
    then
      echo "${candidate}"
      return 0
    fi
  done
  return 1
}

chmod +x "${PROJECT_LAUNCHER}"

install_cli_wrapper() {
  mkdir -p "${LOCAL_BIN_DIR}"
  cat > "${LOCAL_BIN_DIR}/codex-plus" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec "${CODEX_AUTH_POOL_BIN:-codex-auth-pool}" cli-run -- "$@"
EOF
  chmod +x "${LOCAL_BIN_DIR}/codex-plus"
}

if ! PYTHON_BIN="$(find_python)"; then
  echo "[codex-auth-pool] Python 3.10+ is required."
  echo "Install Python 3.10 or newer, then rerun ./install.sh"
  exit 1
fi

if command -v pipx >/dev/null 2>&1; then
  echo "[codex-auth-pool] Installing with pipx..."
  pipx install "${PROJECT_DIR}" --force
  mkdir -p "${LOCAL_BIN_DIR}"
  ln -sf "${PROJECT_LAUNCHER}" "${LOCAL_BIN_DIR}/codex-auth-pool-local"
  install_cli_wrapper
  restart_background_service_if_present
  echo
  echo "Installed command: codex-auth-pool"
  echo "Plus-only CLI command: codex-plus"
  echo "Runtime state lives under: ~/.codex-auth-pool and ~/.codex"
  echo "Next:"
  echo "  $(next_command)"
  echo
  echo "Also available inside the project directory:"
  echo "  ./codex-auth-pool status"
  exit 0
fi

echo "[codex-auth-pool] pipx not found, installing into local virtualenv..."
echo "[codex-auth-pool] Using ${PYTHON_BIN}..."
"${PYTHON_BIN}" -m venv "${VENV_DIR}"
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip
pip install -e "${PROJECT_DIR}"
mkdir -p "${LOCAL_BIN_DIR}"
ln -sf "${PROJECT_LAUNCHER}" "${LOCAL_BIN_DIR}/codex-auth-pool"
install_cli_wrapper
restart_background_service_if_present

cat <<EOF

Installed into:
  ${VENV_DIR}

Runtime state still lives under:
  ~/.codex-auth-pool
  ~/.codex

Use from anywhere:
  $(next_command)
  codex-plus

Use inside this project directory:
  ./codex-auth-pool status

EOF
