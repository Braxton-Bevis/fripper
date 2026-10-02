"""Render each artboard in artboards.html to docs/media/<id>.png using Playwright.

Run: .venv\\Scripts\\python.exe docs\\design\\render.py
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".local" / "browsers"))

from playwright.sync_api import sync_playwright  # noqa: E402

SOURCE = Path(__file__).with_name("artboards.html")
OUT = ROOT / "docs" / "media"
BOARDS = ("banner", "poster", "flyer", "story", "brand", "steps")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1700, "height": 1200}, device_scale_factor=1)
        page.goto(SOURCE.as_uri(), wait_until="networkidle")
        page.wait_for_function("document.querySelector('#flyer-qr img')?.src.startsWith('data:')")
        page.evaluate("document.fonts.ready")
        for board in BOARDS:
            target = OUT / f"{board}.png"
            page.locator(f"#{board}").screenshot(path=str(target))
            print("Wrote", target)
        browser.close()


if __name__ == "__main__":
    main()
