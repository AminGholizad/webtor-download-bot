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


def run_aria2_download(
    download_url: str,
    target_dir: str,
    original_magnet: str,
    yaml_path: Optional[str] = None,
):
    current_url = download_url
    target_dir = os.path.abspath(target_dir)
    os.makedirs(target_dir, exist_ok=True)

    max_retries = 10
    retry_count = 0

    while True:
        need_refresh = False
        error_triggered = False
        completed = False
        gid = None
        pbar = None

        with config.slot_manager as slot:
            download_filename = get_filename_from_url(current_url)
            full_path = os.path.join(target_dir, download_filename)

            # Initialize TQDM bar for this specific download
            # position=slot + 1 to leave room for general logs at the top
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

            try:
                # Tell aria2 to add the direct HTTP URI download assignment
                gid = call_aria2_rpc("aria2.addUri", [[current_url], options])
            except Exception as e:
                pbar.close()
                if retry_count < max_retries:
                    retry_count += 1
                    tqdm.write(f"⚠️ RPC failed to add download for {download_filename}: {e}. Retrying ({retry_count}/{max_retries})...")
                    time.sleep(2)
                    continue
                else:
                    if yaml_path:
                        update_yaml_field(
                            yaml_path, original_magnet, {"status": "FAILED: RPC Startup Error"}
                        )
                    tqdm.write(f"❌ RPC failed to add download: {e}")
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
                    diff = completed_length - pbar.n
                    if diff > 0:
                        pbar.update(diff)

                if status == "complete":
                    completed = True
                    break

                elif status == "error":
                    error_code = int(status_info.get("errorCode", 0))
                    # Clean up the broken task from the daemon's stack
                    try:
                        call_aria2_rpc("aria2.removeDownloadResult", [gid])
                    except Exception:
                        pass

                    # Error code 1 = An unknown error occurred / stream dropped
                    # Error code 22 = HTTP response status code was unacceptable (401/403/Expired token)
                    if error_code in [1, 22]:
                        tqdm.write(
                            f"⚠️ Link validation failed via RPC (Code {error_code}) for {download_filename}."
                        )
                        need_refresh = True
                        error_triggered = True
                        break
                    else:
                        if retry_count < max_retries:
                            retry_count += 1
                            tqdm.write(
                                f"⚠️ Download failed for {download_filename} (RPC Code: {error_code}). Retrying ({retry_count}/{max_retries})...."
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

            if attempt_failed:
                pbar.close()
                continue

            if completed:
                tqdm.write(f"✅ Downloaded: {download_filename}")
                extract_and_cleanup(full_path, pbar)
                pbar.close()
                if yaml_path:
                    update_yaml_field(yaml_path, original_magnet, {"status": "DONE"})

                # Clean complete job list footprint from the server session allocation map
                try:
                    call_aria2_rpc("aria2.removeDownloadResult", [gid])
                except Exception:
                    pass
                return

            pbar.close()

            if error_triggered and not need_refresh:
                return

        # Outside the SlotManager context: release slot while scraping fresh URL
        if need_refresh:
            tqdm.write(f"🔄 Link expired or timed out. Refreshing token for magnet...")
            with sync_playwright() as pl:
                browser, page = get_browser_context(pl)
                match = re.search(r"dn=([^&]+)", original_magnet)
                title = unquote(match.group(1)).replace("+", " ") if match else "Expired Item"

                scrape_result = scrape_webtor_url(page, original_magnet, title)
                browser.close()

            match scrape_result:
                case Ok(fresh_url):
                    tqdm.write(f"✨ Fresh URL retrieved successfully for: {title[:20]}")
                    if yaml_path:
                        update_yaml_field(
                            yaml_path,
                            original_magnet,
                            {"download_url": fresh_url},
                        )
                    current_url = fresh_url
                    continue
                case Err(e):
                    tqdm.write(f"❌ Could not auto-refresh link: {e}")
                    if yaml_path:
                        update_yaml_field(
                            yaml_path,
                            original_magnet,
                            {"status": f"FAILED: Refresh failed ({e})"},
                        )
                    return
