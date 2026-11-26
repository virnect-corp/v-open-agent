package ai.voa.client.session

import ai.voa.client.protocol.ApiErrorResponse
import ai.voa.client.protocol.FrameAccepted
import ai.voa.client.protocol.FrameResult
import ai.voa.client.protocol.ProtocolContract
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException

sealed interface UploadOutcome {
    data class Processed(val result: FrameResult) : UploadOutcome

    data class Accepted(val frameId: String) : UploadOutcome // result follows as frame_result

    data object Busy : UploadOutcome // 409 frame_busy: skipped, not an error (SPEC 7.4)

    data class Failed(val status: Int, val code: String) : UploadOutcome

    data object NotDue : UploadOutcome
}

/**
 * 프레임 업로드 `POST /v1/sessions/{id}/frames` (SPEC 7.6, ADR-0009).
 * - `enabled`(제어 연결 인증과 미션 확정) 전에는 0건
 * - 진행 중 1개, `intervalMs`보다 빠르게 보내지 않는다(`config`, `mission`이 바꾼다)
 * - 연속 실패 수는 상태 표시줄 "서버 오류"에 쓴다(409 frame_busy는 실패가 아님)
 * 프레임 바이트는 이 호출 동안만 메모리에 있고 저장하거나 기록하지 않는다.
 */
class FrameUploader(
    private val client: OkHttpClient,
    private val baseUrl: HttpUrl,
    private val sessionId: String,
    private val tokens: SessionTokens,
    private val clock: MonotonicClock,
    intervalMs: Int,
) {
    @Volatile var intervalMs: Int = intervalMs

    @Volatile var enabled: Boolean = false

    private val lock = Any()
    private var inFlight = false
    private var lastSentMs: Long? = null
    private var seq = 0

    @Volatile var sent = 0
        private set

    @Volatile var skipped = 0
        private set

    @Volatile var failed = 0
        private set

    @Volatile var consecutiveFailures = 0
        private set

    fun due(): Boolean = synchronized(lock) { dueLocked() }

    private fun dueLocked(): Boolean {
        val last = lastSentMs
        return enabled && !inFlight && (last == null || clock.nowMs() - last >= intervalMs)
    }

    /** Blocking: call from a worker thread with a JPEG whose long side is at most 640 px. */
    fun upload(jpeg: ByteArray, captureNs: Long, qualityFlags: List<String> = emptyList()): UploadOutcome {
        val frameNumber = synchronized(lock) {
            if (!dueLocked()) return UploadOutcome.NotDue
            inFlight = true
            lastSentMs = clock.nowMs()
            seq += 1
            seq
        }
        val frameId = "f_%06d".format(frameNumber)
        val builder = Request.Builder()
            .url(baseUrl.resolve("v1/sessions/$sessionId/frames") ?: error("bad session id"))
            .header("Authorization", "Bearer ${tokens.token}")
            .header("X-VOA-Stream-Id", "camera_0")
            .header("X-VOA-Seq", frameNumber.toString())
            .header("X-VOA-Frame-Id", frameId)
            .header("X-VOA-Capture-Ts-Ns", captureNs.toString())
        if (qualityFlags.isNotEmpty()) builder.header("X-VOA-Quality-Flags", qualityFlags.joinToString(","))
        val outcome = try {
            client.newCall(builder.post(jpeg.toRequestBody(JPEG)).build()).execute().use { response ->
                val text = response.body?.string().orEmpty()
                when {
                    response.code == 200 ->
                        UploadOutcome.Processed(ProtocolContract.json.decodeFromString(FrameResult.serializer(), text))
                    response.code == 202 ->
                        UploadOutcome.Accepted(
                            ProtocolContract.json.decodeFromString(FrameAccepted.serializer(), text).frameId,
                        )
                    else -> {
                        val code = runCatching {
                            ProtocolContract.json.decodeFromString(ApiErrorResponse.serializer(), text).code
                        }.getOrDefault("http_${response.code}")
                        if (response.code == 409 && code == "frame_busy") UploadOutcome.Busy
                        else UploadOutcome.Failed(response.code, code)
                    }
                }
            }
        } catch (_: IOException) {
            UploadOutcome.Failed(0, "network")
        } catch (_: IllegalArgumentException) {
            UploadOutcome.Failed(0, "invalid_response")
        }
        synchronized(lock) {
            inFlight = false
            when (outcome) {
                is UploadOutcome.Processed, is UploadOutcome.Accepted -> {
                    sent += 1
                    consecutiveFailures = 0
                }
                UploadOutcome.Busy -> skipped += 1
                is UploadOutcome.Failed -> {
                    failed += 1
                    consecutiveFailures += 1
                }
                UploadOutcome.NotDue -> Unit
            }
        }
        return outcome
    }

    companion object {
        private val JPEG = "image/jpeg".toMediaType()
    }
}
