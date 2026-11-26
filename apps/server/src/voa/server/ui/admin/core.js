// Admin page logic without the DOM (TASK-45, SPEC 7.7, ADR-0011).
// The login token lives only in this module's memory: never in the URL, storage or
// cookies. Reloading the page logs out. Server text is shown via textContent only.

export const MESSAGES = {
  unauthorized: "아이디나 비밀번호가 맞지 않거나 로그인이 끝났습니다.",
  too_many_attempts: "로그인 시도가 많아 5분 동안 막혔습니다.",
  account_pending: "승인 대기 중인 계정입니다.",
  password_change_required: "비밀번호를 먼저 바꿔야 합니다.",
  not_admin: "관리자 계정이 아닙니다.",
  invalid_request: "입력 형식이 맞지 않습니다.",
  not_found: "대상을 찾을 수 없습니다.",
  not_ready: "서버가 아직 준비되지 않았습니다.",
  mission_not_found: "미션을 찾을 수 없습니다.",
  mission_exists: "같은 ID의 미션이 이미 있습니다.",
  mission_in_use: "이 미션으로 동작 중인 세션이 있어 보관할 수 없습니다.",
  draft_missing: "저장된 편집본이 없습니다. 먼저 저장하세요.",
  profile_invalid: "판정 규칙 형식이 맞지 않습니다. 값과 필수 항목을 확인하세요.",
  profile_model_mismatch: "근거 모델이 등록된 모델과 맞지 않습니다(종류나 labels).",
  model_not_ready: "근거 모델의 가중치가 없거나 sha256이 맞지 않습니다.",
  model_not_found: "모델을 찾을 수 없습니다.",
  model_not_servable: "이 모델은 서버에서 실행할 수 없습니다(추적 모델).",
  model_not_deployed: "배치되지 않은 모델입니다.",
  model_in_use: "게시된 미션이나 진행 중인 세션이 쓰는 모델이라 해제할 수 없습니다.",
  device_unavailable: "GPU 배치는 아직 지원하지 않습니다.",
  no_model_deployed: "모델을 먼저 배치하세요.",
  frame_rejected: "JPEG(긴 변 640px 이하, 크기 제한 이내)만 시험할 수 있습니다.",
  settings_invalid: "저장된 설정 파일이 손상되었습니다. 저장하면 새 설정으로 바뀝니다.",
  style_not_found: "스타일 연결이 없는 스타일을 가리킵니다.",
  json_invalid: "JSON 형식이 맞지 않습니다.",
  network: "서버에 연결할 수 없습니다.",
};

export function message(code) {
  return MESSAGES[code] ?? `요청이 실패했습니다 (${code}).`;
}

export const TABS = ["missions", "users", "models", "settings", "capacity", "changes"];

// --- API client ---

export function createApi(fetchImpl) {
  let token = null;
  return {
    setToken(value) {
      token = value;
    },
    clearToken() {
      token = null;
    },
    hasToken() {
      return token !== null;
    },
    async call(method, path, { json, bytes, contentType } = {}) {
      const headers = {};
      if (token !== null) headers.Authorization = `Bearer ${token}`;
      let body;
      if (json !== undefined) {
        headers["Content-Type"] = "application/json";
        body = JSON.stringify(json);
      } else if (bytes !== undefined) {
        headers["Content-Type"] = contentType ?? "application/octet-stream";
        body = bytes;
      }
      let response;
      try {
        response = await fetchImpl(path, { method, headers, body, credentials: "omit", cache: "no-store" });
      } catch {
        return { ok: false, status: 0, data: { code: "network" } };
      }
      let data = null;
      try {
        data = await response.json();
      } catch {
        data = null;
      }
      return { ok: response.ok, status: response.status, data };
    },
  };
}

// --- mission editor (pure helpers on a plain object) ---

const TEMPLATE_PARAMS = {
  min_score: 0.5,
  roi_min_overlap: 0.3,
  confirm_frames: 3,
  clear_frames: 3,
  staleness_ms: 1000,
  unknown_after_ms: 1500,
  warning_ttl_ms: 4000,
  window_ms: 5000,
  window_max_items: 256,
};

export function templateScenario(id, modelId) {
  return {
    id,
    required_kinds: ["object_detection"],
    required_models: { object_detection: modelId },
    required_streams: ["camera_0"],
    labels: [],
    motion_classes: [],
    roi: [0.0, 0.0, 1.0, 1.0],
    blocking_flags: ["occluded", "frame_dropped"],
    warning_text: "위험 요소가 감지되었습니다. 주변을 확인하세요.",
    severity: "attention",
  };
}

export function newEditor(detail, modelIds = []) {
  const source = detail.draft ?? detail.published ?? null;
  const profile = source
    ? structuredClone(source.profile)
    : {
        profile_id: detail.mission_id,
        degraded_notice_text: "카메라 상태를 확인할 수 없어 감지가 제한됩니다.",
        params: { ...TEMPLATE_PARAMS },
        scenarios: [templateScenario(`${detail.mission_id}_rule`, modelIds[0] ?? "")],
      };
  delete profile.profile_version; // the server sets it on publish
  const settings = source ? structuredClone(source.settings) : { frame_interval_ms: null, hud_style_map: {} };
  return {
    mission_id: detail.mission_id,
    name: detail.name,
    description: detail.description,
    latest_version: detail.latest_version,
    status: detail.status,
    fromDraft: Boolean(detail.draft),
    profile,
    settings,
    dirty: false,
    result: null, // {ok, text}
  };
}

const NUMBER_PARAMS = new Set(Object.keys(TEMPLATE_PARAMS));

export function setParam(editor, key, value) {
  if (!NUMBER_PARAMS.has(key)) throw new Error(`unknown param ${key}`);
  editor.profile.params[key] = Number(value);
  editor.dirty = true;
}

export function setScenario(editor, index, key, value) {
  const scenario = editor.profile.scenarios[index];
  if (key === "model") scenario.required_models.object_detection = value;
  else if (key === "labels") scenario.labels = splitList(value);
  else if (key === "roi") scenario.roi = splitList(value).map(Number);
  else if (["id", "severity", "warning_text"].includes(key)) scenario[key] = value;
  else throw new Error(`unknown scenario field ${key}`);
  editor.dirty = true;
}

export function addScenario(editor, modelIds = []) {
  const n = editor.profile.scenarios.length + 1;
  editor.profile.scenarios.push(templateScenario(`${editor.mission_id}_rule${n}`, modelIds[0] ?? ""));
  editor.dirty = true;
}

export function removeScenario(editor, index) {
  if (editor.profile.scenarios.length <= 1) return false; // a mission needs one rule
  editor.profile.scenarios.splice(index, 1);
  editor.dirty = true;
  return true;
}

export function setSetting(editor, key, value) {
  if (key === "frame_interval_ms") {
    editor.settings.frame_interval_ms = value === "" || value === null ? null : Number(value);
  } else if (key === "attention" || key === "degraded") {
    if (value) editor.settings.hud_style_map[key] = value;
    else delete editor.settings.hud_style_map[key];
  } else {
    throw new Error(`unknown setting ${key}`);
  }
  editor.dirty = true;
}

export function editorJson(editor) {
  return JSON.stringify(editor.profile, null, 2);
}

export function applyJson(editor, text) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    return "json_invalid";
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return "json_invalid";
  delete parsed.profile_version;
  editor.profile = parsed;
  editor.dirty = true;
  return null;
}

function splitList(value) {
  return String(value)
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

// --- controller ---

export class AdminController {
  constructor(api, onChange = () => {}) {
    this.api = api;
    this.onChange = onChange;
    this.state = AdminController.initial();
    this.pendingPassword = null;
  }

  static initial() {
    return {
      screen: "login", // login | password | main
      tab: "missions",
      busy: false,
      error: null, // text shown in the current screen
      notice: null, // success text
      username: null,
      missions: [],
      editor: null,
      users: [],
      models: [],
      settings: null,
      capacity: null,
      changes: [],
      detect: null,
    };
  }

  set(patch) {
    Object.assign(this.state, patch);
    this.onChange(this.state);
  }

  fail(code) {
    this.set({ busy: false, error: message(code), notice: null });
  }

  /** Common handling: 401 logs out, 403 password change, else the error text. */
  async request(method, path, options) {
    const result = await this.api.call(method, path, options);
    if (result.status === 401 && this.state.screen !== "login") {
      this.logout(message("unauthorized"));
      return null;
    }
    if (result.status === 403 && result.data?.code === "password_change_required") {
      this.set({ screen: "password", busy: false, error: message("password_change_required") });
      return null;
    }
    if (!result.ok) {
      this.fail(result.data?.code ?? `http_${result.status}`);
      return null;
    }
    return result.data ?? {};
  }

  // --- account ---

  async login(username, password) {
    this.set({ busy: true, error: null, notice: null });
    const result = await this.api.call("POST", "/v1/auth/login", { json: { username, password } });
    if (!result.ok) return this.fail(result.data?.code ?? "network");
    if (result.data.role !== "admin") return this.fail("not_admin");
    this.api.setToken(result.data.token);
    if (result.data.password_change_required) {
      this.pendingPassword = password; // memory only, for the change request; never rendered
      return this.set({ screen: "password", busy: false, username });
    }
    this.set({ screen: "main", busy: false, username });
    return this.show("missions");
  }

  async changePassword(nextPassword) {
    this.set({ busy: true, error: null });
    const current = this.pendingPassword ?? "";
    const done = await this.request("POST", "/v1/auth/password", {
      json: { current_password: current, new_password: nextPassword },
    });
    if (done === null) return;
    this.pendingPassword = null;
    this.set({ screen: "main", busy: false, notice: "비밀번호를 바꿨습니다." });
    return this.show("missions");
  }

  logout(error = null) {
    this.api.clearToken();
    this.pendingPassword = null;
    this.state = AdminController.initial();
    this.set({ error });
  }

  // --- tabs ---

  async show(tab) {
    if (!TABS.includes(tab)) return;
    this.set({ tab, busy: true, error: null, editor: tab === "missions" ? this.state.editor : null });
    const loaders = {
      missions: () => this.loadMissions(),
      users: async () => {
        const data = await this.request("GET", "/v1/admin/users");
        if (data) this.set({ users: data.users });
      },
      models: () => this.loadModels(),
      settings: async () => {
        const data = await this.request("GET", "/v1/admin/settings");
        if (data) this.set({ settings: data });
      },
      capacity: async () => {
        const data = await this.request("GET", "/v1/admin/capacity");
        if (data) this.set({ capacity: data });
      },
      changes: async () => {
        const data = await this.request("GET", "/v1/admin/changes");
        if (data) this.set({ changes: data.changes });
      },
    };
    await loaders[tab]();
    if (this.state.busy) this.set({ busy: false });
  }

  async loadMissions() {
    const data = await this.request("GET", "/v1/admin/missions");
    if (data) this.set({ missions: data.missions });
  }

  async loadModels() {
    const data = await this.request("GET", "/v1/admin/models");
    if (data) this.set({ models: data.models });
  }

  // --- missions ---

  async createMission(fields) {
    this.set({ busy: true, error: null, notice: null });
    const body = { mission_id: fields.mission_id, name: fields.name, description: fields.description ?? "" };
    if (fields.from_mission_id) body.from_mission_id = fields.from_mission_id;
    const created = await this.request("POST", "/v1/admin/missions", { json: body });
    if (created === null) return;
    await this.loadMissions();
    return this.openMission(created.mission_id);
  }

  async archiveMission(missionId) {
    this.set({ busy: true, error: null, notice: null });
    const done = await this.request("DELETE", `/v1/admin/missions/${encodeURIComponent(missionId)}`);
    if (done === null) return;
    if (this.state.editor?.mission_id === missionId) this.set({ editor: null });
    await this.loadMissions();
    this.set({ busy: false, notice: `${missionId} 미션을 보관했습니다.` });
  }

  async openMission(missionId) {
    this.set({ busy: true, error: null });
    const detail = await this.request("GET", `/v1/admin/missions/${encodeURIComponent(missionId)}`);
    if (detail === null) return;
    if (this.state.models.length === 0) await this.loadModels();
    this.set({ busy: false, editor: newEditor(detail, this.detectionModels()) });
  }

  detectionModels() {
    return this.state.models.filter((m) => m.kind === "object_detection").map((m) => m.model_id);
  }

  closeEditor() {
    this.set({ editor: null, error: null });
  }

  touch() {
    this.onChange(this.state);
  }

  async saveDraft() {
    const editor = this.state.editor;
    this.set({ busy: true, error: null, notice: null });
    const saved = await this.request("PUT", `/v1/admin/missions/${encodeURIComponent(editor.mission_id)}`, {
      json: { name: editor.name, description: editor.description, profile: editor.profile, settings: editor.settings },
    });
    if (saved === null) {
      editor.result = { ok: false, text: this.state.error };
      return false;
    }
    editor.dirty = false;
    editor.fromDraft = true;
    this.set({ busy: false });
    return true;
  }

  async validate() {
    const editor = this.state.editor;
    if (editor.dirty && !(await this.saveDraft())) return;
    const id = encodeURIComponent(editor.mission_id);
    const result = await this.api.call("POST", `/v1/admin/missions/${id}:validate`);
    if (result.status === 401) return this.logout(message("unauthorized"));
    editor.result = result.ok
      ? { ok: true, text: `검증 통과: 스키마와 모델 대조. 게시하면 v${result.data.next_version}이 됩니다.` }
      : { ok: false, text: message(result.data?.code ?? "network") };
    this.set({ busy: false });
  }

  async publish() {
    const editor = this.state.editor;
    if (editor.dirty && !(await this.saveDraft())) return;
    this.set({ busy: true });
    const id = encodeURIComponent(editor.mission_id);
    const result = await this.api.call("POST", `/v1/admin/missions/${id}:publish`);
    if (result.status === 401) return this.logout(message("unauthorized"));
    if (!result.ok) {
      editor.result = { ok: false, text: message(result.data?.code ?? "network") };
      return this.set({ busy: false });
    }
    await this.loadMissions();
    this.set({
      busy: false,
      editor: null,
      notice: `${editor.mission_id} v${result.data.version}을 게시했습니다. 새로 시작하는 세션부터 쓰입니다.`,
    });
  }

  // --- users ---

  async userAction(userId, action) {
    this.set({ busy: true, error: null, notice: null });
    const data = await this.request("POST", `/v1/admin/users/${encodeURIComponent(userId)}:${action}`);
    if (data === null) return;
    const notice =
      action === "reset-password"
        ? `임시 비밀번호: ${data.temporary_password} (한 번만 표시됩니다. 다음 로그인 때 바꿔야 합니다.)`
        : action === "approve"
          ? "승인했습니다."
          : "사용을 중지했습니다. 진행 중인 세션도 끝났습니다.";
    const list = await this.request("GET", "/v1/admin/users");
    this.set({ busy: false, notice, users: list ? list.users : this.state.users });
  }

  // --- models ---

  async deploy(modelId, scoreFloor) {
    this.set({ busy: true, error: null, notice: null });
    const done = await this.request("POST", "/v1/admin/deployments", {
      json: { model_id: modelId, device: "cpu", score_floor: Number(scoreFloor) },
    });
    if (done === null) return;
    await this.loadModels();
    this.set({ busy: false, notice: `${modelId}를 배치했습니다.` });
  }

  async undeploy(modelId) {
    this.set({ busy: true, error: null, notice: null });
    const done = await this.request("DELETE", `/v1/admin/deployments/${encodeURIComponent(modelId)}`);
    if (done === null) return;
    await this.loadModels();
    this.set({ busy: false, notice: `${modelId} 배치를 해제했습니다.` });
  }

  async detect(modelId, bytes) {
    this.set({ busy: true, error: null, detect: null });
    const data = await this.request("POST", `/v1/admin/detect?model_id=${encodeURIComponent(modelId)}`, {
      bytes,
      contentType: "image/jpeg",
    });
    if (data === null) return;
    this.set({ busy: false, detect: data });
  }

  // --- settings ---

  async saveSettings(patch) {
    this.set({ busy: true, error: null, notice: null });
    const saved = await this.request("PUT", "/v1/admin/settings", { json: patch });
    if (saved === null) return;
    const data = await this.request("GET", "/v1/admin/settings");
    this.set({
      busy: false,
      settings: data ?? this.state.settings,
      notice: "저장했습니다. 프레임 간격은 진행 중인 세션에 바로, HUD 스타일은 다음 경고부터 적용됩니다.",
    });
  }
}
