#!/usr/bin/env bash
# Stage C0 실모델 검증 컨테이너 (ADR-0007). 네트워크 없이 실행한다.
#   scripts/vision.sh pytest -q -m model apps/server/tests/contracts/test_rtdetr_provider.py
#   scripts/vision.sh bash
set -euo pipefail
cd "$(dirname "$0")/.."
export VOA_UID="${VOA_UID:-$(id -u)}" VOA_GID="${VOA_GID:-$(id -g)}"
if [ "$#" -eq 0 ]; then set -- bash; fi
exec docker compose --profile vision run --rm --build vision "$@"
