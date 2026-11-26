package ai.voa.client.session

import ai.voa.client.testing.FakeMonotonicClock
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

private const val RESULT =
    """{"frame_id":"f_000001","seq":1,"status":"processed","results":[],"states":{"fire_in_view":"candidate"},""" +
        """"timing":{"capture_ts_monotonic_ns":5,"received_at_utc":"2026-11-26T07:00:00Z",""" +
        """"processing_started_at_utc":"2026-11-26T07:00:00Z","completed_at_utc":"2026-11-26T07:00:00Z",""" +
        """"queue_ms":0.1,"provider_ms":400.0,"engine_us":1800}}"""

class FrameUploaderTest {
    private lateinit var server: MockWebServer
    private val clock = FakeMonotonicClock(initialMs = 50_000)
    private val tokens = SessionTokens("tok_a", "ref_a")
    private val jpeg = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 1, 2, 3)

    @BeforeTest
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @AfterTest
    fun tearDown() {
        server.shutdown()
    }

    private fun uploader(interval: Int = 333) =
        FrameUploader(OkHttpClient(), server.url("/"), "sess_01", tokens, { clock.currentMs }, interval)

    @Test
    fun `nothing is uploaded before the link is up and a mission is chosen`() {
        val u = uploader()
        assertEquals(false, u.due())
        assertEquals(UploadOutcome.NotDue, u.upload(jpeg, 1))
        assertEquals(0, server.requestCount)
    }

    @Test
    fun `a frame carries the token, stream headers and the JPEG body`() {
        server.enqueue(MockResponse().setBody(RESULT))
        val u = uploader().apply { enabled = true }
        val outcome = u.upload(jpeg, captureNs = 123_456_789, qualityFlags = listOf("camera_motion_uncompensated"))
        assertIs<UploadOutcome.Processed>(outcome)
        val request = server.takeRequest()
        assertEquals("/v1/sessions/sess_01/frames", request.path)
        assertEquals("Bearer tok_a", request.getHeader("Authorization"))
        assertEquals("image/jpeg", request.getHeader("Content-Type"))
        assertEquals("camera_0", request.getHeader("X-VOA-Stream-Id"))
        assertEquals("1", request.getHeader("X-VOA-Seq"))
        assertEquals("f_000001", request.getHeader("X-VOA-Frame-Id"))
        assertEquals("123456789", request.getHeader("X-VOA-Capture-Ts-Ns"))
        assertEquals("camera_motion_uncompensated", request.getHeader("X-VOA-Quality-Flags"))
        assertTrue(request.body.readByteArray().contentEquals(jpeg))
    }

    @Test
    fun `never faster than the interval, which config can change, and a refreshed token is used`() {
        repeat(3) { server.enqueue(MockResponse().setBody(RESULT)) }
        val u = uploader(interval = 500).apply { enabled = true }
        assertIs<UploadOutcome.Processed>(u.upload(jpeg, 1))
        clock.advanceBy(499)
        assertEquals(UploadOutcome.NotDue, u.upload(jpeg, 2))
        clock.advanceBy(1)
        tokens.token = "tok_b" // ControlChannel refreshed it
        assertIs<UploadOutcome.Processed>(u.upload(jpeg, 3))
        u.intervalMs = 1_000
        clock.advanceBy(999)
        assertEquals(false, u.due())
        clock.advanceBy(1)
        assertIs<UploadOutcome.Processed>(u.upload(jpeg, 4))
        server.takeRequest()
        assertEquals("Bearer tok_b", server.takeRequest().getHeader("Authorization"))
        assertEquals("3", server.takeRequest().getHeader("X-VOA-Seq"))
    }

    @Test
    fun `only one frame is in flight`() {
        val release = CountDownLatch(1)
        server.enqueue(MockResponse().setBody(RESULT).setBodyDelay(300, TimeUnit.MILLISECONDS))
        val u = uploader(interval = 0).apply { enabled = true }
        val first = thread { u.upload(jpeg, 1); release.countDown() }
        Thread.sleep(100)
        assertEquals(false, u.due())
        assertEquals(UploadOutcome.NotDue, u.upload(jpeg, 2))
        release.await(3, TimeUnit.SECONDS)
        first.join()
        assertEquals(1, server.requestCount)
        assertEquals(true, u.due())
    }

    @Test
    fun `202 counts as sent, frame_busy is skipped not failed, errors count up in a row`() {
        val u = uploader(interval = 0).apply { enabled = true }
        server.enqueue(MockResponse().setResponseCode(202).setBody("""{"frame_id":"f_000001","status":"accepted"}"""))
        assertEquals(UploadOutcome.Accepted("f_000001"), u.upload(jpeg, 1))
        server.enqueue(MockResponse().setResponseCode(409).setBody("""{"code":"frame_busy"}"""))
        assertEquals(UploadOutcome.Busy, u.upload(jpeg, 2))
        repeat(3) { server.enqueue(MockResponse().setResponseCode(503).setBody("""{"code":"no_model_deployed"}""")) }
        repeat(3) { assertEquals(UploadOutcome.Failed(503, "no_model_deployed"), u.upload(jpeg, 3L + it)) }
        assertEquals(listOf(1, 1, 3, 3), listOf(u.sent, u.skipped, u.failed, u.consecutiveFailures))
        server.enqueue(MockResponse().setBody(RESULT))
        u.upload(jpeg, 9)
        assertEquals(0, u.consecutiveFailures)
        server.shutdown()
        assertEquals(UploadOutcome.Failed(0, "network"), u.upload(jpeg, 10))
    }
}
