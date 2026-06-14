"""配置加载与点位模型的单元测试（不需要网络/模拟器）。"""
from __future__ import annotations

import textwrap

import pytest

from simulator.points import Point, SimulationSpec, load_config


def test_load_default_config():
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "config" / "points.yaml"
    cfg = load_config(path)
    assert cfg.points
    assert cfg.device_name
    # 四类寄存器都应有点位
    for rt in ("holding", "input", "coil", "discrete"):
        assert cfg.points_by_type(rt), f"缺少 {rt} 点位"


def test_point_encode_decode_register():
    p = Point(name="t", register_type="holding", address=0, scale=10)
    assert p.encode(22.5) == 225
    assert p.decode(225) == 22.5


def test_point_encode_clamps_to_uint16():
    p = Point(name="t", register_type="holding", address=0, scale=1)
    assert p.encode(-5) == 0
    assert p.encode(70000) == 0xFFFF


def test_bit_point_encode_decode():
    p = Point(name="c", register_type="coil", address=0)
    assert p.encode(1) == 1
    assert p.encode(0) == 0
    assert p.decode(1) == 1


def test_duplicate_address_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(textwrap.dedent("""
        points:
          - {name: a, register_type: holding, address: 0}
          - {name: b, register_type: holding, address: 0}
    """), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate address"):
        load_config(bad)


def test_invalid_register_type_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(textwrap.dedent("""
        points:
          - {name: a, register_type: bogus, address: 0}
    """), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid register_type"):
        load_config(bad)


def test_simulation_spec_defaults():
    s = SimulationSpec(mode="sine", amplitude=2.0, period=30)
    assert s.mode == "sine"
    assert s.noise == 0.0
    assert s.values == []


def test_load_fleet():
    from pathlib import Path
    from simulator.points import load_fleet
    path = Path(__file__).resolve().parent.parent / "config" / "fleet.yaml"
    members = load_fleet(path)
    assert len(members) >= 2
    names = {m.name for m in members}
    assert "AHU-01" in names
    # 端口不冲突，且每台都有点位
    ports = [m.config.port for m in members]
    assert len(ports) == len(set(ports))
    for m in members:
        assert m.config.points
        assert m.config.device_name == m.name
