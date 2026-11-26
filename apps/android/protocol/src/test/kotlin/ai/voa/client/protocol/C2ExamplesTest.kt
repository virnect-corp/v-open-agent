package ai.voa.client.protocol

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.nio.file.Files
import java.nio.file.Path
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

// contracts/fixtures/c2_examples.json: the server test validates the same file (AND-02).
class C2ExamplesTest {
    private val examples: JsonObject = run {
        val path = listOf(
            Path.of("..", "..", "contracts", "fixtures", "c2_examples.json"),
            Path.of("..", "..", "..", "contracts", "fixtures", "c2_examples.json"),
        ).firstOrNull { Files.exists(it) } ?: error("contracts/fixtures/c2_examples.json not found")
        ProtocolContract.json.parseToJsonElement(Files.readString(path)).jsonObject
    }

    private inline fun <reified T> decode(key: String): T =
        ProtocolContract.json.decodeFromString(examples.getValue(key).toString())

    @Test
    fun `session responses with and without a mission decode`() {
        val created = decode<CreateSessionResponse>("session_created")
        assertEquals(333, created.frameIntervalMs)
        assertEquals("fire_c1", created.mission?.missionId)
        assertNull(decode<CreateSessionResponse>("session_created_no_mission").mission)
    }

    @Test
    fun `rest bodies decode`() {
        val capacity = decode<ApiErrorResponse>("capacity_error_body")
        assertEquals(30, capacity.retryAfterS)
        assertEquals("pending", decode<SignupResponse>("signup_response").status)
        assertEquals(listOf("fire"), decode<MissionsResponse>("missions").missions[0].scenarios[0].labels)
        assertEquals(398.2, decode<FrameResult>("frame_result_body").timing.providerMs)
        assertEquals("accepted", decode<FrameAccepted>("frame_accepted_body").status)
    }

    @Test
    fun `every server message example decodes to its type`() {
        val messages = (examples.getValue("server_messages") as JsonArray).map { element ->
            element.jsonObject.getValue("type").jsonPrimitive.content to
                ProtocolContract.decodeServerMessage(element.toString())
        }
        val byType = messages.toMap()
        assertIs<MissionMessage>(byType["mission"])
        assertEquals(500, assertIs<ConfigMessage>(byType["config"]).frameIntervalMs)
        assertEquals(1, assertIs<HeartbeatMessage>(byType["heartbeat"]).frames?.skipped)
        val alert = assertIs<AlertMessage>(byType["alert"])
        assertEquals(FlashMode.FULL_SCREEN, assertNotNull(alert.presentation).flash.mode)
        assertEquals("degraded_default", assertIs<NoticeMessage>(byType["notice"]).presentation?.styleId)
        assertEquals(RiskState.WARNING, assertIs<FrameResultMessage>(byType["frame_result"]).states["fire_in_view"])
        assertTrue(messages.any { (_, m) -> m is ErrorMessage && m.retryAfterS == 30 })
    }

    @Test
    fun `login response decodes and hides its token`() {
        val login = decode<LoginResponse>("login_response")
        assertEquals("wearer", login.role)
        assertTrue(!login.toString().contains("tok_example_login"))
    }

    @Test
    fun `the removed auth message is no longer a client message`() {
        assertFailsWith<ProtocolParseException> {
            ProtocolContract.decodeClientMessage("""{"type":"auth","token":"t"}""")
        }
    }

    @Test
    fun `a session request names at most one credential`() {
        val caps = SessionCapabilities(listOf(InputCap.CAMERA_0), listOf(OutputCap.HUD))
        val byLogin = CreateSessionRequest(missionId = "fire_c1", capabilities = caps) // bearer login (TASK-47)
        val body0 = ProtocolContract.json.encodeToString(CreateSessionRequest.serializer(), byLogin)
        assertTrue(!body0.contains("username") && !body0.contains("device_id") && body0.contains("\"mission_id\":\"fire_c1\""))
        assertFailsWith<ProtocolParseException> {
            CreateSessionRequest(deviceId = "dev_01", username = "kim", capabilities = caps)
        }
        val body = ProtocolContract.json.encodeToString(
            CreateSessionRequest.serializer(),
            CreateSessionRequest(
                username = "kim",
                password = "pw",
                capabilities = caps.copy(hud = HudCapabilities(color = true, flashModes = listOf(FlashMode.NONE, FlashMode.FULL_SCREEN))),
            ),
        )
        assertTrue(body.contains("\"username\":\"kim\"") && !body.contains("device_id"))
        assertTrue(body.contains("\"flash_modes\":[\"none\",\"full_screen\"]"))
    }
}
