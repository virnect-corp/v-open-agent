# ADR-0005: resume token을 refresh token으로 부르고, refresh token이 만료된 세션은 정리한다

- Status: accepted (결정 2의 WebSocket `resume` 메시지 이름은 ADR-0006에서 `alert_sync`로 변경)
- Date: 2026-11-18
- Owners: Server, Contract, Android
- Related: ADR-0002, COM-01, COM-04, SPEC §7.1, §8.1, §8.3, TASK-17, TASK-20, TASK-27

## Context

- ADR-0002는 오래 유효한 token으로 짧은 access token을 다시 받고, 쓸 때마다 회전하도록 정했다. 이 역할은 일반적인 refresh token과 같지만 이름이 "resume token"이었다.
- WebSocket에도 `resume(last_alert_seq)` 메시지가 있다. 이것은 token과 무관하게 "마지막으로 받은 경고 번호 이후를 이어 받겠다"는 재접속 신호다. 같은 단어가 두 가지 다른 동작을 가리켜 혼동이 생긴다.
- 서버는 세션 엔진과 tick 루프를 메모리에 두는데(TASK-18), 세션을 끝내는 규칙이 없어 프로세스가 끝날 때까지 남는다.

## Decision

1. **명칭 변경**: token은 `refresh_token`, endpoint는 `POST /v1/sessions/{session_id}/refresh {refresh_token}`, 응답 필드는 `refresh_token`, `refresh_token_expires_at_utc`, 설정은 `VOA_REFRESH_TOKEN_TTL_S`(기본 86400)로 부른다. 회전 규칙(일회성, 이전 token 둘 즉시 폐기, 새 refresh token 만료는 회전 시점부터 다시 계산)은 ADR-0002 그대로다.
2. **WebSocket `resume` 메시지는 이름을 바꾸지 않는다.** 재접속 순서는 REST `refresh` → WebSocket `auth` → WebSocket `resume(last_alert_seq)`이다.
3. **세션 정리**: WebSocket 연결이 없고 refresh token이 만료된 세션은 정리한다. tick 루프를 멈추고, 엔진과 token digest를 메모리에서 지우고, `sessions.ended_at_utc`를 기록한다. 연결 중인 세션은 연결이 끝날 때까지 유지한다. 정리 검사 주기는 `VOA_SESSION_SWEEP_INTERVAL_S`(기본 60)다.
4. 정리된 세션의 refresh는 401, WebSocket `auth`는 close 4401이다. 장치는 credential로 새 세션을 만든다(서버 재시작 후와 같다).

## Alternatives

- **이름을 그대로 둠**: WebSocket `resume`과 계속 혼동된다. 기각.
- **access token 만료 시 세션 정리**: 15분 안에 재접속하지 못한 장치가 세션과 진행 중인 episode를 잃는다. 재접속 창을 refresh token 수명으로 둔다는 ADR-0002 의도와 맞지 않는다. 기각.
- **연결 중인 세션도 refresh token 만료 시 끊음**: 진행 중인 경고 전달을 끊는다. 기각.

## Consequences

- 서버와 Android의 REST 계약이 바뀐다(`resume_token` → `refresh_token`, `/resume` → `/refresh`). 두 쪽을 같은 변경 세트로 맞춰야 한다(03-collaboration-architecture §3).
- 기본값으로는 연결이 끊긴 세션이 최대 24시간 메모리에 남는다. 더 짧게 하려면 `VOA_REFRESH_TOKEN_TTL_S`를 줄인다.
- 되돌림: 명칭만 되돌리면 되고 동작은 같다.

## Verification

- TASK-27: 이름 변경 후 TASK-17 테스트가 새 이름으로 통과, 구 endpoint `/resume`과 구 필드는 존재하지 않음, refresh token 만료 + 미연결 세션은 정리(메모리 제거, `ended_at_utc` 기록, refresh 401, auth 4401), 연결 중 세션은 유지, 연결 종료 후 다음 검사에서 정리.
- Android: 같은 이름으로 `TransportMessages.kt` 갱신 후 계약 테스트 통과.
