# ADR-0004: 시나리오 공통 인프라 - 한 프레임을 여러 모델이 분석하고, 시나리오는 근거 모델을 선언한다

- Status: accepted
- Date: 2026-11-18
- Owners: Contract, Engine, Provider/Replay, Server
- Related: ENG-19, CON-04, INP-05, ADM-01, OQ-01, OQ-08~11, T-R19, T-R20, UC-10, SPEC §4.2~4.4, §4.9, §9

## Context

- 제품의 핵심은 특정 시나리오가 아니라 **여러 위험 시나리오(철판 균열, 벽 붕괴, 화재, 연기 등)가 공통으로 쓰는 인프라**다. 시나리오는 "모델이 내는 label + profile 시나리오 정의"로 추가되어야 하고, 시나리오마다 엔진이나 서버 코드를 바꾸지 않아야 한다.
- 계약(Observation/RiskDecision), `ObservationProvider` 인터페이스, label을 모르는 `DecisionEngine`, profile, 리플레이는 이미 시나리오와 무관하게 동작한다(TASK-02~13).
- 그러나 **한 카메라 프레임을 서버에서 여러 모델이 분석하면** 현재 SPEC은 올바르게 동작하지 않는다.
  1. Admission의 순서 키가 `(stream_id, source_kind)`다(SPEC §4.2). 크랙 모델과 화재 모델이 같은 `camera_0` 프레임에서 각각 `object_detection` 관측을 내면 seq가 섞여 두 번째 모델 관측이 `out_of_order`로 버려진다.
  2. Frame Assembly는 `required_kinds`가 모이면 프레임을 완성한다(SPEC §4.4). 크랙 모델 관측만으로 화재 시나리오 프레임이 완성되어, 화재 모델이 보지 않은 프레임이 화재 `absent`로 판정된다. 이는 G2(판단 불가를 안전으로 바꾸지 않음)를 깨뜨린다.
- 모델을 처음부터 학습하기는 어렵다. Hugging Face 등에서 공개된 사전 학습 모델을 우선 검토한다.
- 첫 실제 시나리오는 아직 정하지 않았다(OQ-01). 후보는 철판/벽 균열, 벽 손상, 화재, 연기이며, 사용할 수 있는 오픈 모델 조사(OQ-08) 결과를 보고 정한다. 이 ADR은 어떤 시나리오가 선택되어도 같은 인프라를 쓰도록 하는 결정이다.

## Decision

### 1. 시나리오는 데이터, 인프라는 공통
새 시나리오 추가 = (a) 해당 label을 내는 Provider와 모델 manifest, (b) profile 시나리오 1개, (c) 합성 fixture와 기대 결정. `DecisionEngine`, 계약, 서버 코드는 시나리오별로 바꾸지 않는다.

### 2. 관측의 모델 출처를 키에 포함한다
- 영상 계열 관측(`object_detection`, `track_state`)은 `model`이 반드시 있어야 한다. 관측을 낸 모델은 `model.id`로 식별한다.
- Admission의 순서 검사(seq, capture 시각)와 Window 키를 `(stream_id, source_kind, model_id)`로 바꾼다. `model`이 없는 관측(health 등)은 `model_id = ""`로 둔다.
- 같은 프레임을 분석한 모델들은 같은 `frame_id`를 쓰고, seq는 모델마다 독립적으로 증가한다.

### 3. 시나리오는 근거로 쓸 모델을 선언한다
- profile 시나리오에 `required_models: {<source_kind>: <model_id>}`를 추가한다. `required_kinds`의 모든 kind에 대해 모델을 지정해야 한다.
- Frame Assembly는 `stream_id ∈ required_streams`, `source_kind ∈ required_kinds`, `model.id == required_models[source_kind]`인 관측만 그 시나리오 버퍼에 넣는다.
- 결과: 화재 시나리오는 화재 모델이 처리한 프레임으로만 판정된다. 화재 모델이 멈추면 다른 모델이 돌고 있어도 화재 시나리오만 행 2(`input_stale`)로 `unknown`이 된다.

### 4. 모델 manifest에 label 목록을 둔다
SPEC §9의 모델 manifest에 `kind`(`object_detection` 등)와 `labels`(모델이 낼 수 있는 label 목록)를 추가한다. profile 검증은 시나리오의 `labels`가 `required_models` 모델의 `labels` 안에 있는지 확인한다(manifest를 읽을 수 있는 `voa.config`에서 수행, 엔진은 I/O 없음 유지). 관리 화면의 선택지도 이 목록에서 나온다.

### 5. 모델 조달: open-vocabulary detector + RT-DETR/RF-DETR 미세조정
두 종류의 Provider를 같은 계약으로 둔다. 둘 다 출력은 `object_detection`(label, bbox, score)이다.
- **open-vocabulary Provider (탐색용)**: 실행 시 텍스트 프롬프트로 클래스를 지정한다. 기본 후보는 OmDet-Turbo(`omlab/omdet-turbo-swin-tiny-hf`, 모델 카드 기준 Apache-2.0, 실시간 지향), 대체 후보는 OWLv2(`google/owlv2-base-patch16-ensemble`, Apache-2.0)와 Grounding DINO tiny(`IDEA-Research/grounding-dino-tiny`, Apache-2.0). 프롬프트 목록이 이 모델 manifest의 `labels`가 된다. 새 시나리오 후보는 재학습 없이 먼저 여기서 검증한다.
- **DETR 계열 미세조정 Provider (정확도용)**: RT-DETR(`PekingU/rtdetr_*`, Hugging Face transformers, 모델 카드 기준 Apache-2.0) 또는 RF-DETR(`rfdetr` 패키지, Apache-2.0인 크기만, segmentation 변형 포함)을 CC BY 4.0 등 허용 라이선스 데이터셋으로 미세조정한 체크포인트를 쓴다. `rfdetr[plus]`와 PML 라이선스 크기(Atto, Femto, Pico, XL, 2XL, NAS)는 제외한다. 학습 라벨이 manifest의 `labels`가 된다. 정확도가 필요한 시나리오는 profile의 `required_models`를 이 모델로 바꾼다. 둘 중 무엇을 쓸지는 같은 데이터로 비교해 정한다(OQ-11).
- 조사 결과 바로 쓸 수 있는 공개 전용 모델(크랙, 화재 등)은 대부분 Ultralytics YOLO 기반(AGPL)이거나 라이선스가 불분명해 기본 후보에서 제외한다. 
- 첫 실제 시나리오는 open-vocabulary Provider로 후보(철판/벽 균열, 벽 손상, 화재, 연기)를 시험한 결과를 보고 정한다(OQ-01, OQ-08).
- 가중치 다운로드는 사용자가 명시적으로 실행한다. 코드와 스크립트에서 자동 다운로드(`from_pretrained`, `hf download`)를 하지 않고 로컬 경로에서 읽는다.
- 코드, 가중치, 학습 데이터셋의 라이선스를 각각 확인하고 manifest에 근거를 남긴다. AGPL, NC, 비공개 API 의무는 기본 설치에서 제외한다(AGENTS 규칙 7).
- 실모델 테스트는 `@pytest.mark.model`로 분리하고 기본 실행은 mock으로 통과한다.

### 6. 감지 시나리오 관리 화면 (S 우선순위, Stage B 이후)
웹 관리 화면에서 profile 시나리오(대상 label, 근거 모델, 임계값, ROI, 경고 문구)를 편집한다. 저장 시 profile 검증을 통과해야 하고 `profile_version`을 올린다. 변경은 새로 시작하는 세션부터 적용한다(SPEC §4.6). label은 manifest 목록에서만 고르고, 경고 문구는 승인 후 적용하며, 변경자와 시각을 기록한다. 원본 미디어는 표시하거나 저장하지 않는다.

### 7. 유지하는 규칙
VLM은 위험을 판정하지 않는다(Stage E의 설명 전용). 수치 주장(균열 폭, 거리) 금지, 원본 미디어 저장 금지(기본값), 자동 신고 금지, 판단은 `DecisionEngine`만 한다.

## Alternatives

- **모델 결과를 하나의 관측으로 합치는 composite provider**: 키 변경이 필요 없지만, 모델 하나가 실패했을 때 "그 모델이 보지 못함"과 "대상 없음"을 구분할 수 없어 거짓 `absent`가 생긴다. 기각.
- **모델마다 다른 `stream_id` 사용**: 키 변경 없이 분리되지만 `stream_id`는 센서 이름이고 `sensor_down`이 센서 단위로 동작한다(SPEC §4.3). 카메라 하나가 죽어도 모델별 가짜 stream은 down으로 보이지 않는다. 기각.
- **비지도 이상 탐지로 시나리오 없이 탐지**: 시선에 따라 장면이 계속 바뀌는 스마트글래스에서는 "정상"을 정의할 수 없어 오경보가 과도하다. 기각.
- **VLM이 직접 위험 판정**: AGENTS 규칙 2와 근거 검증 원칙에 어긋난다. 기각.
- **바로 쓸 수 있는 공개 전용 모델(크랙, 화재, 연기 등)**: 조사한 모델 대부분이 Ultralytics YOLO(AGPL) 기반이거나 라이선스가 불분명하다. 기본 설치 후보에서 제외한다.
- **처음부터 학습**: 데이터와 비용 부담이 크다. open-vocabulary 검증과 DETR 계열 미세조정으로 부족할 때만 검토한다.

## Consequences

- 계약 변경: Observation 검증(영상 계열은 `model` 필수), profile 스키마(`required_models`), 모델 manifest(`kind`, `labels`). JSON Schema 스냅샷과 Android 계약 리뷰가 필요하다.
- 엔진 변경: Admission, Window, Frame Assembly 키와 필터. INV-07, INV-01/02 테스트와 TASK-11 불변식 생성기에 "같은 프레임을 두 모델이 분석, 한 모델만 정지" 경우를 추가한다(T-R19).
- 기존 profile, fixture, mock은 `required_models`와 `mock-detector`/`mock-tracker`로 갱신한다.
- 반영 순서: Stage A(TASK-14까지) 완료 직후 SPEC/PLAN을 갱신하고, 계약/엔진 변경을 새 Task로 수행한다. 실모델 Provider(Stage C)는 그 다음이다.
- 되돌림: `required_models`를 선택 필드로 되돌리고 키를 `(stream_id, source_kind)`로 복원한다. 단, 그 경우 다중 모델 구성은 지원하지 않는다.

## Verification

- 새 엔진 테스트: 같은 `frame_id`를 크랙/화재 두 모델이 분석할 때 두 관측 모두 `accepted`, 각 시나리오는 자기 모델 관측만 근거로 가짐. 화재 모델만 멈추면 화재 시나리오만 `unknown`, 크랙 시나리오는 영향 없음(T-R19).
- profile 검증: `required_models` 누락, manifest에 없는 label, kind 불일치 각각 거부.
- CON-03 contract suite: 실모델 Provider가 mock과 같은 suite를 통과하고 `model`을 항상 채움.
- 첫 실제 시나리오는 정상 장면의 FP/hour를 T-R20으로 측정한 뒤에만 성능을 말한다.

## SPEC/PLAN 반영 (Stage A 완료 직후)

- SPEC §3.3: 영상 계열 `model` 필수 규칙
- SPEC §4.2, §4.3: 키 `(stream_id, source_kind, model_id)`
- SPEC §4.4: `required_models` 필터
- SPEC §4.9: `required_models` 필드와 manifest label 검증
- SPEC §9: manifest `kind`, `labels`, 모델 조달 방침
- PLAN: 엔진/계약 변경 Task와 관리 화면 Task 추가
