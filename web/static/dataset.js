/* =============================================================================
   数据集构建器
   - /api/dataset/schema 构建设备/特征选择
   - /api/dataset/preview 实时预览行数/列/样例
   - /api/dataset/build  按选择 POST 下载（zip/parquet/wide/long）
   ============================================================================= */
"use strict";

const state = {
  minutes: "15",
  downsample: 1,
  window: 0,
  windowStats: ["mean", "std", "diff"],
  split: "",
  fmt: "zip",
  trendMin: "",          // 历史趋势的时间范围（""=全部）
  source: "memory",      // 数据来源：memory 内存近期 / disk 磁盘完整历史
  labels: { alarm: true, out_of_range: false, per_device: false },
  cols: new Map(),     // column -> {meta, checked}
  schema: null,
};

const MAX_TREND_CHARTS = 40;
const cssv = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

const $ = (id) => document.getElementById(id);

// ---- 初始化 ---------------------------------------------------------------
async function init() {
  state.schema = await fetch("/api/dataset/schema").then((r) => r.json());
  buildFeatureGrid(state.schema);
  wireSegments();
  wireLabels();
  wireToolbar();
  $("btnPreview").addEventListener("click", doPreview);
  $("btnExport").addEventListener("click", doExport);
  wireTrend();
  startClock();
  pollStats();
  setInterval(pollStats, 3000);
  refreshSummary();
  loadTrends();          // 首屏即画出所有变量的历史趋势
}

// ---- 特征选择网格 ---------------------------------------------------------
function buildFeatureGrid(schema) {
  const grid = $("featGrid");
  grid.innerHTML = "";
  for (const dev of schema.devices) {
    const card = document.createElement("div");
    card.className = "feat__dev";
    card.innerHTML = `
      <div class="feat__head">
        <label class="check check--dev">
          <input type="checkbox" class="dev-all" data-device="${dev.device}" checked>
          <span><b>${dev.device}</b> <em>${dev.type || ""}</em></span>
        </label>
        <span class="feat__count" data-count="${dev.device}"></span>
      </div>
      <div class="feat__points"></div>`;
    const pts = card.querySelector(".feat__points");
    for (const p of dev.points) {
      state.cols.set(p.column, { meta: { ...p, device: dev.device }, checked: true });
      const chip = document.createElement("label");
      chip.className = "pchip";
      chip.dataset.bit = String(p.is_bit);
      chip.dataset.column = p.column;
      chip.innerHTML = `
        <input type="checkbox" class="pt" data-column="${p.column}" checked>
        <span class="pchip__name">${p.point}</span>
        <span class="pchip__meta">${p.is_bit ? "0/1" : (p.unit || "")}</span>`;
      pts.appendChild(chip);
    }
    grid.appendChild(card);
  }

  grid.addEventListener("change", (e) => {
    const t = e.target;
    if (t.classList.contains("pt")) {
      state.cols.get(t.dataset.column).checked = t.checked;
    } else if (t.classList.contains("dev-all")) {
      grid.querySelectorAll(`.pt`).forEach((cb) => {
        const c = state.cols.get(cb.dataset.column);
        if (c.meta.device === t.dataset.device) {
          cb.checked = t.checked; c.checked = t.checked;
        }
      });
    }
    refreshSummary();
    scheduleTrend();      // 选择变化后自动刷新趋势图（防抖）
  });
  updateDevCounts();
}

let _trendTimer;
function scheduleTrend() { clearTimeout(_trendTimer); _trendTimer = setTimeout(loadTrends, 350); }

function updateDevCounts() {
  for (const dev of state.schema.devices) {
    const total = dev.points.length;
    const on = dev.points.filter((p) => state.cols.get(p.column).checked).length;
    const el = document.querySelector(`[data-count="${dev.device}"]`);
    if (el) el.textContent = `${on}/${total}`;
  }
}

function selectedColumns() {
  return [...state.cols.entries()].filter(([, v]) => v.checked).map(([c]) => c);
}
function selectedDevices() {
  const devs = new Set();
  for (const [, v] of state.cols) if (v.checked) devs.add(v.meta.device);
  return [...devs];
}

// ---- 工具栏（过滤 / 全选） ------------------------------------------------
function wireToolbar() {
  document.querySelectorAll(".zone__tools [data-filter]").forEach((b) => {
    b.addEventListener("click", () => setFilter(b.dataset.filter));
  });
  $("selAll").addEventListener("click", () => setAll(true));
  $("selNone").addEventListener("click", () => setAll(false));
}
function setAll(on) {
  document.querySelectorAll(".feat .pt, .feat .dev-all").forEach((cb) => (cb.checked = on));
  for (const v of state.cols.values()) v.checked = on;
  refreshSummary();
}
function setFilter(kind) {
  document.querySelectorAll(".feat .pchip").forEach((chip) => {
    const isBit = chip.dataset.bit === "true";
    const show = kind === "all" || (kind === "bit" && isBit) || (kind === "analog" && !isBit);
    chip.style.display = show ? "" : "none";
  });
}

// ---- 段选择器 -------------------------------------------------------------
function wireSegments() {
  seg("rangeSeg", (b) => (state.minutes = b.dataset.min));
  seg("srcSeg", (b) => { state.source = b.dataset.src; onSourceChange(); });
  seg("dsSeg", (b) => (state.downsample = +b.dataset.ds));
  seg("winSeg", (b) => {
    state.window = +b.dataset.win;
    $("winStatsSeg").hidden = state.window < 2;     // 开窗才显示统计量选择
  });
  seg("splitSeg", (b) => (state.split = b.dataset.split));
  seg("fmtSeg", (b) => {
    state.fmt = b.dataset.fmt;
    $("sumFmt").textContent = { zip: "ZIP", parquet: "Parquet", wide: "宽表 CSV", long: "长表 CSV" }[state.fmt];
  });
  // 滑窗统计量：多选切换
  const ws = $("winStatsSeg");
  ws.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    b.classList.toggle("on");
    state.windowStats = [...ws.querySelectorAll("button.on")].map((x) => x.dataset.stat);
    refreshSummary();
  });
}
function seg(id, onPick) {
  const el = $(id);
  el.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    el.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
    b.classList.add("on"); onPick(b); refreshSummary();
  });
}
function wireLabels() {
  $("lblAlarm").addEventListener("change", (e) => { state.labels.alarm = e.target.checked; refreshSummary(); });
  $("lblOOR").addEventListener("change", (e) => { state.labels.out_of_range = e.target.checked; refreshSummary(); });
  $("lblPerDev").addEventListener("change", (e) => { state.labels.per_device = e.target.checked; refreshSummary(); });
}

// ---- 摘要 -----------------------------------------------------------------
function currentSelection() {
  return {
    minutes: state.minutes || null,
    columns: selectedColumns(),
    devices: selectedDevices(),
    downsample: state.downsample,
    window: state.window,
    window_stats: state.windowStats,
    split: state.split || null,
    source: state.source,
    labels: state.labels,
    format: state.fmt,
  };
}

// 数据来源切换：磁盘时拉取可回看时长并刷新趋势
async function onSourceChange() {
  const note = $("srcNote");
  if (state.source === "disk") {
    note.textContent = "读取中…";
    try {
      const d = await fetch("/api/dataset/disk").then((r) => r.json());
      note.textContent = d.available
        ? `磁盘可回看 ${d.span_human}（${d.rows.toLocaleString()} 样本，${d.files} 个文件）`
        : "磁盘暂无历史文件";
    } catch (_) { note.textContent = ""; }
  } else {
    note.textContent = "";
  }
  loadTrends();
}
function labelCount() {
  return (state.labels.alarm ? 1 : 0) + (state.labels.out_of_range ? 1 : 0);
}
function refreshSummary() {
  updateDevCounts();
  const nSel = selectedColumns().length;
  const winNote = state.window >= 2 ? `（+滑窗×${state.windowStats.length}/模拟量）` : "";
  $("sumFeat").textContent = `${nSel}${winNote}`;
  const lbls = [];
  if (state.labels.alarm) lbls.push("alarm");
  if (state.labels.out_of_range) lbls.push("oor");
  if (state.labels.per_device) lbls.push(`按设备×${selectedDevices().length}`);
  $("sumLabels").textContent = lbls.length ? lbls.join(" + ") : "无";
  const splitRow = $("sumSplit");
  if (splitRow) splitRow.textContent = state.split ? state.split.replace(/\//g, " / ") : "不划分";
}

// ---- 历史趋势小图 ---------------------------------------------------------
function wireTrend() {
  seg("trendRangeSeg", (b) => { state.trendMin = b.dataset.min; loadTrends(); });
  $("btnTrend").addEventListener("click", loadTrends);
}

async function loadTrends() {
  const cols = selectedColumns();
  const grid = $("trendGrid");
  if (!cols.length) { grid.innerHTML = '<div class="trend__empty">请先在上方勾选特征</div>'; return; }
  const shown = cols.slice(0, MAX_TREND_CHARTS);
  $("trendNote").textContent =
    `${cols.length} 个特征` + (cols.length > MAX_TREND_CHARTS ? `（图表显示前 ${MAX_TREND_CHARTS}）` : "");
  let res;
  try {
    res = await fetch("/api/dataset/series", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ columns: shown, minutes: state.trendMin || null,
                             max_points: 240, source: state.source }),
    }).then((r) => r.json());
  } catch (_) { return; }

  if (!res.points) { grid.innerHTML = '<div class="trend__empty">暂无历史数据，稍候记录器积累后刷新</div>'; return; }
  grid.innerHTML = "";
  for (const s of res.series) {
    const card = document.createElement("div");
    card.className = "tcard";
    const vals = s.data.filter((v) => v != null);
    const last = vals.length ? vals[vals.length - 1] : null;
    const lo = vals.length ? Math.min(...vals) : 0;
    const hi = vals.length ? Math.max(...vals) : 1;
    card.innerHTML = `
      <div class="tcard__head">
        <span class="tcard__name" title="${s.column}">${s.device}·${s.point}</span>
        <span class="tcard__val">${fmtV(last)}<i>${s.unit || (s.is_bit ? "" : "")}</i></span>
      </div>
      <canvas></canvas>
      <div class="tcard__foot"><span>min ${fmtV(lo)}</span><span>max ${fmtV(hi)}</span></div>`;
    grid.appendChild(card);
    drawTrend(card.querySelector("canvas"), s);
  }
  $("trendNote").textContent += ` · ${res.points} 点 / 跨度 ${res.span_human}`;
}

function drawTrend(canvas, s) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 240, h = 64;
  canvas.width = w * dpr; canvas.height = h * dpr;
  canvas.style.height = h + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const data = s.data;
  const vals = data.filter((v) => v != null);
  if (vals.length < 2) return;
  let lo = s.is_bit ? 0 : (s.min != null ? s.min : Math.min(...vals));
  let hi = s.is_bit ? 1 : (s.max != null ? s.max : Math.max(...vals));
  if (hi - lo < 1e-9) { hi = lo + 1; }
  const pad = s.is_bit ? 0.15 : (hi - lo) * 0.08;
  lo -= pad; hi += pad;

  // 网格基线
  ctx.strokeStyle = cssv("--line-soft"); ctx.lineWidth = 1;
  for (let i = 1; i < 3; i++) { const y = (h / 3) * i; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }

  const n = data.length;
  const xOf = (i) => (i / (n - 1)) * w;
  const yOf = (v) => h - ((v - lo) / (hi - lo)) * h;
  const stroke = s.is_bit ? cssv("--cmd") : (/alarm/i.test(s.point) ? cssv("--alarm") : cssv("--amber"));

  // 面积
  ctx.beginPath();
  let started = false;
  data.forEach((v, i) => { if (v == null) return; const x = xOf(i), y = yOf(v); started ? ctx.lineTo(x, y) : (ctx.moveTo(x, y), started = true); });
  // 趋势线
  ctx.strokeStyle = stroke; ctx.lineWidth = 1.5; ctx.lineJoin = "round";
  if (s.is_bit) { drawStep(ctx, data, xOf, yOf); } else { ctx.stroke(); }

  // 末端点
  const li = lastIdx(data);
  if (li >= 0) {
    ctx.beginPath(); ctx.arc(xOf(li), yOf(data[li]), 2.4, 0, Math.PI * 2);
    ctx.fillStyle = stroke; ctx.shadowColor = stroke; ctx.shadowBlur = 6; ctx.fill(); ctx.shadowBlur = 0;
  }
}
function drawStep(ctx, data, xOf, yOf) {
  ctx.beginPath(); let started = false, py = 0;
  data.forEach((v, i) => {
    if (v == null) return; const x = xOf(i), y = yOf(v);
    if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, py); ctx.lineTo(x, y); }
    py = y;
  });
  ctx.stroke();
}
function lastIdx(d) { for (let i = d.length - 1; i >= 0; i--) if (d[i] != null) return i; return -1; }
function fmtV(v) { return v == null ? "—" : (Number.isInteger(v) ? v : v.toFixed(2)); }

// ---- 预览 -----------------------------------------------------------------
async function doPreview() {
  const sel = currentSelection();
  if (!sel.columns.length) return toast("请至少选择一个特征点位", true);
  const p = await fetch("/api/dataset/preview", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sel),
  }).then((r) => r.json());

  $("sumRows").textContent = p.rows;
  $("sumInterval").textContent = `${p.effective_interval}s`;
  const box = $("previewBox"); box.hidden = false;
  const tbl = $("previewTable");
  const head = p.header.map((h) => `<th>${h.replace(/^.*\./, "")}</th>`).join("");
  const rows = p.sample.map((r) =>
    `<tr>${r.map((v) => `<td>${v == null ? "" : v}</td>`).join("")}</tr>`).join("");
  tbl.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${rows}</tbody>`;
  toast(`✓ 预览：${p.rows} 行 × ${p.n_columns} 列`);
}

// ---- 导出下载 -------------------------------------------------------------
async function doExport() {
  const sel = currentSelection();
  if (!sel.columns.length) return toast("请至少选择一个特征点位", true);
  $("buildHint").textContent = "正在生成…";
  try {
    const res = await fetch("/api/dataset/build", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sel),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const fn = (cd.match(/filename=([^;]+)/) || [])[1] || `dataset.${state.fmt}`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = fn; document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    $("buildHint").textContent = `已下载 ${fn}（${(blob.size / 1024).toFixed(1)} KB）`;
    toast(`✓ 已导出 ${fn}`);
  } catch (e) {
    $("buildHint").textContent = "";
    toast(`✗ 导出失败：${e.message}`, true);
  }
}

// ---- 状态轮询 -------------------------------------------------------------
async function pollStats() {
  try {
    const s = await fetch("/api/dataset/stats").then((r) => r.json());
    $("recState").textContent = s.recording ? "● ON" : "OFF";
    $("recState").style.color = s.recording ? "var(--ok)" : "var(--ink-faint)";
    $("recRows").textContent = s.rows.toLocaleString();
    $("recSpan").textContent = s.span_human || "0s";
    $("footStatus").textContent = s.parquet_available
      ? `采集中 · ${s.rows} 样本 · Parquet 可用`
      : `采集中 · ${s.rows} 样本 · (Parquet 不可用，pip install pyarrow)`;
  } catch (_) { $("footStatus").textContent = "状态获取失败"; }
}

let toastTimer;
function toast(msg, err = false) {
  const t = $("toast"); t.textContent = msg; t.hidden = false; t.dataset.err = String(err);
  requestAnimationFrame(() => (t.dataset.show = "true"));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.dataset.show = "false"), 2800);
}
function startClock() {
  setInterval(() => {
    $("clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  }, 1000);
}

init();
