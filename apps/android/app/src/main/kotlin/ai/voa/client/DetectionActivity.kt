package ai.voa.client

import ai.voa.client.protocol.FrameResultMessage
import ai.voa.client.protocol.HeartbeatMessage
import ai.voa.client.protocol.RiskState
import ai.voa.client.protocol.ServerMessage
import ai.voa.client.session.ControlListener
import ai.voa.client.session.LinkState
import ai.voa.client.session.UploadOutcome
import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Matrix
import android.os.Bundle
import android.os.SystemClock
import android.view.Gravity
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
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * 감지 화면 (AND-03). 가로 고정(manifest sensorLandscape). 카메라 미리보기와 프레임 업로드:
 * 긴 변 640px JPEG, 진행 중 1개, `frame_interval_ms` 간격, 연결과 미션 확정 전에는 0건.
 * 프레임은 메모리에서만 만들고 보낸 뒤 버린다(파일, 캐시에 쓰지 않음).
 * HUD 경고 팝업, 상태 표시줄, 음성, ACK는 AND-04에서 붙는다.
 */
class DetectionActivity : ComponentActivity(), ControlListener {
    private lateinit var preview: PreviewView
    private lateinit var status: TextView
    private lateinit var debug: TextView
    private val analysis: ExecutorService = Executors.newSingleThreadExecutor()
    private val network: ExecutorService = Executors.newSingleThreadExecutor()
    @Volatile private var lastRttMs: Long? = null
    @Volatile private var lastFailure: String? = null

    private val permission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) startCamera() else status.text = "카메라 권한이 없어 감지할 수 없습니다."
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        hideSystemBars()
        if (!VoaSession.connected || !VoaSession.missionConfirmed) return finish()
        preview = PreviewView(this).apply { scaleType = PreviewView.ScaleType.FIT_CENTER }
        status = TextView(this).apply {
            setTextColor(0xFFFFFFFF.toInt())
            setBackgroundColor(0x99000000.toInt())
            setPadding(16, 8, 16, 8)
        }
        debug = TextView(this).apply {
            setTextColor(0xFFFFFFFF.toInt())
            setBackgroundColor(0x99000000.toInt())
            setPadding(16, 8, 16, 8)
            textSize = 12f
        }
        val back = Button(this).apply { text = "미션 변경" }
        back.setOnClickListener { finish() }
        val root = FrameLayout(this).apply {
            setBackgroundColor(0xFF000000.toInt())
            addView(preview, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
            addView(status, FrameLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.TOP or Gravity.END))
            addView(debug, FrameLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.TOP or Gravity.START))
            addView(back, FrameLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.BOTTOM or Gravity.START))
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
        render()
    }

    override fun onPause() {
        if (VoaSession.screen === this) VoaSession.screen = null
        super.onPause()
    }

    override fun onDestroy() {
        analysis.shutdownNow()
        network.shutdownNow()
        if (isFinishing) VoaSession.disconnect() // leaving detection gives the slot back (TASK-47)
        super.onDestroy()
    }

    private fun startCamera() {
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            val provider = future.get()
            val previewUse = Preview.Builder().build().also { it.surfaceProvider = preview.surfaceProvider }
            val analyzer = ImageAnalysis.Builder()
                .setResolutionSelector(
                    ResolutionSelector.Builder()
                        .setResolutionStrategy(
                            ResolutionStrategy(
                                android.util.Size(640, 480),
                                ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER,
                            ),
                        ).build(),
                )
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
                .build()
            analyzer.setAnalyzer(analysis) { image -> onFrame(image) }
            provider.unbindAll()
            provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, previewUse, analyzer)
        }, ContextCompat.getMainExecutor(this))
    }

    /** Called for each analysis frame; most are dropped until the uploader is due. */
    private fun onFrame(image: ImageProxy) {
        val uploader = VoaSession.uploader
        if (uploader == null || !uploader.due()) return image.close()
        val captureNs = SystemClock.elapsedRealtimeNanos()
        val jpeg = try {
            toJpeg(image)
        } finally {
            image.close()
        }
        network.execute {
            val sentAt = SystemClock.elapsedRealtime()
            when (val outcome = uploader.upload(jpeg, captureNs)) {
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

    private fun toJpeg(image: ImageProxy): ByteArray {
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
        val out = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
        bitmap.recycle()
        return out.toByteArray()
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
        render()
    }

    override fun onMessage(message: ServerMessage) {
        if (message is HeartbeatMessage || message is FrameResultMessage) render()
    }

    override fun onCapacity(retryAfterS: Int?) = render()

    private fun render() {
        if (!this::status.isInitialized) return
        val state = VoaSession.channel?.state ?: LinkState.STOPPED
        val states = VoaSession.states
        val summary = when {
            state != LinkState.ACTIVE -> linkLabel(state)
            states.values.contains(RiskState.WARNING) -> "경고"
            states.isEmpty() || states.values.contains(RiskState.UNKNOWN) -> "판단 불가"
            states.values.contains(RiskState.CANDIDATE) -> "확인 중"
            else -> "정상"
        }
        status.text = summary // the full status bar rules come with AND-04
        val u = VoaSession.uploader
        val timing = VoaSession.lastResult?.timing
        debug.text = buildString {
            append("미션 ${VoaSession.mission?.name ?: "-"}  간격 ${u?.intervalMs ?: "-"} ms\n")
            append("보냄 ${u?.sent ?: 0}  건너뜀 ${u?.skipped ?: 0}  실패 ${u?.failed ?: 0}")
            if (timing != null) {
                append("\n왕복 ${lastRttMs ?: "-"} ms  추론 ${timing.providerMs.roundToInt()} ms")
            }
            lastFailure?.let { append("\n마지막 실패: $it") }
        }
    }

    companion object {
        private const val MAX_SIDE = 640
        private const val JPEG_QUALITY = 80
    }
}
