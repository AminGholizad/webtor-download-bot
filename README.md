# 📖 Webtor Parallel Downloader

A high-performance Python automation tool managed by **uv** that transforms **Webtor.io** into a powerful CLI download manager. It features metadata-aware task management, command caching, and parallel background downloads with real-time progress tracking.

## ✨ Features

* **YAML-based Task Management**: Track your library with structured metadata (Title, Quality, Size, Magnet).
* **Command Caching**: Automatically saves scraped `curl` commands to the YAML file. If you restart the script, it skips the browser phase and resumes downloads instantly.
* **Smart Resuming**: Automatically injects `curl -C -` to pick up exactly where an interrupted download left off.
* **Parallel Downloads**: Multi-threaded background workers handle up to `N` downloads simultaneously with stacked progress bars.
* **Auto-Extraction & Cleanup**: Unzips archives immediately upon completion and deletes the original `.zip` to save disk space.
* **CRC Resilience**: Bypasses Webtor's "on-the-fly" ZIP CRC-32 errors during extraction.
* **Headless-Ready**: Optimized for servers using `xvfb` to bypass bot detection.
* **Beautiful UI**: Stacked progress bars showing percentage, downloaded size, and transfer speed.

---

## 🚀 Installation

### 1. Prerequisites
Ensure you have Python 3.13+ and `curl` installed. For headless environments (Linux servers), `xvfb` is required.

### 2. Setup with `uv` (Recommended)
This script uses `uv` for ultra-fast dependency management.
```bash
# Install uv if you haven't
curl -LsSf https://astral.sh/uv/install.sh | sh

# Sync dependencies and install browsers
uv sync
uv run playwright install chromium
```

### 3. Manual Installation (Pip)
```bash
pip install playwright playwright-stealth pyperclip tqdm
playwright install chromium
```

---

📂 YAML Structure

The script expects a .yaml file for batch processing. This allows the bot to track which files are DONE and store the curl_cmd for future use.

```YAML

- title: Some Title
  size: 1.21 GB
  magnet: magnet:?xt=urn:btih:417EC...
```
  
---

## 🛠 Usage

### Batch Processing (Recommended)
The script will iterate through the YAML, skip finished items, and download pending ones.
```bash
xvfb-run --auto-servernum uv run main.py --file links.yaml
```

### Single Magnet Link
```bash
xvfb-run --auto-servernum uv run main.py "magnet:?xt=urn:btih:..."
```

### Custom Download Folder
The default is `~/Downloads`. To change it:
```bash
xvfb-run --auto-servernum uv run main.py "magnet:?xt=urn:btih:..." "/path/to/custom/folder"
xvfb-run --auto-servernum uv run main.py --file links.yaml "/path/to/custom/folder"
```

### Convenience Script
You can also use the provided `download.sh`:
```bash
./download.sh "magnet:?xt=urn:btih:..."
./download.sh "magnet:?xt=urn:btih:..." "/path/to/custom/folder"
./download.sh  --file links.yaml
./download.sh  --file links.yaml "/path/to/custom/folder"
```

---

## ⚙️ Configuration

Adjust these settings at the top of `main.py`:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | Number of simultaneous file transfers. |
| `target_folder` | `~/Downloads` | Default directory if not specified in CLI. |

---

## 🧠 Logic Flow

1.  **Load YAML**: Reads the list of links and identifies items where `status != "DONE"`.
2.  **Check Cache**: If an item already has a `curl_cmd` saved in the YAML, it fires the download thread immediately.
3.  **Scrape**: For items without a cached command, it opens Playwright, navigates Webtor, and extracts the curl command via the clipboard.
4.  **Update YAML**: The `curl_cmd` is saved back to the file instantly so you never have to scrape it twice.
5.  **Download & Extract**: `curl` handles the data transfer, and Python handles the unzipping and status updates.

---

## 📝 Important Notes

* **Progress Bars**: The UI uses `tqdm.write()` to ensure logs don't break the stacked progress bars. Ensure your terminal window is large enough to show one bar per concurrent download.
* **Captchas**: If blocked by Cloudflare, run once on a local machine (without `xvfb`) to solve the challenge. The session is preserved in `./webtor_session`.
* **Persistence**: Do not manually edit the `status` or `curl_cmd` fields in the YAML while the script is running.
* **Webtor ZIPs**: This script ignores CRC errors. This is intentional, as Webtor generates ZIP footers dynamically, which often triggers false-positive corruption flags in standard extraction tools.
* **Clipboard**: The script uses the system clipboard. Avoid copying/pasting other text while the **Scraper** phase (the part opening the browser) is active. Once the downloads start, you can use your clipboard freely.

---
