import os
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import unquote

import typer
from playwright.sync_api import sync_playwright
from result import Err, Ok
from tqdm import tqdm
from typing_extensions import Annotated

import config
from downloader import run_aria2_download
from scraper import get_browser_context, scrape_webtor_url
from utils import load_yaml, update_yaml_field

app = typer.Typer()


def is_rpc_server_running() -> bool:
    """Checks if the aria2 RPC port is currently open and responding."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        try:
            s.connect((config.ARIA2_RPC_HOST, config.ARIA2_RPC_PORT))
            return True
        except (socket.timeout, ConnectionRefusedError):
            return False


def ensure_aria2_rpc_daemon():
    """Checks for an active aria2 RPC server instance, spinning one up if missing."""
    if is_rpc_server_running():
        tqdm.write("ℹ️ aria2 RPC server is already running. Reusing active instance.")
        return

    tqdm.write("⚙️ aria2 RPC server not detected. Initializing daemon in background...")

    aria_cmd = [
        "aria2c",
        "--enable-rpc",
        f"--rpc-listen-port={config.ARIA2_RPC_PORT}",
        "--rpc-listen-all=false",
        f"--rpc-secret={config.ARIA2_RPC_SECRET}",
        "--daemon=true",  # Automatically detaches the process to run cleanly in the background
    ]

    try:
        # We use Popen so it runs independently without blocking the script execution line
        subprocess.Popen(
            aria_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Keeps process alive even if parent script exits completely
        )

        # Give the background daemon a quick window to bind to the network socket port
        for _ in range(5):
            time.sleep(0.5)
            if is_rpc_server_running():
                tqdm.write(
                    "⚡ aria2 RPC server successfully started and bound to port."
                )
                return

        tqdm.write("❌ Critical: aria2 executable ran but port failed to open.")
    except FileNotFoundError:
        tqdm.write(
            "❌ Error: 'aria2c' binary was not found on your system PATH. Please install it."
        )
        raise typer.Exit(code=1)


def get_pending_items(all_entries) -> list[Any]:
    """filter items that aren't already DONE and have a magnet link"""
    pending_items = [
        item
        for item in all_entries
        if item.get("status") not in ["DONE", "RETRIED"] and item.get("magnet")
    ]
    return pending_items


def download_cached(
    pending_items: list[Any],
    executor: ThreadPoolExecutor,
    target_folder: str,
    yaml_path: str,
) -> None:
    for item in pending_items:
        if item.get("download_url"):
            tqdm.write(f"⚡ Cached: {item.get('title')[:20]}...")
            executor.submit(
                run_aria2_download,
                item["download_url"],
                target_folder,
                item["magnet"],
                yaml_path,
            )


def scrape_n_download(
    pending_items: list[Any],
    executor: ThreadPoolExecutor,
    target_folder: str,
    yaml_path: Optional[str] = None,
) -> None:
    items_to_scrape = [i for i in pending_items if not i.get("download_url")]
    if items_to_scrape:
        with sync_playwright() as pl:
            browser, page = get_browser_context(pl)

            for item in items_to_scrape:
                m = item["magnet"]
                title = item.get("title", "Unknown")

                match scrape_webtor_url(page, m, title):
                    case Ok(captured_url):
                        # Save it under download_url key to minimize rewriting the YAML architecture
                        if yaml_path:
                            update_yaml_field(
                                yaml_path, m, {"download_url": captured_url}
                            )
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
    ensure_aria2_rpc_daemon()  # Ensure server is up before doing work
    config.global_target_folder = os.path.expanduser(target_folder)

    all_entries = load_yaml(yaml_path)
    pending_items = get_pending_items(all_entries)
    if not pending_items:
        print("✅ No pending items.")
        return

    tqdm.write(f"⚙️ Found {len(pending_items)} pending items.")

    with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_DOWNLOADS) as executor:
        config.global_executor = (
            executor  # Keep references accessible to asynchronous link re-grabs
        )
        download_cached(pending_items, executor, config.global_target_folder, yaml_path)
        scrape_n_download(
            pending_items, executor, config.global_target_folder, yaml_path
        )

    tqdm.write("🏁 Processing finished.")


@app.command()
def link(
    magnet_link: str,
    target_folder: Annotated[str, typer.Option("--target", "-t")] = "~/Downloads",
):
    ensure_aria2_rpc_daemon()  # Ensure server is up before doing work
    config.global_target_folder = os.path.expanduser(target_folder)

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
        with ThreadPoolExecutor(max_workers=1) as executor:
            config.global_executor = executor
            run_aria2_download(download_link, config.global_target_folder, magnet_link)
    tqdm.write("🏁 Processing finished.")


if __name__ == "__main__":
    app()
