#!/usr/bin/env bash
# Admin/sim page logic tests (TASK-45) in a Node container with no network.
# Node's built-in test runner only: no npm packages are installed.
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker compose --profile tools run --rm ui-test "$@"
