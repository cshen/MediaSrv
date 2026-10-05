#!/bin/bash
#
# Render / install the MediaSrv launchd service from config.toml.
# See deploy/install_launchd.py for details.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

exec uv run python deploy/install_launchd.py "$@"
