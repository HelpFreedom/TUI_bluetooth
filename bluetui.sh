#!/usr/bin/env bash
# Convenience launcher: runs bluetui without requiring `pip install`.
# `readlink -f` so the launcher works when invoked via a symlink
# (e.g. `~/.local/bin/bt -> bluetui.sh`).
set -e
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$HERE"
exec python3 -m bluetui "$@"
