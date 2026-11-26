// TASK-46 sim page logic: rule tables and the device flow with fake fetch/WebSocket/clock.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  BUILT_IN,
  HUD_MODELS,
  STATUS,
  SimController,
  message,
  reducePresentation,
  statusBar,
} from "../../src/voa/server/ui/sim/core.js";

const ATTENTION = {
  style_id: "attention_default",
  text: "불꽃으로 보이는 것이 감지되었습니다.",
  position: "top_center",
  text_size: "large",
  color: "red",
  flash: { mode: "full_screen", count: 3, on_ms: 200, off_ms: 200 },
  show_ms: 4000,
  speech: true,
  haptic: "pattern_a",
};

// --- presentation reduction (SPEC 7.4) ---

test("presentation is reduced per HUD model: full screen, then border, then none", () => {
  const color = reducePresentation(ATTENTION, HUD_MODELS.color_full.hud);
  assert.equal(color.flash.mode, "full_screen");
  assert.equal(color.color, "red");
  const border = reducePresentation(ATTENTION, HUD_MODELS.mono_border.hud);
  assert.deepEqual([border.flash.mode, border.color, border.inverse], ["border", null, true]);
  const plain = reducePresentation(ATTENTION, HUD_MODELS.mono_plain.hud);
  assert.deepEqual([plain.flash.mode, plain.flash.count], ["none", 0]);
  const undeclared = reducePresentation(ATTENTION);
  assert.deepEqual([undeclared.flash.mode, undeclared.color, undeclared.maxLines], ["none", null, 2]);
  const borderOnly = reducePresentation({ ...ATTENTION, flash: { ...ATTENTION.flash, mode: "border" } }, HUD_MODELS.color_full.hud);
  assert.equal(borderOnly.flash.mode, "border");
  assert.equal(plain.text, ATTENTION.text);
  assert.equal(plain.showMs, 4000);
});

// --- status bar rule table (SPEC 7.4) ---

const BASE = {
  capacity: false,
  connected: true,
  lastServerMessageMs: 10_000,
  watchdogMs: 3000,
  uploadFailures: 0,
  uploading: true,
  lastResponseMs: 10_000,
  frameIntervalMs: 333,
  states: { fire_in_view: "clear" },
  now: 10_100,
};

const TABLE = [
  ["capacity beats everything", { capacity: true, connected: false, uploadFailures: 9 }, "capacity", false],
  ["closed socket", { connected: false, uploadFailures: 9 }, "disconnected", false],
  ["watchdog silence", { now: 13_000 }, "disconnected", false],
  ["three failed uploads", { uploadFailures: 3, states: { a: "warning" } }, "server_error", false],
  ["two failures are not yet an error", { uploadFailures: 2 }, "normal", true],
  ["no response for 2 x interval + sync timeout", { now: 11_666, lastServerMessageMs: 11_600, states: { a: "warning" } }, "slow", true],
  ["not slow when not uploading", { uploading: false, now: 11_666, lastServerMessageMs: 11_600 }, "normal", true],
  ["warning", { states: { a: "warning", b: "unknown" } }, "warning", true],
  ["unknown", { states: { a: "unknown", b: "candidate" } }, "unknown", true],
  ["nothing received is not normal", { states: {} }, "unknown", true],
  ["candidate", { states: { a: "candidate", b: "clear" } }, "candidate", true],
  ["normal", { states: { a: "clear", b: "resolved" } }, "normal", true],
];

for (const [name, patch, key, showStates] of TABLE) {
  test(`status bar: ${name}`, () => {
    const row = statusBar({ ...BASE, ...patch });
    assert.equal(row.key, key);
    assert.equal(row.showStates, showStates);
    assert.equal(row.rank, STATUS.findIndex((s) => s.key === key));
  });
}

// --- device flow ---

class FakeSocket {
  static last = null;
  constructor(url, protocols) {
    this.url = url;
    this.protocols = protocols;
    this.sent = [];
    this.closed = false;
    FakeSocket.last = this;
  }
  send(text) {
    this.sent.push(JSON.parse(text));
  }
  close() {
    this.closed = true;
  }
  serverSends(msg) {
    this.onmessage({ data: JSON.stringify(msg) });
  }
}

const SESSION = {
  session_id: "sess_1",
  token: "tok-SECRET-1",
  refresh_token: "ref-SECRET-1",
  heartbeat_ms: 1000,
  watchdog_ms: 3000,
  frame_interval_ms: 333,
  mission: { mission_id: "indoor_research_v0", version: 1, name: "실내", frame_interval_ms: 333 },
};
const FIRE = { mission_id: "fire_c1", version: 2, name: "화재 감시", frame_interval_ms: 500 };

function harness(routes = {}) {
  const calls = [];
  const spoken = [];
  const clock = { now: 1000 };
  const timers = [];
  const table = {
    "POST /v1/sessions": [200, SESSION],
    "GET /v1/missions": [200, { missions: [FIRE] }],
    "PUT /v1/sessions/sess_1/mission": [200, FIRE],
    ...routes,
  };
  const fetch = async (path, init) => {
    calls.push({ path, ...init });
    const handler = table[`${init.method} ${path}`];
    const [status, body] = typeof handler === "function" ? handler(init) : handler ?? [404, { code: "not_found" }];
    if (status === "network") throw new TypeError("offline");
    return { ok: status >= 200 && status < 300, status, json: async () => body };
  };
  const controller = new SimController({
    fetch,
    WebSocket: FakeSocket,
    now: () => clock.now,
    wsUrl: (path) => `ws://test${path}`,
    onChange: () => {},
    speak: (text) => spoken.push(text),
    setTimer: (fn, ms) => timers.push({ fn, ms }) - 1,
    clearTimer: () => {},
  });
  return { controller, calls, spoken, clock, timers };
}

const FRAME = (clock) => ({ bytes: new Uint8Array([0xff, 0xd8, 1]), image: "frame-image", captureMs: clock.now });

async function ready(h) {
  await h.controller.login("kim", "pw-12345678");
  FakeSocket.last.serverSends({ type: "auth_ok", session_id: "sess_1", server_time_utc: "2026-11-26T00:00:00Z" });
  await h.controller.chooseMission("fire_c1");
  return FakeSocket.last;
}

test("login opens the control socket with the bearer subprotocol, then waits for a mission", async () => {
  const h = harness();
  await h.controller.login("kim", "pw-12345678", "sim");
  const body = JSON.parse(h.calls[0].body);
  assert.deepEqual(body.capabilities.inputs, ["camera_0"]);
  assert.deepEqual(body.capabilities.hud, HUD_MODELS.color_full.hud);
  assert.equal(body.device_label, "sim");
  const ws = FakeSocket.last;
  assert.equal(ws.url, "ws://test/v1/streams/sess_1");
  assert.deepEqual(ws.protocols, ["voa.v1", "voa.bearer.tok-SECRET-1"]);
  assert.equal(h.controller.state.screen, "missions");
  assert.deepEqual(h.controller.state.missions, [FIRE]);
  assert.ok(!JSON.stringify(h.controller.state).includes("SECRET"), "tokens are not page state");
  ws.serverSends({ type: "auth_ok", session_id: "sess_1", server_time_utc: "x" });
  assert.deepEqual(ws.sent[0], { type: "alert_sync", last_alert_seq: 0 });
  assert.equal(h.controller.startCapture(), false, "no capture before the mission is chosen");
});

test("no upload before auth_ok and the mission choice", async () => {
  const h = harness();
  await h.controller.login("kim", "pw-12345678");
  await h.controller.chooseMission("fire_c1");
  assert.equal(h.controller.startCapture(), false, "socket not authenticated yet");
  assert.equal(await h.controller.upload(FRAME(h.clock)), null);
  assert.ok(!h.calls.some((c) => c.path.endsWith("/frames")));
  FakeSocket.last.serverSends({ type: "auth_ok", session_id: "sess_1", server_time_utc: "x" });
  assert.equal(h.controller.startCapture(), true);
});

test("frames: one in flight, never faster than frame_interval_ms, config and mission change it", async () => {
  let release;
  const h = harness({
    "POST /v1/sessions/sess_1/frames": () => [200, { frame_id: "f_000001", seq: 1, status: "processed", results: [], states: { fire_in_view: "candidate" }, timing: { queue_ms: 1, provider_ms: 300, engine_us: 50 } }],
  });
  const ws = await ready(h);
  assert.equal(h.controller.state.frameIntervalMs, 500, "the mission's interval");
  h.controller.startCapture();
  assert.equal(await h.controller.upload(FRAME(h.clock)), 200);
  const sent = h.calls.find((c) => c.path.endsWith("/frames"));
  assert.equal(sent.headers["Content-Type"], "image/jpeg");
  assert.equal(sent.headers.Authorization, "Bearer tok-SECRET-1");
  assert.equal(sent.headers["X-VOA-Frame-Id"], "f_000001");
  assert.equal(await h.controller.upload(FRAME(h.clock)), null, "interval not reached");
  h.clock.now += 499;
  assert.equal(h.controller.due(), false);
  h.clock.now += 1;
  assert.equal(h.controller.due(), true);
  ws.serverSends({ type: "config", frame_interval_ms: 750 });
  assert.equal(h.controller.state.frameIntervalMs, 750);
  assert.equal(h.controller.due(), false);
  ws.serverSends({ type: "mission", mission_id: "fire_c1", version: 3, name: "화재 감시", frame_interval_ms: 333 });
  assert.equal(h.controller.state.mission.version, 3);
  assert.equal(h.controller.due(), true);
  void release;
});

test("only page-made JPEG frames are uploaded, never a file", async () => {
  const h = harness({ "POST /v1/sessions/sess_1/frames": [202, { frame_id: "f_000001", status: "accepted" }] });
  await ready(h);
  h.controller.startCapture();
  const frame = FRAME(h.clock);
  await h.controller.upload(frame);
  for (const call of h.calls) {
    if (call.body instanceof Uint8Array) assert.equal(call.body, frame.bytes);
    else if (call.body !== undefined) assert.equal(typeof call.body, "string");
  }
  for (const file of ["core.js", "app.js"]) {
    const source = readFileSync(new URL(`../../src/voa/server/ui/sim/${file}`, import.meta.url), "utf8");
    assert.ok(!/fetch\([^)]*file/i.test(source), `${file} never fetches with a file`);
    assert.ok(!source.includes("FormData"), `${file} never builds a form upload`);
  }
});

test("202 then frame_result draws boxes on the judged frame with its age", async () => {
  const h = harness({ "POST /v1/sessions/sess_1/frames": [202, { frame_id: "f_000001", status: "accepted" }] });
  const ws = await ready(h);
  h.controller.startCapture();
  await h.controller.upload(FRAME(h.clock));
  assert.equal(h.controller.state.judged, null);
  h.clock.now += 1200;
  ws.serverSends({ type: "frame_result", frame_id: "f_000001", seq: 1, status: "processed", results: [{ model_id: "m", observation_id: "o", detections: [{ label: "fire", score: 0.08, bbox: [0.4, 0.3, 0.5, 0.6] }] }], states: { fire_in_view: "candidate" }, timing: { queue_ms: 800, provider_ms: 380, engine_us: 60 } });
  const judged = h.controller.state.judged;
  assert.equal(judged.image, "frame-image");
  assert.equal(judged.detections[0].label, "fire");
  assert.equal(h.controller.state.timing.rttMs, 1200);
  assert.equal(h.controller.frames.size, 0, "the frame is released after use");
});

test("an alert is shown, spoken and acknowledged received then played; a repeat only re-ACKs", async () => {
  const h = harness();
  const ws = await ready(h);
  ws.sent.length = 0;
  const alert = { type: "alert", alert_seq: 1, risk_event_id: "re_1", scenario_id: "fire_in_view", alert: { alert_id: "al_1", text: ATTENTION.text, text_kind: "approved_template", delivery_state: "sent" }, presentation: ATTENTION };
  ws.serverSends(alert);
  assert.deepEqual(ws.sent, [
    { type: "ack", alert_id: "al_1", status: "received" },
    { type: "ack", alert_id: "al_1", status: "played" },
  ]);
  assert.equal(h.controller.state.hud.text, ATTENTION.text);
  assert.deepEqual(h.spoken, [ATTENTION.text]);
  ws.sent.length = 0;
  ws.serverSends(alert);
  assert.deepEqual(ws.sent.map((m) => m.status), ["received", "played"]);
  assert.equal(h.spoken.length, 1, "not rendered again");
  h.clock.now += 4000;
  h.controller.expireHud();
  assert.equal(h.controller.state.hud, null);
  assert.equal(h.controller.state.lastAlertSeq, 1);
});

test("capacity: session refused with 503 or socket closed 4503 shows the notice once", async () => {
  const refused = harness({ "POST /v1/sessions": [503, { code: "capacity_exceeded", active: 3, max: 3, retry_after_s: 30 }] });
  await refused.controller.login("kim", "pw-12345678");
  assert.equal(refused.controller.state.capacity, true);
  assert.equal(refused.controller.state.error, message("capacity_exceeded"));
  assert.deepEqual(refused.spoken, [BUILT_IN.capacity]);
  assert.equal(refused.controller.status().key, "capacity");

  const closed = harness();
  await closed.controller.login("kim", "pw-12345678");
  const ws = FakeSocket.last;
  ws.serverSends({ type: "error", code: "capacity_exceeded", message: "x", active: 3, max: 3, retry_after_s: 30 });
  ws.onclose({ code: 4503 });
  assert.equal(closed.controller.status().key, "capacity");
  assert.deepEqual(closed.spoken, [BUILT_IN.capacity]);
  assert.equal(closed.timers.length, 0, "no reconnect loop on capacity");
});

test("a lost connection is announced once and reconnects with a refreshed token", async () => {
  const h = harness({ "POST /v1/sessions/sess_1/refresh": [200, { token: "tok-2", refresh_token: "ref-2" }] });
  const ws = await ready(h);
  ws.onclose({ code: 1006 });
  assert.equal(h.controller.status().key, "disconnected");
  assert.deepEqual(h.spoken, [BUILT_IN.disconnected]);
  assert.equal(h.timers[0].ms, 1000);
  await h.timers[0].fn();
  assert.notEqual(FakeSocket.last, ws);
  assert.deepEqual(FakeSocket.last.protocols, ["voa.v1", "voa.bearer.tok-2"]);
  FakeSocket.last.serverSends({ type: "auth_ok", session_id: "sess_1", server_time_utc: "x" });
  assert.equal(h.controller.status().showStates, true);
});

test("watchdog: silence for watchdog_ms counts as disconnected", async () => {
  const h = harness();
  await ready(h);
  h.clock.now += 3000;
  h.controller.checkWatchdog();
  assert.equal(h.controller.state.connected, false);
  assert.deepEqual(h.spoken, [BUILT_IN.disconnected]);
  assert.equal(h.timers.length, 1);
});

test("three failed uploads show a server error; frame_busy is not a failure", async () => {
  let status = 409;
  const h = harness({ "POST /v1/sessions/sess_1/frames": () => (status === 409 ? [409, { code: "frame_busy" }] : [503, { code: "no_model_deployed" }]) });
  const ws = await ready(h);
  const beat = () => ws.serverSends({ type: "heartbeat", server_time_utc: "x", states: { fire_in_view: "clear" } });
  h.controller.startCapture();
  for (let i = 0; i < 3; i++) {
    h.clock.now += 600;
    beat();
    await h.controller.upload(FRAME(h.clock));
  }
  assert.equal(h.controller.state.uploadFailures, 0);
  assert.equal(h.controller.state.counters.skipped, 3);
  status = 503;
  for (let i = 0; i < 3; i++) {
    h.clock.now += 600;
    beat();
    await h.controller.upload(FRAME(h.clock));
  }
  assert.equal(h.controller.status().key, "server_error");
  assert.equal(h.controller.frames.size, 0);
});

test("login errors and signup pending", async () => {
  const pending = harness({ "POST /v1/sessions": [403, { code: "account_pending" }] });
  await pending.controller.login("kim", "x");
  assert.equal(pending.controller.state.screen, "login");
  assert.equal(pending.controller.state.error, message("account_pending"));
  const signup = harness({ "POST /v1/auth/signup": [201, { user_id: "usr_1", status: "pending" }] });
  await signup.controller.signup("kim", "pw-12345678", "김");
  assert.match(signup.controller.state.notice, /승인/);
  assert.deepEqual(JSON.parse(signup.calls[0].body), { username: "kim", password: "pw-12345678", display_name: "김" });
});

test("the page code never touches browser storage, cookies or the URL", () => {
  for (const file of ["core.js", "app.js"]) {
    const source = readFileSync(new URL(`../../src/voa/server/ui/sim/${file}`, import.meta.url), "utf8");
    for (const banned of ["localStorage", "sessionStorage", "indexedDB", "document.cookie", "history.", "innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function"]) {
      assert.ok(!source.includes(banned), `${file} uses ${banned}`);
    }
  }
});
