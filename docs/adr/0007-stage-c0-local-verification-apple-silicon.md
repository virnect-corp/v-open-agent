# ADR-0007: Stage C0 - Apple Silicon 로컬에서 실모델 경로를 검증한다

- Status: accepted
- Date: 2026-11-19
- Owners: Provider/Replay, Verification
- Related: INP-03, INP-05, NFR-02, NFR-03, T-R14, OQ-01, OQ-04, OQ-06, OQ-08, ADR-0004, SPEC §7.4, §9

## Context

- 현재 개발 장비는 Apple M1 Mac이고, 빌드/검사/실행은 Docker Desktop의 Linux arm64 VM 안에서 한다(AGENTS.md, CLAUDE.md §4).
- Docker Desktop for Mac의 컨테이너는 Metal GPU에 접근할 수 없다. 컨테이너 안에서 PyTorch `mps` 백엔드는 쓸 수 없고 CPU 추론만 가능하다. 호스트 웹캠 같은 USB 장치도 컨테이너로 전달되지 않는다.
- Stage B(MVP-1)는 mock 관측으로 시뮬레이터 → 서버 → 엔진 → alert → ACK 경로를 검증했다. 다음 MVP의 목적은 **실제 모델 출력이 같은 경로를 통과하는지와, 그때의 지연과 오경고를 측정하는 것**이다. 정확도 목표는 아니다.
- 첫 실제 대상(OQ-01: 균열, 벽 손상, 화재, 연기)은 정답 데이터와 미세조정이 필요하고 아직 정하지 않았다. 이를 기다리면 실모델 경로 검증이 늦어진다.
- WebSocket `frame` 메시지(카메라 이미지)는 예약 상태이고 Android 계약과 묶여 있다(SPEC §7.2, §7.4, ADR-0003). 지금 확정하면 계약 변경 비용이 크다.
- 확인한 모델 사실(2026-11-19, 모델 카드 기준):
  - RT-DETR R18 `PekingU/rtdetr_r18vd`: Apache-2.0, COCO 2017 학습, 약 20M 파라미터, transformers `RTDetrForObjectDetection`으로 로드
  - OmDet-Turbo `omlab/omdet-turbo-swin-tiny-hf`: Apache-2.0, 약 0.1B 파라미터
  - RF-DETR: Nano/Small/Medium/Large 탐지는 Apache-2.0, Atto/Femto/Pico/XL/2XL/NAS는 PML 1.0(`rfdetr[plus]`)

## Decision

### 1. Stage C0를 Stage C의 첫 부분으로 둔다
Stage C0는 로컬 검증 단계다. 실행 환경은 Docker Linux arm64, **CPU 전용**이다. GPU와 Metal을 가정하지 않는다. 처리 속도와 지연의 목표치는 두지 않고 측정만 한다(값이 없으면 `TBD`).

### 2. 검증 시나리오는 COCO `person` 탐지 전용 시나리오다
- 새 profile `apps/server/config/profiles/local_verify_c0.yaml`에 시나리오 `person_in_walkway` 하나를 둔다. `required_kinds: [object_detection]`, `required_models: {object_detection: rtdetr-r18-coco}`, `labels: [person]`이다. tracker가 필요 없으므로 `camera_motion_uncompensated` 판정 문제를 C0에서 다루지 않는다.
- 기존 `indoor_research_v0`(mock 모델)와 그 fixture는 바꾸지 않는다.
- OQ-01(첫 실제 대상)은 미정으로 유지한다. C0 시나리오는 인프라 검증용이며 제품 시나리오가 아니다.

### 3. detector는 RT-DETR R18(COCO)이다
- `voa.providers.rtdetr.RtDetrProvider`가 transformers와 torch(CPU)로 추론하고 `object_detection` Draft를 만든다. bbox는 이미지 크기로 정규화한다.
- manifest `rtdetr-r18-coco`의 `labels`는 Provider가 내보낼 label 목록이다(C0는 `[person]`). 목록 밖의 COCO 클래스는 내보내지 않는다.
- Provider 하한 점수는 설정값(기본 0.25)이고, 경고 판단의 `min_score`는 profile이 정한다. 두 값 모두 검증용 초기값이며 OQ-06에서 다시 정한다.
- 가중치는 사용자가 직접 받아 `var/models/rtdetr-r18-coco/`에 둔다. manifest는 `weights: file`, 고정 `revision`(Hugging Face commit), `sha256`을 가진다. 서버와 시뮬레이터는 다운로드하지 않는다.
- 의존성은 선택 설치 그룹 `vision`(torch, transformers, Pillow)에 둔다. 기본 `dev` 이미지와 `scripts/check.sh`는 이 그룹 없이 오프라인으로 통과해야 한다(DAT-03). 실모델 테스트는 `@pytest.mark.model`이다.

### 4. 입력은 사용자가 준비한 이미지 프레임 폴더다
- 사용자가 자기 영상에서 호스트 도구로 프레임(JPEG/PNG)을 추출한 폴더를 컨테이너에 읽기 전용으로 마운트한다. 프레임 간격은 실행 인자 `--fps`로 준다.
- 코드는 이미지를 메모리에서만 읽고, 복사, 저장, 로그 기록을 하지 않는다(AGENTS 규칙 5). 프레임 폴더는 저장소 밖 또는 git 제외 경로(`var/`)에 둔다. 주변 사람이 찍힌 영상은 동의를 받았거나 비식별된 것만 쓴다.

### 5. C0에서 Provider는 장치 쪽(시뮬레이터)에서 돈다
- 시뮬레이터가 프레임 폴더를 읽어 Provider로 Draft를 만들고, 기존 `observation` 메시지로 보낸다. 서버, 엔진, WebSocket 계약은 바꾸지 않는다.
- 서버 쪽 Provider와 `frame` 메시지(SPEC §7.4 Stage C 행)는 실제 장치가 이미지를 보내는 단계(Stage C1 이후)에서 확정한다. C0의 이 배치는 검증용 예외다.

### 6. 오프라인 평가 러너로 측정한다
`python -m voa.eval`이 프레임 폴더 → Provider → `DecisionEngine`을 서버 없이 실행한다. 시간은 `--fps`에서 만든 monotonic 값과 고정 epoch 시계를 쓴다. 출력은 결정 JSONL과 PLAN §4 평가 보고 형식이다. Provider 추론 시간 p50/p95와 엔진 step p50/p95는 측정값으로 쓴다. 정답 라벨이 없으면 recall과 FP/hour는 `TBD`다.

## Alternatives

- **macOS에서 직접 실행(PyTorch `mps`)**: 더 빠르지만 호스트에 패키지를 설치하지 않는 규칙(CLAUDE.md §4)을 어긴다. 필요하면 측정 전용 실험으로 따로 제안한다.
- **OmDet-Turbo를 먼저 사용**: 프롬프트로 임의 대상을 시험할 수 있지만 RT-DETR R18보다 약 5배 크고 CPU 지연이 클 것으로 예상된다(미측정). C1에서 open-vocabulary 검증용으로 다룬다.
- **RF-DETR Nano**: Apache-2.0이지만 `rfdetr` 패키지의 의존성과 CPU 추론 경로를 아직 확인하지 않았다. OQ-11 비교에서 다룬다.
- **Ultralytics YOLO**: AGPL이라 기본 설치에서 제외한다(SPEC §9).
- **지금 `frame` 메시지 구현**: 서버 Provider 경로를 검증할 수 있지만 Android 계약까지 바뀐다. 이미지 전송 크기와 주기 결정이 필요해 C1로 미룬다.
- **웹캠 실시간 입력**: Docker Desktop for Mac에서 장치를 전달할 수 없다. 실시간 카메라는 Stage F(Android)에서 다룬다.

## Consequences

- 장점: 계약, 엔진, 서버를 바꾸지 않고 실모델 출력으로 경고 경로와 지연을 측정할 수 있다. 기본 개발 환경과 검사는 그대로다.
- 비용: `vision` 이미지가 커진다(torch CPU 포함). CPU 추론이라 처리 가능한 fps가 낮을 수 있다. 실시간 데모는 없다.
- 라이선스: Pillow의 SPDX는 `MIT-CMU`다. 2026-11-20 소유자 승인으로 SPEC §9.1 허용 목록에 `MIT-CMU`를 추가했다(TASK-29).
- 되돌림: `vision` 그룹, Provider 모듈, C0 profile과 manifest를 지우면 Stage B 상태로 돌아간다.

## Verification

- 가중치 없이 실행되는 단위 테스트: 가짜 모델로 label 필터, bbox 정규화, Draft 스키마, 하한 점수, 프레임 폴더 읽기 전용(파일 생성 0건)을 검증한다.
- `@pytest.mark.model` 테스트: 사용자가 받은 가중치의 sha256 대조 후 추론 1회
- `scripts/check.sh`가 `vision` 그룹 없이 통과한다.
- 평가 러너 보고서에 측정하지 않은 값이 `TBD`로 남는지 검사한다.
