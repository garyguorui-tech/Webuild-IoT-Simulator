"""数据读取正确性测试：读到的值符合配置（类型、量程、初始值）。"""
from __future__ import annotations

import pytest


def test_read_all_points(client, config):
    """一次轮询应返回与配置等量、且全部成功的读数。"""
    readings = client.read_all()
    assert len(readings) == len(config.points)
    assert all(r.ok for r in readings)


def test_all_values_in_range(client):
    """每个点的工程值都应落在配置量程内（仿真有 clamp 保证）。"""
    readings = client.read_all()
    out = [r for r in readings if not r.in_range]
    assert not out, f"out-of-range points: {[r.point.name for r in out]}"


def test_bit_points_are_boolean(client, config):
    """coil / discrete 点位读到的值只能是 0 或 1。"""
    for p in config.points:
        if p.is_bit:
            r = client.read_point(p)
            assert r.ok
            assert r.value in (0, 1), f"{p.name} -> {r.value}"


def test_static_point_keeps_initial(client, config):
    """static 模式的点位不会被引擎改动，应保持初始值。"""
    p = config.point("temp_setpoint")  # holding, static, initial 24.0
    r = client.read_point(p)
    assert r.ok
    assert r.value == pytest.approx(p.initial, abs=0.01)


def test_engineering_scaling(client, config):
    """缩放正确：原始寄存器值 = 工程值 * scale。"""
    p = config.point("temp_setpoint")  # scale=10, initial=24.0 -> raw 240
    r = client.read_point(p)
    assert r.raw == p.encode(r.value)
    assert r.raw == 240


def test_sine_point_varies_over_time(client, config):
    """正弦点位在若干次采样间应出现变化（证明数据是动态的）。"""
    import time
    p = config.point("supply_air_temp")  # sine
    samples = []
    for _ in range(8):
        samples.append(client.read_point(p).raw)
        time.sleep(0.3)
    assert len(set(samples)) > 1, f"sine point not changing: {samples}"
