import concurrent.futures
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from urllib.parse import unquote

import pyperclip
import yaml
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from tqdm import tqdm

# --- SETTINGS ---
MAX_CONCURRENT_DOWNLOADS = 3
# ----------------

# Lock to prevent file corruption during parallel status updates
file_modify_lock = threading.Lock()


def load_yaml(file_path):
    if not os.path.exists(file_path):
        return []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        return []


def save_yaml(file_path, data):
    with open(file_path, "w", encoding="utf-8") as f:
        # Use allow_unicode=True to preserve movie titles with special characters
        yaml.dump(
            data, f, default_flow_style=False, sort_keys=False, allow_unicode=True
        )


def update_yaml_field(file_path, magnet_link, updates: dict):
    """
    Updates multiple fields (like status and curl_cmd) for a specific magnet.
    'updates' should be a dictionary like {'status': 'DONE', 'curl_cmd': '...'}
    """
    if not file_path:
        return

    with file_modify_lock:
        data = load_yaml(file_path)
        updated = False
        for entry in data:
            if entry.get("magnet") == magnet_link:
                entry.update(updates)
                updated = True
                break

        if updated:
            save_yaml(file_path, data)


def extract_and_cleanup(zip_path, pbar):
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


def run_curl_download(raw_command, target_dir, slot_index, original_magnet, file_path):
    """
    slot_index determines the vertical position of the progress bar.
    """
    target_dir = os.path.abspath(target_dir)
    os.makedirs(target_dir, exist_ok=True)

    match = re.search(r'-o\s+"([^"]+)"', raw_command)
    if not match:
        update_yaml_field(
            file_path,
            original_magnet,
            {"status": "FAILED: Could not parse curl command"},
        )
        tqdm.write(
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
    # position=slot_index + 1 to leave room for general logs at the top
    # UI SETUP:
    # We use unit="B" and unit_scale=True so tqdm handles K, M, G suffixes automatically
    pbar = tqdm(
        total=100,
        desc=f"🚀 {clean_filename[:20]}",
        unit="B",
        unit_scale=True,
        position=slot_index + 1,
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
            pbar.n = int((percent / 100) * total_bytes)
            pbar.refresh()

    process.wait()
    if process.returncode in [0, 18]:
        extract_and_cleanup(full_path, pbar)
        update_yaml_field(file_path, original_magnet, {"status": "DONE"})
    else:
        update_yaml_field(
            file_path,
            original_magnet,
            {"status": f"FAILED: Curl Exit Code {process.returncode}"},
        )
        tqdm.write(
            f"\n⚠️ Download failed for {clean_filename} (Exit Code: {process.returncode})"
        )

    pbar.close()


def main():
    if len(sys.argv) < 2:
        print("Usage: xvfb-run --auto-servernum uv run main.py --file movies.yaml")
        return

    target_folder = os.path.expanduser("~/Downloads")
    source_file = None
    all_entries = []

    if sys.argv[1] in ["--file", "-f"]:
        source_file = sys.argv[2]
        if len(sys.argv) > 3:
            target_folder = os.path.expanduser(sys.argv[3])
        all_entries = load_yaml(source_file)
    else:
        # Fallback for single magnet string passed via CLI
        all_entries = [{"magnet": sys.argv[1], "title": "Manual Entry"}]
        if len(sys.argv) > 2:
            target_folder = os.path.expanduser(sys.argv[2])

    # filter items that aren't already DONE and have a magnet link
    pending_items = [
        item
        for item in all_entries
        if item.get("status") != "DONE" and item.get("magnet")
    ]

    if not pending_items:
        tqdm.write("✅ All items are already marked DONE.")
        return

    tqdm.write(f"⚙️ Found {len(pending_items)} pending items.")

    # We use a Semaphore to limit active downloads and manage bar slots
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_CONCURRENT_DOWNLOADS
    ) as executor:
        current_slot = 0

        # 1. Start cached commands
        for item in pending_items:
            if item.get("curl_cmd"):
                tqdm.write(f"⚡ Using cached command for: {item.get('title')}")
                executor.submit(
                    run_curl_download,
                    item["curl_cmd"],
                    target_folder,
                    current_slot % MAX_CONCURRENT_DOWNLOADS,
                    item["magnet"],
                    source_file,
                )
                current_slot += 1

        # 2. Scrape missing commands
        items_to_scrape = [i for i in pending_items if not i.get("curl_cmd")]
        if items_to_scrape:
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    "./webtor_session",
                    headless=False,  # xvfb handles this
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = context.pages[0]
                Stealth().apply_stealth_sync(page)

                for item in items_to_scrape:
                    m = item["magnet"]
                    title = item.get("title", "Unknown")

                    try:
                        tqdm.write(f"🌐 Fetching Webtor CMD for: {title}")
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
                            # Save the command to YAML so we don't scrape it next time
                            update_yaml_field(
                                source_file, m, {"curl_cmd": captured_curl}
                            )
                            # Start download
                            executor.submit(
                                run_curl_download,
                                captured_curl,
                                target_folder,
                                current_slot % MAX_CONCURRENT_DOWNLOADS,
                                m,
                                source_file,
                            )
                            current_slot += 1
                        else:
                            update_yaml_field(
                                source_file, m, {"status": "FAILED: Scraping Error"}
                            )
                            tqdm.write(f"❌ Failed to grab command for link {i + 1}")

                    except Exception as e:
                        tqdm.write(f"❌ Error on {title}: {e}")
                        update_yaml_field(
                            source_file, m, {"status": "FAILED: Timeout/UI Error"}
                        )

                context.close()

        tqdm.write("⏳ Scraping finished. Waiting for all downloads...")
        executor.shutdown(wait=True)

    print("🏁 Processing finished.")


if __name__ == "__main__":
    main()
