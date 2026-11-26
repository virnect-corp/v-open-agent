package ai.voa.client.session

import ai.voa.client.protocol.AuthOkMessage
import ai.voa.client.protocol.CreateSessionRequest
import ai.voa.client.protocol.InputCap
import ai.voa.client.protocol.OutputCap
import ai.voa.client.protocol.ServerMessage
import ai.voa.client.protocol.SessionCapabilities
import ai.voa.client.testing.FakeMonotonicClock
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.TimeUnit
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertTrue

private const val AUTH_OK = """{"type":"auth_ok","session_id":"sess_01","server_time_utc":"2026-11-26T07:00:00Z"}"""
private const val REFRESHED =
    """{"token":"tok_new","token_expires_at_utc":"2026-11-26T07:15:00Z",""" +
        """"refresh_token":"ref_new","refresh_token_expires_at_utc":"2026-11-27T07:00:00Z"}"""

/** Server side of one upgraded socket: records frames, can send or close. */
class ServerSocket : WebSocketListener() {
    val received = CopyOnWriteArrayList<String>()

    @Volatile var socket: WebSocket? = null

    override fun onOpen(webSocket: WebSocket, response: Response) {
        socket = webSocket
    }

    override fun onMessage(webSocket: WebSocket, text: String) {
        received += text
    }

    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
        webSocket.close(code, null) // answer the client's close so the server can shut down
    }
}

class FakeScheduler : Scheduler {
    val tasks = CopyOnWriteArrayList<Pair<Long, () -> Unit>>()

    override fun schedule(delayMs: Long, task: () -> Unit): Cancellable {
        val entry = delayMs to task
        tasks += entry
        return Cancellable { tasks.remove(entry) }
    }

    fun runNext() {
        val (_, task) = tasks.removeAt(0)
        task()
    }
}

class RecordingListener : ControlListener {
    val states = CopyOnWriteArrayList<LinkState>()
    val messages = CopyOnWriteArrayList<ServerMessage>()
    @Volatile var announced = 0
    @Volatile var capacity: Int? = null

    override fun onState(state: LinkState) {
        states += state
    }

    override fun onMessage(message: ServerMessage) {
        messages += message
    }

    override fun onDisconnectAnnounce() {
        announced += 1
    }

    override fun onCapacity(retryAfterS: Int?) {
        capacity = retryAfterS
    }
}

fun eventually(timeoutMs: Long = 3_000, check: () -> Boolean) {
    val end = System.currentTimeMillis() + timeoutMs
    while (System.currentTimeMillis() < end) {
        if (check()) return
        Thread.sleep(10)
    }
    throw AssertionError("condition not met within $timeoutMs ms")
}

class ControlChannelTest {
    private lateinit var server: MockWebServer
    private val client = OkHttpClient.Builder().readTimeout(5, TimeUnit.SECONDS).build()
    private val clock = FakeMonotonicClock(initialMs = 10_000)
    private val scheduler = FakeScheduler()
    private val listener = RecordingListener()

    @BeforeTest
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @AfterTest
    fun tearDown() {
        server.shutdown()
    }

    private fun channel(tokens: SessionTokens = SessionTokens("tok_old", "ref_old")) = ControlChannel(
        client = client,
        baseUrl = server.url("/"),
        sessionId = "sess_01",
        tokens = tokens,
        api = SessionApi(server.url("/"), client),
        clock = { clock.currentMs },
        scheduler = scheduler,
        watchdogMs = 3_000,
        listener = listener,
    )

    private fun upgrade(): ServerSocket {
        val socket = ServerSocket()
        server.enqueue(MockResponse().withWebSocketUpgrade(socket))
        return socket
    }

    @Test
    fun `the token rides in the handshake header and auth_ok is answered with alert_sync`() {
        val socket = upgrade()
        val channel = channel()
        channel.lastAlertSeq = 4
        channel.start()
        val handshake = server.takeRequest(3, TimeUnit.SECONDS)!!
        assertEquals("Bearer tok_old", handshake.getHeader("Authorization"))
        assertEquals("/v1/streams/sess_01", handshake.path)
        eventually { socket.socket != null }
        socket.socket!!.send(AUTH_OK)
        eventually { channel.state == LinkState.ACTIVE }
        eventually { socket.received.isNotEmpty() }
        assertEquals("""{"type":"alert_sync","last_alert_seq":4}""", socket.received[0])
        assertIs<AuthOkMessage>(listener.messages[0])
        channel.stop()
    }

    @Test
    fun `nothing is sent before auth_ok`() {
        val socket = upgrade()
        val channel = channel()
        channel.start()
        eventually { socket.socket != null }
        assertFalse(channel.send(ai.voa.client.protocol.AlertSyncMessage(0)))
        channel.stop()
    }

    @Test
    fun `watchdog boundary, one announcement, then reconnect with a refreshed token`() {
        val first = upgrade()
        val channel = channel()
        channel.start()
        eventually { first.socket != null }
        first.socket!!.send(AUTH_OK)
        eventually { channel.state == LinkState.ACTIVE }

        clock.advanceBy(2_999)
        channel.tick()
        assertEquals(LinkState.ACTIVE, channel.state)
        clock.advanceBy(1)
        channel.tick()
        assertEquals(LinkState.RECONNECTING, channel.state)
        assertEquals(1, listener.announced)
        assertEquals(1_000L, scheduler.tasks.single().first)

        server.takeRequest() // first handshake
        server.enqueue(MockResponse().setBody(REFRESHED).setHeader("Content-Type", "application/json"))
        val second = upgrade()
        scheduler.runNext()
        assertEquals("/v1/sessions/sess_01/refresh", server.takeRequest(3, TimeUnit.SECONDS)!!.path)
        assertEquals("Bearer tok_new", server.takeRequest(3, TimeUnit.SECONDS)!!.getHeader("Authorization"))
        eventually { second.socket != null }
        second.socket!!.send(AUTH_OK)
        eventually { channel.state == LinkState.ACTIVE }
        assertEquals(1, listener.announced, "announced once per outage")
        channel.stop()
    }

    @Test
    fun `close 4503 is capacity and does not reconnect`() {
        val socket = upgrade()
        val channel = channel()
        channel.start()
        eventually { socket.socket != null }
        socket.socket!!.send(
            """{"type":"error","code":"capacity_exceeded","message":"full","active":3,"max":3,"retry_after_s":30}""",
        )
        eventually { listener.capacity == 30 }
        socket.socket!!.close(4503, null)
        eventually { channel.state == LinkState.CAPACITY }
        assertTrue(scheduler.tasks.isEmpty())
    }

    @Test
    fun `a refused refresh after 4401 ends in UNAUTHORIZED`() {
        val socket = upgrade()
        val channel = channel()
        channel.start()
        eventually { socket.socket != null }
        server.takeRequest()
        socket.socket!!.close(4401, null)
        eventually { channel.state == LinkState.RECONNECTING }
        server.enqueue(MockResponse().setResponseCode(401).setBody("""{"code":"unauthorized"}"""))
        scheduler.runNext()
        assertEquals(LinkState.UNAUTHORIZED, channel.state)
    }

    @Test
    fun `reconnect delays are 1, 2, 4, 8 then 8 seconds`() {
        val policy = ReconnectPolicy()
        assertEquals(listOf(1_000L, 2_000L, 4_000L, 8_000L, 8_000L), List(5) { policy.nextDelayMs() })
        policy.reset()
        assertEquals(1_000L, policy.nextDelayMs())
    }

    @Test
    fun `tokens never show in toString`() {
        assertFalse(SessionTokens("tok_secret_1", "ref_secret_2").toString().contains("secret"))
    }
}

class SessionApiTest {
    private lateinit var server: MockWebServer
    private val client = OkHttpClient()

    @BeforeTest
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @AfterTest
    fun tearDown() {
        server.shutdown()
    }

    private val caps = SessionCapabilities(listOf(InputCap.CAMERA_0), listOf(OutputCap.HUD))

    @Test
    fun `wearer login sends the account and parses the session`() {
        server.enqueue(
            MockResponse().setBody(
                """{"session_id":"sess_01","token":"t","token_expires_at_utc":"2026-11-26T07:15:00Z",""" +
                    """"refresh_token":"r","refresh_token_expires_at_utc":"2026-11-27T07:00:00Z",""" +
                    """"heartbeat_ms":1000,"watchdog_ms":3000,"frame_interval_ms":333,"mission":null}""",
            ),
        )
        val result = SessionApi(server.url("/"), client).createSession(
            CreateSessionRequest(username = "kim", password = "pw-1", capabilities = caps),
        )
        val body = server.takeRequest().body.readUtf8()
        assertTrue(body.contains("\"username\":\"kim\"") && !body.contains("device_id"))
        assertEquals("sess_01", assertIs<ApiResult.Ok<*>>(result).value.let { (it as ai.voa.client.protocol.CreateSessionResponse).sessionId })
    }

    @Test
    fun `server error codes and the capacity retry time come back, network failures say network`() {
        val api = SessionApi(server.url("/"), client)
        val request = CreateSessionRequest(username = "kim", password = "pw", capabilities = caps)
        server.enqueue(MockResponse().setResponseCode(403).setBody("""{"code":"account_pending"}"""))
        assertEquals(ApiResult.Failed(403, "account_pending"), api.createSession(request))
        server.enqueue(
            MockResponse().setResponseCode(503).setHeader("Retry-After", "30")
                .setBody("""{"code":"capacity_exceeded","active":3,"max":3,"retry_after_s":30}"""),
        )
        assertEquals(ApiResult.Failed(503, "capacity_exceeded", 30), api.createSession(request))
        server.enqueue(MockResponse().setResponseCode(429).setBody("""{"code":"too_many_attempts"}"""))
        assertEquals("too_many_attempts", (api.createSession(request) as ApiResult.Failed).code)
        server.shutdown()
        assertEquals(ApiResult.Failed(0, "network"), api.createSession(request))
    }

    @Test
    fun `login holds no session, connecting sends the login token and the mission, disconnect deletes`() {
        val api = SessionApi(server.url("/"), client)
        server.enqueue(
            MockResponse().setBody(
                """{"token":"login_tok","token_expires_at_utc":"2026-11-26T07:15:00Z","role":"wearer","password_change_required":false}""",
            ),
        )
        val login = api.login(ai.voa.client.protocol.LoginRequest("kim", "pw-1"))
        assertEquals("login_tok", (login as ApiResult.Ok).value.token)
        assertEquals("/v1/auth/login", server.takeRequest().path)

        server.enqueue(
            MockResponse().setBody(
                """{"session_id":"sess_01","token":"t","token_expires_at_utc":"2026-11-26T07:15:00Z",""" +
                    """"refresh_token":"r","refresh_token_expires_at_utc":"2026-11-27T07:00:00Z",""" +
                    """"heartbeat_ms":1000,"watchdog_ms":3000,"frame_interval_ms":333,""" +
                    """"mission":{"mission_id":"fire_c1","version":1,"name":"fire_c1","frame_interval_ms":333}}""",
            ),
        )
        api.createSession(CreateSessionRequest(missionId = "fire_c1", capabilities = caps), loginToken = "login_tok")
        val connect = server.takeRequest()
        assertEquals("Bearer login_tok", connect.getHeader("Authorization"))
        val body = connect.body.readUtf8()
        assertTrue(body.contains("\"mission_id\":\"fire_c1\"") && !body.contains("username") && !body.contains("password"))

        server.enqueue(MockResponse().setResponseCode(204))
        assertEquals(ApiResult.Ok(Unit), api.disconnect("sess_01", "session_tok"))
        val delete = server.takeRequest()
        assertEquals("DELETE", delete.method)
        assertEquals("/v1/sessions/sess_01", delete.path)
        assertEquals("Bearer session_tok", delete.getHeader("Authorization"))
    }
}
