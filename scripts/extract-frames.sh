#!/usr/bin/env bash
# 영상에서 이미지 프레임을 뽑는다 (Stage C0, ADR-0007). ffmpeg는 Docker 안에서만 실행한다.
#   scripts/extract-frames.sh ~/Movies/walk.mov walk01 5
# 결과: apps/server/var/frames/<name>/00001.jpg ... (git 제외, 긴 변 640px 이하)
# - 영상 폴더는 읽기 전용으로 마운트하고, 컨테이너는 네트워크 없이 실행한다.
# - 오디오와 메타데이터(촬영 위치 등)는 버린다. 원본 영상은 복사하지 않는다.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "usage: scripts/extract-frames.sh <video> <name> [fps=5]" >&2
  exit 2
fi
video="$1"; name="$2"; fps="${3:-5}"

if [ ! -f "$video" ]; then echo "error: video file not found" >&2; exit 2; fi
if ! [[ "$name" =~ ^[A-Za-z0-9_-]+$ ]]; then echo "error: name must match [A-Za-z0-9_-]+" >&2; exit 2; fi
if ! [[ "$fps" =~ ^[0-9]+([.][0-9]+)?$ ]] || [ "$fps" = "0" ]; then echo "error: fps must be a positive number" >&2; exit 2; fi

out="apps/server/var/frames/$name"
if [ -d "$out" ] && [ -n "$(ls -A "$out")" ]; then echo "error: $out is not empty" >&2; exit 2; fi
mkdir -p "$out"

dir="$(cd "$(dirname "$video")" && pwd)"
base="$(basename "$video")"
export VOA_UID="${VOA_UID:-$(id -u)}" VOA_GID="${VOA_GID:-$(id -g)}"
docker compose --profile tools run --rm --build -v "$dir:/input:ro" frames \
  ffmpeg -nostdin -hide_banner -loglevel error -i "/input/$base" \
    -an -map_metadata -1 \
    -vf "fps=$fps,scale='if(gt(iw,ih),min(640,iw),-2)':'if(gt(iw,ih),-2,min(640,ih))'" \
    -q:v 3 "/workspace/$out/%05d.jpg"
echo "frames: $(ls "$out" | wc -l | tr -d ' ') -> $out (fps $fps)"
