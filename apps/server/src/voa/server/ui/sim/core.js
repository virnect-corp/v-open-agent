// Sim page logic without the DOM (TASK-46, SPEC 7.1, 7.2, 7.4, 7.6; ADR-0009, ADR-0010).
// It plays the glasses app with the same API: login, session, control WebSocket,
// mission choice, frame upload (one in flight), HUD presentation, status bar, ACKs.
// Tokens live only in this module's memory. Frames are JPEG blobs made in the page;
// a video file is played locally and never uploaded.

// --- built-in device texts (SPEC 7.4: the device owns these, not the server) ---

export const BUILT_IN = {
  disconnected: "서버 연결이 끊겨 경고를 받을 수 없습니다.",
  capacity: "동시 접속 정원을 넘었습니다. 잠시 후 다시 시도하세요.",
};

export const MESSAGES = {
  unauthorized: "아이디나 비밀번호가 맞지 않습니다.",
  account_pending: "승인 대기 중입니다. 관리자가 승인하면 로그인할 수 있습니다.",
  too_many_attempts: "로그인 시도가 많아 5분 동안 막혔습니다.",
  password_change_required: "임시 비밀번호입니다. 관리자에게 문의해 비밀번호를 바꾸세요.",
  capacity_exceeded: "동시 접속 정원을 넘었습니다. 잠시 후 다시 시도하세요.",
  signup_disabled: "가입을 받지 않는 서버입니다.",
  username_taken: "이미 쓰는 아이디입니다.",
  invalid_request: "입력 형식이 맞지 않습니다.",
  mission_not_found: "미션을 찾을 수 없습니다.",
  not_ready: "서버가 아직 준비되지 않았습니다.",
  network: "서버에 연결할 수 없습니다.",
};

export function message(code) {
  return MESSAGES[code] ?? `요청이 실패했습니다 (${code}).`;
}

// --- HUD models (SPEC 7.4) ---

export const HUD_MODELS = {
  color_full: { label: "컬러, 전체 화면 깜빡임", hud: { color: true, flash_modes: ["none", "border", "full_screen"], max_lines: 2 } },
  mono_border: { label: "단색, 테두리 깜빡임", hud: { color: false, flash_modes: ["none", "border"], max_lines: 2 } },
  mono_plain: { label: "단색, 깜빡임 없음", hud: { color: false, flash_modes: ["none"], max_lines: 2 } },
};
const FLASH_ORDER = ["full_screen", "border", "none"];

/** Reduce a presentation to what the HUD supports (SPEC 7.4 rules). */
export function reducePresentation(presentation, hud = HUD_MODELS.mono_plain.hud) {
  const supported = new Set(hud.flash_modes ?? ["none"]);
  let mode = presentation.flash.mode;
  for (let i = FLASH_ORDER.indexOf(mode); i < FLASH_ORDER.length; i++) {
    mode = FLASH_ORDER[i];
    if (supported.has(mode) || mode === "none") break;
  }
  return {
    styleId: presentation.style_id,
    text: presentation.text,
    maxLines: hud.max_lines ?? 2,
    position: presentation.position,
    textSize: presentation.text_size,
    color: hud.color ? presentation.color : null, // mono: inverse emphasis instead
    inverse: !hud.color,
    flash: { ...presentation.flash, mode, count: mode === "none" ? 0 : presentation.flash.count },
    showMs: presentation.show_ms,
    speech: presentation.speech,
    haptic: presentation.haptic,
  };
}

// --- status bar (SPEC 7.4, upper row wins) ---

export const STATUS = [
  { key: "capacity", label: "정원 초과", tone: "gray" },
  { key: "disconnected", label: "연결 끊김", tone: "gray" },
  { key: "server_error", label: "서버 오류", tone: "red-border" },
  { key: "slow", label: "응답 지연", tone: "yellow" },
  { key: "warning", label: "경고", tone: "red" },
  { key: "unknown", label: "판단 불가", tone: "gray" },
  { key: "candidate", label: "확인 중", tone: "yellow-dot" },
  { key: "normal", label: "정상", tone: "green-dot" },
];
export const SYNC_TIMEOUT_MS = 1000; // VOA_FRAME_SYNC_TIMEOUT_MS default (SPEC 8.3)
export const FAILURES_FOR_ERROR = 3;

/**
 * input: {capacity, connected, lastServerMessageMs, watchdogMs, uploadFailures,
 *         uploading, lastResponseMs, frameIntervalMs, states, now}
 * returns one STATUS row plus `showStates` (hidden when the link itself is in doubt).
 */
export function statusBar(input) {
  const pick = (key, showStates) => ({ ...STATUS.find((s) => s.key === key), rank: STATUS.findIndex((s) => s.key === key), showStates });
  if (input.capacity) return pick("capacity", false);
  const silent = input.lastServerMessageMs !== null && input.now - input.lastServerMessageMs >= input.watchdogMs;
  if (!input.connected || silent) return pick("disconnected", false);
  if (input.uploadFailures >= FAILURES_FOR_ERROR) return pick("server_error", false);
  const waitLimit = 2 * input.frameIntervalMs + SYNC_TIMEOUT_MS;
  if (input.uploading && input.lastResponseMs !== null && input.now - input.lastResponseMs >= waitLimit) {
    return pick("slow", true);
  }
  const values = Object.values(input.states ?? {});
  if (values.includes("warning")) return pick("warning", true);
  if (values.length === 0 || values.includes("unknown")) return pick("unknown", true); // nothing received is not normal
  if (values.includes("candidate")) return pick("candidate", true);
  return pick("normal", true);
}

// --- session controller ---

const OUTPUTS = ["hud", "speaker"];
const RECONNECT_MS = [1000, 2000, 4000, 8000];

/**
 * deps: {fetch, WebSocket, now(): ms, wsUrl(path): string, onChange(state), speak(text),
 *        setTimer(fn, ms), clearTimer(id)}
 */
export class SimController {
  constructor(deps) {
    this.deps = deps;
    this.tokens = null; // {token, refresh_token}: memory only
    this.ws = null;
    this.reconnectTimer = null;
    this.reconnectAttempt = 0;
    this.played = new Set(); // alert_ids already rendered
    this.frames = new Map(); // frame_id -> {image, captureMs}: memory only, a few at most
    this.state = SimController.initial();
  }

  static initial() {
    return {
      screen: "login", // login | missions | glass
      error: null,
      notice: null,
      busy: false,
      hudModel: "color_full",
      sessionId: null,
      heartbeatMs: 1000,
      watchdogMs: 3000,
      frameIntervalMs: 333,
      missions: [],
      mission: null, // {mission_id, version, name, frame_interval_ms}
      missionConfirmed: false,
      connected: false,
      authOk: false,
      capacity: false,
      lastServerMessageMs: null,
      disconnectAnnounced: false,
      states: {},
      uploading: false,
      inFlight: null, // frame_id
      lastSentMs: null,
      lastResponseMs: null,
      uploadFailures: 0,
      counters: { sent: 0, skipped: 0, failed: 0 },
      timing: null,
      judged: null, // {frameId, image, captureMs, detections}
      hud: null, // reduced presentation being shown
      hudUntilMs: null,
      lastAlertSeq: 0,
      log: [], // ids only
    };
  }

  set(patch) {
    Object.assign(this.state, patch);
    this.deps.onChange(this.state);
  }

  log(entry) {
    this.state.log = [entry, ...this.state.log].slice(0, 30);
  }

  status() {
    return statusBar({ ...this.state, now: this.deps.now() });
  }

  async api(method, path, { json, bytes, headers = {} } = {}) {
    const all = { ...headers };
    if (this.tokens) all.Authorization = `Bearer ${this.tokens.token}`;
    let body;
    if (json !== undefined) {
      all["Content-Type"] = "application/json";
      body = JSON.stringify(json);
    } else if (bytes !== undefined) {
      all["Content-Type"] = "image/jpeg";
      body = bytes;
    }
    try {
      const response = await this.deps.fetch(path, { method, headers: all, body, credentials: "omit", cache: "no-store" });
      let data = null;
      try {
        data = await response.json();
      } catch {
        data = null;
      }
      return { ok: response.ok, status: response.status, data };
    } catch {
      return { ok: false, status: 0, data: { code: "network" } };
    }
  }

  // --- account and session ---

  async signup(username, password, displayName) {
    this.set({ busy: true, error: null, notice: null });
    const body = { username, password };
    if (displayName) body.display_name = displayName;
    const result = await this.api("POST", "/v1/auth/signup", { json: body });
    if (!result.ok) return this.set({ busy: false, error: message(result.data?.code ?? "network") });
    const pending = result.data.status === "pending";
    this.set({ busy: false, notice: pending ? "가입 신청이 접수되었습니다. 관리자 승인 후 로그인할 수 있습니다." : "가입했습니다. 로그인하세요." });
  }

  async login(username, password, deviceLabel) {
    this.set({ busy: true, error: null, notice: null, capacity: false });
    const json = {
      username,
      password,
      capabilities: { inputs: ["camera_0"], outputs: OUTPUTS, hud: HUD_MODELS[this.state.hudModel].hud },
    };
    if (deviceLabel) json.device_label = deviceLabel;
    const result = await this.api("POST", "/v1/sessions", { json });
    if (!result.ok) {
      const code = result.data?.code ?? "network";
      if (code === "capacity_exceeded") this.enterCapacity();
      return this.set({ busy: false, error: message(code) });
    }
    const s = result.data;
    this.tokens = { token: s.token, refresh_token: s.refresh_token };
    this.set({
      busy: false,
      sessionId: s.session_id,
      heartbeatMs: s.heartbeat_ms,
      watchdogMs: s.watchdog_ms,
      frameIntervalMs: s.frame_interval_ms,
      mission: s.mission,
      screen: "missions",
    });
    this.connect();
    await this.loadMissions();
  }

  enterCapacity() {
    if (!this.state.capacity) this.deps.speak(BUILT_IN.capacity);
    this.set({ capacity: true });
  }

  async loadMissions() {
    const result = await this.api("GET", "/v1/missions");
    if (result.ok) this.set({ missions: result.data.missions });
    else this.set({ error: message(result.data?.code ?? "network") });
  }

  /** The wearer picks a mission on the first screen; capture waits for this. */
  async chooseMission(missionId) {
    this.set({ busy: true, error: null });
    const result = await this.api("PUT", `/v1/sessions/${encodeURIComponent(this.state.sessionId)}/mission`, { json: { mission_id: missionId } });
    if (!result.ok) return this.set({ busy: false, error: message(result.data?.code ?? "network") });
    this.played.clear();
    this.set({
      busy: false,
      mission: result.data,
      frameIntervalMs: result.data.frame_interval_ms,
      missionConfirmed: true,
      states: {},
      screen: "glass",
    });
  }

  changeMission() {
    this.stopCapture();
    this.set({ missionConfirmed: false, screen: "missions" });
    return this.loadMissions();
  }

  // --- control WebSocket ---

  connect() {
    const protocols = ["voa.v1", `voa.bearer.${this.tokens.token}`]; // browsers cannot set headers
    const ws = new this.deps.WebSocket(this.deps.wsUrl(`/v1/streams/${encodeURIComponent(this.state.sessionId)}`), protocols);
    this.ws = ws;
    ws.onmessage = (event) => this.onMessage(JSON.parse(event.data));
    ws.onclose = (event) => this.onClose(ws, event.code);
    ws.onopen = () => {};
  }

  send(message_) {
    if (this.ws && this.state.connected) this.ws.send(JSON.stringify(message_));
  }

  onMessage(msg) {
    const now = this.deps.now();
    this.state.lastServerMessageMs = now;
    switch (msg.type) {
      case "auth_ok":
        this.reconnectAttempt = 0;
        this.set({ connected: true, authOk: true, disconnectAnnounced: false });
        this.send({ type: "alert_sync", last_alert_seq: this.state.lastAlertSeq });
        break;
      case "mission":
        this.set({ mission: { mission_id: msg.mission_id, version: msg.version, name: msg.name, frame_interval_ms: msg.frame_interval_ms }, frameIntervalMs: msg.frame_interval_ms });
        break;
      case "config":
        this.set({ frameIntervalMs: msg.frame_interval_ms });
        break;
      case "heartbeat":
        this.set({ states: msg.states });
        break;
      case "alert":
        this.onAlert(msg);
        break;
      case "notice":
        if (msg.presentation) this.show(msg.presentation, null);
        break;
      case "frame_result":
        this.onResult(msg, now);
        break;
      case "error":
        if (msg.code === "capacity_exceeded") this.enterCapacity();
        this.log(`error ${msg.code}`);
        this.deps.onChange(this.state);
        break;
      default:
        this.deps.onChange(this.state);
    }
  }

  onAlert(msg) {
    const id = msg.alert.alert_id;
    this.state.lastAlertSeq = Math.max(this.state.lastAlertSeq, msg.alert_seq);
    this.send({ type: "ack", alert_id: id, status: "received" });
    this.log(`alert ${id}`);
    if (this.played.has(id)) {
      this.send({ type: "ack", alert_id: id, status: "played" }); // already shown: ACK only
      return this.deps.onChange(this.state);
    }
    if (msg.presentation) this.show(msg.presentation, id);
  }

  /** Start HUD display; `played` is the display start (SPEC 7.4), not the end of flashing. */
  show(presentation, alertId) {
    const reduced = reducePresentation(presentation, HUD_MODELS[this.state.hudModel].hud);
    this.set({ hud: reduced, hudUntilMs: this.deps.now() + reduced.showMs });
    if (reduced.speech) this.deps.speak(reduced.text);
    if (alertId !== null) {
      this.played.add(alertId);
      this.send({ type: "ack", alert_id: alertId, status: "played" });
    }
  }

  onClose(ws, code) {
    if (ws !== this.ws) return;
    const wasConnected = this.state.connected;
    this.set({ connected: false, authOk: false });
    if (code === 4503) return this.enterCapacity();
    if (code === 4401 || code === 1008) return this.logout(message("unauthorized"));
    if (wasConnected && !this.state.disconnectAnnounced) {
      this.deps.speak(BUILT_IN.disconnected);
      this.set({ disconnectAnnounced: true });
    }
    this.scheduleReconnect();
  }

  /** Watchdog (SPEC 7.4): called by the page every tick. */
  checkWatchdog() {
    const s = this.state;
    if (!s.connected || s.lastServerMessageMs === null) return;
    if (this.deps.now() - s.lastServerMessageMs >= s.watchdogMs) {
      if (!s.disconnectAnnounced) this.deps.speak(BUILT_IN.disconnected);
      this.set({ connected: false, authOk: false, disconnectAnnounced: true });
      const ws = this.ws;
      this.ws = null;
      try {
        ws?.close();
      } catch {
        /* already closed */
      }
      this.scheduleReconnect();
    }
  }

  scheduleReconnect() {
    if (this.reconnectTimer !== null || this.tokens === null) return;
    const delay = RECONNECT_MS[Math.min(this.reconnectAttempt, RECONNECT_MS.length - 1)];
    this.reconnectAttempt += 1;
    this.reconnectTimer = this.deps.setTimer(async () => {
      this.reconnectTimer = null;
      const refreshed = await this.api("POST", `/v1/sessions/${encodeURIComponent(this.state.sessionId)}/refresh`, {
        json: { refresh_token: this.tokens.refresh_token },
      });
      if (refreshed.ok) this.tokens = { token: refreshed.data.token, refresh_token: refreshed.data.refresh_token };
      else if (refreshed.status === 401) return this.logout(message("unauthorized"));
      this.connect();
    }, delay);
  }

  logout(error = null) {
    this.stopCapture();
    if (this.reconnectTimer !== null) this.deps.clearTimer(this.reconnectTimer);
    this.reconnectTimer = null;
    const ws = this.ws;
    this.ws = null;
    try {
      ws?.close();
    } catch {
      /* ignore */
    }
    this.tokens = null;
    this.frames.clear();
    this.played.clear();
    const hudModel = this.state.hudModel;
    this.state = SimController.initial();
    this.set({ error, hudModel });
  }

  // --- capture and upload ---

  canCapture() {
    const s = this.state;
    return s.connected && s.authOk && s.missionConfirmed && !s.capacity;
  }

  startCapture() {
    if (!this.canCapture()) return false;
    this.set({ uploading: true, lastResponseMs: this.deps.now() });
    return true;
  }

  stopCapture() {
    this.set({ uploading: false, inFlight: null });
  }

  /** Whether a frame should be taken now: one in flight, never faster than the interval. */
  due() {
    const s = this.state;
    if (!s.uploading || !this.canCapture() || s.inFlight !== null) return false;
    return s.lastSentMs === null || this.deps.now() - s.lastSentMs >= s.frameIntervalMs;
  }

  /**
   * Send one captured frame: {bytes (JPEG, long side <= 640), image (for the judged-frame
   * window), captureMs}. Returns the response status.
   */
  async upload(frame) {
    if (!this.due()) return null;
    const s = this.state;
    const seq = s.counters.sent + s.counters.skipped + s.counters.failed + 1;
    const frameId = `f_${String(seq).padStart(6, "0")}`;
    this.frames.set(frameId, { image: frame.image, captureMs: frame.captureMs });
    this.set({ inFlight: frameId, lastSentMs: this.deps.now() });
    const result = await this.api("POST", `/v1/sessions/${encodeURIComponent(s.sessionId)}/frames`, {
      bytes: frame.bytes,
      headers: {
        "X-VOA-Stream-Id": "camera_0",
        "X-VOA-Seq": String(seq),
        "X-VOA-Frame-Id": frameId,
        "X-VOA-Capture-Ts-Ns": String(Math.round(frame.captureMs * 1e6)),
      },
    });
    const now = this.deps.now();
    this.state.inFlight = null;
    if (result.status === 200) {
      this.state.counters.sent += 1;
      this.set({ uploadFailures: 0, lastResponseMs: now });
      this.onResult(result.data, now);
    } else if (result.status === 202) {
      this.state.counters.sent += 1;
      this.set({ uploadFailures: 0, lastResponseMs: now }); // result follows as frame_result
    } else if (result.status === 409 && result.data?.code === "frame_busy") {
      this.state.counters.skipped += 1; // not an error (SPEC 7.4)
      this.frames.delete(frameId);
      this.set({ lastResponseMs: now });
    } else {
      this.state.counters.failed += 1;
      this.frames.delete(frameId);
      this.set({ uploadFailures: s.uploadFailures + 1 });
      if (result.status === 401) this.scheduleReconnect();
    }
    return result.status;
  }

  onResult(result, now) {
    this.state.lastResponseMs = now;
    const frame = this.frames.get(result.frame_id);
    const detections = (result.results ?? []).flatMap((r) => r.detections);
    this.frames.delete(result.frame_id);
    for (const id of [...this.frames.keys()]) if (id < result.frame_id) this.frames.delete(id); // older ones are stale
    this.set({
      states: result.states ?? this.state.states,
      timing: result.timing ? { ...result.timing, rttMs: frame ? now - frame.captureMs : null } : this.state.timing,
      judged: frame ? { frameId: result.frame_id, image: frame.image, captureMs: frame.captureMs, detections } : this.state.judged,
    });
  }

  /** Clear an expired HUD popup; called by the page every tick. */
  expireHud() {
    if (this.state.hud && this.deps.now() >= this.state.hudUntilMs) this.set({ hud: null, hudUntilMs: null });
  }
}
