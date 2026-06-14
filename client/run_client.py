"""采集客户端启动入口。

启动后自动连接模拟器、周期轮询全部点位并打印；连接断开自动重连。

用法:
    python -m client.run_client
    python -m client.run_client --config config/points.yaml
    python -m client.run_client --host 127.0.0.1 --port 1502 --cycles 5
    python -m client.run_client --csv readings.csv      # 同时落 CSV
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 控制台默认 GBK 码页会让中文输出乱码，强制 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from client.modbus_client import FieldDeviceClient, PointReading  # noqa: E402
from simulator.points import load_config  # noqa: E402

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "points.yaml"


def _print_cycle(readings: list[PointReading], cycle: int) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"\n─── cycle {cycle}  @ {ts} ─────────────────────────────")
    for r in readings:
        print(r)


def main() -> None:
    parser = argparse.ArgumentParser(description="现场设备采集客户端（自动连接+重连）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--host", default=None, help="覆盖配置中的模拟器地址")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--cycles", type=int, default=None,
                        help="轮询多少周期后退出（默认无限）")
    parser.add_argument("--csv", default=None, help="把读数追加写入 CSV 文件")
    parser.add_argument("--mqtt", default=None, metavar="HOST[:PORT]",
                        help="把读数发布到 MQTT broker（需 paho-mqtt），如 127.0.0.1:1883")
    parser.add_argument("--mqtt-topic", default=None,
                        help="MQTT topic 前缀（默认 field/<设备名>）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    config = load_config(args.config)
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port

    csv_writer = None
    csv_file = None
    if args.csv:
        csv_file = open(args.csv, "a", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_file)
        if csv_file.tell() == 0:
            csv_writer.writerow(["timestamp", "point", "value", "unit", "in_range"])

    mqtt_sink = None
    if args.mqtt:
        from client.mqtt_sink import MqttSink
        host, _, port = args.mqtt.partition(":")
        prefix = args.mqtt_topic or f"field/{config.device_name}"
        mqtt_sink = MqttSink(host=host, port=int(port or 1883), topic_prefix=prefix)
        mqtt_sink.connect()
        print(f"MQTT 转发已开启 -> {host}:{port or 1883}  topic 前缀 {prefix}")

    client = FieldDeviceClient(config)
    print(f"采集客户端启动 -> {config.host}:{config.port} "
          f"(设备 {config.device_name}, {len(config.points)} 个点位)")
    print(f"轮询周期 {config.poll_interval}s，断线自动重连。Ctrl-C 退出。")

    cycle = 0
    try:
        for readings in client.poll_forever(max_cycles=args.cycles):
            cycle += 1
            _print_cycle(readings, cycle)
            if csv_writer:
                for r in readings:
                    csv_writer.writerow(
                        [f"{r.timestamp:.0f}", r.point.name, r.value,
                         r.point.unit, r.in_range])
                csv_file.flush()
            if mqtt_sink:
                mqtt_sink.publish(readings)
    except KeyboardInterrupt:
        print("\n客户端已停止。")
    finally:
        client.close()
        if csv_file:
            csv_file.close()
        if mqtt_sink:
            mqtt_sink.close()


if __name__ == "__main__":
    main()
