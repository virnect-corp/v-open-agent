package ai.voa.client

import ai.voa.client.protocol.CreateSessionRequest
import ai.voa.client.protocol.CreateSessionResponse
import ai.voa.client.protocol.FlashMode
import ai.voa.client.protocol.HudCapabilities
import ai.voa.client.protocol.InputCap
import ai.voa.client.protocol.MissionMessage
import ai.voa.client.protocol.OutputCap
import ai.voa.client.protocol.ServerMessage
import ai.voa.client.protocol.SessionCapabilities
import ai.voa.client.protocol.SignupRequest
import ai.voa.client.session.ApiResult
import ai.voa.client.session.Cancellable
import ai.voa.client.session.ControlChannel
import ai.voa.client.session.ControlListener
import ai.voa.client.session.LinkState
import ai.voa.client.session.SessionApi
import ai.voa.client.session.SessionTokens
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit

/**
 * 로그인(가입) -> 연결 화면 (AND-02). 미션 선택과 감지 화면은 AND-03, AND-04에서 붙는다.
 * 비밀번호와 token은 메모리에만 두고 로그, 파일, 화면 상태 저장에 넣지 않는다.
 */
class MainActivity : ComponentActivity() {
    private val main = Handler(Looper.getMainLooper())
    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .pingInterval(0, TimeUnit.SECONDS)
        .build()
    private var channel: ControlChannel? = null
    private var session: CreateSessionResponse? = null
    private var linkText: TextView? = null
    private var lastServer = "http://127.0.0.1:8765" // kept between screens; the password is not
    private var lastUser = ""
    private var missionText: TextView? = null
    private val tick = object : Runnable {
        override fun run() {
            channel?.tick()
            main.postDelayed(this, TICK_MS)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        showLogin(error = null)
    }

    override fun onDestroy() {
        stopSession()
        super.onDestroy()
    }

    // --- screens ---

    private fun showLogin(error: String?, notice: String? = null) {
        val server = field("서버 주소", lastServer, InputType.TYPE_TEXT_VARIATION_URI)
        val user = field("아이디", lastUser, InputType.TYPE_CLASS_TEXT)
        val pass = field("비밀번호", "", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD)
        val label = field("장치 이름 (선택, 표시용)", "", InputType.TYPE_CLASS_TEXT)
        val status = TextView(this).apply {
            text = error ?: notice ?: ""
            setTextColor(if (error != null) 0xFFC62828.toInt() else 0xFF2E7D32.toInt())
        }
        val login = Button(this).apply { text = "로그인" }
        val signup = Button(this).apply { text = "가입 신청" }
        val remember = {
            lastServer = server.second.text.toString().trim()
            lastUser = user.second.text.toString().trim()
        }
        login.setOnClickListener {
            remember()
            val url = server.second.text.toString().trim().toHttpUrlOrNull()
                ?: return@setOnClickListener showLogin(Messages.of("bad_url"))
            setBusy(listOf(login, signup), true)
            login(url, user.second.text.toString().trim(), pass.second.text.toString(), label.second.text.toString().trim())
        }
        signup.setOnClickListener {
            remember()
            val url = server.second.text.toString().trim().toHttpUrlOrNull()
                ?: return@setOnClickListener showLogin(Messages.of("bad_url"))
            setBusy(listOf(login, signup), true)
            signup(url, user.second.text.toString().trim(), pass.second.text.toString())
        }
        setScreen(
            title("VOA 글래스"),
            muted("착용자 또는 관리자 계정으로 로그인합니다. 앱을 닫으면 로그아웃됩니다."),
            server.first, user.first, pass.first, label.first,
            status, login, signup,
        )
    }

    private fun showConnected(created: CreateSessionResponse) {
        val link = TextView(this).apply { textSize = 18f }
        val mission = TextView(this)
        linkText = link
        missionText = mission
        renderMission(created.mission?.let { "${it.name} v${it.version}" })
        val logout = Button(this).apply { text = "로그아웃" }
        logout.setOnClickListener {
            stopSession()
            showLogin(error = null, notice = "로그아웃했습니다.")
        }
        setScreen(
            title("연결"),
            link,
            mission,
            muted("프레임 간격 ${created.frameIntervalMs} ms. 미션 선택과 감지 화면은 다음 단계(AND-03)에서 붙습니다."),
            logout,
        )
        renderLink(channel?.state ?: LinkState.CONNECTING)
    }

    // --- actions ---

    private fun login(url: HttpUrl, username: String, password: String, deviceLabel: String) {
        val api = SessionApi(url, client)
        val request = CreateSessionRequest(
            username = username,
            password = password,
            deviceLabel = deviceLabel.ifEmpty { null },
            capabilities = SessionCapabilities(
                inputs = listOf(InputCap.CAMERA_0),
                outputs = listOf(OutputCap.HUD, OutputCap.SPEAKER, OutputCap.HAPTIC),
                hud = HudCapabilities(
                    color = true,
                    flashModes = listOf(FlashMode.NONE, FlashMode.BORDER, FlashMode.FULL_SCREEN),
                    maxLines = 2,
                ),
            ),
        )
        lifecycleScope.launch {
            when (val result = withContext(Dispatchers.IO) { api.createSession(request) }) {
                is ApiResult.Ok -> startSession(url, api, result.value)
                is ApiResult.Failed -> showLogin(Messages.of(result.code))
            }
        }
    }

    private fun signup(url: HttpUrl, username: String, password: String) {
        val api = SessionApi(url, client)
        lifecycleScope.launch {
            when (val result = withContext(Dispatchers.IO) { api.signup(SignupRequest(username, password)) }) {
                is ApiResult.Ok -> showLogin(
                    error = null,
                    notice = if (result.value.status == "pending") {
                        "가입 신청이 접수되었습니다. 관리자 승인 후 로그인할 수 있습니다."
                    } else {
                        "가입했습니다. 로그인하세요."
                    },
                )
                is ApiResult.Failed -> showLogin(Messages.of(result.code))
            }
        }
    }

    private fun startSession(url: HttpUrl, api: SessionApi, created: CreateSessionResponse) {
        session = created
        val listener = object : ControlListener {
            override fun onState(state: LinkState) = runOnUiThread { renderLink(state) }

            override fun onMessage(message: ServerMessage) = runOnUiThread {
                if (message is MissionMessage) renderMission("${message.name} v${message.version}")
            }

            override fun onDisconnectAnnounce() = runOnUiThread {
                linkText?.text = BUILT_IN_DISCONNECTED // TTS and HUD output come with AND-04
            }

            override fun onCapacity(retryAfterS: Int?) = runOnUiThread {
                linkText?.text = Messages.of("capacity_exceeded")
            }
        }
        channel = ControlChannel(
            client = client,
            baseUrl = url,
            sessionId = created.sessionId,
            tokens = SessionTokens(created.token, created.refreshToken),
            api = api,
            clock = { SystemClock.elapsedRealtime() },
            scheduler = { delayMs, task ->
                val runnable = Runnable { Thread { task() }.start() } // refresh is blocking I/O
                main.postDelayed(runnable, delayMs)
                Cancellable { main.removeCallbacks(runnable) }
            },
            watchdogMs = created.watchdogMs.toLong(),
            listener = listener,
        )
        showConnected(created)
        channel?.start()
        main.postDelayed(tick, TICK_MS)
    }

    private fun stopSession() {
        main.removeCallbacks(tick)
        channel?.stop()
        channel = null
        session = null
    }

    // --- rendering helpers ---

    private fun renderLink(state: LinkState) {
        linkText?.text = when (state) {
            LinkState.CONNECTING -> "연결 중"
            LinkState.ACTIVE -> "연결됨 (인증됨)"
            LinkState.RECONNECTING -> "연결 끊김, 다시 연결 중"
            LinkState.CAPACITY -> Messages.of("capacity_exceeded")
            LinkState.UNAUTHORIZED -> "로그인이 끝났습니다. 다시 로그인하세요."
            LinkState.STOPPED -> "멈춤"
        }
        if (state == LinkState.UNAUTHORIZED) {
            stopSession()
            showLogin(Messages.of("unauthorized"))
        }
    }

    private fun renderMission(name: String?) {
        missionText?.text = "미션: ${name ?: "아직 고르지 않음"}"
    }

    private fun setBusy(buttons: List<Button>, busy: Boolean) = buttons.forEach { it.isEnabled = !busy }

    private fun field(label: String, value: String, inputType: Int): Pair<View, EditText> {
        val edit = EditText(this).apply {
            setText(value)
            this.inputType = inputType
            setSingleLine()
        }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(TextView(context).apply { text = label })
            addView(edit)
        }
        return box to edit
    }

    private fun title(text: String) = TextView(this).apply {
        this.text = text
        textSize = 24f
        setTypeface(typeface, Typeface.BOLD)
    }

    private fun muted(text: String) = TextView(this).apply {
        this.text = text
        setTextColor(0xFF5D6673.toInt())
    }

    private fun setScreen(vararg views: View) {
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.TOP
            val pad = (24 * resources.displayMetrics.density).toInt()
            setPadding(pad, pad, pad, pad)
            views.forEach { addView(it) }
        }
        setContentView(ScrollView(this).apply { addView(column) })
    }

    companion object {
        private const val TICK_MS = 250L
        const val BUILT_IN_DISCONNECTED = "서버 연결이 끊겨 경고를 받을 수 없습니다."
    }
}
