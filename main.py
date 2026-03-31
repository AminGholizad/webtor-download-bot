import concurrent.futures
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
from tqdm import tqdm

# --- SETTINGS ---
MAX_CONCURRENT_DOWNLOADS = 3
# ----------------


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
                    # Extract individual file
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


def run_curl_with_progress(raw_command, target_dir, magnet_index):
    """Executes curl and maps its output to a tqdm progress bar."""
    
    target_dir = os.path.abspath(target_dir)
    if not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    pattern = r'-o\s+"([^"]+)"'
    match = re.search(pattern, raw_command)

    if not match:
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
    pbar = tqdm(
        total=100,
        desc=f"🚀 {clean_filename[:25]}",
        unit="%",
        position=magnet_index,
        leave=False,
    )

    process = subprocess.Popen(
        fixed_command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    for line in iter(process.stdout.readline, ""):
        # Matches the percentage in curl's default progress meter
        progress_match = re.search(r"(\d+)\s+[\d.]+[kMG]", line)
        if progress_match:
            percent = int(progress_match.group(1))
            pbar.n = percent
            pbar.refresh()

    process.wait()

    if (
        process.returncode == 0 or process.returncode == 18
    ):  # 18 is 'partial file' but often workable
        extract_and_cleanup(full_path, pbar)
    else:
        print(
            f"\n⚠️ Download failed for {clean_filename} (Exit Code: {process.returncode})"
        )

    pbar.close()


def process_magnet(magnet_link, download_path, index):
    """Automates Webtor to get the curl command."""
    # Unique session folder to prevent Playwright conflicts
    user_data_dir = f"./session_thread_{index}"

    with sync_playwright() as p:
        stealth = Stealth()
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir,
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.pages[0]
            stealth.apply_stealth_sync(page)
            print(f"🌐 Opening Webtor.io...")
            page.goto(
                "https://webtor.io/", wait_until="domcontentloaded", timeout=60000
            )
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
            context.close()

            if captured_curl.startswith("curl"):
                run_curl_with_progress(captured_curl, download_path, index)

        except Exception as e:
            print(f"\n❌ Link {index} Error: {e}")
        finally:
            # Clean up the temporary session folder
            import shutil

            if os.path.exists(user_data_dir):
                shutil.rmtree(user_data_dir, ignore_errors=True)


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run script.py --file links.txt './downloads'")
        return

    target_folder = "./downloads"
    magnets = []

    if sys.argv[1] in ["--file", "-f"]:
        file_path = sys.argv[2]
        target_folder = sys.argv[3] if len(sys.argv) > 3 else target_folder
        if not os.path.exists(file_path):
            print(f"File not found: {file_path}")
            return
        with open(file_path, "r") as f:
            magnets = [line.strip() for line in f if line.strip().startswith("magnet:")]
    else:
        magnets = [sys.argv[1]]
        target_folder = sys.argv[2] if len(sys.argv) > 2 else target_folder

    print(
        f"⚙️ Starting {len(magnets)} downloads (Parallel Max: {MAX_CONCURRENT_DOWNLOADS})"
    )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_CONCURRENT_DOWNLOADS
    ) as executor:
        # We pass 'index' to position=index in tqdm so bars stack correctly
        futures = [
            executor.submit(process_magnet, m, target_folder, i)
            for i, m in enumerate(magnets)
        ]
        concurrent.futures.wait(futures)

    print("\n🏁 Process finished.")


if __name__ == "__main__":
    main()
