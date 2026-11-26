package ai.voa.client.output

import ai.voa.client.protocol.FlashMode
import ai.voa.client.protocol.HudCapabilities
import ai.voa.client.protocol.Presentation
import ai.voa.client.protocol.RiskState

/** What the HUD actually draws after reducing a presentation to this device (SPEC 7.4). */
data class RenderSpec(
    val styleId: String,
    val text: String,
    val maxLines: Int,
    val position: String,
    val large: Boolean,
    val color: String?, // null on a mono HUD: inverse emphasis instead
    val flashMode: FlashMode,
    val flashCount: Int,
    val onMs: Int,
    val offMs: Int,
    val showMs: Int,
    val speech: Boolean,
    val haptic: String,
)

/** The same rules as the sim page (TASK-46 core.js reducePresentation). */
object HudRules {
    private val flashOrder = listOf(FlashMode.FULL_SCREEN, FlashMode.BORDER, FlashMode.NONE)

    val MINIMUM = HudCapabilities(color = false, flashModes = listOf(FlashMode.NONE), maxLines = 2)

    fun reduce(p: Presentation, hud: HudCapabilities = MINIMUM): RenderSpec {
        val supported = hud.flashModes.toSet()
        val mode = flashOrder.drop(flashOrder.indexOf(p.flash.mode))
            .first { it == FlashMode.NONE || it in supported }
        return RenderSpec(
            styleId = p.styleId,
            text = p.text,
            maxLines = hud.maxLines,
            position = p.position,
            large = p.textSize == "large",
            color = if (hud.color) p.color else null,
            flashMode = mode,
            flashCount = if (mode == FlashMode.NONE) 0 else p.flash.count,
            onMs = p.flash.onMs,
            offMs = p.flash.offMs,
            showMs = p.showMs,
            speech = p.speech,
            haptic = p.haptic,
        )
    }
}

enum class StatusKey(val label: String, val tone: String) {
    CAPACITY("정원 초과", "gray"),
    DISCONNECTED("연결 끊김", "gray"),
    SERVER_ERROR("서버 오류", "red-border"),
    SLOW("응답 지연", "yellow"),
    WARNING("경고", "red"),
    UNKNOWN("판단 불가", "gray"),
    CANDIDATE("확인 중", "yellow-dot"),
    NORMAL("정상", "green-dot"),
}

data class StatusInput(
    val capacity: Boolean,
    val connected: Boolean,
    val lastServerMessageMs: Long?,
    val watchdogMs: Long,
    val uploadFailures: Int,
    val uploading: Boolean,
    val lastResponseMs: Long?,
    val frameIntervalMs: Int,
    val states: Map<String, RiskState>,
    val nowMs: Long,
)

data class StatusRow(val key: StatusKey, val showStates: Boolean)

/** HUD status bar: the upper row wins (SPEC 7.4). Same table as the sim page. */
object StatusBar {
    const val SYNC_TIMEOUT_MS = 1_000L // VOA_FRAME_SYNC_TIMEOUT_MS default (not in the session response)
    const val FAILURES_FOR_ERROR = 3

    fun compute(i: StatusInput): StatusRow {
        if (i.capacity) return StatusRow(StatusKey.CAPACITY, false)
        val silent = i.lastServerMessageMs != null && i.nowMs - i.lastServerMessageMs >= i.watchdogMs
        if (!i.connected || silent) return StatusRow(StatusKey.DISCONNECTED, false)
        if (i.uploadFailures >= FAILURES_FOR_ERROR) return StatusRow(StatusKey.SERVER_ERROR, false)
        val waitLimit = 2L * i.frameIntervalMs + SYNC_TIMEOUT_MS
        if (i.uploading && i.lastResponseMs != null && i.nowMs - i.lastResponseMs >= waitLimit) {
            return StatusRow(StatusKey.SLOW, true)
        }
        val values = i.states.values
        return when {
            RiskState.WARNING in values -> StatusRow(StatusKey.WARNING, true)
            values.isEmpty() || RiskState.UNKNOWN in values -> StatusRow(StatusKey.UNKNOWN, true) // nothing received is not normal
            RiskState.CANDIDATE in values -> StatusRow(StatusKey.CANDIDATE, true)
            else -> StatusRow(StatusKey.NORMAL, true)
        }
    }
}

/**
 * ACK rules (SPEC 7.4): `received` right after parsing; `played` when HUD display starts;
 * an alert already shown is not drawn again, its `played` is just re-sent.
 */
class AlertTracker {
    private val shown = LinkedHashSet<String>()

    /** True if this alert should be drawn now (first time). */
    fun firstTime(alertId: String): Boolean {
        val added = shown.add(alertId)
        while (shown.size > 256) shown.remove(shown.first())
        return added
    }

    fun clear() = shown.clear()
}
