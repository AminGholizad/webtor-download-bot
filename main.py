import os
import re
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import unquote

import typer
import yaml
from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright
from playwright_stealth import Stealth
from result import Err, Ok, Result
from tqdm import tqdm
from typing_extensions import Annotated

# --- SETTINGS ---
MAX_CONCURRENT_DOWNLOADS = 3
# ----------------

# Lock to prevent file corruption during parallel status updates
file_modify_lock = threading.Lock()

app = typer.Typer()


class SlotManager:
    """Manages vertical terminal lines to prevent progress bars from overlapping."""

    def __init__(self, max_slots):
        self.max_slots = max_slots
        self.slots = [False] * max_slots
        self.lock = threading.Lock()
        self._local = threading.local()

    def aquire(self):
        """Acquires a slot. Returns the slot index."""
        with self.lock:
            for i, occupied in enumerate(self.slots):
                if not occupied:
                    self.slots[i] = True
                    self._local.slot = i
                    return i
        # Fallback to 0 if all slots are somehow full
        self._local.slot = 0
        return 0

    def release(self):
        """Releases the slot."""
        slot = getattr(self._local, "slot", None)
        if slot is not None:
            with self.lock:
                if 0 <= slot < self.max_slots:
                    self.slots[slot] = False
            del self._local.slot

    def __enter__(self):
        return self.aquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


slot_manager = SlotManager(MAX_CONCURRENT_DOWNLOADS)


def load_yaml(yaml_path: str) -> list[Any]:
    if not os.path.exists(yaml_path):
        return []
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_yaml(yaml_path: str, data: Any) -> None:
    with open(yaml_path, "w", encoding="utf-8") as f:
        # Use allow_unicode=True to preserve titles with special characters
        yaml.dump(
            data, f, default_flow_style=False, sort_keys=False, allow_unicode=True
        )


def update_yaml_field(
    yaml_path: str, magnet_link: str, updates: dict[str, str]
) -> None:
    """
    Updates multiple fields (like status and curl_cmd) for a specific magnet.
    'updates' should be a dictionary like {'status': 'DONE', 'curl_cmd': '...'}
    """
    if not yaml_path:
        return

    with file_modify_lock:
        data = load_yaml(yaml_path)
        updated = False
        for entry in data:
            if entry.get("magnet") == magnet_link:
                entry.update(updates)
                updated = True
                break

        if updated:
            save_yaml(yaml_path, data)


def extract_and_cleanup(zip_path: str, pbar: tqdm) -> None:
    """
    Unzips the file member-by-member to ignore CRC errors
    and deletes the original ZIP.
    """
    if not os.path.exists(zip_path):
        tqdm.write(f"❌ Extraction failed: {zip_path} not found.")
        return

    # Create a folder name based on the zip name (without .zip)
    extract_to = zip_path.rsplit(".", 1)[0]
    pbar.set_description(f"📦 Unzipping: {os.path.basename(extract_to)[:15]}")

    if not os.path.exists(extract_to):
        os.makedirs(extract_to)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                try:
                    zf.extract(member, extract_to)
                except (zipfile.BadZipFile, RuntimeError) as e:
                    # This catches CRC errors or decryption errors per-file
                    tqdm.write(
                        f"\n⚠️ Skipping corrupt file inside ZIP ({member.filename}): {e}"
                    )
                    continue

        # We delete the ZIP even if some internal files were corrupt,
        # as requested ("ignore CRC check error after extraction completed").
        os.remove(zip_path)
        tqdm.write(f"🗑️ Deleted original ZIP: {zip_path}")
        pbar.set_description(f"✅ Finished: {os.path.basename(extract_to)[:20]}")
    except Exception as e:
        tqdm.write(f"❌ Critical ZIP extraction error: {e}")


def get_filename_from_url(url: str) -> str:
    """Extracts a clean filename from the Webtor download URL."""
    try:
        # Handles URL formats like https://.../filename.zip?token=...
        path_part = url.split("?")[0]
        filename = os.path.basename(path_part)
        return unquote(filename)
    except Exception:
        return "download.zip"


def run_aria2_download(
    download_url: str,
    target_dir: str,
    original_magnet: str,
    yaml_path: Optional[str] = None,
):
    with slot_manager as slot:
        target_dir = os.path.abspath(target_dir)
        os.makedirs(target_dir, exist_ok=True)

        download_filename = get_filename_from_url(download_url)
        full_path = os.path.join(target_dir, download_filename)
        # Initialize TQDM bar for this specific download
        # position=slot + 1 to leave room for general logs at the top
        # UI SETUP:
        # We use unit="B" and unit_scale=True so tqdm handles K, M, G suffixes automatically
        pbar = tqdm(
            total=100,
            desc=f"🚀 {download_filename[:20]}",
            unit="B",
            unit_scale=True,
            position=slot + 1,
            leave=False,
            dynamic_ncols=True,
            bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{rate_fmt}]",
        )

        # Build aria2c command with standard stream reporting
        # --summary-interval=1 forces output updates every second
        aria_command = [
            "aria2c",
            "--continue=True",
            f"--dir={target_dir}",
            f"--out={download_filename}",
            "--summary-interval=1",
            download_url,
        ]

        process = subprocess.Popen(
            aria_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        if process.stdout is None:
            if yaml_path:
                update_yaml_field(
                    yaml_path,
                    original_magnet,
                    {"status": "FAILED: Could not open stdout pipe"},
                )
            tqdm.write(
                f"❌ Failed to start download for {download_filename}: Could not open stdout pipe."
            )
            pbar.close()
            return

        total_bytes = 0
        for line in iter(process.stdout.readline, ""):
            # Parse total size from aria2 output line: e.g., "Size: 28.5MiB" or "[(#123456 28.5MiB/100MiB(28%)]"
            if total_bytes == 0:
                size_match = re.search(r"Size:\s*(\d+(?:\.\d+)?)\s*([kKMG])i?B", line)
                if size_match:
                    val = float(size_match.group(1))
                    unit = size_match.group(2).upper()
                    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3}
                    total_bytes = int(val * multipliers.get(unit, 1))
                    pbar.total = total_bytes

            # Match aria2 standard progress lines: e.g., " (28%)" or "30% "
            progress_match = re.search(r"\((\d+)%\)", line) or re.search(
                r"(\d+)%\s", line
            )
            if progress_match and total_bytes > 0:
                percent = int(progress_match.group(1))
                pbar.n = int((percent / 100) * total_bytes)
                pbar.refresh()

        process.wait()
        if process.returncode == 0:
            tqdm.write(f"✅ Downloaded: {download_filename}")
            extract_and_cleanup(full_path, pbar)
            if yaml_path:
                update_yaml_field(yaml_path, original_magnet, {"status": "DONE"})
        else:
            if yaml_path:
                update_yaml_field(
                    yaml_path,
                    original_magnet,
                    {"status": f"FAILED: aria2 Exit Code {process.returncode}"},
                )
            tqdm.write(
                f"\n⚠️ Download failed for {download_filename} (Exit Code: {process.returncode})"
            )

        pbar.close()


def get_pending_items(all_entries) -> list[str]:
    """filter items that aren't already DONE and have a magnet link"""
    pending_items = [
        item
        for item in all_entries
        if item.get("status") != "DONE" and item.get("magnet")
    ]
    return pending_items


def download_cached(
    pending_items: list[Any],
    executor: ThreadPoolExecutor,
    target_folder: str,
    yaml_path: str,
) -> None:
    for item in pending_items:
        if item.get("curl_cmd"):
            tqdm.write(f"⚡ Cached: {item.get('title')[:20]}...")
            executor.submit(
                run_aria2_download,
                item["curl_cmd"],
                target_folder,
                item["magnet"],
                yaml_path,
            )


def get_browser_context(pl: Playwright) -> tuple[BrowserContext, Page]:
    """Creates a configured browser context with stealth and clipboard permissions."""
    context = pl.chromium.launch_persistent_context(
        "./webtor_session",
        headless=False,  # xvfb handles this
        args=["--disable-blink-features=AutomationControlled"],
    )
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    page = context.pages[0]
    Stealth().apply_stealth_sync(page)
    return context, page


def scrape_webtor_url(page, magnet: str, title: str) -> Result[str, str]:
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

        url_btn = page.wait_for_selector("a:has-text('url')", timeout=100000)
        if not url_btn:
            msg = "FAILED: URL copy button not found"
            tqdm.write(f"❌ Scrape error on {title}: {msg}")
            return Err(msg)
        url_btn.click()

        time.sleep(2)  # Safe clipboard buffer
        captured_url = page.evaluate("navigator.clipboard.readText()").strip()

        if captured_url.startswith("http"):
            return Ok(captured_url)
        else:
            msg = "FAILED: Scraping Error (No valid URL captured)"
            tqdm.write(f"❌ Failed to grab URL link for {title}")
            return Err(msg)
    except Exception as e:
        msg = f"FAILED: Browser error ({type(e).__name__})"
        tqdm.write(f"❌ Scrape error on {title}: {e}")
        return Err(msg)


def scrape_n_download(
    pending_items: list[Any],
    executor: ThreadPoolExecutor,
    target_folder: str,
    yaml_path: Optional[str] = None,
) -> None:
    items_to_scrape = [i for i in pending_items if not i.get("curl_cmd")]
    if items_to_scrape:
        with sync_playwright() as pl:
            browser, page = get_browser_context(pl)

            for item in items_to_scrape:
                m = item["magnet"]
                title = item.get("title", "Unknown")

                match scrape_webtor_url(page, m, title):
                    case Ok(captured_url):
                        # Save it under curl_cmd key to minimize rewriting the YAML architecture
                        if yaml_path:
                            update_yaml_field(yaml_path, m, {"curl_cmd": captured_url})
                        # Start aria2 download
                        executor.submit(
                            run_aria2_download,
                            captured_url,
                            target_folder,
                            m,
                            yaml_path,
                        )
                    case Err(e):
                        if yaml_path:
                            update_yaml_field(yaml_path, m, {"status": e})

            browser.close()

    tqdm.write("⏳ Scraping finished. Waiting for downloads to complete...")


@app.command()
def file(
    yaml_path: str,
    target_folder: Annotated[str, typer.Option("--target", "-t")] = "~/Downloads",
):
    """use links inside a yaml file"""
    target_folder = os.path.expanduser(target_folder)
    all_entries = load_yaml(yaml_path)
    pending_items = get_pending_items(all_entries)
    if not pending_items:
        print("✅ No pending items.")
        return

    tqdm.write(f"⚙️ Found {len(pending_items)} pending items.")

    # We use a Semaphore to limit active downloads and manage bar slots
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS) as executor:
        download_cached(pending_items, executor, target_folder, yaml_path)
        scrape_n_download(pending_items, executor, target_folder, yaml_path)
    tqdm.write("🏁 Processing finished.")


@app.command()
def link(
    magnet_link: str,
    target_folder: Annotated[str, typer.Option("--target", "-t")] = "~/Downloads",
):
    target_folder = os.path.expanduser(target_folder)
    match = re.search("dn=(.+)&", magnet_link)
    name = unquote(match.group(1)).replace("+", " ") if match else "Manual Entry"
    download_link = ""
    with sync_playwright() as p:
        browser, page = get_browser_context(p)
        match scrape_webtor_url(page, magnet_link, name):
            case Ok(captured_link):
                download_link = captured_link
        browser.close()
    if download_link:
        run_aria2_download(download_link, target_folder, magnet_link)
    tqdm.write("🏁 Processing finished.")


if __name__ == "__main__":
    app()
