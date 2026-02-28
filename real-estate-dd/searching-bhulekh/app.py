#!/usr/bin/env python3
"""
Bhulekh Odisha RoR PDF Downloader
Web interface to automate downloading Record of Rights PDFs from bhulekh.ori.nic.in
"""

import argparse
import asyncio
import base64
import json
import os
import queue
import sys
import threading
import uuid

from flask import Flask, render_template, request, jsonify, Response, send_from_directory
from playwright.async_api import async_playwright

app = Flask(__name__)

OUTPUT_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
URL = "https://bhulekh.ori.nic.in/RoRView.aspx"

# ASP.NET element selectors — verified via --discover
SELECTORS = {
    "district": "#ctl00_ContentPlaceHolder1_ddlDistrict",
    "tahsil": "#ctl00_ContentPlaceHolder1_ddlTahsil",
    "ri_circle": "#ctl00_ContentPlaceHolder1_ddlRI",
    "village": "#ctl00_ContentPlaceHolder1_ddlVillage",
    "khatiyan_radio": "#ctl00_ContentPlaceHolder1_rbtnRORSearchtype_0",
    "khatiyan_dropdown": "#ctl00_ContentPlaceHolder1_ddlBindData",
    "view_ror_btn": "#ctl00_ContentPlaceHolder1_btnRORFront",
    "refresh_btn": "#ctl00_ContentPlaceHolder1_btnRefresh",
    "session_restart": "#ctl00_ContentPlaceHolder1_LinkButton1",
}

# Store progress queues per job
jobs = {}


class RorTargetNavigationError(RuntimeError):
    """Raised when View RoR does not resolve to SRoRFront_Uni.aspx in time."""

    def __init__(self, message, main_url=None, popup_seen=False, page_urls=None):
        super().__init__(message)
        self.main_url = main_url
        self.popup_seen = popup_seen
        self.page_urls = page_urls or []


def send_event(job_id, event_type, data):
    """Push a progress event to the job's queue."""
    if job_id in jobs:
        jobs[job_id]["events"].put({"type": event_type, "data": data})



async def handle_session_timeout(page):
    """If the page shows a session timeout, click the restart link."""
    text = await page.inner_text("body")
    if "timed out" in text.lower():
        restart = await page.query_selector(SELECTORS["session_restart"])
        if restart:
            await restart.click()
            await page.wait_for_load_state("networkidle", timeout=30000)
            return True
    return False


async def select_and_wait_postback(page, selector, visible_text=None, index=None):
    """Select a dropdown option and wait for the ASP.NET AJAX postback response."""
    dropdown = page.locator(selector)
    await dropdown.wait_for(state="visible", timeout=15000)

    try:
        async with page.expect_response(
            lambda r: "RoRView.aspx" in r.url and r.request.method == "POST",
            timeout=30000,
        ):
            if visible_text is not None:
                await page.select_option(selector, label=visible_text)
            elif index is not None:
                await page.select_option(selector, index=index)
    except Exception:
        # Fallback: just wait a bit if response wasn't caught
        await page.wait_for_timeout(3000)

    # Give DOM time to update after AJAX response
    await page.wait_for_timeout(1000)


async def save_page_as_pdf(page, pdf_path):
    """Save a page as PDF using CDP (works in headed and headless mode)."""
    cdp = await page.context.new_cdp_session(page)
    result = await cdp.send("Page.printToPDF", {
        "landscape": False,
        "printBackground": True,
        "preferCSSPageSize": True,
    })
    pdf_bytes = base64.b64decode(result["data"])
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)
    await cdp.detach()
    return len(pdf_bytes)


async def wait_for_ror_target_page(page, context, timeout_ms=45000):
    """Click View RoR and resolve the page that reaches SRoRFront_Uni.aspx."""
    popup_state = {"seen": False}

    async def wait_for_popup_target():
        popup = await context.wait_for_event("page", timeout=timeout_ms)
        popup_state["seen"] = True
        await popup.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        await popup.wait_for_url("**/SRoRFront_Uni.aspx", timeout=timeout_ms)
        return popup

    same_tab_task = asyncio.create_task(
        page.wait_for_url("**/SRoRFront_Uni.aspx", timeout=timeout_ms)
    )
    popup_task = asyncio.create_task(wait_for_popup_target())
    pending = {same_tab_task, popup_task}
    same_tab_error = None
    popup_error = None

    try:
        await page.locator(SELECTORS["view_ror_btn"]).click()

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is same_tab_task:
                    if task.exception() is None:
                        for other in pending:
                            other.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                        return page
                    same_tab_error = task.exception()
                elif task is popup_task:
                    if task.exception() is None:
                        for other in pending:
                            other.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                        return task.result()
                    popup_error = task.exception()

        raise RorTargetNavigationError(
            (
                "SRoR target not reached after clicking View RoR. "
                f"same_tab_error={same_tab_error}; popup_error={popup_error}"
            ),
            main_url=page.url,
            popup_seen=popup_state["seen"],
            page_urls=[p.url for p in context.pages],
        )
    except Exception as exc:
        if not isinstance(exc, RorTargetNavigationError):
            raise RorTargetNavigationError(
                f"SRoR target not reached: {exc}",
                main_url=page.url,
                popup_seen=popup_state["seen"],
                page_urls=[p.url for p in context.pages],
            ) from exc
        raise
    finally:
        for task in (same_tab_task, popup_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(same_tab_task, popup_task, return_exceptions=True)


async def navigate_and_select_location(page, district, tahsil, village):
    """Navigate to the portal and select district/tahsil/village, then click Khatiyan radio."""
    # Handle session timeout if present
    await handle_session_timeout(page)

    # Select District
    await select_and_wait_postback(page, SELECTORS["district"], visible_text=district)

    # Select Tahsil
    await select_and_wait_postback(page, SELECTORS["tahsil"], visible_text=tahsil)

    # Select Village (RI Circle is disabled, skip it)
    await select_and_wait_postback(page, SELECTORS["village"], visible_text=village)

    # Click the Khatiyan radio button to reveal the khatiyan dropdown.
    # The radio's onclick uses __doPostBack which may not fire reliably with force-click,
    # so we also trigger the postback via JS as a fallback.
    try:
        async with page.expect_response(
            lambda r: "RoRView.aspx" in r.url and r.request.method == "POST",
            timeout=15000,
        ):
            await page.click(SELECTORS["khatiyan_radio"], force=True)
    except Exception:
        # Force the postback via JS if the click didn't trigger it
        await page.evaluate(
            "javascript:setTimeout(\"__doPostBack('ctl00$ContentPlaceHolder1$rbtnRORSearchtype$0','')\", 0)"
        )
        await page.wait_for_timeout(3000)

    await page.wait_for_timeout(2000)

    # Verify khatiyan dropdown appeared
    khatiyan_dd = page.locator(SELECTORS["khatiyan_dropdown"])
    await khatiyan_dd.wait_for(state="visible", timeout=15000)


async def process_single_khatiyan(context, khatiyan, output_dir, job_id, district, tahsil, village):
    """Open a fresh page, select location + khatiyan, click View RoR, save as PDF."""
    khatiyan = khatiyan.strip()
    safe_name = khatiyan.replace("/", "_").replace("\\", "_").replace(" ", "_")
    pdf_path = os.path.join(output_dir, f"{safe_name}.pdf")

    page = await context.new_page()
    target_page = None
    try:
        await page.goto(URL, wait_until="networkidle", timeout=60000)
        await handle_session_timeout(page)

        # Select location
        await navigate_and_select_location(page, district, tahsil, village)

        selector = SELECTORS["khatiyan_dropdown"]
        dropdown = page.locator(selector)
        await dropdown.wait_for(state="visible", timeout=15000)

        # Check if the khatiyan exists in the dropdown
        options = await page.eval_on_selector(
            selector,
            "el => Array.from(el.options).map(o => o.text.trim())"
        )

        if khatiyan not in options:
            send_event(job_id, "error", {
                "khatiyan": khatiyan,
                "message": f"Khatiyan '{khatiyan}' not found in dropdown. Available: {options[:15]}..."
            })
            return False

        # Select the khatiyan and wait for postback update.
        await select_and_wait_postback(page, selector, visible_text=khatiyan)

        target_page = await wait_for_ror_target_page(page, context, timeout_ms=45000)

        # Save the RoR page as PDF
        pdf_size = await save_page_as_pdf(target_page, pdf_path)
        source_url = target_page.url

        send_event(job_id, "success", {
            "khatiyan": khatiyan,
            "filename": f"{safe_name}.pdf",
            "size": pdf_size,
            "source_url": source_url,
        })
        return True

    except RorTargetNavigationError as e:
        send_event(job_id, "error", {
            "khatiyan": khatiyan,
            "message": str(e),
            "reason": "SRoR target not reached",
            "main_url": e.main_url,
            "popup_seen": e.popup_seen,
            "page_urls": e.page_urls,
        })
        return False
    except Exception as e:
        send_event(job_id, "error", {
            "khatiyan": khatiyan,
            "message": str(e),
        })
        return False
    finally:
        if target_page is not None and target_page is not page and not target_page.is_closed():
            await target_page.close()
        await page.close()


async def run_scraping_job(job_id, district, tahsil, village, khatiyans):
    """Main scraping coroutine — runs in a background thread's event loop."""
    output_dir = os.path.join(OUTPUT_BASE, job_id)
    os.makedirs(output_dir, exist_ok=True)

    succeeded = 0
    failed = 0

    send_event(job_id, "status", {"message": "Launching browser..."})

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        try:
            send_event(job_id, "status", {"message": "Browser launched. Starting downloads..."})

            # Process each khatiyan in a fresh page
            for i, khatiyan in enumerate(khatiyans):
                khatiyan = khatiyan.strip()
                if not khatiyan:
                    continue

                send_event(job_id, "status", {
                    "message": f"Processing khatiyan {i + 1}/{len(khatiyans)}: {khatiyan}"
                })

                success = await process_single_khatiyan(context, khatiyan, output_dir, job_id, district, tahsil, village)
                if success:
                    succeeded += 1
                else:
                    failed += 1

        except Exception as e:
            send_event(job_id, "error", {"khatiyan": "N/A", "message": f"Fatal error: {e}"})
        finally:
            await browser.close()

    send_event(job_id, "done", {"succeeded": succeeded, "failed": failed})


def run_in_thread(job_id, district, tahsil, village, khatiyans):
    """Run the async scraping job in a background thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(
        run_scraping_job(job_id, district, tahsil, village, khatiyans)
    )
    loop.close()


# ─── Flask Routes ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    data = request.json
    district = data.get("district", "").strip()
    tahsil = data.get("tahasil", "").strip() or data.get("tahsil", "").strip()
    village = data.get("village", "").strip()
    khatiyans_raw = data.get("khatiyans", "").strip()

    if not all([district, tahsil, village, khatiyans_raw]):
        return jsonify({"error": "District, Tahsil, Village, and Khatiyans are required"}), 400

    khatiyans = [k.strip() for k in khatiyans_raw.split(",") if k.strip()]
    if not khatiyans:
        return jsonify({"error": "No valid khatiyan numbers provided"}), 400

    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "events": queue.Queue(),
        "params": {
            "district": district,
            "tahsil": tahsil,
            "village": village,
            "khatiyans": khatiyans,
        },
    }

    thread = threading.Thread(
        target=run_in_thread,
        args=(job_id, district, tahsil, village, khatiyans),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id):
    if job_id not in jobs:
        return "Job not found", 404

    def event_stream():
        q = jobs[job_id]["events"]
        while True:
            try:
                event = q.get(timeout=60)
                yield f"data: {json.dumps(event)}\n\n"
                if event["type"] == "done":
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(event_stream(), mimetype="text/event-stream")


@app.route("/download/<job_id>/<filename>")
def download(job_id, filename):
    dir_path = os.path.join(OUTPUT_BASE, job_id)
    if not os.path.isdir(dir_path):
        return "Job not found", 404
    return send_from_directory(dir_path, filename, as_attachment=True)


# ─── Discovery Mode ─────────────────────────────────────────────────────────

async def discover(headless=False):
    """Print all form element IDs from the Bhulekh page."""
    mode = "headless" if headless else "headed"
    print(f"Opening Bhulekh page in {mode} mode for element discovery...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()
        await navigate_to_portal(page)

        # Handle session timeout
        text = await page.inner_text("body")
        if "timed out" in text.lower():
            restart = await page.query_selector(SELECTORS["session_restart"])
            if restart:
                await restart.click()
                await page.wait_for_load_state("networkidle", timeout=30000)
                print("  (handled session timeout)")

        print("\n=== SELECT elements ===")
        selects = await page.query_selector_all("select")
        for s in selects:
            id_val = await s.get_attribute("id") or "(no id)"
            name_val = await s.get_attribute("name") or "(no name)"
            options_count = await s.evaluate("el => el.options.length")
            disabled = await s.evaluate("el => el.disabled")
            first_options = await s.evaluate(
                "el => Array.from(el.options).slice(0, 5).map(o => o.text.trim())"
            )
            print(f"  id={id_val}  name={name_val}  options={options_count}  disabled={disabled}  first={first_options}")

        print("\n=== RADIO buttons ===")
        radios = await page.query_selector_all("input[type=radio]")
        for r in radios:
            id_val = await r.get_attribute("id") or "(no id)"
            name_val = await r.get_attribute("name") or "(no name)"
            value = await r.get_attribute("value") or "(no value)"
            disabled = await r.get_attribute("disabled")
            print(f"  id={id_val}  name={name_val}  value={value}  disabled={disabled}")

        print("\n=== INPUT[type=submit] / BUTTON elements ===")
        buttons = await page.query_selector_all("input[type=submit], input[type=button], button")
        for b in buttons:
            id_val = await b.get_attribute("id") or "(no id)"
            value = await b.get_attribute("value") or ""
            visible = await b.is_visible()
            print(f"  id={id_val}  value={value}  visible={visible}")

        if not headless:
            print("\nDone. Close the browser window to exit.")
            await page.wait_for_event("close", timeout=300000)
        else:
            print("\nDiscovery complete.")
        await browser.close()


# ─── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bhulekh Odisha RoR PDF Downloader")
    parser.add_argument("--discover", action="store_true",
                        help="Open browser (headed) and print all form element IDs")
    parser.add_argument("--discover-headless", action="store_true",
                        help="Discover element IDs in headless mode (no GUI)")
    parser.add_argument("--port", type=int, default=2173, help="Flask port (default 5000)")
    args = parser.parse_args()

    if args.discover or args.discover_headless:
        asyncio.run(discover(headless=args.discover_headless))
    else:
        os.makedirs(OUTPUT_BASE, exist_ok=True)
        print(f"Starting server on http://localhost:{args.port}")
        app.run(debug=True, port=args.port, threaded=True)
