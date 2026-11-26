package ai.voa.client.protocol

import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonClassDiscriminator

// REST 세션 및 ACK API 모델 (SPEC 7.1, 7.3)
// HUD가 그릴 수 있는 것 (SPEC 7.4). 서버는 기록만 하고 축소는 장치가 한다.
@Serializable
data class HudCapabilities(
    val color: Boolean = false,
    @SerialName("flash_modes")
    val flashModes: List<FlashMode> = listOf(FlashMode.NONE),
    @SerialName("max_lines")
    val maxLines: Int = 2,
) {
    init {
        if (maxLines !in 1..6 || flashModes.size > 3) {
            throw ProtocolParseException(ProtocolParseException.CODE_REJECTED_SCHEMA, "invalid hud capabilities")
        }
    }
}

@Serializable
data class SessionCapabilities(
    val inputs: List<InputCap>,
    val outputs: List<OutputCap>,
    val hud: HudCapabilities? = null,
)

/**
 * 세션 생성 (SPEC 7.1, ADR-0011). 착용자/관리자 계정(`username`, `password`) 또는
 * 무인 장치(`device_id`, `secret`) 중 하나. 비밀번호와 secret은 toString에 나오지 않는다.
 */
@Serializable
data class CreateSessionRequest(
    @SerialName("device_id")
    val deviceId: String? = null,
    val secret: String? = null,
    val username: String? = null,
    val password: String? = null,
    @SerialName("device_label")
    val deviceLabel: String? = null,
    @SerialName("mission_id")
    val missionId: String? = null,
    val capabilities: SessionCapabilities,
) {
    init {
        // 장치 credential, 계정, 또는 둘 다 없음(헤더의 로그인 token으로 연결, TASK-47)
        if (deviceId != null && username != null) {
            throw ProtocolParseException(
                ProtocolParseException.CODE_REJECTED_SCHEMA,
                "device_id and username cannot both be set",
            )
        }
        deviceId?.let { ProtocolValidation.requireValidId(it, "device_id") }
        username?.let { ProtocolValidation.requireNonBlank(it, "username") }
    }

    override fun toString(): String =
        "CreateSessionRequest(deviceId=$deviceId, secret=<redacted>, username=$username, " +
            "password=<redacted>, deviceLabel=$deviceLabel, missionId=$missionId, capabilities=$capabilities)"
}

@Serializable
data class MissionRef(
    @SerialName("mission_id")
    val missionId: String,
    val version: Int,
    val name: String,
    @SerialName("frame_interval_ms")
    val frameIntervalMs: Int,
)

/** 계정 로그인 (ADR-0011). 정원을 차지하지 않는다; 연결은 미션을 고를 때 (TASK-47). */
@Serializable
data class LoginRequest(
    val username: String,
    val password: String,
) {
    override fun toString(): String = "LoginRequest(username=$username, password=<redacted>)"
}

@Serializable
data class LoginResponse(
    val token: String,
    @SerialName("token_expires_at_utc")
    val tokenExpiresAtUtc: String,
    val role: String, // admin | wearer
    @SerialName("password_change_required")
    val passwordChangeRequired: Boolean,
) {
    override fun toString(): String = "LoginResponse(token=<redacted>, role=$role)"
}

@Serializable
data class SignupRequest(
    val username: String,
    val password: String,
    @SerialName("display_name")
    val displayName: String? = null,
) {
    override fun toString(): String = "SignupRequest(username=$username, password=<redacted>)"
}

@Serializable
data class SignupResponse(
    @SerialName("user_id")
    val userId: String,
    val status: String, // pending | active
)

@Serializable
data class CreateSessionResponse(
    @SerialName("session_id")
    val sessionId: String,
    val token: String,
    @SerialName("token_expires_at_utc")
    val tokenExpiresAtUtc: String,
    @SerialName("refresh_token")
    val refreshToken: String,
    @SerialName("refresh_token_expires_at_utc")
    val refreshTokenExpiresAtUtc: String,
    @SerialName("heartbeat_ms")
    val heartbeatMs: Int,
    @SerialName("watchdog_ms")
    val watchdogMs: Int,
    @SerialName("frame_interval_ms")
    val frameIntervalMs: Int,
    val mission: MissionRef? = null,
) {
    init {
        ProtocolValidation.requireValidId(sessionId, "session_id")
        ProtocolValidation.requireNonBlank(token, "token")
        ProtocolValidation.requireUtcTimestamp(tokenExpiresAtUtc, "token_expires_at_utc")
        ProtocolValidation.requireNonBlank(refreshToken, "refresh_token")
        ProtocolValidation.requireUtcTimestamp(refreshTokenExpiresAtUtc, "refresh_token_expires_at_utc")
        if (heartbeatMs <= 0 || watchdogMs < 2 * heartbeatMs) {
            throw ProtocolParseException(
                errorCode = ProtocolParseException.CODE_REJECTED_SCHEMA,
                reason = "invalid heartbeat_ms or watchdog_ms bounds",
            )
        }
    }

    override fun toString(): String =
        "CreateSessionResponse(sessionId=$sessionId, token=<redacted>, tokenExpiresAtUtc=$tokenExpiresAtUtc, refreshToken=<redacted>, refreshTokenExpiresAtUtc=$refreshTokenExpiresAtUtc, heartbeatMs=$heartbeatMs, watchdogMs=$watchdogMs, frameIntervalMs=$frameIntervalMs, mission=$mission)"
}

@Serializable
data class RefreshSessionRequest(
    @SerialName("refresh_token")
    val refreshToken: String,
) {
    init {
        ProtocolValidation.requireNonBlank(refreshToken, "refresh_token")
    }

    override fun toString(): String = "RefreshSessionRequest(refreshToken=<redacted>)"
}

@Serializable
data class RefreshSessionResponse(
    @SerialName("session_id")
    val sessionId: String? = null,
    val token: String,
    @SerialName("token_expires_at_utc")
    val tokenExpiresAtUtc: String,
    @SerialName("refresh_token")
    val refreshToken: String,
    @SerialName("refresh_token_expires_at_utc")
    val refreshTokenExpiresAtUtc: String,
) {
    init {
        if (sessionId != null) {
            ProtocolValidation.requireValidId(sessionId, "session_id")
        }
        ProtocolValidation.requireNonBlank(token, "token")
        ProtocolValidation.requireUtcTimestamp(tokenExpiresAtUtc, "token_expires_at_utc")
        ProtocolValidation.requireNonBlank(refreshToken, "refresh_token")
        ProtocolValidation.requireUtcTimestamp(refreshTokenExpiresAtUtc, "refresh_token_expires_at_utc")
    }

    override fun toString(): String =
        "RefreshSessionResponse(sessionId=$sessionId, token=<redacted>, tokenExpiresAtUtc=$tokenExpiresAtUtc, refreshToken=<redacted>, refreshTokenExpiresAtUtc=$refreshTokenExpiresAtUtc)"
}

@Serializable
data class AckAlertRequest(
    val status: AckStatus,
)

/** REST 오류 본문. 정원 초과(503)만 active, max, retry_after_s를 함께 준다 (ADR-0010). */
@Serializable
data class ApiErrorResponse(
    val code: String,
    val active: Int? = null,
    val max: Int? = null,
    @SerialName("retry_after_s")
    val retryAfterS: Int? = null,
) {
    init {
        ProtocolValidation.requireNonBlank(code, "code")
    }
}

// WebSocket C->S 메시지 (SPEC 7.2)
@OptIn(ExperimentalSerializationApi::class)
@Serializable
@JsonClassDiscriminator("type")
sealed interface ClientMessage

// 인증은 WebSocket 핸드셰이크 헤더로 한다 (ADR-0009). 첫 메시지 `auth`는 없어졌다.

@Serializable
@SerialName("alert_sync")
data class AlertSyncMessage(
    @SerialName("last_alert_seq")
    val lastAlertSeq: Int,
) : ClientMessage {
    init {
        if (lastAlertSeq < 0) {
            throw ProtocolParseException(
                errorCode = ProtocolParseException.CODE_REJECTED_SCHEMA,
                reason = "last_alert_seq must be >= 0",
            )
        }
    }
}

@Serializable
@SerialName("observation")
data class ObservationMessage(
    val observation: ObservationDraft,
) : ClientMessage

@Serializable
@SerialName("ack")
data class AckMessage(
    @SerialName("alert_id")
    val alertId: String,
    val status: AckStatus,
) : ClientMessage {
    init {
        ProtocolValidation.requireValidId(alertId, "alert_id")
    }
}

// WebSocket S->C 메시지 (SPEC 7.2)
@OptIn(ExperimentalSerializationApi::class)
@Serializable
@JsonClassDiscriminator("type")
sealed interface ServerMessage

@Serializable
@SerialName("auth_ok")
data class AuthOkMessage(
    @SerialName("session_id")
    val sessionId: String,
    @SerialName("server_time_utc")
    val serverTimeUtc: String,
) : ServerMessage {
    init {
        ProtocolValidation.requireValidId(sessionId, "session_id")
        ProtocolValidation.requireUtcTimestamp(serverTimeUtc, "server_time_utc")
    }
}

@Serializable
@SerialName("admission")
data class AdmissionMessage(
    val seq: Int,
    @SerialName("stream_id")
    val streamId: String,
    val status: AdmissionStatus,
) : ServerMessage {
    init {
        if (seq < 0) {
            throw ProtocolParseException(
                errorCode = ProtocolParseException.CODE_REJECTED_SCHEMA,
                reason = "seq must be >= 0",
            )
        }
        ProtocolValidation.requireValidId(streamId, "stream_id")
        if (status == AdmissionStatus.ACCEPTED) {
            throw ProtocolParseException(
                errorCode = ProtocolParseException.CODE_REJECTED_SCHEMA,
                reason = "admission message is sent only for non-accepted statuses",
            )
        }
    }
}

@Serializable
@SerialName("decision")
data class DecisionMessage(
    val decision: RiskDecision,
) : ServerMessage

@Serializable
@SerialName("alert")
data class AlertMessage(
    @SerialName("alert_seq")
    val alertSeq: Int,
    val alert: Alert,
    @SerialName("risk_event_id")
    val riskEventId: String,
    @SerialName("scenario_id")
    val scenarioId: String,
    val presentation: Presentation? = null,
) : ServerMessage {
    init {
        if (alertSeq < 1) {
            throw ProtocolParseException(
                errorCode = ProtocolParseException.CODE_REJECTED_SCHEMA,
                reason = "alert_seq must be >= 1",
            )
        }
        ProtocolValidation.requireValidId(riskEventId, "risk_event_id")
        ProtocolValidation.requireValidId(scenarioId, "scenario_id")
    }
}

@Serializable
@SerialName("notice")
data class NoticeMessage(
    @SerialName("notice_id")
    val noticeId: String,
    val kind: NoticeKind,
    @SerialName("scenario_id")
    val scenarioId: String,
    val text: String,
    val presentation: Presentation? = null,
) : ServerMessage {
    init {
        ProtocolValidation.requireValidId(noticeId, "notice_id")
        ProtocolValidation.requireValidId(scenarioId, "scenario_id")
        ProtocolValidation.requireNonBlank(text, "text")
    }

    override fun toString(): String =
        "NoticeMessage(noticeId=$noticeId, kind=$kind, scenarioId=$scenarioId)"
}

@Serializable
@SerialName("heartbeat")
data class HeartbeatMessage(
    @SerialName("server_time_utc")
    val serverTimeUtc: String,
    val states: Map<String, RiskState>,
    val frames: FrameCounters? = null,
) : ServerMessage {
    init {
        ProtocolValidation.requireUtcTimestamp(serverTimeUtc, "server_time_utc")
        states.keys.forEach { ProtocolValidation.requireValidId(it, "states.scenario_id") }
    }
}

@Serializable
@SerialName("error")
data class ErrorMessage(
    val code: String,
    val message: String,
    val active: Int? = null,
    val max: Int? = null,
    @SerialName("retry_after_s")
    val retryAfterS: Int? = null,
) : ServerMessage {
    init {
        ProtocolValidation.requireNonBlank(code, "code")
        ProtocolValidation.requireNonBlank(message, "message")
    }
}

@Serializable
data class FrameCounters(
    val received: Int,
    val processed: Int,
    val skipped: Int,
    val rejected: Int,
)

// --- Stage C2 (ADR-0009, ADR-0010) ---

@Serializable
@SerialName("mission")
data class MissionMessage(
    @SerialName("mission_id")
    val missionId: String,
    val version: Int,
    val name: String,
    @SerialName("frame_interval_ms")
    val frameIntervalMs: Int,
) : ServerMessage

/** 전송 간격 설정. 판단 규칙이 아니다. */
@Serializable
@SerialName("config")
data class ConfigMessage(
    @SerialName("frame_interval_ms")
    val frameIntervalMs: Int,
) : ServerMessage {
    init {
        if (frameIntervalMs !in 100..5000) {
            throw ProtocolParseException(ProtocolParseException.CODE_REJECTED_SCHEMA, "frame_interval_ms out of range")
        }
    }
}

@Serializable
data class FrameModelResult(
    @SerialName("model_id")
    val modelId: String,
    @SerialName("observation_id")
    val observationId: String,
    val detections: List<Detection>,
)

@Serializable
data class FrameTiming(
    @SerialName("capture_ts_monotonic_ns")
    val captureTsMonotonicNs: Long,
    @SerialName("received_at_utc")
    val receivedAtUtc: String,
    @SerialName("processing_started_at_utc")
    val processingStartedAtUtc: String,
    @SerialName("completed_at_utc")
    val completedAtUtc: String,
    @SerialName("queue_ms")
    val queueMs: Double,
    @SerialName("provider_ms")
    val providerMs: Double,
    @SerialName("engine_us")
    val engineUs: Long,
)

/** 업로드 응답(200) 본문이자 `frame_result` 메시지의 내용 (SPEC 7.6). */
@Serializable
data class FrameResult(
    @SerialName("frame_id")
    val frameId: String,
    val seq: Int,
    val status: String,
    val results: List<FrameModelResult>,
    val states: Map<String, RiskState>,
    val timing: FrameTiming,
)

@Serializable
@SerialName("frame_result")
data class FrameResultMessage(
    @SerialName("frame_id")
    val frameId: String,
    val seq: Int,
    val status: String,
    val results: List<FrameModelResult>,
    val states: Map<String, RiskState>,
    val timing: FrameTiming,
) : ServerMessage

/** 업로드 응답 202: 결과는 나중에 `frame_result`로 온다. */
@Serializable
data class FrameAccepted(
    @SerialName("frame_id")
    val frameId: String,
    val status: String,
)

@Serializable
data class MissionScenario(
    val id: String,
    val labels: List<String>,
    val severity: String,
)

@Serializable
data class MissionSummary(
    @SerialName("mission_id")
    val missionId: String,
    val name: String,
    val description: String,
    val version: Int,
    val scenarios: List<MissionScenario>,
    val models: List<String>,
    @SerialName("frame_interval_ms")
    val frameIntervalMs: Int,
)

@Serializable
data class MissionsResponse(
    val missions: List<MissionSummary>,
)

@Serializable
data class SelectMissionRequest(
    @SerialName("mission_id")
    val missionId: String,
)

@Serializable
enum class FlashMode {
    @SerialName("none")
    NONE,

    @SerialName("border")
    BORDER,

    @SerialName("full_screen")
    FULL_SCREEN,
}

@Serializable
data class Flash(
    val mode: FlashMode,
    val count: Int,
    @SerialName("on_ms")
    val onMs: Int,
    @SerialName("off_ms")
    val offMs: Int,
)

/** HUD 표시 방법 (SPEC 7.4). 모든 기종에 같은 값이 오고, 장치가 자기 능력에 맞춰 줄인다. */
@Serializable
data class Presentation(
    @SerialName("style_id")
    val styleId: String,
    val text: String,
    val position: String, // top_center | center | bottom_center
    @SerialName("text_size")
    val textSize: String, // normal | large
    val color: String, // red | yellow | white
    val flash: Flash,
    @SerialName("show_ms")
    val showMs: Int,
    val speech: Boolean,
    val haptic: String, // none | pattern_a | pattern_b
) {
    override fun toString(): String = "Presentation(styleId=$styleId, position=$position, color=$color, flash=$flash)"
}
