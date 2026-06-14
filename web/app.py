"""实时监控前端的后端桥接服务（Flask）—— 支持多设备总览。

职责：为每台设备起一个 DataHub（Modbus 主站），后台持续轮询，把最新值与历史
缓冲保存在内存，并通过 REST + SSE 推给浏览器。FleetHub 聚合多台设备。

    浏览器 ──HTTP/SSE──> Flask(web/app.py) ──Modbus TCP──> 各设备模拟器

页面:
    GET  /                设备群总览页 (overview.html)
    GET  /device          单设备详情页 (device.html，前端按 ?device= 取数)

接口（device 省略时默认第一台，兼容单设备）:
    GET  /api/fleet           所有设备的概要（连接/报警数/KPI）
    GET  /api/fleet/stream    SSE：周期推送设备群概要
    GET  /api/meta?device=    指定设备的点位元数据
    GET  /api/history?device= 指定设备每点最近 N 个采样
    GET  /api/stream?device=  SSE：指定设备的最新读数
    POST /api/write           写可写点 {device, name, value}
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from flask import Flask, Response, jsonify, request, send_from_directory  # noqa: E402

from client.modbus_client import FieldDeviceClient  # noqa: E402
from client.recorder import DatasetRecorder  # noqa: E402
from simulator.points import load_config, load_fleet  # noqa: E402

log = logging.getLogger("web.app")

STATIC_DIR = Path(__file__).resolve().parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "points.yaml"
DEFAULT_FLEET = PROJECT_ROOT / "config" / "fleet.yaml"
HISTORY_LEN = 120  # 每个点位保留的历史采样数（约 4 分钟 @2s）


class DataHub:
    """单台设备的后台采集器 + 内存数据中心。"""

    def __init__(self, config, poll_interval: float = 1.0):
        self.config = config
        self._poll_interval = poll_interval
        self._client = FieldDeviceClient(config)
        self._lock = threading.Lock()
        self._latest: dict[str, dict] = {}
        self._history: dict[str, collections.deque] = {
            p.name: collections.deque(maxlen=HISTORY_LEN) for p in config.points
        }
        self.connected = False
        self._stop = threading.Event()
        self._seq = 0

    def start(self) -> None:
        threading.Thread(target=self._run, name=f"hub-{self.config.device_name}",
                         daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._client.close()

    def _run(self) -> None:
        backoff = self.config.reconnect_backoff
        attempt = 0
        while not self._stop.is_set():
            try:
                if not self._client.connected:
                    self._client.connect()
                    self.connected = True
                    attempt = 0
                    log.info("[%s] connected", self.config.device_name)
                self._ingest(self._client.read_all())
                time.sleep(self._poll_interval)
            except Exception as exc:
                self.connected = False
                self._client.close()
                delay = backoff[min(attempt, len(backoff) - 1)]
                log.warning("[%s] poll error: %s — reconnect in %.0fs",
                            self.config.device_name, exc, delay)
                self._stop.wait(delay)
                attempt += 1

    def _ingest(self, readings) -> None:
        ts = time.time()
        with self._lock:
            self._seq += 1
            for r in readings:
                self._latest[r.point.name] = {
                    "value": round(r.value, 3), "raw": r.raw,
                    "in_range": r.in_range, "ok": r.ok, "t": ts,
                }
                self._history[r.point.name].append({"t": round(ts, 2), "v": round(r.value, 3)})

    def snapshot(self) -> dict:
        with self._lock:
            return {"seq": self._seq, "connected": self.connected,
                    "t": time.time(), "readings": dict(self._latest)}

    def history(self) -> dict:
        with self._lock:
            return {name: list(buf) for name, buf in self._history.items()}

    def summary(self, device_type: str = "") -> dict:
        """供总览页的设备概要：连接状态 + 报警数 + 前几个 KPI。"""
        cfg = self.config
        snap = self.snapshot()
        alarms = 0
        kpis: list[dict] = []
        for p in cfg.points:
            r = snap["readings"].get(p.name)
            if not r:
                continue
            if not r["in_range"]:
                alarms += 1
            if p.is_bit and "alarm" in p.name and r["value"] >= 1:
                alarms += 1
            if not p.is_bit and "alarm" not in p.name and len(kpis) < 3:
                kpis.append({"name": p.name, "value": r["value"], "unit": p.unit})
        return {
            "device": cfg.device_name, "type": device_type,
            "host": cfg.host, "port": cfg.port,
            "connected": self.connected, "points": len(cfg.points),
            "alarms": alarms, "kpis": kpis,
        }

    def write(self, name: str, value: float) -> None:
        self._client.write_point_by_name(name, value)


class FleetHub:
    """聚合多台设备的 DataHub。"""

    def __init__(self):
        self.order: list[str] = []
        self.hubs: dict[str, DataHub] = {}
        self.types: dict[str, str] = {}

    def add(self, name: str, device_type: str, hub: DataHub) -> None:
        self.order.append(name)
        self.hubs[name] = hub
        self.types[name] = device_type

    @classmethod
    def from_members(cls, members) -> "FleetHub":
        f = cls()
        for m in members:
            f.add(m.name, m.type, DataHub(m.config, poll_interval=m.config.poll_interval))
        return f

    @classmethod
    def single(cls, config, device_type: str = "") -> "FleetHub":
        f = cls()
        f.add(config.device_name, device_type,
              DataHub(config, poll_interval=config.poll_interval))
        return f

    def start(self) -> None:
        for h in self.hubs.values():
            h.start()

    def stop(self) -> None:
        for h in self.hubs.values():
            h.stop()

    def get(self, name: str | None) -> DataHub:
        """按名取 DataHub；name 为空时返回第一台（兼容单设备）。"""
        if name and name in self.hubs:
            return self.hubs[name]
        return self.hubs[self.order[0]]

    def fleet_summary(self) -> list[dict]:
        return [self.hubs[n].summary(self.types[n]) for n in self.order]


def create_app(fleet: FleetHub, recorder: DatasetRecorder | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)

    # 历史数据记录器：随平台启动持续记录，供一键导出训练数据集
    if recorder is None:
        recorder = DatasetRecorder(fleet)
        recorder.start()
    app.recorder = recorder  # 便于测试访问

    @app.get("/")
    def overview():
        return send_from_directory(STATIC_DIR, "overview.html")

    @app.get("/device")
    def device():
        return send_from_directory(STATIC_DIR, "device.html")

    @app.get("/dataset")
    def dataset():
        return send_from_directory(STATIC_DIR, "dataset.html")

    @app.get("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename)

    # ---- 设备群概要 --------------------------------------------------- #
    @app.get("/api/fleet")
    def api_fleet():
        return jsonify({"devices": fleet.fleet_summary()})

    @app.get("/api/fleet/stream")
    def api_fleet_stream():
        def gen():
            while True:
                yield f"data: {json.dumps({'devices': fleet.fleet_summary()})}\n\n"
                time.sleep(1.0)
        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- 单设备 ------------------------------------------------------- #
    @app.get("/api/meta")
    def api_meta():
        hub = fleet.get(request.args.get("device"))
        cfg = hub.config
        return jsonify({
            "device": cfg.device_name, "host": cfg.host, "port": cfg.port,
            "poll_interval": cfg.poll_interval,
            "points": [{
                "name": p.name, "description": p.description, "unit": p.unit,
                "register_type": p.register_type, "address": p.address,
                "writable": p.writable, "is_bit": p.is_bit,
                "min": p.min, "max": p.max, "initial": p.initial,
                "scale": p.scale, "sim_mode": p.simulation.mode,
            } for p in cfg.points],
        })

    @app.get("/api/history")
    def api_history():
        return jsonify(fleet.get(request.args.get("device")).history())

    @app.get("/api/stream")
    def api_stream():
        hub = fleet.get(request.args.get("device"))

        def gen():
            last_seq = -1
            while True:
                snap = hub.snapshot()
                if snap["seq"] != last_seq:
                    last_seq = snap["seq"]
                    yield f"data: {json.dumps(snap)}\n\n"
                time.sleep(0.5)
        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/write")
    def api_write():
        body = request.get_json(force=True)
        name, value = body.get("name"), body.get("value")
        if name is None or value is None:
            return jsonify({"ok": False, "error": "name 与 value 必填"}), 400
        try:
            fleet.get(body.get("device")).write(name, float(value))
            return jsonify({"ok": True, "name": name, "value": value})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    # ---- 历史数据集 --------------------------------------------------- #
    @app.get("/api/dataset/stats")
    def api_dataset_stats():
        s = recorder.stats()
        s["columns_preview"] = recorder.columns[:8]
        return jsonify(s)

    @app.get("/api/dataset/schema")
    def api_dataset_schema():
        """按设备分组的点位清单，供前端构建数据集选择界面。"""
        return jsonify(recorder.schema())

    def _selection_from(src) -> dict:
        """从 query 或 JSON body 解析数据集选择条件。"""
        minutes = src.get("minutes")
        minutes = float(minutes) if minutes not in (None, "", "all") else None
        downsample = int(src.get("downsample") or 1)
        labels = src.get("labels") or {}
        if isinstance(labels, list):  # query 形式 labels=alarm,out_of_range
            labels = {k: True for k in labels}
        win_stats = src.get("window_stats")
        if isinstance(win_stats, str):  # query 形式 window_stats=mean,std
            win_stats = [s for s in win_stats.split(",") if s]
        return {
            "minutes": minutes,
            "devices": src.get("devices") or None,
            "columns": src.get("columns") or None,
            "downsample": downsample,
            "labels": labels,
            "window": int(src.get("window") or 0),
            "window_stats": win_stats or None,
            "split": src.get("split") or None,
            "source": src.get("source") or "memory",
        }

    @app.post("/api/dataset/preview")
    def api_dataset_preview():
        """按选择预览：行数、列、前若干行样例（不下载）。"""
        sel = _selection_from(request.get_json(force=True, silent=True) or {})
        minutes = sel.pop("minutes")
        return jsonify(recorder.preview(minutes, **sel))

    @app.post("/api/dataset/series")
    def api_dataset_series():
        """返回所选列的历史时间序列，供构建器里画趋势图。"""
        body = request.get_json(force=True, silent=True) or {}
        minutes = body.get("minutes")
        minutes = float(minutes) if minutes not in (None, "", "all") else None
        return jsonify(recorder.series(
            columns=body.get("columns") or None,
            minutes=minutes,
            max_points=int(body.get("max_points") or 300),
            source=body.get("source") or "memory",
        ))

    @app.get("/api/dataset/disk")
    def api_dataset_disk():
        """磁盘历史概要（点「磁盘」来源时展示可回看时长）。"""
        return jsonify(recorder.disk_stats())

    @app.post("/api/dataset/build")
    def api_dataset_build():
        """按选择构建并下载训练数据集。

        body: {minutes, devices[], columns[], downsample, labels{alarm,out_of_range}, format}
        format: zip(默认) | wide | long | parquet
        """
        body = request.get_json(force=True, silent=True) or {}
        sel = _selection_from(body)
        minutes = sel.pop("minutes")
        fmt = body.get("format", "zip")
        tag = f"last{int(minutes)}min" if minutes else "all"
        try:
            if fmt == "wide":
                data = recorder.export_wide_csv(minutes, **sel)
                return Response(data, mimetype="text/csv", headers={
                    "Content-Disposition": f"attachment; filename=dataset_wide_{tag}.csv"})
            if fmt == "long":
                data = recorder.export_long_csv(minutes, **sel)
                return Response(data, mimetype="text/csv", headers={
                    "Content-Disposition": f"attachment; filename=dataset_long_{tag}.csv"})
            if fmt == "parquet":
                blob = recorder.export_parquet(minutes, **sel)
                return Response(blob, mimetype="application/octet-stream", headers={
                    "Content-Disposition": f"attachment; filename=dataset_wide_{tag}.parquet"})
            blob = recorder.export_zip(minutes, main_format=body.get("main_format", "csv"), **sel)
            return Response(blob, mimetype="application/zip", headers={
                "Content-Disposition": f"attachment; filename=field_dataset_{tag}.zip"})
        except RuntimeError as exc:  # 如 pyarrow 未装
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/dataset/export")
    def api_dataset_export():
        """快速导出（GET，兼容旧接口）：minutes, format=wide|long|zip|parquet。"""
        sel = _selection_from(request.args.to_dict())
        minutes = sel.pop("minutes")
        fmt = request.args.get("format", "zip")
        tag = f"last{int(minutes)}min" if minutes else "all"
        if fmt == "wide":
            return Response(recorder.export_wide_csv(minutes, **sel), mimetype="text/csv",
                            headers={"Content-Disposition": f"attachment; filename=dataset_wide_{tag}.csv"})
        if fmt == "long":
            return Response(recorder.export_long_csv(minutes, **sel), mimetype="text/csv",
                            headers={"Content-Disposition": f"attachment; filename=dataset_long_{tag}.csv"})
        if fmt == "parquet":
            return Response(recorder.export_parquet(minutes, **sel), mimetype="application/octet-stream",
                            headers={"Content-Disposition": f"attachment; filename=dataset_wide_{tag}.parquet"})
        return Response(recorder.export_zip(minutes, **sel), mimetype="application/zip",
                        headers={"Content-Disposition": f"attachment; filename=field_dataset_{tag}.zip"})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="现场设备实时监控 Web 服务（多设备）")
    parser.add_argument("--fleet", default=str(DEFAULT_FLEET),
                        help="设备群配置；不存在则回退单设备 --config")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="单设备点表（仅在无 fleet 时使用）")
    parser.add_argument("--web-host", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8000)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stdout)

    if Path(args.fleet).exists():
        fleet = FleetHub.from_members(load_fleet(args.fleet))
    else:
        fleet = FleetHub.single(load_config(args.config))
    fleet.start()

    app = create_app(fleet)
    print(f"监控总览: http://{args.web_host}:{args.web_port}  "
          f"({len(fleet.order)} 台设备: {', '.join(fleet.order)})")
    app.run(host=args.web_host, port=args.web_port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
