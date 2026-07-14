import os
import zipfile
from typing import Any
from urllib.parse import unquote

import yaml
from tqdm import tqdm

from config import file_modify_lock


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
    Updates multiple fields (like status and download_url) for a specific magnet.
    'updates' should be a dictionary like {'status': 'DONE', 'download_url': '...'}
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
        pbar.write(f"❌ Extraction failed: {zip_path} not found.")
        return

    if not zipfile.is_zipfile(zip_path):
        pbar.write(f"ℹ️ Downloaded file is not a ZIP archive. Skipping extraction: {zip_path}")
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
                    pbar.write(
                        f"\n⚠️ Skipping corrupt file inside ZIP ({member.filename}): {e}"
                    )
                    continue

        # We delete the ZIP even if some internal files were corrupt,
        # as requested ("ignore CRC check error after extraction completed").
        os.remove(zip_path)
        pbar.write(f"🗑️ Deleted original ZIP: {zip_path}")
        pbar.set_description(f"✅ Finished: {os.path.basename(extract_to)[:20]}")
    except Exception as e:
        pbar.write(f"❌ Critical ZIP extraction error: {e}")


def get_filename_from_url(url: str) -> str:
    """Extracts a clean filename from the Webtor download URL."""
    try:
        clean_url = url.split("?")[0].split("#")[0]
        filename = clean_url.rstrip("/").split("/")[-1]
        decoded_filename = unquote(filename)
        return decoded_filename if decoded_filename else "download.zip"
    except Exception:
        return "download.zip"
