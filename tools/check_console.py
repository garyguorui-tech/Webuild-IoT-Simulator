"""加载面板并捕获浏览器 console 错误（验证前端无运行时报错）。"""
import sys
import time

from playwright.sync_api import sync_playwright

url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
errors = []
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page()
    page.on("console", lambda m: errors.append((m.type, m.text))
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errors.append(("pageerror", str(e))))
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(5)
    browser.close()

if errors:
    print("发现 console 问题:")
    for t, msg in errors:
        print(f"  [{t}] {msg}")
    sys.exit(1)
print("OK: 无 console error / warning")
