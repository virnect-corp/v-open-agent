package ai.voa.client

// 서버 오류 코드 -> 사용자 문구 (시뮬 페이지와 같은 문구, TASK-46). 서버 원문은 보여 주지 않는다.
object Messages {
    private val texts = mapOf(
        "unauthorized" to "아이디나 비밀번호가 맞지 않습니다.",
        "account_pending" to "승인 대기 중입니다. 관리자가 승인하면 로그인할 수 있습니다.",
        "too_many_attempts" to "로그인 시도가 많아 5분 동안 막혔습니다.",
        "password_change_required" to "비밀번호를 바꿔야 하는 계정입니다. 관리자 페이지에서 바꾼 뒤 로그인하세요.",
        "capacity_exceeded" to "동시 접속 정원을 넘었습니다. 잠시 후 다시 시도하세요.",
        "signup_disabled" to "가입을 받지 않는 서버입니다.",
        "username_taken" to "이미 쓰는 아이디입니다.",
        "invalid_request" to "입력 형식이 맞지 않습니다.",
        "not_ready" to "서버가 아직 준비되지 않았습니다.",
        "network" to "서버에 연결할 수 없습니다. 주소와 adb reverse를 확인하세요.",
        "invalid_response" to "서버 응답 형식이 맞지 않습니다. 앱과 서버 버전을 확인하세요.",
        "bad_url" to "서버 주소 형식이 맞지 않습니다.",
    )

    fun of(code: String): String = texts[code] ?: "요청이 실패했습니다 ($code)."
}
