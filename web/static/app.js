/* =============================================================================
   现场设备 HMI 前端逻辑
   - 启动时拉 /api/meta 建面板、/api/history 回填曲线
   - /api/stream (SSE) 持续推最新读数，驱动趋势曲线 + 指示灯 + 读数
   - 可写点：滑块/开关 -> POST /api/write
   ============================================================================= */
"use strict";

const state = {
  meta: null,
  points: new Map(),        // name -> point meta
  series: new Map(),        // name -> [values]  (趋势缓冲)
  charts: new Map(),        // name -> {canvas, ctx}
  startTime: Date.now(),
  lastSeq: -1,
};

const MAX_POINTS = 120;     // 曲线保留的采样数
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

// 当前设备（来自 URL ?device=NAME；为空时后端默认第一台）
const DEVICE = new URLSearchParams(location.search).get("device") || "";
const qd = (extra = "") => (DEVICE ? `?device=${encodeURIComponent(DEVICE)}${extra}` : (extra ? "?" + extra.replace(/^&/, "") : ""));

// ---- 初始化 ---------------------------------------------------------------
async function init() {
  const meta = await fetch(`/api/meta${qd()}`).then((r) => r.json());
  state.meta = meta;
  meta.points.forEach((p) => state.points.set(p.name, p));

  document.getElementById("deviceName").textContent = meta.device;
  document.getElementById("deviceLink").textContent =
    `Modbus TCP · ${meta.host}:${meta.port}`;
  document.getElementById("pollRate").textContent = `${meta.poll_interval}s`;
  document.getElementById("pointCount").textContent = meta.points.length;

  buildAnalog(meta.points.filter((p) => !p.is_bit));
  buildBits(meta.points.filter((p) => p.is_bit));

  await backfillHistory();
  startClock();
  connectStream();
  requestAnimationFrame(renderLoop);
}

// ---- 构建模拟量仪表 -------------------------------------------------------
function buildAnalog(points) {
  const grid = document.getElementById("analogGrid");
  for (const p of points) {
    const cmd = p.writable;
    const el = document.createElement("article");
    el.className = "inst";
    el.dataset.cmd = cmd;
    el.dataset.name = p.name;

    el.innerHTML = `
      <div class="inst__top">
        <div>
          <div class="inst__name">${p.name}</div>
          <div class="inst__desc">${p.description || ""}</div>
        </div>
        <span class="inst__badge" data-k="${cmd ? "cmd" : "ro"}">
          ${cmd ? "CMD" : p.register_type === "input" ? "INPUT" : "HOLDING"}
        </span>
      </div>
      <div class="inst__read">
        <span class="inst__val" data-val>—</span>
        <span class="inst__unit">${p.unit || ""}</span>
        <span class="inst__flag">越限 OUT OF RANGE</span>
      </div>
      <div class="inst__chart"><canvas></canvas></div>
      <div class="inst__scale">
        <span>${fmtNum(p.min)}</span>
        <span style="color:var(--ink-faint)">${p.sim_mode}</span>
        <span>${fmtNum(p.max)}</span>
      </div>
      ${cmd ? cmdControl(p) : ""}
    `;
    grid.appendChild(el);

    const canvas = el.querySelector("canvas");
    state.charts.set(p.name, { canvas, ctx: canvas.getContext("2d") });
    state.series.set(p.name, []);

    if (cmd) wireCmd(el, p);
  }
}

function cmdControl(p) {
  const step = p.scale >= 10 ? 0.5 : 1;
  return `
    <div class="inst__cmd">
      <div class="inst__cmd-row">
        <input type="range" min="${p.min}" max="${p.max}" step="${step}" value="${p.initial ?? p.min}">
        <span class="inst__cmd-out">—</span>
        <button class="btn-set">写入</button>
      </div>
    </div>`;
}

function wireCmd(el, p) {
  const range = el.querySelector('input[type="range"]');
  const out = el.querySelector(".inst__cmd-out");
  const btn = el.querySelector(".btn-set");
  const unit = p.unit || "";
  out.textContent = `${fmtNum(+range.value)} ${unit}`;
  range.addEventListener("input", () => {
    out.textContent = `${fmtNum(+range.value)} ${unit}`;
  });
  btn.addEventListener("click", () => writePoint(p.name, +range.value));
}

// ---- 构建位点位（指示灯 / 开关） -----------------------------------------
function buildBits(points) {
  const grid = document.getElementById("bitGrid");
  for (const p of points) {
    const writable = p.writable;
    const el = document.createElement("article");
    el.className = "lamp";
    el.dataset.name = p.name;
    el.dataset.on = "false";
    el.dataset.writable = writable;
    // 名字里含 alarm 的点亮起时显示为报警色
    el.dataset.alarmPoint = /alarm/i.test(p.name);

    el.innerHTML = `
      <span class="lamp__led"></span>
      <div class="lamp__body">
        <div class="lamp__name">${p.name}</div>
        <div class="lamp__desc">${p.description || ""}</div>
        <div class="lamp__state" data-state>—</div>
      </div>
      ${writable ? '<button class="toggle" data-on="false" aria-label="toggle"></button>' : ""}
    `;
    grid.appendChild(el);

    if (writable) {
      el.querySelector(".toggle").addEventListener("click", () => {
        const next = el.dataset.on === "true" ? 0 : 1;
        writePoint(p.name, next);
      });
    }
  }
}

// ---- 历史回填 -------------------------------------------------------------
async function backfillHistory() {
  try {
    const hist = await fetch(`/api/history${qd()}`).then((r) => r.json());
    for (const [name, samples] of Object.entries(hist)) {
      if (!state.series.has(name)) continue;
      state.series.set(name, samples.map((s) => s.v).slice(-MAX_POINTS));
    }
  } catch (_) { /* 历史可选 */ }
}

// ---- SSE 实时流 -----------------------------------------------------------
function connectStream() {
  const es = new EventSource(`/api/stream${qd()}`);
  es.onmessage = (ev) => applySnapshot(JSON.parse(ev.data));
  es.onerror = () => setLink(false, "重连中…");
}

function applySnapshot(snap) {
  setLink(snap.connected, snap.connected ? "LINK UP" : "LINK DOWN");
  document.getElementById("footStatus").textContent =
    snap.connected ? "数据流正常" : "等待设备…";
  if (snap.seq === state.lastSeq) return;
  state.lastSeq = snap.seq;

  for (const [name, r] of Object.entries(snap.readings)) {
    const p = state.points.get(name);
    if (!p) continue;
    if (p.is_bit) updateBit(name, r);
    else updateAnalog(name, r);
  }
}

function updateAnalog(name, r) {
  const el = document.querySelector(`.inst[data-name="${CSS.escape(name)}"]`);
  if (!el) return;
  el.dataset.alarm = String(!r.in_range);
  el.querySelector("[data-val]").textContent = fmtNum(r.value);

  const buf = state.series.get(name);
  buf.push(r.value);
  if (buf.length > MAX_POINTS) buf.shift();
}

function updateBit(name, r) {
  const el = document.querySelector(`.lamp[data-name="${CSS.escape(name)}"]`);
  if (!el) return;
  const on = r.value >= 1;
  el.dataset.on = String(on);
  el.dataset.alarm = String(on && el.dataset.alarmPoint === "true");
  el.querySelector("[data-state]").textContent = on ? "ACTIVE · 1" : "INACTIVE · 0";
  const tg = el.querySelector(".toggle");
  if (tg) tg.dataset.on = String(on);
}

// ---- 写值 -----------------------------------------------------------------
async function writePoint(name, value) {
  try {
    const res = await fetch("/api/write", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device: DEVICE, name, value }),
    }).then((r) => r.json());
    if (res.ok) toast(`✓ 已写入 ${name} = ${fmtNum(value)}`);
    else toast(`✗ 写入失败：${res.error}`, true);
  } catch (e) {
    toast(`✗ 写入失败：${e}`, true);
  }
}

// ---- 趋势曲线绘制（每帧重绘所有图） ---------------------------------------
function renderLoop() {
  for (const [name, { canvas, ctx }] of state.charts) {
    drawChart(name, canvas, ctx);
  }
  requestAnimationFrame(renderLoop);
}

function drawChart(name, canvas, ctx) {
  const p = state.points.get(name);
  const data = state.series.get(name);
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr; canvas.height = h * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  // 基线网格
  ctx.strokeStyle = css("--line-soft");
  ctx.lineWidth = 1;
  for (let i = 1; i < 3; i++) {
    const y = (h / 3) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  if (!data || data.length < 2) return;

  // 取量程，留 8% 余量
  let lo = p.min, hi = p.max;
  if (lo == null || hi == null) {
    lo = Math.min(...data); hi = Math.max(...data);
  }
  const pad = (hi - lo) * 0.08 || 1;
  lo -= pad; hi += pad;
  const xOf = (i) => (i / (MAX_POINTS - 1)) * w;
  const yOf = (v) => h - ((v - lo) / (hi - lo)) * h;

  const alarm = !lastInRange(p, data[data.length - 1]);
  const stroke = alarm ? css("--alarm") : p.writable ? css("--cmd") : css("--amber");

  // 填充
  const startX = xOf(MAX_POINTS - data.length);
  ctx.beginPath();
  ctx.moveTo(startX, h);
  data.forEach((v, i) => ctx.lineTo(xOf(MAX_POINTS - data.length + i), yOf(v)));
  ctx.lineTo(xOf(MAX_POINTS - 1), h);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, withAlpha(stroke, 0.22));
  grad.addColorStop(1, withAlpha(stroke, 0));
  ctx.fillStyle = grad; ctx.fill();

  // 趋势线
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = xOf(MAX_POINTS - data.length + i), y = yOf(v);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.strokeStyle = stroke; ctx.lineWidth = 1.6;
  ctx.lineJoin = "round"; ctx.stroke();

  // 末端发光点
  const lx = xOf(MAX_POINTS - 1), ly = yOf(data[data.length - 1]);
  ctx.beginPath(); ctx.arc(lx, ly, 2.6, 0, Math.PI * 2);
  ctx.fillStyle = stroke; ctx.shadowColor = stroke; ctx.shadowBlur = 8;
  ctx.fill(); ctx.shadowBlur = 0;
}

function lastInRange(p, v) {
  if (p.min != null && v < p.min) return false;
  if (p.max != null && v > p.max) return false;
  return true;
}

// ---- 小工具 ---------------------------------------------------------------
function fmtNum(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}
function withAlpha(oklchStr, a) {
  // css() 返回形如 "oklch(0.82 0.145 74)"，转成带 alpha
  const m = oklchStr.match(/oklch\(([^)]+)\)/);
  return m ? `oklch(${m[1]} / ${a})` : oklchStr;
}
function setLink(up, txt) {
  const el = document.getElementById("linkState");
  el.dataset.up = String(up);
  el.querySelector(".link__txt").textContent = txt;
}
let toastTimer;
function toast(msg, err = false) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.hidden = false; t.dataset.err = String(err);
  requestAnimationFrame(() => (t.dataset.show = "true"));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.dataset.show = "false"), 2600);
}
function startClock() {
  const clk = document.getElementById("clock");
  const up = document.getElementById("uptime");
  setInterval(() => {
    const d = new Date();
    clk.textContent = d.toLocaleTimeString("zh-CN", { hour12: false });
    const s = Math.floor((Date.now() - state.startTime) / 1000);
    up.textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }, 1000);
}

init();
