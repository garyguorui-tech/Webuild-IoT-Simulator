"""InfluxDB v2 时序库落库 Sink（可选依赖 influxdb-client）。

把采集到的点位读数写入 InfluxDB，供 Grafana 等可视化。设计为
FieldDeviceClient.poll_forever 的 on_readings 回调，或在 collector 中直接调用。

measurement: field_point
  tags:   device, point, unit, type(register_type)
  fields: value(float), raw(int), in_range(bool)

    from client.influx_sink import InfluxSink
    sink = InfluxSink(url="http://influxdb:8086", token="...", org="acme", bucket="field")
    sink.connect()
    sink.write("AHU-01", readings)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

log = logging.getLogger("client.influx")

if TYPE_CHECKING:
    from client.modbus_client import PointReading

try:
    from influxdb_client import InfluxDBClient, Point as InfluxPoint
    from influxdb_client.client.write_api import SYNCHRONOUS
    INFLUX_AVAILABLE = True
except ImportError:
    INFLUX_AVAILABLE = False


class InfluxSink:
    """把读数写入 InfluxDB v2。未安装 influxdb-client 时构造即报错。"""

    def __init__(self, url: str, token: str, org: str, bucket: str):
        if not INFLUX_AVAILABLE:
            raise RuntimeError("influxdb-client 未安装：pip install influxdb-client")
        self.url, self.token, self.org, self.bucket = url, token, org, bucket
        self._client = None
        self._write_api = None

    def connect(self) -> None:
        self._client = InfluxDBClient(url=self.url, token=self.token, org=self.org)
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
        log.info("InfluxDB connected: %s org=%s bucket=%s", self.url, self.org, self.bucket)

    def close(self) -> None:
        if self._client:
            self._client.close()

    def write(self, device: str, readings: "list[PointReading]") -> None:
        """写入一台设备的一批读数。"""
        records = []
        for r in readings:
            if not r.ok:
                continue
            records.append(
                InfluxPoint("field_point")
                .tag("device", device)
                .tag("point", r.point.name)
                .tag("unit", r.point.unit or "")
                .tag("type", r.point.register_type)
                .field("value", float(r.value))
                .field("raw", int(r.raw))
                .field("in_range", bool(r.in_range))
            )
        if records:
            self._write_api.write(bucket=self.bucket, org=self.org, record=records)
