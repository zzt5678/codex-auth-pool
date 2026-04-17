#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
LOCAL_BIN_DIR="${HOME}/.local/bin"
PROJECT_LAUNCHER="${PROJECT_DIR}/codex-auth-pool"

chmod +x "${PROJECT_LAUNCHER}"

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
python3 -m venv "${VENV_DIR}"
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
