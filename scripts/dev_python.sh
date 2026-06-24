#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/api/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python"
REQUIRED_VERSION="3.12"

python_version() {
  "$1" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
}

require_python312() {
  local python_bin="$1"
  if ! command -v "${python_bin}" >/dev/null 2>&1 && [[ ! -x "${python_bin}" ]]; then
    echo "Python interpreter not found: ${python_bin}" >&2
    echo "Install Python ${REQUIRED_VERSION}, or run: make dev-setup PYTHON_BIN=/path/to/python3.12" >&2
    exit 1
  fi

  local actual_version
  actual_version="$(python_version "${python_bin}")"
  if [[ "${actual_version}" != "${REQUIRED_VERSION}" ]]; then
    echo "Python ${REQUIRED_VERSION} is required; ${python_bin} reports ${actual_version}." >&2
    exit 1
  fi
}

require_venv() {
  if [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "Missing local virtual environment: ${VENV_DIR}" >&2
    echo "Create it with: make dev-setup PYTHON_BIN=/path/to/python3.12" >&2
    exit 1
  fi

  local actual_version
  actual_version="$(python_version "${VENV_PYTHON}")"
  if [[ "${actual_version}" != "${REQUIRED_VERSION}" ]]; then
    echo "Existing ${VENV_DIR} uses Python ${actual_version}; Python ${REQUIRED_VERSION} is required." >&2
    echo "Move or remove the incompatible venv explicitly, then run:" >&2
    echo "  make dev-setup PYTHON_BIN=/path/to/python3.12" >&2
    exit 1
  fi
}

case "${1:-}" in
  setup)
    requested_python="${PYTHON_BIN:-python3.12}"
    if [[ -e "${VENV_DIR}" ]]; then
      require_venv
    else
      require_python312 "${requested_python}"
      "${requested_python}" -m venv "${VENV_DIR}"
    fi
    "${VENV_PYTHON}" -m pip install -r "${ROOT_DIR}/api/requirements.txt"
    ;;
  run)
    shift
    require_venv
    exec "${VENV_PYTHON}" "$@"
    ;;
  check)
    require_venv
    echo "${VENV_PYTHON} (Python ${REQUIRED_VERSION})"
    ;;
  *)
    echo "Usage: $0 {setup|run|check}" >&2
    exit 2
    ;;
esac
