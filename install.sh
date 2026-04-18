#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
LOCAL_BIN_DIR="${HOME}/.local/bin"
PROJECT_LAUNCHER="${PROJECT_DIR}/codex-auth-pool"

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
  echo
  echo "Installed command: codex-auth-pool"
  echo "Runtime state lives under: ~/.codex-auth-pool and ~/.codex"
  echo "Next:"
  echo "  codex-auth-pool init --install-launchd --restart-after-switch"
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

cat <<EOF

Installed into:
  ${VENV_DIR}

Runtime state still lives under:
  ~/.codex-auth-pool
  ~/.codex

Use from anywhere:
  codex-auth-pool init --install-launchd --restart-after-switch

Use inside this project directory:
  ./codex-auth-pool status

EOF
