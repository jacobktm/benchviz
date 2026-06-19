from playwright.sync_api import sync_playwright
from e2e_config import get_server_url
import time

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--no-sandbox', '--disable-setuid-sandbox'])
        page = browser.new_page()
        page.goto(f"{get_server_url()}/compare")
        
        print("Page loaded.")
        
        # Check initial systems
        page.wait_for_selector(".system-checkbox-item")
        system_checkboxes = page.query_selector_all(".system-checkbox-item input")
        
        print(f"Found {len(system_checkboxes)} system checkboxes.")
        if len(system_checkboxes) >= 2:
            system_checkboxes[0].check()
            system_checkboxes[1].check()
            print("Checked two systems.")
        else:
            print("Not enough systems to compare.")
            browser.close()
            return
            
        page.screenshot(path="screenshots/compare_step1.png")
            
        # Click Find Benchmarks
        page.click("#findBenchmarksBtn")
        print("Clicked Find Benchmarks.")
        
        # Wait for benchmark list to populate
        time.sleep(1)
        page.wait_for_selector("#benchmarkList input")
        bm_checkboxes = page.query_selector_all("#benchmarkList input[name='benchmarks']")
        
        print(f"Found {len(bm_checkboxes)} common benchmarks.")
        if len(bm_checkboxes) > 0:
            bm_checkboxes[0].check()
            print("Checked a common benchmark.")
        else:
            print("No common benchmarks found.")
            browser.close()
            return
            
        page.screenshot(path="screenshots/compare_step2.png")
            
        # Click Generate
        page.click("#generateBtn")
        print("Clicked Generate Comparison.")
        
        # Wait for charts to load
        time.sleep(3)
        page.screenshot(path="screenshots/compare_final.png", full_page=True)
        print("Final screenshot taken.")
        browser.close()

if __name__ == "__main__":
    run()
