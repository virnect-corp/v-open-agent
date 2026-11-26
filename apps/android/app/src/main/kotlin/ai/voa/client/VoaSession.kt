package ai.voa.client

import ai.voa.client.protocol.ConfigMessage
import ai.voa.client.protocol.CreateSessionResponse
import ai.voa.client.protocol.FrameResult
import ai.voa.client.protocol.FrameResultMessage
import ai.voa.client.protocol.HeartbeatMessage
import ai.voa.client.protocol.MissionMessage
import ai.voa.client.protocol.MissionRef
import ai.voa.client.protocol.RiskState
import ai.voa.client.protocol.ServerMessage
import ai.voa.client.session.Cancellable
import ai.voa.client.session.ControlChannel
import ai.voa.client.session.ControlListener
import ai.voa.client.session.FrameUploader
import ai.voa.client.session.LinkState
import ai.voa.client.session.SessionApi
import ai.voa.client.session.SessionTokens
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import okhttp3.HttpUrl
import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit

/**
 * 앱 전체가 함께 쓰는 세션 (메모리에만). 로그인 화면, 미션 화면, 감지 화면이 바뀌어도
 * 제어 연결이 유지된다. 프로세스가 끝나면 사라진다(새로 로그인).
 */
object VoaSession {
    val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .build()

    private val main = Handler(Looper.getMainLooper())

    var api: SessionApi? = null
        private set
    var baseUrl: HttpUrl? = null
        private set
    var created: CreateSessionResponse? = null
        private set
    var tokens: SessionTokens? = null
        private set
    var channel: ControlChannel? = null
        private set
    var uploader: FrameUploader? = null
        private set

    var mission: MissionRef? = null
    var missionConfirmed = false

    @Volatile var states: Map<String, RiskState> = emptyMap()
        private set
    @Volatile var lastResult: FrameResult? = null
        private set
    @Volatile var announcement: String? = null

    /** 지금 화면에 연결 이벤트를 넘긴다(한 번에 하나). */
    var screen: ControlListener? = null

    val active: Boolean get() = channel != null

    private val tick = object : Runnable {
        override fun run() {
            channel?.tick()
            main.postDelayed(this, 250)
        }
    }

    fun start(url: HttpUrl, sessionApi: SessionApi, session: CreateSessionResponse) {
        stop()
        api = sessionApi
        baseUrl = url
        created = session
        mission = session.mission
        missionConfirmed = false
        val sessionTokens = SessionTokens(session.token, session.refreshToken)
        tokens = sessionTokens
        uploader = FrameUploader(
            client, url, session.sessionId, sessionTokens, { SystemClock.elapsedRealtime() },
            session.mission?.frameIntervalMs ?: session.frameIntervalMs,
        )
        channel = ControlChannel(
            client = client,
            baseUrl = url,
            sessionId = session.sessionId,
            tokens = sessionTokens,
            api = sessionApi,
            clock = { SystemClock.elapsedRealtime() },
            scheduler = { delayMs, task ->
                val runnable = Runnable { Thread { task() }.start() } // refresh is blocking I/O
                main.postDelayed(runnable, delayMs)
                Cancellable { main.removeCallbacks(runnable) }
            },
            watchdogMs = session.watchdogMs.toLong(),
            listener = Forwarder,
        )
        channel?.start()
        main.postDelayed(tick, 250)
    }

    fun confirmMission(ref: MissionRef) {
        mission = ref
        missionConfirmed = true
        states = emptyMap()
        uploader?.intervalMs = ref.frameIntervalMs
        refreshGate()
    }

    fun recordResult(result: FrameResult) {
        lastResult = result
        states = result.states
    }

    fun stop() {
        main.removeCallbacks(tick)
        channel?.stop()
        channel = null
        uploader = null
        tokens = null
        created = null
        api = null
        mission = null
        missionConfirmed = false
        states = emptyMap()
        lastResult = null
    }

    /** 업로드는 제어 연결이 인증되고 미션이 확정된 뒤에만 (ADR-0009). */
    private fun refreshGate() {
        uploader?.enabled = channel?.state == LinkState.ACTIVE && missionConfirmed
    }

    private object Forwarder : ControlListener {
        override fun onState(state: LinkState) {
            refreshGate()
            main.post { screen?.onState(state) }
        }

        override fun onMessage(message: ServerMessage) {
            when (message) {
                is ConfigMessage -> uploader?.intervalMs = message.frameIntervalMs
                is MissionMessage -> {
                    mission = MissionRef(message.missionId, message.version, message.name, message.frameIntervalMs)
                    uploader?.intervalMs = message.frameIntervalMs
                }
                is HeartbeatMessage -> states = message.states
                is FrameResultMessage -> recordResult(
                    FrameResult(message.frameId, message.seq, message.status, message.results, message.states, message.timing),
                )
                else -> Unit
            }
            main.post { screen?.onMessage(message) }
        }

        override fun onDisconnectAnnounce() {
            main.post { screen?.onDisconnectAnnounce() }
        }

        override fun onCapacity(retryAfterS: Int?) {
            main.post { screen?.onCapacity(retryAfterS) }
        }
    }
}
