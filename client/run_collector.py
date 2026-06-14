"""设备群采集器 —— Docker 整链路的数据采集服务。

读取 config/fleet.yaml，连接全部设备的 Modbus 模拟器，周期轮询所有点位，
并行落到 InfluxDB（供 Grafana）与发布到 MQTT(EMQX)。带断线重连。

配置全部走环境变量（compose 注入），便于容器化：
    MODBUS_HOST     模拟器主机名（compose 里是服务名，如 fieldsim）。默认 127.0.0.1
    FLEET_CONFIG    设备群配置路径。默认 config/fleet.yaml
    POLL_INTERVAL   轮询周期秒。默认 2

    INFLUX_URL / INFLUX_TOKEN / INFLUX_ORG / INFLUX_BUCKET   —— 配齐才启用 Influx
    MQTT_HOST / MQTT_PORT / MQTT_TOPIC_PREFIX                —— 配 MQTT_HOST 才启用

单机调试也可直接跑：
    MODBUS_HOST=127.0.0.1 python -m client.run_collector
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from client.modbus_client import FieldDeviceClient  # noqa: E402
from simulator.points import load_fleet  # noqa: E402

log = logging.getLogger("collector")

DEFAULT_FLEET = Path(__file__).resolve().parent.parent / "config" / "fleet.yaml"


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def build_influx():
    url = _env("INFLUX_URL")
    token = _env("INFLUX_TOKEN")
    org = _env("INFLUX_ORG")
    bucket = _env("INFLUX_BUCKET")
    if not all([url, token, org, bucket]):
        log.info("InfluxDB 未配置（缺 INFLUX_* 环境变量），跳过时序落库")
        return None
    from client.influx_sink import InfluxSink
    sink = InfluxSink(url=url, token=token, org=org, bucket=bucket)
    sink.connect()
    return sink


def build_mqtt():
    host = _env("MQTT_HOST")
    if not host:
        log.info("MQTT 未配置（缺 MQTT_HOST），跳过 MQTT 发布")
        return None, ""
    from client.mqtt_sink import MqttSink
    port = int(_env("MQTT_PORT", "1883"))
    prefix = _env("MQTT_TOPIC_PREFIX", "field")
    # 每台设备一个 sink 复用同一连接较繁琐，这里用一个连接、按设备拼 topic
    sink = MqttSink(host=host, port=port, topic_prefix=prefix,
                    client_id="fleet-collector")
    # 等待 broker 就绪的简单重试
    for attempt in range(10):
        try:
            sink.connect()
            break
        except Exception as exc:
            log.warning("MQTT 连接失败(%d/10): %s，2s 后重试", attempt + 1, exc)
            time.sleep(2)
    else:
        log.error("MQTT 始终连不上，禁用 MQTT 发布")
        return None, ""
    return sink, prefix


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stdout)

    fleet_path = _env("FLEET_CONFIG", str(DEFAULT_FLEET))
    modbus_host = _env("MODBUS_HOST", "127.0.0.1")
    poll_interval = float(_env("POLL_INTERVAL", "2"))

    members = load_fleet(fleet_path)
    # 把每台设备的连接地址改写为容器服务名/指定主机
    clients = []
    for m in members:
        m.config.host = modbus_host
        m.config.poll_interval = poll_interval
        clients.append((m.name, FieldDeviceClient(m.config)))
    log.info("采集器启动：%d 台设备 @ %s, 周期 %.1fs",
             len(clients), modbus_host, poll_interval)

    influx = build_influx()
    mqtt, mqtt_prefix = build_mqtt()

    # 简单的逐设备轮询循环 + 断线重连
    backoff = 1.0
    while True:
        any_ok = False
        for name, client in clients:
            try:
                if not client.connected:
                    client.connect()
                    log.info("[%s] connected", name)
                readings = client.read_all()
                any_ok = True
                if influx:
                    influx.write(name, readings)
                if mqtt:
                    # 复用 MqttSink，但按设备覆盖前缀
                    mqtt.prefix = f"{mqtt_prefix}/{name}"
                    mqtt.publish(readings)
            except Exception as exc:
                client.close()
                log.warning("[%s] 采集失败: %s", name, exc)
        if any_ok:
            backoff = 1.0
            time.sleep(poll_interval)
        else:
            log.warning("全部设备不可达，%.0fs 后重试", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()
