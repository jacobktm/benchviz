"""E2E test: resolution class pooling toggle on the compare page.

Prerequisites:
  - Server running at the URL in e2e_config.json (or http://127.0.0.1:8765).
  - At least 2 systems and 1 benchmark with resolution variations in the DB.
  - playwright (`pip install playwright && playwright install chromium`).

Usage:
  python3 tests/e2e_compare_resolution.py
"""

from playwright.sync_api import sync_playwright
from e2e_config import get_server_url
import time
import os


SCREENSHOT_DIR = "screenshots"


def run():
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    base_url = get_server_url()

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        # Log console messages and network requests for debugging
        page.on("console", lambda msg: print(f"[console] {msg.text}"))
        page.on("response", lambda resp: print(
            f"[{resp.status}] {resp.url[:120]}"
        ) if resp.status >= 400 else None)

        # ── 1. Load compare page ──────────────────────────────────
        page.goto(f"{base_url}/compare")
        page.wait_for_selector("#systemSelect")
        print("✓ Compare page loaded")

        # ── 2. Select two systems that share benchmarks ───────────
        # Query /api/common_benchmarks to find a valid pair.
        option_values = [
            o.get_attribute("value")
            for o in page.query_selector_all("#systemSelect option")
            if o.get_attribute("value")
        ]
        pair = None
        for i, a in enumerate(option_values):
            for b in option_values[i + 1:]:
                resp = page.evaluate(
                    f"fetch('/api/common_benchmarks?system_id={a}&system_id={b}')"
                    ".then(r => r.json()).then(d => (d.benchmarks || []).length)"
                )
                if resp > 0:
                    pair = (a, b)
                    print(f"  Found pair: system {a} + system {b} ({resp} shared benchmarks)")
                    break
            if pair:
                break

        if not pair:
            print("✗ No two systems share a common benchmark")
            browser.close()
            return

        page.select_option("#systemSelect", pair[0])
        page.click("#addSystemBtn")
        time.sleep(0.3)
        page.select_option("#systemSelect", pair[1])
        page.click("#addSystemBtn")
        time.sleep(0.3)
        print("✓ Two systems selected")

        page.screenshot(path=f"{SCREENSHOT_DIR}/resolution_pool_01_systems.png")

        # ── 3. Wait for benchmarks to load ────────────────────────
        # The benchmark panel auto-fetches after systems are added.
        page.wait_for_function(
            "() => {"
            "  const el = document.getElementById('benchmarkPanel');"
            "  if (!el) return false;"
            "  const txt = el.innerText || '';"
            "  return !txt.includes('Select at least one system')"
            "      && !txt.includes('Loading');"
            "}",
            timeout=20000,
        )
        time.sleep(0.3)
        panel_text = page.eval_on_selector("#benchmarkPanel", "el => el.innerText")
        if "No common benchmarks" in panel_text:
            print("⚠ No common benchmarks between selected systems — cannot test further")
            print(f"   Panel: {panel_text[:200]}")
            browser.close()
            return
        print("✓ Benchmark list loaded")

        # ── 4. Select the first benchmark (all configs) ────────────
        # Expand the first benchmark to expose its config rows.
        first_toggle = page.query_selector("#benchmarkPanel .bm-toggle")
        if first_toggle:
            first_toggle.click()
            time.sleep(0.3)

        # Click the "Add" button in the "All configurations" row.
        all_cfg_btn = page.query_selector("#benchmarkPanel .bm-configs .btn-primary-sm")
        if not all_cfg_btn:
            print("✗ No 'All configurations' Add button found")
            browser.close()
            return

        all_cfg_btn.click()
        time.sleep(0.3)
        print("✓ First benchmark selected (all configs)")

        page.screenshot(path=f"{SCREENSHOT_DIR}/resolution_pool_02_benchmark.png")

        # ── 5. Enable resolution pooling ──────────────────────────
        pool_checkbox = page.query_selector("#poolResolutionClasses")
        if not pool_checkbox:
            print("⚠ poolResolutionClasses checkbox not found in DOM")
            print("  (deploy the new compare.html template to test this)")
            page.screenshot(path=f"{SCREENSHOT_DIR}/resolution_pool_no_checkbox.png")
            browser.close()
            return

        pool_checkbox.check()
        print("✓ Resolution pooling enabled")

        # ── 6. Generate comparison ─────────────────────────────────
        page.click("#generateBtn")
        print("✓ Generate clicked, waiting for charts…")

        # Wait for charts to appear (chartsContainer's placeholder text changes)
        page.wait_for_function(
            "() => document.getElementById('chartsContainer')"
            " && !document.getElementById('chartsContainer').innerText.includes('Configure Comparison')",
            timeout=20000,
        )
        time.sleep(2)  # let chart.js render

        page.screenshot(path=f"{SCREENSHOT_DIR}/resolution_pool_03_charts.png", full_page=True)
        print("✓ Charts rendered, screenshot saved")

        # ── 7. Disable pooling and regenerate ─────────────────────
        # The configure accordion may have collapsed after generation; re-open it.
        accordion_summary = page.query_selector("#configureAccordion summary")
        if accordion_summary:
            accordion_summary.click()
            time.sleep(0.3)
        pool_checkbox.uncheck()
        page.click("#generateBtn")
        time.sleep(2)

        page.screenshot(path=f"{SCREENSHOT_DIR}/resolution_pool_04_unpooled.png", full_page=True)
        print("✓ Unpooled charts rendered")

        browser.close()
        print("\n✓ E2E resolution pooling test finished")


if __name__ == "__main__":
    run()
