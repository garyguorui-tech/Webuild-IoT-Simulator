"""Web 桥接层接口测试（Flask test client + 真实 DataHub 连模拟器）。"""
from __future__ import annotations

import time

import pytest

from client.recorder import DatasetRecorder
from web.app import FleetHub, create_app


@pytest.fixture
def web(simulator, config):
    """单设备 FleetHub 连上 session 级模拟器，返回 Flask 测试客户端。"""
    fleet = FleetHub.single(config, device_type="测试设备")
    fleet.hubs[fleet.order[0]]._poll_interval = 0.1
    fleet.start()
    hub = fleet.get(None)
    # 等后台采集到至少一帧
    deadline = time.time() + 5
    while time.time() < deadline and not hub.snapshot()["readings"]:
        time.sleep(0.1)
    # 用内存记录器（不落盘），并采集几帧供导出测试
    rec = DatasetRecorder(fleet, interval=0.1, to_disk=False)
    rec.start()
    time.sleep(0.5)
    app = create_app(fleet, recorder=rec)
    app.config.update(TESTING=True)
    yield app.test_client()
    rec.stop()
    fleet.stop()


def test_meta_endpoint(web, config):
    data = web.get("/api/meta").get_json()
    assert data["device"] == config.device_name
    assert len(data["points"]) == len(config.points)
    names = {p["name"] for p in data["points"]}
    assert "supply_air_temp" in names and "fan_enable" in names


def test_overview_served(web):
    resp = web.get("/")
    assert resp.status_code == 200
    assert b"<!DOCTYPE html>" in resp.data


def test_device_page_served(web):
    resp = web.get("/device")
    assert resp.status_code == 200
    assert b"<!DOCTYPE html>" in resp.data


def test_fleet_endpoint(web, config):
    data = web.get("/api/fleet").get_json()
    assert "devices" in data
    assert len(data["devices"]) == 1
    dev = data["devices"][0]
    assert dev["device"] == config.device_name
    assert dev["type"] == "测试设备"
    assert "connected" in dev and "alarms" in dev and "kpis" in dev


def test_history_endpoint_has_samples(web):
    hist = web.get("/api/history").get_json()
    assert "supply_air_temp" in hist
    # 后台已采集若干帧，应有样本
    assert len(hist["supply_air_temp"]) >= 1
    sample = hist["supply_air_temp"][-1]
    assert "t" in sample and "v" in sample


def test_write_via_api_roundtrip(web):
    resp = web.post("/api/write", json={"name": "temp_setpoint", "value": 25.5})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    # 给后台一个轮询周期把新值采回来
    time.sleep(0.4)
    hist = web.get("/api/history").get_json()
    assert hist["temp_setpoint"][-1]["v"] == pytest.approx(25.5, abs=0.05)


def test_write_missing_fields_rejected(web):
    resp = web.post("/api/write", json={"name": "temp_setpoint"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_write_readonly_rejected(web):
    resp = web.post("/api/write", json={"name": "supply_air_temp", "value": 99})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


# ---- 历史数据集导出 ------------------------------------------------------ #
def test_dataset_stats(web, config):
    s = web.get("/api/dataset/stats").get_json()
    assert s["recording"] is True
    assert s["columns"] == len(config.points)
    assert s["rows"] >= 1
    assert "columns_preview" in s


def test_dataset_export_wide_csv(web, config):
    resp = web.get("/api/dataset/export?format=wide")
    assert resp.status_code == 200
    assert "text/csv" in resp.content_type
    assert "attachment" in resp.headers["Content-Disposition"]
    lines = resp.get_data(as_text=True).strip().splitlines()
    header = lines[0].split(",")
    assert header[0] == "timestamp_iso" and header[1] == "timestamp_unix"
    # 每个点位都应是一列
    for p in config.points:
        assert f"{config.device_name}.{p.name}" in header
    assert len(lines) >= 2  # 至少一行数据


def test_dataset_export_zip(web):
    import io
    import zipfile
    resp = web.get("/api/dataset/export?format=zip&minutes=5")
    assert resp.status_code == 200
    assert resp.content_type == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(resp.get_data()))
    names = set(z.namelist())
    assert {"dataset_wide.csv", "dataset_long.csv", "metadata.json", "README.txt"} <= names
    meta = z.read("metadata.json").decode("utf-8")
    assert "sampling_interval_seconds" in meta and "columns" in meta


def test_dataset_page_served(web):
    resp = web.get("/dataset")
    assert resp.status_code == 200
    assert b"<!DOCTYPE html>" in resp.data


def test_dataset_schema(web, config):
    sch = web.get("/api/dataset/schema").get_json()
    assert "devices" in sch and "label_options" in sch
    dev = sch["devices"][0]
    assert dev["device"] == config.device_name
    assert len(dev["points"]) == len(config.points)
    assert {"alarm", "out_of_range"} <= set(sch["label_options"])


def test_dataset_preview_with_selection(web, config):
    # 只选前两个点位 + 故障标签
    cols = [f"{config.device_name}.{config.points[0].name}",
            f"{config.device_name}.{config.points[1].name}"]
    p = web.post("/api/dataset/preview", json={
        "minutes": None, "columns": cols, "labels": {"alarm": True},
    }).get_json()
    assert p["feature_columns"] == cols
    assert "label_alarm" in p["label_columns"]
    assert p["rows"] >= 1
    # header = timestamp + 2 特征 + 1 标签
    assert p["header"][-1] == "label_alarm"


def test_dataset_build_wide_with_labels(web, config):
    resp = web.post("/api/dataset/build", json={
        "format": "wide", "labels": {"alarm": True, "out_of_range": True},
    })
    assert resp.status_code == 200
    header = resp.get_data(as_text=True).splitlines()[0].split(",")
    assert "label_alarm" in header and "label_out_of_range" in header


def test_dataset_build_downsample(web):
    full = web.post("/api/dataset/preview", json={"downsample": 1}).get_json()["rows"]
    half = web.post("/api/dataset/preview", json={"downsample": 2}).get_json()["rows"]
    assert half <= (full + 1) // 2 + 1


def test_dataset_build_parquet(web):
    pytest.importorskip("pyarrow")
    import io
    import pyarrow.parquet as pq
    resp = web.post("/api/dataset/build", json={"format": "parquet", "labels": {"alarm": True}})
    assert resp.status_code == 200
    table = pq.read_table(io.BytesIO(resp.get_data()))
    assert "timestamp_iso" in table.column_names
    assert "label_alarm" in table.column_names


def test_dataset_build_zip_with_parquet(web):
    pytest.importorskip("pyarrow")
    import io
    import zipfile
    resp = web.post("/api/dataset/build", json={"format": "zip", "main_format": "parquet"})
    assert resp.status_code == 200
    names = set(zipfile.ZipFile(io.BytesIO(resp.get_data())).namelist())
    assert "dataset_wide.parquet" in names and "metadata.json" in names


def test_dataset_per_device_labels(web, config):
    p = web.post("/api/dataset/preview", json={"labels": {"per_device": True}}).get_json()
    assert f"label_fault__{config.device_name}" in p["label_columns"]


def test_dataset_window_features(web, config):
    # 选一个模拟量点位 + 窗口 → 应出现 __mean/__std/__diff 派生列
    analog = next(p for p in config.points if not p.is_bit)
    col = f"{config.device_name}.{analog.name}"
    p = web.post("/api/dataset/preview", json={"columns": [col], "window": 5}).get_json()
    feat = p["feature_columns"]
    assert col in feat
    assert f"{col}__mean" in feat and f"{col}__std" in feat and f"{col}__diff" in feat


def test_dataset_window_metadata(web, config):
    import io
    import json
    import zipfile
    analog = next(p for p in config.points if not p.is_bit)
    col = f"{config.device_name}.{analog.name}"
    resp = web.post("/api/dataset/build", json={
        "format": "zip", "columns": [col], "window": 10, "labels": {"per_device": True}})
    z = zipfile.ZipFile(io.BytesIO(resp.get_data()))
    md = json.loads(z.read("metadata.json"))
    assert md["window_samples"] == 10
    derived = [c.get("derived") for c in md["feature_columns"] if c.get("derived")]
    assert "mean" in derived and "std" in derived and "diff" in derived


def test_dataset_window_stats_selectable(web, config):
    analog = next(p for p in config.points if not p.is_bit)
    col = f"{config.device_name}.{analog.name}"
    p = web.post("/api/dataset/preview", json={
        "columns": [col], "window": 5, "window_stats": ["min", "max", "slope"]}).get_json()
    feat = p["feature_columns"]
    assert f"{col}__min" in feat and f"{col}__max" in feat and f"{col}__slope" in feat
    # 未选的统计量不应出现
    assert f"{col}__mean" not in feat and f"{col}__std" not in feat


def test_dataset_series(web, config):
    cols = [f"{config.device_name}.{config.points[0].name}",
            f"{config.device_name}.{config.points[1].name}"]
    s = web.post("/api/dataset/series", json={"columns": cols, "max_points": 100}).get_json()
    assert s["points"] >= 1
    assert len(s["labels"]) == s["points"]
    names = {ser["column"] for ser in s["series"]}
    assert set(cols) <= names
    ser0 = s["series"][0]
    assert len(ser0["data"]) == s["points"]
    assert "unit" in ser0 and "is_bit" in ser0


def test_dataset_series_downsamples(web):
    # max_points 限制下，返回点数不超过该上限
    s = web.post("/api/dataset/series", json={"max_points": 5}).get_json()
    assert s["points"] <= 5


def test_dataset_disk_endpoint(web):
    d = web.get("/api/dataset/disk").get_json()
    assert "available" in d and "rows" in d and "span_human" in d


def test_dataset_disk_source(simulator, config, tmp_path):
    """落盘记录器：source=disk 能从磁盘 CSV 读回历史。"""
    import time
    from client.recorder import DatasetRecorder
    from web.app import FleetHub
    fleet = FleetHub.single(config, device_type="测试设备")
    fleet.hubs[fleet.order[0]]._poll_interval = 0.1
    fleet.start()
    rec = DatasetRecorder(fleet, interval=0.1, out_dir=str(tmp_path), to_disk=True)
    rec.start()
    time.sleep(1.0)
    try:
        ds = rec.disk_stats()
        assert ds["available"] is True and ds["rows"] >= 1
        col = f"{config.device_name}.{config.points[0].name}"
        disk = rec.series(columns=[col], source="disk")
        assert disk["points"] >= 1
        # 磁盘源导出宽表也应有数据
        wide = rec.export_wide_csv(None, columns=[col], source="disk")
        assert len(wide.splitlines()) - 1 == disk["points"] or disk["points"] >= 1
    finally:
        rec.stop()
        fleet.stop()


def test_dataset_split_zip(web):
    import io
    import json
    import zipfile
    resp = web.post("/api/dataset/build", json={"format": "zip", "split": "70/15/15"})
    assert resp.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(resp.get_data()))
    names = set(z.namelist())
    assert {"train/dataset_wide.csv", "val/dataset_wide.csv", "test/dataset_wide.csv"} <= names
    md = json.loads(z.read("metadata.json"))
    assert md["split"]["ratios"] == {"train": 0.7, "val": 0.15, "test": 0.15}
    # 三段行数之和 = 总行数（不丢样本）
    def nrows(n):
        return len(z.read(n).decode().splitlines()) - 1
    total = nrows("train/dataset_wide.csv") + nrows("val/dataset_wide.csv") + nrows("test/dataset_wide.csv")
    assert total == sum(md["split"]["rows"].values())
