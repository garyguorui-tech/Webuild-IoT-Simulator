"""断线重连测试：模拟器停掉再起来，客户端能自动恢复采集。

这里用独立的模拟器实例（独立端口），避免影响 session 级共享模拟器，
以便安全地执行 stop/start。
"""
from __future__ import annotations

import dataclasses
import threading
import time

import pytest
from pymodbus.exceptions import ConnectionException

from client.modbus_client import FieldDeviceClient
from tests.conftest import SimulatorHandle, _free_port


@pytest.fixture
def restartable_sim(config):
    """一个可停止/重启的独立模拟器（独立端口）。"""
    cfg = dataclasses.replace(config, port=_free_port())
    handle = SimulatorHandle(cfg, update_interval=0.2)
    handle.start()
    yield handle, cfg
    handle.stop()


def test_read_fails_after_simulator_down(restartable_sim):
    """模拟器停掉后读取应失败（抛连接异常）。"""
    handle, cfg = restartable_sim
    client = FieldDeviceClient(cfg)
    client.connect()
    assert client.read_all()  # 正常

    handle.stop()
    time.sleep(0.3)
    with pytest.raises((ConnectionException, OSError)):
        client.read_all()
    client.close()


def test_auto_reconnect_after_restart(restartable_sim):
    """模拟器恢复后，poll_forever 应自动重连并继续产出读数。"""
    handle, cfg = restartable_sim
    client = FieldDeviceClient(cfg)

    collected: list = []
    stop_flag = threading.Event()

    def consume() -> None:
        # 无限轮询直到收集到「停机前」和「恢复后」的数据
        for readings in client.poll_forever():
            collected.append(time.time())
            if stop_flag.is_set():
                break

    t = threading.Thread(target=consume, daemon=True)
    t.start()

    # 先确认正常采集
    time.sleep(0.5)
    before = len(collected)
    assert before > 0, "应在停机前采到数据"

    # 模拟掉线
    handle.stop()
    time.sleep(0.6)
    during = len(collected)

    # 在原端口重启模拟器
    handle.start()
    time.sleep(1.2)
    after = len(collected)

    stop_flag.set()
    client.close()

    assert after > during, (
        f"恢复后应继续采到新数据: before={before} during={during} after={after}")


def test_reconnect_backoff_sequence_config(config):
    """重连退避序列应来自配置且非空。"""
    assert config.reconnect_backoff
    assert all(d > 0 for d in config.reconnect_backoff)
