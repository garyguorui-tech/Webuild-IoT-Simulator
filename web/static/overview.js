/* =============================================================================
   设备群总览页逻辑
   - SSE /api/fleet/stream 周期推送每台设备概要（连接/报警/KPI）
   - 渲染设备瓦片，点击进入 /device?device=NAME 详情页
   ============================================================================= */
"use strict";

const tiles = new Map();   // device name -> 瓦片 DOM
let built = false;

function buildTile(d) {
  const a = document.createElement("a");
  a.className = "tile";
  a.href = `/device?device=${encodeURIComponent(d.device)}`;
  a.dataset.device = d.device;
  a.innerHTML = `
    <div class="tile__head">
      <div>
        <div class="tile__name">${d.device}</div>
        <div class="tile__type">${d.type || "现场设备"}</div>
      </div>
      <div class="tile__link" data-up="false">
        <span class="link__lamp"></span><span class="tile__linktxt">—</span>
      </div>
    </div>
    <div class="tile__kpis"></div>
    <div class="tile__foot">
      <span class="tile__addr">${d.host}:${d.port}</span>
      <span class="tile__alarm" data-has="false">● <span class="tile__alarmn">0</span> 报警</span>
    </div>`;
  document.getElementById("fleetGrid").appendChild(a);
  tiles.set(d.device, a);
  return a;
}

function updateTile(d) {
  const el = tiles.get(d.device) || buildTile(d);

  const link = el.querySelector(".tile__link");
  link.dataset.up = String(d.connected);
  link.querySelector(".tile__linktxt").textContent = d.connected ? "ONLINE" : "OFFLINE";

  // KPI（最多 3 个模拟量）
  el.querySelector(".tile__kpis").innerHTML = d.kpis.map((k) => `
    <div class="kpi">
      <div class="kpi__v">${fmt(k.value)}<span class="kpi__u">${k.unit || ""}</span></div>
      <div class="kpi__k">${k.name}</div>
    </div>`).join("");

  const alarm = el.querySelector(".tile__alarm");
  alarm.dataset.has = String(d.alarms > 0);
  alarm.querySelector(".tile__alarmn").textContent = d.alarms;
  el.dataset.alarm = String(d.alarms > 0);
}

function applyFleet(devices) {
  document.getElementById("devCount").textContent = devices.length;
  document.getElementById("onlineCount").textContent =
    devices.filter((d) => d.connected).length;
  document.getElementById("alarmCount").textContent =
    devices.reduce((s, d) => s + d.alarms, 0);
  document.getElementById("footStatus").textContent =
    devices.every((d) => d.connected) ? "全部设备在线" : "部分设备离线";
  devices.forEach(updateTile);
  built = true;
}

function connect() {
  const es = new EventSource("/api/fleet/stream");
  es.onmessage = (ev) => applyFleet(JSON.parse(ev.data).devices);
  es.onerror = () => {
    document.getElementById("footStatus").textContent = "重连中…";
  };
}

function fmt(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

function startClock() {
  const clk = document.getElementById("clock");
  setInterval(() => {
    clk.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  }, 1000);
}

// 首屏先拉一次，避免等 SSE 第一帧
fetch("/api/fleet").then((r) => r.json()).then((j) => applyFleet(j.devices)).catch(() => {});
startClock();
connect();
