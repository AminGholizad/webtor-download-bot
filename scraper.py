import os
import time
from typing import Any
import time
from playwright.sync_api import BrowserContext, Page, Playwright
from playwright_stealth import Stealth
from result import Err, Ok, Result
from tqdm import tqdm


def get_browser_context(
    pl: Playwright, max_wait_seconds: int = 300
) -> tuple[BrowserContext, Page]:
    """
    Creates a configured browser context with stealth and clipboard permissions.
    If the session directory is locked by another script instance, it will wait
    until it is released or the timeout is reached.
    """
    start_time = time.time()

    while True:
        try:
            context = pl.chromium.launch_persistent_context(
                "./webtor_session",
                headless=False,  # xvfb handles this
                args=["--disable-blink-features=AutomationControlled"],
            )
            # If successful, break out of the retry loop
            break
        except Exception as e:
            # Check if the error message indicates a locked profile folder
            error_msg = str(e).lower()
            if (
                "profile already in use" in error_msg
                or "lock" in error_msg
                or "target closed" in error_msg
            ):
                elapsed = int(time.time() - start_time)
                if elapsed >= max_wait_seconds:
                    print(
                        f"\n❌ Critical: Playwright session directory remained locked for over {max_wait_seconds}s. Exiting."
                    )
                    raise e

                print(
                    f"⏳ Session profile folder is currently locked by another instance. Retrying in 5 seconds... ({elapsed}s elapsed)",
                    end="\r",
                )
                time.sleep(5)
            else:
                # If it's a completely unrelated Playwright crash, raise it immediately
                raise e

    print(
        "🔓 Acquired browser session lock successfully.                        "
    )  # Clear the trailing carriage return text
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
