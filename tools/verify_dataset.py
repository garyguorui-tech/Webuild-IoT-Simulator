"""自包含验证：起设备群 -> 记录 -> 一键导出 ML 数据集 -> 校验内容。

不依赖长期后台服务，单进程跑完即退出，产出真实样例数据集到 docs/sample_dataset.zip。
"""
import io
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from client.recorder import DatasetRecorder
from simulator.points import load_fleet
from web.app import FleetHub, create_app

members = load_fleet("config/fleet.yaml")
fleet = FleetHub.from_members(members)
fleet.start()
time.sleep(1.5)

rec = DatasetRecorder(fleet, interval=0.5, to_disk=False)
rec.start()
print("记录中，采集 6 秒 ...")
time.sleep(6)

app = create_app(fleet, recorder=rec)
client = app.test_client()

# 1) 状态
s = client.get("/api/dataset/stats").get_json()
print(f"[stats] recording={s['recording']} rows={s['rows']} columns={s['columns']} "
      f"devices={s['devices']} span={s['span_human']}")

# 2) 宽表导出
wide = client.get("/api/dataset/export?format=wide").get_data(as_text=True)
lines = wide.strip().splitlines()
header = lines[0].split(",")
print(f"[wide csv] {len(lines)-1} 行 x {len(header)} 列")
print(f"[wide csv] 前 6 列: {header[:6]}")
print(f"[wide csv] 数据样例行: {lines[1].split(',')[:6]}")

# 3) ZIP 导出（落地到 docs 作为样例）
blob = client.get("/api/dataset/export?format=zip&minutes=5").get_data()
out = Path("docs/sample_dataset.zip")
out.write_bytes(blob)
z = zipfile.ZipFile(io.BytesIO(blob))
print(f"[zip] {out} ({len(blob)} bytes) 内含: {z.namelist()}")
meta = z.read("metadata.json").decode("utf-8")
import json as _json
md = _json.loads(meta)
print(f"[metadata] 采样间隔={md['sampling_interval_seconds']}s 列数={len(md['columns'])} "
      f"对齐={md['aligned']} 值类型={md['value_kind']}")
print(f"[metadata] 示例列定义: {md['columns'][0]}")

rec.stop()
fleet.stop()
print("OK: 数据集导出验证通过")
