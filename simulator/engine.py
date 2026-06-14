"""仿真引擎：让点位值随时间变化。

独立于具体协议——引擎只负责按 simulation 配置计算每个点位的
新工程值，再通过回调写入协议数据区（Modbus datastore / BACnet 对象）。
这样新增协议（BACnet、OPC UA…）时引擎可直接复用。
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from typing import Callable

from .points import Point

log = logging.getLogger("simulator.engine")

# 写值回调: (point, raw_value) -> None
WriteFunc = Callable[[Point, int], None]


class SimulationEngine:
    """后台线程，按各点位的 simulation 模式周期性更新点位值。

    注意：mode=static 的点位在初始化后绝不会被引擎覆盖，
    因此「可写点」应配置为 static，客户端写入的值才能保持。
    """

    def __init__(self, points: list[Point], write_func: WriteFunc,
                 update_interval: float = 1.0):
        self._points = points
        self._write = write_func
        self._interval = update_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = time.monotonic()
        # step 模式的游标: point.name -> (上次阶跃时刻, 当前索引/状态)
        self._step_state: dict[str, tuple[float, int]] = {}
        # noise 模式（随机游走）的当前值
        self._walk_value: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    def write_initial_values(self) -> None:
        """把所有点位的初始值写入数据区（启动时调用一次）。"""
        for p in self._points:
            self._write(p, p.encode(p.initial))
        log.info("initialized %d points", len(self._points))

    def start(self) -> None:
        self.write_initial_values()
        self._thread = threading.Thread(
            target=self._run, name="sim-engine", daemon=True)
        self._thread.start()
        log.info("simulation engine started (update every %.1fs)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            now = time.monotonic()
            for p in self._points:
                try:
                    value = self._next_value(p, now)
                    if value is not None:
                        self._write(p, p.encode(value))
                except Exception:  # 单点异常不拖垮整个引擎
                    log.exception("update failed for point %s", p.name)

    def _next_value(self, p: Point, now: float) -> float | None:
        """计算点位下一时刻的工程值；返回 None 表示本周期不更新。"""
        mode = p.simulation.mode
        elapsed = now - self._t0

        if mode == "static":
            return None  # 永不覆盖（可写点依赖这一点）

        if mode == "sine":
            base = p.initial
            value = (base
                     + p.simulation.amplitude * math.sin(
                         2 * math.pi * elapsed / p.simulation.period)
                     + random.gauss(0, p.simulation.noise))
            return self._clamp(p, value)

        if mode == "noise":
            # 围绕初始值的有界随机游走
            current = self._walk_value.get(p.name, p.initial)
            current += random.gauss(0, p.simulation.noise)
            # 向初始值轻微回归，避免长时间漂出量程
            current += (p.initial - current) * 0.05
            current = self._clamp(p, current)
            self._walk_value[p.name] = current
            return current

        if mode == "step":
            last_t, idx = self._step_state.get(p.name, (self._t0, 0))
            if now - last_t < p.simulation.interval:
                return None
            if p.is_bit:
                idx = 1 - (idx if idx in (0, 1) else int(bool(p.initial)))
                value: float = idx
            else:
                values = p.simulation.values or [p.initial]
                idx = (idx + 1) % len(values)
                value = values[idx]
            self._step_state[p.name] = (now, idx)
            return self._clamp(p, value)

        log.warning("point %s: unknown simulation mode %r", p.name, mode)
        return None

    @staticmethod
    def _clamp(p: Point, value: float) -> float:
        if p.min is not None:
            value = max(value, p.min)
        if p.max is not None:
            value = min(value, p.max)
        return value
