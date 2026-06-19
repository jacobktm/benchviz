"""E2E test: system update + merge redirect.

Goes to a system detail page, changes the identifier,
submits the form, and verifies the redirect lands on
a valid page (not a 404).
"""

from playwright.sync_api import sync_playwright
from e2e_config import get_server_url
import datetime
import os
import time


SCREENSHOT_DIR = "screenshots"


def run():
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    base_url = get_server_url()

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        # ── 1. Dashboard → first system ──────────────────────────
        page.goto(f"{base_url}/")
        page.wait_for_selector("a[href*='/system/']", timeout=15000)
        link = page.query_selector("a[href*='/system/']")
        system_url = link.get_attribute("href")
        print(f"  System URL: {system_url}")

        page.goto(f"{base_url}{system_url}")
        page.wait_for_selector("#identifier", timeout=15000)
        page.screenshot(path=f"{SCREENSHOT_DIR}/sysupdate_01_load.png")
        print("✓ System page loaded")

        # ── 2. Read current identifier & append a suffix ──────────
        orig = page.input_value("#identifier")
        new = f"{orig}-e2e-{datetime.datetime.now().strftime('%H%M%S')}"
        page.fill("#identifier", new)
        print(f"  {orig} → {new}")
        page.screenshot(path=f"{SCREENSHOT_DIR}/sysupdate_02_filled.png")

        # ── 3. Submit the first form (Save Profile) ──────────────
        with page.expect_navigation(timeout=15000):
            page.click("form .btn-primary:text('Save Profile')")

        time.sleep(1)
        final_url = page.url
        print(f"  Redirected to: {final_url}")

        # ── 4. Verify the target page loaded (not a 404) ──────────
        body = page.eval_on_selector("body", "el => el.innerText")
        if "404" in body and "Not Found" in body:
            print(f"✗ Redirect landed on 404: {final_url}")
        else:
            print("✓ Redirect landed on a valid page")
        page.screenshot(path=f"{SCREENSHOT_DIR}/sysupdate_03_result.png", full_page=True)

        browser.close()
        print("\n✓ E2E system update test finished")


if __name__ == "__main__":
    run()
