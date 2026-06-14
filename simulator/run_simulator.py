"""模拟器启动入口。

用法:
    python -m simulator.run_simulator                          # 默认配置
    python -m simulator.run_simulator --config config/points.yaml
    python -m simulator.run_simulator --port 1502 --update-interval 0.5
    python -m simulator.run_simulator --bacnet                 # 同时启用 BACnet/IP（实验性）
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# 支持 `python simulator/run_simulator.py` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 控制台默认 GBK 码页会让中文日志乱码，强制 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from simulator.modbus_server import ModbusSimulator  # noqa: E402
from simulator.points import load_config  # noqa: E402

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "points.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description="Niagara 风格现场设备模拟器")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="点表 YAML 路径 (默认 config/points.yaml)")
    parser.add_argument("--host", default=None, help="覆盖配置中的监听地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖配置中的端口")
    parser.add_argument("--update-interval", type=float, default=1.0,
                        help="点位值刷新周期，秒 (默认 1.0)")
    parser.add_argument("--bacnet", action="store_true",
                        help="同时启动简化版 BACnet/IP 服务（需要 bacpypes3）")
    parser.add_argument("--bacnet-address", default="127.0.0.1/24",
                        help="BACnet 绑定地址 (默认 127.0.0.1/24)")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    config = load_config(args.config)
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port

    sim = ModbusSimulator(config, update_interval=args.update_interval)

    async def serve_all() -> None:
        tasks = [asyncio.create_task(sim.serve(), name="modbus")]
        if args.bacnet:
            from simulator.bacnet_server import BACnetSimulator  # 延迟导入可选依赖
            bacnet = BACnetSimulator(
                config, address=args.bacnet_address,
                update_interval=args.update_interval)
            tasks.append(asyncio.create_task(bacnet.serve(), name="bacnet"))
        await asyncio.gather(*tasks)

    try:
        asyncio.run(serve_all())
    except KeyboardInterrupt:
        logging.getLogger("simulator").info("stopped by user")


if __name__ == "__main__":
    main()
