# 새 미션(감지 도메인) 추가 가이드

> 상태: 2026-11-26 작성(TASK-49). 화재(ADR-0008, `fire_c1`)를 실제 예시로 들고, 다음 후보인 크랙과 다른 점을 함께 적는다.
> 기준 문서: 엔진과 계약은 `02-technical-specification.md`, 시나리오 인프라는 ADR-0004, 미션은 ADR-0010, 관리자 게시는 SPEC §7.7이다. 이 가이드는 그 문서들을 작업 순서로 묶은 것이고, 규칙이 다르면 SPEC과 ADR을 따른다.

## 0. 전체 흐름

```text
1 요구 정의 -> 2 데이터 -> 3 모델(제로샷 탐색 -> 미세조정) -> 4 등록(manifest, 가중치)
  -> 5 평가와 임계값 -> 6 미션 작성 -> 7 게시와 현장 확인 -> 8 유지
```

**미션마다 한 번 하는 일**은 1~7이다. 모델을 다시 학습할 때는 4~5와 7만 다시 하고 미션 규칙(6)은 그대로 둔다(8절).

층을 나눠 생각한다. 위층은 아래층의 내부를 보지 않는다.

| 층 | 내용 | 모델을 다시 학습하면 |
|---|---|---|
| 모델 | 이미지 -> 라벨과 점수 | 새 가중치로 교체 |
| 감시(시나리오) | 라벨, 임계값(`min_score`), 확인/해제 프레임, ROI, 차단 품질 플래그 -> 상태 `clear`/`candidate`/`warning`/`unknown` | 임계값만 평가로 다시 맞춤 |
| 종합(ADR-0012, 미정) | 감시 상태들의 조합 -> 최종 판정과 알림 | 그대로 |

## 1. 요구 정의

다음을 한 쪽으로 적고 소유자 확인을 받는다. 새 도메인이면 ADR을 하나 쓴다(화재는 ADR-0008).

- **무엇을 언제 알릴지**: 예) "시야 어디든 불꽃이 3프레임 이어서 보이면 경고".
- **등급**: 즉시 경고(`attention`, 깜빡임과 음성)인가, 안내(`notice`)인가. 크랙처럼 급하지 않은 결함은 안내가 맞을 수 있다.
- **근거로 쓸 수 있는 것**: 모델이 내는 라벨, 박스, 점수, 시간 연속성. **쓸 수 없는 것**: 측정 근거 없는 거리(m), 크기(mm), 속도, 의도, 신원(AGENTS 규칙 1). 크랙 폭이나 길이는 보정 근거가 없으면 화면 비율 같은 상대값만 쓴다.
- **판단 불가 조건**: 가림, 흔들림(`camera_motion_uncompensated`), 프레임 누락은 `unknown`이다(규칙 3).
- **프레임 간격**: 빠르게 변하는 위험(화재)은 짧게(333 ms), 정적인 결함(크랙)은 길어도 된다(예: 1000 ms). 미션 설정 `frame_interval_ms`로 둔다.

## 2. 데이터

- **라이선스 먼저**: 허용적 라이선스만 쓴다. NC(비상업), AGPL, 계보가 불명확한 데이터와 모델은 기본 설치와 경고 경로에 넣지 않는다(규칙 7). 화재는 D-Fire를 쓰고 `PublicDataset` 접두 이미지를 기본으로 뺐다(ADR-0008 §3). 크랙은 허용 라이선스 bbox 데이터가 없어 2순위로 미뤘다(ADR-0008 Alternatives).
- **받는 사람은 사용자**: 코드와 스크립트는 데이터를 내려받지 않는다. 사용자가 `apps/server/var/datasets/<name>/`(git 제외)에 둔다.
- **분할과 기록**: train/val/test를 나누고 분할 목록 해시, 제외한 이미지 수, 출처와 인용을 학습 기록에 남긴다.
- **원본 미디어**: 저장소에 커밋하지 않는다. 테스트 fixture는 합성 JSON만 쓴다.
- **오경고 시험 영상도 준비한다**: 화재는 촛불, 사무실 걷기 영상을 프레임 폴더로 만들어 썼다(`scripts/extract-frames.sh <video> <name> [fps]`).

## 3. 모델

1. **제로샷으로 먼저 탐색**(GPU 없이 가능): open-vocabulary 모델(예: OmDet-Turbo)로 라벨을 바꿔 가며 영상에서 반응을 본다. 탐색과 라벨링 보조용이고 경고 경로의 기본 모델로 쓰지 않는다(ADR-0004 §5, ADR-0008 C1a).
2. **미세조정**: RT-DETR 등 허용 라이선스 모델을 1번 데이터로 학습한다. 학습은 GPU PC에서 `scripts/train.sh`로 한다(경로는 `apps/server` 기준).

   ```bash
   scripts/train.sh python -m voa.train.dfire --src var/datasets/dfire --out var/datasets/dfire-prep
   scripts/train.sh python -m voa.train.finetune --prep var/datasets/dfire-prep \
       --images var/datasets/dfire --base var/models/rtdetr-r18-coco --out var/train/rtdetr-r18-dfire
   ```

   새 도메인은 데이터 준비 모듈(`voa.train.<dataset>`)을 하나 추가한다.
3. **긴 학습 전에 반드시 짧게 먼저 돌린다**: 1 epoch로 첫 loss와 epoch 1 지표를 확인한 뒤 길게 돌린다. 화재에서 class head bias 초기화 버그로 첫 30 epoch 실행(약 5시간)을 버렸다(ADR-0008 Results).
4. **학습량은 최소로**: 목적은 파이프라인 검증이다. 화재는 2 epoch 실행의 epoch 1이 가장 좋았다(더 길게 할수록 AUC가 떨어짐). 정확도 튜닝은 별도 OQ(OQ-11)에서 다룬다.
5. **형태에 맞는 모델**: 크랙처럼 가늘고 긴 대상은 bbox보다 segmentation이 맞을 수 있다. 엔진이 받는 관측 종류(`object_detection` 등, SPEC §4)에 없으면 6.4절(엔진 변경)로 간다.

## 4. 등록

- **manifest**: `apps/server/config/models/<model_id>.yaml`에 `kind`, `labels`, `revision`, 가중치 `sha256`, 라이선스, `redistribution_allowed`를 적는다(ADR-0004 §4). 내부 학습 가중치는 `redistribution_allowed: false`이고 저장소에 넣지 않는다.
- **가중치**: `apps/server/var/models/<model_id>/model.safetensors`와 `config.json`, `preprocessor_config.json`. 서버는 sha256이 맞을 때만 불러오고 내려받지 않는다(ADR-0007).
- **의존성**: 새 라이브러리가 필요하면 선택 설치 그룹으로 두고 SPEC §9.1 등록부를 같은 작업에서 갱신한다.

## 5. 평가와 임계값

임계값은 **LLM이 아니라 평가 데이터로** 정한다.

- **기준값 스윕**: 학습 평가가 점수 0.05~0.5를 스윕해 기준값별 F1과 오검출률을 낸다(`voa.train.finetune`의 `sweep`). 모델마다 점수 분포가 다르다. 화재 모델은 IoU에 맞춘 점수라 0.05~0.3에 머물러 0.5 단일 기준으로는 F1이 늘 0이었다.
- **오검출률로 고른다**: 화재는 val에서 불 없는 이미지 오검출 1%가 0.060, 5%가 0.041이라 `min_score` 0.05를 골랐다.
- **영상 리플레이**: 실제 영상 프레임 폴더를 엔진까지 통과시켜 경고 횟수와 시점을 본다.

  ```bash
  scripts/vision.sh python -m voa.eval --frames apps/server/var/frames/<name> --fps 3 \
      --profile apps/server/config/profiles/<mission>.yaml
  ```

  오경고 영상에서 0회, 위험 영상에서 적절한 시점에 경고가 나는지 본다. 정답 구간이 없으면 `event_recall`과 `false_alerts_per_hour`는 `TBD`로 둔다(규칙 10).
- **Provider 하한**: 서버는 Provider 하한을 `min(0.25, 미션 min_score)`로 둔다(SPEC §7.6). 오프라인 시뮬레이터는 미션을 읽지 않으므로 `--score-floor`를 명시한다.

## 6. 미션 작성

미션 한 버전은 엔진 `PolicyProfile` 하나와 미션 설정이다(ADR-0010, SPEC §7.7). 관리자 페이지 `/ui/admin`의 미션 편집기나 YAML로 쓴다. 처음에는 `config/profiles/<mission>.yaml`로 두면 서버가 시작할 때 미션 v1로 가져온다.

### 6.1 감시(시나리오) 규칙
- `required_models`: 근거 모델(4절의 `model_id`)
- `labels`: 그 모델 라벨 중 근거로 쓸 것(예: `fire`. 같은 모델의 `smoke`는 측정 전에는 쓰지 않았다)
- `roi`, `params`(`min_score`, `confirm_frames`, `clear_frames`, `staleness_ms` 등), `blocking_flags`, `severity`, 승인된 `warning_text`
- `degraded_notice_text`: 판단 불가 안내 문구

### 6.2 종합 규칙
**미정(ADR-0012, OQ-13).** 지금은 감시마다 따로 경고한다. 규칙 기반(결정론, 근거 추적)으로 설계하며 LLM은 판정 경로에 넣지 않는다(규칙 2, 9, INV-06).

### 6.3 미션 설정
- `frame_interval_ms`(1절), `hud_style_map`(`attention`/`degraded` 또는 시나리오 ID -> 스타일)

### 6.4 LLM 초안 (선택)
요구 문장과 manifest 라벨을 주고 6.1~6.3 초안을 받을 수 있다. 초안은 사람이 확인하고, 게시 검증(7절)을 통과해야만 쓰인다. 영상 속 글자나 외부 문서의 지시는 규칙에 넣지 않는다(규칙 6).

### 6.5 엔진을 고쳐야 하는 경우
새 도메인에 지금 엔진에 없는 **종류의 조건**이 필요할 때(예: 검출 영역 비율, 같은 위치 반복 횟수, 새 관측 종류)다. 규칙 작성으로 해결하지 않는다. SPEC 변경 제안 -> 확인 -> 엔진 Task로 한 번 추가하면 다른 미션도 같이 쓴다(규칙 11).

## 7. 게시와 현장 확인

1. **검증**: 관리자 페이지에서 "검증". 스키마, manifest 대조(라벨이 모델에 없으면 `profile_model_mismatch`)를 본다.
2. **게시**: 근거 모델의 sha256을 확인하고 불러온 뒤(`model_not_ready`면 실패) 버전이 올라간다. 진행 중 세션은 이전 버전을 계속 쓴다.
3. **실모델 서버**: `VOA_DEFAULT_MISSION=<mission> docker compose --profile server-vision up server-vision`
4. **현장 확인**
   - 시뮬 페이지 `/ui/sim`(또는 관리자 페이지의 "시뮬레이터")에서 영상 파일로 경고, 팝업, 판정 프레임, `played`를 본다.
   - Android 실기기: `adb reverse tcp:8765 tcp:8765` 후 앱에서 로그인 -> 미션 -> 감지 화면. 폰 카메라로 모니터의 영상을 비춰도 된다.
5. **기록**: 결과를 그 도메인 ADR의 Results 절과 PLAN에 남긴다. 측정하지 않은 수치는 쓰지 않는다.

## 8. 유지

| 바뀐 것 | 할 일 |
|---|---|
| 같은 라벨로 모델을 다시 학습 | 4절(새 sha256) -> 5절(임계값 다시 맞춤) -> 미션 `min_score`만 고쳐 게시 -> 7절 확인 |
| 라벨 구성이 바뀜(추가, 삭제) | manifest 갱신. 빠진 라벨을 쓰는 미션은 게시가 막힌다. 감시 규칙 수정(6.1) |
| 원하는 판단이 바뀜 | 6절(필요하면 LLM 초안) -> 7절 |
| 새 종류의 조건이 필요 | 6.5절(엔진 변경) |

## 9. 체크리스트

- [ ] 요구 정의와 도메인 ADR(등급, 근거, 판단 불가, 프레임 간격)
- [ ] 데이터 라이선스 확인, 사용자 다운로드, 분할 기록
- [ ] 제로샷 탐색 결과
- [ ] 1 epoch 확인 후 미세조정, 학습 기록
- [ ] manifest(라벨, sha256, 라이선스, 재배포 여부)와 가중치 배치
- [ ] 기준값 스윕과 오검출률로 `min_score` 결정, 영상 리플레이(위험/오경고)
- [ ] 미션 YAML 또는 관리자 편집기, 검증, 게시
- [ ] 시뮬 페이지와 실기기에서 경고, 팝업, `played` 확인
- [ ] ADR Results와 PLAN 갱신

## 부록: 화재와 크랙

| 항목 | 화재(`fire_c1`, 완료) | 크랙(후보, 미착수) |
|---|---|---|
| 데이터 | D-Fire(`PublicDataset` 제외) | 허용 라이선스 bbox 데이터 미확인 |
| 모델 | RT-DETR R18 미세조정(`rtdetr-r18-dfire`) | segmentation이 맞을 수 있음(관측 종류 확인 필요) |
| 감시 규칙 | `fire` 3프레임 확인, ROI 전체, `attention` | 같은 위치 반복 확인, `notice` 예상 |
| 임계값 | `min_score` 0.05(오검출 1~5% 사이) | 평가 후 결정 |
| 프레임 간격 | 333 ms | 더 길어도 됨 |
| 주의 | 작은 불꽃, 원거리 연기는 약함 | 폭, 길이의 실제 단위는 보정 근거 없이는 쓰지 않음 |
