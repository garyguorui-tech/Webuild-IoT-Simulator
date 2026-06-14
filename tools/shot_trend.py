"""聚焦截图：只截历史趋势区，清晰展示每个变量的时间序列。"""
import sys
import time

from playwright.sync_api import sync_playwright

url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8230/dataset"
out = sys.argv[2] if len(sys.argv) > 2 else "docs/dataset-trends.png"

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 1000}, device_scale_factor=2)
    pg.goto(url, wait_until="domcontentloaded")
    time.sleep(4)               # 等趋势图渲染
    # 只截「历史趋势」整个 section
    sec = pg.query_selector("xpath=//h2[contains(.,'历史趋势')]/ancestor::section")
    (sec or pg).screenshot(path=out)
    b.close()
print("saved", out)
