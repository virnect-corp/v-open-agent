package ai.voa.client.session

import ai.voa.client.protocol.ApiErrorResponse
import ai.voa.client.protocol.CreateSessionRequest
import ai.voa.client.protocol.CreateSessionResponse
import ai.voa.client.protocol.LoginRequest
import ai.voa.client.protocol.LoginResponse
import ai.voa.client.protocol.MissionRef
import ai.voa.client.protocol.MissionsResponse
import ai.voa.client.protocol.ProtocolContract
import ai.voa.client.protocol.RefreshSessionRequest
import ai.voa.client.protocol.RefreshSessionResponse
import ai.voa.client.protocol.SelectMissionRequest
import ai.voa.client.protocol.SignupRequest
import ai.voa.client.protocol.SignupResponse
import kotlinx.serialization.KSerializer
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException

/** REST 결과. 실패는 서버의 고정 오류 코드만 담는다(요청 내용이나 비밀번호는 담지 않음). */
sealed interface ApiResult<out T> {
    data class Ok<T>(val value: T) : ApiResult<T>

    data class Failed(
        val status: Int,
        val code: String,
        val retryAfterS: Int? = null,
    ) : ApiResult<Nothing>
}

/**
 * 세션 REST API (SPEC 7.1, ADR-0010, ADR-0011). 호출은 블로킹이므로 IO 스레드에서 쓴다.
 * 네트워크 실패는 code "network"(status 0)다.
 */
class SessionApi(
    private val baseUrl: HttpUrl,
    private val client: OkHttpClient,
) {
    fun signup(request: SignupRequest): ApiResult<SignupResponse> =
        post("v1/auth/signup", request, SignupRequest.serializer(), SignupResponse.serializer(), token = null)

    /** 계정 로그인: 로그인 token만 받고 정원은 차지하지 않는다 (TASK-47). */
    fun login(request: LoginRequest): ApiResult<LoginResponse> =
        post("v1/auth/login", request, LoginRequest.serializer(), LoginResponse.serializer(), token = null)

    /** 연결: `loginToken`이 있으면 헤더로 보낸다(본문에 계정 없음). */
    fun createSession(request: CreateSessionRequest, loginToken: String? = null): ApiResult<CreateSessionResponse> =
        post("v1/sessions", request, CreateSessionRequest.serializer(), CreateSessionResponse.serializer(), token = loginToken)

    /** 연결 해제: 세션을 끝내 정원을 돌려준다. 로그인은 유지된다 (TASK-47). */
    fun disconnect(sessionId: String, sessionToken: String): ApiResult<Unit> =
        try {
            client.newCall(builder("v1/sessions/$sessionId", sessionToken).delete().build()).execute().use { response ->
                if (response.isSuccessful) ApiResult.Ok(Unit) else ApiResult.Failed(response.code, "http_${response.code}")
            }
        } catch (_: IOException) {
            ApiResult.Failed(0, "network")
        }

    fun refresh(sessionId: String, refreshToken: String): ApiResult<RefreshSessionResponse> =
        post(
            "v1/sessions/$sessionId/refresh",
            RefreshSessionRequest(refreshToken),
            RefreshSessionRequest.serializer(),
            RefreshSessionResponse.serializer(),
            token = null,
        )

    fun missions(token: String): ApiResult<MissionsResponse> =
        execute(builder("v1/missions", token).get().build(), MissionsResponse.serializer())

    fun selectMission(sessionId: String, missionId: String, token: String): ApiResult<MissionRef> =
        execute(
            builder("v1/sessions/$sessionId/mission", token)
                .put(json(SelectMissionRequest(missionId), SelectMissionRequest.serializer()))
                .build(),
            MissionRef.serializer(),
        )

    private fun <Q, R> post(
        path: String,
        body: Q,
        requestSerializer: KSerializer<Q>,
        responseSerializer: KSerializer<R>,
        token: String?,
    ): ApiResult<R> = execute(builder(path, token).post(json(body, requestSerializer)).build(), responseSerializer)

    private fun builder(path: String, token: String?): Request.Builder {
        val builder = Request.Builder().url(baseUrl.resolve(path) ?: error("bad path"))
        if (token != null) builder.header("Authorization", "Bearer $token")
        return builder
    }

    private fun <Q> json(body: Q, serializer: KSerializer<Q>): RequestBody =
        ProtocolContract.json.encodeToString(serializer, body).toRequestBody(JSON)

    private fun <R> execute(request: Request, serializer: KSerializer<R>): ApiResult<R> =
        try {
            client.newCall(request).execute().use { response ->
                val text = response.body?.string().orEmpty()
                if (response.isSuccessful) {
                    ApiResult.Ok(ProtocolContract.json.decodeFromString(serializer, text))
                } else {
                    val error = runCatching {
                        ProtocolContract.json.decodeFromString(ApiErrorResponse.serializer(), text)
                    }.getOrNull()
                    val retryAfter = error?.retryAfterS ?: response.header("Retry-After")?.toIntOrNull()
                    ApiResult.Failed(response.code, error?.code ?: "http_${response.code}", retryAfter)
                }
            }
        } catch (_: IOException) {
            ApiResult.Failed(0, "network")
        } catch (_: IllegalArgumentException) {
            ApiResult.Failed(0, "invalid_response") // schema mismatch; never echoes the body
        }

    companion object {
        private val JSON = "application/json".toMediaType()
    }
}
