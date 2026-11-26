package ai.voa.client.output

import ai.voa.client.protocol.Flash
import ai.voa.client.protocol.FlashMode
import ai.voa.client.protocol.HudCapabilities
import ai.voa.client.protocol.Presentation
import ai.voa.client.protocol.RiskState
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

// Same cases as apps/server/tests/ui/sim_core.test.mjs so glasses and sim behave alike.
class HudRulesTest {
    private val attention = Presentation(
        styleId = "attention_default",
        text = "불꽃으로 보이는 것이 감지되었습니다.",
        position = "top_center",
        textSize = "large",
        color = "red",
        flash = Flash(FlashMode.FULL_SCREEN, 3, 200, 200),
        showMs = 4000,
        speech = true,
        haptic = "pattern_a",
    )
    private val colorFull = HudCapabilities(true, listOf(FlashMode.NONE, FlashMode.BORDER, FlashMode.FULL_SCREEN), 2)
    private val monoBorder = HudCapabilities(false, listOf(FlashMode.NONE, FlashMode.BORDER), 2)
    private val monoPlain = HudCapabilities(false, listOf(FlashMode.NONE), 2)

    @Test
    fun `presentation is reduced per HUD model - full screen, then border, then none`() {
        val color = HudRules.reduce(attention, colorFull)
        assertEquals(FlashMode.FULL_SCREEN, color.flashMode)
        assertEquals("red", color.color)
        val border = HudRules.reduce(attention, monoBorder)
        assertEquals(FlashMode.BORDER, border.flashMode)
        assertNull(border.color)
        val plain = HudRules.reduce(attention, monoPlain)
        assertEquals(FlashMode.NONE to 0, plain.flashMode to plain.flashCount)
        val undeclared = HudRules.reduce(attention)
        assertEquals(Triple(FlashMode.NONE, null, 2), Triple(undeclared.flashMode, undeclared.color, undeclared.maxLines))
        val borderOnly = HudRules.reduce(attention.copy(flash = attention.flash.copy(mode = FlashMode.BORDER)), colorFull)
        assertEquals(FlashMode.BORDER, borderOnly.flashMode)
        assertEquals(attention.text, plain.text)
        assertEquals(4000, plain.showMs)
        assertTrue(color.large && color.speech)
    }

    private val base = StatusInput(
        capacity = false,
        connected = true,
        lastServerMessageMs = 10_000,
        watchdogMs = 3_000,
        uploadFailures = 0,
        uploading = true,
        lastResponseMs = 10_000,
        frameIntervalMs = 333,
        states = mapOf("fire_in_view" to RiskState.CLEAR),
        nowMs = 10_100,
    )

    private fun row(change: StatusInput.() -> StatusInput) = StatusBar.compute(base.change())

    @Test
    fun `status bar rule table - the upper row wins`() {
        val w = RiskState.WARNING
        val cases = listOf(
            row { copy(capacity = true, connected = false, uploadFailures = 9) } to StatusRow(StatusKey.CAPACITY, false),
            row { copy(connected = false, uploadFailures = 9) } to StatusRow(StatusKey.DISCONNECTED, false),
            row { copy(nowMs = 13_000) } to StatusRow(StatusKey.DISCONNECTED, false),
            row { copy(uploadFailures = 3, states = mapOf("a" to w)) } to StatusRow(StatusKey.SERVER_ERROR, false),
            row { copy(uploadFailures = 2) } to StatusRow(StatusKey.NORMAL, true),
            row { copy(nowMs = 11_666, lastServerMessageMs = 11_600, states = mapOf("a" to w)) } to StatusRow(StatusKey.SLOW, true),
            row { copy(uploading = false, nowMs = 11_666, lastServerMessageMs = 11_600) } to StatusRow(StatusKey.NORMAL, true),
            row { copy(states = mapOf("a" to w, "b" to RiskState.UNKNOWN)) } to StatusRow(StatusKey.WARNING, true),
            row { copy(states = mapOf("a" to RiskState.UNKNOWN, "b" to RiskState.CANDIDATE)) } to StatusRow(StatusKey.UNKNOWN, true),
            row { copy(states = emptyMap()) } to StatusRow(StatusKey.UNKNOWN, true),
            row { copy(states = mapOf("a" to RiskState.CANDIDATE, "b" to RiskState.CLEAR)) } to StatusRow(StatusKey.CANDIDATE, true),
            row { copy(states = mapOf("a" to RiskState.CLEAR, "b" to RiskState.RESOLVED)) } to StatusRow(StatusKey.NORMAL, true),
        )
        cases.forEachIndexed { index, (actual, expected) -> assertEquals(expected, actual, "row $index") }
    }

    @Test
    fun `an alert is drawn once, repeats only re-ACK`() {
        val tracker = AlertTracker()
        assertTrue(tracker.firstTime("alert_1"))
        assertFalse(tracker.firstTime("alert_1"))
        assertTrue(tracker.firstTime("alert_2"))
        tracker.clear()
        assertTrue(tracker.firstTime("alert_1"))
    }
}
