"""TASK-14 evidence repository, explanation validator, template fallback, actions (SPEC 6)."""

import hashlib
from pathlib import Path

import pytest

from tests.builders import build_observation
from voa.config.profile_loader import load_profile
from voa.contracts.decision import RiskDecision
from voa.contracts.observation import Detection, DetectionPayload
from voa.engine.engine import DecisionEngine
from voa.engine.ports import FixedEpochClock, SequentialIdFactory
from voa.engine.profile import PolicyProfile
from voa.explain.actions import (
    ALLOWED_ACTIONS,
    ActionExecutor,
    ForbiddenActionError,
    idempotency_key,
)
from voa.explain.repository import ObservationRepository
from voa.explain.template import TemplateExplanationProvider
from voa.explain.validator import (
    ACTION_TERMS,
    IDENTITY_TERMS,
    ExplanationValidator,
    ValidationResult,
)

MS = 1_000_000
SESSION = "sess_0001"
OBSTACLE = "visible_obstacle_in_corridor"
OBSTACLE_TEXT = "전방 통로에 장애물이 보입니다."


@pytest.fixture
def profile() -> PolicyProfile:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "profiles" / "valid.yaml"
    result = load_profile(path)
    assert isinstance(result, PolicyProfile)
    return result


def warmed_engine(profile: PolicyProfile) -> tuple[DecisionEngine, list[RiskDecision]]:
    """Three obstacle frames at 0/100/200 ms -> candidate, warning on obs_0003."""
    engine = DecisionEngine(SESSION, "dev_0001", profile, SequentialIdFactory(), FixedEpochClock())
    decisions: list[RiskDecision] = []
    for i in range(3):
        now = i * 100 * MS
        obs = build_observation(
            observation_id=f"obs_{i + 1:04d}",
            session_id=SESSION,
            seq=i,
            capture_ts_monotonic_ns=now,
            server_received_mono_ns=now,
            payload=DetectionPayload(
                kind="object_detection",
                frame_id=f"f{i}",
                detections=[Detection(label="obstacle", bbox=(0.3, 0.5, 0.7, 0.9), score=0.9)],
            ),
        )
        decisions += engine.step(obs, now).decisions
    return engine, decisions


@pytest.fixture
def repo(profile: PolicyProfile) -> ObservationRepository:
    return ObservationRepository(warmed_engine(profile)[0])


@pytest.fixture
def validator(repo: ObservationRepository) -> ExplanationValidator:
    return ExplanationValidator(repo)


NOW = 200 * MS
CLEAN = "통로 아래쪽에 장애물로 보이는 물체가 연속으로 관측되었습니다."


# --- Repository: engine public queries only, read-only (ENG-12) ---


def test_repository_reads_through_public_engine_queries(repo: ObservationRepository) -> None:
    observation = repo.get("obs_0003")
    assert observation is not None and observation.session_id == SESSION
    assert repo.session_of("obs_0003") == SESSION
    assert repo.get("obs_missing") is None and repo.session_of("obs_missing") is None
    assert repo.is_fresh("obs_0001", 1000 * MS)
    assert not repo.is_fresh("obs_0001", 1001 * MS)
    assert not repo.is_fresh("obs_missing", NOW)


def test_repository_does_not_advance_engine_time(profile: PolicyProfile) -> None:
    engine, _ = warmed_engine(profile)
    repo = ObservationRepository(engine)
    assert not repo.is_fresh("obs_0003", 60_000 * MS)  # far-future query
    assert repo.get("obs_0001") is not None  # nothing evicted
    assert engine.tick(300 * MS) == []  # engine clock not moved past 300 ms
    with pytest.raises(ValueError):
        repo.is_fresh("obs_0003", 0)  # earlier than the engine's last time


def test_repository_uses_only_public_engine_api() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "src" / "voa" / "explain" / "repository.py"
    ).read_text(encoding="utf-8")
    assert "._" not in source.replace("self._engine", "")


# --- Validator: success ---


def test_clean_explanation_with_fresh_evidence_passes(validator: ExplanationValidator) -> None:
    result = validator.validate(CLEAN, ["obs_0002", "obs_0003"], SESSION, NOW)
    assert result == ValidationResult(ok=True, violations=())


@pytest.mark.parametrize("text", ["3번째 프레임에서 보였습니다.", "장애물 2개가 보입니다."])
def test_counts_without_units_are_not_numeric_claims(
    validator: ExplanationValidator, text: str
) -> None:
    assert validator.validate(text, ["obs_0003"], SESSION, NOW).ok


# --- Validator: each violation code (T-R08) ---


@pytest.mark.parametrize(
    "text",
    [
        "약 1.2m 앞에 있습니다.",
        "시속 40km로 접근합니다.",
        "충돌 확률 30%",
        "2초 뒤",
        "5 KM",
        "3,5 미터",
        "10㎞",
        "1분 안에",
        "80 퍼센트",
    ],
)
def test_numeric_claim(validator: ExplanationValidator, text: str) -> None:
    result = validator.validate(text, ["obs_0003"], SESSION, NOW)
    assert result.violations == ("numeric_claim",)


@pytest.mark.parametrize("term", IDENTITY_TERMS)
def test_identity_claim(validator: ExplanationValidator, term: str) -> None:
    result = validator.validate(f"앞 사람의 {term} 확인", ["obs_0003"], SESSION, NOW)
    assert result.violations == ("identity_claim",)


@pytest.mark.parametrize("term", ACTION_TERMS)
def test_action_claim(validator: ExplanationValidator, term: str) -> None:
    result = validator.validate(f"관리자에게 {term}습니다", ["obs_0003"], SESSION, NOW)
    assert result.violations == ("action_claim",)


def test_unknown_evidence_including_empty_list(validator: ExplanationValidator) -> None:
    assert validator.validate(CLEAN, ["obs_0003", "obs_9999"], SESSION, NOW).violations == (
        "unknown_evidence",
    )
    assert validator.validate(CLEAN, [], SESSION, NOW).violations == ("unknown_evidence",)


def test_foreign_session(validator: ExplanationValidator) -> None:
    result = validator.validate(CLEAN, ["obs_0003"], "sess_other", NOW)
    assert result.violations == ("foreign_session",)


def test_stale_evidence(validator: ExplanationValidator) -> None:
    """Delay: obs_0001 was received at 0 ms; 1001 ms later it is past staleness_ms."""
    assert validator.validate(CLEAN, ["obs_0001"], SESSION, 1000 * MS).ok
    result = validator.validate(CLEAN, ["obs_0001"], SESSION, 1001 * MS)
    assert result.violations == ("stale_evidence",)


def test_all_violations_reported_once_in_spec_order(validator: ExplanationValidator) -> None:
    text = f"1.2m 앞, {IDENTITY_TERMS[0]} 확인, {ACTION_TERMS[0]}다, 2초, {IDENTITY_TERMS[1]}"
    result = validator.validate(text, ["obs_9999", "obs_8888"], SESSION, NOW)
    assert result == ValidationResult(
        ok=False,
        violations=("unknown_evidence", "numeric_claim", "identity_claim", "action_claim"),
    )


# --- Template fallback (EXP-01) ---


def test_valid_candidate_is_kept_as_validated_explanation(
    profile: PolicyProfile, repo: ObservationRepository
) -> None:
    _, decisions = warmed_engine(profile)
    warning = decisions[-1]
    provider = TemplateExplanationProvider(profile, ExplanationValidator(repo))
    explanation = provider.explain(warning, CLEAN, NOW)
    assert (explanation.text, explanation.text_kind) == (CLEAN, "validated_explanation")
    assert explanation.violations == ()


@pytest.mark.parametrize(
    ("candidate", "now"),
    [
        ("약 1.2m 앞입니다.", NOW),  # numeric_claim
        (f"{IDENTITY_TERMS[0]} 확인됨", NOW),  # identity_claim
        (f"{ACTION_TERMS[0]}습니다", NOW),  # action_claim
        (CLEAN, 1201 * MS),  # stale_evidence (obs_0003 received at 200 ms)
        (None, NOW),  # no explainer available (e.g. VLM off or timed out)
        ("", NOW),
    ],
)
def test_any_violation_or_missing_candidate_falls_back_to_template(
    profile: PolicyProfile, repo: ObservationRepository, candidate: str | None, now: int
) -> None:
    _, decisions = warmed_engine(profile)
    provider = TemplateExplanationProvider(profile, ExplanationValidator(repo))
    explanation = provider.explain(decisions[-1], candidate, now)
    assert (explanation.text, explanation.text_kind) == (OBSTACLE_TEXT, "approved_template")


def test_unknown_and_foreign_evidence_fall_back(
    profile: PolicyProfile, repo: ObservationRepository
) -> None:
    _, decisions = warmed_engine(profile)
    provider = TemplateExplanationProvider(profile, ExplanationValidator(repo))
    unknown = decisions[-1].model_copy(update={"evidence_ids": ["obs_9999"]})
    foreign = decisions[-1].model_copy(update={"session_id": "sess_other"})
    for decision, code in ((unknown, "unknown_evidence"), (foreign, "foreign_session")):
        explanation = provider.explain(decision, CLEAN, NOW)
        assert explanation.text_kind == "approved_template"
        assert explanation.violations == (code,)


def test_unknown_scenario_is_a_caller_error(
    profile: PolicyProfile, repo: ObservationRepository
) -> None:
    _, decisions = warmed_engine(profile)
    provider = TemplateExplanationProvider(profile, ExplanationValidator(repo))
    with pytest.raises(ValueError):
        provider.explain(decisions[-1].model_copy(update={"scenario_id": "nope"}), CLEAN, NOW)


# --- Actions (EXP-02) ---


IDS = {
    "device_id": "dev_0001",
    "scenario_id": OBSTACLE,
    "risk_event_id": "risk_0001",
    "alert_id": "alert_0001",
}


@pytest.mark.parametrize("action", sorted(ALLOWED_ACTIONS))
def test_allowed_actions_return_a_request_with_deterministic_key(action: str) -> None:
    executor = ActionExecutor()
    first = executor.execute(action, **IDS)
    second = ActionExecutor().execute(action, **IDS)
    assert first == second
    assert first.action_kind == action
    expected = hashlib.sha256(
        "|".join(["dev_0001", OBSTACLE, "risk_0001", "alert_0001", action]).encode()
    ).hexdigest()
    assert first.idempotency_key == expected == idempotency_key(action_kind=action, **IDS)


def test_allowlist_matches_spec() -> None:
    assert ALLOWED_ACTIONS == frozenset(
        {
            "play_alert",
            "show_text",
            "repeat_last_verified_alert",
            "acknowledge_alert",
            "export_metadata_if_authorized",
        }
    )


@pytest.mark.parametrize("field", sorted(IDS))
def test_key_changes_with_every_input(field: str) -> None:
    base = idempotency_key(action_kind="play_alert", **IDS)
    assert idempotency_key(action_kind="play_alert", **{**IDS, field: "other"}) != base
    assert idempotency_key(action_kind="show_text", **IDS) != base


@pytest.mark.parametrize(
    "action",
    [
        "call_emergency",
        "stop_machine",
        "send_message",
        "PLAY_ALERT",
        " play_alert",
        "play_alert\n",
        "",
        "경고 꺼",
    ],
)
def test_forbidden_actions_raise(action: str) -> None:
    with pytest.raises(ForbiddenActionError):
        ActionExecutor().execute(action, **IDS)
