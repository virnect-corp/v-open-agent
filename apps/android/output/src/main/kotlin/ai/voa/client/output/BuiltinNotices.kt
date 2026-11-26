package ai.voa.client.output

// 서버 연결 단절 시 장치가 스스로 출력하는 내장 고정 문구 (SPEC 7.4)
object BuiltinNotices {
    const val WATCHDOG_DISCONNECTED_TEXT: String = "서버 연결이 끊겨 경고를 받을 수 없습니다."
    const val CAPACITY_TEXT: String = "동시 접속 정원을 넘었습니다. 잠시 후 다시 시도하세요."
    const val DEGRADED_STATUS_LABEL: String = "감지 제한"
    const val RECONNECTED_STATUS_LABEL: String = "연결 복구"
}

