"""一键演示（多设备）：单进程同时启动设备群里的全部模拟器 + 总览面板。

读取 config/fleet.yaml，为每台设备起一个 Modbus 模拟器（各占端口），
再起 Web 服务提供「设备群总览 + 单设备详情」面板。

    python run_fleet.py                 # 总览 http://127.0.0.1:8000
    python run_fleet.py --web-port 8080 --open
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
from simulator.points import load_fleet  # noqa: E402
from web.app import FleetHub, create_app  # noqa: E402

DEFAULT_FLEET = Path(__file__).resolve().parent / "config" / "fleet.yaml"


def _start_simulator_thread(config, update_interval: float,
                            bind_host: str | None = None) -> None:
    """在后台线程的独立事件循环里运行一台 Modbus 模拟器。"""
    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        sim = ModbusSimulator(config, update_interval=update_interval,
                              bind_host=bind_host)
        loop.run_until_complete(sim.serve())

    threading.Thread(target=_run, name=f"sim-{config.device_name}", daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="设备群模拟器 + 总览面板 一键演示")
    parser.add_argument("--fleet", default=str(DEFAULT_FLEET))
    parser.add_argument("--web-host", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8000)
    parser.add_argument("--modbus-bind", default=None,
                        help="覆盖各模拟器的监听地址（容器内用 0.0.0.0 才能被跨容器访问）")
    parser.add_argument("--update-interval", type=float, default=0.5)
    parser.add_argument("--open", action="store_true", help="自动打开浏览器")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stdout)

    members = load_fleet(args.fleet)

    # 1) 后台起全部模拟器（绑定地址可单独覆盖为 0.0.0.0，config.host 仍供本进程客户端连接）
    for m in members:
        _start_simulator_thread(m.config, args.update_interval,
                                bind_host=args.modbus_bind)
    time.sleep(1.5)  # 等所有端口就绪

    # 2) 起设备群数据中心（每台一个 Modbus 主站）
    fleet = FleetHub.from_members(members)
    fleet.start()

    # 3) 起 Web 总览面板
    app = create_app(fleet)
    url = f"http://{args.web_host}:{args.web_port}"
    print("=" * 64)
    print(f"  设备群监控总览:  {url}")
    print(f"  设备 ({len(members)}):", ", ".join(
        f"{m.name}@{m.config.port}" for m in members))
    print("  按 Ctrl-C 退出")
    print("=" * 64)

    if args.open:
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()

    app.run(host=args.web_host, port=args.web_port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
