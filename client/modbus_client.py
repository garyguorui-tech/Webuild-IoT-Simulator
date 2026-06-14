"""Modbus TCP 采集客户端，带断线重连。

封装 pymodbus 同步客户端，提供贴近现场语义的 API：
  * connect()         —— 建立连接（失败抛异常）
  * read_point()      —— 按点位定义读取单点（自动选 FC、自动解码工程值）
  * read_all()        —— 轮询全部点位，返回 PointReading 列表
  * write_point()     —— 写 holding/coil 点
  * poll_forever()    —— 周期轮询 + 断线自动重连（CLI 主循环用）

地址语义：配置中的协议地址与 pymodbus 请求地址一致（见 modbus_server.py 说明）。
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Callable, Iterator

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException

from simulator.points import Point, SimulatorConfig

log = logging.getLogger("client.modbus")


@dataclasses.dataclass
class PointReading:
    """一次点位读取结果。"""

    point: Point
    raw: int                     # 原始寄存器值
    value: float                 # 解码后的工程值
    timestamp: float             # time.time()
    in_range: bool               # 工程值是否落在配置量程内
    ok: bool = True              # 本次读取是否成功
    error: str = ""

    def __str__(self) -> str:
        p = self.point
        if not self.ok:
            return f"  {p.name:<22} ERROR: {self.error}"
        flag = "" if self.in_range else "  <!> OUT OF RANGE"
        unit = f" {p.unit}" if p.unit else ""
        if p.is_bit:
            return f"  {p.name:<22} {int(self.value)}{flag}"
        return f"  {p.name:<22} {self.value:>8.2f}{unit}{flag}"


class FieldDeviceClient:
    """面向现场设备的 Modbus 采集客户端。"""

    def __init__(self, config: SimulatorConfig):
        self.config = config
        self._client = ModbusTcpClient(
            host=config.host, port=config.port, timeout=config.poll_timeout)

    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return bool(self._client.connected)

    def connect(self) -> bool:
        """建立 TCP 连接，成功返回 True，失败抛 ConnectionException。"""
        if self._client.connect():
            log.info("connected to %s:%d", self.config.host, self.config.port)
            return True
        raise ConnectionException(
            f"cannot connect to {self.config.host}:{self.config.port}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FieldDeviceClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def _read_raw(self, point: Point) -> int:
        """按点位类型选择功能码读取原始值。"""
        uid = self.config.unit_id
        fc = point.read_fc
        if fc == 3:
            rr = self._client.read_holding_registers(point.address, count=1, slave=uid)
        elif fc == 4:
            rr = self._client.read_input_registers(point.address, count=1, slave=uid)
        elif fc == 1:
            rr = self._client.read_coils(point.address, count=1, slave=uid)
        else:  # fc == 2
            rr = self._client.read_discrete_inputs(point.address, count=1, slave=uid)

        if rr.isError():
            raise ModbusException(f"read {point.name} failed: {rr}")
        return int(rr.bits[0]) if point.is_bit else int(rr.registers[0])

    def read_point(self, point: Point) -> PointReading:
        """读取单点，异常被捕获并标记在 PointReading 上。"""
        try:
            raw = self._read_raw(point)
            value = point.decode(raw)
            in_range = self._check_range(point, value)
            return PointReading(point, raw, value, time.time(), in_range)
        except (ModbusException, ConnectionException, OSError) as exc:
            return PointReading(point, 0, 0.0, time.time(), False,
                                ok=False, error=str(exc))

    def read_all(self) -> list[PointReading]:
        """轮询全部点位。任一点失败会抛出，便于上层触发重连。"""
        readings = [self.read_point(p) for p in self.config.points]
        for r in readings:
            if not r.ok:
                raise ConnectionException(r.error)
        return readings

    def write_point(self, point: Point, value: float) -> None:
        """写 holding register 或 coil。"""
        if not point.writable:
            raise ValueError(f"point {point.name} is not writable")
        uid = self.config.unit_id
        if point.register_type == "holding":
            raw = point.encode(value)
            rr = self._client.write_register(point.address, raw, slave=uid)
        elif point.register_type == "coil":
            rr = self._client.write_coil(point.address, bool(value), slave=uid)
        else:
            raise ValueError(
                f"point {point.name} ({point.register_type}) is not writable")
        if rr.isError():
            raise ModbusException(f"write {point.name} failed: {rr}")
        log.info("wrote %s = %s", point.name, value)

    def write_point_by_name(self, name: str, value: float) -> None:
        self.write_point(self.config.point(name), value)

    @staticmethod
    def _check_range(point: Point, value: float) -> bool:
        if point.is_bit:
            return value in (0, 1)
        if point.min is not None and value < point.min:
            return False
        if point.max is not None and value > point.max:
            return False
        return True

    # ------------------------------------------------------------------ #
    def poll_forever(
        self,
        on_readings: Callable[[list[PointReading]], None] | None = None,
        max_cycles: int | None = None,
    ) -> Iterator[list[PointReading]]:
        """周期轮询 + 断线自动重连（指数退避）。

        作为生成器逐周期产出 readings，便于 CLI / 测试消费；
        on_readings 是可选的副作用回调（如写日志/落库）。
        max_cycles 为 None 表示无限运行。
        """
        backoff = self.config.reconnect_backoff
        cycle = 0
        while max_cycles is None or cycle < max_cycles:
            try:
                if not self.connected:
                    self._reconnect(backoff)
                readings = self.read_all()
                if on_readings:
                    on_readings(readings)
                yield readings
                cycle += 1
                time.sleep(self.config.poll_interval)
            except (ConnectionException, ModbusException, OSError) as exc:
                log.warning("polling error: %s — will reconnect", exc)
                self.close()

    def _reconnect(self, backoff: list[float]) -> None:
        """带指数退避的重连，直到成功。"""
        attempt = 0
        while True:
            try:
                self.connect()
                return
            except ConnectionException:
                delay = backoff[min(attempt, len(backoff) - 1)]
                log.warning("reconnect attempt %d failed, retry in %.0fs",
                            attempt + 1, delay)
                time.sleep(delay)
                attempt += 1
