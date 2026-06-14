"""用 Playwright 给运行中的监控面板截图（开发期生成 README 配图用）。

    python tools/screenshot.py http://127.0.0.1:8000 docs/dashboard.png
"""
import sys
import time

from playwright.sync_api import sync_playwright


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    out = sys.argv[2] if len(sys.argv) > 2 else "dashboard.png"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1024},
                                device_scale_factor=2)
        # 注意：SSE 长连接会让 networkidle 永不触发，这里用 domcontentloaded
        page.goto(url, wait_until="domcontentloaded")
        # 等若干秒让 SSE 推几帧、趋势曲线长出来
        time.sleep(8)
        page.screenshot(path=out, full_page=True)
        browser.close()
    print("saved", out)


if __name__ == "__main__":
    main()
