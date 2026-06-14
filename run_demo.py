"""一键演示：在单个进程内同时启动模拟器 + Web 监控面板。

适合快速体验或演示：不必开两个终端。生产部署仍建议分开运行
（simulator 与 web 各自独立进程，见 README）。

    python run_demo.py                 # 模拟器 :1502，面板 http://127.0.0.1:8000
    python run_demo.py --web-port 8080 --modbus-port 1502
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from simulator.modbus_server import ModbusSimulator  # noqa: E402
from simulator.points import load_config  # noqa: E402
from web.app import FleetHub, create_app  # noqa: E402

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config" / "points.yaml"


def _start_simulator_thread(config, update_interval: float) -> None:
    """在后台线程的独立事件循环里运行 Modbus 模拟器。"""
    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        sim = ModbusSimulator(config, update_interval=update_interval)
        loop.run_until_complete(sim.serve())

    threading.Thread(target=_run, name="sim", daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟器 + Web 面板 一键演示")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--modbus-port", type=int, default=None)
    parser.add_argument("--web-host", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8000)
    parser.add_argument("--update-interval", type=float, default=0.5)
    parser.add_argument("--open", action="store_true",
                        help="服务起来后自动在默认浏览器打开面板")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stdout)

    config = load_config(args.config)
    if args.modbus_port:
        config.port = args.modbus_port

    # 1) 后台起模拟器
    _start_simulator_thread(config, args.update_interval)
    time.sleep(1.5)  # 等端口就绪

    # 2) 起数据中心（作为 Modbus 主站连模拟器），单设备包成一员设备群
    fleet = FleetHub.single(config)
    fleet.start()

    # 3) 起 Web 面板
    app = create_app(fleet)
    url = f"http://{args.web_host}:{args.web_port}"
    print("=" * 64)
    print(f"  现场设备监控面板:  {url}")
    print(f"  Modbus 模拟器:     {config.host}:{config.port}  (设备 {config.device_name})")
    print("  按 Ctrl-C 退出")
    print("=" * 64)

    if args.open:
        # 稍等服务端就绪后再打开浏览器，避免首次访问连接被拒
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()

    app.run(host=args.web_host, port=args.web_port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
