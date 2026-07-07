import os
import shutil
import time
from typing import Any
import time
from playwright.sync_api import BrowserContext, Page, Playwright
from playwright_stealth import Stealth
from result import Err, Ok, Result
from tqdm import tqdm


def clear_stale_locks(profile_dir: str = "./webtor_session"):
    """
    Checks for and removes stale lock files left behind by unexpected crashes
    if no active chrome/chromium process is running.
    """
    if not os.path.exists(profile_dir):
        return

    # Files Chromium uses to mark a profile directory as 'active'
    lock_files = ["SingletonLock", "lock", "LOCK"]

    for filename in lock_files:
        lock_path = os.path.join(profile_dir, filename)
        if os.path.exists(lock_path) or os.path.islink(lock_path):
            try:
                # Attempt to remove the file/symlink.
                # If an active process has an open file handle on it, this will usually fail or block on Windows,
                # but on Linux/macOS we do this carefully.
                if os.path.islink(lock_path):
                    os.unlink(lock_path)
                else:
                    os.remove(lock_path)
                print(f"🗑️ Cleaned up stale crash lock file: {lock_path}")
            except Exception:
                # If we can't delete it, an active script is legitimately using it right now.
                pass


def get_browser_context(
    pl: Playwright, max_wait_seconds: int = 300
) -> tuple[BrowserContext, Page]:
    """
    Creates a configured browser context with stealth and clipboard permissions.
    Safely clears stale crash locks or waits if another instance is actively running.
    """
    profile_path = "./webtor_session"
    start_time = time.time()

    # First, attempt to clear locks that were left abandoned by a previous crash
    clear_stale_locks(profile_path)

    while True:
        try:
            context = pl.chromium.launch_persistent_context(
                profile_path,
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

                # Every few iterations, try clearing the locks again in case an instance just closed
                if elapsed % 15 == 0:
                    clear_stale_locks(profile_path)

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
