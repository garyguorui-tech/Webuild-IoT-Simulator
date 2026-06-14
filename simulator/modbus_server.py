"""Modbus TCP 模拟设备服务端（基于 pymodbus 3.9）。

把 YAML 点表映射到 Modbus 数据区:
    holding  -> Holding Registers  (FC03/06/16)
    input    -> Input Registers    (FC04)
    coil     -> Coils              (FC01/05/15)
    discrete -> Discrete Inputs    (FC02)

实现说明（针对 pymodbus 3.9.x，已在 requirements 中固定版本）:
  * ModbusSlaveContext.getValues/setValues 内部固定做 address += 1，
    服务端请求处理与本模块的引擎写入走同一套偏移，因此配置文件中的
    协议地址 N 与客户端请求地址 N 一一对应，无需额外换算。
  * ModbusSlaveContext.__init__ 在 3.9.2 中有缺陷：co/ir/hr 是否生效
    取决于 di 参数是否为 None，因此必须同时传入全部四个数据块。
"""
from __future__ import annotations

import asyncio
import logging

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.server import ServerAsyncStop, StartAsyncTcpServer

from .engine import SimulationEngine
from .points import Point, SimulatorConfig

log = logging.getLogger("simulator.modbus")

# register_type -> 引擎写值时使用的功能码（datastore 内部按 fc 路由到对应区）
_WRITE_FC = {"holding": 3, "input": 4, "coil": 1, "discrete": 2}


def _block_for(points: list[Point], register_type: str) -> ModbusSequentialDataBlock:
    """为某一寄存器类型创建顺序数据块，容量覆盖该类型的最大地址。

    +2 = 内部 address+1 偏移占一格，再留一格冗余。
    """
    addrs = [p.address for p in points if p.register_type == register_type]
    size = (max(addrs) + 2) if addrs else 1
    return ModbusSequentialDataBlock(0, [0] * (size + 1))


class ModbusSimulator:
    """可独立运行、也可被测试内嵌启动的 Modbus TCP 模拟器。"""

    def __init__(self, config: SimulatorConfig, update_interval: float = 1.0,
                 bind_host: str | None = None):
        self.config = config
        # 绑定地址可独立于 config.host：容器内服务端绑 0.0.0.0，
        # 而同进程/同主机的客户端仍用 config.host(127.0.0.1) 连接。
        self.bind_host = bind_host or config.host
        slave = ModbusSlaveContext(
            di=_block_for(config.points, "discrete"),
            co=_block_for(config.points, "coil"),
            ir=_block_for(config.points, "input"),
            hr=_block_for(config.points, "holding"),
        )
        self._slave = slave
        # single=True: 任意 unit id 都路由到同一个数据区，贴近常见网关行为
        self.context = ModbusServerContext(slaves=slave, single=True)
        self.engine = SimulationEngine(
            config.points, self._write_point, update_interval=update_interval)

    # ------------------------------------------------------------------ #
    def _write_point(self, point: Point, raw: int) -> None:
        """引擎回调：把原始值写进 Modbus 数据区。"""
        self._slave.setValues(_WRITE_FC[point.register_type], point.address, [raw])

    def read_point_raw(self, point: Point) -> int:
        """直接从数据区读原始值（测试/调试用，不走网络）。"""
        return self._slave.getValues(point.read_fc, point.address, count=1)[0]

    # ------------------------------------------------------------------ #
    async def serve(self) -> None:
        """启动仿真引擎并阻塞运行 Modbus TCP 服务。"""
        self.engine.start()
        identity = ModbusDeviceIdentification(info_name={
            "VendorName": "ACME-IoT",
            "ProductName": f"Niagara-style Field Device Simulator ({self.config.device_name})",
            "ModelName": self.config.device_name,
            "MajorMinorRevision": "1.0",
        })
        log.info("Modbus TCP simulator '%s' listening on %s:%d (%d points)",
                 self.config.device_name, self.bind_host,
                 self.config.port, len(self.config.points))
        try:
            await StartAsyncTcpServer(
                context=self.context,
                identity=identity,
                address=(self.bind_host, self.config.port),
            )
        finally:
            self.engine.stop()

    async def shutdown(self) -> None:
        """停止服务（供程序化调用）。"""
        self.engine.stop()
        await ServerAsyncStop()


def run(config: SimulatorConfig, update_interval: float = 1.0) -> None:
    """同步入口：运行直到 Ctrl-C。"""
    sim = ModbusSimulator(config, update_interval=update_interval)
    try:
        asyncio.run(sim.serve())
    except KeyboardInterrupt:
        log.info("simulator stopped by user")
