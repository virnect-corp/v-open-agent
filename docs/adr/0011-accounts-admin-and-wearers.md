# ADR-0011: 계정 - 고정 관리자와 착용자 가입

- Status: accepted (2026-11-26, 소유자 결정)
- Date: 2026-11-26
- Owners: Server, Contract, Web, Android
- Related: COM-01, DAT-01, DAT-02, ADR-0002, ADR-0005, ADR-0009, ADR-0010, SPEC §7.1, §7.7, §8.1, §8.2

## Context

- 지금은 장치(`devices.yaml`의 `device_id`와 secret)로만 세션을 만들고, 관리자 API는 파일 기반 token(ADR-0009)이다. 사람 단위 계정은 없다.
- 소유자 요구(2026-11-26): 인증이 필요하므로 관리자는 `admin/admin`으로 고정하고, **착용자는 가입해서 등록**할 수 있어야 한다.
- 고정된 기본 비밀번호는 흔한 침해 경로다. 엣지 서버가 사내망에 노출되면 누구나 관리자로 들어올 수 있다. 그래서 요구를 지키되 다음 안전장치를 붙인다.

## Decision

### 1. 관리자 계정

- 서버에는 관리자 계정 `admin`이 하나 있고, 초기 비밀번호는 `admin`이다.
- **안전장치 (기본값)**:
  - 초기 비밀번호 상태에서는 `127.0.0.1` 접속에서만 로그인을 받는다.
  - 초기 비밀번호로 로그인하면 응답에 `password_change_required: true`를 담는다. 비밀번호를 바꾸기 전까지 관리자 API는 403 `password_change_required`다. 바꾸는 API만 허용한다.
  - `VOA_BIND`가 `127.0.0.1`이 아닌데 관리자 비밀번호가 초기값이면 서버는 시작하지 않는다.
- 관리자 로그인: `POST /v1/auth/login {username: "admin", password}` → `{token, token_expires_at_utc, role: "admin", password_change_required}`. 관리자 API는 이 token을 `Authorization: Bearer`로 쓴다.
- ADR-0009의 파일 기반 관리자 token(`VOA_ADMIN_TOKEN_FILE`)은 이것으로 대체한다.

### 2. 착용자 가입과 승인

- 가입: `POST /v1/auth/signup {username, password, display_name?}` → 201 `{user_id, status}`
  - `username`: 영문 소문자, 숫자, `_`, `-`로 3~32자. 중복이면 409 `username_taken`
  - 비밀번호: 8자 이상이고 `username`과 같을 수 없다.
- **승인 (기본값)**: 가입하면 `pending` 상태다. 관리자가 승인해야 `active`가 되어 로그인할 수 있다.
  - `VOA_SIGNUP_REQUIRES_APPROVAL=false`로 바꾸면 가입 즉시 `active`다(개발용).
  - `VOA_SIGNUP_ENABLED=false`면 가입 API가 403 `signup_disabled`다.
- 관리자 관리:

  | 메서드와 경로 | 동작 |
  |---|---|
  | `GET /v1/admin/users` | 목록 (`pending` 먼저) |
  | `POST /v1/admin/users/{id}:approve` | 승인 |
  | `POST /v1/admin/users/{id}:disable` | 사용 중지. 진행 중인 세션은 정리한다 |
  | `POST /v1/admin/users/{id}:reset-password` | 임시 비밀번호 발급. 다음 로그인 때 변경 필요 |

### 3. 착용자 세션 (글래스와 시뮬)

- 착용자는 계정으로 세션을 만든다.
  - `POST /v1/sessions {username, password, device_label?, capabilities, mission_id?}`
  - `device_label`은 표시용이다(예: "현장 A 글래스 1"). 인증에는 쓰지 않는다.
  - 응답은 지금과 같다(§7.1). 세션에 `user_id`가 붙는다.
- 관리자 계정도 시뮬 페이지에서 세션을 만들 수 있다(소유자 요구 2026-11-26, TASK-46A). 기본 비밀번호를 바꾸기 전에는 403 `password_change_required`다. 관리자 페이지의 "시뮬레이터" 메뉴로 열면 다시 로그인하지 않는다: 관리자 페이지가 연 창이 같은 출처에서 준비 메시지를 보내면, 관리자 페이지가 로그인 token을 `postMessage`로 그 창에만 넘기고(URL과 저장소에는 두지 않음), 시뮬 페이지는 `POST /v1/sessions`에 `Authorization: Bearer <관리자 로그인 token>`과 `capabilities`만 보내 세션을 만든다. 시뮬 페이지를 직접 열면 로그인 화면이 나온다. 세션은 `sessions.user_id`에 관리자 ID로 남고 동시 세션 상한에 포함된다.
- 그 뒤 흐름은 ADR-0010과 같다.
  1. `GET /v1/missions`
  2. 미션 선택
  3. wss 핸드셰이크
  4. `auth_ok` 후 프레임 전송
- 인증 실패는 원인을 구분하지 않고 401이다. 비밀번호 오류, 없는 계정, `pending`, `disabled`를 모두 같게 다룬다.
  - 단, 비밀번호가 맞는데 `pending`이면 403 `account_pending`을 줄 수 있다. 착용자가 "승인 대기"를 알 수 있게 하기 위해서다. 비밀번호가 맞을 때만 상태를 알려 주므로 계정 존재 여부는 새지 않는다.
- 기존 장치 credential(`devices.yaml`)로 만드는 세션은 사람이 없는 장치와 테스트용으로 남긴다. 둘 중 하나로 인증한다.
- 동시 세션 상한(`VOA_MAX_SESSIONS`, ADR-0010)은 계정과 장치를 합쳐 센다. 한 착용자가 여러 세션을 열 수 있지만 상한 안에서다.

### 4. 저장과 보안

- 비밀번호는 기존 장치 secret과 같은 scrypt(n=2^14, r=8, p=1, 16바이트 salt, 32바이트 digest)로 저장하고, 검증은 `hmac.compare_digest`로 한다. 평문은 저장하지도 로그에 남기지도 않는다.
- 없는 계정도 같은 비용의 scrypt를 계산한다. 응답 시간으로 계정 존재 여부를 알 수 없게 하기 위해서다.
- 로그인 실패가 같은 `username`으로 5분 안에 5회면 그 계정의 로그인을 5분 동안 429 `too_many_attempts`로 막는다.
- 루프백이 아닌 bind에서는 TLS가 필수다(ADR-0009). 그래서 비밀번호는 https로만 오간다.
- SQLite `users(user_id PK, username UNIQUE, display_name NULL, role, status, password_salt_hex, password_digest_hex, password_change_required, created_at_utc, updated_at_utc)`를 추가한다. 이름 외의 개인정보(연락처, 소속)는 받지 않는다.
- `sessions`에 `user_id NULL` 열을 추가한다(장치 credential 세션은 NULL).
- 로그 허용 키에 `user_id`를 추가한다. `username`, `display_name`, 비밀번호는 로그에 남기지 않는다.
- 계정 정보는 위험 판단에 쓰지 않는다. 엔진은 사람을 모른다(AGENTS 규칙 2, 4).

## Alternatives

- **관리자 비밀번호를 설치 시 무작위 생성**: 더 안전하지만 소유자 요구(`admin/admin` 고정)와 다르다. 대신 첫 로그인 변경과 루프백 제한으로 위험을 줄인다.
- **가입 즉시 활성**: 편하지만 엣지 서버에 누구나 세션을 열어 동시 3세션을 차지할 수 있다. 기본은 승인으로 두고 설정으로 끌 수 있다.
- **외부 인증(SSO, OAuth)**: 지금 범위를 넘는다. 계정 저장소를 경계 뒤에 두어 나중에 바꿀 수 있게 한다.

## Consequences

- 착용자별 세션과 기록 추적이 가능하다(`sessions.user_id`). 사람을 판단에 쓰지는 않는다.
- 비용: 계정 테이블, 가입, 승인, 로그인, 시도 제한, 비밀번호 변경 화면이 추가된다.
- 운영: 사내망에 노출하기 전에 관리자 비밀번호를 바꾸지 않으면 서버가 시작하지 않는다.
- **Android 연동**: Android 참조 클라이언트도 이 결정에 맞춰 개발해야 한다(`docs/05-android-client-plan.md` AND-06A, 소유자 지시 2026-11-26).

## Verification

- 관리자:
  - 초기 비밀번호는 루프백에서만 로그인되고, 변경 전에는 관리자 API가 403이다.
  - 변경 후 정상 동작한다.
  - 루프백이 아닌 bind에서 초기 비밀번호면 시작을 거부한다.
- 가입: 형식 위반 400, 중복 409, 기본 `pending`, 승인 후 로그인, 승인 끄면 즉시 `active`, 가입 끄면 403
- 로그인:
  - 비밀번호 오류, 없는 계정, `disabled`는 모두 401이고 응답 시간 차이가 없다.
  - 비밀번호가 맞고 `pending`이면 403 `account_pending`이다.
  - 5회 실패하면 429다.
  - 사용 중지하면 진행 중인 세션이 정리된다.
- 저장과 로그: DB에 평문 비밀번호 없음, 로그에 `username`과 비밀번호 없음(프라이버시 검사 확장)
