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


def run_curl_download(raw_command, target_dir, pbar_index):
    """The background worker that handles the actual download."""
    target_dir = os.path.abspath(target_dir)
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

    # Pre-inject Resume and Path
    fixed_command = raw_command.replace(f'"{encoded_filename}"', f'"{full_path}"', 1)
    if "-C -" not in fixed_command:
        fixed_command = fixed_command.replace("curl", "curl -C -", 1)

    # Initialize TQDM bar for this specific download
    # position=magnet_index allows multiple bars to stack correctly
    pbar = tqdm(
        total=100,
        desc=f"🚀 {clean_filename[:25]}",
        unit="%",
        position=pbar_index,
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
            pbar.n = int(progress_match.group(1))
            pbar.refresh()

    process.wait()
    if process.returncode in [0, 18]:
        extract_and_cleanup(full_path, pbar)
    else:
        print(
            f"\n⚠️ Download failed for {clean_filename} (Exit Code: {process.returncode})"
        )

    pbar.close()


def process_magnet(magnet_link, download_path, index):
    """Automates Webtor by intercepting the internal download data, bypassing the clipboard."""
    user_data_dir = f"./session_thread_{index}"

    with sync_playwright() as p:
        stealth = Stealth()
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )

            page = context.pages[0]
            stealth.apply_stealth_sync(page)

            print(f"🌐 [{index}] Processing Link...")
            page.goto(
                "https://webtor.io/", wait_until="domcontentloaded", timeout=60000
            )
            search_input = page.wait_for_selector(
                'input[placeholder*="magnet"]', timeout=30000
            )
            search_input.fill(magnet_link)
            search_input.press("Enter")

            # Wait for the ZIP button to appear
            zip_selector = "button:has-text('ZIP')"
            print("⏳ Waiting for ZIP button (fetching metadata)...")
            page.wait_for_selector(zip_selector, timeout=180000)

            # --- THE CLIPBOARD BYPASS ---
            # Instead of clicking 'Copy curl', we extract the 'share' or 'download' data
            # directly from the button's attributes or the page's internal state.
            # Webtor stores the curl command in a hidden attribute or generates it via JS.

            print(f"🧬 [{index}] Extracting command...")

            # We trigger the 'Copy' logic but intercept the result immediately
            # via a JavaScript injection that returns the string directly to Python.
            captured_curl = page.evaluate("""
                () => {
                    const btn = document.querySelector("button:contains('copy curl cmd')") ||
                                document.querySelector("button i.fa-terminal").closest('button');
                    if (btn) {
                        // We simulate the click but return the 'value' it would have copied
                        // This uses the internal app's state if available
                        return window.app?.$store?.state?.download?.curl || null;
                    }
                    return null;
                }
            """)

            # IF the internal state trick fails, we use the "Listener" trick:
            if not captured_curl:
                # We overwrite the clipboard API in this specific page so that
                # when the button 'writes' to it, it returns the string to us instead.
                page.evaluate(
                    "navigator.clipboard.writeText = (text) => { window.capturedText = text; }"
                )
                page.click("text='copy curl cmd'")
                time.sleep(1)
                captured_curl = page.evaluate("window.capturedText")

            context.close()

            if captured_curl and captured_curl.startswith("curl"):
                run_curl_with_progress(captured_curl, download_path, index)
            else:
                print(f"❌ Thread {index}: Could not capture CURL command.")

        except Exception as e:
            print(f"\n❌ Link {index} Error: {e}")
        finally:
            # Clean up the temporary session folder
            import shutil

            if os.path.exists(user_data_dir):
                shutil.rmtree(user_data_dir, ignore_errors=True)


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

    print(f"⚙️ Found {len(magnets)} unique links. Starting Sequential Scraper...")

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
                            run_curl_download, captured_curl, target_folder, i
                        )
                    else:
                        print(f"❌ Failed to grab command for link {i + 1}")

                except Exception as e:
                    print(f"❌ Error on link {i + 1}: {e}")

            print("✔ All links scraped. Browser closing. Downloads continuing...")
            context.close()

    print("\n🏁 All background tasks finished.")


if __name__ == "__main__":
    main()
