"""连接性测试：能否与模拟器建立 Modbus TCP 连接。"""
from __future__ import annotations

import pytest
from pymodbus.exceptions import ConnectionException

from client.modbus_client import FieldDeviceClient


def test_connect_succeeds(simulator, config):
    """客户端应能连上正在运行的模拟器。"""
    client = FieldDeviceClient(config)
    assert client.connect() is True
    assert client.connected is True
    client.close()


def test_context_manager_connects(simulator, config):
    """with 语法进入即连接、退出即断开。"""
    with FieldDeviceClient(config) as client:
        assert client.connected is True
    assert client.connected is False


def test_connect_to_dead_port_raises(config):
    """连一个没有服务的端口应抛 ConnectionException。"""
    import dataclasses
    bad = dataclasses.replace(config, port=1)  # 1 端口无服务
    client = FieldDeviceClient(bad)
    with pytest.raises(ConnectionException):
        client.connect()
