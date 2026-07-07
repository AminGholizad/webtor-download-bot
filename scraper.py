import os
import shutil
import threading
from typing import Any
import time
from playwright.sync_api import BrowserContext, Page, Playwright
from playwright_stealth import Stealth
from result import Err, Ok, Result
from tqdm import tqdm


def get_browser_context(pl: Playwright) -> tuple[BrowserContext, Page]:
    """
    Creates a unique, isolated browser context per running worker thread.
    This completely prevents ProcessSingleton lock collisions during concurrent token refreshes.
    """
    # Create an isolated path using both Process ID and Thread ID
    pid = os.getpid()
    tid = threading.get_ident()
    unique_profile_path = os.path.abspath(f"./webtor_session_{pid}_{tid}")

    try:
        context = pl.chromium.launch_persistent_context(
            unique_profile_path,
            headless=False,  # xvfb handles this
            args=["--disable-blink-features=AutomationControlled"],
        )
    except Exception as e:
        tqdm.write(f"❌ Failed to initialize unique browser session: {e}")
        raise e

    # Inject a custom cleanup handler so the isolated folder drops off disk when closed
    original_close = context.close

    def custom_close_and_cleanup():
        original_close()
        try:
            if os.path.exists(unique_profile_path):
                shutil.rmtree(unique_profile_path)
        except Exception:
            pass  # Fail silently if files are momentarily held by OS exit structures

    context.close = custom_close_and_cleanup

    context.grant_permissions(["clipboard-read", "clipboard-write"])
    page = context.pages[0]
    Stealth().apply_stealth_sync(page)
    return context, page


def scrape_webtor_url(page: Page, magnet: str, title: str) -> Result[str, str]:
    """
    Scrapes the webtor.io site for a direct URL link for a given magnet link.
    Returns a Result containing either the URL string or an error message.
    """
    try:
        tqdm.write(f"🌐 Fetching Webtor URL for: {title[:20]}...")
        page.goto("https://webtor.io/", wait_until="domcontentloaded")

        search_input = page.wait_for_selector('input[placeholder*="magnet" i]')
        if not search_input:
            msg = "FAILED: Magnet input not found"
            tqdm.write(f"❌ Scrape error on {title}: {msg}")
            return Err(msg)

        search_input.fill(magnet)
        search_input.press("Enter")

        zip_btn = page.wait_for_selector("button:has-text('ZIP')", timeout=180000)
        if not zip_btn:
            msg = "FAILED: ZIP button not found"
            tqdm.write(f"❌ Scrape error on {title}: {msg}")
            return Err(msg)
        zip_btn.click()

        url_btn = page.wait_for_selector("a:text-is('URL')", timeout=100000)
        if not url_btn:
            msg = "FAILED: URL copy button not found"
            tqdm.write(f"❌ Scrape error on {title}: {msg}")
            return Err(msg)
        url_btn.click()

        time.sleep(2) # Safe clipboard buffer
        captured_url = page.evaluate("navigator.clipboard.readText()").strip()

        if captured_url and captured_url.startswith("http"):
            return Ok(captured_url)
        else:
            msg = "FAILED: Scraping Error (No valid URL captured)"
            tqdm.write(f"❌ Failed to grab URL link for {title}")
            return Err(msg)
    except Exception as e:
        msg = f"FAILED: Browser error ({type(e).__name__})"
        tqdm.write(f"❌ Scrape error on {title}: {e}")
        return Err(msg)
