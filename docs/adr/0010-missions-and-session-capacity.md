# ADR-0010: 미션 - 다목적 구성 단위와 동시 세션 용량

- Status: accepted (2026-11-26, 소유자 결정)
- Date: 2026-11-26
- Owners: Contract, Server, Engine(profile), Provider/Replay, Web
- Related: ENG-15, ENG-19, INP-05, COM-01, COM-02, NFR-03, ADR-0004, ADR-0008, ADR-0009, SPEC §4.9, §7.1, §7.5, §7.6, §7.7

## Context

- ADR-0009는 서버 쪽 추론과 관리자 API를 정했지만, 활성 profile이 **하나**뿐이라 시스템이 "화재 감시" 한 가지 용도에 묶인다.
- 소유자 요구(2026-11-26):
  - 여러 활용 분야에 쓸 수 있어야 한다.
  - 초기 페이지에서 목적 단위를 **등록, 수정, 삭제, 목록**으로 관리한다.
  - 하나를 고르면 그 목적에 특화된 모델들을 결합해 동작한다.
  - API도 이 목적 단위를 입력으로 받는다.
  - 동시 사용자 수도 설계에 포함한다.
- 용어 충돌: 엔진의 `scenario`는 판정 규칙 하나다(예: `fire_in_view` "불꽃이 연속 3프레임이면 경고"). 사용자가 고르는 상위 단위는 다른 이름이어야 한다.

## Decision

### 1. 용어: 미션(Mission)

- **미션**은 사용자가 고르는 목적 단위다. 예: "화재 감시", "통로 안전", "설비 균열 점검". 미션 하나는 다음을 묶는다.
  - 판정 규칙 `scenarios`: 지금의 profile 시나리오와 같다(SPEC §4.9). 여러 개를 넣을 수 있다.
  - 판정 파라미터 `params`: `min_score`, `confirm_frames`, `staleness_ms` 등(§4.9)
  - 근거 모델: 각 scenario의 `required_models`. 미션이 쓰는 모델 목록은 여기서 나온다(ADR-0004 다중 모델).
  - 판단 불가 고지 문구 `degraded_notice_text`
  - HUD 스타일 연결 `hud_style_map`(ADR-0009 결정 5)
  - 권장 프레임 간격 `frame_interval_ms`. 없으면 서버 기본값을 쓴다.
- 엔진은 바뀌지 않는다. **미션 1개의 한 버전 = 엔진 `PolicyProfile` 1개**다(`profile_id = mission_id`, `profile_version = 미션 버전`). 결정의 `policy_version`은 `<mission_id>@<version>`이다.
- 쓰지 않는 용어:
  - "태스크": PLAN의 TASK와 혼동된다.
  - "액션": 장치나 기계 제어로 오해되며, 이는 AGENTS 규칙 4의 금지 영역이다.
  - "감지": 모델 출력과 혼동된다.
  - "시나리오": 엔진 내부 규칙과 충돌한다.

### 2. 미션의 수명과 버전

- 상태는 `draft`(편집 중), `published`(장치가 고를 수 있음), `archived`(삭제됨) 세 가지다.
- **수정은 새 버전을 만든다.** `draft`를 검증해서 게시하면 버전이 1 올라간다. 이미 그 미션으로 동작 중인 세션은 시작할 때의 버전으로 끝까지 판단한다(ADR-0009 결정 3과 같다).
- **삭제는 보관(archive)이다.** 지난 결정의 `policy_version`이 그 미션 버전을 가리키므로 정의를 지우지 않는다. 진행 중인 세션이 쓰는 미션은 보관할 수 없다(409 `mission_in_use`).
- 게시할 때 검증은 다음 순서다. 하나라도 실패하면 게시되지 않고 이전 버전이 그대로 쓰인다.
  1. 스키마(§4.9)
  2. manifest 대조(`check_profile_models`)
  3. 근거 모델의 가중치 준비(sha256)
  4. 모델 배치
- 게시 시 근거 모델이 아직 배치되지 않았으면 서버가 **자동으로 배치**한다. 가중치가 준비되지 않았으면 400 `model_not_ready`다. 모델은 미션들 사이에 공유하고, 어느 게시 미션도 쓰지 않는 모델만 해제할 수 있다.
- 저장 위치: `VOA_ACTIVE_CONFIG_DIR/missions/<mission_id>/v<version>.yaml`(게시본)과 `draft.yaml`(편집본). 기존 `config/profiles/*.yaml`(`fire_c1`, `local_verify_c0`, `indoor_research_v0`)은 서버가 처음 시작할 때 같은 ID의 미션 v1로 가져온다.

### 3. 장치가 미션을 고르는 흐름 (API가 미션을 입력으로 받음)

1. `POST /v1/sessions {device_id, secret, capabilities, mission_id?}`: 세션을 만든다. `mission_id`를 주면 바로 그 미션에 묶는다.
2. `GET /v1/missions` (세션 access token): 게시된 미션 목록을 받는다. 필드는 `mission_id, name, description, version, scenarios[{id, labels, severity}], models[], frame_interval_ms`다. 초기 화면에서 이 목록을 보여 준다.
3. `PUT /v1/sessions/{session_id}/mission {mission_id}`: 고른 미션의 최신 게시 버전으로 세션을 묶는다.
   - 응답: `{mission_id, version, name, frame_interval_ms}`
   - 같은 세션에서 다른 미션으로 **바꿀 수 있다.** 바꾸면 그 세션의 엔진을 새로 만든다. 이전 미션의 진행 중 경고는 기한이 지나면 정리되고, 새 미션의 시나리오는 모두 `unknown`에서 시작한다.
   - 제어 WebSocket에 `mission` 메시지(`mission_id, version, name, frame_interval_ms`)를 보낸다.
4. 미션이 정해지지 않은 세션의 프레임 업로드는 409 `mission_not_selected`다.
5. **제어 WebSocket이 연결된 뒤에 프레임을 보내기 시작한다 (소유자 요구).**
   - 클라이언트는 `auth_ok`와 미션 확정(`mission` 메시지 또는 선택 응답)을 모두 받은 뒤에 캡처와 업로드를 시작한다.
   - 서버도 이를 강제한다. 그 세션의 제어 WebSocket이 열려 있지 않으면 업로드는 409 `control_not_connected`다.
   - 연결이 끊기면 클라이언트는 업로드를 멈추고, 재접속(`auth_ok`)한 뒤 다시 시작한다.
   - 이유: 경고를 받을 통로가 없는 상태에서 판정만 진행되면, 경고가 장치에 닿지 못한 채 `queued`로 쌓인다.

- 시뮬 페이지와 글래스 앱은 같은 순서를 쓴다. 초기 화면에서 미션을 고르고, 그 미션으로 감지를 시작한다.

### 4. 관리자 API의 미션 관리 (ADR-0009 §7.7의 profile API를 대체)

| 메서드와 경로 | 동작 |
|---|---|
| `GET /v1/admin/missions` | 목록(상태, 최신 버전, 사용 중인 세션 수) |
| `POST /v1/admin/missions` | 새 미션 만들기(`draft`). 기존 미션 복제도 가능(`from_mission_id`) |
| `GET /v1/admin/missions/{id}` | `draft`와 최신 게시본 |
| `PUT /v1/admin/missions/{id}` | `draft` 수정. 게시본은 바뀌지 않는다 |
| `POST /v1/admin/missions/{id}:validate` | `draft` 검증만 한다 |
| `POST /v1/admin/missions/{id}:publish` | 검증, 모델 자동 배치, 버전 증가 |
| `DELETE /v1/admin/missions/{id}` | 보관. 사용 중이면 409 `mission_in_use` |
| `GET /v1/admin/missions/{id}/versions` | 버전 목록과 각 버전 정의 |

- 모든 변경은 `admin_changes`에 남긴다. `action`은 `mission_create`, `mission_update`, `mission_publish`, `mission_archive`이고 `target`은 `mission_id`다.

### 5. 동시 세션 용량: 서버 1대에 3세션 (소유자 결정)

- `VOA_MAX_SESSIONS=3`: 동시에 존재하는 장치 세션(글래스, 시뮬)의 상한이다. 관리자 접속은 세지 않는다. 세션 정리(ADR-0005)로 빈자리가 생기면 다시 받는다.
  - 세션 생성이 상한을 넘으면 `POST /v1/sessions`가 503 `{"code":"capacity_exceeded","active":3,"max":3,"retry_after_s":30}`이고 `Retry-After` 헤더를 붙인다.
  - **WebSocket 연결 시 정원 초과 안내 (소유자 요구)**: 제어 WebSocket 연결 수도 같은 상한을 지킨다. 예를 들어 세션은 남아 있지만 연결을 새로 여는 경우다. 브라우저는 거절된 핸드셰이크의 HTTP 본문을 읽을 수 없으므로, 서버는 연결을 받은 뒤 `error(code="capacity_exceeded", active, max, retry_after_s)`를 보내고 close **4503**으로 닫는다.
  - 장치는 상태 표시줄에 "정원 초과"를 띄우고, 장치 내장 문구 "동시 접속 정원을 넘었습니다. 잠시 후 다시 시도하세요."를 1회 안내한다(SPEC §7.4). 프레임은 보내지 않고, `retry_after_s` 뒤에 다시 시도한다.
- 추론 자원:
  - 모델 인스턴스는 세션 사이에 공유한다.
  - 프레임은 세션마다 stream당 처리 중 1개다(ADR-0009).
  - 여러 세션의 프레임은 모델별 작업 큐에서 **세션 순서대로 번갈아(round-robin)** 처리한다. 한 세션이 다른 세션을 굶기지 않게 하기 위해서다.
- **부하 시 간격 늘리기**
  - 세션 프레임의 평균 대기(`queue_ms`)가 `frame_interval_ms`를 넘는 상태가 5초 이상 이어지면, 서버가 그 세션의 간격을 1.5배로 늘려 `config`로 알린다. 상한은 `VOA_FRAME_INTERVAL_MAX_MS`(기본 1000)다.
  - 대기가 간격의 절반 아래로 10초 이상 내려가면 미션 권장값까지 되돌린다.
  - 간격이 길어져 엔진의 `staleness_ms`를 넘으면 그 시나리오는 엔진 규칙대로 `unknown`이 된다. 부하를 숨기지 않는다.
- 용량 기준(측정 전 추정, 실측으로 확정): `세션 수 × 초당 프레임 × 미션의 모델 수 ≤ 측정한 추론 처리량 × 0.7`
  - 맥 CPU(RT-DETR R18 약 300 ms): 3세션 × 3fps × 1모델 = 초당 9회로, 추정 처리량을 넘을 수 있다. 3세션에서는 간격 늘리기가 동작할 것으로 예상한다.
  - 노트북 RTX 3070은 여유가 있을 것으로 예상한다. 값은 TBD다.
- `GET /v1/admin/capacity`: 세션 수와 상한, 모델별 처리량(최근 1분), 세션별 실제 간격과 평균 대기를 보여 준다.

### 6. 비동기 결과와 머리 움직임 (ADR-0009 결정 5 보강)

업로드부터 응답까지 약 0.35초가 걸린다. 그사이 머리를 돌리면 서버가 준 박스 위치는 현재 시야와 달라진다.

- **글래스 HUD에는 박스나 위치를 가리키는 표시를 그리지 않는다.** 경고는 위치와 무관한 문구로 한다(측정 근거 없는 방향과 거리 주장 금지, AGENTS 규칙 1).
- **시뮬 화면은 박스를 "판정한 그 프레임" 위에 그린다.**
  - 브라우저는 보낸 프레임을 `frame_id`로 메모리에 잠깐 들고 있다가, 응답이 오면 그 프레임 위에 박스를 그려 "판정 프레임" 창에 보여 준다. 표시한 뒤 버린다.
  - 창에 프레임의 나이(촬영 후 경과 시간)를 함께 표시한다.
  - 실시간 영상 쪽에는 글래스와 같은 HUD 문구와 상태 표시줄만 그린다.
- **머리 움직임 품질 플래그**
  - 업로드 헤더에 선택 필드 `X-VOA-Quality-Flags`(QualityFlag 쉼표 목록, §3.2)를 둔다.
  - 장치는 IMU 회전 속도가 기준을 넘으면 `camera_motion_uncompensated`를 붙인다. IMU가 없는 기기는 붙이지 않는다.
  - 서버는 이 플래그를 관측의 `quality_flags`에 그대로 넣는다. 엔진의 `blocking_flags` 규칙(§4.5)이 그 프레임을 근거에서 빼므로, 흔들린 프레임 때문에 경고가 잘못 확정되거나 해제되지 않는다.
- 나중에 위치 표시(예: 왼쪽 화살표)가 필요하면, 서버가 촬영 시점 기준의 대략적인 방향을 주고 장치가 IMU로 촬영 후 회전량을 보정해야 한다. 이는 OQ-12로 남긴다(IMU 제공 여부 OQ-03, Stage F).

## Alternatives

- **미션 없이 활성 profile 하나(ADR-0009 원안)**: 단순하지만 여러 용도를 동시에 운영할 수 없다.
- **장치마다 profile 파일 배포**: 중앙 관리가 안 되고 글래스 연동이 어려워진다.
- **미션 수정 즉시 반영**: 진행 중 판단의 근거 추적이 깨진다. 버전과 새 세션 적용을 택했다.
- **동시 10세션 이상**: GPU 배치 추론과 분산이 필요해 지금 범위를 넘는다. 3세션으로 시작하고 실측 후 다시 정한다.

## Consequences

- 장점:
  - 하나의 서버와 API로 여러 용도를 운영한다. 새 용도는 미션 등록(규칙, 모델, 임계값, HUD)만으로 추가한다(ADR-0004 원칙 유지).
  - 엔진과 결정 계약은 바뀌지 않는다.
- 비용:
  - 미션 저장소, 버전, 게시 흐름, 자동 배치, 세션별 엔진 재생성, 용량 관리가 추가된다.
  - 기존 `VOA_POLICY_PROFILE` 단일 설정은 "기본 미션"(`VOA_DEFAULT_MISSION`)으로 바뀐다. `mission_id`를 생략한 세션은 기본 미션을 쓴다.
- 되돌림: 미션이 하나뿐이고 기본 미션만 쓰면 지금 동작과 같다.
- **Android 연동**: Android 참조 클라이언트도 이 결정에 맞춰 개발해야 한다(`docs/05-android-client-plan.md` AND-06A, 소유자 지시 2026-11-26).

## Verification

- 미션:
  - CRUD, 복제, 검증 실패 시 이전 게시본 유지, 게시 시 버전 증가와 모델 자동 배치
  - 가중치 미준비면 `model_not_ready`, 사용 중 보관은 409
  - 보관한 미션의 지난 결정 `policy_version`이 계속 해석됨
  - 기존 profile 3개를 미션 v1로 가져오기
- 세션:
  - 미션 지정 생성, 목록 조회, 선택, 전환(엔진 재생성, 모든 시나리오 `unknown`에서 시작)
  - 미선택 업로드는 409 `mission_not_selected`, 제어 WebSocket 없이 업로드는 409 `control_not_connected`, `mission` 메시지 수신
  - 진행 중 세션은 게시 후에도 이전 버전 유지
- 용량:
  - 4번째 세션은 503 `capacity_exceeded`와 `Retry-After`, 정원 초과 WebSocket은 `error(capacity_exceeded)` 후 close 4503, 세션 정리 후 다시 허용
  - 세션 간 round-robin(한 세션이 많이 보내도 다른 세션이 처리됨)
  - 대기 증가 시 간격 1.5배와 `config`, 회복 시 되돌리기
  - 간격이 `staleness`를 넘으면 `unknown`
- 머리 움직임:
  - `X-VOA-Quality-Flags: camera_motion_uncompensated` 프레임은 근거에서 빠짐(`quality_blocked`)
  - 알 수 없는 플래그는 400 `frame_rejected`
- 화면: 초기 미션 목록과 선택, 미션 편집기, 판정 프레임 창의 박스와 프레임 나이
