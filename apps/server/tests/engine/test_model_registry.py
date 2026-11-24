"""TASK-26 model manifests and profile cross-check (SPEC 4.9, 9; CON-04, DAT-05)."""

import shutil
from pathlib import Path

import pytest

from voa.config.model_registry import ModelManifest, check_profile_models, load_manifests
from voa.config.profile_loader import load_profile
from voa.engine.profile import PolicyProfile, ProfileError
from voa.replay.__main__ import main as replay_main

SERVER = Path(__file__).resolve().parents[2]
DEFAULT_MODELS = SERVER / "config" / "models"
DEFAULT_PROFILE = SERVER / "config" / "profiles" / "indoor_research_v0.yaml"
TEST_MODELS = SERVER / "tests" / "fixtures" / "models"
TWO_MODELS = SERVER / "tests" / "fixtures" / "profiles" / "two_models.yaml"
SCENARIOS = SERVER.parents[1] / "contracts" / "fixtures" / "scenarios"

VALID = """model_id: {model_id}
revision: "0"
kind: object_detection
labels: [obstacle, person]
weights: builtin
sha256: null
license: same-as-project
license_evidence_url: apps/server/pyproject.toml
redistribution_allowed: true
reviewed_at: 2026-11-18
reviewer: tester
"""


def profile(path: Path = DEFAULT_PROFILE) -> PolicyProfile:
    loaded = load_profile(path)
    assert isinstance(loaded, PolicyProfile)
    return loaded


def manifests(path: Path = DEFAULT_MODELS) -> dict[str, ModelManifest]:
    loaded = load_manifests(path)
    assert not isinstance(loaded, ProfileError), loaded
    return loaded


def write(tmp_path: Path, name: str, text: str) -> Path:
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


# --- Success ---


def test_default_mock_manifests_load() -> None:
    loaded = manifests()
    assert sorted(loaded) == [
        "mock-detector",
        "mock-tracker",
        "omdet-turbo-fire",
        "rtdetr-r18-coco",
    ]
    detector, tracker = loaded["mock-detector"], loaded["mock-tracker"]
    assert (detector.kind, detector.labels, detector.weights) == (
        "object_detection",
        ["person", "obstacle"],
        "builtin",
    )
    assert (tracker.kind, tracker.labels, tracker.sha256) == ("track_state", ["person"], None)


def test_default_profile_matches_default_manifests() -> None:
    assert check_profile_models(profile(), manifests()) is None


def test_two_model_profile_matches_test_manifests() -> None:
    assert check_profile_models(profile(TWO_MODELS), manifests(TEST_MODELS)) is None


def test_file_weights_with_sha256_are_accepted(tmp_path: Path) -> None:
    text = VALID.format(model_id="real-det").replace(
        "weights: builtin\nsha256: null", "weights: file\nsha256: " + "a" * 64
    )
    loaded = manifests(write(tmp_path, "real-det.yaml", text))
    assert loaded["real-det"].sha256 == "a" * 64


# --- Failure: profile does not match the manifests ---


def violation(scenarios_models: dict[str, ModelManifest], **edits: object) -> ProfileError:
    data = profile().model_dump(mode="json")
    for path, value in edits.items():
        index, field = path.split("__")
        data["scenarios"][int(index[1:])][field] = value
    result = check_profile_models(PolicyProfile.model_validate(data), scenarios_models)
    assert isinstance(result, ProfileError)
    assert result.code == "profile_invalid"
    return result


def test_unknown_model_is_rejected() -> None:
    error = violation(manifests(), s1__required_models={"object_detection": "secret-detector"})
    assert "visible_obstacle_in_corridor" in error.detail
    assert "required_models.object_detection" in error.detail
    assert "secret-detector" not in error.detail


def test_kind_mismatch_is_rejected() -> None:
    error = violation(manifests(), s1__required_models={"object_detection": "mock-tracker"})
    assert "kind" in error.detail


def test_label_outside_detector_manifest_is_rejected() -> None:
    error = violation(manifests(), s1__labels=["obstacle", "secret-label"])
    assert "labels" in error.detail and "secret-label" not in error.detail


def test_track_scenario_label_must_also_be_in_tracker_manifest() -> None:
    # "obstacle" is a mock-detector label but not a mock-tracker label.
    error = violation(manifests(), s0__labels=["person", "obstacle"])
    assert "moving_object_in_attention_zone" in error.detail and "labels" in error.detail


def test_all_violations_are_reported_in_stable_order() -> None:
    error = violation(
        manifests(),
        s0__labels=["person", "obstacle"],
        s1__required_models={"object_detection": "nope"},
    )
    assert error.detail.index("moving_object") < error.detail.index("visible_obstacle")


# --- Failure: manifest files themselves are invalid ---


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (("weights: builtin\nsha256: null", "weights: file\nsha256: null"), "sha256 missing"),
        (("weights: builtin\nsha256: null", "weights: file\nsha256: xyz"), "sha256 format"),
        (("sha256: null", "sha256: " + "a" * 64), "builtin with sha256"),
        (("labels: [obstacle, person]", "labels: []"), "empty labels"),
        (("labels: [obstacle, person]", "labels: [obstacle, obstacle]"), "duplicate labels"),
        (("kind: object_detection", "kind: health"), "unsupported kind"),
        (("reviewer: tester\n", "reviewer: tester\nnote: x\n"), "unknown key"),
        (("license: same-as-project\n", ""), "missing license"),
    ],
)
def test_invalid_manifest_is_profile_invalid(
    tmp_path: Path, edit: tuple[str, str], reason: str
) -> None:
    text = VALID.format(model_id="m1")
    assert edit[0] in text, reason
    result = load_manifests(write(tmp_path, "m1.yaml", text.replace(edit[0], edit[1], 1)))
    assert isinstance(result, ProfileError) and result.code == "profile_invalid", reason


def test_file_name_must_match_model_id(tmp_path: Path) -> None:
    result = load_manifests(write(tmp_path, "other.yaml", VALID.format(model_id="m1")))
    assert isinstance(result, ProfileError) and result.code == "profile_invalid"


def test_missing_directory_and_bad_yaml(tmp_path: Path) -> None:
    missing = load_manifests(tmp_path / "nope")
    assert isinstance(missing, ProfileError) and missing.code == "file_not_found"
    bad = load_manifests(write(tmp_path, "m1.yaml", "model_id: [unclosed"))
    assert isinstance(bad, ProfileError) and bad.code == "yaml_invalid"
    root = load_manifests(write(tmp_path, "m1.yaml", "- a list"))
    assert isinstance(root, ProfileError) and root.code == "yaml_invalid"


def test_empty_directory_has_no_models(tmp_path: Path) -> None:
    assert load_manifests(tmp_path) == {}
    result = check_profile_models(profile(), {})
    assert isinstance(result, ProfileError)


def test_error_detail_never_echoes_manifest_values(tmp_path: Path) -> None:
    text = VALID.format(model_id="m1").replace("reviewer: tester", "reviewer: [secret-token]")
    result = load_manifests(write(tmp_path, "m1.yaml", text))
    assert isinstance(result, ProfileError) and "secret-token" not in result.detail


# --- Replay CLI cross-checks by default ---


def test_replay_rejects_profile_whose_models_are_not_registered(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = replay_main(
        [str(SCENARIOS / "tr19_two_models_one_stops.jsonl"), "--profile", str(TWO_MODELS)]
    )
    assert code == 2
    assert "profile_invalid" in capsys.readouterr().err


def test_replay_accepts_explicit_models_directory(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    models = tmp_path / "models"
    shutil.copytree(TEST_MODELS, models)
    code = replay_main(
        [
            str(SCENARIOS / "tr19_two_models_one_stops.jsonl"),
            "--profile",
            str(TWO_MODELS),
            "--models",
            str(models),
            "--expected",
            str(SCENARIOS / "tr19_two_models_one_stops.expected.jsonl"),
        ]
    )
    assert code == 0, capsys.readouterr().err
