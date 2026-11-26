"""Shared C2 examples (contracts/fixtures/c2_examples.json) match the server models.

The Android client decodes the same file (AND-02), so a contract change that is not
reflected here fails on both sides.
"""

import json
from pathlib import Path

from pydantic import TypeAdapter

from voa.contracts.messages import ServerMessage
from voa.server.accounts import LoginResponse
from voa.server.auth import MissionRef, SessionCreated

EXAMPLES = json.loads(
    (Path(__file__).resolve().parents[4] / "contracts" / "fixtures" / "c2_examples.json").read_text(
        encoding="utf-8"
    )
)


def test_session_responses_match_the_server_model() -> None:
    for key in ("session_created", "session_created_no_mission"):
        created = SessionCreated.model_validate(EXAMPLES[key])
        assert created.model_dump(mode="json") == EXAMPLES[key]


def test_every_server_message_example_validates_and_round_trips() -> None:
    adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)
    types = set()
    for example in EXAMPLES["server_messages"]:
        message = adapter.validate_python(example)
        assert message.model_dump(mode="json", exclude_none=True) == example
        types.add(example["type"])
    expected = {"auth_ok", "mission", "config", "heartbeat", "alert", "notice", "frame_result"}
    assert expected | {"error"} <= types


def test_mission_refs_in_examples_match() -> None:
    MissionRef.model_validate(EXAMPLES["session_created"]["mission"])


def test_login_response_example_matches() -> None:
    login = LoginResponse.model_validate(EXAMPLES["login_response"])
    assert login.model_dump(mode="json") == EXAMPLES["login_response"]
