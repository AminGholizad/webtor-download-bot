import os
import re
import subprocess
import sys
import time
import zipfile
from urllib.parse import unquote

import pyperclip
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth


def extract_and_cleanup(zip_path):
    """Unzips the file and deletes the original ZIP."""
    if not os.path.exists(zip_path):
        print(f"❌ Extraction failed: {zip_path} not found.")
        return

    # Create a folder name based on the zip name (without .zip)
    extract_to = zip_path.rsplit(".", 1)[0]

    print(f"📦 Extracting to: {extract_to}...")
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(extract_to)
        print("✅ Extraction complete.")

        # Delete the zip file
        os.remove(zip_path)
        print(f"🗑️ Deleted original ZIP: {zip_path}")
    except zipfile.BadZipFile:
        print("❌ Error: The downloaded file is not a valid ZIP or is corrupted.")
    except Exception as e:
        print(f"❌ Extraction error: {e}")


def decode_and_run_curl(raw_command, target_dir):
    """Decodes filename, runs curl, and triggers extraction."""
    target_dir = os.path.abspath(target_dir)
    if not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    pattern = r'-o\s+"([^"]+)"'
    match = re.search(pattern, raw_command)

    if match:
        encoded_filename = match.group(1)
        clean_filename = unquote(encoded_filename)
        full_path = os.path.join(target_dir, clean_filename)

        fixed_command = raw_command.replace(
            f'"{encoded_filename}"', f'"{full_path}"', 1
        )

        print(f"✨ Decoded Filename: {clean_filename}")
        print("🚀 Starting download...")

        # Run curl and wait for it to finish
        process = subprocess.run(fixed_command, shell=True)

        # If curl exited successfully (return code 0)
        if process.returncode == 0:
            print("\n✅ Download finished successfully.")
            extract_and_cleanup(full_path)
        else:
            print(
                f"\n❌ Curl failed with exit code {process.returncode}. Skipping extraction."
            )
    else:
        print("⚠️ Could not parse -o flag. Extraction skipped.")
        subprocess.run(raw_command, shell=True)


def run_webtor_automation(magnet_link, download_path):
    with sync_playwright() as p:
        user_data_dir = "./webtor_session"
        stealth = Stealth()

        context = p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            permissions=["clipboard-read", "clipboard-write"],
        )

        page = context.pages[0]
        stealth.apply_stealth_sync(page)

        print(f"🌐 Opening Webtor.io...")
        page.goto("https://webtor.io/", wait_until="domcontentloaded")

        try:
            search_input = page.wait_for_selector(
                'input[placeholder*="magnet"]', timeout=30000
            )
            search_input.fill(magnet_link)
            search_input.press("Enter")

            zip_selector = "button:has-text('ZIP')"
            print("⏳ Waiting for ZIP button (fetching metadata)...")
            page.wait_for_selector(zip_selector, timeout=180000)
            page.click(zip_selector)

            curl_btn = "text='copy curl cmd'"
            page.wait_for_selector(curl_btn, timeout=30000)
            page.click(curl_btn)

            time.sleep(2)
            captured_curl = pyperclip.paste().strip()

            if captured_curl.startswith("curl"):
                context.close()
                decode_and_run_curl(captured_curl, download_path)
            else:
                print("❌ Failed to capture curl command.")
                context.close()

        except Exception as e:
            print(f"❌ Automation Error: {e}")
            context.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run script.py '<MAGNET_LINK>' '[DOWNLOAD_FOLDER]'")
    else:
        magnet = sys.argv[1]
        folder = sys.argv[2] if len(sys.argv) > 2 else "/mnt/Storage/jellyfin/Movies/"
        run_webtor_automation(magnet, folder)
