"""点位模型与配置加载。

simulator 与 client 共用本模块：双方从同一份 YAML 读取点表，
保证地址、倍率、量程定义一致（类似 Niagara 中 driver 的 point database）。
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

# Modbus 寄存器类型 -> (pymodbus datastore 读 fc 码, 是否为 bit 量)
REGISTER_TYPES = {
    "holding": (3, False),   # FC03 Read Holding Registers
    "input": (4, False),     # FC04 Read Input Registers
    "coil": (1, True),       # FC01 Read Coils
    "discrete": (2, True),   # FC02 Read Discrete Inputs
}

UINT16_MAX = 0xFFFF


@dataclasses.dataclass
class SimulationSpec:
    """点位值随时间的变化模式。"""

    mode: str = "static"          # sine | noise | step | static
    amplitude: float = 0.0        # sine: 振幅（工程值）
    period: float = 60.0          # sine: 周期（秒）
    noise: float = 0.0            # sine/noise: 高斯噪声标准差（工程值）
    interval: float = 10.0        # step: 阶跃间隔（秒）
    values: list[float] = dataclasses.field(default_factory=list)  # step: 循环值表


@dataclasses.dataclass
class Point:
    """一个现场点位（对应 Niagara proxy point）。"""

    name: str
    register_type: str            # holding | input | coil | discrete
    address: int                  # Modbus 协议地址（0 起始）
    description: str = ""
    unit: str = ""
    scale: float = 1.0            # raw = round(eng * scale)
    initial: float = 0.0
    min: float | None = None
    max: float | None = None
    writable: bool = False
    simulation: SimulationSpec = dataclasses.field(default_factory=SimulationSpec)

    @property
    def is_bit(self) -> bool:
        """是否为 1bit 点（coil / discrete input）。"""
        return REGISTER_TYPES[self.register_type][1]

    @property
    def read_fc(self) -> int:
        """读取该点位所用的 Modbus 功能码。"""
        return REGISTER_TYPES[self.register_type][0]

    def encode(self, eng_value: float) -> int:
        """工程值 -> 原始寄存器值（uint16 或 0/1）。"""
        if self.is_bit:
            return 1 if eng_value else 0
        raw = round(eng_value * self.scale)
        return int(min(max(raw, 0), UINT16_MAX))

    def decode(self, raw: int) -> float:
        """原始寄存器值 -> 工程值。"""
        if self.is_bit:
            return 1 if raw else 0
        return raw / self.scale


@dataclasses.dataclass
class SimulatorConfig:
    """整份 YAML 配置。"""

    device_name: str
    unit_id: int
    host: str
    port: int
    poll_interval: float
    poll_timeout: float
    reconnect_backoff: list[float]
    points: list[Point]

    def points_by_type(self, register_type: str) -> list[Point]:
        return [p for p in self.points if p.register_type == register_type]

    def point(self, name: str) -> Point:
        for p in self.points:
            if p.name == name:
                return p
        raise KeyError(f"point not found: {name}")


def _build_point(raw: dict[str, Any]) -> Point:
    rt = raw.get("register_type", "")
    if rt not in REGISTER_TYPES:
        raise ValueError(
            f"point {raw.get('name')!r}: invalid register_type {rt!r}, "
            f"expected one of {sorted(REGISTER_TYPES)}"
        )
    sim = SimulationSpec(**raw.get("simulation", {}))
    return Point(
        name=raw["name"],
        register_type=rt,
        address=int(raw["address"]),
        description=raw.get("description", ""),
        unit=raw.get("unit", ""),
        scale=float(raw.get("scale", 1)),
        initial=float(raw.get("initial", 0)),
        min=raw.get("min"),
        max=raw.get("max"),
        writable=bool(raw.get("writable", False)),
        simulation=sim,
    )


def load_config(path: str | Path) -> SimulatorConfig:
    """加载并校验 YAML 点表配置。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    points = [_build_point(p) for p in data.get("points", [])]
    if not points:
        raise ValueError(f"{path}: no points defined")

    # 同类型地址不允许冲突
    seen: set[tuple[str, int]] = set()
    for p in points:
        key = (p.register_type, p.address)
        if key in seen:
            raise ValueError(f"duplicate address: {p.register_type}[{p.address}]")
        seen.add(key)

    device = data.get("device", {})
    modbus = data.get("modbus", {})
    polling = data.get("polling", {})
    return SimulatorConfig(
        device_name=device.get("name", "SIM-DEVICE"),
        unit_id=int(device.get("unit_id", 1)),
        host=modbus.get("host", "127.0.0.1"),
        port=int(modbus.get("port", 1502)),
        poll_interval=float(polling.get("interval", 2.0)),
        poll_timeout=float(polling.get("timeout", 3.0)),
        reconnect_backoff=[float(x) for x in polling.get("reconnect_backoff", [1, 2, 5, 10])],
        points=points,
    )


@dataclasses.dataclass
class FleetMember:
    """设备群中的一台设备。"""

    name: str
    type: str
    config: SimulatorConfig


def load_fleet(path: str | Path) -> list[FleetMember]:
    """加载设备群配置（config/fleet.yaml）。

    fleet.yaml 中每台设备的 config 路径相对项目根（即 fleet.yaml 的上一级）。
    fleet 里的 port 会覆盖各点表自身的 modbus.port。
    """
    fleet_path = Path(path).resolve()
    project_root = fleet_path.parent.parent
    data = yaml.safe_load(fleet_path.read_text(encoding="utf-8"))

    members: list[FleetMember] = []
    seen_ports: set[int] = set()
    for d in data.get("devices", []):
        cfg = load_config(project_root / d["config"])
        if "port" in d:
            cfg.port = int(d["port"])
        if cfg.port in seen_ports:
            raise ValueError(f"fleet: duplicate port {cfg.port}")
        seen_ports.add(cfg.port)
        members.append(FleetMember(
            name=d.get("name", cfg.device_name),
            type=d.get("type", ""),
            config=cfg,
        ))
    if not members:
        raise ValueError(f"{path}: no devices defined")
    return members
