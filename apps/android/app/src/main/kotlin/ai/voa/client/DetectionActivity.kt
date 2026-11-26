package ai.voa.client

import ai.voa.client.output.AlertTracker
import ai.voa.client.output.BuiltinNotices
import ai.voa.client.output.HudRules
import ai.voa.client.output.StatusBar
import ai.voa.client.output.StatusInput
import ai.voa.client.protocol.AckMessage
import ai.voa.client.protocol.AckStatus
import ai.voa.client.protocol.AlertMessage
import ai.voa.client.protocol.Detection
import ai.voa.client.protocol.FrameResultMessage
import ai.voa.client.protocol.FlashMode
import ai.voa.client.protocol.HudCapabilities
import ai.voa.client.protocol.NoticeMessage
import ai.voa.client.protocol.Presentation
import ai.voa.client.protocol.ServerMessage
import ai.voa.client.session.ControlListener
import ai.voa.client.session.LinkState
import ai.voa.client.session.UploadOutcome
import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Matrix
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.os.VibrationEffect
import android.os.VibratorManager
import android.speech.tts.TextToSpeech
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream
import java.util.Locale
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * 감지 화면 (AND-03). 가로 고정(manifest sensorLandscape). 카메라 미리보기와 프레임 업로드:
 * 긴 변 640px JPEG, 진행 중 1개, `frame_interval_ms` 간격, 연결과 미션 확정 전에는 0건.
 * 프레임은 메모리에서만 만들고 보낸 뒤 버린다(파일, 캐시에 쓰지 않음).
 * HUD(AND-04): alert/notice presentation을 이 기기에 맞게 줄여 그리고(HudRules), 음성(TTS)과
 * 진동을 내고, 표시 시작 때 played ACK를 보낸다. 상태 표시줄은 SPEC 7.4 규칙표를 그대로 쓴다.
 * 탐지 박스는 그리지 않는다.
 *
 * 글래스 모드(AND-04A): 광학식 글래스는 검정이 투명이므로 기본은 카메라 미리보기 끔(검정 바탕에
 * HUD만). 미리보기를 켜면 화면을 꽉 채운다(레터박스 없음). 선택: 경고 때 판정한 프레임과 박스를
 * 가운데에 잠깐 보여 준다. 두 설정은 기기에 저장한다(민감 정보 아님).
 */
class DetectionActivity : ComponentActivity(), ControlListener {
    private lateinit var preview: PreviewView
    private lateinit var hud: HudLayer
    private lateinit var debug: TextView
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    private val alerts = AlertTracker()
    private var announcedCapacity = false
    private val prefs by lazy { getSharedPreferences("voa_display", MODE_PRIVATE) }
    private var previewOn = false
    private var showJudged = false
    private var provider: ProcessCameraProvider? = null
    private lateinit var previewButton: Button
    private lateinit var judgedButton: Button
    @Volatile private var encodeMs: Long? = null
    @Volatile private var frameKb: Int? = null
    @Volatile private var frameSize: String? = null

    /** The frame the server is judging (by frame_id) and the last judged one, memory only. */
    private val pendingFrames = java.util.concurrent.ConcurrentHashMap<String, Bitmap>()
    @Volatile private var lastJudged: Pair<Bitmap, List<Detection>>? = null
    private val ticker = Handler(Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() {
            render()
            ticker.postDelayed(this, 250)
        }
    }
    private val analysis: ExecutorService = Executors.newSingleThreadExecutor()
    private val network: ExecutorService = Executors.newSingleThreadExecutor()
    @Volatile private var lastRttMs: Long? = null
    @Volatile private var lastFailure: String? = null

    private val permission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) startCamera() else debug.text = "카메라 권한이 없어 감지할 수 없습니다."
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        hideSystemBars()
        if (!VoaSession.connected || !VoaSession.missionConfirmed) return finish()
        previewOn = prefs.getBoolean("preview", false)
        showJudged = prefs.getBoolean("judged", false)
        preview = PreviewView(this).apply { scaleType = PreviewView.ScaleType.FILL_CENTER }
        hud = HudLayer(this)
        tts = TextToSpeech(this) { code ->
            ttsReady = code == TextToSpeech.SUCCESS
            tts?.language = Locale.KOREAN
        }
        debug = TextView(this).apply {
            setTextColor(0xFFFFFFFF.toInt())
            setBackgroundColor(0x99000000.toInt())
            setPadding(16, 8, 16, 8)
            textSize = 12f
        }
        val back = Button(this).apply { text = "미션 변경" }
        back.setOnClickListener { finish() }
        previewButton = Button(this)
        previewButton.setOnClickListener {
            previewOn = !previewOn
            prefs.edit().putBoolean("preview", previewOn).apply()
            bindCamera()
        }
        judgedButton = Button(this)
        judgedButton.setOnClickListener {
            showJudged = !showJudged
            prefs.edit().putBoolean("judged", showJudged).apply()
            if (!showJudged) clearJudged()
            labelButtons()
        }
        labelButtons()
        val root = FrameLayout(this).apply {
            setBackgroundColor(0xFF000000.toInt())
            addView(preview, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
            addView(hud, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
            // bottom-right: the HUD popup and status bar use the top of the glasses display
            addView(debug, FrameLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.BOTTOM or Gravity.END))
            addView(
                android.widget.LinearLayout(context).apply {
                    orientation = android.widget.LinearLayout.HORIZONTAL
                    addView(back)
                    addView(previewButton)
                    addView(judgedButton)
                },
                FrameLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.BOTTOM or Gravity.START),
            )
        }
        setContentView(root)
        render()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            startCamera()
        } else {
            permission.launch(Manifest.permission.CAMERA)
        }
    }

    override fun onResume() {
        super.onResume()
        VoaSession.screen = this
        ticker.post(tick)
    }

    override fun onPause() {
        if (VoaSession.screen === this) VoaSession.screen = null
        ticker.removeCallbacks(tick)
        super.onPause()
    }

    override fun onDestroy() {
        analysis.shutdownNow()
        network.shutdownNow()
        tts?.shutdown()
        clearJudged()
        if (isFinishing) VoaSession.disconnect() // leaving detection gives the slot back (TASK-47)
        super.onDestroy()
    }

    private fun startCamera() {
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            provider = future.get()
            bindCamera()
        }, ContextCompat.getMainExecutor(this))
    }

    /** Analysis always; the preview only when it is on (off = black = see-through on glasses). */
    private fun bindCamera() {
        val cameras = provider ?: return
        labelButtons()
        preview.visibility = if (previewOn) View.VISIBLE else View.INVISIBLE
        val analyzer = ImageAnalysis.Builder()
            .setResolutionSelector(
                ResolutionSelector.Builder()
                    .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY)
                    .setResolutionStrategy(
                        ResolutionStrategy(
                            android.util.Size(640, 360),
                            ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER,
                        ),
                    ).build(),
            )
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
            .build()
        analyzer.setAnalyzer(analysis) { image -> onFrame(image) }
        cameras.unbindAll()
        if (previewOn) {
            val previewUse = Preview.Builder()
                .setResolutionSelector(
                    ResolutionSelector.Builder()
                        .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY).build(),
                ).build()
                .also { it.surfaceProvider = preview.surfaceProvider }
            cameras.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, previewUse, analyzer)
        } else {
            cameras.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, analyzer)
        }
    }

    private fun labelButtons() {
        if (!this::previewButton.isInitialized) return
        previewButton.text = if (previewOn) "미리보기 끄기" else "미리보기 켜기"
        judgedButton.text = if (showJudged) "검출 장면 끄기" else "검출 장면 켜기"
    }

    private fun clearJudged() {
        pendingFrames.values.forEach { it.recycle() }
        pendingFrames.clear()
        lastJudged?.first?.recycle()
        lastJudged = null
    }

    /** A result arrived for one of our frames: keep that frame as the judged scene. */
    private fun judged(frameId: String, detections: List<Detection>) {
        val frame = pendingFrames.remove(frameId) ?: return
        lastJudged?.first?.recycle()
        lastJudged = frame to detections
    }

    /** Called for each analysis frame; most are dropped until the uploader is due. */
    private fun onFrame(image: ImageProxy) {
        val uploader = VoaSession.uploader
        if (uploader == null || !uploader.due()) return image.close()
        val captureNs = SystemClock.elapsedRealtimeNanos()
        val began = SystemClock.elapsedRealtime()
        val (jpeg, frame) = try {
            toJpeg(image, keep = showJudged)
        } finally {
            image.close()
        }
        encodeMs = SystemClock.elapsedRealtime() - began
        frameKb = jpeg.size / 1024
        network.execute {
            val sentAt = SystemClock.elapsedRealtime()
            val outcome = uploader.upload(jpeg, captureNs)
            if (frame != null) {
                when (outcome) {
                    is UploadOutcome.Processed -> {
                        pendingFrames[outcome.result.frameId] = frame
                        judged(outcome.result.frameId, outcome.result.results.flatMap { it.detections })
                    }
                    is UploadOutcome.Accepted -> pendingFrames[outcome.frameId] = frame // frame_result later
                    else -> frame.recycle()
                }
            }
            if (outcome != UploadOutcome.NotDue && outcome !is UploadOutcome.Failed) {
                VoaSession.lastResponseMs = SystemClock.elapsedRealtime()
            }
            when (outcome) {
                is UploadOutcome.Processed -> {
                    lastRttMs = SystemClock.elapsedRealtime() - sentAt
                    VoaSession.recordResult(outcome.result)
                }
                is UploadOutcome.Failed -> lastFailure = "${outcome.status} ${outcome.code}"
                else -> Unit
            }
            runOnUiThread { render() }
        }
    }

    /** JPEG for the server; with `keep`, also the bitmap for the judged-scene view. */
    private fun toJpeg(image: ImageProxy, keep: Boolean): Pair<ByteArray, Bitmap?> {
        var bitmap = image.toBitmap()
        val rotation = image.imageInfo.rotationDegrees
        val scale = MAX_SIDE.toFloat() / max(bitmap.width, bitmap.height)
        if (rotation != 0 || scale < 1f) {
            val matrix = Matrix().apply {
                if (scale < 1f) postScale(scale, scale)
                if (rotation != 0) postRotate(rotation.toFloat())
            }
            val transformed = Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
            if (transformed !== bitmap) bitmap.recycle()
            bitmap = transformed
        }
        frameSize = "${bitmap.width}x${bitmap.height}"
        val out = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
        if (!keep) {
            bitmap.recycle()
            return out.toByteArray() to null
        }
        return out.toByteArray() to bitmap
    }

    // --- display ---

    private fun hideSystemBars() {
        window.decorView.post {
            window.insetsController?.let {
                it.hide(android.view.WindowInsets.Type.systemBars())
                it.systemBarsBehavior = android.view.WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        }
    }

    override fun onState(state: LinkState) {
        if (state == LinkState.UNAUTHORIZED) { // the server ended the session (e.g. idle)
            VoaSession.notice = "연결이 끝났습니다. 미션을 다시 고르면 새로 연결합니다."
            return finish()
        }
        if (state == LinkState.ACTIVE) alerts.clear()
        render()
    }

    override fun onMessage(message: ServerMessage) {
        when (message) {
            is AlertMessage -> onAlert(message)
            is NoticeMessage -> message.presentation?.let { present(it, interrupt = false) }
            is FrameResultMessage -> judged(message.frameId, message.results.flatMap { it.detections })
            else -> Unit
        }
        render()
    }

    /** Draw once; `played` when the HUD display starts. A repeat only re-sends `played` (SPEC 7.4). */
    private fun onAlert(message: AlertMessage) {
        val id = message.alert.alertId
        if (alerts.firstTime(id)) {
            message.presentation?.let { present(it, interrupt = true) }
            val scene = lastJudged
            if (showJudged && scene != null) {
                hud.showSnapshot(scene.first, scene.second, VoaSession.missionLabels, message.presentation?.showMs ?: 4000)
            }
        }
        VoaSession.channel?.send(AckMessage(id, AckStatus.PLAYED))
    }

    private fun present(presentation: Presentation, interrupt: Boolean) {
        val spec = HudRules.reduce(presentation, PHONE_HUD)
        hud.show(spec)
        if (spec.speech) speak(spec.text, interrupt)
        vibrate(spec.haptic)
    }

    /** A new alert interrupts what is playing; a notice waits (SPEC 7.4). */
    private fun speak(text: String, interrupt: Boolean) {
        if (!ttsReady) return
        tts?.speak(text, if (interrupt) TextToSpeech.QUEUE_FLUSH else TextToSpeech.QUEUE_ADD, null, "voa")
    }

    private fun vibrate(pattern: String) {
        val timings = when (pattern) {
            "pattern_a" -> longArrayOf(0, 200, 100, 200, 100, 200)
            "pattern_b" -> longArrayOf(0, 400)
            else -> return
        }
        val manager = getSystemService(VibratorManager::class.java) ?: return
        manager.defaultVibrator.vibrate(VibrationEffect.createWaveform(timings, -1))
    }

    override fun onDisconnectAnnounce() {
        speak(BuiltinNotices.WATCHDOG_DISCONNECTED_TEXT, interrupt = true) // the device's own text
        render()
    }

    override fun onCapacity(retryAfterS: Int?) {
        if (!announcedCapacity) speak(BuiltinNotices.CAPACITY_TEXT, interrupt = true)
        announcedCapacity = true
        render()
    }

    private fun render() {
        if (!this::hud.isInitialized) return
        val state = VoaSession.channel?.state ?: LinkState.STOPPED
        val u = VoaSession.uploader
        val row = StatusBar.compute(
            StatusInput(
                capacity = VoaSession.capacity,
                connected = state == LinkState.ACTIVE,
                lastServerMessageMs = VoaSession.lastServerMessageMs,
                watchdogMs = (VoaSession.created?.watchdogMs ?: 3000).toLong(),
                uploadFailures = u?.consecutiveFailures ?: 0,
                uploading = u?.enabled == true,
                lastResponseMs = VoaSession.lastResponseMs,
                frameIntervalMs = u?.intervalMs ?: 333,
                states = VoaSession.states,
                nowMs = SystemClock.elapsedRealtime(),
            ),
        )
        hud.setStatus(row)
        val timing = VoaSession.lastResult?.timing
        debug.text = buildString {
            append("미션 ${VoaSession.mission?.name ?: "-"}  간격 ${u?.intervalMs ?: "-"} ms\n")
            append("보냄 ${u?.sent ?: 0}  건너뜀 ${u?.skipped ?: 0}  실패 ${u?.failed ?: 0}")
            if (timing != null) {
                append("\n왕복 ${lastRttMs ?: "-"} ms  추론 ${timing.providerMs.roundToInt()} ms")
            }
            if (row.showStates && VoaSession.states.isNotEmpty()) {
                append("\n" + VoaSession.states.entries.joinToString { "${it.key}: ${it.value.name.lowercase()}" })
            }
            if (encodeMs != null) append("\n프레임 ${frameSize ?: "-"}  ${frameKb ?: "-"} KB  변환 ${encodeMs} ms")
            lastFailure?.let { append("\n마지막 실패: $it") }
            if (!ttsReady) append("\n음성 없음 (OQ-14)")
        }
    }

    companion object {
        /** What this phone's HUD can draw; also declared at connect (VoaSession). */
        val PHONE_HUD = HudCapabilities(
            color = true,
            flashModes = listOf(FlashMode.NONE, FlashMode.BORDER, FlashMode.FULL_SCREEN),
            maxLines = 2,
        )
        private const val MAX_SIDE = 640
        private const val JPEG_QUALITY = 80
    }
}
