# 📖 Webtor Parallel Downloader

A high-performance Python script that automates **Webtor.io** to generate `curl` download commands for magnet links. It features a sequential browser scraper to avoid clipboard conflicts and a parallel background downloader with real-time progress bars.

## ✨ Features

* **Sequential Scraping**: One browser instance handles links one-by-one to ensure the system clipboard never gets "raced" or corrupted.
* **Parallel Downloads**: Background workers download multiple files simultaneously using `curl`.
* **Smart Resume**: Uses `curl -C -` to automatically resume interrupted downloads.
* **Auto-Extraction**: Extracts ZIP files immediately upon completion and cleans up the original archive.
* **CRC Resilience**: Bypasses CRC-32 check errors (common with Webtor's "on-the-fly" ZIP generation).
* **Headless Support**: Designed to run on servers using `xvfb`.
* **Beautiful UI**: Stacked progress bars showing percentage, downloaded size, and transfer speed.

---

## 🚀 Installation

### 1. Prerequisites
Ensure you have Python 3.13+, `curl` installed on your system. The `xvfb` is also recommended to have headless mode while passing bot check.

### 2. Setup with `uv` (Recommended)
This script uses `uv` for ultra-fast dependency management.
```bash
# Install uv if you haven't
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Playwright browsers
uv run playwright install chromium
```

### 3. Manual Installation (Pip)
```bash
pip install playwright playwright-stealth pyperclip tqdm
playwright install chromium
```

---

## 🛠 Usage

### Single Magnet Link
```bash
xvfb-run --auto-servernum uv run main.py "magnet:?xt=urn:btih:..."
```

### Batch Processing from File
Create a `links.txt` with one magnet link per line:
```bash
xvfb-run --auto-servernum uv run main.py --file links.txt
```

### Custom Download Folder
The default is `~/Downloads`. To change it:
```bash
xvfb-run --auto-servernum uv run main.py "magnet:?xt=urn:btih:..." "/path/to/custom/folder"
xvfb-run --auto-servernum uv run main.py --file links.txt "/path/to/custom/folder"
```

you can also use provided download.sh for convinence.

```bash
./download.sh "magnet:?xt=urn:btih:..."
./download.sh "magnet:?xt=urn:btih:..." "/path/to/custom/folder"
./download.sh  --file links.txt
./download.sh  --file links.txt "/path/to/custom/folder"
```

---

## ⚙️ Configuration

You can adjust the following variables directly in `main.py`:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | Number of files to download at the same time. |
| `target_folder` | `~/Downloads` | The default directory for saved files. |
| `time.sleep(2)` | `2` | Buffer for clipboard synchronization. |

---

## 📝 Important Notes

* **First Run & Captchas**: If you encounter Cloudflare captchas, run the script once without `xvfb` on your local machine to solve the challenge. The session will be saved in `./webtor_session`.
* **Webtor ZIPs**: This script ignores CRC errors. This is intentional, as Webtor generates ZIP footers dynamically, which often triggers false-positive corruption flags in standard extraction tools.
* **Clipboard**: The script uses the system clipboard. Avoid copying/pasting other text while the **Scraper** phase (the part opening the browser) is active. Once the downloads start, you can use your clipboard freely.

---

## 📜 License
MIT License. Use responsibly.
