# ADR-0009: 서버 쪽 추론, 프레임 전송 방식, 관리자 설정 API

- Status: accepted (2026-11-26, 소유자 결정 반영)
- Date: 2026-11-26
- Owners: Contract, Server, Provider/Replay, Engine(profile), Android
- Related: INP-01, INP-03, INP-05, COM-02, ENG-15, ENG-19, DAT-01, DAT-02, NFR-03, ADR-0004, ADR-0007, ADR-0008, SPEC §4.9, §7.2, §7.4, §8.1, §8.2, §9

## Context

- 지금까지(Stage C0, TASK-35 초안)는 ADR-0007의 임시 예외대로 **장치 쪽에서 추론**하고 서버에는 탐지 결과(`observation`)만 보냈다. 이 구조에서는 스마트 글래스 앱마다 모델과 추론 런타임이 들어가야 하고, 모델을 바꾸면 모든 장치를 다시 배포해야 한다. 글래스 SDK와 연동하기 어렵다.
- 소유자 요구(2026-11-26):
  1. **백엔드가 먼저**이고, 장치와 화면은 백엔드 API만 호출한다. 이렇게 해야 나중에 스마트 글래스 연동이 쉽다.
  2. **관리자 페이지**에서 API로 어떤 모델을 배치할지, 모델을 여러 개 배치할지, 임계값을 어떻게 할지 설정한다.
  3. **시뮬 페이지**는 웹캠이나 동영상에서 주기적으로 캡처해 올리고, 서버의 판정(탐지 박스와 경고)을 받는다.
- SPEC은 원래 Stage C의 목표로 `camera_0`의 `frame`(JPEG, 긴 변 640px 이하) 메시지와 서버 Provider를 예약해 두었다(§7.2 예약 메시지, §7.4 입력 표). 이 ADR은 그 예약을 대체한다. 프레임은 WebSocket `frame` 메시지가 아니라 HTTP 업로드로 받는다(결정 1).
- 측정 사실: RT-DETR R18 추론은 M1 Docker CPU에서 프레임당 약 300~400 ms(약 3fps)였다(ADR-0007, ADR-0008). 엔진의 시간 규칙은 `confirm_frames 3`, `staleness_ms 1000`, `unknown_after_ms 1500`이다. 즉 **판정에 필요한 프레임 속도는 초당 2~5장**이고 30fps 영상은 필요 없다.

## 핵심 결정 1: 영상 전송 방식

### 요구 조건
- 판정에 필요한 속도는 **초당 2~5장**이다(엔진 `confirm_frames 3`, `staleness_ms 1000`, CPU 추론 약 300 ms).
- 프레임마다 `frame_id`와 촬영 시각이 붙어야 한다. 관측 하나가 근거(evidence) 하나이기 때문이다(ADR-0004).
- 경고, notice, ACK는 **프레임 전송 때문에 늦어지면 안 된다.**
- 추론이 밀리면 오래된 프레임을 쌓지 말고 최신 프레임만 처리한다.
- 같은 API를 브라우저 시뮬 페이지와 Android 글래스 앱이 모두 쓸 수 있어야 한다.
- 원본 미디어를 저장하지 않는다(AGENTS 규칙 5).

### 비교

| 기준 | A. HTTP 업로드 + 결과 폴링 | **B. HTTP 업로드(응답에 결과) + 제어 채널 분리** | C. WebSocket 하나에 이미지와 제어를 함께 | D. gRPC 양방향 스트리밍 | E. WebRTC (서버가 영상을 계속 보고 결과를 스트리밍) |
|---|---|---|---|---|---|
| 방식 | 프레임마다 `POST`, 결과는 `GET`을 반복 호출 | 프레임마다 `POST`(본문 = JPEG). **응답에 그 프레임의 탐지 결과**가 옴. 경고, ACK, heartbeat는 기존 세션 WebSocket(제어 전용) | 세션 WebSocket에 JPEG 바이너리와 경고를 같이 보냄 | HTTP/2 위 양방향 스트림(protobuf) 하나로 프레임 보내고 결과 받음 | 장치가 영상 트랙(H.264/VP8)을 보내고, 서버가 디코딩해 2~5fps로 샘플링해 추론. 결과는 DataChannel로 계속 보냄 |
| 경고 지연 | 폴링 주기만큼 늦음. 결국 푸시 채널이 따로 필요 | 제어 채널이 작아서 즉시 | **프레임 전송 뒤에 줄을 섬**(50 KB 프레임이면 1 Mbps에서 약 0.4초) | 스트림 단위로 섞여 나가 대체로 즉시. TCP 수준의 막힘은 남음 | 즉시(DataChannel) |
| 밀릴 때 처리 | 클라이언트가 응답을 기다림 | **진행 중인 요청을 1개로 제한**해, 응답이 와야 다음 최신 프레임을 보냄. 처리 속도에 맞춰 자동으로 fps가 조절되고 대기열이 안 생김 | 서버가 최신 프레임만 처리(직접 구현) | 스트림 흐름 제어 + 직접 구현 | 지터 버퍼와 디코더가 지연을 흡수하므로 오래된 프레임이 쌓일 수 있음. 샘플링 직접 구현 |
| 프레임 단위 근거 | 가능 | **가능**(요청 헤더에 `frame_id`, 촬영 시각) | 가능 | 가능 | RTP 타임스탬프로 재구성 필요 |
| 대역폭 (640x360, 3fps) | 약 1 Mbps + 요청 헤더 | 약 0.7~1.2 Mbps + 요청 헤더(keep-alive로 수백 바이트) | 약 0.7~1.2 Mbps | 비슷(protobuf 오버헤드 작음) | 약 0.3~1 Mbps(영상 압축이 가장 효율적) |
| 브라우저 시뮬 | 쉬움 | **쉬움**(`fetch`, `WebSocket` 표준) | 쉬움 | **어려움**: 브라우저는 gRPC를 직접 못 씀. gRPC-Web과 프록시(Envoy)가 필요하고, gRPC-Web은 클라이언트 스트리밍을 지원하지 않음 | 쉬움(브라우저 내장) |
| Android 글래스 | 쉬움(OkHttp) | **쉬움**(OkHttp, 표준 WebSocket) | 쉬움 | 쉬움(grpc-kotlin, 코드 생성) | SDK가 영상 스트림을 주면 쉽고, 아니면 libwebrtc 통합 필요 |
| 서버 구현 | 쉬움 | **쉬움**(FastAPI 엔드포인트 하나 추가, 기존 WS 유지) | 쉬움 | 보통: grpcio, protobuf 스키마, 별도 포트. 기존 REST/WS와 이중 구조 | 어려움: aiortc와 PyAV(FFmpeg) 미디어 스택, 코덱과 FFmpeg 빌드 라이선스 검토, SDP/ICE 시그널링 |
| 디버깅과 운영 | curl로 시험 | **curl로 시험**, 프록시와 로드밸런서 친화적 | 전용 도구 필요 | grpcurl, HTTP/2 필요 | 어려움(미디어 디버깅) |
| 지금 코드 재사용 | 낮음 | **높음**: 세션, 인증, `alert_sync`, ACK, heartbeat를 그대로 씀 | 높음 | 낮음(제어 계약을 다시 정의) | 낮음 |

### 결정

**B. 프레임은 HTTP로 업로드하고, 응답으로 그 프레임의 탐지 결과를 받는다. 경고와 ACK는 기존 세션 WebSocket(제어 전용)으로 유지한다.**

- `POST /v1/sessions/{session_id}/frames`
  - 본문은 JPEG 바이트(`Content-Type: image/jpeg`, 긴 변 640px 이하, `VOA_FRAME_MAX_BYTES` 기본 500 KB 이하)다. JSON+base64는 크기가 33% 커지므로 쓰지 않는다.
  - 메타데이터는 헤더로 보낸다: `X-VOA-Stream-Id: camera_0`, `X-VOA-Seq`, `X-VOA-Frame-Id`, `X-VOA-Capture-Ts-Ns`.
  - 인증은 `Authorization: Bearer <access token>`이다.
  - 서버는 추론하고 엔진 `step`까지 마친 뒤 응답한다.
    ```json
    {"frame_id": "...", "seq": 12, "status": "processed",
     "results": [{"model_id": "...", "observation_id": "obs_...",
                  "detections": [{"label": "...", "score": 0.1, "bbox": [x1, y1, x2, y2]}]}],
     "states": {"fire_in_view": "candidate"},
     "timing": {"capture_ts_monotonic_ns": 1234567890,
                "received_at_utc": "2026-11-26T10:00:00.120Z",
                "processing_started_at_utc": "2026-11-26T10:00:00.121Z",
                "completed_at_utc": "2026-11-26T10:00:00.436Z",
                "queue_ms": 1, "provider_ms": 312, "engine_us": 55}}
    ```
  - **타임스탬프 (소유자 요청)**: 응답과 `frame_result`에 `timing`을 담는다.
    - `capture_ts_monotonic_ns`: 장치가 보낸 값을 그대로 돌려준다. 장치는 이 값으로 자기 시계 기준 왕복 지연(촬영 → 결과 수신)을 잰다.
    - `received_at_utc`, `processing_started_at_utc`, `completed_at_utc`: 서버 시계(UTC)
    - `queue_ms`, `provider_ms`, `engine_us`: 서버 구간별 소요 시간
    - 서버와 장치의 시계는 맞춰져 있지 않을 수 있다. 그래서 장치 시각과 서버 시각을 빼서 쓰지 않고, 각자 자기 시계 안에서만 차이를 계산한다(ADR-0001).
    - 이 값으로 PLAN §4의 `e2e_p95_ms`를 측정한다. 응답의 `observation_id`가 경고 결정의 `evidence_ids`에 들어가므로, 장치는 어느 프레임이 경고의 근거인지 연결해 "촬영 → 경고 표시" 지연도 잴 수 있다.
  - 서버가 이미 그 세션의 다른 프레임을 처리 중이면 즉시 **`409 frame_busy`**를 돌려주고 버린다. 오래된 프레임을 쌓지 않는다.
  - 클라이언트는 **진행 중인 요청을 1개로 제한**한다. 응답을 받은 뒤 그 시점의 최신 프레임을 보내므로, 처리 속도에 맞춰 fps가 자동으로 조절된다. 상한은 서버가 주는 `frame_interval_ms`다.
  - 오류 응답은 다음과 같다. 모두 원문 입력을 되돌려주지 않는다.
    - `400 frame_rejected`: 크기, 형식, 해상도 위반
    - `400 stream_not_declared`
    - `401 unauthorized`
    - `409 frame_busy`
    - `422`는 쓰지 않는다(§7.1 규칙).
- **경고, notice, 결정, heartbeat, ACK, `alert_sync`는 지금의 세션 WebSocket 그대로**다. 이 채널에는 작은 JSON만 흐르므로 프레임 전송 때문에 경고가 늦어지지 않는다.
- 탐지 박스는 **HTTP 응답에 바로 담긴다.** 화면은 그 응답으로 박스를 그린다. 별도의 `detections` 푸시 메시지는 만들지 않는다.
- **A(결과 폴링)는 쓰지 않는다.** 결과는 업로드 응답에 오고, 경고는 푸시 채널로 오기 때문이다.
- **C(WebSocket 하나에 이미지와 제어를 함께)는 쓰지 않는다.** 이미지가 경고 앞을 막는다. WebSocket 자체가 이미지에 부적합한 것은 아니다.
- **D(gRPC)는 지금 하지 않는다.** 브라우저 시뮬 페이지가 프록시 없이 쓸 수 없고, gRPC-Web은 업로드 스트리밍을 지원하지 않는다. 지금 REST/WS 계약과 이중 구조가 된다. Android 전용 고성능 연동이 필요해지면 같은 의미의 서비스로 추가를 검토한다.
- **E(WebRTC)는 지금 하지 않는다.** 2~5fps 판정에는 이점이 작고, 미디어 스택, 라이선스, 시그널링 비용이 크다. 다음 조건 중 하나가 생기면 새 ADR로 검토한다. 서버 쪽 프레임 입력은 `FrameSource` 경계 뒤에 두어 전송 방식만 바꿀 수 있게 한다.
  - 글래스 SDK가 JPEG 스냅샷을 줄 수 없고 영상 스트림만 줄 때(OQ-02)
  - 측정 결과 Wi-Fi 대역폭이 JPEG 업로드로 부족할 때
  - 10fps 이상이 필요한 시나리오가 생길 때
- RTSP/RTMP 푸시는 채택하지 않는다. 미디어 서버가 추가되고 녹화 기능 때문에 원본 저장 금지를 관리하기 어렵다.

### 동기 응답과 비동기 결과 (소유자 결정, 2026-11-26)

추론 시간이 길어질 수 있고, 나중에 여러 서브에이전트가 검토해 종합 의견을 내는 구조로 확장한다. 그래서 결과를 두 층으로 나눈다.

- **fast path (경고 경로)**: 탐지 → 엔진 판정 → 경고. AGENTS 규칙 9에 따라 VLM, 에이전트, OCR, STT를 기다리지 않는다.
  - 업로드 응답은 `VOA_FRAME_SYNC_TIMEOUT_MS`(기본 1000) 안에 탐지와 엔진 `step`이 끝나면 `200 processed`로 결과를 담는다.
  - 넘으면 `202 accepted {frame_id}`로 먼저 답한다. 결과는 끝나는 대로 제어 WebSocket의 **`frame_result`** 메시지(업로드 응답과 같은 본문)로 보낸다.
  - 처리 중 프레임은 stream당 1개 규칙(409)은 그대로다.
- **slow path (분석 경로, 후속)**: 확정된 결정과 그 근거만 입력으로 받는 분석기(VLM, 여러 서브에이전트의 검토와 종합)가 끝나는 대로 제어 WebSocket의 **`analysis`** 메시지로 보낸다.
  - 필드: `analysis_id`, `risk_event_id`, `scenario_id`, `claims[{text, evidence_ids}]`, `reviewers[]`, `status`
  - 이 메시지는 경고를 만들거나, 막거나, 바꾸지 않는다. 판단은 `DecisionEngine`만 한다(AGENTS 규칙 2).
  - claim은 §6 validator를 통과한 것만 보낸다(EXP-03, T-R09).
  - 메시지 이름과 필드는 SPEC에 예약만 하고, 구현은 Stage E에서 한다.

### 프레임 간격 조정 (소유자 결정)

- 관리자가 `frame_interval_ms`(기본 333, 범위 100~5000)를 바꾼다. 장치에는 두 경로로 알린다.
  - 세션 생성 응답에 포함한다.
  - 진행 중인 세션에는 제어 WebSocket의 **`config`** 메시지(`frame_interval_ms`)로 바로 보낸다.
- 이것은 전송 속도 설정일 뿐이고 판단 규칙(profile)이 아니다. 그래서 진행 중인 세션에도 즉시 적용한다. 판단 규칙은 결정 3대로 새 세션부터 적용한다.

### 연결 보안과 인증 (소유자 결정)

- `VOA_BIND`가 `127.0.0.1`이 아니면 **TLS(https/wss)가 필수**다. 인증서와 키가 없으면 서버가 시작을 거부한다. 지금까지는 `VOA_PROFILE=pilot`에서만 필수였다.
- **WebSocket은 핸드셰이크(HTTP upgrade) 단계에서 인증한다.** 인증에 실패하면 WebSocket을 열지 않고 HTTP 401로 거절한다. 지금의 "연결 후 첫 메시지 `auth`(5초 안, close 4401)" 방식은 이것으로 대체한다(SPEC §7.2 갱신).
  - 글래스 앱처럼 헤더를 붙일 수 있는 클라이언트: `Authorization: Bearer <access token>`
  - 브라우저(WebSocket API가 헤더를 못 붙임): `Sec-WebSocket-Protocol: voa.v1, voa.bearer.<access token>`. 서버는 `voa.v1`만 되돌려 준다.
  - token은 URL과 query에 넣지 않는다. 핸드셰이크 헤더는 로그에 남기지 않는다(SPEC §8.2).
- 프레임 업로드와 관리자 API는 요청마다 `Authorization: Bearer`를 쓴다. 관리자 token은 장치 token과 분리한다(결정 3).

## 핵심 결정 2: 서버 쪽 추론 파이프라인

- 세션마다 stream당 **처리 중인 프레임은 1개**다. 처리 중에 온 프레임은 `409 frame_busy`로 버리고 `skipped`로 센다. 오래된 프레임을 쌓아 두었다 처리하지 않는다. 클라이언트도 진행 중인 요청을 1개로 제한하므로 보통은 409가 나지 않는다.
- 프레임은 **배치된 모든 모델** 중 활성 profile의 시나리오가 `required_models`로 요구하는 모델에 넣는다. 모델마다 같은 `frame_id`로 `object_detection` Draft를 만들고, 서버가 `observation_id`와 수신 시각을 채워 엔진 `step`에 넣는다(ADR-0004 다중 모델 키).
- 추론은 이벤트 루프 밖의 워커(스레드 또는 프로세스)에서 하고, heartbeat, ACK, 세션 관리를 막지 않는다. 모델 인스턴스는 세션 간에 공유하고, 장치(CPU/GPU)는 배치 설정이 정한다.
- 프레임 바이트와 디코딩된 이미지는 추론이 끝나면 버린다. DB, 파일, 로그에 남지 않는다(DAT-01, DAT-02). `observations_meta`에는 지금처럼 메타데이터만 남긴다.
- Provider 하한 점수는 profile `min_score`보다 높을 수 없다. 서버가 배치 시점에 `min(배치 하한, 해당 모델을 쓰는 시나리오의 min_score)`로 맞춘다(TASK-39에서 찾은 함정).
- 장치 쪽 추론 예외(ADR-0007 §5, `--frames` 모드)는 오프라인 평가와 회귀 테스트용으로 남긴다. 제품 경로는 이 ADR의 서버 추론이다.

## 핵심 결정 3: 관리자 설정 API

- **배치(deployment)**: manifest에 등록된 모델 중 무엇을 메모리에 올릴지 정한다. 여러 모델을 동시에 배치할 수 있다.
  - `GET /v1/admin/models`: manifest 목록, 가중치 파일 상태(없음 / sha256 불일치 / 준비됨), 배치 상태
  - `POST /v1/admin/deployments {model_id, device: cpu|cuda, score_floor}`: sha256 확인 후 로드. 다운로드하지 않는다.
  - `DELETE /v1/admin/deployments/{model_id}`: 해제. 활성 profile이 이 모델을 요구하면 409 `model_in_use`
- **정책(profile)**: 시나리오별 근거 모델, label, ROI, `min_score`, `confirm_frames`, `clear_frames` 등. 기존 profile 스키마(SPEC §4.9)를 그대로 쓴다.
  - `GET /v1/admin/profile`: 활성 profile과 버전
  - `PUT /v1/admin/profile`: 새 profile 전체를 받는다. 검증 순서는 다음과 같다.
    1. 스키마(§4.9)
    2. manifest 대조(`check_profile_models`)
    3. 요구 모델이 모두 배치됨
  - 검증을 통과하면 `profile_version`을 올려 저장한다(`var/config/` 아래 YAML). 실패하면 400과 stable code를 돌려주고, 활성 설정은 바뀌지 않는다.
  - `POST /v1/admin/profile:validate`: 저장하지 않고 검증만 한다(화면의 "적용 전 확인").
  - `POST /v1/admin/detect` (multipart JPEG 1장, 모델 지정): 엔진을 거치지 않고 탐지 결과만 돌려준다. 이미지는 저장하지 않는다.
- **적용 시점**: 새 profile은 **새로 시작하는 세션부터** 적용한다. 진행 중인 세션은 시작할 때의 profile로 끝까지 판단한다.
  - 이유: 엔진의 상태 기계는 세션 안에서 결정론적이어야 하고(INV), 판단 도중 규칙이 바뀌면 근거 추적이 깨진다.
  - 결정에는 지금처럼 `policy_version`이 남는다. 즉시 적용이 필요하면 관리자가 세션을 끊는 API를 따로 둔다(후속).
- **인증과 기록**:
  - 관리자 API는 장치 token과 분리된 관리자 token(`VOA_ADMIN_TOKEN_FILE`, scrypt digest 보관)으로 보호한다.
  - 기본은 `127.0.0.1`에서만 받는다.
  - 변경마다 `admin_changes(id, changed_at_utc, action, target, from_version, to_version, result_code)`를 SQLite에 남긴다. 요청 원문과 token은 남기지 않는다(SPEC §8.1에 테이블 추가).

## 핵심 결정 4: 화면은 서버가 제공하고 공개 API만 쓴다

- 서버가 정적 페이지 두 개를 제공한다. 둘 다 별도 백엔드 없이 위 API만 호출한다.
  - `/ui/admin`: 모델 목록과 배치, 시나리오와 임계값 편집(검증 후 적용), 이미지 한 장 시험, 변경 기록
  - `/ui/sim`: 장치 역할. 장치 credential로 세션을 만들고 제어 WebSocket에 연결한다. 웹캠이나 동영상에서 캡처해 `POST .../frames`로 올리고(진행 중 1개), 응답의 탐지 박스, WebSocket으로 오는 HUD 경고와 음성(TTS), ACK(표시 후 `played`), 시나리오 상태를 보여준다. 동영상 파일은 브라우저 밖으로 나가지 않는다.
- 시뮬 페이지가 쓰는 API는 실제 스마트 글래스 앱이 쓰는 것과 **완전히 같다.** 그래서 시뮬 페이지가 동작하면 글래스 연동 계약도 검증된다.
- TASK-35 초안(장치 쪽 추론 브리지 `voa.simulator.web`)은 병합하지 않는다. 화면 요소(캡처, HUD, TTS, 박스 그리기)와 `GlassClient`의 `defer_played`는 재사용한다.

## 결정 기록 (2026-11-26)

1. 전송 방식: HTTP 업로드(응답에 결과) + 제어 WebSocket 분리로 확정. 추론이 길어지면 `202`와 `frame_result`로 비동기 처리한다.
2. `frame_interval_ms`: 기본 333, 관리자가 조정하고 진행 중인 세션에도 `config`로 즉시 알린다.
3. profile 변경은 새 세션부터 적용한다.
4. 인증: 루프백이 아니면 TLS 필수, WebSocket은 핸드셰이크에서 인증, 관리자는 별도 token(파일 기반 단일 token).
5. 나중에 여러 서브에이전트가 검토해 종합 의견을 내는 구조로 확장한다. 결과는 `analysis`로 비동기 전달하고 경고 경로와 분리한다.
6. 업로드 응답과 `frame_result`에 요청 타임스탬프와 구간별 소요 시간(`timing`)을 담는다.

## Alternatives

- **HTTP 업로드 + 결과 폴링**: 결과는 업로드 응답에 담을 수 있으므로 폴링이 필요 없고, 경고는 푸시가 필요하다.
- **WebSocket 하나에 이미지와 제어**: 이미지 전송이 경고, ACK 앞을 막는다(head-of-line).
- **gRPC 양방향 스트리밍**: Android에는 좋지만 브라우저 시뮬이 프록시 없이 쓸 수 없고 gRPC-Web은 업로드 스트리밍을 지원하지 않는다.
- **WebRTC**: 대역폭 효율과 저지연은 좋다. 하지만 판정에 필요한 속도가 2~5fps라 이점이 작고, 미디어 스택과 코덱 라이선스, 시그널링 비용이 크다. 위 조건이 생기면 다시 검토한다.
- **장치 쪽 추론 유지(ADR-0007 방식)**: 대역폭은 작지만 장치마다 모델 배포가 필요하고, 관리자가 모델과 임계값을 중앙에서 바꿀 수 없다.
- **profile 즉시 적용**: 운영은 편하지만 세션 중 결정 규칙이 바뀌어 근거 추적과 결정론이 깨진다. 필요해지면 리셋 규칙과 함께 따로 결정한다.

## Consequences

- 장점: 글래스 앱은 "캡처 → JPEG → 전송, 경고 렌더링 → ACK"만 구현하면 된다. 모델 교체와 임계값 조정은 서버에서 중앙으로 한다. 엔진, 결정, 경고 계약은 바뀌지 않는다.
- 비용: 서버가 세션 수만큼 추론 부하를 진다. 1 stream 기준 CPU 약 3fps, 다중 세션과 다중 모델은 GPU가 필요할 수 있다(OQ-04). 장치에서 서버로 가는 대역폭은 3fps에 약 1 Mbps다. 연결은 HTTP(업로드)와 WebSocket(제어) 두 개다.
- 후속 Task(제안):
  1. SPEC 확장
  2. 프레임 업로드 API와 서버 추론 파이프라인
  3. 관리자 API(배치, profile 검증과 버전, 단건 시험, 변경 기록, 인증)
  4. `/ui/admin`
  5. `/ui/sim`
  6. Android 참조 클라이언트 프레임 업로드(Stage F)
- 되돌림: 프레임 업로드와 관리자 API를 끄면 현재의 `observation` 경로가 그대로 남는다.

## Verification

- 계약: 프레임 업로드의 헤더 누락, 크기와 해상도 초과, JPEG 아님, 미선언 stream, seq 역행, 다른 세션 token, 처리 중 중복 요청마다 정해진 HTTP 상태와 code를 돌려준다. 업로드가 진행 중이어도 제어 WebSocket의 alert가 지연되지 않는다.
- 파이프라인: 가짜 모델로 프레임 → 관측 → 결정 → alert, 추론 지연 시 최신 프레임만 처리, 모델 2개가 같은 `frame_id`를 공유, 프레임 바이트가 디스크, DB, 로그에 남지 않음(프라이버시 누출 검사 확장)
- 관리자: 미배치 모델을 요구하는 profile 거부, 사용 중인 모델 해제 409, 검증 실패 시 활성 설정 유지, 버전 증가와 변경 기록, 진행 중 세션은 이전 profile 유지, 관리자 token 없으면 401
- E2E: `/ui/sim`과 같은 API를 쓰는 테스트 클라이언트로 영상 프레임 → 경고 → `played`
