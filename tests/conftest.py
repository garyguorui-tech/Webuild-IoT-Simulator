"""pytest 公共夹具：在后台线程启动一个真实的 Modbus 模拟器供测试连接。

每个测试 session 启动一次模拟器（独立事件循环 + 随机空闲端口），
测试通过真实 TCP 连接它，覆盖端到端链路。
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest

from client.modbus_client import FieldDeviceClient
from simulator.modbus_server import ModbusSimulator
from simulator.points import load_config

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "points.yaml"


def _free_port() -> int:
    """向 OS 申请一个空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(host: str, port: int, timeout: float = 10.0) -> None:
    """阻塞直到端口可连接，超时抛错。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(f"simulator did not open {host}:{port} within {timeout}s")


class SimulatorHandle:
    """在后台线程里跑的模拟器，可在测试中停止/重启（用于断线重连测试）。"""

    def __init__(self, config, update_interval: float = 0.2):
        self.config = config
        self._update_interval = update_interval
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.sim: ModbusSimulator | None = None

    def start(self) -> None:
        ready = threading.Event()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            self.sim = ModbusSimulator(
                self.config, update_interval=self._update_interval)
            loop.call_soon(ready.set)
            try:
                loop.run_until_complete(self.sim.serve())
            except asyncio.CancelledError:
                pass
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        ready.wait(timeout=5)
        _wait_port(self.config.host, self.config.port)

    def stop(self) -> None:
        """停止服务线程（模拟模拟器掉线）。

        只调用 ServerAsyncStop：它会让 serve() 里 await 的 TCP 服务返回，
        run_until_complete 随之自然结束、事件循环正常关闭——无需强制 stop loop，
        避免「Event loop stopped before Future completed」。
        """
        if self.sim:
            self.sim.engine.stop()
        if self._loop and self._loop.is_running():
            from pymodbus.server import ServerAsyncStop
            fut = asyncio.run_coroutine_threadsafe(ServerAsyncStop(), self._loop)
            try:
                fut.result(timeout=5)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)


@pytest.fixture(scope="session")
def config():
    """加载点表配置，并把端口换成随机空闲端口，避免与真实运行冲突。"""
    cfg = load_config(CONFIG_PATH)
    cfg.host = "127.0.0.1"
    cfg.port = _free_port()
    cfg.poll_interval = 0.1            # 测试里加快轮询
    cfg.reconnect_backoff = [0.2, 0.4, 0.6]
    return cfg


@pytest.fixture(scope="session")
def simulator(config):
    """整个测试 session 共享的运行中模拟器。"""
    handle = SimulatorHandle(config, update_interval=0.2)
    handle.start()
    yield handle
    handle.stop()


@pytest.fixture
def client(simulator, config):
    """已连接的客户端，测试结束自动关闭。"""
    c = FieldDeviceClient(config)
    c.connect()
    yield c
    c.close()
