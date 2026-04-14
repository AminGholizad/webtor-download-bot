import concurrent.futures
import os
import re
import subprocess
import sys
import time
import zipfile
import threading
from urllib.parse import unquote

import pyperclip
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from tqdm import tqdm

# --- SETTINGS ---
MAX_CONCURRENT_DOWNLOADS = 3
FAILED_LINKS_FILE = "failed_links.txt"
# ----------------

# Thread lock to prevent multiple threads from writing to the file at once
file_lock = threading.Lock()


def log_failure(magnet_link):
    """Appends a failed magnet link to the failure file."""
    with file_lock:
        with open(FAILED_LINKS_FILE, "a") as f:
            f.write(f"{magnet_link}\n")


def extract_and_cleanup(zip_path, pbar):
    """
    Unzips the file member-by-member to ignore CRC errors
    and deletes the original ZIP.
    """
    if not os.path.exists(zip_path):
        print(f"❌ Extraction failed: {zip_path} not found.")
        return

    # Create a folder name based on the zip name (without .zip)
    extract_to = zip_path.rsplit(".", 1)[0]
    pbar.set_description(f"📦 Extracting: {os.path.basename(extract_to)[:20]}...")
    print(f"📦 Extracting to: {extract_to}...")

    if not os.path.exists(extract_to):
        os.makedirs(extract_to)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                try:
                    zf.extract(member, extract_to)
                except (zipfile.BadZipFile, RuntimeError) as e:
                    # This catches CRC errors or decryption errors per-file
                    print(
                        f"\n⚠️ Skipping corrupt file inside ZIP ({member.filename}): {e}"
                    )
                    continue

        # We delete the ZIP even if some internal files were corrupt,
        # as requested ("ignore CRC check error after extraction completed").
        os.remove(zip_path)
        print(f"🗑️ Deleted original ZIP: {zip_path}")
        pbar.set_description(f"✅ Finished: {os.path.basename(extract_to)[:20]}")

    except Exception as e:
        print(f"\n❌ Critical ZIP Error: {e}")


def run_curl_download(raw_command, target_dir, pbar_index, original_magnet):
    """The background worker with a cleaner 'Size/Total' UI."""
    target_dir = os.path.abspath(target_dir)
    os.makedirs(target_dir, exist_ok=True)

    match = re.search(r'-o\s+"([^"]+)"', raw_command)
    if not match:
        log_failure(original_magnet)
        print(
            f"\n❌ Curl failed with exit code {process.returncode}. Skipping extraction."
        )
        subprocess.run(raw_command, shell=True)
        return

    encoded_filename = match.group(1)
    clean_filename = unquote(encoded_filename)
    full_path = os.path.join(target_dir, clean_filename)

    fixed_command = raw_command.replace(f'"{encoded_filename}"', f'"{full_path}"', 1)
    if "-C -" not in fixed_command:
        fixed_command = fixed_command.replace("curl", "curl -C -", 1)

    # Initialize TQDM bar for this specific download
    # position=magnet_index allows multiple bars to stack correctly
    # UI SETUP:
    # We use unit="B" and unit_scale=True so tqdm handles K, M, G suffixes automatically
    pbar = tqdm(
        total=100,
        desc=f"🚀 {clean_filename[:20]}",
        unit="B",
        unit_scale=True,
        position=pbar_index,
        leave=False,
        bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
    )

    process = subprocess.Popen(
        fixed_command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    total_bytes = 0
    for line in iter(process.stdout.readline, ""):
        if total_bytes == 0:
            size_match = re.search(r"(\d+(?:\.\d+)?[kMG])", line)
            if size_match:
                raw_size = size_match.group(1)
                multipliers = {"k": 1024, "M": 1024**2, "G": 1024**3}
                val = float(re.sub(r"[kMG]", "", raw_size))
                unit = raw_size[-1]
                total_bytes = int(val * multipliers.get(unit, 1))
                pbar.total = total_bytes

        progress_match = re.search(
            r"(\d+)\s+([\d.]+[kMG])\s+(\d+)\s+([\d.]+[kMG])", line
        )
        if progress_match and total_bytes > 0:
            percent = int(progress_match.group(1))
            # Calculate current bytes based on percentage
            current_bytes = int((percent / 100) * total_bytes)
            pbar.n = current_bytes
            pbar.refresh()

    process.wait()
    if process.returncode in [0, 18]:
        extract_and_cleanup(full_path, pbar)
    else:
        log_failure(original_magnet)
        print(
            f"\n⚠️ Download failed for {clean_filename} (Exit Code: {process.returncode})"
        )
    pbar.close()


def main():
    if len(sys.argv) < 2:
        print("Usage: xvfb-run --auto-servernum uv run main.py --file links.txt")
        return

    target_folder = os.path.expanduser("~/Downloads")
    magnets = []

    if sys.argv[1] in ["--file", "-f"]:
        file_path = sys.argv[2]
        if len(sys.argv) > 3:
            target_folder = sys.argv[3]
        if not os.path.exists(file_path):
            print(f"File not found: {file_path}")
            return
        with open(file_path, "r") as f:
            magnets = list(
                set(line.strip() for line in f if line.strip().startswith("magnet:"))
            )
    else:
        magnets = [sys.argv[1]]
        if len(sys.argv) > 2:
            target_folder = os.path.expanduser(sys.argv[2])

    print(f"⚙️ Found {len(magnets)} unique links. Opening scraper...")

    # Shared pool for background downloads
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_CONCURRENT_DOWNLOADS
    ) as executor:
        with sync_playwright() as p:
            # Single browser context for everything
            context = p.chromium.launch_persistent_context(
                "./webtor_session",
                headless=False,  # xvfb handles this
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0]
            Stealth().apply_stealth_sync(page)

            for i, m in enumerate(magnets):
                try:
                    print(
                        f"🌐 [{i + 1}/{len(magnets)}] Fetching command from Webtor..."
                    )
                    page.goto("https://webtor.io/", wait_until="domcontentloaded")

                    search_input = page.wait_for_selector(
                        'input[placeholder*="magnet"]'
                    )
                    search_input.fill(m)
                    search_input.press("Enter")

                    zip_btn = page.wait_for_selector(
                        "button:has-text('ZIP')", timeout=180000
                    )
                    zip_btn.click()

                    copy_btn = page.wait_for_selector(
                        "text='copy curl cmd'", timeout=30000
                    )
                    copy_btn.click()

                    time.sleep(2)  # Safe clipboard buffer
                    captured_curl = pyperclip.paste().strip()

                    if captured_curl.startswith("curl"):
                        # Submit to background thread and move to NEXT magnet immediately
                        executor.submit(
                            run_curl_download, captured_curl, target_folder, i, m
                        )
                    else:
                        print(f"❌ Failed to grab command for link {i + 1}")
                        log_failure(m)

                except Exception as e:
                    print(f"❌ Error on link {i + 1}: {e}")
                    log_failure(m)

            print("✔ All links scraped. Browser closing. Downloads continuing...")
            context.close()

    print(f"\n🏁 Finished. Check '{FAILED_LINKS_FILE}' if any failed.")


if __name__ == "__main__":
    main()
