package ai.voa.client

import ai.voa.client.protocol.AckMessage
import ai.voa.client.protocol.AckStatus
import ai.voa.client.protocol.AlertMessage
import ai.voa.client.protocol.ConfigMessage
import ai.voa.client.protocol.CreateSessionRequest
import ai.voa.client.protocol.CreateSessionResponse
import ai.voa.client.protocol.InputCap
import ai.voa.client.protocol.LoginResponse
import ai.voa.client.protocol.OutputCap
import ai.voa.client.protocol.SessionCapabilities
import ai.voa.client.protocol.FrameResult
import ai.voa.client.protocol.FrameResultMessage
import ai.voa.client.protocol.HeartbeatMessage
import ai.voa.client.protocol.MissionMessage
import ai.voa.client.protocol.MissionRef
import ai.voa.client.protocol.RiskState
import ai.voa.client.protocol.ServerMessage
import ai.voa.client.session.ApiResult
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
 * 앱 전체가 함께 쓰는 로그인과 연결 (메모리에만, 프로세스가 끝나면 사라진다).
 * - 로그인: 계정 확인과 로그인 token. 정원을 차지하지 않는다 (TASK-47)
 * - 연결: 미션을 고를 때 만드는 세션과 제어 연결. 감지 화면을 나가면 해제한다
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
    private var loginToken: String? = null // memory only; never logged or shown
    var role: String? = null
        private set
    var deviceLabel: String? = null
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

    /** A message for the mission screen after detection ended on its own (e.g. session ended). */
    var notice: String? = null

    /** Whether the device has a TTS engine (AOSP devices may not, OQ-14). Set at app start. */
    @Volatile var speechAvailable = true

    // status bar inputs (SPEC 7.4, AND-04)
    @Volatile var capacity = false
    @Volatile var lastServerMessageMs: Long? = null
    @Volatile var lastResponseMs: Long? = null

    /** 지금 화면에 연결 이벤트를 넘긴다(한 번에 하나). */
    var screen: ControlListener? = null

    val loggedIn: Boolean get() = loginToken != null
    val connected: Boolean get() = channel != null

    /** The bearer for mission lists: the login token (TASK-47). */
    fun missionsToken(): String? = tokens?.token ?: loginToken

    fun signedIn(url: HttpUrl, sessionApi: SessionApi, login: LoginResponse, label: String) {
        logout()
        api = sessionApi
        baseUrl = url
        loginToken = login.token
        role = login.role
        deviceLabel = label
    }

    /** Connect with the chosen mission: a session from the login token, then the control socket. */
    fun connect(missionId: String): ApiResult<CreateSessionResponse> {
        val sessionApi = api ?: return ApiResult.Failed(401, "unauthorized")
        val url = baseUrl ?: return ApiResult.Failed(401, "unauthorized")
        val request = CreateSessionRequest(
            missionId = missionId,
            deviceLabel = deviceLabel,
            capabilities = SessionCapabilities(
                inputs = listOf(InputCap.CAMERA_0),
                outputs = listOfNotNull(OutputCap.HUD, OutputCap.SPEAKER.takeIf { speechAvailable }, OutputCap.HAPTIC),
                hud = DetectionActivity.PHONE_HUD,
            ),
        )
        val result = sessionApi.createSession(request, loginToken)
        if (result is ApiResult.Ok) {
            main.post {
                start(url, sessionApi, result.value)
                result.value.mission?.let { confirmMission(it) }
            }
        }
        return result
    }

    /** Disconnect: stop the socket and end the session so the slot is free; the login stays. */
    fun disconnect() {
        val sessionApi = api
        val session = created
        val sessionToken = tokens?.token
        stopConnection()
        if (sessionApi != null && session != null && sessionToken != null) {
            Thread { sessionApi.disconnect(session.sessionId, sessionToken) }.start()
        }
    }

    fun logout() {
        disconnect()
        loginToken = null
        role = null
        api = null
        baseUrl = null
    }

    private val tick = object : Runnable {
        override fun run() {
            channel?.tick()
            main.postDelayed(this, 250)
        }
    }

    private fun start(url: HttpUrl, sessionApi: SessionApi, session: CreateSessionResponse) {
        stopConnection()
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

    private fun stopConnection() {
        main.removeCallbacks(tick)
        channel?.stop()
        channel = null
        uploader = null
        tokens = null
        created = null
        mission = null
        missionConfirmed = false
        states = emptyMap()
        lastResult = null
        capacity = false
        lastServerMessageMs = null
        lastResponseMs = null
    }

    /** 업로드는 제어 연결이 인증되고 미션이 확정된 뒤에만 (ADR-0009). */
    private fun refreshGate() {
        uploader?.enabled = channel?.state == LinkState.ACTIVE && missionConfirmed
    }

    private object Forwarder : ControlListener {
        override fun onState(state: LinkState) {
            if (state == LinkState.CAPACITY) capacity = true
            refreshGate()
            main.post { screen?.onState(state) }
        }

        override fun onMessage(message: ServerMessage) {
            lastServerMessageMs = SystemClock.elapsedRealtime()
            when (message) {
                is AlertMessage -> {
                    // received ACK right after parsing (SPEC 7.4); played comes when the HUD shows it
                    channel?.let { it.lastAlertSeq = maxOf(it.lastAlertSeq, message.alertSeq) }
                    channel?.send(AckMessage(message.alert.alertId, AckStatus.RECEIVED))
                }
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
            capacity = true
            main.post { screen?.onCapacity(retryAfterS) }
        }
    }
}
