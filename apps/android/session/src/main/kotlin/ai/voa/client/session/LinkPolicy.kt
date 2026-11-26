package ai.voa.client.session

/** 재접속 간격 1, 2, 4, 8초(최대 8초). `auth_ok`를 받으면 처음부터 (SPEC 7.4). */
class ReconnectPolicy(private val delaysMs: List<Long> = listOf(1_000L, 2_000L, 4_000L, 8_000L)) {
    private var attempt = 0

    fun nextDelayMs(): Long = delaysMs[minOf(attempt, delaysMs.size - 1)].also { attempt += 1 }

    fun reset() {
        attempt = 0
    }
}

/**
 * 장치 쪽 watchdog (SPEC 7.4): `watchdog_ms` 동안 서버 메시지가 하나도 없으면 끊긴 것으로 본다.
 * 경계: 마지막 메시지 후 정확히 `watchdog_ms`가 지나면 만료.
 */
class Watchdog(private val watchdogMs: Long) {
    private var lastMessageMs: Long? = null

    fun onMessage(nowMs: Long) {
        lastMessageMs = nowMs
    }

    fun reset() {
        lastMessageMs = null
    }

    fun expired(nowMs: Long): Boolean {
        val last = lastMessageMs ?: return false
        return nowMs - last >= watchdogMs
    }
}

/** 지연 실행 포트. 앱은 Handler/코루틴으로, 테스트는 가짜로 구현한다. */
fun interface Scheduler {
    fun schedule(delayMs: Long, task: () -> Unit): Cancellable
}

fun interface Cancellable {
    fun cancel()
}
