"""给数据集构建器截图：加载 -> 取消选一个设备 -> 点预览 -> 截全图。"""
import sys
import time

from playwright.sync_api import sync_playwright

url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8200/dataset"
out = sys.argv[2] if len(sys.argv) > 2 else "docs/dataset-builder.png"

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1480, "height": 1080}, device_scale_factor=2)
    pg.goto(url, wait_until="domcontentloaded")
    time.sleep(3)               # 等 schema 渲染
    # 切磁盘来源（完整历史）、勾标签、开滑窗、选划分、点预览
    try:
        pg.click("#srcSeg button[data-src='disk']")
        time.sleep(2.5)         # 等磁盘趋势重载
        pg.check("#lblOOR")
        pg.check("#lblPerDev")
        pg.click("#winSeg button[data-win='10']")
        time.sleep(0.3)
        pg.click("#winStatsSeg button[data-stat='slope']")
        pg.click("#splitSeg button[data-split='70/15/15']")
        pg.click("#btnPreview")
        time.sleep(1.5)
    except Exception as e:
        print("interact warn:", e)
    time.sleep(1)
    pg.screenshot(path=out, full_page=True)
    b.close()
print("saved", out)
