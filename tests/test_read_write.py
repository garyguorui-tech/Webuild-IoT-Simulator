"""读写测试：写 holding register / coil 后读回验证。"""
from __future__ import annotations

import pytest
from pymodbus.exceptions import ModbusException


def test_write_holding_register_roundtrip(client, config):
    """写温度设定值再读回，应一致。"""
    p = config.point("temp_setpoint")  # holding, writable, scale=10
    client.write_point(p, 26.5)
    r = client.read_point(p)
    assert r.ok
    assert r.value == pytest.approx(26.5, abs=0.05)
    assert r.raw == 265


def test_write_then_read_multiple_values(client, config):
    """连续写多个值，每次都能读回对应值。"""
    p = config.point("fan_speed_cmd")  # holding, scale=1
    for target in (10, 55, 80, 100, 0):
        client.write_point(p, target)
        assert client.read_point(p).value == pytest.approx(target, abs=0.5)


def test_write_coil_roundtrip(client, config):
    """写线圈（风机启停）再读回。"""
    p = config.point("fan_enable")  # coil, writable
    client.write_point(p, 0)
    assert client.read_point(p).value == 0
    client.write_point(p, 1)
    assert client.read_point(p).value == 1


def test_write_by_name(client, config):
    """按名字写入的便捷接口。"""
    client.write_point_by_name("temp_setpoint", 22.0)
    assert client.read_point(config.point("temp_setpoint")).value == pytest.approx(22.0, abs=0.05)


def test_write_readonly_point_rejected(client, config):
    """写只读点（input register 反映的非 writable 点）应被拒绝。"""
    p = config.point("supply_air_temp")  # writable=False
    with pytest.raises(ValueError):
        client.write_point(p, 99.0)


def test_write_input_register_point_rejected(client, config):
    """input register 类型本身不可写。"""
    p = config.point("return_air_temp")  # input register
    with pytest.raises(ValueError):
        client.write_point(p, 10.0)
