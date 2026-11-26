# implementation plan - Task와 진행 방식

> 각 Task는 `01-product-requirements.md`의 REQ ID에서 도출했다. 산출물 경로, 이름, 동작은 `02-technical-specification.md`(v0.5)를 따른다.
> **한 owner는 한 번에 Task 하나만** 구현/검증하고, 끝나면 이 표의 상태를 `DONE`으로 바꾼다. 다중 개발자 병렬 작업은 `03-collaboration-architecture.md` §4~5를 따른다.
> Claude Code에서는 `/task`(다음 TODO 자동 선택) 또는 `/task TASK-07`. Codex에서는 §5의 프롬프트를 쓴다.

---

## 1. 진행 규칙

1. **선택**: 단일 owner는 선행 Task가 모두 `DONE`인 Task 중 번호가 가장 작은 것을 고른다. 팀 리더는 선행 Task가 모두 `DONE`이고 소유 파일이 겹치지 않는 Task를 여러 owner에게 병렬 배정할 수 있다.
2. **계획**: 수정할 파일, REQ/INV/T-R ID, 테스트 목록을 제시한다. 명백하고 낮은 위험의 로컬 작업은 보고 후 계속하고, 명세 변경, 비가역 작업, 사용자 선호에 따라 결과가 달라지는 경우에만 확인을 받는다. 테스트 목록에는 수용 테스트 전부와 성공, 실패, 결측/지연 각 1개 이상이 들어가야 한다.
3. **테스트 먼저**: 수용 기준을 테스트로 옮겨 실패하는 것을 확인한 뒤 구현한다.
4. **검증**: `scripts/check.sh`를 실제로 실행하고, 그 출력으로 보고한다.
5. **기록**: 이 표의 상태를 `DONE`으로 바꾼다. SPEC과 다르게 구현해야 했다면 SPEC을 먼저 고치자고 제안하고 확인을 받는다. 커밋 메시지는 `TASK-xx: <제목> (REQ ids)`로 쓴다.
6. **범위 엄수**: Task 범위 밖은 건드리지 않는다. 필요한 작업이 생기면 새 Task를 제안한다.

**완료 정의 (DoD)**: 수용 테스트 통과 / 기존 테스트 유지 / `scripts/check.sh` 통과 / 외부 네트워크/다운로드 없음 / 이 표 갱신.

---

## 2. Stage A — 판단 엔진 코어 (서버 없음) ★

| Task | 제목 | REQ | 산출물 (SPEC 절) | 수용 테스트 | 선행 | 상태 |
|---|---|---|---|---|---|---|
| TASK-01 | 프로젝트 스캐폴드 | NFR-01, DAT-03 | `git init`, `apps/server/pyproject.toml`(§1 도구/의존성), `apps/server/src/voa/` 하위 패키지(§1 트리, 빈 `__init__.py`), `scripts/check.sh`, `.gitignore`(`.venv/`, `var/`, `apps/server/config/devices.yaml`) | Docker 개발 이미지 빌드 후 `scripts/check.sh` 통과, `import voa` 테스트 1개 | — | DONE |
| TASK-02 | Observation 계약 | CON-01, CON-02, ENG-01 | `apps/server/src/voa/contracts/enums.py`, `apps/server/src/voa/contracts/observation.py` (§3.1–3.3, Draft 포함) | BBox/Score 경계값, payload kind 판별, extra 필드 거부, 빈 detections 허용, JSON Schema 스냅샷 | 01 | DONE |
| TASK-03 | RiskDecision/Alert 계약 | CON-01, ENG-11, ENG-13 | `apps/server/src/voa/contracts/decision.py` (§3.4, `ReasonCode` §4.8) | `contracts/fixtures/risk_event.json` 통과, 검증기 1–4 각각 위반 시 거부, 미등록 reason_code 거부, `contracts/schemas` 스냅샷 | 02 | DONE |
| TASK-04 | Policy Profile | ENG-15, INV-10 | `apps/server/src/voa/engine/profile.py`(모델/검증), `apps/server/src/voa/config/profile_loader.py`(YAML → 모델 / `ProfileError`), `apps/server/config/profiles/indoor_research_v0.yaml`, `apps/server/tests/fixtures/profiles/` | 정상 로드, §4.9 검증 항목마다 실패 케이스 1개 이상, `file_not_found`/`yaml_invalid`/`profile_invalid` 구분, detail 비밀 데이터 미포함 | 02 | DONE |
| TASK-05 | 포트/테스트 도구/순수성 검사 | ENG-14, ENG-17, INV-06 | `apps/server/src/voa/engine/ports.py`(`IdFactory`, `UtcClock`, `SequentialIdFactory`, `FixedEpochClock`), `apps/server/tests/builders.py`(관측/프레임 빌더), `apps/server/tests/engine/test_purity.py`(AST import 허용 목록) | risk/alert/obs/sess/notice prefix별 ID 순번 결정론, clock 매핑, 금지 모듈 import 시 테스트 실패 확인 | 02 | DONE |
| TASK-06 | 단계 1 Admission | ENG-01, ENG-02, INV-07 | `apps/server/src/voa/engine/admission.py` (§4.2) | `AdmissionStatus` 6종 각각 발생, 검사 순서, 처음 받은 key는 3/4번 통과 (T-R04) | 03, 04, 05 | DONE |
| TASK-07 | 단계 2/3 Window/Freshness/Frame Assembly | ENG-03, ENG-12 | `apps/server/src/voa/engine/window.py` (§4.3, §4.4) | fresh 경계값(=staleness 포함), `window_max_items`/`window_ms` 축출 시 인덱스 동기화, sensor_down 갱신, 프레임 완성 1회, 늦은 중복 frame 무시, 완성 시 stale이면 폐기 | 06 | DONE |
| TASK-08 | 단계 4 Scenario Evaluator | ENG-04, ENG-05, ENG-16 | `apps/server/src/voa/engine/evaluator.py` (§4.5) | 시나리오 2종 × met/absent/indeterminate, 판정 순서(차단 flag 우선), `overlap` 계산/경계, label/score/motion 필터, track lost의 `roi_min_overlap` 경계 (T-R02, T-R12, T-R15) | 07 | DONE |
| TASK-09 | 단계 5 State Machine | ENG-06–10, ENG-18, INV-03, INV-04 | `apps/server/src/voa/engine/state_machine.py` (§4.6) | 전이표 행 1–10 각각 1개 이상, 표에 없는 전이 미발생, 호출당 전이 1회, unknown 진입 시 카운터 초기화, risk_event_id 발급/유지/종료 규칙 (T-R01, T-R03) | 08 | DONE |
| TASK-10 | 단계 6 + `DecisionEngine` 통합 | ENG-10–15, INV-08–10 | `apps/server/src/voa/engine/engine.py` (§4.1, §4.7) | `step`/`tick` 적용 범위(시간 규칙 행 1/2/4), 결정 필드(§4.7), warning 진입당 alert 1개, `now` 역행 시 ValueError, `ProfileError` 시 config_error/결정 0개, `get_observation`/`is_fresh` 읽기 전용 계약 | 09 | DONE |
| TASK-11 | 불변식 테스트 묶음 | G1, G2 | `apps/server/tests/engine/test_invariants.py` | INV-01–10 전부. 고정 시드 `random.Random(seed)`로 무작위 관측/tick 시퀀스 200개를 생성해 매 결정마다 INV 검사(테스트 코드에서만 random 사용) | 10 | DONE |
| TASK-12 | Fixture 재생기/Mock Provider | INP-01, CON-03 | `apps/server/src/voa/providers/base.py`, `apps/server/src/voa/providers/mock.py`, `apps/server/src/voa/simulator/fixture.py`(§5 형식 파서/재생), provider contract suite, `contracts/fixtures/scenarios/` UC-01–04 + `.expected.jsonl` | 4개 fixture의 결정이 기대와 일치, mock과 독립 test double이 같은 provider contract suite 통과 | 10 | DONE |
| TASK-13 | Replay CLI | INP-02, ENG-14, NFR-02 | `apps/server/src/voa/replay/runner.py`, `apps/server/src/voa/replay/__main__.py` (§5) | 2회 실행 출력 바이트 동일(INV-05), 기대 불일치 시 줄/필드 출력 + exit 1, stderr에 p50/p95 | 12 | DONE |
| TASK-14 | 근거 검증/설명 폴백/행동 제한 | ENG-12, EXP-01, EXP-02 | `apps/server/src/voa/explain/repository.py`, `apps/server/src/voa/explain/validator.py`, `apps/server/src/voa/explain/template.py`, `apps/server/src/voa/explain/actions.py` (§6) | 위반 코드 6종 각각 → 템플릿 폴백 (T-R08), 금지 행동 → `ForbiddenActionError`, idempotency_key 결정론 | 10 | DONE |
| TASK-15 | SQLite 메타데이터 저장 | DAT-01, DAT-02 | `apps/server/src/voa/storage/sqlite.py` (§8.1), 로그 포매터 (§8.2) | 4개 테이블 CRUD, `PRAGMA table_info`로 BLOB/허용 외 컬럼 없음 검사, 보존 기간 삭제, 로그 허용 키 외 필드 제거 | 03 | DONE |
| TASK-15A | 의존성 라이선스 등록부 검증 | DAT-05, NFR-04 | `apps/server/tests/test_dependency_registry.py`, `02-technical-specification.md` §9 | `apps/server/pyproject.toml` 직접 의존성이 등록부에 누락되면 실패, AGPL/NC/비공개 API 의무 항목이 기본 설치에 있으면 실패 | 01 | DONE |

TASK-07 검증 (2026-11-10): 기존 Docker 개발 이미지에서 네트워크를 차단하고 120초 상한으로 대상 검사 1회 실행. `src/voa/engine/window.py`, `tests/engine/test_window.py`의 ruff lint/format 및 strict mypy 통과. `pytest -q tests/engine/test_window.py tests/engine/test_purity.py::test_engine_and_contracts_import_only_allowlisted_modules` 28 passed. 개수/시간 축출과 Admission ID 해제 연동, 신선도/보존 경계, health 복구, 시나리오별 조립, 중복/지연/stale 프레임 폐기, 축출된 근거 사용 방지를 확인했다. 사용자 지정의 제한 검증 원칙에 따라 전체 `scripts/check.sh`는 미실행. 엔진 step/tick에서 prune/축출 ID 해제/조립을 연결하는 작업과 전체 엔진 동작 검증은 TASK-10에서 수행한다. SPEC/계약/의존성 변경 없음.

TASK-08 검증 (2026-11-10): 구현/검증 완료된 TASK-07 커밋 위에서 별도 브랜치로 진행. 기존 Docker 개발 이미지에서 네트워크를 차단하고 120초 상한으로 대상 검사 1회 실행. `src/voa/engine/evaluator.py`, `tests/engine/test_evaluator.py`의 ruff lint/format 및 strict mypy 통과. `pytest -q tests/engine/test_evaluator.py tests/engine/test_purity.py::test_engine_and_contracts_import_only_allowlisted_modules` 55 passed. 시나리오 2종의 3값 판정, 차단 flag 우선순위, overlap/score 경계, label/motion/quality 필터, qualifying 대상 유무에 따른 lost track 처리, 시나리오 독립성, 근거 순서 및 입력 불변성을 확인했다. TASK-07 조립기와 연결해 미완성/stale 프레임이 평가되지 않음을 확인했다. 사용자 지정의 제한 검증 원칙에 따라 전체 `scripts/check.sh`는 미실행. 센서 down과 unknown/warning 상태 전이, 엔진 전체 동작은 TASK-09/10에서 구현/검증한다. SPEC/계약/의존성 변경 없음.

TASK-09 검증 (2026-11-10): 구현/검증 완료된 TASK-08 커밋 위에서 별도 브랜치로 진행. 기존 Docker 개발 이미지에서 네트워크를 차단하고 120초 상한으로 대상 검사 1회 실행. `src/voa/engine/state_machine.py`, `tests/engine/test_state_machine.py`의 ruff lint/format 및 strict mypy 통과. `pytest -q tests/engine/test_state_machine.py tests/engine/test_purity.py::test_engine_and_contracts_import_only_allowlisted_modules` 59 passed. 전이표 행 1–10, 호출당 전이 1회, 센서 down/지연 우선순위, 시간 경계, 프레임 없는 호출의 카운터 유지, unknown 진입 시 카운터 초기화, risk_event_id 발급/유지/종료와 재현 결정론을 확인했다. 사용자 지정의 제한 검증 원칙에 따라 전체 `scripts/check.sh`는 미실행. 상태 전이를 RiskDecision/Alert로 변환하고 step/tick과 연결하는 엔진 통합은 TASK-10에서 구현/검증한다. SPEC/계약/의존성 변경 없음.

TASK-15 검증 (2026-11-10): 네트워크 차단 Docker 개발 컨테이너에서 `pytest -q tests/storage` 44 passed. `src/voa/storage`, `tests/storage` 범위의 ruff lint/format 통과, mypy는 None 검사 보완 후 동일 대상 1회 재검사 통과. 이후 사용자가 전체 회귀 검증을 요청하여 `scripts/check.sh`를 120초 상한으로 1회 실행했고, ruff lint/format, mypy 35개 파일, pytest 216개가 모두 통과했다. 실제 서버 연결 검증은 `voa.server`에 API 앱과 실행 진입점이 아직 없어 수행할 수 없다. 서버 시작 시/시간별 `purge_expired` 호출과 `JsonLogFormatter` 연결은 Stage B 서버 조립 시 수행한다.

**Stage A 게이트**: TASK-01–15A가 모두 DONE이고, T-R01/02/03/04/08/12/15와 INV-01–10이 통과해야 한다. `python -m voa.replay contracts/fixtures/scenarios/uc01_obstacle.jsonl --expected ...`가 exit 0이어야 한다.

---

## 2-1. Stage A+ — 다중 모델 공통 인프라 (ADR-0004)

한 카메라 프레임을 여러 모델이 분석해도 각 시나리오가 자기 근거 모델의 관측만으로 판정하도록 Stage A 산출물을 보완한다. 시나리오(균열, 벽 손상, 화재, 연기 등)는 이 인프라 위에 모델 manifest와 profile로 추가한다. **Stage B 시작 전에 끝낸다.**

| Task | 제목 | REQ | 산출물 (SPEC 절) | 수용 테스트 | 선행 | 상태 |
|---|---|---|---|---|---|---|
| TASK-24 | 모델 출처 계약과 profile `required_models` | ENG-19, CON-04 | `apps/server/src/voa/contracts/observation.py`(영상 계열 `model` 필수, Draft/Observation), `apps/server/src/voa/engine/profile.py`(`required_models`), `apps/server/config/profiles/indoor_research_v0.yaml`, `apps/server/tests/fixtures/profiles/`, `apps/server/tests/builders.py`(영상 관측 기본 `model`), `contracts/schemas` 스냅샷 (§3.3, §4.9) | 영상 kind에 `model` 없음 → `ValidationError`(Draft, Observation 각각), health는 `model` 없이 허용, `required_models` key가 `required_kinds`와 다르거나 누락 → `profile_invalid`, 기존 테스트와 UC fixture 유지 | 14 | DONE |
| TASK-25 | 엔진 다중 모델 키와 근거 모델 필터 | ENG-19, INV-01, INV-02, INV-07 | `apps/server/src/voa/engine/admission.py`, `apps/server/src/voa/engine/window.py`(키 `(stream_id, source_kind, model_id)`, Frame Assembly `required_models` 필터), `apps/server/tests/engine/test_invariants.py` 생성기에 두 모델 경우 추가, 두 모델 UC fixture (§4.2~4.4) | T-R19: 같은 `frame_id`를 두 모델이 분석하면 두 관측 모두 accepted, 각 시나리오 근거는 자기 모델 관측만, 한 모델만 멈추면 그 모델 시나리오만 unknown(`input_stale`), 다른 모델 관측으로 프레임이 완성되지 않음, `window_max_items`와 seq 검사는 모델 키별 | 24 | DONE |
| TASK-26 | 모델 manifest와 profile 대조 | CON-04, DAT-05 | `apps/server/src/voa/config/model_registry.py`(`load_manifests`, `check_profile_models`), `apps/server/config/models/mock-detector.yaml`, `apps/server/config/models/mock-tracker.yaml`, 리플레이 CLI의 기본 대조 (§4.9, §9) | manifest 누락, kind 불일치, label이 manifest 밖 → `profile_invalid`(detail에 시나리오 ID와 항목 이름만), `weights: file`인데 `sha256` 없음 → 거부, 기본 profile과 mock manifest는 통과 | 24 | DONE |

**Stage A+ 게이트**: TASK-24~26이 DONE이고, T-R19와 기존 INV-01~10, UC-01~04 리플레이가 통과해야 한다.

---

## 3. Stage B — 서버/전송 (MVP-1 완성)

| Task | 제목 | REQ | 산출물 (SPEC 절) | 수용 테스트 | 선행 | 상태 |
|---|---|---|---|---|---|---|
| TASK-16 | 설정/앱 골격 | COM-06, DAT-03, DAT-04 | 의존성 추가(fastapi, uvicorn, httpx-dev, §9 등록부 갱신), `apps/server/src/voa/server/settings.py`, `apps/server/src/voa/server/runtime.py`, `apps/server/src/voa/server/app.py`, `apps/server/src/voa/server/__main__.py`, `/v1/healthz`, `/v1/readyz`, `compose.yaml` server 서비스의 소스 bind mount와 모델 파라미터(`VOA_MODELS_DIR`, `VOA_MODEL_WEIGHTS_DIR`, 가중치 볼륨) (§8.3, §8.4) | 기본값, 숫자 교차 검증, pilot cert/key 없음 시작 거부, pilot uvicorn SSL 인자 전달, RAW_MEDIA=true 시작 거부, profile 무효 또는 모델 manifest 대조 실패 시 readyz 503(body에 원문 없음), `SecureIdFactory` ID 형식, `AnchoredUtcClock` monotonic-to-UTC 매핑 | A+ 게이트 | DONE |
| TASK-17 | 장치 등록/세션/capability | COM-01, DEV-01 | `apps/server/src/voa/server/auth.py`, `apps/server/config/devices.example.yaml`(파일과 로더는 TASK-16에서 작성, 여기서는 scrypt 검증과 세션 발급), `POST /v1/sessions`, `POST /v1/sessions/{id}/resume` (§7.1, §7.4 enum) | scrypt 형식/비교, 정상 발급, access/resume 만료, resume 회전 후 이전 token 폐기, 서버 재시작 후 resume 401, 잘못된 secret/미등록 장치 401, capability 범위 밖 400, outputs 빈 목록 400, insecure dev는 127.0.0.1에서만 | 16 | DONE |
| TASK-18 | WS 스트림 ↔ 엔진 | COM-02 | `apps/server/src/voa/contracts/messages.py`, JSON Schema 스냅샷, `apps/server/src/voa/server/ws.py`, 세션별 엔진, tick 루프 (§7.2) | 5초 안에 auth 없음/token 오류 → 4401, Draft → 관측 → `decision`/`alert` push, 입력 끊김 → unknown push, non-accepted → `admission`, heartbeat 주기, 미선언 입력은 health down 주입 (T-R17), `alert_sync`(구 `resume`) message는 auth 후 `last_alert_seq`만 수용, 예약 메시지 → `unsupported_message_type` | 17 | DONE |
| TASK-19 | Alert 전달/ACK/notice | COM-03, DEV-03 | `apps/server/src/voa/server/dispatch.py`, `POST /v1/alerts/{id}/ack`, WS `ack`, `notice` 송신 (§7.3, §7.4) | queued→expired, sent→unknown_delivery, received 유지, received→played, 역행 없음, 중복 ACK 무시, 늦은 ACK 상태 복구와 `late_ack` 로그 (T-R11), notice 조건 (T-R18) | 18 | DONE |
| TASK-20 | 재접속/중복 방지 | COM-04 | REST `refresh` token 회전 후 WS `auth` 및 `alert_sync(last_alert_seq)` 처리, 서버와 Android 메시지 이름 변경 (§7.1~7.3, ADR-0006) | 이전 refresh token 재사용 거부, 재전송 조건 1/2/3 각각 위반 시 미전송, 같은 alert_id 유지, 시뮬레이터 측 alert_id 1회 재생 (T-R10, UC-05) | 19 | DONE |
| TASK-21 | Backpressure/status | COM-05 | 입력 큐, drop 계측, stream별 `frame_dropped` 부착, 제어 경로 분리, `GET /v1/sessions/{id}/status` (§7.5) | 큐 포화 시 오래된 영상 폐기, 폐기 대상 없음 시 신규 영상 거부, health/ACK 보존, 같은 stream의 다음 영상에만 flag, status 응답 필드 | 18 | DONE |
| TASK-22 | 시뮬레이터 = 글래스 클라이언트 참조 구현 | INP-01, COM-02, DEV-02, DEV-04, DEV-05 | `apps/server/src/voa/simulator/adapter.py`(세션/WS/전송), `apps/server/src/voa/simulator/outputs.py`(가상 HUD/speaker/haptic 출력 로그), `apps/server/src/voa/simulator/watchdog.py`, `apps/server/src/voa/simulator/__main__.py` (`--fixture --server --device --secret --outputs hud,speaker`) (§7.4) | uvicorn을 띄운 통합 테스트로 UC-01–05/08/09: 출력 로그로 렌더링/1회 재생/ACK 시점 검증, 서버 중단 시 watchdog 고지와 backoff 재접속 (T-R16) | 20, 21 | DONE |
| TASK-23 | 프라이버시 누출 검사 | DAT-01, DAT-02 | `apps/server/tests/integration/test_privacy.py` | E2E 실행 후 DB/캡처 로그/`tmp_path`에서 token/secret/payload 텍스트 검출 0건 | 22, 15 | DONE |
| TASK-27 | refresh token 명칭 변경, 세션 정리, 보존 기간 정리 | COM-01, DAT-01 | `apps/server/src/voa/server/auth.py`, `apps/server/src/voa/server/settings.py`, `apps/server/src/voa/server/app.py`(`/v1/sessions/{id}/refresh`, 세션 정리 루프, 시작 시와 1시간마다 `purge_expired`) (§7.1, §8.1, §8.3, ADR-0005) | 구 `/resume` endpoint와 `resume_token` 필드 없음, refresh 회전, refresh token 만료 + 미연결 세션 정리(메모리 제거, `ended_at_utc`, refresh 401, auth 4401), 연결 중 세션 유지 후 연결 종료 뒤 정리, 보존 기간 지난 행 삭제, `VOA_SESSION_SWEEP_INTERVAL_S` 검증 | 18 | DONE |
| TASK-28 | 서버 로그 JSON 포매터 연결과 로그 수준 하한 | DAT-02 | `apps/server/src/voa/server/logconfig.py`(`server_log_config`, `LoggerEventFilter`, `pin_log_levels`), `apps/server/src/voa/server/__main__.py`, `apps/server/src/voa/server/app.py` lifespan, `tests/integration/test_privacy.py` 실행 모드 확장 (§8.2) | uvicorn 기본/DEBUG, `python -m voa.server` 설정 기본/DEBUG 4가지 모드에서 E2E 누출 0건, `server_log_config` 출력이 JSON 한 줄이고 `late_ack` event 포함, 비구조 기록은 logger 이름이 event, 수준 하한은 올리기만 하고 낮추지 않음 | 23 | DONE |

**Stage B 게이트 = MVP-1**: T-R10/T-R11/T-R16/T-R17/T-R18, 인증 거부, 재접속 시나리오가 통과하고, 시뮬레이터 → 서버 → 엔진 → alert → ACK까지 시연할 수 있어야 한다.

---

## 4. Stage C~F — 개략 (해당 단계에 들어갈 때 SPEC을 먼저 확장한 뒤 Task로 세분화)

| Stage | 범위 | 핵심 REQ | 진입 전 결정 |
|---|---|---|---|
| C 실 Vision | detector 어댑터 + model manifest → tracker(+`camera_motion_uncompensated` 판정) → 평가 러너(B0 프레임 단위 규칙 vs B2 엔진: recall, FP/hour, p95) | INP-03, NFR-02/03, T-R14 | OQ-01, OQ-04, OQ-06 |
| D Audio/OCR | SED 시나리오 추가(SPEC §4.9 확장), PTT ASR, partial/final, OCR 비신뢰 처리 | INP-04, T-R05/06/07/13 | OQ-05 |
| E VLM 설명 | 확정된 결정만 입력, claim+evidence_ids 구조화 출력, §6 validator, 타임아웃 폴백, unsupported claim rate 측정 | EXP-03, T-R09 | OQ-04 |
| F Android 클라이언트 및 실기기 | `05-android-client-plan.md`의 Android 참조 클라이언트, 기종별 어댑터, TLS 및 등록 운영, 배터리, 열, 끊김 측정 | COM, DEV, DAT-04 | OQ-02, OQ-03 |

### 4.1 Stage C0 — Apple Silicon 로컬 실모델 검증 (ADR-0007)

목적은 실모델 출력이 프레임 → Provider → 엔진 → alert → ACK 경로를 통과하는지와 그때의 지연을 측정하는 것이다. 정확도 목표는 없다. 실행 환경은 Docker Linux arm64 CPU 전용이고, 시나리오는 COCO `person` 탐지 전용 `person_in_walkway`(profile `local_verify_c0`)다.

| Task | 제목 | REQ | 산출물 (SPEC 절) | 수용 테스트 | 선행 | 상태 |
|---|---|---|---|---|---|---|
| TASK-29 | vision 의존성과 RT-DETR Provider | INP-03, INP-05 | `apps/server/pyproject.toml` 선택 그룹 `vision`(torch, transformers, Pillow), §9.1 등록부와 허용 목록(`MIT-CMU`), `docker/server.Dockerfile` `vision-dev` target과 compose 서비스, `apps/server/src/voa/providers/rtdetr.py`, `apps/server/config/models/rtdetr-r18-coco.yaml`, `apps/server/config/profiles/local_verify_c0.yaml` (§9, ADR-0007) | 가짜 모델로 label 필터, bbox 정규화, 하한 점수, Draft 스키마, `model` 출처 검증. `vision` 미설치 시 명확한 오류. `scripts/check.sh`는 `vision` 없이 통과. `@pytest.mark.model`: 사용자가 받은 가중치의 sha256 대조 후 추론 1회 | B 게이트 | DONE |
| TASK-30 | 프레임 폴더 입력과 오프라인 평가 러너 | NFR-02, NFR-03, T-R14 | `apps/server/src/voa/simulator/frames.py`(읽기 전용 프레임 소스, `--fps`), `apps/server/src/voa/eval/__main__.py`(프레임 → Provider → 엔진, 서버와 같은 tick 간격, 결정 JSONL, PLAN §4 형식 보고서), `scripts/extract-frames.sh`(Docker ffmpeg, 네트워크 없음, 오디오와 메타데이터 제거, 긴 변 640px 이하) | 파일 이름 순서와 프레임 시각 결정론, 실행 중 파일 생성 0건, 손상 이미지는 건너뛰고 계수, 빈 폴더 오류, Provider/step p50/p95 출력, 정답 없을 때 recall/FP/hour `TBD` | 29 | DONE |
| TASK-31 | 시뮬레이터 실모델 모드 | INP-01, COM-02 | `python -m voa.simulator --frames DIR --fps N --model rtdetr-r18-coco`, 프레임 → Provider → `observation` 전송 | 가짜 Provider로 uvicorn E2E(경고, ACK), 추론이 프레임 간격보다 느리면 밀린 프레임을 건너뛰고 계수, `vision` 미설치 시 exit 2 | 29, 30 | DONE |
| TASK-32 | C0 실영상 검증과 임계값 조정 | NFR-02, NFR-03 | 사용자 영상 `walk01`(3fps, 98장)로 평가 러너와 시뮬레이터 → 서버 경로 실행, `local_verify_c0` v2(`min_score` 0.5 → 0.7), ADR-0007 측정 결과 | ROI 안 정지 물체가 0.65로 계속 잡혀도 경고 없음, 0.9 사람은 3장 뒤 경고 1회. 실영상: 경고 2회 모두 실제 사람, ACK received/played, provider p50/p95 기록 | 31 | DONE |

**Stage C0 완료 기준**: 사용자가 준비한 프레임 폴더로 평가 러너와 시뮬레이터 실모델 모드가 동작하고, M1 Docker CPU에서 측정한 Provider 추론 p50/p95와 경고 지연이 보고서로 남는다.

### 4.2 Stage C1 — 첫 실제 시나리오: 화재 (ADR-0008)

목적은 화재(화염) 시나리오 `fire_in_view`를 manifest와 profile만으로 추가하고, 화재 영상으로 경고 경로와 오경고를 측정하는 것이다. C1a는 학습 없이 open-vocabulary 모델로 먼저 시험하고, C1b는 D-Fire로 미세조정한 RT-DETR R18로 정확도를 측정한다. 연기는 label로만 기록하고 시나리오는 두지 않는다. 학습 환경(GPU)은 미정이라 TASK-38은 GPU가 정해질 때까지 대기한다(OQ-04).

| Task | 제목 | REQ | 산출물 (SPEC 절) | 수용 테스트 | 선행 | 상태 |
|---|---|---|---|---|---|---|
| TASK-33 | C1a open-vocabulary Provider와 화재 profile | INP-05, ENG-19 | `apps/server/src/voa/providers/omdet.py`(OmDet-Turbo, 프롬프트 = manifest `labels`), `apps/server/config/models/omdet-turbo-fire.yaml`(labels `[fire, smoke]`, `weights: file`), `apps/server/config/profiles/fire_c1.yaml`(시나리오 `fire_in_view`), 시뮬레이터와 평가 러너 `--model` 선택 (§9, ADR-0008) | 가짜 모델로 label 필터, bbox 정규화, Draft 스키마, `model` 출처. `fire_in_view`: 화염 연속 → 경고 1회, 사라짐 → 해제, 연기 단독 → 경고 없음. `@pytest.mark.model`: sha256 대조 후 추론 1회 | 32 | DONE |
| TASK-34 | 이벤트 정답 기반 평가 | NFR-02, T-R14, T-R20 | `python -m voa.eval --labels FILE`(구간 정답 JSONL: 시작, 끝 초), `event_recall`, `false_alerts_per_hour`, 첫 경고까지의 지연 | 정답 구간 안 경고 = 적중, 밖 = 오경고, 정답 없음 → `TBD` 유지, 겹치거나 역순 구간 → 오류, 결정론 | 30 | TODO |
| TASK-35 | 시뮬레이터 웹 페이지: 웹캠과 영상 업로드 | INP-01, COM-02 | ADR-0007 보강(브라우저 입력), 로컬 전용 웹 페이지(웹캠 실시간, 영상 파일 업로드 → 프레임 → Provider → 기존 `observation`), HUD 경고와 음성 출력, ACK, 상태와 지연 표시 | 프레임과 업로드 영상은 메모리에서만 처리하고 디스크, DB, 로그에 남지 않음(프라이버시 누출 검사 확장), `127.0.0.1` 전용, 추론이 늦으면 프레임 건너뜀, 가짜 Provider E2E | 31, 33 | SUPERSEDED (ADR-0009: 장치 쪽 추론 대신 서버 추론. 화면은 TASK-44) |
| TASK-36 | C1a 실영상 검증 | NFR-02, NFR-03, T-R20 | 공개 화재 영상, 소형 화염(촛불, 라이터), 오경고 유발 장면(조명, 석양, 반사, 주황색 조끼, 모니터)의 프레임 폴더 평가, ADR-0008 결과 절 | 장면별 경고 수와 오경고 원인 기록, Provider p50/p95, 정답 구간이 있으면 recall과 FP/hour | 33, 34 | TODO (촛불 공개 영상 1건 측정 완료, ADR-0008 결과 절. 정답 구간 평가는 TASK-34 후) |
| TASK-37 | D-Fire 데이터 준비 도구 | NFR-04 | 사용자가 받은 D-Fire(YOLO 형식) → `python -m voa.train.dfire`: 학습/검증/평가 분할 JSONL(정규화 꼭짓점 bbox, 객체 없는 이미지는 음성 샘플로 유지), `PublicDataset` 접두 이미지 기본 제외, 데이터 기록(제외 수, 분할 해시, 인용) | 합성 YOLO 라벨 폴더로 기본 제외와 옵션 포함, 분할 결정론, 잘못된 라벨 줄 건너뛰고 계수, 원본 폴더 변경 0건 | 32 | DONE |
| TASK-38 | RT-DETR R18 D-Fire 미세조정 | INP-05, OQ-11 | `python -m voa.train.finetune`(RT-DETR R18, AMP, val 클래스별 box P/R/F1과 fire 이미지 단위 recall/오검출률, `best/` `last/` `train_record.json`, head bias 사전확률 초기화, 점수 기준 sweep, 학습 후 test 평가, `--negatives`, `epochs.jsonl`), Docker `train-cuda` target과 `scripts/train.sh`(GPU PC에서 빌드), 선택 그룹 `train`(scipy, §9.1) | 맥 CPU 스모크(몇 step, 손실 감소와 산출 파일 형식), GPU 1회 학습은 사용자 환경에서 실행 | 37, OQ-04 | DONE |
| TASK-39 | C1b 미세조정 모델 평가와 비교 | NFR-02, NFR-03, OQ-06 | manifest `rtdetr-r18-dfire`(`redistribution_allowed: false`), D-Fire 평가 분할과 TASK-36 영상으로 C1a와 비교, `fire_c1` 임계값 제안 | 같은 프레임 폴더와 정답으로 두 모델 보고서, 임계값 변경은 측정 근거와 함께 기록 | 36, 38 | DONE (정답 구간 비교는 TASK-34 후) |

**Stage C1 완료 기준**: 화재 영상(공개 영상과 소형 화염)과 오경고 유발 장면에 대해 `fire_in_view`의 경고, 해제, 오경고 수와 지연이 보고서로 남고, 경고 경로에 쓸 모델과 임계값이 측정 근거와 함께 정해진다.

### 4.3 Stage C2 — 미션, 서버 추론, 관리자 설정, 웹 화면 (ADR-0009, ADR-0010)

목적은 장치가 프레임만 올리고 서버가 추론과 판정을 하는 제품 경로를, **여러 용도(미션)**로 운영할 수 있게 만드는 것이다. 사용자는 초기 화면에서 미션을 고르고, 그 미션에 특화된 모델과 규칙으로 감지한다. 관리자 페이지와 시뮬 페이지는 공개 API만 쓰고, 시뮬 페이지가 쓰는 API는 스마트 글래스 앱과 같다. 동시 장치 세션은 서버 1대에 3개다.

| Task | 제목 | REQ | 산출물 (SPEC 절) | 수용 테스트 | 선행 | 상태 |
|---|---|---|---|---|---|---|
| TASK-40 | WebSocket 핸드셰이크 인증과 TLS 규칙 | COM-01, DAT-02 | `voa.server.ws`(`Authorization` 헤더 또는 `Sec-WebSocket-Protocol: voa.v1, voa.bearer.<token>`, 실패 시 HTTP 401, `auth` 메시지 폐지), settings TLS 규칙(`VOA_EXPOSURE`: 네트워크 노출이면 필수, compose는 `loopback` 선언), `GlassClient` 핸드셰이크 인증, 기존 테스트 이전 (§7.1, §7.2) | 헤더와 subprotocol 각각 성공, token 없음, 무효, 다른 세션, 정리된 세션은 401, 응답 subprotocol은 `voa.v1`만, 폐지된 `auth`는 `unsupported_message_type`, 핸드셰이크 헤더가 로그에 없음, 루프백이 아닌 bind에 TLS가 없으면 시작 거부, 재접속 순서 유지 | 39 | DONE |
| TASK-40A | 계정: 고정 관리자와 착용자 가입 | COM-01, DAT-01, DAT-02 | `users` 테이블, `POST /v1/auth/login`, `/signup`, `/password`, 관리자 초기 비밀번호 안전장치(루프백만, 변경 전 403, 루프백이 아니면 시작 거부), 착용자 세션 생성(`username`, `password`), 시도 제한, `/v1/admin/users*`, `sessions.user_id`, 로그 키 `user_id` (§7.1, §7.7, §8.1, §8.2, ADR-0011) | 초기 비밀번호 루프백만과 변경 강제, 가입 형식과 중복, 승인 전 403 `account_pending`, 승인 후 로그인, 실패 원인 구분 없음과 같은 비용, 5회 실패 429, 사용 중지 시 세션 정리, DB와 로그에 평문 비밀번호와 `username` 없음 | 40 | DONE |
| TASK-41 | 미션 저장소와 세션의 미션 선택 | ENG-15, ENG-19 | 미션 모델과 저장(`VOA_ACTIVE_CONFIG_DIR/missions`, 버전, `draft`, 보관), 기존 profile 3개를 미션 v1로 가져오기, `POST /v1/sessions`의 `mission_id`, `GET /v1/missions`, `PUT /v1/sessions/{id}/mission`(엔진 재생성), `mission` 메시지, `VOA_DEFAULT_MISSION` (§7.1, §7.2, §7.7 미션) | 미션 지정 생성, 목록, 선택, 전환 시 모든 시나리오 `unknown`에서 시작, 없는 미션 404, 보관 미션 선택 불가, `policy_version`이 `<mission>@<version>`, 진행 중 세션은 게시 후에도 이전 버전 유지, 가져오기 결정론 | 40 | DONE |
| TASK-42 | 프레임 업로드 API와 서버 추론 파이프라인 | INP-01, INP-03, ENG-19, DAT-01 | `POST /v1/sessions/{id}/frames`(조건: 미션 선택과 제어 WS 연결), 서버 Provider 실행(워커, 모델 공유), 미션의 다중 모델 관측, `X-VOA-Quality-Flags`, 응답 `results`/`states`/`timing`, `202` + `frame_result`, `409 frame_busy`/`mission_not_selected`/`control_not_connected`, heartbeat `frames` (§7.6) | 가짜 모델로 프레임 → 관측 → 결정 → alert, 오류 표의 각 code, 처리 중 중복 409와 대기열 없음, 동기 한도 초과 시 202와 `frame_result`, 업로드 중에도 제어 WS alert가 늦어지지 않음, 모델 2개가 같은 `frame_id` 공유, `camera_motion_uncompensated` 프레임은 근거 제외, `timing`과 `capture_ts` 반환, 프레임 바이트가 디스크, DB, 로그에 없음 | 41 | DONE |
| TASK-43 | 동시 세션 상한과 부하 제어 | NFR-03, COM-01 | `VOA_MAX_SESSIONS`(세션 생성 503과 `Retry-After`, 제어 WS `error(capacity_exceeded)` 후 close 4503), 모델별 round-robin 작업 큐, 부하 시 간격 1.5배와 회복(`config`), `GET /v1/admin/capacity`, Docker `server-vision` target과 compose 서비스(실모델 서버) (§7.1, §7.2, §7.5) | 4번째 세션 503, 정원 초과 WS 4503과 안내 필드, 세션 정리 후 다시 허용, 한 세션의 과다 전송이 다른 세션을 굶기지 않음, 대기 증가 시 간격 증가와 회복, 간격이 staleness를 넘으면 `unknown`, 맥 CPU 3세션 실측(처리량, 간격) 기록 | 42 | DONE |
| TASK-44A | 관리자 미션 API | INP-05, ENG-15 | `/v1/admin/missions` 목록/생성/복제, `GET/PUT/DELETE /v1/admin/missions/{id}`, `:validate`, `:publish`(스키마, manifest, 가중치 로드), `/versions`, `GET /v1/admin/changes`, `admin_changes` 테이블, 버전별 미션 설정 `v<N>.settings.yaml`(`frame_interval_ms`, `hud_style_map`), 세션 응답/`mission` 메시지/`/v1/missions`의 `frame_interval_ms` (§7.7, §8.1) | 관리자 token 없음/무효 401, 초안 없음 400, 검증 실패 시 게시본 유지, 가중치 미준비 게시 거부, 버전 증가, 진행 중 세션은 자기 버전 유지, 사용 중 미션 보관 409, 변경 기록에 원문과 token 없음 | 41, 42 | DONE |
| TASK-44B | 모델 배치, 설정, HUD 표시 | INP-05 | `/v1/admin/models`, `deployments`(device, score_floor, 다중 배치, 게시 미션이 쓰는 모델 해제 409), `settings`(프레임 간격, `hud_styles`, `hud_style_map`), `detect`, alert/notice `presentation`(§7.4) (§7.7) | 설정 없으면 503, 다중 배치, 사용 중 모델 해제 409, `frame_interval_ms` 변경 시 `config`, HUD 스타일 선택(미션 > 전역, 시나리오 > 등급 > 기본), detect 이미지 미저장 | 44A | DONE |
| TASK-45 | 관리자 페이지 `/ui/admin` | INP-05, ENG-15 | 서버가 제공하는 정적 페이지(API만 호출, 시안 `docs/design/ui-mockups.md`): **관리자 로그인(첫 로그인 시 비밀번호 변경)**, **초기 화면 = 미션 목록과 등록, 수정, 삭제(보관), 복제**, 착용자 승인과 사용 중지, 미션 편집기(판정 규칙, 근거 모델, 임계값, HUD, 검증 후 게시), 모델 배치, 프레임 간격, 용량, 이미지 한 장 시험, 변경 기록 | API 가짜 응답으로 화면 상태 전환, 관리자 token이 URL과 저장소에 남지 않음, 검증 실패 문구 표시 | 44A, 44B | DONE |
| TASK-46 | 시뮬 페이지 `/ui/sim` | INP-01, COM-02, DEV-02 | 장치 역할 정적 페이지(시안 `docs/design/ui-mockups.md`): **로그인과 가입(승인 대기 안내)** → 세션 생성 → wss 핸드셰이크 → **초기 화면에서 미션 선택** → `auth_ok`와 미션 확정 후 캡처 시작(웹캠 또는 동영상, 진행 중 1개, `frame_interval_ms`), 글래스 뷰(§7.4 HUD 규칙, 상태 표시줄, 박스 없음), **판정 프레임 창**(보낸 프레임 위 박스와 프레임 나이), 표시 후 `played` ACK, `timing`, 정원 초과와 끊김 안내. TASK-35 초안 화면 요소 재사용 | 같은 API를 쓰는 테스트 클라이언트로 미션 선택 → 영상 프레임 → 경고 → `played`, 연결 전 업로드 없음, 동영상 파일 미업로드, `config`와 `mission` 수신 처리, 정원 초과 안내, `presentation`과 상태 표시줄 우선순위를 규칙표 기반 단위 테스트로 검증 | 40A, 42, 43, 44A, 44B | TODO |

**Android 연동 (소유자 지시, 2026-11-26)**: Android 참조 클라이언트도 이 계약(핸드셰이크 인증, 계정, 미션 선택, 연결 후 프레임 업로드, `presentation` HUD와 상태 표시줄, 정원 초과 안내)에 **맞춰 개발해야** 한다. 작업은 `docs/05-android-client-plan.md`의 **AND-06A**이고, 선행은 TASK-40, 40A, 41, 42, 43, 44다. 서버 Task를 진행할 때 Android 쪽 영향(`apps/android/protocol` 계약 스냅샷 포함)을 함께 보고한다.

**Stage C2 완료 기준**: 관리자 페이지에서 미션을 등록하고 게시하면, 시뮬 페이지에서 그 미션을 골라 동영상이나 웹캠으로 올린 프레임을 서버가 그 미션의 모델과 규칙으로 판정하고, HUD 경고, ACK, 구간별 지연이 화면과 기록에 남는다. 동시 3세션까지 동작하고 넘으면 안내한다. 프레임은 어디에도 저장되지 않는다.

평가 보고 형식(Stage C~): `run_id, code_commit, profile, model_artifacts{id,revision,sha256}, hardware, dataset, metrics{event_recall, false_alerts_per_hour, e2e_p95_ms, unknown_rate, unsupported_claim_rate}, limitations`. 측정하지 않은 값은 `TBD`로 둔다.

---

## 5. 에이전트 프롬프트

### 5.1 공통 (Codex는 매번 앞에 붙임. Claude Code는 `/task`가 자동 적용)
```text
AGENTS.md, docs/01-product-requirements.md, docs/02-technical-specification.md, docs/03-collaboration-architecture.md, docs/04-implementation-plan.md 중 이번 Task와 관련된 부분을 먼저 읽어라.
04-implementation-plan.md 에서 Task 하나만 구현한다. 경로/이름/스키마/상태/전이/reason_code 는 02-technical-specification.md 그대로 쓴다.
기본 실행은 로컬/mock. 외부 API 호출, 가중치 다운로드, 음성/영상 파일 저장 금지.
시작 전 수정 파일, REQ/INV/T-R ID, 테스트 계획을 짧게 보고하라.
끝나면 scripts/check.sh 결과와 함께 아래 보고 템플릿으로 보고하고 04-implementation-plan.md 상태를 갱신하라.
```

### 5.2 Task 실행 (Codex)
```text
04-implementation-plan.md 의 <TASK-ID> 를 구현하라. 선행 Task 가 DONE 이 아니면 중단하고 보고하라.
```

### 5.3 감사 (구현 후 수시)
```text
01-product-requirements.md 의 M 우선순위 REQ 와 02-technical-specification.md §4.0 INV 기준으로 compliance audit 를 하라. 코드는 수정하지 않는다.
결함은 위험도순으로, 각 항목에 파일:줄 근거를 붙인다. 확인하지 못한 항목은 PASS 가 아니라 UNVERIFIED.
중점: 근거 없는 warning, stale 근거, unknown→clear 오판, 중복 alert, raw media/토큰 로그, 비신뢰 텍스트의 명령 승격, 라이선스 미등록.
```

### 5.4 보고 템플릿
```md
### Task
- ID / REQ / INV / T-R:
- Files changed:
### 검증
- Commands + 결과 (passed / failed / skipped):
### 설계 영향
- 스키마/전이/SPEC 변경 여부:
### 미완료/위험
- NOT TESTED 항목과 이유, 후속 Task 제안:
```

### 5.5 금지 문구
"환각 0%", "사고 예방 보장", "모든 글래스 지원", "8GB GPU면 충분", 측정 근거가 없는 latency/accuracy 수치, "LLM이 경고를 직접 판단하도록 변경", "성능 테스트를 위해 원본 저장".
