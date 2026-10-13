"""
visual_ui_validation.py
Automated Playwright Visual UI Validation & Interaction Testing for ArbForge.
Navigates through the Terminal Dashboard (/) and Interactive Docs (/docs-interactive),
tests all controls, captures console logs, validates DOM state, and saves screenshots.
"""

import json
import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ARTIFACT_DIR = Path(r"C:\Users\Abhishek Mishra\.gemini\antigravity\brain\88a8af7c-3c26-40e5-a17e-cdbd36d409c3")
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

DOCS_SCREENSHOT_DIR = Path(r"c:\Projects\ArbForge\docs\screenshots")
DOCS_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

console_messages = []
page_errors = []

def run_visual_validation():
    print("[1/5] Starting Playwright browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=1.25)
        page = context.new_page()

        # Capture console messages and errors
        page.on("console", lambda msg: console_messages.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda err: page_errors.append(str(err)))

        # -------------------------------------------------------------------
        # Test 1: Terminal Dashboard (http://127.0.0.1:8000/)
        # -------------------------------------------------------------------
        print("[2/5] Navigating to Terminal Dashboard at http://127.0.0.1:8000/ ...")
        page.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded")

        # Wait for SSE data to arrive and render
        print("  Waiting for SSE live data stream...")
        page.wait_for_selector("#statusBadge.live", timeout=15000)
        page.wait_for_selector("#matrixBody tr td:first-child", timeout=15000)
        # Give 3 seconds to let ticks flash and pulse render
        time.sleep(3.0)

        # Validate header metrics
        status_text = page.locator("#statusText").inner_text()
        pairs_count = page.locator("#metricPairs").inner_text()
        clock_text = page.locator("#clock").inner_text()
        print(f"  Status: {status_text} | Pairs: {pairs_count} | Clock: {clock_text}")
        assert status_text == "LIVE", f"Expected LIVE status, got {status_text}"
        assert int(pairs_count) == 7, f"Expected 7 pairs, got {pairs_count}"

        # Validate Orderbook Matrix rows
        rows = page.locator("#matrixBody tr").all()
        print(f"  Orderbook Matrix has {len(rows)} rows.")
        assert len(rows) == 7, f"Expected 7 rows in orderbook matrix, got {len(rows)}"

        # Validate Triangular Arbitrage Cycles
        cycle_cards = page.locator("#cyclesPanel .cycle-card").all()
        print(f"  Triangular Arbitrage Panel has {len(cycle_cards)} cycle cards.")
        assert len(cycle_cards) == 6, f"Expected 6 cycle cards, got {len(cycle_cards)}"

        # Validate Legs formatting
        leg_rows = page.locator(".cycle-card .leg-row").all()
        first_leg_action = page.locator(".cycle-card .leg-row .leg-action").first.inner_text()
        print(f"  Total leg rows rendered: {len(leg_rows)} | First leg action: '{first_leg_action}'")
        assert "BUY @ Ask" in first_leg_action or "SELL @ Bid" in first_leg_action

        # Test Controls Interaction: Sliders, Capital buttons, Audio toggle
        print("  Testing Interactive Controls...")
        # 1. Yield Threshold slider
        threshold_slider = page.locator("#thresholdSlider")
        threshold_slider.evaluate("el => { el.value = '0.10'; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }")
        time.sleep(0.5)
        thresh_val = page.locator("#thresholdValue").inner_text()
        print(f"  Threshold slider updated to: {thresh_val}")
        assert "0.10%" in thresh_val

        # 2. Fee slider
        fee_slider = page.locator("#feeSlider")
        fee_slider.evaluate("el => { el.value = '5.0'; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }")
        time.sleep(0.5)
        fee_val = page.locator("#feeValue").inner_text()
        print(f"  Fee slider updated to: {fee_val}")
        assert "5.0 bps" in fee_val

        # 3. Capital Selector ($50K)
        page.locator('.capital-btn[data-val="50000"]').click()
        time.sleep(0.5)
        active_btn = page.locator(".capital-btn.active").inner_text()
        print(f"  Active Capital button: {active_btn}")
        assert "$50K" in active_btn

        # 4. Audio toggle
        page.locator("#audioToggle").click()
        time.sleep(0.3)
        audio_has_on = "on" in (page.locator("#audioToggle").get_attribute("class") or "")
        print(f"  Audio toggle state: {'ON' if audio_has_on else 'OFF'}")
        assert audio_has_on, "Audio toggle should have class 'on'"

        # 5. Audit Log verification
        audit_entries = page.locator("#auditLog .log-entry").all()
        print(f"  Audit log entries count: {len(audit_entries)}")
        assert len(audit_entries) >= 4, f"Expected at least 4 audit log entries, got {len(audit_entries)}"

        # Reset sliders back to default
        threshold_slider.evaluate("el => { el.value = '0.05'; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }")
        fee_slider.evaluate("el => { el.value = '10.0'; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }")
        page.locator('.capital-btn[data-val="10000"]').click()
        time.sleep(1.0)

        # Capture Dashboard Screenshots
        dash_screenshot_path = ARTIFACT_DIR / "terminal_dashboard.png"
        dash_doc_path = DOCS_SCREENSHOT_DIR / "terminal_dashboard.png"
        page.screenshot(path=str(dash_screenshot_path), full_page=False)
        page.screenshot(path=str(dash_doc_path), full_page=False)
        print(f"[3/5] Saved terminal dashboard screenshot to: {dash_screenshot_path} and {dash_doc_path}")

        # -------------------------------------------------------------------
        # Test 2: Interactive Documentation (http://127.0.0.1:8000/docs-interactive)
        # -------------------------------------------------------------------
        print("[4/5] Navigating to Interactive Docs at http://127.0.0.1:8000/docs-interactive ...")
        page.goto("http://127.0.0.1:8000/docs-interactive", wait_until="domcontentloaded")
        time.sleep(1.5)

        # Validate Engine Health Badge in Docs Header
        doc_health = page.locator("#headerStatusText").inner_text()
        print(f"  Docs Header Health Badge: {doc_health}")
        assert "LIVE" in doc_health or "OK" in doc_health

        # Test Interactive Simulator: Click "Profitable Forward" preset
        print("  Testing Interactive Simulator Presets...")
        page.locator(".preset-btn[onclick*='profitable']").click()
        time.sleep(0.5)
        res_net = page.locator("#resNet").inner_text()
        print(f"  Simulator Net Yield with Profitable Forward: {res_net}")
        assert "+" in res_net or float(res_net.replace("%", "").replace("+", "")) > 0, f"Expected positive yield with Profitable Forward, got {res_net}"

        # Test Interactive Graph Explorer: Click "ETH" node on Canvas
        print("  Testing Interactive Graph Canvas Click...")
        graph_canvas = page.locator("#interactiveGraph")
        graph_canvas.scroll_into_view_if_needed()
        time.sleep(0.5)
        canvas_box = graph_canvas.bounding_box()
        scale_x = canvas_box["width"] / 600
        scale_y = canvas_box["height"] / 340
        graph_canvas.click(position={"x": 410 * scale_x, "y": 300 * scale_y})
        time.sleep(0.5)
        graph_info = page.locator("#graphInfo").inner_text()
        print(f"  Graph Explorer Info: '{graph_info}'")
        assert "ETH" in graph_info

        # Test Interactive API Console Tabs: Matrix, Cycles, Health, Config GET, Config POST
        print("  Testing Interactive API Console Tabs...")
        page.locator(".api-tester").scroll_into_view_if_needed()
        time.sleep(0.5)
        for tab_id in ["matrix", "cycles", "health", "config_get", "config_post"]:
            page.locator(f".api-tab[onclick*=\"'{tab_id}'\"]").click()
            time.sleep(0.3)
            page.locator(".api-bar button:has-text('Send Request')").click()
            time.sleep(1.0)
            resp_text = page.locator("#apiResponse").inner_text()
            print(f"    Tab '{tab_id}' response status: {resp_text[:30]}...")
            assert "HTTP 200 OK" in resp_text, f"Expected HTTP 200 OK for {tab_id}, got: {resp_text[:100]}"

        # Test Troubleshooting Accordion
        print("  Testing Troubleshooting Accordion...")
        page.locator("#troubleshooting").scroll_into_view_if_needed()
        time.sleep(0.5)
        accordion_items = page.locator(".accordion-item").all()
        print(f"  Accordion items found: {len(accordion_items)}")
        assert len(accordion_items) >= 3
        # Click 2nd item to open
        page.locator(".accordion-header").nth(1).click()
        time.sleep(0.3)
        is_open = "open" in (page.locator(".accordion-item").nth(1).get_attribute("class") or "")
        assert is_open, "Second accordion item should be opened after click"

        # Capture Docs Screenshots
        docs_screenshot_path = ARTIFACT_DIR / "interactive_docs.png"
        docs_doc_path = DOCS_SCREENSHOT_DIR / "interactive_docs.png"
        page.screenshot(path=str(docs_screenshot_path), full_page=False)
        page.screenshot(path=str(docs_doc_path), full_page=False)
        print(f"[5/5] Saved interactive docs screenshot to: {docs_screenshot_path} and {docs_doc_path}")

        # Capture full page of docs simulator section
        sim_box = page.locator("#simulator").bounding_box()
        if sim_box:
            sim_screenshot_path = ARTIFACT_DIR / "docs_simulator.png"
            sim_doc_path = DOCS_SCREENSHOT_DIR / "docs_simulator.png"
            page.locator("#simulator").screenshot(path=str(sim_screenshot_path))
            page.locator("#simulator").screenshot(path=str(sim_doc_path))
            print(f"  Saved simulator screenshot to: {sim_screenshot_path}")

        browser.close()

    print("\n--- Summary of Console Messages ---")
    for msg in console_messages:
        print(" ", msg)
    print("--- Summary of Page Errors ---")
    for err in page_errors:
        print(" ", err)
    assert len(page_errors) == 0, f"Encountered page errors: {page_errors}"
    print("\n>>> ALL VISUAL UI VALIDATIONS AND FUNCTIONAL FLOWS PASSED PERFECTLY! <<<")

if __name__ == "__main__":
    run_visual_validation()
