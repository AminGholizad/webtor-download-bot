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

    def acquire(self):
        """Acquires a slot. If no slots are available, returns 0 (fallback).
        Note: The ThreadPoolExecutor should limit calls to max_slots.
        """
        with self.lock:
            for i, occupied in enumerate(self.slots):
                if not occupied:
                    self.slots[i] = True
                    return i
        return 0

    def release(self, i):
        with self.lock:
            if 0 <= i < self.max_slots:
                self.slots[i] = False


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


def fix_curl_cmd(
    command: str, target_dir: str, original_magnet: str, yaml_path: Optional[str] = None
) -> tuple[str, str]:
    # Remove silence flags to ensure progress output is captured
    command = re.sub(r"\s-sS?\s", " ", command)
    command = re.sub(r"^curl\s-sS?\s", "curl ", command)

    match = re.search(r'-o\s+"([^"]+)"', command)
    if not match:
        if yaml_path:
            update_yaml_field(
                yaml_path,
                original_magnet,
                {"status": "FAILED: Could not parse curl command"},
            )
        tqdm.write("\n❌ Could not parse curl command.")
        return command, os.path.curdir

    encoded_filename = match.group(1)
    clean_filename = unquote(encoded_filename)
    full_path = os.path.abspath(os.path.join(target_dir, clean_filename))

    # Use the full match for safer replacement
    fixed_command = command.replace(match.group(0), f'-o "{full_path}"', 1)

    if "-C -" not in fixed_command:
        fixed_command = fixed_command.replace("curl", "curl -C -", 1)
    return fixed_command, full_path


def run_curl_download(
    raw_command: str,
    target_dir: str,
    original_magnet: str,
    yaml_path: Optional[str] = None,
):
    # 1. Claim a visual slot
    slot = slot_manager.acquire()

    target_dir = os.path.abspath(target_dir)
    os.makedirs(target_dir, exist_ok=True)

    fixed_command, full_path = fix_curl_cmd(
        raw_command, target_dir, original_magnet, yaml_path
    )
    download_filename = os.path.basename(full_path)
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

    process = subprocess.Popen(
        fixed_command,
        shell=True,
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
        slot_manager.release(slot)
        return

    total_bytes = 0
    for line in iter(process.stdout.readline, ""):
        if total_bytes == 0:
            # Look for total size (e.g., 28.5M or 100k)
            size_match = re.search(r"(\d+(?:\.\d+)?)\s*([kKMG])", line)
            if size_match:
                val = float(size_match.group(1))
                unit = size_match.group(2).upper()
                multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3}
                total_bytes = int(val * multipliers.get(unit, 1))
                pbar.total = total_bytes

        # Match curl progress line: % Total % Received % Xferd ...
        # e.g., 10 28.5M 10 28.5M
        progress_match = re.search(
            r"(\d+)\s+([\d.]+[kKMG])\s+(\d+)\s+([\d.]+[kKMG])", line
        )
        if progress_match and total_bytes > 0:
            percent = int(progress_match.group(1))
            pbar.n = int((percent / 100) * total_bytes)
            pbar.refresh()

    process.wait()
    if process.returncode in [0, 18]:
        tqdm.write(f"✅ Downloaded: {download_filename}")
        extract_and_cleanup(full_path, pbar)
        if yaml_path:
            update_yaml_field(yaml_path, original_magnet, {"status": "DONE"})
    else:
        if yaml_path:
            update_yaml_field(
                yaml_path,
                original_magnet,
                {"status": f"FAILED: Curl Exit Code {process.returncode}"},
            )
        tqdm.write(
            f"\n⚠️ Download failed for {download_filename} (Exit Code: {process.returncode})"
        )

    pbar.close()
    # 2. Free the slot for the next download in queue
    slot_manager.release(slot)


def get_pending_items(all_entries):
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
                run_curl_download,
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


def scrape_webtor_curl(page, magnet: str, title: str) -> Result[str, str]:
    """
    Scrapes the webtor.io site for a curl command for a given magnet link.
    Returns a Result containing either the curl command or an error message.
    """
    try:
        tqdm.write(f"🌐 Fetching Webtor CMD for: {title[:20]}...")
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

        copy_btn = page.wait_for_selector("a:has-text('curl')", timeout=100000)
        if not copy_btn:
            msg = "FAILED: Curl copy button not found"
            tqdm.write(f"❌ Scrape error on {title}: {msg}")
            return Err(msg)
        copy_btn.click()

        time.sleep(2)  # Safe clipboard buffer
        captured_curl = page.evaluate("navigator.clipboard.readText()").strip()

        if captured_curl.startswith("curl"):
            return Ok(captured_curl)
        else:
            msg = "FAILED: Scraping Error"
            tqdm.write(f"❌ Failed to grab command for link {title}")
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

                match scrape_webtor_curl(page, m, title):
                    case Ok(captured_curl):
                        # Save the command to YAML so we don't scrape it next time
                        if yaml_path:
                            update_yaml_field(yaml_path, m, {"curl_cmd": captured_curl})
                        # Start download
                        executor.submit(
                            run_curl_download,
                            captured_curl,
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

    with sync_playwright() as p:
        browser, page = get_browser_context(p)
        match scrape_webtor_curl(page, magnet_link, "Manual Entry"):
            case Ok(captured_link):
                run_curl_download(captured_link, target_folder, magnet_link)
        browser.close()

    tqdm.write("🏁 Processing finished.")


if __name__ == "__main__":
    app()
