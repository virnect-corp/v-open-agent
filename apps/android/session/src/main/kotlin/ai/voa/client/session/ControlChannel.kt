package ai.voa.client.session

import ai.voa.client.protocol.AlertSyncMessage
import ai.voa.client.protocol.AuthOkMessage
import ai.voa.client.protocol.ClientMessage
import ai.voa.client.protocol.ErrorMessage
import ai.voa.client.protocol.ProtocolContract
import ai.voa.client.protocol.ProtocolParseException
import ai.voa.client.protocol.ServerMessage
import okhttp3.HttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener

enum class LinkState {
    CONNECTING,
    ACTIVE, // auth_ok 받음
    RECONNECTING,
    CAPACITY, // 정원 초과 (close 4503), 자동 재접속하지 않음
    UNAUTHORIZED, // token과 refresh가 모두 거절됨
    STOPPED,
}

/** 세션 token은 메모리에만 둔다. toString에 나오지 않는다. */
class SessionTokens(var token: String, var refreshToken: String) {
    override fun toString(): String = "SessionTokens(<redacted>)"
}

interface ControlListener {
    fun onState(state: LinkState) {}

    fun onMessage(message: ServerMessage) {}

    /** 연결이 끊긴 순간 1회: 장치 내장 문구를 내보낸다 (SPEC 7.4). */
    fun onDisconnectAnnounce() {}

    /** 정원 초과 1회: 내장 문구와 재시도 시간 (ADR-0010). */
    fun onCapacity(retryAfterS: Int?) {}
}

/**
 * 제어 WebSocket `/v1/streams/{session_id}` (SPEC 7.1~7.4, ADR-0009).
 * - 인증은 핸드셰이크 `Authorization: Bearer` 헤더. 첫 메시지 `auth`는 없다.
 * - `auth_ok` 후 `alert_sync`, 서버 메시지마다 watchdog 갱신
 * - 끊기면 1/2/4/8초 재접속, 재접속 전에 refresh로 token을 갱신한다
 * - close 4503은 정원 초과(재접속 안 함), 4401/1008 또는 HTTP 401은 refresh 후 재시도,
 *   refresh도 401이면 UNAUTHORIZED
 * 원문 프레임, token은 로그로 남기지 않는다.
 */
class ControlChannel(
    private val client: OkHttpClient,
    private val baseUrl: HttpUrl,
    private val sessionId: String,
    private val tokens: SessionTokens,
    private val api: SessionApi,
    private val clock: MonotonicClock,
    private val scheduler: Scheduler,
    watchdogMs: Long,
    private val listener: ControlListener,
) {
    private val watchdog = Watchdog(watchdogMs)
    private val reconnect = ReconnectPolicy()
    private var socket: WebSocket? = null
    private var pending: Cancellable? = null
    private var announced = false

    var state: LinkState = LinkState.STOPPED
        private set

    /** 받은 가장 큰 alert_seq. 재접속하면 `alert_sync`로 알린다. */
    var lastAlertSeq: Int = 0

    fun start() {
        if (state != LinkState.STOPPED) return
        open()
    }

    fun stop() {
        pending?.cancel()
        pending = null
        setState(LinkState.STOPPED)
        socket?.close(1000, null)
        socket = null
    }

    fun send(message: ClientMessage): Boolean {
        if (state != LinkState.ACTIVE) return false
        val text = ProtocolContract.json.encodeToString(ClientMessage.serializer(), message)
        return socket?.send(text) ?: false
    }

    /** 앱이 주기적으로 부른다(예: 250 ms). */
    fun tick() {
        if (state == LinkState.ACTIVE && watchdog.expired(clock.nowMs())) {
            lost()
        }
    }

    private fun open() {
        setState(LinkState.CONNECTING)
        watchdog.reset()
        // OkHttp upgrades an http(s) URL to ws(s); the token rides in the handshake header.
        val request = Request.Builder()
            .url(baseUrl.resolve("v1/streams/$sessionId") ?: error("bad session id"))
            .header("Authorization", "Bearer ${tokens.token}")
            .build()
        socket = client.newWebSocket(request, Listener())
    }

    private inner class Listener : WebSocketListener() {
        override fun onMessage(webSocket: WebSocket, text: String) {
            if (webSocket !== socket) return
            val message = try {
                ProtocolContract.decodeServerMessage(text)
            } catch (_: ProtocolParseException) {
                return // unknown or invalid frames are dropped; the raw text is never logged
            }
            watchdog.onMessage(clock.nowMs())
            when (message) {
                is AuthOkMessage -> {
                    reconnect.reset()
                    announced = false
                    setState(LinkState.ACTIVE)
                    send(AlertSyncMessage(lastAlertSeq))
                }
                is ErrorMessage -> if (message.code == "capacity_exceeded") listener.onCapacity(message.retryAfterS)
                else -> Unit
            }
            listener.onMessage(message)
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(code, null)
            closed(webSocket, code, httpStatus = null)
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            closed(webSocket, code = null, httpStatus = response?.code)
        }
    }

    private fun closed(webSocket: WebSocket, code: Int?, httpStatus: Int?) {
        if (webSocket !== socket || state == LinkState.STOPPED) return
        socket = null
        when {
            code == CLOSE_CAPACITY -> {
                setState(LinkState.CAPACITY)
            }
            code == CLOSE_UNAUTHORIZED || code == CLOSE_POLICY || httpStatus == 401 -> retryWithRefresh()
            else -> lost()
        }
    }

    private fun lost() {
        val wasActive = state == LinkState.ACTIVE
        socket?.cancel()
        socket = null
        if (wasActive && !announced) {
            announced = true
            listener.onDisconnectAnnounce()
        }
        retryWithRefresh()
    }

    private fun retryWithRefresh() {
        setState(LinkState.RECONNECTING)
        pending?.cancel()
        pending = scheduler.schedule(reconnect.nextDelayMs()) {
            pending = null
            if (state != LinkState.RECONNECTING) return@schedule
            when (val refreshed = api.refresh(sessionId, tokens.refreshToken)) {
                is ApiResult.Ok -> {
                    tokens.token = refreshed.value.token
                    tokens.refreshToken = refreshed.value.refreshToken
                    open()
                }
                is ApiResult.Failed ->
                    if (refreshed.status == 401) setState(LinkState.UNAUTHORIZED) else retryWithRefresh()
            }
        }
    }

    private fun setState(next: LinkState) {
        if (state == next) return
        state = next
        listener.onState(next)
    }

    companion object {
        const val CLOSE_CAPACITY = 4503
        const val CLOSE_UNAUTHORIZED = 4401
        const val CLOSE_POLICY = 1008
    }
}
