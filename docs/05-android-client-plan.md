# android client plan - Android 클라이언트 구현 계획

> 상태: AND-01 완료, AND-02부터 C2 계약으로 재계획(2026-11-26)
> 범위: Android 기준 클라이언트, 에뮬레이터 우선, 실기기 SDK 어댑터 분리
> 근거: `01-product-requirements.md` DEV-01~05, COM-01~04, DAT-02~04, `02-technical-specification.md` §7.1~7.4

## 1. 목적

Android 클라이언트는 위험을 판단하지 않는다. 입력 수집과 전송, 서버 경고 출력, ACK, 연결 감시만 담당한다. 판단 규칙과 시나리오 상태는 서버의 `DecisionEngine`에만 둔다.

첫 목표는 특정 스마트글래스 SDK에 묶인 앱이 아니다. 표준 Android 에뮬레이터와 휴대폰에서 서버 Stage B 계약을 검증하는 참조 클라이언트를 먼저 만든다. 실제 글래스는 기종이 확정된 뒤 입력과 출력 어댑터만 교체한다.

## 2. 범위

### 2.1 1차 범위

- Kotlin Android 앱 스캐폴드
- 장치 등록 정보를 사용한 세션 생성
- WebSocket 인증, heartbeat, watchdog, 재연결, `alert_sync`
- `alert`, `notice`, `decision`, `admission`, `heartbeat`, `error` 메시지 파싱
- HUD, TTS, haptic 출력 어댑터
- `received` 및 `played` ACK 분리
- `alert_id` 기준 중복 재생 방지
- 카메라 상태 `health` 관측 전송
- 합성 `ObservationDraft` 전송을 사용한 에뮬레이터 E2E

### 2.2 후속 범위

- Stage C: CameraX 또는 기기 SDK 카메라 프레임 어댑터, IMU 배치 전송
- Stage D: 마이크 PCM 배치, PTT 시작과 종료 이벤트
- Stage F: 기종별 HUD, 버튼, 오디오, 진동 SDK 어댑터

### 2.3 범위 밖

- Android 단의 위험 판단
- 얼굴, 신원, 의도 추론
- 서버 없이 새로운 경고 생성
- 원본 이미지와 음성의 파일 저장
- 백그라운드 자동 녹음
- 실기기 종류가 정해지기 전의 vendor SDK 종속 구현

## 3. 구현 전 확정 게이트

Android 구현은 아래 항목이 서버 계약에 반영된 뒤 시작한다.

### GATE-A: 서버 인증과 재접속 계약

- 계약 상태: `02-technical-specification.md` §7.1에 확정
- `secret_scrypt`는 16바이트 salt와 32바이트 digest를 분리 저장
- access token은 메모리에만 유지하고 refresh token은 Android Keystore key로 보호한 앱 전용 저장소에 보관
- access token 만료 또는 프로세스 재시작 후는 REST refresh endpoint(`/v1/sessions/{id}/refresh`)로 token을 회전한 뒤 WebSocket `auth`를 수행
- 서버 재시작으로 refresh가 401이면 장치 credential로 새 세션 생성
- 재생 완료 `alert_id` 보존 범위는 현재 세션과 refresh token 만료 시각까지로 제한

### GATE-B: 메시지 JSON Schema

- 계약 상태: `02-technical-specification.md` §7.2~7.5에 envelope, error, ACK, status API를 확정
- TASK-18에서 모든 C→S, S→C 메시지를 `voa.contracts.messages`의 Pydantic 모델과 JSON Schema 스냅샷으로 발행
- Android가 사용할 positive fixture와 negative fixture 제공
- 에러 코드, WebSocket close code, 늦은 ACK 상태 전이를 명시

### GATE-C: Android 제품 기준

- package name
- min SDK, target SDK
- 1차 참조 장치 또는 에뮬레이터 이미지
- HUD, speaker, haptic 중 필수 출력
- 실행 시작은 사용자가 보이는 Activity에서 명시적으로 한다. Android 12 이상의 백그라운드 foreground service 시작 제한과 Android 14 이상의 camera, microphone while-in-use 권한 제한을 회피하기 위한 조건이다.

## 4. 권장 구조

```text
apps/android/
  app/                    Activity, foreground service, DI composition
  protocol/               API request, WS message, JSON fixture
  session/                session, auth, heartbeat, watchdog, alert_sync
  output/                 HUD, TTS, haptic, ACK aggregation
  input-mock/             synthetic ObservationDraft source
  input-media/            Stage C/D camera, IMU, microphone, PTT adapters
  device-adapter/         vendor-specific glasses SDK boundary
  test-support/           fake clock, fake transport, fake outputs
```

의존성 방향은 `app -> session -> protocol` 및 `app -> output`으로 유지한다. `device-adapter` 모듈은 앱 코어에 vendor 타입이 퍼지지 않도록 인터페이스를 구현한다.

WebSocket, JSON, 암호화 저장소 라이브러리는 Android 스캐폴드 Task에서 후보를 비교한 후 하나를 고른다. 선택 전에 라이선스, 오프라인 빌드, min SDK 지원, 테스트 대역 가능 여부를 확인하고 `02-technical-specification.md` §9 등록부를 먼저 갱신한다.

## 5. 세션 상태 모델

```text
STOPPED
  -> PROVISIONED
  -> SESSION_CREATING
  -> WS_AUTHENTICATING
  -> ACTIVE
  -> DEGRADED
  -> RECONNECTING
  -> ACTIVE
  -> STOPPED
```

- `STOPPED`: 사용자가 시작하지 않았거나 명시적으로 종료한 상태
- `PROVISIONED`: device ID와 credential을 사용할 수 있는 상태
- `SESSION_CREATING`: REST session 생성 중
- `WS_AUTHENTICATING`: WebSocket 연결 후 5초 안에 `auth` 전송 중
- `ACTIVE`: heartbeat 수신, 관측 송신, 경고 출력 가능
- `DEGRADED`: 일부 입력 또는 출력 불가, health 관측과 UI 상태로 표현
- `RECONNECTING`: 1, 2, 4, 8초 backoff로 재연결 시도

상태 전이와 timeout은 `Clock` 인터페이스를 통해 monotonic 시각으로 계산한다. Android 구현은 `SystemClock.elapsedRealtime()` 기반으로 하고, 테스트는 fake clock을 주입한다. 벽시계 변경이 watchdog에 영향을 주지 않아야 한다.

## 6. 핵심 컴포넌트

### 6.1 `SessionApi`

- `POST /v1/sessions` 요청
- `POST /v1/sessions/{session_id}/refresh` token 회전 요청 (ADR-0005)
- 입력과 출력 capability 선언
- 401, `capability_not_allowed`, `no_output_capability` 구분
- access token은 메모리에만 유지하고 refresh token은 Keystore key로 보호

### 6.2 `StreamClient`

- WebSocket 연결 및 첫 `auth` 메시지 강제
- 재연결은 REST token 회전, WebSocket `auth`, `alert_sync(last_alert_seq)` 순서로 수행 (ADR-0006)
- 수신 메시지를 타입별 handler로 라우팅
- 원문 입력을 에러 로그로 반환하지 않음
- 연결 종료 시 원인을 제한된 error code로만 기록

### 6.3 `Watchdog`

- 서버의 모든 메시지 수신 시 monotonic deadline 갱신
- `watchdog_ms` 동안 무수신이면 내장 문구를 선언된 모든 출력으로 1회 고지
- 재연결 중 동일 단절 episode에서 중복 고지 금지
- 첫 heartbeat 수신 후 음성 없이 복구 상태 표시

### 6.4 `AlertDeduplicator`

- `alert_id`별 `received`, `played`, 최종 `alert_seq` 관리
- 이미 `played`인 alert는 렌더링하지 않고 `played` ACK만 재전송
- 프로세스 재시작 후의 보존 규칙은 GATE-A에서 확정
- 저장하더라도 alert 문구, 관측 payload, token은 저장하지 않음

### 6.5 `OutputRouter`

- HUD: 현재 경고 문구와 degraded 상태를 별도 레이어로 표시
- TTS: `utteranceId=alert_id`를 사용하고 완료 callback으로 출력 종료를 확인
- TTS 없음 또는 초기화 실패: 사전 포함된 고정 경고음으로 fallback
- haptic: alert 패턴과 degraded 패턴을 분리
- 출력은 선언한 모든 capability에 요청하되, `played` ACK는 하나 이상의 출력이 완료된 뒤 전송
- 새 alert는 notice 음성을 중단할 수 있지만 notice는 alert를 중단하지 못함

### 6.6 `HealthReporter`

- 카메라, 마이크, 출력 어댑터의 상태를 관찰
- 선언한 입력이 사용 불가하면 `health(status="down")` 전송
- 복구 후 `health(status="ok")` 전송
- detail code는 정해진 허용 목록만 사용하고 예외 문구를 넣지 않음

## 7. 보안과 프라이버시

- 장치 credential은 Android Keystore의 비출력 key로 암호화한 앱 전용 저장소에 보관한다.
- access token, secret, refresh token, 관측 payload, alert text를 로그에 남기지 않는다.
- debug 앱의 cleartext 허용은 에뮬레이터 호스트와 개발 바인드에만 제한한다. pilot 빌드는 TLS 실패 시 연결하지 않는다.
- 원본 미디어는 메모리 큐를 넘어 파일, cache, 백업, crash attachment로 남지 않는다.
- crash report는 기본 OFF로 두고, 외부 서비스를 자동 추가하지 않는다.
- 앱이 중단되면 카메라와 마이크 자원을 즉시 해제한다.

## 8. Android 생명주기 제약

세션은 사용자가 보이는 화면에서 명시적으로 시작한 foreground service가 소유한다. camera와 microphone을 쓰는 후속 단계에서는 선언한 service type과 runtime permission을 모두 검증한다.

- 권한 거부: 해당 capability를 세션 요청에서 제외하거나 health down 전송
- 권한 회수: 입력 즉시 중단, health down, UI degraded 표시
- 앱 프로세스 종료: 서버가 기존 세션을 신뢰하지 않도록 재접속 계약(ADR-0002, ADR-0005, ADR-0006)에 따라 복구
- 부팅 자동 시작: 사용자 의도와 Android 제한을 모두 확정하기 전에는 구현하지 않음

## 9. 작업 패키지

### AND-00: 서버 계약 결정 보완

- 선행: TASK-17~20 설계 전
- 산출물: GATE-A/B 결정, message JSON Schema, 공유 fixture
- 수용: Android 계약 테스트가 서버 positive/negative fixture를 그대로 파싱

### AND-01: Android 스캐폴드와 계약 클라이언트

- 선행: TASK-18, AND-00
- 산출물: Gradle 모듈, protocol model, serializer, fake transport, lint/test 명령
- 수용: 외부 서버 없이 positive/negative JSON fixture 테스트 통과
- 실패: 알 수 없는 필드, 타입, enum을 거부하고 원문을 로그로 남기지 않음
- 결손: 선택 필드 누락 fixture 통과

> **2026-11-26 재계획 (소유자 결정):** AND-01 뒤의 옛 AND-02~06은 Stage B 계약(장치 secret, 첫 메시지 `auth`, 합성 `observation`)을 기준으로 했다. 그 계약은 Stage C2(ADR-0009, ADR-0010, ADR-0011)로 바뀌었으므로, 옛 AND-02~06과 AND-06A를 아래 AND-02~05로 다시 나눈다. 화면 흐름은 시뮬 페이지(`/ui/sim`)와 같다: **로그인(가입) → 미션 선택 → 감지 화면**. 빌드는 이 맥에 설치된 Android SDK와 JDK 17, `gradlew`로 하고 설치는 adb로 한다(서버 Docker 규칙의 예외, 호스트에 새 설치 없음). 실기기는 `adb reverse tcp:8765 tcp:8765`로 폰의 `127.0.0.1:8765`를 개발 맥 서버에 잇는다(루프백이라 TLS 없이 허용).

### AND-02: 로그인, 세션, 제어 WebSocket

- 상태: DONE (2026-11-26, S22 실기기에서 가입, 승인 후 로그인, 제어 연결 인증, 끊김 후 재연결 시도, 세션 소멸 시 로그인 화면 복귀 확인)

- 선행: AND-01, TASK-40, TASK-40A
- 산출물:
  - `protocol`: C2 계약 모델(세션 생성은 계정 또는 장치, `capabilities.hud`, 응답의 `frame_interval_ms`, `mission`), 가입, `mission`, `config`, `frame_result`, `error`의 정원 필드, `alert`/`notice` `presentation`, `heartbeat.frames`. `auth` 메시지 삭제
  - `session`: `SessionApi`(가입, 세션 생성, refresh), `ControlChannel`(핸드셰이크 `Authorization: Bearer`, `auth_ok` 후 `alert_sync`, heartbeat watchdog, 1/2/4/8초 재접속과 token 갱신, close 4503 정원 초과, 4401 로그아웃)
  - `app`: 서버 주소, 로그인, 가입 화면과 연결 상태 표시. cleartext는 `127.0.0.1`/`localhost`만 허용
- 수용: 정상 로그인과 연결, 401/403(`account_pending`, `password_change_required`)/429/503 정원 초과 문구, 핸드셰이크 헤더, watchdog 경계, 재접속 backoff, 비밀번호와 token이 `toString`, 로그, 예외 문구에 없음

### AND-03: 미션 선택과 프레임 업로드

- 상태: DONE (2026-11-26). 로그인과 미션 선택은 세로 고정, 감지 화면은 가로 고정(sensorLandscape, 전체 화면)이고 장치 이름은 입력받지 않고 시스템 기기 이름(없으면 제조사와 모델)을 쓴다(소유자 요구). S22에서 server-vision과 화재 미션으로 폰 카메라가 맥 화면의 산불 영상을 비춰 업로드 수백 장, 실패 0, 왕복 약 380 ms, 추론 약 365 ms, 서버 판정 warning 확인

- 선행: AND-02, TASK-41, TASK-42
- 산출물: 미션 선택 화면(`GET /v1/missions`, `PUT .../mission`), 감지 화면의 CameraX 미리보기, `FrameUploader`(긴 변 640px JPEG, 진행 중 1개, `frame_interval_ms`와 `config`/`mission` 반영, `auth_ok`와 미션 확정 전에는 0건, 202 후 `frame_result`)
- 수용: 연결 전 업로드 0건, 간격과 진행 중 1개, 409 `frame_busy`는 오류 아님, 연속 3회 실패 집계, 프레임이 파일과 캐시에 남지 않음

### AND-03A: 로그인과 연결 분리 반영

- 선행: AND-03, TASK-47
- 산출물: 로그인은 `/v1/auth/login`(로그인 token만, 정원 차지 안 함), 미션 목록은 로그인 token, 미션을 고르면 `POST /v1/sessions`(로그인 token + `mission_id`)로 연결, 감지 화면을 나가면 `DELETE /v1/sessions/{id}`로 해제, 서버가 끝낸 세션은 미션 화면으로
- 수용: 시뮬 페이지와 같은 시나리오(미션 화면 정원 0, 선택 시 1, 미션 변경 시 0)

### AND-04: HUD 출력, 상태 표시줄, ACK

- 선행: AND-03, TASK-44B
- 산출물: `presentation` 렌더러(위치, 색, 깜빡임 축소, 유지 시간), TTS, 진동, `received`/`played` ACK와 중복 방지, 상태 표시줄 규칙표, 정원 초과와 연결 끊김 내장 문구
- 수용: 시뮬 페이지와 같은 규칙표 단위 테스트(축소, 상태 표시줄 우선순위), 같은 `alert_id`는 1회 렌더링

### AND-05: 실기기 E2E

- 선행: AND-04, TASK-46
- 산출물: S22에서 `server-vision`과 화재 미션으로 로그인 → 미션 → 카메라 → 경고 → `played`
- 수용: 서버 DB의 `played`, 상태 표시줄 전이, Logcat과 앱 저장소에 비밀번호, token, 프레임 없음

### (대체됨) AND-06A: Stage C2 서버 계약 반영

> 2026-11-26 재계획으로 아래 내용은 AND-02~05에 나눠 넣었다. 계약 변경표는 참고용으로 남긴다.

서버 쪽 추론, 미션, 계정 도입으로 Android 참조 클라이언트의 접속 방식이 바뀐다. 서버 Task(TASK-40~46)와 **같은 계약으로 맞춰 개발해야** 한다(소유자 지시, 2026-11-26). 시뮬 페이지(`/ui/sim`)와 Android 앱은 같은 API를 쓰므로, 시뮬 페이지가 통과한 시나리오를 Android 계측 테스트로 그대로 다시 확인한다.

- 선행: TASK-40(핸드셰이크 인증), TASK-40A(계정), TASK-41(미션), TASK-42(프레임 업로드), TASK-43(정원), TASK-44(`presentation`), AND-06
- 변경점:

| 항목 | 지금 (AND-02~04) | 바꿀 계약 | SPEC |
|---|---|---|---|
| 로그인 | 장치 `device_id`, secret | 착용자 계정: 가입 화면, 로그인(`username`, `password`), 승인 대기(403 `account_pending`), 시도 제한(429) 안내. 장치 credential은 무인 장치용으로 유지 | §7.1, ADR-0011 |
| WebSocket 인증 | 연결 후 첫 메시지 `auth` | **핸드셰이크에서 `Authorization: Bearer`**(OkHttp 헤더), 실패는 HTTP 401. 루프백이 아니면 wss만 허용 | §7.1, §7.2, ADR-0009 |
| 미션 | 없음 | 로그인 후 **초기 화면에서 `GET /v1/missions` 목록과 선택**(`PUT /v1/sessions/{id}/mission`), `mission` 메시지, 미션 전환 | §7.1, §7.2, ADR-0010 |
| 프레임 전송 | `observation`(합성) | **`auth_ok`와 미션 확정 후에만** 카메라 프레임을 JPEG(긴 변 640px 이하)로 `POST /v1/sessions/{id}/frames`, 진행 중 1개, `frame_interval_ms`와 `config` 반영, 끊기면 중지 | §7.2, §7.6 |
| 품질 플래그 | 없음 | IMU 회전 속도가 기준을 넘으면 `X-VOA-Quality-Flags: camera_motion_uncompensated` | §7.6, ADR-0010 |
| 결과 | 없음 | 업로드 응답과 `frame_result`의 `states`, `timing`(왕복 지연 기록). 박스는 HUD에 그리지 않음 | §7.4, §7.6 |
| HUD | 고정 렌더링 | `alert`/`notice`의 **`presentation` 규칙표대로**(문구, 위치, 색, 전체 화면 또는 테두리 깜빡임, 유지 시간, 음성, 진동), `capabilities.hud` 선언과 축소 규칙, **상태 표시줄**(정원 초과 > 연결 끊김 > 서버 오류 > 응답 지연 > 경고 > 판단 불가 > 확인 중 > 정상) | §7.4, ADR-0009 결정 5 |
| 정원 초과 | 없음 | 세션 생성 503 `capacity_exceeded`와 `Retry-After`, 제어 WS `error(capacity_exceeded)` 후 close 4503 → "정원 초과" 표시와 내장 문구 1회, `retry_after_s` 뒤 재시도 | §7.1, §7.2, ADR-0010 |

- 산출물: `protocol` 모듈 계약 갱신(인증, 미션, `frame_result`, `config`, `mission`, `presentation`, 정원 초과), `session` 모듈 로그인과 가입 화면, 미션 선택 화면, `input-media` 프레임 업로더, `output` HUD 렌더러와 상태 표시줄
- 수용:
  - 계약 테스트(`contracts/schemas` 스냅샷)
  - 가짜 서버로 로그인 → 미션 선택 → 연결 후 업로드 → 경고 → `played`
  - 연결 전 업로드 0건, 정원 초과 안내, `presentation` 축소 규칙과 상태 표시줄 우선순위 단위 테스트
  - 프레임과 비밀번호가 로그, 파일, 캐시에 남지 않음
  - 시뮬 페이지와 같은 시나리오를 계측 테스트로 재현

### AND-07: Stage C camera/IMU 어댑터

- 선행: AND-03(프레임 업로드 계약, ADR-0009)
- 산출물: camera frame source(JPEG 스냅샷, `frame_interval_ms`), IMU 회전 속도 → `camera_motion_uncompensated` 플래그, memory-only, 진행 중 1개
- 수용: 세션 종료 후 프레임 참조 0건, cache 파일 0건, provider OFF 시 AND-06 통과

### AND-08: Stage D microphone/PTT 어댑터

- 선행: Stage D 메시지 계약 확정, AND-07
- 산출물: PCM source, PTT event source, health integration
- 수용: PTT 구간 밖 음성은 명령 입력으로 쓰지 않음, partial ASR은 행동을 트리거하지 않음

### AND-09: 스마트글래스 기종 어댑터

- 선행: OQ-02, OQ-03 결정, AND-07 또는 AND-08
- 산출물: vendor camera, HUD, button, audio, haptic adapter
- 수용: core 모듈 변경 없이 vendor 모듈 교체, 기종별 capability matrix 문서화

## 10. 검증 매트릭스

| 계층 | 검증 |
|---|---|
| JVM unit | session 상태, fake clock watchdog, backoff, dedupe, ACK aggregation, health 전이 |
| Contract | 서버 JSON Schema positive/negative fixture, 알 수 없는 enum과 extra field 거부 |
| Android instrumented | TTS callback, HUD lifecycle, haptic capability, permission 거부와 회수 |
| Integration | Docker 서버, Android 에뮬레이터, session→WS→alert→ACK |
| Resilience | 네트워크 단절, 서버 중단, 프로세스 종료, 중복 alert, 늦은 ACK |
| Privacy | Logcat, app storage, crash output, 서버 로그의 금지 데이터 검사 |
| Compatibility | 확정된 min SDK, target SDK, 참조 실기기 각 1개 이상 |

## 11. 완료 기준

- Android 앱은 판단 로직 없이 서버 계약만 구현한다.
- 외부 네트워크, 실모델, 실제 미디어 없이 AND-06 E2E가 통과한다.
- 중복 alert 재생은 0건이고 received와 played가 구분된다.
- watchdog 고지는 단절 episode당 1회이고 재연결 후 alert_sync를 보낸다.
- 카메라 권한 회수와 장치 오류가 unknown 경로로 연결된다.
- token, secret, raw media, OCR/STT 전문이 로그와 저장소에 없다.
- 추가 의존성은 허용적 라이선스이고 `02-technical-specification.md` §9에 등록된다.

## 12. 주요 위험과 완화

| 위험 | 완화 |
|---|---|
| Android foreground service 제한 | 보이는 Activity에서 명시적 시작, service type과 권한 매트릭스 테스트 |
| vendor SDK에 코어 종속 | `device-adapter` 경계, fake adapter로 core 테스트 |
| 재연결 후 중복 재생 | `alert_id` 저장 범위를 GATE-A에서 확정, dedupe contract test |
| TTS 없음 또는 callback 누락 | 고정 경고음 fallback, fake TTS timeout test |
| 서버와 Android schema drift | 서버가 발행한 공유 JSON fixture를 양쪽 CI에서 사용 |
| 원본 미디어 유출 | memory-only queue, app storage 검사, crash attachment OFF |
| 배터리와 열 예산 미정 | 실기기 확정 전에 수치 주장 금지, Stage F에서 측정 로그 수집 |

## 13. 공식 Android 근거

- Foreground service 백그라운드 시작 제한: <https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start>
- Foreground service type과 권한: <https://developer.android.com/develop/background-work/services/fgs/declare>
- Android Keystore: <https://developer.android.com/privacy-and-security/keystore>
- TTS 완료 callback: <https://developer.android.com/reference/android/speech/tts/TextToSpeech>
- 에뮬레이터의 호스트 loopback `10.0.2.2`: <https://developer.android.com/studio/run/emulator-networking-address>
- 네트워크 변경 callback: <https://developer.android.com/develop/connectivity/network-ops/reading-network-state>
