import json
import os
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import unquote
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright
from result import Err, Ok
from tqdm import tqdm

import config
from scraper import get_browser_context, scrape_webtor_url
from utils import extract_and_cleanup, get_filename_from_url, update_yaml_field


def call_aria2_rpc(method: str, params: list) -> Any:
    """Helper function to communicate with the aria2 JSON-RPC server."""
    payload = {
        "jsonrpc": "2.0",
        "id": "q-py",
        "method": method,
        "params": [f"token:{config.ARIA2_RPC_SECRET}"] + params,
    }

    req = Request(
        config.ARIA2_RPC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urlopen(req, timeout=5) as response:
            res = json.loads(response.read().decode("utf-8"))
            if "error" in res:
                raise Exception(res["error"].get("message"))
            return res.get("result")
    except Exception as e:
        raise Exception(f"RPC Connection Error: {e}")


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

        # RPC Options setup (Tells aria2 where to store the specific files)
        options = {"dir": target_dir, "out": download_filename, "continue": "true"}

        max_retries = 10
        retry_count = 0
        error_triggered = False

        while True:
            try:
                # Tell aria2 to add the direct HTTP URI download download assignment
                gid = call_aria2_rpc("aria2.addUri", [[download_url], options])
            except Exception as e:
                if retry_count < max_retries:
                    retry_count += 1
                    tqdm.write(f"⚠️ RPC failed to add download for {download_filename}: {e}. Retrying ({retry_count}/{max_retries})...")
                    time.sleep(2)
                    continue
                else:
                    if yaml_path:
                        update_yaml_field(
                            yaml_path, original_magnet, {"status": f"FAILED: RPC Startup Error"}
                        )
                    tqdm.write(f"❌ RPC failed to add download: {e}")
                    pbar.close()
                    return

            # Status polling loop
            attempt_failed = False
            while True:
                time.sleep(1)  # Poll status updates every 1 second
                try:
                    status_info = call_aria2_rpc("aria2.tellStatus", [gid])
                except Exception as e:
                    tqdm.write(f"⚠️ Failed to poll RPC status for {download_filename}: {e}")
                    continue

                status = status_info.get("status")
                total_length = int(status_info.get("totalLength", 0))
                completed_length = int(status_info.get("completedLength", 0))

                if total_length > 0:
                    pbar.total = total_length
                    pbar.n = completed_length
                    pbar.refresh()

                if status == "complete":
                    break

                elif status == "error":
                    error_code = int(status_info.get("errorCode", 0))
                    # Error code 1 = An unknown error occurred / stream dropped
                    # Error code 22 = HTTP response status code was unacceptable (401/403/Expired token)
                    if error_code in [1, 22]:
                        tqdm.write(
                            f"⚠️ Link validation failed via RPC (Code {error_code}) for {download_filename}."
                        )
                        # Clean up the broken task from the daemon's stack
                        try:
                            call_aria2_rpc("aria2.removeDownloadResult", [gid])
                        except Exception:
                            pass

                        threading.Thread(
                            target=handle_expired_link,
                            args=(original_magnet, yaml_path),
                            daemon=True,
                        ).start()
                        error_triggered = True
                        break
                    else:
                        # Clean up the broken task from the daemon's stack
                        try:
                            call_aria2_rpc("aria2.removeDownloadResult", [gid])
                        except Exception:
                            pass

                        if retry_count < max_retries:
                            retry_count += 1
                            tqdm.write(
                                f"⚠️ Download failed for {download_filename} (RPC Code: {error_code}). Retrying ({retry_count}/{max_retries})..."
                            )
                            time.sleep(2)
                            attempt_failed = True
                            break
                        else:
                            if yaml_path:
                                update_yaml_field(
                                    yaml_path,
                                    original_magnet,
                                    {"status": f"FAILED: aria2 RPC Error Code {error_code}"},
                                )
                            tqdm.write(
                                f"\n❌ Download dropped permanently for {download_filename} (RPC Code: {error_code})"
                            )
                            error_triggered = True
                            break

                elif status == "removed":
                    error_triggered = True
                    break

            if error_triggered or status == "complete":
                break

            if attempt_failed:
                continue

        pbar.close()

        if not error_triggered:
            tqdm.write(f"✅ Downloaded: {download_filename}")
            extract_and_cleanup(full_path, pbar)
            if yaml_path:
                update_yaml_field(yaml_path, original_magnet, {"status": "DONE"})

            # Clean complete job list footprint from the server session allocation map
            try:
                call_aria2_rpc("aria2.removeDownloadResult", [gid])
            except Exception:
                pass
