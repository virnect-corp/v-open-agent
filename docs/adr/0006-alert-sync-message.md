# ADR-0006: WebSocket `resume` 메시지를 `alert_sync`로 부른다

- Status: accepted
- Date: 2026-11-18
- Owners: Server, Contract, Android
- Related: ADR-0002, ADR-0005, COM-04, DEV-04, SPEC §7.2~7.4, TASK-20, T-R10, T-R16, UC-05

## Context

- WebSocket `resume {last_alert_seq}` 메시지는 "장치가 마지막으로 받은 경고 번호를 알리고, 그 뒤 경고 중 아직 의미 있는 것만 다시 받는다"는 뜻이다.
- 감지와 판단은 연결과 무관하게 서버 엔진에서 계속 진행된다. 다시 감지하거나 재개하는 것이 없으므로 `resume`은 무엇을 재개하는지 드러나지 않는다. ADR-0005에서 REST token 회전을 `refresh`로 바꾼 뒤에도 이 이름이 혼동의 원인으로 남았다.
- `reconnect`도 검토했지만 보내는 시점(재접속)만 가리키고 서버 동작을 드러내지 않는다. 처음 연결에서 `last_alert_seq=0`으로 보내는 경우와도 맞지 않고, 소켓 재연결과 장치 watchdog의 backoff 재접속과 이름이 겹친다.

## Decision

1. WebSocket 메시지 `resume`을 **`alert_sync`**로 부른다. 필드는 그대로 `last_alert_seq`(0 이상)다.
2. 의미: "장치는 `last_alert_seq`까지 받았다. 그 뒤 alert 중 SPEC §7.3의 세 조건을 모두 만족하는 것만 같은 `alert_id`로 다시 보내라." 처음 연결에서는 0을 보낸다.
3. 재접속 순서: (토큰 만료 시) REST `refresh` → WebSocket `auth` → WebSocket `alert_sync(last_alert_seq)`.
4. `auth` 전에 오면 `protocol_error` 후 close 4401이다(기존 `resume` 규칙 그대로).

## Alternatives

- `resume` 유지: 무엇을 재개하는지 모호하고 과거 resume token과도 혼동된다. 기각.
- `reconnect`: 위 Context의 이유로 기각.
- `re-detect`: 다시 감지하지 않으므로 동작과 다르다. 기각.

## Consequences

- WebSocket 계약(`contracts/messages.py`, `ws_client_message` 스냅샷)과 Android `TransportMessages.kt`, `ProtocolContract.kt`를 같은 변경 세트로 바꾼다.
- 되돌림: 이름만 되돌리면 되고 동작은 같다.

## Verification

- TASK-20: `alert_sync` 처리와 재전송 조건 1/2/3 테스트, 구 `resume` type은 `unsupported_message_type`.
- Android: `AlertSyncMessage` encode/decode, 구 `resume` type 거부 테스트.
