"""MQTT 落库/转发示例（可选依赖 paho-mqtt）。

把采集到的点位读数发布到 MQTT broker，便于接入 EMQX / Mosquitto / 云 IoT 平台，
或下游的 Telegraf -> InfluxDB 时序入库管线。

设计为 FieldDeviceClient.poll_forever 的 on_readings 回调：

    from client.mqtt_sink import MqttSink
    sink = MqttSink(host="127.0.0.1", port=1883, topic_prefix="field/AHU-01")
    sink.connect()
    for _ in client.poll_forever(on_readings=sink.publish):
        ...

每个点位发布到独立 topic（便于按点订阅），payload 为 JSON：
    topic:   field/AHU-01/supply_air_temp
    payload: {"value": 23.7, "unit": "degC", "raw": 237, "in_range": true, "ts": 1700000000.0}

并额外发布一条聚合快照到 <prefix>/_snapshot，方便整机订阅。

InfluxDB 直写方案（替代/补充 MQTT）见本文件末尾 InfluxSink 注释。
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

log = logging.getLogger("client.mqtt")

if TYPE_CHECKING:
    from client.modbus_client import PointReading

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False


class MqttSink:
    """把读数发布到 MQTT broker。未安装 paho-mqtt 时构造即报错。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 1883,
                 topic_prefix: str = "field/device", qos: int = 0,
                 client_id: str = "niagara-sim-client"):
        if not MQTT_AVAILABLE:
            raise RuntimeError("paho-mqtt 未安装：pip install paho-mqtt")
        self.host = host
        self.port = port
        self.prefix = topic_prefix.rstrip("/")
        self.qos = qos
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)

    def connect(self) -> None:
        self._client.connect(self.host, self.port, keepalive=30)
        self._client.loop_start()
        log.info("MQTT connected to %s:%d, prefix=%s", self.host, self.port, self.prefix)

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def publish(self, readings: "list[PointReading]") -> None:
        """供 client.poll_forever(on_readings=...) 调用的回调。"""
        snapshot: dict[str, float] = {}
        ts = time.time()
        for r in readings:
            if not r.ok:
                continue
            payload = json.dumps({
                "value": r.value, "unit": r.point.unit, "raw": r.raw,
                "in_range": r.in_range, "ts": ts,
            })
            self._client.publish(f"{self.prefix}/{r.point.name}", payload, qos=self.qos)
            snapshot[r.point.name] = r.value
        self._client.publish(f"{self.prefix}/_snapshot",
                             json.dumps({"ts": ts, "points": snapshot}), qos=self.qos)


# ---------------------------------------------------------------------------
# 备选：直写 InfluxDB（时序库）示例骨架。
# 安装: pip install influxdb-client
#
# from influxdb_client import InfluxDBClient, Point as InfluxPoint
# from influxdb_client.client.write_api import SYNCHRONOUS
#
# class InfluxSink:
#     def __init__(self, url, token, org, bucket, device="AHU-01"):
#         self._w = InfluxDBClient(url=url, token=token, org=org).write_api(SYNCHRONOUS)
#         self._bucket, self._org, self._device = bucket, org, device
#
#     def publish(self, readings):
#         pts = []
#         for r in readings:
#             if not r.ok:
#                 continue
#             pts.append(
#                 InfluxPoint("field_point")
#                 .tag("device", self._device)
#                 .tag("point", r.point.name)
#                 .tag("unit", r.point.unit)
#                 .field("value", float(r.value)))
#         self._w.write(bucket=self._bucket, org=self._org, record=pts)
#
# 用法与 MqttSink 相同：client.poll_forever(on_readings=influx.publish)
