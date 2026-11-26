// Sim page rendering (TASK-46). Logic is in core.js. Each screen is built once and then
// updated in place, so the video keeps playing and typed input is not lost.
// Text from the server is set with textContent only. Frames stay in memory.
import { HANDOFF_READY, HANDOFF_WAIT_MS, HUD_MODELS, SimController, acceptHandoff, message } from "./core.js";

const MAX_SIDE = 640;
const JPEG_QUALITY = 0.8;
const STATE_NAMES = { clear: "정상", candidate: "확인 중", warning: "경고", unknown: "판단 불가", resolved: "해제" };

const root = document.getElementById("app");
const timers = new Map();
let timerSeq = 0;

const controller = new SimController({
  fetch: window.fetch.bind(window),
  WebSocket: window.WebSocket,
  now: () => performance.now(),
  wsUrl: (path) => `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}${path}`,
  onChange: () => scheduleRender(),
  speak,
  setTimer: (fn, ms) => {
    const id = ++timerSeq;
    timers.set(id, window.setTimeout(() => { timers.delete(id); fn(); }, ms));
    return id;
  },
  clearTimer: (id) => {
    window.clearTimeout(timers.get(id));
    timers.delete(id);
  },
});

function speak(text) {
  if (!("speechSynthesis" in window)) return;
  window.speechSynthesis.cancel(); // a new alert interrupts what is playing
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "ko-KR";
  window.speechSynthesis.speak(utterance);
}

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

let built = null; // {screen, update(state)}
let renderQueued = false;

function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  window.requestAnimationFrame(() => {
    renderQueued = false;
    render(controller.state);
  });
}

function render(state) {
  if (built?.screen !== state.screen) {
    built = { login: buildLogin, missions: buildMissions, glass: buildGlass }[state.screen]();
    root.replaceChildren(built.node);
  }
  built.update(state);
}

function banner() {
  const error = h("p", { class: "banner error", role: "alert", hidden: true });
  const notice = h("p", { class: "banner ok", role: "status", hidden: true });
  const update = (state) => {
    error.hidden = !state.error;
    error.textContent = state.error ?? "";
    notice.hidden = !state.notice;
    notice.textContent = state.notice ?? "";
  };
  return { nodes: [error, notice], update };
}

function hudModelSelect() {
  return h(
    "select",
    { onchange: (e) => controller.set({ hudModel: e.target.value }) },
    Object.entries(HUD_MODELS).map(([key, m]) => h("option", { value: key, selected: key === controller.state.hudModel }, m.label)),
  );
}

// --- login and signup ---

function buildLogin() {
  const messages = banner();
  const user = h("input", { autocomplete: "username", required: true });
  const pass = h("input", { type: "password", autocomplete: "current-password", required: true });
  const label = h("input", { maxlength: 64, placeholder: "시뮬 페이지" });
  const newUser = h("input", { pattern: "[a-z0-9_-]{3,32}", required: true });
  const newName = h("input", { maxlength: 64 });
  const newPass = h("input", { type: "password", minlength: 8, autocomplete: "new-password", required: true });
  const login = h(
    "form",
    { onsubmit: (e) => { e.preventDefault(); controller.login(user.value.trim(), pass.value, label.value.trim()); } },
    field("아이디", user),
    field("비밀번호", pass),
    field("장치 이름 (선택, 표시용)", label),
    field("기종 HUD", hudModelSelect()),
    h("button", { type: "submit", class: "primary" }, "로그인"),
  );
  const signup = h(
    "form",
    { onsubmit: (e) => { e.preventDefault(); controller.signup(newUser.value.trim(), newPass.value, newName.value.trim()); } },
    field("아이디 (영문 소문자, 숫자, _ - 3~32자)", newUser),
    field("표시 이름 (선택)", newName),
    field("비밀번호 (8자 이상)", newPass),
    h("button", { type: "submit" }, "가입 신청"),
    h("p", { class: "muted" }, "이름 외의 개인정보는 받지 않습니다. 계정 정보는 위험 판단에 쓰지 않습니다."),
  );
  const node = h(
    "main",
    { class: "card narrow" },
    h("h1", {}, "VOA 시뮬레이터"),
    h("p", { class: "muted" }, "글래스 앱과 같은 API를 씁니다. 착용자 또는 관리자 계정으로 로그인합니다. 새로고침하면 로그아웃됩니다."),
    ...messages.nodes,
    h("div", { class: "split" }, h("section", {}, h("h2", {}, "로그인"), login), h("section", {}, h("h2", {}, "가입"), signup)),
  );
  return { screen: "login", node, update: (state) => messages.update(state) };
}

// --- mission choice ---

function buildMissions() {
  const messages = banner();
  const list = h("div", { class: "missions" });
  const node = h(
    "main",
    { class: "card" },
    h("div", { class: "row between" }, h("h1", {}, "미션"), h("button", { class: "link", onclick: () => controller.logout() }, "로그아웃")),
    h("p", { class: "muted" }, "무엇을 감시할지 고르면, 그 미션에 맞는 모델과 규칙으로 감지를 시작합니다."),
    ...messages.nodes,
    list,
  );
  let shown = null;
  const update = (state) => {
    messages.update(state);
    if (shown === state.missions) return;
    shown = state.missions;
    list.replaceChildren(
      ...state.missions.map((m) =>
        h(
          "article",
          { class: "mission" },
          h("h2", {}, m.name),
          h("p", { class: "muted" }, m.description || m.mission_id),
          h("p", {}, `v${m.version}, 감시: ${m.scenarios.map((s) => s.id).join(", ")}, 모델: ${m.models.join(", ")}`),
          h("button", { class: "primary", onclick: () => controller.chooseMission(m.mission_id) }, "이 미션으로 시작"),
        ),
      ),
    );
  };
  return { screen: "missions", node, update };
}

// --- glass view ---

function buildGlass() {
  const messages = banner();
  const video = h("video", { muted: true, playsinline: true, class: "source" });
  video.muted = true;
  const capture = document.createElement("canvas");
  const fileInput = h("input", { type: "file", accept: "video/*", hidden: true, onchange: () => openVideo(fileInput.files?.[0]) });
  let objectUrl = null;
  let stream = null;

  async function startCamera() {
    stopSource();
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640 }, audio: false });
    } catch {
      return controller.set({ error: "카메라를 열 수 없습니다." });
    }
    video.srcObject = stream;
    await video.play();
    controller.startCapture();
  }

  async function openVideo(file) {
    if (!file) return;
    stopSource();
    objectUrl = URL.createObjectURL(file); // played locally; the file is never uploaded
    video.src = objectUrl;
    video.loop = true;
    await video.play();
    controller.startCapture();
  }

  function stopSource() {
    controller.stopCapture();
    video.pause();
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
    video.srcObject = null;
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = null;
    video.removeAttribute("src");
  }

  // HUD layer (SPEC 7.4): popup + status bar, no boxes on the glass view
  const statusDot = h("span", { class: "dot" });
  const statusText = h("span", {});
  const statusBarEl = h("div", { class: "statusbar", "aria-live": "off" }, statusDot, statusText);
  const popupText = h("p", { class: "popup-text" });
  const popup = h("div", { class: "popup", hidden: true }, popupText);
  const flashEl = h("div", { class: "flash" });
  const glass = h("div", { class: "glass" }, h("div", { class: "safe" }, popup, statusBarEl), flashEl);

  // judged-frame window: boxes drawn on the frame that was judged, with its age
  const pip = document.createElement("canvas");
  pip.className = "pip";
  const pipAge = h("span", { class: "muted" });

  const missionName = h("strong", {});
  const interval = h("span", { class: "muted" });
  const link = h("dd", {});
  const upload = h("dd", {});
  const scenarios = h("dl", { class: "states" });
  const timing = h("dl", { class: "timing" });
  const log = h("ol", { class: "log mono" });

  const node = h(
    "div",
    { class: "shell" },
    h(
      "header",
      { class: "top" },
      h("strong", {}, "VOA 시뮬레이터"),
      h("span", {}, "미션: ", missionName, " "),
      h("button", { class: "link", onclick: () => { stopSource(); controller.changeMission(); } }, "변경"),
      interval,
      h("button", { class: "link right", onclick: () => { stopSource(); controller.logout(); } }, "로그아웃"),
    ),
    ...messages.nodes,
    h(
      "div",
      { class: "row" },
      h("button", { onclick: startCamera }, "웹캠 시작"),
      h("button", { onclick: () => fileInput.click() }, "동영상 열기"),
      fileInput,
      h("button", { onclick: stopSource }, "정지"),
      field("기종 HUD", hudModelSelect()),
    ),
    h(
      "div",
      { class: "stage" },
      h("div", { class: "view" }, video, glass),
      h("aside", { class: "side" }, h("h2", {}, "판정 프레임 (디버그)"), pip, pipAge, h("p", { class: "muted" }, "보낸 프레임 위에 그립니다. 글래스에는 표시하지 않습니다. 빨간 실선은 미션이 판정에 쓰는 라벨, 흐린 점선은 그 밖의 라벨입니다.")),
    ),
    h(
      "div",
      { class: "panels" },
      h("section", {}, h("h2", {}, "연결"), h("dl", {}, h("dt", {}, "제어 연결"), link, h("dt", {}, "프레임 업로드"), upload)),
      h("section", {}, h("h2", {}, "시나리오 상태"), scenarios),
      h("section", {}, h("h2", {}, "마지막 응답 timing (개발용)"), timing),
      h("section", {}, h("h2", {}, "기록 (ID만)"), log),
    ),
  );

  let lastJudged = null;
  let lastHud = null;
  const update = (state) => {
    messages.update(state);
    missionName.textContent = state.mission ? `${state.mission.name} v${state.mission.version}` : "-";
    interval.textContent = `프레임 간격 ${state.frameIntervalMs} ms`;
    const status = controller.status();
    statusBarEl.className = `statusbar ${status.tone}`;
    statusText.textContent = status.label;
    link.textContent = state.capacity ? "정원 초과" : state.connected ? "연결됨 (wss/ws, 인증됨)" : "끊김, 다시 연결 중";
    const c = state.counters;
    upload.textContent = `${state.uploading ? "보내는 중" : "멈춤"}, 보냄 ${c.sent}, 건너뜀 ${c.skipped}, 실패 ${c.failed}`;
    scenarios.replaceChildren(
      ...(status.showStates
        ? Object.entries(state.states).flatMap(([id, value]) => [h("dt", {}, id), h("dd", { class: `state ${value}` }, STATE_NAMES[value] ?? value)])
        : [h("dd", { class: "muted" }, "연결 상태를 확인할 수 없어 감지 상태를 숨깁니다.")]),
    );
    const t = state.timing;
    timing.replaceChildren(
      ...(t
        ? [
            ["왕복 (촬영 → 결과)", t.rttMs === null ? "-" : `${Math.round(t.rttMs)} ms`],
            ["대기", `${t.queue_ms} ms`],
            ["추론", `${t.provider_ms} ms`],
            ["엔진", `${t.engine_us} us`],
          ].flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])
        : [h("dd", { class: "muted" }, "아직 응답이 없습니다.")]),
    );
    log.replaceChildren(...state.log.map((row) => h("li", {}, row)));
    if (state.hud !== lastHud) {
      lastHud = state.hud;
      drawHud(state.hud);
    }
    if (state.judged !== lastJudged) {
      lastJudged = state.judged;
      drawJudged(state.judged);
    }
    if (state.judged) pipAge.textContent = `프레임 나이 ${Math.round(performance.now() - state.judged.captureMs)} ms`;
  };

  function drawHud(hud) {
    popup.hidden = !hud;
    flashEl.className = "flash";
    if (!hud) return;
    popupText.textContent = hud.text;
    popupText.style.setProperty("--lines", String(hud.maxLines));
    popup.className = `popup ${hud.position} ${hud.textSize} ${hud.color ?? "inverse"}`;
    if (hud.flash.mode !== "none") {
      flashEl.style.setProperty("--on", `${hud.flash.on_ms}ms`);
      flashEl.style.setProperty("--period", `${hud.flash.on_ms + hud.flash.off_ms}ms`);
      flashEl.style.setProperty("--count", String(hud.flash.count));
      void flashEl.offsetWidth; // restart the animation
      flashEl.className = `flash ${hud.flash.mode} ${hud.color ?? "inverse"}`;
    }
  }

  function drawJudged(judged) {
    const ctx = pip.getContext("2d");
    if (!judged) return ctx.clearRect(0, 0, pip.width, pip.height);
    const image = judged.image;
    pip.width = image.width;
    pip.height = image.height;
    ctx.drawImage(image, 0, 0);
    ctx.font = "14px system-ui";
    // Labels the mission does not judge on first, faint and dashed; judged ones on top.
    const ordered = [...judged.detections].sort((a, b) => Number(a.judged) - Number(b.judged));
    for (const d of ordered) {
      const [x1, y1, x2, y2] = d.bbox;
      ctx.save();
      if (d.judged) {
        ctx.lineWidth = 3;
        ctx.setLineDash([]);
        ctx.strokeStyle = ctx.fillStyle = "#ff3b30";
      } else {
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.globalAlpha = 0.5;
        ctx.strokeStyle = ctx.fillStyle = "#c8ccd2";
      }
      ctx.strokeRect(x1 * pip.width, y1 * pip.height, (x2 - x1) * pip.width, (y2 - y1) * pip.height);
      ctx.fillText(`${d.label} ${Number(d.score).toFixed(2)}`, x1 * pip.width + 3, Math.max(14, y1 * pip.height - 4));
      ctx.restore();
    }
  }

  async function grab() {
    if (!controller.due() || video.readyState < 2) return;
    const scale = Math.min(1, MAX_SIDE / Math.max(video.videoWidth, video.videoHeight));
    capture.width = Math.round(video.videoWidth * scale);
    capture.height = Math.round(video.videoHeight * scale);
    capture.getContext("2d").drawImage(video, 0, 0, capture.width, capture.height);
    const captureMs = performance.now();
    const blob = await new Promise((resolve) => capture.toBlob(resolve, "image/jpeg", JPEG_QUALITY));
    if (!blob) return;
    const [bytes, image] = await Promise.all([blob.arrayBuffer(), createImageBitmap(blob)]);
    await controller.upload({ bytes: new Uint8Array(bytes), image, captureMs });
  }

  return { screen: "glass", node, update, grab };
}

// Opened from the admin page: ask it for the admin login instead of showing the form.
function adminHandoff() {
  const opener = window.opener;
  if (!opener) return;
  controller.set({ notice: "관리자 페이지의 로그인으로 시작하는 중입니다." });
  const done = (event) => {
    const token = acceptHandoff(event, window.origin, opener);
    if (token === null) return;
    window.removeEventListener("message", done);
    window.clearTimeout(fallback);
    controller.loginAsAdmin(token);
  };
  const fallback = window.setTimeout(() => {
    window.removeEventListener("message", done);
    controller.set({ notice: null, error: message("admin_handoff_missing") });
  }, HANDOFF_WAIT_MS);
  window.addEventListener("message", done);
  opener.postMessage({ type: HANDOFF_READY }, window.origin);
}

// One page-wide tick: watchdog, HUD expiry and, on the glass view, frame capture.
let grabbing = false;
window.setInterval(async () => {
  controller.checkWatchdog();
  controller.expireHud();
  if (built?.screen !== "glass") return;
  if (!grabbing) {
    grabbing = true;
    try {
      await built.grab();
    } finally {
      grabbing = false;
    }
  }
  scheduleRender();
}, 50);

render(controller.state);
adminHandoff(); // once, after the first screen (TASK-46A)
