// TASK-45 admin page logic with fake API responses (node --test, no packages).
// Run: scripts/ui-test.sh
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  AdminController,
  addScenario,
  applyJson,
  createApi,
  message,
  newEditor,
  removeScenario,
  setScenario,
  setSetting,
} from "../../src/voa/server/ui/admin/core.js";

const TOKEN = "tok-SECRET-123";

function fakeServer(routes) {
  const calls = [];
  const fetchImpl = async (path, init) => {
    calls.push({ path, ...init });
    const key = `${init.method} ${path}`;
    const handler = routes[key];
    if (handler === undefined) return respond(404, { code: "not_found" });
    const [status, body] = typeof handler === "function" ? handler(init, calls) : handler;
    if (status === "network") throw new TypeError("failed to fetch");
    return respond(status, body);
  };
  return { fetchImpl, calls };
}

function respond(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

const MISSIONS = {
  missions: [
    { mission_id: "fire_c1", name: "화재", description: "", status: "published", latest_version: 1, has_draft: false, sessions: 0 },
  ],
};
const MODELS = {
  models: [
    { model_id: "rtdetr-r18-dfire", kind: "object_detection", revision: "1", labels: ["fire"], weights: "ready", deployed: true, device: "cpu", score_floor: 0.25, used_by: ["fire_c1"] },
    { model_id: "mock-tracker", kind: "track_state", revision: "0", labels: [], weights: "ready", deployed: false, device: null, score_floor: null, used_by: [] },
  ],
};
const FIRE_DETAIL = {
  ...MISSIONS.missions[0],
  draft: null,
  published: {
    version: 1,
    profile: {
      profile_id: "fire_c1",
      profile_version: 1,
      degraded_notice_text: "카메라 상태를 확인할 수 없어 감지가 제한됩니다.",
      params: { min_score: 0.05, confirm_frames: 3 },
      scenarios: [{ id: "fire_in_view", required_models: { object_detection: "rtdetr-r18-dfire" }, labels: ["fire"], roi: [0, 0, 1, 1], severity: "attention", warning_text: "불꽃" }],
    },
    settings: { frame_interval_ms: null, hud_style_map: {} },
  },
};

function adminRoutes(extra = {}) {
  return {
    "POST /v1/auth/login": [200, { token: TOKEN, role: "admin", password_change_required: false }],
    "GET /v1/admin/missions": [200, MISSIONS],
    "GET /v1/admin/models": [200, MODELS],
    "GET /v1/admin/missions/fire_c1": [200, FIRE_DETAIL],
    ...extra,
  };
}

async function loggedIn(extra) {
  const server = fakeServer(adminRoutes(extra));
  const states = [];
  const controller = new AdminController(createApi(server.fetchImpl), (s) => states.push(s.screen));
  await controller.login("admin", "pw-123456");
  return { controller, server, states };
}

test("login shows the mission list and sends the token only in the Authorization header", async () => {
  const { controller, server } = await loggedIn();
  assert.equal(controller.state.screen, "main");
  assert.equal(controller.state.tab, "missions");
  assert.deepEqual(controller.state.missions, MISSIONS.missions);
  const authed = server.calls.filter((c) => c.path !== "/v1/auth/login");
  assert.ok(authed.length > 0);
  for (const call of server.calls) {
    assert.ok(!call.path.includes(TOKEN), "token never in a URL");
    assert.equal(call.credentials, "omit");
  }
  for (const call of authed) assert.equal(call.headers.Authorization, `Bearer ${TOKEN}`);
  assert.ok(!JSON.stringify(controller.state).includes(TOKEN), "token never in page state");
});

test("a wearer account cannot open the admin page", async () => {
  const server = fakeServer({ "POST /v1/auth/login": [200, { token: TOKEN, role: "wearer", password_change_required: false }] });
  const controller = new AdminController(createApi(server.fetchImpl));
  await controller.login("kim", "pw-123456");
  assert.equal(controller.state.screen, "login");
  assert.equal(controller.state.error, message("not_admin"));
  assert.equal(controller.api.hasToken(), false);
});

test("login errors show their own text", async () => {
  for (const [status, code] of [[401, "unauthorized"], [429, "too_many_attempts"], [403, "account_pending"]]) {
    const server = fakeServer({ "POST /v1/auth/login": [status, { code }] });
    const controller = new AdminController(createApi(server.fetchImpl));
    await controller.login("admin", "x");
    assert.equal(controller.state.screen, "login");
    assert.equal(controller.state.error, message(code));
  }
  const offline = new AdminController(createApi(fakeServer({ "POST /v1/auth/login": ["network", null] }).fetchImpl));
  await offline.login("admin", "x");
  assert.equal(offline.state.error, message("network"));
});

test("the first login must change the default password before anything else", async () => {
  const server = fakeServer(
    adminRoutes({
      "POST /v1/auth/login": [200, { token: TOKEN, role: "admin", password_change_required: true }],
      "POST /v1/auth/password": (init) => {
        const body = JSON.parse(init.body);
        assert.deepEqual(body, { current_password: "initial-pass-77", new_password: "new-pass-1" });
        return [200, { password_change_required: false }];
      },
    }),
  );
  const controller = new AdminController(createApi(server.fetchImpl));
  await controller.login("admin", "initial-pass-77");
  assert.equal(controller.state.screen, "password");
  assert.ok(!JSON.stringify(controller.state).includes("initial-pass-77"), "no password in state"); 
  assert.ok(!("currentPassword" in controller.state));
  await controller.changePassword("new-pass-1");
  assert.equal(controller.state.screen, "main");
  assert.equal(controller.pendingPassword, null);
});

test("a 403 password_change_required from any call returns to the password screen", async () => {
  const { controller } = await loggedIn({ "GET /v1/admin/users": [403, { code: "password_change_required" }] });
  await controller.show("users");
  assert.equal(controller.state.screen, "password");
});

test("an expired token logs out to the login screen", async () => {
  const { controller } = await loggedIn({ "GET /v1/admin/changes": [401, { code: "unauthorized" }] });
  await controller.show("changes");
  assert.equal(controller.state.screen, "login");
  assert.equal(controller.state.error, message("unauthorized"));
  assert.equal(controller.api.hasToken(), false);
  assert.deepEqual(controller.state.missions, []);
});

test("editing, validating and publishing a mission", async () => {
  const puts = [];
  const { controller } = await loggedIn({
    "PUT /v1/admin/missions/fire_c1": (init) => {
      puts.push(JSON.parse(init.body));
      return [200, { ...MISSIONS.missions[0], has_draft: true }];
    },
    "POST /v1/admin/missions/fire_c1:validate": [200, { valid: true, next_version: 2 }],
    "POST /v1/admin/missions/fire_c1:publish": [200, { mission_id: "fire_c1", version: 2, status: "published" }],
  });
  await controller.openMission("fire_c1");
  const editor = controller.state.editor;
  assert.equal(editor.latest_version, 1);
  assert.ok(!("profile_version" in editor.profile));
  setScenario(editor, 0, "labels", "fire, smoke");
  setSetting(editor, "frame_interval_ms", "500");
  await controller.validate();
  assert.equal(puts.length, 1, "unsaved edits are saved before validating");
  assert.deepEqual(puts[0].profile.scenarios[0].labels, ["fire", "smoke"]);
  assert.equal(puts[0].settings.frame_interval_ms, 500);
  assert.equal(editor.result.ok, true);
  assert.match(editor.result.text, /v2/);
  await controller.publish();
  assert.equal(puts.length, 1, "nothing new to save");
  assert.equal(controller.state.editor, null);
  assert.match(controller.state.notice, /v2을 게시/);
});

test("a failed validation or publish shows why and keeps the editor open", async () => {
  const { controller } = await loggedIn({
    "PUT /v1/admin/missions/fire_c1": [200, MISSIONS.missions[0]],
    "POST /v1/admin/missions/fire_c1:validate": [400, { code: "profile_model_mismatch" }],
    "POST /v1/admin/missions/fire_c1:publish": [400, { code: "model_not_ready" }],
  });
  await controller.openMission("fire_c1");
  await controller.validate();
  assert.deepEqual(controller.state.editor.result, { ok: false, text: message("profile_model_mismatch") });
  await controller.publish();
  assert.ok(controller.state.editor !== null);
  assert.deepEqual(controller.state.editor.result, { ok: false, text: message("model_not_ready") });
});

test("archiving a mission in use shows the conflict", async () => {
  const { controller } = await loggedIn({ "DELETE /v1/admin/missions/fire_c1": [409, { code: "mission_in_use" }] });
  await controller.archiveMission("fire_c1");
  assert.equal(controller.state.error, message("mission_in_use"));
});

test("creating or copying a mission opens its editor", async () => {
  let body;
  const { controller } = await loggedIn({
    "POST /v1/admin/missions": (init) => {
      body = JSON.parse(init.body);
      return [201, { mission_id: "fire_c1", status: "draft" }];
    },
  });
  await controller.createMission({ mission_id: "fire_c1", name: "화재 복사본", from_mission_id: "fire_c1" });
  assert.deepEqual(body, { mission_id: "fire_c1", name: "화재 복사본", description: "", from_mission_id: "fire_c1" });
  assert.equal(controller.state.editor.mission_id, "fire_c1");
});

test("editor helpers keep at least one rule and reject bad JSON", () => {
  const editor = newEditor({ mission_id: "new_one", name: "새", description: "", latest_version: 0, status: "draft", draft: null, published: null }, ["m1"]);
  assert.equal(editor.profile.profile_id, "new_one");
  assert.equal(editor.profile.scenarios[0].required_models.object_detection, "m1");
  assert.equal(removeScenario(editor, 0), false);
  addScenario(editor, ["m1"]);
  assert.equal(editor.profile.scenarios.length, 2);
  assert.equal(removeScenario(editor, 1), true);
  setScenario(editor, 0, "roi", "0.1, 0.2, 0.9, 1");
  assert.deepEqual(editor.profile.scenarios[0].roi, [0.1, 0.2, 0.9, 1]);
  setSetting(editor, "attention", "attention_default");
  setSetting(editor, "attention", "");
  assert.deepEqual(editor.settings.hud_style_map, {});
  assert.equal(applyJson(editor, "{not json"), "json_invalid");
  assert.equal(applyJson(editor, "[1]"), "json_invalid");
  assert.equal(applyJson(editor, '{"profile_id": "new_one", "profile_version": 9}'), null);
  assert.ok(!("profile_version" in editor.profile));
});

test("a reset password is shown once as a notice", async () => {
  const { controller } = await loggedIn({
    "POST /v1/admin/users/usr_1:reset-password": [200, { user_id: "usr_1", temporary_password: "temp-xyz" }],
    "GET /v1/admin/users": [200, { users: [] }],
  });
  await controller.userAction("usr_1", "reset-password");
  assert.match(controller.state.notice, /temp-xyz/);
});

test("deploy, undeploy conflict and one-image detect", async () => {
  const { controller, server } = await loggedIn({
    "POST /v1/admin/deployments": [201, { model_id: "rtdetr-r18-dfire", deployed: true }],
    "DELETE /v1/admin/deployments/rtdetr-r18-dfire": [409, { code: "model_in_use" }],
    "POST /v1/admin/detect?model_id=rtdetr-r18-dfire": [200, { model_id: "rtdetr-r18-dfire", width: 640, height: 360, detections: [{ label: "fire", score: 0.08, bbox: [0.4, 0.3, 0.5, 0.6] }], provider_ms: 314 }],
  });
  await controller.deploy("rtdetr-r18-dfire", "0.3");
  const deploy = server.calls.find((c) => c.path === "/v1/admin/deployments");
  assert.deepEqual(JSON.parse(deploy.body), { model_id: "rtdetr-r18-dfire", device: "cpu", score_floor: 0.3 });
  await controller.undeploy("rtdetr-r18-dfire");
  assert.equal(controller.state.error, message("model_in_use"));
  const image = new Uint8Array([0xff, 0xd8]);
  await controller.detect("rtdetr-r18-dfire", image);
  const detect = server.calls.at(-1);
  assert.equal(detect.headers["Content-Type"], "image/jpeg");
  assert.equal(detect.body, image);
  assert.equal(controller.state.detect.detections[0].label, "fire");
});

test("settings save shows unknown style errors and reloads on success", async () => {
  let calls = 0;
  const { controller } = await loggedIn({
    "PUT /v1/admin/settings": () => (calls++ === 0 ? [400, { code: "style_not_found" }] : [200, {}]),
    "GET /v1/admin/settings": [200, { frame_interval_ms: 500, hud_styles: {}, hud_style_map: {}, styles: {} }],
  });
  await controller.saveSettings({ hud_style_map: { attention: "nope" } });
  assert.equal(controller.state.error, message("style_not_found"));
  await controller.saveSettings({ frame_interval_ms: 500 });
  assert.equal(controller.state.settings.frame_interval_ms, 500);
  assert.ok(controller.state.notice);
});

test("the page code never touches browser storage, cookies or the URL", () => {
  for (const file of ["core.js", "app.js"]) {
    const source = readFileSync(new URL(`../../src/voa/server/ui/admin/${file}`, import.meta.url), "utf8");
    for (const banned of ["localStorage", "sessionStorage", "indexedDB", "document.cookie", "location", "history.", "innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function"]) {
      assert.ok(!source.includes(banned), `${file} uses ${banned}`);
    }
  }
});

test("the sim handoff answers only the window this page opened, same origin, once logged in", async () => {
  const { answerHandoff, HANDOFF_READY, HANDOFF_LOGIN } = await import("../../src/voa/server/ui/admin/core.js");
  const posted = [];
  const sim = { postMessage: (data, origin) => posted.push({ data, origin }) };
  const other = { postMessage: () => assert.fail("must not answer another window") };
  const api = createApi(async () => respond(200, {}));
  const ready = (source, origin = "http://127.0.0.1:8765", type = HANDOFF_READY) => ({ source, origin, data: { type } });
  assert.equal(answerHandoff(ready(sim), "http://127.0.0.1:8765", sim, api), false, "not logged in yet");
  api.setToken(TOKEN);
  assert.equal(answerHandoff(ready(other), "http://127.0.0.1:8765", sim, api), false);
  assert.equal(answerHandoff(ready(sim, "http://evil.test"), "http://127.0.0.1:8765", sim, api), false);
  assert.equal(answerHandoff(ready(sim, undefined, "other"), "http://127.0.0.1:8765", sim, api), false);
  assert.equal(answerHandoff(ready(sim), "http://127.0.0.1:8765", null, api), false);
  assert.equal(answerHandoff(ready(sim), "http://127.0.0.1:8765", sim, api), true);
  assert.deepEqual(posted, [{ data: { type: HANDOFF_LOGIN, token: TOKEN }, origin: "http://127.0.0.1:8765" }]);
});

test("before handing the login to the sim window it is checked; an expired one logs out", async () => {
  const valid = await loggedIn({ "GET /v1/admin/capacity": [200, { sessions: 0, max_sessions: 3 }] });
  assert.equal(await valid.controller.verifyLogin(), true);
  const expired = await loggedIn({ "GET /v1/admin/capacity": [401, { code: "unauthorized" }] });
  assert.equal(await expired.controller.verifyLogin(), false);
  assert.equal(expired.controller.state.screen, "login");
  assert.equal(expired.controller.state.error, message("unauthorized"));
  assert.equal(expired.controller.api.hasToken(), false);
});
