// Admin page rendering (TASK-45). All logic is in core.js; this file only draws state
// and forwards user input. Text from the server is set with textContent (no HTML).
import {
  AdminController,
  TABS,
  addScenario,
  applyJson,
  createApi,
  editorJson,
  message,
  removeScenario,
  setParam,
  setScenario,
  setSetting,
} from "./core.js";

const TAB_NAMES = {
  missions: "미션",
  users: "착용자",
  models: "모델 배치",
  settings: "프레임 간격과 HUD",
  capacity: "용량",
  changes: "변경 기록",
};
const STATUS_NAMES = { draft: "편집 중", published: "게시됨", archived: "보관됨" };
const WEIGHT_NAMES = { ready: "준비됨", missing: "없음", sha256_mismatch: "sha256 불일치" };
const USER_STATUS = { pending: "승인 대기", active: "사용 중", disabled: "사용 중지" };

const root = document.getElementById("app");
const controller = new AdminController(createApi(window.fetch.bind(window)), render);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else if (key === "value") el.value = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function field(label, input) {
  return h("label", { class: "field" }, h("span", {}, label), input);
}

function banner(state) {
  return [
    state.error ? h("p", { class: "banner error", role: "alert" }, state.error) : null,
    state.notice ? h("p", { class: "banner ok", role: "status" }, state.notice) : null,
  ];
}

function render(state) {
  const view =
    state.screen === "login" ? loginView(state) : state.screen === "password" ? passwordView(state) : mainView(state);
  root.replaceChildren(view);
}

// --- login and first password change ---

function loginView(state) {
  const user = h("input", { name: "username", autocomplete: "username", required: true });
  const pass = h("input", { name: "password", type: "password", autocomplete: "current-password", required: true });
  const submit = (event) => {
    event.preventDefault();
    controller.login(user.value.trim(), pass.value);
  };
  return h(
    "main",
    { class: "card narrow" },
    h("h1", {}, "VOA 관리자"),
    h("p", { class: "muted" }, "관리자 계정으로 로그인합니다. 새로고침하면 로그아웃됩니다."),
    ...banner(state),
    h(
      "form",
      { onsubmit: submit },
      field("아이디", user),
      field("비밀번호", pass),
      h("button", { type: "submit", disabled: state.busy }, "관리자 로그인"),
    ),
  );
}

function passwordView(state) {
  const next = h("input", { type: "password", minlength: 8, autocomplete: "new-password", required: true });
  const again = h("input", { type: "password", minlength: 8, autocomplete: "new-password", required: true });
  const submit = (event) => {
    event.preventDefault();
    if (next.value !== again.value) {
      controller.set({ error: "새 비밀번호가 서로 다릅니다." });
      return;
    }
    controller.changePassword(next.value);
  };
  return h(
    "main",
    { class: "card narrow" },
    h("h1", {}, "첫 로그인: 비밀번호를 바꿔야 합니다"),
    h(
      "p",
      { class: "muted" },
      "초기 비밀번호(admin)는 이 서버 컴퓨터(127.0.0.1)에서만 쓸 수 있습니다. 바꾸기 전에는 관리 기능이 잠깁니다.",
    ),
    ...banner(state),
    h(
      "form",
      { onsubmit: submit },
      field("새 비밀번호 (8자 이상)", next),
      field("새 비밀번호 확인", again),
      h("button", { type: "submit", disabled: state.busy }, "비밀번호 변경"),
    ),
  );
}

// --- main ---

function mainView(state) {
  const nav = h(
    "nav",
    { class: "tabs", "aria-label": "관리 메뉴" },
    TABS.map((tab) =>
      h(
        "button",
        { class: tab === state.tab ? "tab active" : "tab", "aria-current": tab === state.tab ? "page" : null, onclick: () => controller.show(tab) },
        TAB_NAMES[tab],
      ),
    ),
  );
  const views = {
    missions: missionsView,
    users: usersView,
    models: modelsView,
    settings: settingsView,
    capacity: capacityView,
    changes: changesView,
  };
  return h(
    "div",
    { class: "shell" },
    h(
      "header",
      { class: "top" },
      h("strong", {}, "VOA 관리자"),
      h("span", { class: "muted" }, `${state.username ?? ""} 로그인`),
      h("button", { class: "link", onclick: () => controller.logout() }, "로그아웃"),
    ),
    nav,
    h("main", { class: "content", "aria-busy": state.busy ? "true" : "false" }, ...banner(state), views[state.tab](state)),
  );
}

// --- missions ---

function missionsView(state) {
  if (state.editor) return editorView(state);
  const id = h("input", { pattern: "[a-z0-9_-]{2,64}", required: true, placeholder: "fire_floor2" });
  const name = h("input", { required: true, maxlength: 80 });
  const description = h("input", { maxlength: 500 });
  const from = h("select", {}, h("option", { value: "" }, "빈 미션"), state.missions.map((m) => h("option", { value: m.mission_id }, `${m.name} 복제`)));
  const create = (event) => {
    event.preventDefault();
    controller.createMission({ mission_id: id.value.trim(), name: name.value.trim(), description: description.value, from_mission_id: from.value });
  };
  return h(
    "section",
    {},
    h("h2", {}, "미션"),
    h("p", { class: "muted" }, "착용자가 고르는 목적 단위입니다. 편집하고 검증한 뒤 게시하면 새로 시작하는 세션부터 쓰입니다."),
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, ["미션", "상태", "버전", "세션", ""].map((t) => h("th", {}, t)))),
      h(
        "tbody",
        {},
        state.missions.map((m) =>
          h(
            "tr",
            {},
            h("td", {}, h("strong", {}, m.name), h("div", { class: "muted" }, `${m.mission_id} ${m.description}`)),
            h("td", {}, h("span", { class: `badge ${m.status}` }, STATUS_NAMES[m.status] ?? m.status), m.has_draft ? h("span", { class: "badge draft" }, "편집본 있음") : null),
            h("td", {}, m.latest_version ? `v${m.latest_version}` : "-"),
            h("td", {}, String(m.sessions)),
            h(
              "td",
              { class: "actions" },
              h("button", { onclick: () => controller.openMission(m.mission_id) }, "편집"),
              h("button", { onclick: () => controller.createMission({ mission_id: `${m.mission_id}_copy`, name: `${m.name} 복사본`, from_mission_id: m.mission_id }) }, "복제"),
              m.status !== "archived" ? h("button", { class: "danger", onclick: () => controller.archiveMission(m.mission_id) }, "삭제(보관)") : null,
            ),
          ),
        ),
      ),
    ),
    h("h3", {}, "새 미션"),
    h("form", { class: "row", onsubmit: create }, field("ID", id), field("이름", name), field("설명", description), field("시작", from), h("button", { type: "submit", disabled: state.busy }, "만들기")),
  );
}

function editorView(state) {
  const editor = state.editor;
  const models = controller.detectionModels();
  const styles = state.settings?.styles ? Object.keys(state.settings.styles) : ["attention_default", "degraded_default"];
  const p = editor.profile;
  const changed = () => controller.touch();
  const number = (key, step) =>
    field(key, h("input", { type: "number", step, value: p.params?.[key], onchange: (e) => { setParam(editor, key, e.target.value); changed(); } }));
  const json = h("textarea", { rows: 14, spellcheck: "false", class: "mono" }, editorJson(editor));
  const nextVersion = editor.latest_version + 1;
  return h(
    "section",
    {},
    h("div", { class: "row between" }, h("h2", {}, `${editor.name} 편집`), h("button", { class: "link", onclick: () => controller.closeEditor() }, "목록으로")),
    h("p", { class: "muted" }, `${editor.fromDraft ? "편집본" : "게시본에서 시작"}. 게시하면 v${nextVersion}이 되고, 이미 동작 중인 세션은 이전 버전으로 끝까지 판단합니다.`),
    h(
      "div",
      { class: "row" },
      field("이름", h("input", { value: editor.name, onchange: (e) => { editor.name = e.target.value; editor.dirty = true; changed(); } })),
      field("설명", h("input", { value: editor.description, onchange: (e) => { editor.description = e.target.value; editor.dirty = true; changed(); } })),
    ),
    h("h3", {}, "판정 규칙"),
    (p.scenarios ?? []).map((s, i) =>
      h(
        "fieldset",
        { class: "rule" },
        h("legend", {}, s.id),
        h(
          "div",
          { class: "row" },
          field("ID", h("input", { value: s.id, onchange: (e) => { setScenario(editor, i, "id", e.target.value); changed(); } })),
          field("등급", h("select", { onchange: (e) => { setScenario(editor, i, "severity", e.target.value); changed(); } }, ["attention", "notice"].map((v) => h("option", { value: v, selected: s.severity === v }, v)))),
          field("근거 모델", h("select", { onchange: (e) => { setScenario(editor, i, "model", e.target.value); changed(); } }, h("option", { value: "" }, "선택"), models.map((m) => h("option", { value: m, selected: s.required_models?.object_detection === m }, m)))),
          field("labels (쉼표)", h("input", { value: (s.labels ?? []).join(", "), onchange: (e) => { setScenario(editor, i, "labels", e.target.value); changed(); } })),
          field("ROI x1, y1, x2, y2", h("input", { value: (s.roi ?? []).join(", "), onchange: (e) => { setScenario(editor, i, "roi", e.target.value); changed(); } })),
        ),
        field("경고 문구", h("input", { class: "wide", value: s.warning_text, onchange: (e) => { setScenario(editor, i, "warning_text", e.target.value); changed(); } })),
        h("button", { class: "link danger", onclick: () => { if (!removeScenario(editor, i)) controller.set({ error: "판정 규칙은 하나 이상 있어야 합니다." }); else changed(); } }, "이 규칙 빼기"),
      ),
    ),
    h("button", { onclick: () => { addScenario(editor, models); changed(); } }, "판정 규칙 추가"),
    h("h3", {}, "임계값"),
    h("div", { class: "row" }, number("min_score", "0.01"), number("confirm_frames", "1"), number("clear_frames", "1"), number("staleness_ms", "100"), number("unknown_after_ms", "100"), number("warning_ttl_ms", "100")),
    h("h3", {}, "HUD와 프레임 간격"),
    h(
      "div",
      { class: "row" },
      field("경고(attention) 스타일", styleSelect(styles, editor.settings.hud_style_map.attention, (v) => { setSetting(editor, "attention", v); changed(); })),
      field("판단 불가(degraded) 스타일", styleSelect(styles, editor.settings.hud_style_map.degraded, (v) => { setSetting(editor, "degraded", v); changed(); })),
      field("권장 프레임 간격 ms (비우면 서버 값)", h("input", { type: "number", min: 100, max: 5000, value: editor.settings.frame_interval_ms ?? "", onchange: (e) => { setSetting(editor, "frame_interval_ms", e.target.value); changed(); } })),
    ),
    field("판단 불가 안내 문구", h("input", { class: "wide", value: p.degraded_notice_text, onchange: (e) => { p.degraded_notice_text = e.target.value; editor.dirty = true; changed(); } })),
    h(
      "details",
      {},
      h("summary", {}, "고급: 전체 판정 규칙 (JSON)"),
      json,
      h("button", { onclick: () => { const code = applyJson(editor, json.value); if (code) controller.set({ error: message(code) }); else changed(); } }, "JSON 적용"),
    ),
    editor.result ? h("p", { class: editor.result.ok ? "banner ok" : "banner error", role: "status" }, editor.result.text) : null,
    h(
      "div",
      { class: "row" },
      h("button", { disabled: state.busy, onclick: () => controller.saveDraft() }, editor.dirty ? "저장 *" : "저장"),
      h("button", { disabled: state.busy, onclick: () => controller.validate() }, "검증"),
      h("button", { class: "primary", disabled: state.busy, onclick: () => controller.publish() }, `게시 (v${nextVersion})`),
    ),
    h("p", { class: "muted" }, "게시하면 근거 모델을 자동으로 배치하고 버전이 올라갑니다."),
  );
}

function styleSelect(styles, current, onchange) {
  return h(
    "select",
    { onchange: (e) => onchange(e.target.value) },
    h("option", { value: "" }, "서버 설정 따름"),
    styles.map((s) => h("option", { value: s, selected: current === s }, s)),
  );
}

// --- users ---

function usersView(state) {
  return h(
    "section",
    {},
    h("h2", {}, "착용자"),
    h("p", { class: "muted" }, "승인 대기 계정이 먼저 보입니다. 사용을 중지하면 진행 중인 세션도 끝납니다."),
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, ["아이디", "표시 이름", "상태", ""].map((t) => h("th", {}, t)))),
      h(
        "tbody",
        {},
        state.users
          .filter((u) => u.role === "wearer")
          .map((u) =>
            h(
              "tr",
              {},
              h("td", {}, u.username),
              h("td", {}, u.display_name ?? ""),
              h("td", {}, h("span", { class: `badge ${u.status}` }, USER_STATUS[u.status] ?? u.status)),
              h(
                "td",
                { class: "actions" },
                u.status !== "active" ? h("button", { onclick: () => controller.userAction(u.user_id, "approve") }, "승인") : null,
                u.status !== "disabled" ? h("button", { class: "danger", onclick: () => controller.userAction(u.user_id, "disable") }, "사용 중지") : null,
                h("button", { onclick: () => controller.userAction(u.user_id, "reset-password") }, "임시 비밀번호"),
              ),
            ),
          ),
      ),
    ),
  );
}

// --- models and one-image detect ---

function modelsView(state) {
  const deployed = state.models.filter((m) => m.deployed && m.kind === "object_detection");
  const fileInput = h("input", { type: "file", accept: "image/jpeg" });
  const modelPick = h("select", {}, deployed.map((m) => h("option", { value: m.model_id }, m.model_id)));
  const runDetect = async (event) => {
    event.preventDefault();
    const file = fileInput.files?.[0];
    if (!file) return controller.set({ error: "JPEG 파일을 고르세요." });
    controller.detect(modelPick.value, await file.arrayBuffer());
  };
  return h(
    "section",
    {},
    h("h2", {}, "모델 배치"),
    h("p", { class: "muted" }, "여러 모델을 동시에 배치할 수 있습니다. 가중치는 로컬 파일만 읽고 sha256을 확인합니다."),
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, ["모델", "labels", "가중치", "배치", "쓰는 미션", ""].map((t) => h("th", {}, t)))),
      h(
        "tbody",
        {},
        state.models.map((m) => {
          const floor = h("input", { type: "number", min: 0, max: 1, step: "0.01", value: m.score_floor ?? 0.25, class: "short", "aria-label": `${m.model_id} 하한` });
          return h(
            "tr",
            {},
            h("td", {}, h("strong", {}, m.model_id), h("div", { class: "muted" }, `${m.kind} ${m.revision}`)),
            h("td", {}, m.labels.join(", ")),
            h("td", {}, h("span", { class: `badge ${m.weights}` }, WEIGHT_NAMES[m.weights] ?? m.weights)),
            h("td", {}, m.deployed ? `${m.device}, 하한 ${m.score_floor}` : "배치 안 됨"),
            h("td", {}, m.used_by.join(", ") || "-"),
            h(
              "td",
              { class: "actions" },
              m.kind === "object_detection" ? floor : null,
              m.kind === "object_detection" ? h("button", { onclick: () => controller.deploy(m.model_id, floor.value) }, m.deployed ? "다시 배치" : "배치") : null,
              m.deployed ? h("button", { class: "danger", onclick: () => controller.undeploy(m.model_id) }, "해제") : null,
            ),
          );
        }),
      ),
    ),
    h("h3", {}, "이미지 한 장 시험"),
    h("p", { class: "muted" }, "엔진을 거치지 않고 탐지 결과만 봅니다. 이미지는 저장하지 않습니다."),
    h("form", { class: "row", onsubmit: runDetect }, field("모델", modelPick), field("JPEG", fileInput), h("button", { type: "submit", disabled: state.busy || deployed.length === 0 }, "시험")),
    state.detect
      ? h(
          "div",
          { class: "result" },
          h("p", {}, `${state.detect.model_id}, ${state.detect.width}x${state.detect.height}, ${state.detect.provider_ms} ms, 검출 ${state.detect.detections.length}개`),
          h("ul", {}, state.detect.detections.map((d) => h("li", { class: "mono" }, `${d.label} ${Number(d.score).toFixed(3)} [${d.bbox.map((v) => Number(v).toFixed(2)).join(", ")}]`))),
        )
      : null,
  );
}

// --- settings ---

function settingsView(state) {
  const s = state.settings;
  if (!s) return h("section", {}, h("h2", {}, "프레임 간격과 HUD"), h("p", { class: "muted" }, "설정을 불러오지 못했습니다. 저장하면 새 설정으로 바뀝니다."), settingsForm(state, { frame_interval_ms: 333, hud_style_map: {}, styles: {} }));
  return h("section", {}, h("h2", {}, "프레임 간격과 HUD"), h("p", { class: "muted" }, "판단 규칙이 아니라 표시와 전송 설정이라 진행 중인 세션에도 바로 적용됩니다. 미션에 자체 값이 있으면 미션 값이 우선합니다."), settingsForm(state, s));
}

function settingsForm(state, s) {
  const styles = Object.keys(s.styles ?? {});
  const interval = h("input", { type: "number", min: 100, max: 5000, value: s.frame_interval_ms, required: true });
  const attention = styleSelect(styles, s.hud_style_map?.attention, () => {});
  const degraded = styleSelect(styles, s.hud_style_map?.degraded, () => {});
  const save = (event) => {
    event.preventDefault();
    const map = { ...(s.hud_style_map ?? {}) };
    for (const [key, el] of [["attention", attention], ["degraded", degraded]]) {
      if (el.value) map[key] = el.value;
      else delete map[key];
    }
    controller.saveSettings({ frame_interval_ms: Number(interval.value), hud_style_map: map });
  };
  return h(
    "div",
    {},
    h("form", { class: "row", onsubmit: save }, field("프레임 간격 (100~5000 ms)", interval), field("경고(attention) 스타일", attention), field("판단 불가(degraded) 스타일", degraded), h("button", { type: "submit", disabled: state.busy }, "저장")),
    h("h3", {}, "HUD 스타일"),
    h(
      "ul",
      { class: "styles" },
      Object.entries(s.styles ?? {}).map(([id, st]) =>
        h("li", {}, h("strong", {}, id), ` ${st.color}, ${st.position}, ${st.text_size}, ${st.flash.mode} ${st.flash.count}회, ${st.show_ms} ms, 음성 ${st.speech ? "켬" : "끔"}, 진동 ${st.haptic}`),
      ),
    ),
  );
}

// --- capacity and changes ---

function capacityView(state) {
  const c = state.capacity;
  if (!c) return h("section", {}, h("h2", {}, "용량"));
  const per = Object.entries(c.inferences_last_minute);
  return h(
    "section",
    {},
    h("div", { class: "row between" }, h("h2", {}, "용량"), h("button", { onclick: () => controller.show("capacity") }, "새로고침")),
    h("p", {}, `동시 세션 ${c.sessions} / ${c.max_sessions}`),
    h("p", {}, "최근 1분 추론: ", per.length ? per.map(([m, n]) => `${m} ${n}회`).join(", ") : "없음"),
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, ["세션", "연결", "프레임 간격", "평균 대기"].map((t) => h("th", {}, t)))),
      h("tbody", {}, c.session_details.map((d) => h("tr", {}, h("td", { class: "mono" }, d.session_id), h("td", {}, d.connected ? "연결됨" : "끊김"), h("td", {}, `${d.frame_interval_ms} ms`), h("td", {}, d.queue_ms_avg === null ? "-" : `${d.queue_ms_avg} ms`)))),
    ),
  );
}

function changesView(state) {
  return h(
    "section",
    {},
    h("h2", {}, "변경 기록"),
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, ["시각 (UTC)", "변경", "대상", "버전", "결과"].map((t) => h("th", {}, t)))),
      h(
        "tbody",
        {},
        state.changes.map((c) =>
          h(
            "tr",
            {},
            h("td", { class: "mono" }, c.changed_at_utc),
            h("td", {}, c.action),
            h("td", {}, c.target),
            h("td", {}, c.to_version ? `v${c.from_version ?? 0} → v${c.to_version}` : c.from_version ? `v${c.from_version}` : "-"),
            h("td", {}, h("span", { class: c.result_code === "ok" ? "badge published" : "badge archived" }, c.result_code)),
          ),
        ),
      ),
    ),
  );
}

render(controller.state);
