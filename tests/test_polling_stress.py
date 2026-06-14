"""轮询压力测试：多点位、高频率连续轮询的稳定性与吞吐。"""
from __future__ import annotations

import time


def test_many_polling_cycles(client, config):
    """连续轮询多周期，全部成功、点数稳定。"""
    cycles = 50
    total_ok = 0
    for _ in range(cycles):
        readings = client.read_all()
        assert len(readings) == len(config.points)
        total_ok += sum(1 for r in readings if r.ok)
    assert total_ok == cycles * len(config.points)


def test_polling_throughput(client, config):
    """统计读取吞吐，确保整体读取在合理速度内完成（非功能性 smoke）。"""
    cycles = 30
    start = time.time()
    for _ in range(cycles):
        client.read_all()
    elapsed = time.time() - start
    reads = cycles * len(config.points)
    rate = reads / elapsed
    print(f"\n  压力测试: {reads} 次点读取 / {elapsed:.2f}s = {rate:.0f} reads/s")
    assert rate > 50, f"读取速率过低: {rate:.0f} reads/s"


def test_poll_forever_bounded_cycles(client, config):
    """poll_forever 在 max_cycles 限制下应正好产出对应周期数。"""
    cfg_interval = config.poll_interval
    n = 5
    got = list(client.poll_forever(max_cycles=n))
    assert len(got) == n
    for readings in got:
        assert len(readings) == len(config.points)


def test_concurrent_read_write_consistency(client, config):
    """交错读写同一点位，读到的始终是最近写入的值。"""
    p = config.point("fan_speed_cmd")
    for v in range(0, 101, 20):
        client.write_point(p, v)
        assert client.read_point(p).value == v
