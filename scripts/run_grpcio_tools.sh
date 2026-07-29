#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

PYTHON=(poetry run python)

python_pkg_version() {
  "${PYTHON[@]}" - "$1" <<'PY'
from __future__ import annotations

import importlib.metadata
import sys

try:
    print(importlib.metadata.version(sys.argv[1]))
except importlib.metadata.PackageNotFoundError:
    raise SystemExit(1)
PY
}

ensure_package() {
  local package_name="$1"
  local install_spec="${2:-$1}"
  if ! python_pkg_version "${package_name}" >/dev/null 2>&1; then
    "${PYTHON[@]}" -m pip install "${install_spec}"
  fi
}

ensure_package grpcio
GRPCIO_VERSION="$(python_pkg_version grpcio)"

if ! GRPCIO_TOOLS_VERSION="$(python_pkg_version grpcio-tools 2>/dev/null)"; then
  "${PYTHON[@]}" -m pip install "grpcio-tools==${GRPCIO_VERSION}"
elif [[ "${GRPCIO_TOOLS_VERSION}" != "${GRPCIO_VERSION}" ]]; then
  "${PYTHON[@]}" -m pip install "grpcio-tools==${GRPCIO_VERSION}"
fi

ensure_package mypy-protobuf

mapfile -t PROTO_FILES < <(
  find src/audio_harness/protos -type f -name '*.proto' | sort
)

"${PYTHON[@]}" -m grpc_tools.protoc \
  -I src \
  --python_out=src \
  --grpc_python_out=src \
  --mypy_out=src \
  --mypy_grpc_out=src \
  "${PROTO_FILES[@]}"
