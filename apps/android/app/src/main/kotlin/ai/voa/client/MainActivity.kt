package ai.voa.client

import ai.voa.client.protocol.LoginRequest
import ai.voa.client.protocol.MissionSummary
import ai.voa.client.protocol.SignupRequest
import ai.voa.client.session.ApiResult
import ai.voa.client.session.ControlListener
import ai.voa.client.session.LinkState
import ai.voa.client.session.SessionApi
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.speech.tts.TextToSpeech
import android.text.InputType
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/**
 * 로그인(가입) -> 미션 선택 (AND-02, AND-03). 미션을 고르면 가로 고정 감지 화면으로 간다.
 * 이 화면은 세로 고정이다(소유자 요구, manifest).
 * 비밀번호와 token은 메모리에만 두고 로그, 파일, 화면 상태 저장에 넣지 않는다.
 */
class MainActivity : ComponentActivity(), ControlListener {
    private var lastServer = "http://127.0.0.1:8765" // kept between screens; the password is not
    private var lastUser = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        VoaSession.speechAvailable = packageManager
            .queryIntentServices(Intent(TextToSpeech.Engine.INTENT_ACTION_TTS_SERVICE), 0)
            .isNotEmpty()
        if (VoaSession.loggedIn) showMissions() else showLogin(error = null)
    }

    override fun onResume() {
        super.onResume()
        VoaSession.screen = this
        if (VoaSession.connected) VoaSession.disconnect() // back from detection: give the slot back
        if (VoaSession.loggedIn) showMissions(error = VoaSession.notice.also { VoaSession.notice = null })
    }

    override fun onPause() {
        if (VoaSession.screen === this) VoaSession.screen = null
        super.onPause()
    }

    override fun onDestroy() {
        if (isFinishing) VoaSession.logout() // closing the app disconnects and logs out
        super.onDestroy()
    }

    // --- login ---

    private fun showLogin(error: String?, notice: String? = null) {
        val server = field("서버 주소", lastServer, InputType.TYPE_TEXT_VARIATION_URI)
        val user = field("아이디", lastUser, InputType.TYPE_CLASS_TEXT)
        val pass = field("비밀번호", "", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD)
        val status = TextView(this).apply {
            text = error ?: notice ?: ""
            setTextColor(if (error != null) 0xFFC62828.toInt() else 0xFF2E7D32.toInt())
        }
        val login = Button(this).apply { text = "로그인" }
        val signup = Button(this).apply { text = "가입 신청" }
        fun url(): HttpUrl? {
            lastServer = server.second.text.toString().trim()
            lastUser = user.second.text.toString().trim()
            return lastServer.toHttpUrlOrNull()
        }
        login.setOnClickListener {
            val target = url() ?: return@setOnClickListener showLogin(Messages.of("bad_url"))
            login.isEnabled = false
            signup.isEnabled = false
            login(target, lastUser, pass.second.text.toString(), deviceLabel())
        }
        signup.setOnClickListener {
            val target = url() ?: return@setOnClickListener showLogin(Messages.of("bad_url"))
            login.isEnabled = false
            signup.isEnabled = false
            signup(target, lastUser, pass.second.text.toString())
        }
        setContentView(
            column(
                title("VOA 글래스"),
                muted("착용자 또는 관리자 계정으로 로그인합니다. 앱을 닫으면 로그아웃됩니다."),
                server.first, user.first, pass.first,
                muted("장치: ${deviceLabel()}" + if (VoaSession.speechAvailable) "" else " (음성 엔진 없음: 경고는 화면과 진동으로만)"),
                status, login, signup,
            ),
        )
    }

    /** Login checks the account only and holds no slot (TASK-47). */
    private fun login(url: HttpUrl, username: String, password: String, deviceLabel: String) {
        val api = SessionApi(url, VoaSession.client)
        lifecycleScope.launch {
            when (val result = withContext(Dispatchers.IO) { api.login(LoginRequest(username, password)) }) {
                is ApiResult.Ok -> {
                    if (result.value.passwordChangeRequired) {
                        return@launch showLogin(Messages.of("password_change_required"))
                    }
                    VoaSession.signedIn(url, api, result.value, deviceLabel)
                    showMissions()
                }
                is ApiResult.Failed -> showLogin(Messages.of(result.code))
            }
        }
    }

    private fun signup(url: HttpUrl, username: String, password: String) {
        val api = SessionApi(url, VoaSession.client)
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

    /** 표시용 장치 이름: 사용자가 정한 기기 이름, 없으면 제조사와 모델 (입력받지 않음). */
    private fun deviceLabel(): String {
        val named = Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME)?.trim()
        val label = if (named.isNullOrEmpty()) "${Build.MANUFACTURER} ${Build.MODEL}" else named
        return label.take(64)
    }

    // --- missions (AND-03) ---

    private fun showMissions(error: String? = null) {
        val list = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val status = TextView(this).apply {
            text = error ?: "미션 목록을 불러오는 중"
            setTextColor(if (error != null) 0xFFC62828.toInt() else 0xFF5D6673.toInt())
        }
        val logout = Button(this).apply { text = "로그아웃" }
        logout.setOnClickListener {
            VoaSession.logout()
            showLogin(error = null, notice = "로그아웃했습니다.")
        }
        setContentView(
            column(
                title("미션"),
                muted("무엇을 감시할지 고르면 서버에 연결해 그 미션의 모델과 규칙으로 감지를 시작합니다."),
                status, list, logout,
            ),
        )
        val api = VoaSession.api ?: return showLogin(null)
        val token = VoaSession.missionsToken() ?: return showLogin(null)
        lifecycleScope.launch {
            when (val result = withContext(Dispatchers.IO) { api.missions(token) }) {
                is ApiResult.Ok -> {
                    status.text = if (result.value.missions.isEmpty()) "게시된 미션이 없습니다." else ""
                    result.value.missions.forEach { list.addView(missionCard(it)) }
                }
                is ApiResult.Failed ->
                    if (result.status == 401) {
                        VoaSession.logout()
                        showLogin(Messages.of("unauthorized"))
                    } else {
                        status.text = Messages.of(result.code)
                    }
            }
        }
    }

    private fun missionCard(mission: MissionSummary): LinearLayout {
        val start = Button(this).apply { text = "이 미션으로 시작" }
        start.setOnClickListener {
            start.isEnabled = false
            choose(mission.missionId)
        }
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            val pad = (8 * resources.displayMetrics.density).toInt()
            setPadding(0, pad * 2, 0, pad)
            addView(TextView(context).apply { text = mission.name; textSize = 18f })
            addView(muted("v${mission.version}, 감시: ${mission.scenarios.joinToString { it.id }}, 모델: ${mission.models.joinToString()}"))
            addView(start)
        }
    }

    /** Connect with the chosen mission, then open the landscape detection screen (TASK-47). */
    private fun choose(missionId: String) {
        lifecycleScope.launch {
            when (val result = withContext(Dispatchers.IO) { VoaSession.connect(missionId) }) {
                is ApiResult.Ok -> startActivity(Intent(this@MainActivity, DetectionActivity::class.java))
                is ApiResult.Failed ->
                    if (result.status == 401) {
                        VoaSession.logout()
                        showLogin(Messages.of("unauthorized"))
                    } else {
                        showMissions(Messages.of(result.code))
                    }
            }
        }
    }

    // The mission screen holds no connection; detection shows the link state.
}

fun linkLabel(state: LinkState): String = when (state) {
    LinkState.CONNECTING -> "연결 중"
    LinkState.ACTIVE -> "연결됨 (인증됨)"
    LinkState.RECONNECTING -> "연결 끊김, 다시 연결 중"
    LinkState.CAPACITY -> Messages.of("capacity_exceeded")
    LinkState.UNAUTHORIZED -> "로그인이 끝났습니다"
    LinkState.STOPPED -> "멈춤"
}
