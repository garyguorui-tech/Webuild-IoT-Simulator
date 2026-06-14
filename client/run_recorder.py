"""无界面历史数据记录器 —— 长期采集训练数据到磁盘。

连接设备群全部模拟器，按固定间隔记录时间对齐的宽表样本到
data/recordings/recording_<ts>.csv，可长期运行积累历史数据集。
适合放后台/容器里跑，与 Web 面板互不影响。

    python -m client.run_recorder                       # 默认 fleet.yaml，间隔 2s
    python -m client.run_recorder --interval 5 --minutes 60   # 采 60 分钟后自动停止
    MODBUS_HOST=fieldsim python -m client.run_recorder  # 容器内连服务名
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from client.recorder import DatasetRecorder  # noqa: E402
from simulator.points import load_fleet  # noqa: E402
from web.app import FleetHub  # noqa: E402

DEFAULT_FLEET = Path(__file__).resolve().parent.parent / "config" / "fleet.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description="历史数据记录器（落盘训练数据集）")
    parser.add_argument("--fleet", default=str(DEFAULT_FLEET))
    parser.add_argument("--interval", type=float, default=2.0, help="采样间隔秒")
    parser.add_argument("--minutes", type=float, default=None,
                        help="采集多少分钟后自动停止（默认一直采）")
    parser.add_argument("--out", default=None, help="输出目录（默认 data/recordings）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stdout)

    members = load_fleet(args.fleet)
    modbus_host = os.environ.get("MODBUS_HOST")
    if modbus_host:
        for m in members:
            m.config.host = modbus_host

    fleet = FleetHub.from_members(members)
    fleet.start()
    time.sleep(1.5)  # 等首批数据到位

    recorder = DatasetRecorder(fleet, interval=args.interval, out_dir=args.out)
    recorder.start()

    deadline = time.time() + args.minutes * 60 if args.minutes else None
    print(f"记录中 -> {recorder._disk_path}（间隔 {args.interval}s，"
          f"{'采 %.0f 分钟' % args.minutes if args.minutes else 'Ctrl-C 停止'}）")
    try:
        while True:
            time.sleep(5)
            s = recorder.stats()
            print(f"  已记录 {s['rows']} 样本 / 跨度 {s['span_human']} / {s['columns']} 列")
            if deadline and time.time() >= deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        recorder.stop()
        fleet.stop()
        print(f"完成：{recorder._disk_path}")


if __name__ == "__main__":
    main()
