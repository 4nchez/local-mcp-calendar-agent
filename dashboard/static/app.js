/* 대시보드 로직.
   서버 상태는 MCP Client(/api/*)만 바라본다. API Server를 직접 호출하지 않는다. */

const API = "/api";
const SESSION = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());

const el = {
  log: document.getElementById("log"),
  input: document.getElementById("input"),
  send: document.getElementById("send"),
  chips: document.getElementById("chips"),
  rail: document.getElementById("rail"),
  schedules: document.getElementById("schedules"),
  count: document.getElementById("count"),
  toollog: document.getElementById("toollog"),
  clearLog: document.getElementById("clear-log"),
  status: document.getElementById("status"),
};

let knownIds = new Set();
let busy = false;

/* ---------- 대화 ---------- */
function bubble(text, kind = "agent", tag = "") {
  const div = document.createElement("div");
  div.className = `bubble bubble--${kind}`;
  text.split("\n").forEach((line) => {
    const p = document.createElement("p");
    p.textContent = line;
    div.appendChild(p);
  });
  if (tag) {
    const span = document.createElement("span");
    span.className = "bubble__tag";
    span.textContent = tag;
    div.appendChild(span);
  }
  el.log.appendChild(div);
  el.log.scrollTop = el.log.scrollHeight;
  return div;
}

/* ---------- 실행 레일 ---------- */
function resetRail(running = false) {
  el.rail.querySelectorAll(".rail__step").forEach((step, i) => {
    step.removeAttribute("data-state");
    step.querySelector(".rail__meta").textContent = running && i === 0 ? "처리 중" : "대기";
  });
  if (running) el.rail.querySelector('[data-stage="input"]').dataset.state = "run";
}

function paintRail(trace) {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  trace.forEach((entry, i) => {
    const step = el.rail.querySelector(`[data-stage="${entry.stage}"]`);
    if (!step) return;
    const paint = () => {
      step.dataset.state = entry.status;
      const ms = entry.duration_ms == null ? "" : ` · ${entry.duration_ms}ms`;
      step.querySelector(".rail__meta").textContent = (entry.detail || "-") + ms;
    };
    reduce ? paint() : setTimeout(paint, i * 90);
  });
}

/* ---------- 일정 패널 ---------- */
const WEEK = ["일", "월", "화", "수", "목", "금", "토"];

function dayLabel(date) {
  const today = new Date();
  const diff = Math.round((stripTime(date) - stripTime(today)) / 86400000);
  const base = `${date.getMonth() + 1}월 ${date.getDate()}일 (${WEEK[date.getDay()]})`;
  if (diff === 0) return `오늘 · ${base}`;
  if (diff === 1) return `내일 · ${base}`;
  return base;
}

function stripTime(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

function clock(date) {
  const h = String(date.getHours()).padStart(2, "0");
  const m = String(date.getMinutes()).padStart(2, "0");
  return `${h}:${m}`;
}

function renderSchedules(items) {
  el.count.textContent = `${items.length}건`;
  el.schedules.innerHTML = "";

  if (!items.length) {
    el.schedules.innerHTML = '<p class="empty">등록된 일정이 없습니다.</p>';
    knownIds = new Set();
    return;
  }

  const groups = new Map();
  items.forEach((item) => {
    const start = new Date(item.start_at);
    const key = stripTime(start);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push({ item, start });
  });

  [...groups.entries()]
    .sort((a, b) => a[0] - b[0])
    .forEach(([key, rows]) => {
      const group = document.createElement("div");
      group.className = "daygroup";
      const label = document.createElement("div");
      label.className = "daygroup__label";
      label.textContent = dayLabel(new Date(key));
      group.appendChild(label);

      rows.forEach(({ item, start }) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "event" + (knownIds.size && !knownIds.has(item.id) ? " event--new" : "");
        row.title = "클릭하면 삭제 문장이 입력됩니다";
        row.innerHTML = `<span class="event__time"></span><span class="event__title"></span>`;
        row.querySelector(".event__time").textContent = clock(start);
        row.querySelector(".event__title").textContent = item.title;
        row.addEventListener("click", () => {
          el.input.value = `${item.title} 취소해줘`;
          el.input.focus();
        });
        group.appendChild(row);
      });
      el.schedules.appendChild(group);
    });

  knownIds = new Set(items.map((i) => i.id));
}

/* ---------- 도구 실행 로그 ---------- */
function appendToolLog(calls, intent) {
  if (el.toollog.querySelector(".empty")) el.toollog.innerHTML = "";

  if (!calls.length) {
    const div = document.createElement("div");
    div.className = "logitem";
    div.dataset.ok = "false";
    div.innerHTML = `<div class="logitem__head"><span>도구 미실행</span></div>`;
    div.insertAdjacentHTML(
      "beforeend",
      `<div class="logitem__args">intent=${intent.intent} source=${intent.source}</div>`
    );
    el.toollog.prepend(div);
    return;
  }

  calls.forEach((call) => {
    const div = document.createElement("div");
    div.className = "logitem";
    div.dataset.ok = String(call.ok);

    const head = document.createElement("div");
    head.className = "logitem__head";
    head.innerHTML = `<span>${call.ok ? "" : "! "}${call.tool}()</span><span class="logitem__ms">${call.duration_ms}ms</span>`;

    const args = document.createElement("div");
    args.className = "logitem__args";
    args.textContent = JSON.stringify(call.arguments);

    const result = document.createElement("div");
    result.className = "logitem__result";
    result.textContent = `→ ${call.result.reason || "success"} · intent=${intent.intent}(${intent.source})`;

    div.append(head, args, result);
    el.toollog.prepend(div);
  });
}

/* ---------- 통신 ---------- */
async function send(message) {
  if (busy || !message.trim()) return;
  busy = true;
  el.send.disabled = true;
  bubble(message, "user");
  el.input.value = "";
  resetRail(true);

  try {
    const res = await fetch(`${API}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: SESSION }),
    });
    if (!res.ok) throw new Error(`서버가 ${res.status}로 응답했습니다`);
    const data = await res.json();

    paintRail(data.trace || []);
    const call = (data.tool_calls || [])[0];
    const tag = call
      ? `${call.tool} · ${call.duration_ms}ms · intent ${data.intent.intent} (${data.intent.source})`
      : `intent ${data.intent.intent} (${data.intent.source})`;
    bubble(data.reply, call && !call.ok ? "error" : "agent", tag);
    appendToolLog(data.tool_calls || [], data.intent);
    renderSchedules(data.schedules || []);
  } catch (err) {
    resetRail();
    bubble(`요청을 보내지 못했습니다. ${err.message}\n서버가 켜져 있는지 확인해 주세요.`, "error");
  } finally {
    busy = false;
    el.send.disabled = false;
    el.input.focus();
  }
}

async function refreshStatus() {
  const map = { ok: "ok", degraded: "warn", down: "down", model_missing: "warn", disabled: "warn" };
  try {
    const res = await fetch(`${API}/health`);
    const data = await res.json();
    el.status.querySelectorAll(".status__item").forEach((item) => {
      const state = (data.components?.[item.dataset.key] || {}).status;
      item.dataset.state = map[state] || "down";
      item.title = state || "unknown";
    });
  } catch {
    el.status.querySelectorAll(".status__item").forEach((i) => (i.dataset.state = "down"));
  }
}

async function loadSchedules() {
  try {
    const res = await fetch(`${API}/schedules`);
    renderSchedules(await res.json());
  } catch {
    /* 상태 표시가 이미 알려 준다 */
  }
}

/* ---------- 바인딩 ---------- */
el.send.addEventListener("click", () => send(el.input.value));
el.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) send(el.input.value);
});
el.chips.addEventListener("click", (e) => {
  if (e.target.classList.contains("chip")) send(e.target.textContent);
});
el.clearLog.addEventListener("click", () => {
  el.toollog.innerHTML = '<p class="empty">아직 실행된 도구가 없습니다.</p>';
});

refreshStatus();
loadSchedules();
setInterval(refreshStatus, 20000);
el.input.focus();
