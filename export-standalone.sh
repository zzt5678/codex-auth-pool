#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="${1:-}"

if [[ -z "${DEST_DIR}" ]]; then
  echo "Usage: ./export-standalone.sh /absolute/path/to/codex-auth-pool"
  exit 1
fi

mkdir -p "${DEST_DIR}"

rsync -a \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.pyo' \
  --exclude '*.egg-info' \
  --exclude 'build' \
  --exclude 'dist' \
  "${PROJECT_DIR}/" "${DEST_DIR}/"

echo "Exported standalone project to:"
echo "  ${DEST_DIR}"
echo
echo "Next:"
echo "  cd ${DEST_DIR}"
echo "  ./install.sh"
