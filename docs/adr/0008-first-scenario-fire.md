# ADR-0008: 첫 실제 시나리오는 화재(화염)다

- Status: accepted
- Date: 2026-11-24
- Owners: Provider/Replay, Verification, Engine(profile)
- Related: OQ-01, OQ-04, OQ-06, OQ-08, OQ-10, OQ-11, INP-05, NFR-02, NFR-03, NFR-04, T-R14, T-R20, ADR-0004, ADR-0007, SPEC §9

## Context

- Stage C0(ADR-0007)에서 실모델 출력이 프레임 → Provider → 엔진 → alert → ACK 경로를 통과하는 것을 확인했다(TASK-32). Stage C에 들어가려면 첫 실제 대상(OQ-01)과 후보 모델, 데이터셋(OQ-08)이 필요하다.
- 2026-11-24 조사 결과(웹 페이지만 확인, 다운로드 없음):
  - 균열: 공개 검출 모델은 모두 Ultralytics YOLO 기반(AGPL)이다. bbox 데이터셋(CODEBRIM, dacl10k, DeepCrack)은 비상업(NC) 제한이 있거나 라이선스가 불분명하다.
  - 벽 손상: 데이터가 교량 점검용 NC이고, "붕괴 징후"는 라벨 정의부터 필요하다.
  - 화재/연기: **D-Fire**(https://github.com/gaiasd/DFireDataset)는 LICENSE 파일이 CC0 1.0이다. 21,000장 이상, fire bbox 14,692개, smoke bbox 11,865개, YOLO 형식이다. 단, 파일 이름이 `PublicDataset`으로 시작하는 이미지(HPWREN, FESB, FireNet 출처)는 원 제공자 조건을 따르고, 이미지 모음과 감시 영상 각각 논문 인용이 필요하다.
  - 바로 쓸 수 있는 허용 라이선스 화재 검출기는 없다(대부분 Ultralytics 기반이거나 학습 데이터 계보가 불명확하다).
  - open-vocabulary 모델(OmDet-Turbo, OWLv2, Grounding DINO)의 화재, 연기 제로샷 성능 근거는 빈약하다. 연기 벤치마크(SmokeBench)에서 Grounding DINO mean IoU 0.245로 전용 모델보다 낮았다.
- 개발 장비는 M1 Docker CPU 전용이다(RT-DETR R18 추론 약 300 ms/프레임). 2만 장 규모 미세조정은 CPU에서 비현실적이다.
- 소유자 결정(2026-11-24): 첫 시나리오는 화재로 진행한다. 학습한 가중치는 **내부 개발용**이다.

## Decision

### 1. 첫 실제 시나리오는 화재(화염)다
- profile 시나리오 `fire_in_view`: `labels: [fire]`, `required_kinds: [object_detection]`, 시야 전체 ROI `[0, 0, 1, 1]`, `motion_classes: []`, `severity: attention`. 임계값(`min_score`, `confirm_frames`, `clear_frames`)은 검증용 초기값이고 OQ-06에서 측정으로 다시 정한다.
- 연기(`smoke`)는 같은 모델의 label로 출력하고 관측으로 기록하지만, **첫 profile에는 연기 시나리오를 두지 않는다.** 연기 단독 검출은 안개, 수증기, 분진, 역광으로 오탐이 많고 라벨 일관성이 낮다. 연기 시나리오(예: `notice` 등급)는 오탐 측정 후 별도로 결정한다.
- 균열은 2순위 후보로 남긴다(자체 촬영, 라벨링 필요). 벽 손상은 제외한다.

### 2. 두 단계로 진행한다: C1a 제로샷 → C1b 미세조정
- **C1a (학습 없음)**: OmDet-Turbo(`omlab/omdet-turbo-swin-tiny-hf`, Apache-2.0)를 open-vocabulary Provider로 붙이고 프롬프트 `[fire, smoke]`로 화재 영상을 바로 시험한다. 목적은 정확도가 아니라 **화재 영상으로 경고 경로를 검증하고 오경고 원인을 먼저 수집**하는 것이다. 설치된 transformers(4.57)에 `OmDetTurboForObjectDetection`이 있어 새 의존성은 없다. 가중치는 사용자가 직접 받고 manifest의 revision, sha256으로 대조한다(ADR-0007과 같음).
- **C1b (미세조정)**: RT-DETR R18(`PekingU/rtdetr_r18vd`, Apache-2.0)을 D-Fire로 미세조정해 manifest `rtdetr-r18-dfire`(labels `[fire, smoke]`)로 쓴다. 기존 `RtDetrProvider`와 실행 경로를 그대로 쓰고 모델 파일만 바꾼다. RF-DETR과의 비교는 OQ-11에서 다룬다.
- 경고 경로에 쓰는 모델은 C1b 결과로 정한다. C1a 모델은 탐색과 라벨링 보조용이다(ADR-0004 §5와 같은 방향).

### 3. 데이터 사용 규칙
- 사용자가 D-Fire를 직접 받아 `apps/server/var/datasets/dfire/`(git 제외)에 둔다. 코드와 스크립트는 다운로드하지 않는다.
- 데이터 준비 도구는 기본으로 `PublicDataset` 접두 이미지를 학습과 평가에서 뺀다. 포함하려면 명시적인 옵션이 필요하고, 그 경우 학습 기록에 포함 사실을 남긴다.
- 학습 기록에는 데이터 출처, 제외한 이미지 수, 분할 목록의 해시, D-Fire 논문 인용을 남긴다.
- 학습한 가중치는 내부 개발용이다. manifest에 `redistribution_allowed: false`로 적고 저장소와 배포물에 넣지 않는다. 외부 배포가 필요해지면 데이터 계보를 다시 검토한다.

### 4. 학습 환경
- 학습은 GPU 환경에서 한 번 실행한다. 2026-11-25 개발 노트북(RTX 3070 Laptop 8GB, Windows 11, Docker Desktop WSL2 백엔드)으로 정했고 컨테이너 안 GPU 접근을 확인했다(OQ-04). SSH 세션에서는 Docker Desktop 자격 증명 저장소를 쓸 수 없어, 컨테이너 실행은 로그인 세션의 일회성 예약 작업으로 한다. 맥에서는 몇 step만 도는 스모크 테스트로 학습 코드가 동작하는지만 확인한다.
- 학습 코드는 런타임 패키지와 분리하고, 추가 의존성이 필요하면 별도 선택 설치 그룹으로 두고 SPEC §9.1 등록부를 같은 작업에서 갱신한다. 기본 `dev` 이미지와 `scripts/check.sh`는 그대로 오프라인으로 통과해야 한다.
- 추론은 지금처럼 M1 Docker CPU에서 한다(`vision` 이미지).

### 5. 검증
- 실제 화재 규모는 촬영할 수 없다. 검증은 (a) D-Fire 테스트 분할, (b) D-Fire 감시 영상 등 공개 영상, (c) 로컬에서 안전하게 찍을 수 있는 소형 화염(촛불, 라이터)과 오경고 유발 장면(조명, 석양, 반사, 주황색 조끼, 모니터 화면)을 프레임 폴더로 리플레이해서 한다.
- 이벤트 단위 정답(구간 라벨)을 붙인 영상으로 `event_recall`과 `false_alerts_per_hour`를 측정한다. 정답이 없는 값은 `TBD`로 둔다.

## Alternatives

- **균열을 먼저**: 촬영은 가장 쉽지만 허용 라이선스 bbox 데이터가 없고, 정적 결함이라 시간축 엔진의 이점이 작고, 가늘고 긴 형태라 bbox보다 segmentation이 맞다. 2순위로 둔다.
- **연기를 같은 profile에서 경고**: 오탐 위험이 커서 측정 전에는 넣지 않는다.
- **제로샷 모델만으로 진행**: 근거가 빈약해 경고 경로의 기본 모델로 쓰지 않는다.
- **공개 화재 모델(HF) 사용**: Ultralytics 기반(AGPL)이거나 학습 데이터 계보가 불명확해 제외한다.
- **FASDD 데이터 추가**: 원본 라이선스 필드를 확인하지 못했다(미확인). 확인 후 추가를 검토한다.

## Consequences

- 장점: 라이선스가 확인된 데이터로 시작한다. 엔진, 계약, 서버를 바꾸지 않고 manifest와 profile로 시나리오를 추가한다(ADR-0004 §1). C1a로 GPU 없이 화재 영상 시험을 먼저 시작할 수 있다.
- 비용: 미세조정에 GPU 환경이 필요하다(비용 TBD). 실제 화재로 검증할 수 없어 공개 영상 의존도가 높다.
- 후속: OQ-04에 학습 환경 결정, OQ-06에 화재 임계값과 정답 데이터, 연기 시나리오 결정.
- 되돌림: 화재 profile, manifest, Provider를 지우면 Stage C0 상태로 돌아간다.

## Verification

- 가짜 detector로 `fire_in_view` 시나리오의 경고, 해제, 연기 단독 무경고를 검사한다.
- 데이터 준비 도구는 합성 YOLO 라벨 폴더로 `PublicDataset` 기본 제외와 분할 결정론을 검사한다.
- 실모델 테스트는 `@pytest.mark.model`이고 기본 실행에서 제외한다.
- 평가 보고서는 PLAN §4 형식이고 측정하지 않은 값은 `TBD`다.

## Results (2026-11-24, TASK-33)

- OmDet-Turbo는 transformers 외에 torchvision(처리기), timm(Swin backbone), safetensors가 필요해 `vision` 그룹과 SPEC §9.1에 추가했다(모두 허용 라이선스). transformers `from_pretrained`의 meta 초기화가 timm Swin의 `attn_mask` 버퍼를 `meta`에 남겨, config로 CPU에서 모델을 만든 뒤 가중치를 strict로 불러온다.
- 화재 없는 사무실 영상 `walk01`(3fps, 98장): 제로샷 모델이 흰 벽과 천장 조명 영역을 `fire` 0.29~0.38점으로 매 프레임 검출했다. `min_score` 0.3에서는 처음부터 끝까지 경고였고, 0.45에서는 경고가 없었다. 0.45는 화재 영상 없이 정한 초기값이며 TASK-36에서 화재 영상으로 다시 정한다.
- Provider 추론(M1, Docker CPU): p50 1051 ms, p95 1125 ms(n=98). RT-DETR R18(약 300 ms)의 약 3.5배라 실시간 처리는 약 1fps다.

## Results (2026-11-25, C1a 촛불 영상)

- 입력: 공개 영상(촛불 끄는 장면, 로컬 테스트 전용, 저장소와 배포물에 넣지 않음)을 3fps로 추출한 90장(약 30초, 640x360). 0~22초 촛불 켜짐, 23초 이후 꺼짐(연기 후 어두워짐).
- `fire` 점수: 켜진 구간 0.53~0.65, 꺼진 구간 0.59~0.72(더 높음). 박스는 매 프레임 세로 0.4~1.0으로 **불꽃이 아니라 초 몸통**이었고, 불꽃만 따로 잡힌 프레임은 없었다.
- 프롬프트 비교(`flame`, `fire`, `candle`, `burning flame`, `smoke`): 모두 같은 초 몸통 박스를 냈고 꺼진 뒤에도 점수가 유지되었다.
- 결정: `fire_c1`(`min_score` 0.45)은 0.7초에 경고했고 영상 끝까지 해제되지 않았다. 켜짐과 꺼짐을 가르는 임계값이 없다.
- walk01(화재 없음)에서 흰 벽과 조명을 `fire`로 잡은 결과(TASK-33)와 합치면, OmDet-Turbo 제로샷은 밝거나 흰 물체에 반응하고 화염을 구분하지 못한다.
- 결론: C1a 제로샷 모델은 화재 경고 경로에 쓰지 않는다(§2와 같은 방향). 경로 검증과 오경고 원인 수집용으로만 쓰고, 경고 모델은 C1b(D-Fire 미세조정)로 정한다.
- Provider 추론(M1, Docker CPU): p50 1024 ms, p95 1171 ms(n=90).

## Results (2026-11-25~26, TASK-38 C1b 학습)

환경: 개발 노트북 RTX 3070 Laptop 8GB, Docker `train-cuda`(torch 2.6.0+cu124), D-Fire(`PublicDataset` 제외) train 14,570 / val 1,596 / test 4,025장, batch 8, step당 약 0.3초, epoch당 약 9.7분.

- **분류 head 초기화 버그**: `ignore_mismatched_sizes`로 새로 만든 class head의 bias가 0이라 첫 loss가 약 500~1,100이었고 점수가 0.01~0.04로 붕괴했다(첫 30 epoch 실행, 22 epoch에서 중단). RT-DETR 방식대로 bias를 사전확률 0.01(-4.595)로 초기화해 첫 loss가 약 27로 정상화되었다.
- **평가 기준 오류**: RT-DETR은 IoU에 맞춘 점수(VFL)를 학습해 불꽃처럼 경계가 흐린 박스는 점수가 0.05~0.3에 머문다. 점수 0.5 단일 기준으로는 늘 F1 0이었다. 기준값 sweep(0.05~0.5)과 학습 후 test 평가를 추가했다.
- **학습량과 성능** (val, 불 이미지 435장과 불 없는 이미지 1,161장, 이미지 단위 최고 fire 점수로 계산한 기준값 무관 지표):

| 체크포인트 | AUC | 오검출 1%일 때 탐지율 | 오검출 5%일 때 탐지율 |
|---|---|---|---|
| 2 epoch 실행의 epoch 1 (`e2/best`, 채택) | 0.957 | 0.451 (기준 0.060) | 0.782 (기준 0.041) |
| 빈 이미지 제외 6 epoch (`best`/`last`) | 0.923 / 0.920 | 0.218 / 0.228 | 0.563 / 0.563 |
| 20 epoch (`last`) | 0.855 | 0.317 | 0.474 |

- 학습을 길게 할수록 loss는 줄지만 점수와 구분 능력(AUC)이 떨어졌다. 불 없는 이미지를 빼도 같았으므로 빈 이미지는 원인이 아니다. 원인은 현재 HF RT-DETR 학습 설정(학습률, 정규화 등)으로 추정하며 미확인이다. 목적이 파이프라인 개발이므로 튜닝은 여기서 멈추고 모델 개선은 OQ-11(RF-DETR 비교)에서 다룬다.
- **채택 모델** `e2/best`(sha256 `ed2be0c81dc92f515001d9313570eeb20a867a27428720c232a3ae7257346acc`): test 4,025장에서 fire F1 0.427(점수 0.05, P 0.579, R 0.339), smoke F1 0.323, 불 이미지 탐지율 0.646, 불 없는 이미지 오검출 0.017.
- 실영상(엔진 없이 프레임별 최고 fire 점수): 사무실 walk01 최고 0.050(0.05 초과 0/98), 공개 산불 영상의 큰 불꽃 구간 0.05 초과 34/60(최장 연속 11), 촛불(작은 실내 불꽃) 켜짐 4/67로 사실상 미검출. 작은 불꽃과 원거리 연기는 약하다.

## Results (2026-11-26, TASK-39 C1b 파이프라인)

- `fire_c1` v2: 근거 모델을 `rtdetr-r18-dfire`로 바꾸고 `min_score`를 0.05로 정했다(TASK-38 val 측정: 0.06에서 불 없는 이미지 오검출 1%, 0.041에서 5%). C1a `omdet-turbo-fire`는 탐색용으로만 남긴다.
- Provider 하한 점수(기본 0.25)가 profile `min_score`보다 높으면 C1b 근거가 Provider에서 모두 버려진다. 평가 러너는 기본 하한을 `min(0.25, profile min_score)`로 정한다. 시뮬레이터는 profile을 읽지 않으므로 `--score-floor 0.05`를 명시한다.
- 평가 러너(3fps, 정답 구간 없음):

| 영상 | 경고 | 비고 |
|---|---|---|
| 공개 산불 영상(107장, 약 36초) | 3회(9.3초, 26.0초, 33.0초) | 큰 불꽃은 5초부터 보였지만 첫 경고는 9.3초, 15.7초에 해제된 뒤 불꽃이 남은 구간을 놓침. 26초 이후는 진화 장면(소방차 경광등 또는 남은 불, 정답 없음) |
| 촛불(90장) | 0회 | `candidate`와 `clear`만 반복. 작은 실내 불꽃은 미검출(C1a는 초 몸통에 계속 경고) |
| 사무실 walk01(98장, 불 없음) | 0회 | 전 구간 `clear`(C1a 0.3 기준은 내내 경고) |

- 시뮬레이터 → 서버(산불 영상, `--model rtdetr-r18-dfire --score-floor 0.05`): 107장 중 102장 전송, 5장 지연 건너뜀, Provider p50 337 ms, p95 378 ms(M1 CPU). alert 2건 모두 `ack_received` → HUD, 스피커 → `ack_played`, DB 상태 `played`, 입력 종료 후 `notice` 1회.
- 결론: 화재 시나리오가 실제 학습 모델로 프레임 → Provider → 엔진 → alert → ACK 경로를 통과한다. 정확도(놓침, 지연 경고)는 모델 한계이며 정답 구간 기반 측정은 TASK-34, 모델 개선은 OQ-11에서 다룬다.
