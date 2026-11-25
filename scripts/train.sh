#!/usr/bin/env bash
# 미세조정 학습 (ADR-0008, TASK-38). GPU가 있는 amd64 PC에서 실행한다. 네트워크 없이 돈다.
#   scripts/train.sh python -m voa.train.dfire --src var/datasets/dfire --out var/datasets/dfire-prep
#   scripts/train.sh python -m voa.train.finetune --prep var/datasets/dfire-prep \
#       --images var/datasets/dfire --base var/models/rtdetr-r18-coco --out var/train/rtdetr-r18-dfire
# 경로는 apps/server 기준이다. 결과 가중치는 var/train/<name>/best/ 에 남는다.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$#" -eq 0 ]; then set -- bash; fi
exec docker compose --profile train run --rm --build train "$@"
