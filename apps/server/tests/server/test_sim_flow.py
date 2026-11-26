"""TASK-46 the sim page's flow with the same API, driven from Python (SPEC 7.1, 7.4, 7.6).

wearer signup -> admin approval -> session with `capabilities.hud` -> control socket
with the browser subprotocol -> mission list and choice -> frames -> alert with
`presentation` -> `received`/`played` ACKs, plus "no upload before the socket".
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from tests.server.test_admin_missions import Admin
from tests.server.test_frames import jpeg, make_client, next_of, workdir  # noqa: F401

HUD = {"color": False, "flash_modes": ["none", "border"], "max_lines": 2}


@pytest.fixture
def client(workdir: Path) -> Iterator[TestClient]:  # noqa: F811
    with make_client(workdir) as c:
        yield c


def wearer_session(client: TestClient, **extra: Any) -> dict[str, Any]:
    signup = client.post("/v1/auth/signup", json={"username": "kim", "password": "wearer-pass-1"})
    assert signup.status_code == 201 and signup.json()["status"] == "pending"
    body = {
        "username": "kim",
        "password": "wearer-pass-1",
        "device_label": "sim",
        "capabilities": {"inputs": ["camera_0"], "outputs": ["hud", "speaker"], "hud": HUD},
        **extra,
    }
    assert client.post("/v1/sessions", json=body).json() == {"code": "account_pending"}
    admin = Admin(client)
    assert admin.post(f"/users/{signup.json()['user_id']}:approve").status_code == 200
    created = client.post("/v1/sessions", json=body)
    assert created.status_code == 200
    return dict(created.json())


def browser_socket(client: TestClient, session: dict[str, Any]) -> WebSocketTestSession:
    """As a browser does it: no headers, the token rides in the subprotocol list."""
    return client.websocket_connect(
        f"/v1/streams/{session['session_id']}",
        subprotocols=["voa.v1", f"voa.bearer.{session['token']}"],
    )


def upload(client: TestClient, session: dict[str, Any], seq: int, marker: bytes) -> Any:
    return client.post(
        f"/v1/sessions/{session['session_id']}/frames",
        content=jpeg(marker),
        headers={
            "Authorization": f"Bearer {session['token']}",
            "Content-Type": "image/jpeg",
            "X-VOA-Stream-Id": "camera_0",
            "X-VOA-Seq": str(seq),
            "X-VOA-Frame-Id": f"f_{seq:06d}",
            "X-VOA-Capture-Ts-Ns": str(seq * 333_000_000),
        },
    )


def test_mission_choice_then_frames_then_alert_then_played(client: TestClient) -> None:
    session = wearer_session(client)
    auth = {"Authorization": f"Bearer {session['token']}"}
    with browser_socket(client, session) as ws:
        assert ws.accepted_subprotocol == "voa.v1"
        assert next_of(ws, "auth_ok")["session_id"] == session["session_id"]
        ws.send_json({"type": "alert_sync", "last_alert_seq": 0})
        missions = client.get("/v1/missions", headers=auth).json()["missions"]
        assert "indoor_research_v0" in [m["mission_id"] for m in missions]
        chosen = client.put(
            f"/v1/sessions/{session['session_id']}/mission",
            json={"mission_id": "indoor_research_v0"},
            headers=auth,
        ).json()
        assert next_of(ws, "mission")["frame_interval_ms"] == chosen["frame_interval_ms"] == 333
        for seq in range(1, 4):
            assert upload(client, session, seq, b"OBSTACLE").status_code == 200
        alert = next_of(ws, "alert")
        presentation = alert["presentation"]
        assert presentation["style_id"] == "attention_default"
        assert presentation["text"] == alert["alert"]["text"]
        alert_id = alert["alert"]["alert_id"]
        ws.send_json({"type": "ack", "alert_id": alert_id, "status": "received"})
        ws.send_json({"type": "ack", "alert_id": alert_id, "status": "played"})
        beat = next_of(ws, "heartbeat")
        assert beat["states"]["visible_obstacle_in_corridor"] == "warning"
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    portal = client.portal
    assert portal is not None
    row = portal.call(runtime.store.get_alert, alert_id)  # the server's SQLite thread
    assert row is not None and row["delivery_state"] == "played"


def test_no_upload_is_accepted_before_the_control_socket(client: TestClient) -> None:
    session = wearer_session(client)
    assert upload(client, session, 1, b"OBSTACLE").json() == {"code": "control_not_connected"}


def test_a_browser_token_in_the_wrong_subprotocol_form_is_refused(client: TestClient) -> None:
    session = wearer_session(client)
    with pytest.raises(Exception):  # noqa: B017 - HTTP 401 denial, no accept
        with client.websocket_connect(
            f"/v1/streams/{session['session_id']}",
            subprotocols=[f"voa.bearer.{session['token']}"],  # missing voa.v1
        ) as ws:
            ws.receive_json()
