"""BACnet/IP 扩展（实验性，基于 bacpypes3，可选依赖）。

设计目标：与 Modbus 模拟器共用同一份 YAML 点表和 SimulationEngine，
把寄存器点映射为 BACnet Analog Value，把 bit 点映射为 Binary Value：

    holding/input  -> analogValue  (presentValue = 工程值，带 units 描述)
    coil/discrete  -> binaryValue  (presentValue = active/inactive)

依赖未安装或绑定失败时不影响 Modbus 主功能 —— run_simulator 仅在
`--bacnet` 显式开启时才加载本模块。

安装可选依赖:  pip install bacpypes3
"""
from __future__ import annotations

import asyncio
import logging

from .engine import SimulationEngine
from .points import Point, SimulatorConfig

log = logging.getLogger("simulator.bacnet")

try:
    from bacpypes3.local.analog import AnalogValueObject
    from bacpypes3.local.binary import BinaryValueObject
    from bacpypes3.local.device import DeviceObject
    from bacpypes3.ipv4.app import NormalApplication
    from bacpypes3.pdu import IPv4Address

    BACNET_AVAILABLE = True
except ImportError:  # 可选依赖缺失时模块仍可被导入
    BACNET_AVAILABLE = False


class BACnetSimulator:
    """简化版 BACnet/IP 设备：发布点表为 AV/BV 对象并随引擎更新。"""

    def __init__(self, config: SimulatorConfig, *,
                 address: str = "127.0.0.1/24", port: int = 47808,
                 device_id: int = 599, update_interval: float = 1.0):
        if not BACNET_AVAILABLE:
            raise RuntimeError(
                "bacpypes3 未安装，BACnet 扩展不可用。pip install bacpypes3")
        self.config = config
        self._objects: dict[str, object] = {}

        self.device = DeviceObject(
            objectIdentifier=("device", device_id),
            objectName=f"{config.device_name}-BACnet",
            vendorIdentifier=999,
        )
        self.app = NormalApplication(
            self.device, IPv4Address(f"{address}:{port}"))

        av_idx, bv_idx = 0, 0
        for p in config.points:
            if p.is_bit:
                obj = BinaryValueObject(
                    objectIdentifier=("binaryValue", bv_idx),
                    objectName=p.name,
                    presentValue="active" if p.initial else "inactive",
                    description=p.description,
                )
                bv_idx += 1
            else:
                obj = AnalogValueObject(
                    objectIdentifier=("analogValue", av_idx),
                    objectName=p.name,
                    presentValue=float(p.initial),
                    description=f"{p.description} [{p.unit}]",
                )
                av_idx += 1
            self.app.add_object(obj)
            self._objects[p.name] = obj

        self.engine = SimulationEngine(
            config.points, self._write_point, update_interval=update_interval)

    def _write_point(self, point: Point, raw: int) -> None:
        """引擎回调：BACnet 对象直接存工程值（无需寄存器缩放）。"""
        obj = self._objects[point.name]
        if point.is_bit:
            obj.presentValue = "active" if raw else "inactive"
        else:
            obj.presentValue = point.decode(raw)

    async def serve(self) -> None:
        self.engine.start()
        log.info("BACnet/IP simulator online: device %s, %d objects",
                 self.device.objectIdentifier, len(self._objects))
        try:
            while True:  # bacpypes3 应用随事件循环常驻
                await asyncio.sleep(3600)
        finally:
            self.engine.stop()
            self.app.close()
