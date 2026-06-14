"""历史数据记录器 + 数据集构建器 —— 为物理 AI 建模采集/筛选训练数据。

把设备群里所有设备、所有点位，按固定间隔做**时间对齐**的快照，
每个 tick 产出一行多变量样本（宽表），既留在内存环形缓冲供按需导出，
也持续追加到磁盘 CSV 作为完整历史。

数据集构建（研究员选择训练数据）：
  * 按设备 / 按点位（特征列）筛选
  * 按时间范围（最近 N 分钟 / 全部）+ 降采样（每 N 个取 1）
  * 自动合成标签列（无需手工打标）：
      label_alarm          —— 任一报警点(名称含 alarm 的 0/1 点)激活 = 1
      label_out_of_range   —— 任一模拟量越出配置量程 [min,max] = 1
      label_fault__<设备>  —— 按设备分别出故障标签（多设备多任务学习用）
  * 滑窗特征：对每个模拟量特征追加 __mean/__std/__diff 滑窗统计列（省去研究员预处理）
  * 导出格式：宽表 CSV / 长表 CSV / Parquet / ZIP 包(全都打包+元数据+README)

为什么适合 ML 训练：
  * 宽表（每行一时刻，每列一 device.point）= 直接可用的特征矩阵
  * 所有设备同一 tick 采样 → 时间戳天然对齐，无需重采样
  * 工程值 + 量程元数据 + 合成标签 → 节能/控制/故障诊断开箱即用
"""
from __future__ import annotations

import collections
import csv
import io
import json
import logging
import threading
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

log = logging.getLogger("recorder")

if TYPE_CHECKING:
    from web.app import FleetHub

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    PARQUET_AVAILABLE = True
except ImportError:
    PARQUET_AVAILABLE = False


class DatasetRecorder:
    """按固定间隔记录设备群对齐快照，支持按需筛选/打标/导出训练数据集。"""

    def __init__(self, fleet: "FleetHub", interval: float = 2.0,
                 out_dir: str | Path | None = None, max_rows: int = 30000,
                 to_disk: bool = True):
        self.fleet = fleet
        self.interval = interval
        self.max_rows = max_rows
        self.to_disk = to_disk
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._rows: collections.deque = collections.deque(maxlen=max_rows)
        self._started_at: float | None = None

        # 列定义（顺序固定）：device.point -> 元数据
        self.columns: list[str] = []
        self.col_meta: dict[str, dict] = {}
        self._col_index: dict[str, int] = {}
        for name in fleet.order:
            cfg = fleet.hubs[name].config
            for p in cfg.points:
                col = f"{name}.{p.name}"
                self._col_index[col] = len(self.columns)
                self.columns.append(col)
                self.col_meta[col] = {
                    "column": col, "device": name, "point": p.name,
                    "description": p.description, "unit": p.unit,
                    "register_type": p.register_type, "is_bit": p.is_bit,
                    "min": p.min, "max": p.max,
                }

        self._disk_path: Path | None = None
        if to_disk:
            base = Path(out_dir) if out_dir else (
                Path(__file__).resolve().parent.parent / "data" / "recordings")
            base.mkdir(parents=True, exist_ok=True)
            self._disk_path = base / f"recording_{int(time.time())}.csv"

    # ================= 采集 ============================================= #
    def start(self) -> None:
        self._started_at = time.time()
        if self._disk_path:
            with open(self._disk_path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["timestamp_iso", "timestamp_unix"] + self.columns)
        threading.Thread(target=self._run, name="recorder", daemon=True).start()
        log.info("dataset recorder started: %d 列, 间隔 %.1fs%s",
                 len(self.columns), self.interval,
                 f", 落盘 {self._disk_path}" if self._disk_path else "")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._capture()
            except Exception:
                log.exception("capture failed")

    def _capture(self) -> None:
        ts = time.time()
        snaps = {name: self.fleet.hubs[name].snapshot()["readings"]
                 for name in self.fleet.order}
        values: list = []
        for col in self.columns:
            m = self.col_meta[col]
            r = snaps.get(m["device"], {}).get(m["point"])
            values.append(r["value"] if r else None)
        with self._lock:
            self._rows.append((ts, values))
        if self._disk_path:
            with open(self._disk_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([_iso(ts), round(ts, 3)] + _csv_vals(values))

    # ================= 状态 / 模式 ===================================== #
    def stats(self) -> dict:
        with self._lock:
            n = len(self._rows)
            first = self._rows[0][0] if n else None
            last = self._rows[-1][0] if n else None
        span = (last - first) if (first and last) else 0
        return {
            "recording": not self._stop.is_set(),
            "interval": self.interval, "columns": len(self.columns),
            "devices": len(self.fleet.order), "rows": n,
            "span_seconds": round(span, 1), "span_human": _human(span),
            "started_at": self._started_at,
            "disk_path": str(self._disk_path) if self._disk_path else None,
            "parquet_available": PARQUET_AVAILABLE,
            "disk_available": self._disk_has_data(),
        }

    def _disk_has_data(self) -> bool:
        d = self._recordings_dir()
        return bool(d and d.exists() and next(iter(d.glob("recording_*.csv")), None))

    def schema(self) -> dict:
        """按设备分组的点位清单，供前端构建数据集选择界面。"""
        devices = []
        for name in self.fleet.order:
            cfg = self.fleet.hubs[name].config
            pts = []
            for p in cfg.points:
                col = f"{name}.{p.name}"
                pts.append({
                    "column": col, "point": p.name, "description": p.description,
                    "unit": p.unit, "register_type": p.register_type,
                    "is_bit": p.is_bit, "min": p.min, "max": p.max,
                })
            devices.append({"device": name,
                            "type": self.fleet.types.get(name, ""),
                            "points": pts})
        return {"devices": devices, "label_options": ["alarm", "out_of_range"]}

    def series(self, columns=None, minutes=None, max_points=300, source="memory") -> dict:
        """返回所选列的历史时间序列（供图表展示）。

        source=memory 读内存近期缓冲；source=disk 读 data/recordings 完整历史。
        按需抽样到 <= max_points 点，避免图表过重。
        返回: {labels:[iso...], series:[{column,unit,is_bit,min,max,data:[...]}], points, span_human}
        """
        rows = self._source_rows(minutes, source)
        n = len(rows)
        stride = max(1, (n // max_points) + (1 if n % max_points else 0)) if max_points else 1
        rows = rows[::stride]

        cols = [c for c in (columns or self.columns) if c in self._col_index]
        labels = [_iso(ts) for ts, _ in rows]
        out = []
        for c in cols:
            i = self._col_index[c]
            m = self.col_meta[c]
            out.append({
                "column": c, "device": m["device"], "point": m["point"],
                "unit": m["unit"], "is_bit": m["is_bit"], "min": m["min"], "max": m["max"],
                "data": [r[1][i] for r in rows],
            })
        span = (rows[-1][0] - rows[0][0]) if len(rows) > 1 else 0
        return {"labels": labels, "series": out, "points": len(rows),
                "span_human": _human(span), "stride": stride}

    def metadata(self, sel: "Selection") -> dict:
        feat, label_cols, rows = self._build(sel)
        md = {
            "schema_version": 3,
            "sampling_interval_seconds": self.interval,
            "downsample": sel.downsample,
            "effective_interval_seconds": round(self.interval * sel.downsample, 3),
            "window_samples": sel.window,
            "window_stats": sel.window_stats if sel.window >= 2 else [],
            "aligned": True, "value_kind": "engineering",
            "exported_rows": len(rows),
            "feature_columns": [self._feat_def(c) for c in feat],
            "label_columns": [self._label_def(lc) for lc in label_cols],
            "window_minutes": sel.minutes, "devices": sel.devices or "all",
            "source": sel.source,
        }
        if sel.window >= 2:
            md["window_note"] = (f"每个模拟量特征附加 {'/'.join('__' + s for s in sel.window_stats)} "
                                 f"滑窗统计（窗口 {sel.window} 个样本）")
        if sel.split:
            parts = _partition(rows, sel.split)
            md["split"] = {"ratios": {"train": sel.split[0], "val": sel.split[1], "test": sel.split[2]},
                           "rows": {k: len(v) for k, v in parts.items()},
                           "order": "时间顺序（train 最早，test 最新）",
                           "layout": "train/ val/ test/ 各一个子目录"}
        if rows:
            md["time_start"] = _iso(rows[0][0])
            md["time_end"] = _iso(rows[-1][0])
        return md

    def _feat_def(self, col: str) -> dict:
        if col in self.col_meta:
            return self.col_meta[col]
        for s in WINDOW_STATS:  # 滑窗派生特征 <base>__<stat>
            suf = f"__{s}"
            if col.endswith(suf):
                bm = self.col_meta.get(col[: -len(suf)], {})
                return {"column": col, "device": bm.get("device"), "point": bm.get("point"),
                        "unit": bm.get("unit"), "register_type": bm.get("register_type"),
                        "is_bit": False, "min": None, "max": None, "derived": s}
        return {"column": col}

    @staticmethod
    def _label_def(col: str) -> dict:
        if col in _LABEL_DEFS:
            return _LABEL_DEFS[col]
        if col.startswith("label_fault__"):
            dev = col.split("__", 1)[1]
            return {"column": col, "type": "binary",
                    "definition": f"设备 {dev} 任一报警激活 或 任一模拟量越限时为 1"}
        return {"column": col, "type": "binary", "definition": ""}

    # ================= 选择 + 标签 ===================================== #
    def _columns_for(self, devices, columns) -> list[str]:
        if columns:
            want = set(columns)
            return [c for c in self.columns if c in want]
        if devices:
            ds = set(devices)
            return [c for c in self.columns if self.col_meta[c]["device"] in ds]
        return list(self.columns)

    def _label_sources_by_device(self, devices):
        """按设备汇总报警点 / 可越限模拟量点的列索引。"""
        ds = set(devices) if devices else None
        dev_alarm: dict[str, list] = {}
        dev_oor: dict[str, list] = {}
        for i, c in enumerate(self.columns):
            m = self.col_meta[c]
            if ds and m["device"] not in ds:
                continue
            if m["is_bit"] and "alarm" in m["point"].lower():
                dev_alarm.setdefault(m["device"], []).append(i)
            elif not m["is_bit"] and (m["min"] is not None or m["max"] is not None):
                dev_oor.setdefault(m["device"], []).append((i, m["min"], m["max"]))
        return dev_alarm, dev_oor

    @staticmethod
    def _any_alarm(vals, idxs):
        return 1 if any(vals[i] is not None and vals[i] >= 1 for i in idxs) else 0

    @staticmethod
    def _any_oor(vals, idxs):
        for i, mn, mx in idxs:
            x = vals[i]
            if x is None:
                continue
            if (mn is not None and x < mn) or (mx is not None and x > mx):
                return 1
        return 0

    # ---- 数据来源：内存近期 / 磁盘完整历史 ---------------------------- #
    def _recordings_dir(self) -> Path | None:
        if self._disk_path:
            return self._disk_path.parent
        d = Path(__file__).resolve().parent.parent / "data" / "recordings"
        return d if d.exists() else None

    def _read_disk(self, minutes=None) -> list:
        """从 data/recordings/*.csv 读历史，按列名对齐到当前列顺序。"""
        d = self._recordings_dir()
        if not d or not d.exists():
            return []
        cutoff = (time.time() - minutes * 60) if minutes else None
        name_to_pos = {c: i for i, c in enumerate(self.columns)}
        rows = []
        for f in sorted(d.glob("recording_*.csv")):
            try:
                with open(f, newline="", encoding="utf-8") as fh:
                    r = csv.reader(fh)
                    header = next(r, None)
                    if not header or len(header) < 2:
                        continue
                    # 文件列 -> 当前列索引（按列名映射，兼容配置变化）
                    idx_map = [(j, name_to_pos[c]) for j, c in enumerate(header[2:])
                               if c in name_to_pos]
                    for row in r:
                        if len(row) < 2:
                            continue
                        try:
                            ts = float(row[1])
                        except ValueError:
                            continue
                        if cutoff and ts < cutoff:
                            continue
                        vals = [None] * len(self.columns)
                        data = row[2:]
                        for j, ci in idx_map:
                            if j < len(data) and data[j] != "":
                                vals[ci] = _num(data[j])
                        rows.append((ts, vals))
            except OSError:
                continue
        rows.sort(key=lambda r: r[0])
        return rows

    def _source_rows(self, minutes, source) -> list:
        if source == "disk":
            return self._read_disk(minutes)
        with self._lock:
            rows = list(self._rows)
        if minutes:
            cutoff = time.time() - minutes * 60
            rows = [r for r in rows if r[0] >= cutoff]
        return rows

    def disk_stats(self) -> dict:
        """磁盘历史概要（供「数据来源」开关展示可回看时长）。"""
        d = self._recordings_dir()
        files = sorted(d.glob("recording_*.csv")) if d and d.exists() else []
        rows, first, last = 0, None, None
        for f in files:
            try:
                with open(f, encoding="utf-8") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            if len(lines) <= 1:
                continue
            rows += len(lines) - 1
            for parse_line, keep in ((lines[1], "first"), (lines[-1], "last")):
                try:
                    ts = float(parse_line.split(",", 2)[1])
                except (IndexError, ValueError):
                    continue
                if keep == "first" and (first is None or ts < first):
                    first = ts
                if keep == "last" and (last is None or ts > last):
                    last = ts
        span = (last - first) if (first and last) else 0
        return {
            "available": bool(files), "files": len(files), "rows": rows,
            "span_human": _human(span),
            "earliest": _iso(first) if first else None,
            "latest": _iso(last) if last else None,
        }

    def _build(self, sel: "Selection"):
        """核心：按选择产出 (特征列, 标签列, 行)。行 = (ts, 特征值[], 标签值[])。"""
        rows = self._source_rows(sel.minutes, sel.source)
        if sel.downsample and sel.downsample > 1:
            rows = rows[:: int(sel.downsample)]

        feat = self._columns_for(sel.devices, sel.columns)
        feat_idx = [self._col_index[c] for c in feat]

        # ---- 标签列 ---- #
        need_labels = sel.label_alarm or sel.label_out_of_range or sel.label_per_device
        dev_alarm, dev_oor = self._label_sources_by_device(sel.devices) if need_labels else ({}, {})
        all_alarm = [i for v in dev_alarm.values() for i in v]
        all_oor = [t for v in dev_oor.values() for t in v]
        per_devs = (sel.devices or list(self.fleet.order)) if sel.label_per_device else []

        label_cols = []
        if sel.label_alarm:
            label_cols.append("label_alarm")
        if sel.label_out_of_range:
            label_cols.append("label_out_of_range")
        for d in per_devs:
            label_cols.append(f"label_fault__{d}")

        base = []
        for ts, vals in rows:
            fv = [vals[i] for i in feat_idx]
            lv = []
            if sel.label_alarm:
                lv.append(self._any_alarm(vals, all_alarm))
            if sel.label_out_of_range:
                lv.append(self._any_oor(vals, all_oor))
            for d in per_devs:  # 该设备：任一报警激活 或 任一模拟量越限
                fault = (self._any_alarm(vals, dev_alarm.get(d, []))
                         or self._any_oor(vals, dev_oor.get(d, [])))
                lv.append(fault)
            base.append((ts, fv, lv))

        # ---- 滑窗特征（对模拟量特征算所选统计量） ---- #
        if sel.window and sel.window >= 2:
            feat, base = self._augment_window(feat, base, sel.window, sel.window_stats)
        return feat, label_cols, base

    def _augment_window(self, feat, rows, window, stats):
        """对每个模拟量特征追加所选滑窗统计列：__mean/__std/__min/__max/__diff/__slope/__range。"""
        analog_j = [j for j, c in enumerate(feat) if not self.col_meta[c]["is_bit"]]
        if not analog_j:
            return feat, rows
        new_cols = []
        for j in analog_j:
            new_cols += [f"{feat[j]}__{s}" for s in stats]

        aug = []
        for i in range(len(rows)):
            ts, fv, lv = rows[i]
            extra = []
            lo = max(0, i - window + 1)
            for j in analog_j:
                win = [rows[k][1][j] for k in range(lo, i + 1) if rows[k][1][j] is not None]
                prev = rows[i - 1][1][j] if i > 0 else None
                vals = _window_stats(win, fv[j], prev, stats)
                extra += [_round(v) for v in vals]
            aug.append((ts, fv + extra, lv))
        return feat + new_cols, aug

    # ================= 导出 ============================================ #
    def _wide_csv(self, feat, label_cols, rows) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["timestamp_iso", "timestamp_unix"] + feat + label_cols)
        for ts, fv, lv in rows:
            w.writerow([_iso(ts), round(ts, 3)] + _csv_vals(fv) + lv)
        return buf.getvalue()

    def _long_csv(self, feat, rows) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["timestamp_iso", "timestamp_unix", "device", "point", "value", "unit", "type"])
        for ts, fv, _lv in rows:
            iso, unix = _iso(ts), round(ts, 3)
            for col, v in zip(feat, fv):
                if v is None or col not in self.col_meta:  # 跳过滑窗派生列（长表只放原始点）
                    continue
                m = self.col_meta[col]
                w.writerow([iso, unix, m["device"], m["point"], v, m["unit"], m["register_type"]])
        return buf.getvalue()

    def _parquet_bytes(self, feat, label_cols, rows) -> bytes:
        if not PARQUET_AVAILABLE:
            raise RuntimeError("pyarrow 未安装：pip install pyarrow")
        data = {
            "timestamp_iso": [_iso(ts) for ts, _, _ in rows],
            "timestamp_unix": [round(ts, 3) for ts, _, _ in rows],
        }
        for j, col in enumerate(feat):
            data[col] = [r[1][j] for r in rows]
        for k, lc in enumerate(label_cols):
            data[lc] = [r[2][k] for r in rows]
        sink = io.BytesIO()
        pq.write_table(pa.table(data), sink, compression="snappy")
        return sink.getvalue()

    def export_wide_csv(self, minutes=None, **kw) -> str:
        feat, label_cols, rows = self._build(Selection.make(minutes, **kw))
        return self._wide_csv(feat, label_cols, rows)

    def export_long_csv(self, minutes=None, **kw) -> str:
        feat, _, rows = self._build(Selection.make(minutes, **kw))
        return self._long_csv(feat, rows)

    def export_parquet(self, minutes=None, **kw) -> bytes:
        feat, label_cols, rows = self._build(Selection.make(minutes, **kw))
        return self._parquet_bytes(feat, label_cols, rows)

    def preview(self, minutes=None, limit=8, **kw) -> dict:
        sel = Selection.make(minutes, **kw)
        feat, label_cols, rows = self._build(sel)
        head = []
        for ts, fv, lv in rows[:limit]:
            head.append([_iso(ts)] + [_round(v) for v in fv] + lv)
        return {
            "rows": len(rows),
            "feature_columns": feat,
            "label_columns": label_cols,
            "n_columns": len(feat) + len(label_cols),
            "header": ["timestamp_iso"] + feat + label_cols,
            "sample": head,
            "effective_interval": round(self.interval * sel.downsample, 3),
        }

    def export_zip(self, minutes=None, include_long=True, main_format="csv", **kw) -> bytes:
        sel = Selection.make(minutes, **kw)
        feat, label_cols, rows = self._build(sel)
        want_parquet = main_format == "parquet" and PARQUET_AVAILABLE
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            if sel.split:
                # 按时间顺序切分 train/val/test，各自一个子目录
                for name, part in _partition(rows, sel.split).items():
                    z.writestr(f"{name}/dataset_wide.csv", self._wide_csv(feat, label_cols, part))
                    if want_parquet:
                        z.writestr(f"{name}/dataset_wide.parquet",
                                   self._parquet_bytes(feat, label_cols, part))
            else:
                if want_parquet:
                    z.writestr("dataset_wide.parquet", self._parquet_bytes(feat, label_cols, rows))
                z.writestr("dataset_wide.csv", self._wide_csv(feat, label_cols, rows))
                if include_long:
                    z.writestr("dataset_long.csv", self._long_csv(feat, rows))
            md = self.metadata(sel)
            z.writestr("metadata.json", json.dumps(md, ensure_ascii=False, indent=2))
            z.writestr("README.txt", _readme(md))
        return out.getvalue()


WINDOW_STATS = ("mean", "std", "min", "max", "diff", "slope", "range")


class Selection:
    """一次数据集导出的选择条件。"""

    __slots__ = ("minutes", "devices", "columns", "downsample",
                 "label_alarm", "label_out_of_range", "label_per_device",
                 "window", "window_stats", "split", "source")

    def __init__(self, minutes=None, devices=None, columns=None, downsample=1,
                 label_alarm=False, label_out_of_range=False,
                 label_per_device=False, window=0, window_stats=None, split=None,
                 source="memory"):
        self.minutes = minutes
        self.devices = devices or None
        self.columns = columns or None
        self.downsample = int(downsample) if downsample else 1
        self.label_alarm = bool(label_alarm)
        self.label_out_of_range = bool(label_out_of_range)
        self.label_per_device = bool(label_per_device)   # 按设备分别出故障标签
        self.window = int(window) if window else 0        # 滑窗特征窗口（样本数，0=关）
        # 滑窗统计量（仅 window>0 时生效），过滤掉非法项，保持声明顺序
        ws = window_stats or ["mean", "std", "diff"]
        self.window_stats = [s for s in WINDOW_STATS if s in set(ws)] or ["mean"]
        self.split = _parse_split(split)                  # None 或 (train,val,test) 比例
        self.source = "disk" if source == "disk" else "memory"  # 数据来源：内存近期 / 磁盘完整历史

    @classmethod
    def make(cls, minutes=None, **kw):
        labels = kw.get("labels") or {}
        return cls(
            minutes=minutes,
            devices=kw.get("devices"),
            columns=kw.get("columns"),
            downsample=kw.get("downsample", 1),
            label_alarm=kw.get("label_alarm", labels.get("alarm", False)),
            label_out_of_range=kw.get("label_out_of_range", labels.get("out_of_range", False)),
            label_per_device=kw.get("label_per_device", labels.get("per_device", False)),
            window=kw.get("window", 0),
            window_stats=kw.get("window_stats"),
            split=kw.get("split"),
            source=kw.get("source", "memory"),
        )


_LABEL_DEFS = {
    "label_alarm": {"column": "label_alarm", "type": "binary",
                    "definition": "任一报警点(名称含 alarm 的 0/1 点)激活时为 1"},
    "label_out_of_range": {"column": "label_out_of_range", "type": "binary",
                           "definition": "任一模拟量越出配置量程 [min,max] 时为 1"},
}


# ---------------------------------------------------------------------- #
def _window_stats(win: list, cur, prev, stats: list) -> list:
    """按 stats 顺序返回滑窗统计量列表。win=窗口内非空值，cur=当前值，prev=上一样本值。"""
    out = []
    n = len(win)
    mean = sum(win) / n if n else None
    for s in stats:
        if s == "mean":
            out.append(mean)
        elif s == "std":
            out.append((sum((x - mean) ** 2 for x in win) / n) ** 0.5 if n > 1 else (0.0 if n else None))
        elif s == "min":
            out.append(min(win) if n else None)
        elif s == "max":
            out.append(max(win) if n else None)
        elif s == "range":
            out.append((max(win) - min(win)) if n else None)
        elif s == "diff":
            out.append((cur - prev) if (cur is not None and prev is not None) else None)
        elif s == "slope":  # 窗口内对样本序号的最小二乘斜率（趋势/样本）
            if n > 1:
                xs = range(n)
                mx = (n - 1) / 2
                denom = sum((x - mx) ** 2 for x in xs)
                slope = sum((x - mx) * (y - mean) for x, y in zip(xs, win)) / denom if denom else 0.0
                out.append(slope)
            else:
                out.append(0.0 if n else None)
        else:
            out.append(None)
    return out


def _parse_split(split):
    """把 split 规格解析为 (train,val,test) 比例；无效/关闭返回 None。

    支持 '70/15/15'、'80-10-10'、[0.7,0.15,0.15]、{'train':..,'val':..,'test':..}。
    """
    if not split or split in ("off", "none", "0"):
        return None
    parts = None
    if isinstance(split, str):
        for sep in ("/", "-", ",", ":"):
            if sep in split:
                parts = split.split(sep)
                break
    elif isinstance(split, (list, tuple)):
        parts = list(split)
    elif isinstance(split, dict):
        parts = [split.get("train", 0), split.get("val", 0), split.get("test", 0)]
    if not parts or len(parts) != 3:
        return None
    try:
        nums = [float(x) for x in parts]
    except (TypeError, ValueError):
        return None
    total = sum(nums)
    if total <= 0:
        return None
    return tuple(round(x / total, 4) for x in nums)   # 归一化到比例


def _partition(rows: list, split) -> dict:
    """按时间顺序把 rows 切成 train/val/test（train 最早）。"""
    n = len(rows)
    n_tr = int(n * split[0])
    n_va = int(n * split[1])
    return {
        "train": rows[:n_tr],
        "val": rows[n_tr:n_tr + n_va],
        "test": rows[n_tr + n_va:],
    }


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) + f".{int((ts % 1) * 1000):03d}"


def _csv_vals(vals: list) -> list:
    return ["" if v is None else v for v in vals]


def _round(v):
    return None if v is None else (v if isinstance(v, int) else round(v, 3))


def _num(x: str):
    """CSV 字符串 -> 数值（整数形保留 int，其余 float）。"""
    try:
        if "." in x or "e" in x or "E" in x:
            return float(x)
        return int(x)
    except ValueError:
        try:
            return float(x)
        except ValueError:
            return None


def _human(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60}s"
    return f"{sec // 3600}h{(sec % 3600) // 60}m"


def _split_readme(meta: dict) -> str:
    sp = meta.get("split")
    if not sp:
        return "关（单一 dataset_wide）"
    r, rows = sp["ratios"], sp["rows"]
    return (f"train/val/test = {r['train']}/{r['val']}/{r['test']} "
            f"({rows['train']}/{rows['val']}/{rows['test']} 行)，按时间顺序，分目录存放")


def _readme(meta: dict) -> str:
    feat = meta["feature_columns"]
    labels = meta["label_columns"]
    sample_cols = ", ".join(c["column"] for c in feat[:4])
    label_txt = ("\n".join(f"    {d['column']:<20} {d['definition']}" for d in labels)
                 if labels else "    （本次未附加标签）")
    return f"""现场设备训练数据集 (Field Device Training Dataset)
====================================================

由 Niagara 风格现场设备模拟器导出，供物理 AI / 机器学习建模使用
（节能优化、控制策略、故障诊断等）。

文件
----
- dataset_wide.csv / .parquet  宽表特征矩阵：每行一个时间对齐样本，列为各 device.point 工程值
                               （末尾可含 label_* 标签列）。这是直接可用的训练矩阵。
- dataset_long.csv             长表：每行一个 (时间, 设备, 点位, 值)。
- metadata.json                列定义/单位/量程/采样/标签定义。

本次导出
--------
- 采样间隔 : {meta['sampling_interval_seconds']}s  x 降采样 {meta['downsample']} = 有效 {meta['effective_interval_seconds']}s
- 样本行数 : {meta['exported_rows']}
- 特征列数 : {len(feat)}   设备: {meta['devices']}
- 滑窗特征 : {('窗口 %d 个样本，每个模拟量含 %s' % (meta['window_samples'], '/'.join('__' + s for s in meta['window_stats']))) if meta.get('window_samples', 0) >= 2 else '关'}
- 数据集划分 : {_split_readme(meta)}
- 标签列   :
{label_txt}

快速加载（pandas）
------------------
    import pandas as pd
    df = pd.read_csv("dataset_wide.csv", parse_dates=["timestamp_iso"])
    df = df.set_index("timestamp_iso").ffill()
    # 或 Parquet:  df = pd.read_parquet("dataset_wide.parquet")
    # 示例特征列：{sample_cols} ...
    y = df["label_alarm"]    if "label_alarm" in df else None   # 故障诊断标签
    X = df[[c for c in df.columns if not c.startswith("label_")]]

建模提示
--------
- 节能/控制：以可写点(*_setpoint, *_cmd)为动作、其余为状态，构造状态-动作-反馈样本。
- 故障诊断：用 label_alarm / label_out_of_range 作监督标签；或基于 metadata 量程自定义。
- 物理约束：metadata.json 的 min/max 量程可用于物理可行性约束 / 归一化。
"""
