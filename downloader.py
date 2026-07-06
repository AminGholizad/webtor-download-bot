import os
import re
import subprocess
import threading
from typing import Any, Optional
from urllib.parse import unquote

from playwright.sync_api import sync_playwright
from result import Err, Ok
from tqdm import tqdm

import config
from scraper import get_browser_context, scrape_webtor_url
from utils import extract_and_cleanup, get_filename_from_url, update_yaml_field


def handle_expired_link(original_magnet: str, yaml_path: Optional[str] = None):
    """Regrabs a fresh URL for a magnet that expired or timed out and queues it."""
    tqdm.write(f"🔄 Link expired or timed out. Refreshing token for magnet...")

    with sync_playwright() as pl:
        browser, page = get_browser_context(pl)
        # Fallback tracking info
        match = re.search("dn=(.+)&", original_magnet)
        title = unquote(match.group(1)).replace("+", " ") if match else "Expired Item"

        match scrape_webtor_url(page, original_magnet, title):
            case Ok(fresh_url):
                tqdm.write(f"✨ Fresh URL retrieved successfully for: {title[:20]}")
                if yaml_path:
                    update_yaml_field(
                        yaml_path,
                        original_magnet,
                        {"download_url": fresh_url, "status": "RETRIED"},
                    )

                # Re-submit the freshly pulled URL to the ongoing thread executor pool
                if config.global_executor:
                    config.global_executor.submit(
                        run_aria2_download,
                        fresh_url,
                        config.global_target_folder,
                        original_magnet,
                        yaml_path,
                    )
            case Err(e):
                tqdm.write(f"❌ Could not auto-refresh link: {e}")
                if yaml_path:
                    update_yaml_field(
                        yaml_path,
                        original_magnet,
                        {"status": f"FAILED: Refresh failed ({e})"},
                    )
        browser.close()


def run_aria2_download(
    download_url: str,
    target_dir: str,
    original_magnet: str,
    yaml_path: Optional[str] = None,
):
    with config.slot_manager as slot:
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
            "--continue=true",
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
            # 1. Parse Total Size and Current Downloaded Bytes dynamically from lines like:
            # [#a3a10a 460MiB/1.8GiB(24%) CN:1 DL:0B]
            size_match = re.search(r"([\d.]+)([kKMG])i?B/([\d.]+)([kKMG])i?B", line)

            if size_match:
                multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3}

                # Parse current bytes
                curr_val = float(size_match.group(1))
                curr_unit = size_match.group(2).upper()
                current_bytes = int(curr_val * multipliers.get(curr_unit, 1))

                # Parse total bytes (only need to calculate this once)
                if total_bytes == 0:
                    total_val = float(size_match.group(3))
                    total_unit = size_match.group(4).upper()
                    total_bytes = int(total_val * multipliers.get(total_unit, 1))
                    pbar.total = total_bytes

                # Update progress bar positions accurately based on exact downloaded bytes
                pbar.n = current_bytes
                pbar.refresh()

            # 2. Fallback parser: If the bytes pattern wasn't matched but a percentage is shown
            elif total_bytes > 0:
                progress_match = re.search(r"\((\d+)%\)", line)
                if progress_match:
                    percent = int(progress_match.group(1))
                    pbar.n = int((percent / 100) * total_bytes)
                    pbar.refresh()

        process.wait()
        pbar.close()

        if process.returncode == 0:
            tqdm.write(f"✅ Downloaded: {download_filename}")
            extract_and_cleanup(full_path, pbar)
            if yaml_path:
                update_yaml_field(yaml_path, original_magnet, {"status": "DONE"})

        # Status code 19 = HTTP Status 4xx/5xx error (e.g. 401 Unauthorized / 403 Forbidden on expired tokens)
        # Status code 24 = Authorization failed / Link timed out completely
        elif process.returncode in [19, 24]:
            tqdm.write(
                f"⚠️ Link validation failed (Code {process.returncode}) for {download_filename}."
            )
            # Spawn a non-blocking background thread to grab a fresh link and append back into queue
            threading.Thread(
                target=handle_expired_link,
                args=(original_magnet, yaml_path),
                daemon=True,
            ).start()
        else:
            if yaml_path:
                update_yaml_field(
                    yaml_path,
                    original_magnet,
                    {"status": f"FAILED: aria2 Exit Code {process.returncode}"},
                )
            tqdm.write(
                f"\n❌ Download dropped permanently for {download_filename} (Code: {process.returncode})"
            )
